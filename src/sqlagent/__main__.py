"""Command line entry point.

python -m sqlagent ask "Which category has the highest revenue?"
python -m sqlagent serve
python -m sqlagent eval [--oracle] [--tpm 8000] [--fresh] [--only ID ...]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from .agent import SQLAgent
from .config import ROOT, ConfigError, Settings, app_config, build_agent, build_workspace, load_dotenv
from .dataquality import run_checks
from .db import ReadOnlyDatabase
from .evaluation import CaseOutcome, OracleLLM, TokenPacer, load_cases, render_report, run_eval, summarize


def cmd_ask(args: argparse.Namespace) -> int:
    result = build_agent().ask(args.question)
    for i, step in enumerate(result.steps, 1):
        marker = "ok" if step.ok else "error"
        print(f"[{i}] {step.tool} ({marker})")
        if step.sql:
            print(f"    {step.sql}")
        if step.error:
            print(f"    {step.error}")
    print()
    print(f"Status: {result.status}")
    if result.answer:
        print(f"Answer: {result.answer}")
    if result.error:
        print(f"Error:  {result.error}")
    if result.columns:
        print(" | ".join(result.columns))
        for row in result.rows[:20]:
            print(" | ".join("NULL" if v is None else str(v) for v in row))
    print(
        f"\n{result.llm_calls} model calls, {result.input_tokens + result.output_tokens} tokens, "
        f"{result.latency_ms / 1000:.1f} s"
    )
    return 0 if result.status in ("answered", "cannot_answer") else 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app

    settings = Settings.from_env()
    port = args.port or int(os.environ.get("PORT", "8000"))
    uvicorn.run(create_app(build_agent(settings), app_config(), build_workspace(settings)), host=args.host, port=port)
    return 0


def cmd_check_data(args: argparse.Namespace) -> int:
    load_dotenv(ROOT / ".env")
    db = ReadOnlyDatabase(Path(args.db or os.environ.get("DB_PATH") or ROOT / "data" / "olist.db"))
    results = run_checks(db)
    for check in results:
        print(f"{'ok  ' if check.passed else 'FAIL'} {check.name}: {check.detail}")
    failed = sum(not c.passed for c in results)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


def cmd_eval(args: argparse.Namespace) -> int:
    cases = load_cases(args.questions)
    if args.only:
        cases = [c for c in cases if c.id in set(args.only)]
    if args.oracle:
        load_dotenv(ROOT / ".env")
        db_path = Path(args.db or os.environ.get("DB_PATH") or ROOT / "data" / "olist.db")
        agent = SQLAgent(OracleLLM(cases), ReadOnlyDatabase(db_path))
        results_path = None
    else:
        agent = build_agent(Settings.from_env())
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", agent.llm.model).strip("-").lower()
        results_path = Path(args.out) / f"{slug}.jsonl"

    def progress(outcome: CaseOutcome) -> None:
        print(
            f"{outcome.id:8} {outcome.verdict:16} {outcome.llm_calls} calls  {outcome.tokens:>6} tok  "
            f"{outcome.latency_ms / 1000:5.1f} s",
            flush=True,
        )

    pacer = TokenPacer(args.tpm) if args.tpm and not args.oracle else None
    outcomes = run_eval(agent, cases, results_path, pacer, resume=not args.fresh, on_case=progress)
    summary = summarize(outcomes)
    print(f"\nExecution accuracy: {summary['correct']}/{summary['total']} ({summary['accuracy']}%)")
    if results_path:
        report_path = results_path.with_suffix(".md")
        report_path.write_text(render_report(outcomes, agent.llm.model), encoding="utf-8")
        print(f"Report: {report_path}")
    if args.oracle:
        return 0 if summary["correct"] == summary["total"] else 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sqlagent", description="Text-to-SQL agent for the Olist dataset.")
    sub = parser.add_subparsers(dest="command", required=True)

    ask = sub.add_parser("ask", help="answer one question")
    ask.add_argument("question")
    ask.set_defaults(func=cmd_ask)

    serve = sub.add_parser("serve", help="start the web app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0, help="default: $PORT or 8000")
    serve.set_defaults(func=cmd_serve)

    check = sub.add_parser("check-data", help="verify the database against the assumptions the prompt relies on")
    check.add_argument("--db", default="")
    check.set_defaults(func=cmd_check_data)

    evaluate = sub.add_parser("eval", help="run the reference questions")
    evaluate.add_argument("--questions", default=str(ROOT / "evals" / "questions.json"))
    evaluate.add_argument("--out", default=str(ROOT / "evals" / "results"))
    evaluate.add_argument("--db", default="", help="database path (oracle mode)")
    evaluate.add_argument("--tpm", type=int, default=0, help="tokens-per-minute budget, e.g. 8000 on Groq's free plan")
    evaluate.add_argument("--fresh", action="store_true", help="ignore saved results and start over")
    evaluate.add_argument("--only", nargs="*", help="run only these case ids")
    evaluate.add_argument("--oracle", action="store_true", help="self-test with the gold SQL; no API key needed")
    evaluate.set_defaults(func=cmd_eval)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
