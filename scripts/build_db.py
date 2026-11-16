"""Build data/olist.db (SQLite) from the modelled Olist workbook.

The workbook is the star schema published in
https://github.com/ali-chamas/olist-powerbi-analysis (Olist_PowerBI_Data.xlsx).

    python scripts/build_db.py --download          # fetch the workbook, then build
    python scripts/build_db.py --xlsx path/to.xlsx  # build from a local copy
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlagent.catalog import TABLES, Table, create_index_sql, create_table_sql  # noqa: E402

WORKBOOK_URL = "https://raw.githubusercontent.com/ali-chamas/olist-powerbi-analysis/main/Olist_PowerBI_Data.xlsx"
BATCH = 5000


def to_sql_value(value: object, is_date: bool) -> object:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat() if is_date else value.isoformat(sep=" ")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, bool):
        return int(value)
    return value


def load_table(conn: sqlite3.Connection, workbook, table: Table) -> int:
    sheet = workbook[table.sheet]
    rows = sheet.iter_rows(values_only=True)
    header = [str(h) for h in next(rows)]
    missing = [c for c in table.column_names if c not in header]
    if missing:
        raise SystemExit(f"Sheet {table.sheet!r} is missing columns: {missing}")
    positions = [header.index(c.name) for c in table.columns]
    date_flags = [c.is_date for c in table.columns]

    conn.execute(create_table_sql(table))
    placeholders = ", ".join("?" for _ in table.columns)
    insert = f"INSERT INTO {table.name} VALUES ({placeholders})"

    count = 0
    batch: list[tuple] = []
    for row in rows:
        if row is None or all(v is None for v in row):
            continue
        batch.append(tuple(to_sql_value(row[p], d) for p, d in zip(positions, date_flags, strict=True)))
        if len(batch) >= BATCH:
            conn.executemany(insert, batch)
            count += len(batch)
            batch.clear()
    if batch:
        conn.executemany(insert, batch)
        count += len(batch)
    for statement in create_index_sql(table):
        conn.execute(statement)
    return count


def build(xlsx: Path, out: Path) -> dict[str, int]:
    import openpyxl  # imported here so the rest of the project does not need it

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".db.tmp")
    tmp.unlink(missing_ok=True)

    workbook = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    conn = sqlite3.connect(tmp)
    counts: dict[str, int] = {}
    try:
        with conn:
            for table in TABLES:
                counts[table.name] = load_table(conn, workbook, table)
        conn.execute("ANALYZE")
        conn.commit()
    finally:
        conn.close()
        workbook.close()
    tmp.replace(out)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--xlsx", type=Path, default=ROOT / "data" / "Olist_PowerBI_Data.xlsx")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "olist.db")
    parser.add_argument("--download", action="store_true", help="download the workbook first")
    args = parser.parse_args()

    if args.download and not args.xlsx.exists():
        args.xlsx.parent.mkdir(parents=True, exist_ok=True)
        print(f"Downloading {WORKBOOK_URL}")
        urllib.request.urlretrieve(WORKBOOK_URL, args.xlsx)  # noqa: S310 (fixed https URL)
    if not args.xlsx.exists():
        raise SystemExit(f"{args.xlsx} not found. Pass --download or --xlsx PATH.")

    counts = build(args.xlsx, args.out)
    for name, n in counts.items():
        print(f"{name:10} {n:>8,} rows")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
