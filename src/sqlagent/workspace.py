"""Saved dashboards: the app's own, writable database.

The analytics database is opened read-only and never changes. Dashboards live in a
separate SQLite file (``APP_DB_PATH``, default ``data/app.db``) with three tables:

    dashboards   one row per dashboard, with its current version number
    tiles        the current tiles of each dashboard, in order
    revisions    a full snapshot after every change, so any version can be restored

Every change, by a person or by the agent, goes through ``save``: it checks the version
the change was based on (so two editors cannot silently overwrite each other), replaces
the tiles, and records one revision with a summary of what changed.

The editing operations at the bottom work on an in-memory copy and validate each tile's
SQL and chart type before accepting it. The HTTP API and the dashboard agent share them.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import secrets
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .db import QueryError, ReadOnlyDatabase
from .presentation import CHART_TYPES, ChartMismatch, chart_for

SIZES = ("half", "full")
MAX_TILES = 24
MAX_DASHBOARDS = 200
MAX_TITLE = 120
MAX_DESCRIPTION = 400
MAX_NOTE = 240
MAX_SQL = 4000
MAX_TEXT = 2000  # body of a text tile
TEXT = "text"  # the chart value of a text tile, which has no query
TILE_PREVIEW_ROWS = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS dashboards (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  filters TEXT NOT NULL DEFAULT '{}',
  version INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tiles (
  id TEXT NOT NULL,
  dashboard_id TEXT NOT NULL REFERENCES dashboards(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  title TEXT NOT NULL,
  sql TEXT NOT NULL,
  chart TEXT NOT NULL,
  size TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (dashboard_id, id)
);
CREATE TABLE IF NOT EXISTS revisions (
  dashboard_id TEXT NOT NULL REFERENCES dashboards(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  author TEXT NOT NULL,
  summary TEXT NOT NULL,
  snapshot TEXT NOT NULL,
  PRIMARY KEY (dashboard_id, version)
);
"""


class NotFound(LookupError):
    pass


class Conflict(Exception):
    """The dashboard changed since the version the edit was based on."""


class EditError(ValueError):
    """An edit that was refused. The message is shown to the person or returned to the model."""


class MissingTile(EditError):
    """The edit names a tile that is not on the dashboard."""


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


@dataclass
class Tile:
    id: str
    title: str
    sql: str
    chart: str = "auto"
    size: str = "half"
    note: str = ""


@dataclass
class Dashboard:
    id: str
    title: str
    description: str = ""
    tiles: list[Tile] = field(default_factory=list)
    filters: dict[str, str] = field(default_factory=dict)
    version: int = 0
    created_at: str = ""
    updated_at: str = ""

    def tile(self, tile_id: str) -> Tile:
        for tile in self.tiles:
            if tile.id == tile_id:
                return tile
        known = ", ".join(t.id for t in self.tiles) or "none"
        raise MissingTile(f"No tile with id {tile_id!r}. Tiles on this dashboard: {known}.")

    def snapshot(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "filters": dict(self.filters),
            "tiles": [asdict(t) for t in self.tiles],
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Workspace:
    """Thread-safe access to the app database."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after the first release to an existing app database."""
        columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(dashboards)")}
        if "filters" not in columns:
            with self._conn:
                self._conn.execute("ALTER TABLE dashboards ADD COLUMN filters TEXT NOT NULL DEFAULT '{}'")

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------- reading

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT d.id, d.title, d.description, d.version, d.updated_at, COUNT(t.id) AS tile_count "
                "FROM dashboards d LEFT JOIN tiles t ON t.dashboard_id = d.id "
                "GROUP BY d.id ORDER BY d.updated_at DESC, d.id"
            ).fetchall()
            shapes = self._conn.execute(
                "SELECT dashboard_id, chart, size FROM tiles ORDER BY dashboard_id, position"
            ).fetchall()
        layout: dict[str, list[dict[str, str]]] = {}
        for shape in shapes:
            layout.setdefault(shape["dashboard_id"], []).append({"chart": shape["chart"], "size": shape["size"]})
        # The tile shapes, in order, so a list can draw a thumbnail of each dashboard.
        return [{**dict(r), "layout": layout.get(r["id"], [])} for r in rows]

    def get(self, dashboard_id: str) -> Dashboard:
        with self._lock:
            row = self._conn.execute("SELECT * FROM dashboards WHERE id = ?", (dashboard_id,)).fetchone()
            if row is None:
                raise NotFound(f"No dashboard {dashboard_id!r}.")
            tiles = self._conn.execute(
                "SELECT id, title, sql, chart, size, note FROM tiles WHERE dashboard_id = ? ORDER BY position",
                (dashboard_id,),
            ).fetchall()
        return Dashboard(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            tiles=[Tile(**dict(t)) for t in tiles],
            filters=json.loads(row["filters"] or "{}"),
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def revisions(self, dashboard_id: str) -> list[dict[str, Any]]:
        self.get(dashboard_id)
        with self._lock:
            rows = self._conn.execute(
                "SELECT version, created_at, author, summary, snapshot FROM revisions "
                "WHERE dashboard_id = ? ORDER BY version DESC",
                (dashboard_id,),
            ).fetchall()
        return [
            {
                "version": r["version"],
                "created_at": r["created_at"],
                "author": r["author"],
                "summary": r["summary"],
                "tile_count": len(json.loads(r["snapshot"])["tiles"]),
            }
            for r in rows
        ]

    # ------------------------------------------------------------- writing

    def create(self, title: str, description: str = "", author: str = "you", summary: str = "") -> Dashboard:
        with self._lock:
            count = self._conn.execute("SELECT COUNT(*) FROM dashboards").fetchone()[0]
        if count >= MAX_DASHBOARDS:
            raise EditError(f"The workspace already has {MAX_DASHBOARDS} dashboards. Delete one first.")
        dashboard = Dashboard(id=new_id("d"), title=clean_text(title, MAX_TITLE, "Title") or "Untitled dashboard")
        dashboard.description = clean_text(description, MAX_DESCRIPTION, "Description")
        stamp = now()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO dashboards (id, title, description, version, created_at, updated_at) "
                "VALUES (?, ?, ?, 0, ?, ?)",
                (dashboard.id, dashboard.title, dashboard.description, stamp, stamp),
            )
        return self.save(dashboard, author=author, summary=summary or "Created the dashboard", expected_version=0)

    def save(self, dashboard: Dashboard, author: str, summary: str, expected_version: int | None = None) -> Dashboard:
        """Write the dashboard as a new version. Raises Conflict if someone saved in between."""
        stamp = now()
        with self._lock, self._conn:
            row = self._conn.execute("SELECT version FROM dashboards WHERE id = ?", (dashboard.id,)).fetchone()
            if row is None:
                raise NotFound(f"No dashboard {dashboard.id!r}.")
            current = row["version"]
            if expected_version is not None and current != expected_version:
                raise Conflict(
                    f"The dashboard changed while you were editing (now version {current}). Reload and try again."
                )
            version = current + 1
            self._conn.execute(
                "UPDATE dashboards SET title = ?, description = ?, filters = ?, version = ?, updated_at = ? "
                "WHERE id = ?",
                (dashboard.title, dashboard.description, json.dumps(dashboard.filters), version, stamp, dashboard.id),
            )
            self._conn.execute("DELETE FROM tiles WHERE dashboard_id = ?", (dashboard.id,))
            self._conn.executemany(
                "INSERT INTO tiles (id, dashboard_id, position, title, sql, chart, size, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (t.id, dashboard.id, i, t.title, t.sql, t.chart, t.size, t.note)
                    for i, t in enumerate(dashboard.tiles)
                ],
            )
            self._conn.execute(
                "INSERT INTO revisions (dashboard_id, version, created_at, author, summary, snapshot) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (dashboard.id, version, stamp, author, summary[:300], json.dumps(dashboard.snapshot())),
            )
        return self.get(dashboard.id)

    def restore(self, dashboard_id: str, version: int, author: str = "you") -> Dashboard:
        with self._lock:
            row = self._conn.execute(
                "SELECT snapshot FROM revisions WHERE dashboard_id = ? AND version = ?", (dashboard_id, version)
            ).fetchone()
        if row is None:
            raise NotFound(f"Dashboard {dashboard_id!r} has no version {version}.")
        snapshot = json.loads(row["snapshot"])
        dashboard = self.get(dashboard_id)
        dashboard.title, dashboard.description = snapshot["title"], snapshot["description"]
        dashboard.tiles = [Tile(**t) for t in snapshot["tiles"]]
        dashboard.filters = snapshot.get("filters", {})
        return self.save(dashboard, author=author, summary=f"Restored version {version}")

    def duplicate(self, dashboard_id: str, author: str = "you") -> Dashboard:
        """A new dashboard with the same tiles and filters, titled 'Copy of ...'."""
        source = self.get(dashboard_id)
        title = f"Copy of {source.title}"[:MAX_TITLE]
        copy_ = self.create(title, source.description, author=author, summary=f"Copied from “{source.title}”")
        copy_.tiles = [Tile(**{**asdict(t), "id": new_id("t")}) for t in source.tiles]
        copy_.filters = dict(source.filters)
        return self.save(
            copy_,
            author=author,
            summary=f"Copied {len(copy_.tiles)} tile{'' if len(copy_.tiles) == 1 else 's'}",
            expected_version=copy_.version,
        )

    def delete(self, dashboard_id: str) -> None:
        with self._lock, self._conn:
            deleted = self._conn.execute("DELETE FROM dashboards WHERE id = ?", (dashboard_id,)).rowcount
        if not deleted:
            raise NotFound(f"No dashboard {dashboard_id!r}.")


# ---------------------------------------------------------------- editing


def clean_text(value: Any, limit: int, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise EditError(f"{label} must be text.")
    text = " ".join(value.split())
    if len(text) > limit:
        raise EditError(f"{label} is longer than {limit} characters.")
    return text


def clean_body(value: Any, limit: int, label: str) -> str:
    """Multi-line text: keeps line breaks, trims each line, at most one blank line in a row."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise EditError(f"{label} must be text.")
    lines = [" ".join(line.split()) for line in value.replace("\r\n", "\n").split("\n")]
    text = "\n".join(lines)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    text = text.strip()
    if len(text) > limit:
        raise EditError(f"{label} is longer than {limit} characters.")
    return text


@dataclass
class Change:
    kind: str  # added | updated | removed | moved | renamed | filtered
    title: str
    tile_id: str | None = None


def check_tile(db: ReadOnlyDatabase, sql: str, chart: str) -> tuple[str, dict[str, Any], list[str], list[tuple]]:
    """Run the SQL through the guardrails and check the chart type fits. Returns (clean SQL, chart, columns, rows)."""
    if not isinstance(sql, str) or not sql.strip():
        raise EditError("A tile needs a SQL query.")
    if len(sql) > MAX_SQL:
        raise EditError(f"The query is longer than {MAX_SQL} characters.")
    if chart not in CHART_TYPES:
        raise EditError(f"Unknown chart type {chart!r}. Use one of: {', '.join(CHART_TYPES)}.")
    try:
        result = db.execute(sql, max_rows=TILE_PREVIEW_ROWS)
    except QueryError as exc:
        raise EditError(exc.message) from exc
    try:
        payload = chart_for(result.columns, result.rows, chart)
    except ChartMismatch as exc:
        raise EditError(f"{exc} The query returned columns {result.columns} and {result.row_count} rows.") from exc
    return result.sql, payload, result.columns, result.rows


def default_size(payload: dict[str, Any], columns: list[str]) -> str:
    if payload["type"] == "kpi":
        return "full" if len(payload["items"]) > 2 else "half"
    if payload["type"] == "line" or (payload["type"] == "table" and len(columns) > 3):
        return "full"
    return "half"


def _position(value: Any, length: int) -> int:
    if value is None:
        return length
    if not isinstance(value, int) or isinstance(value, bool):
        raise EditError("position must be a whole number.")
    return max(0, min(value, length))


def add_tile(
    dashboard: Dashboard,
    db: ReadOnlyDatabase,
    title: Any,
    sql: Any,
    chart: Any = "auto",
    size: Any = None,
    note: Any = "",
    position: Any = None,
) -> Change:
    if len(dashboard.tiles) >= MAX_TILES:
        raise EditError(f"A dashboard holds at most {MAX_TILES} tiles. Remove one first.")
    title = clean_text(title, MAX_TITLE, "Title")
    if not title:
        raise EditError("A tile needs a title.")
    chart = chart or "auto"
    clean_sql, payload, columns, _ = check_tile(db, sql, chart)
    if size not in (None, *SIZES):
        raise EditError("size must be 'half' or 'full'.")
    tile = Tile(
        id=new_id("t"),
        title=title,
        sql=clean_sql,
        chart=chart,
        size=size or default_size(payload, columns),
        note=clean_text(note, MAX_NOTE, "Note"),
    )
    dashboard.tiles.insert(_position(position, len(dashboard.tiles)), tile)
    return Change("added", tile.title, tile.id)


def add_text_tile(dashboard: Dashboard, title: Any, text: Any, size: Any = "full", position: Any = None) -> Change:
    """A heading and short text, for sections and context. No query."""
    if len(dashboard.tiles) >= MAX_TILES:
        raise EditError(f"A dashboard holds at most {MAX_TILES} tiles. Remove one first.")
    title = clean_text(title, MAX_TITLE, "Title")
    body = clean_body(text, MAX_TEXT, "Text")
    if not title and not body:
        raise EditError("A text tile needs a title or some text.")
    if size not in (None, *SIZES):
        raise EditError("size must be 'half' or 'full'.")
    tile = Tile(id=new_id("t"), title=title, sql="", chart=TEXT, size=size or "full", note=body)
    dashboard.tiles.insert(_position(position, len(dashboard.tiles)), tile)
    return Change("added", tile.title or "Text", tile.id)


def update_tile(dashboard: Dashboard, db: ReadOnlyDatabase, tile_id: Any, **fields: Any) -> Change:
    tile = dashboard.tile(str(tile_id))
    updated = copy.copy(tile)
    if tile.chart == TEXT:
        if fields.get("sql") is not None or fields.get("chart") not in (None, TEXT):
            raise EditError("A text tile has no query or chart. Remove it and add a chart tile instead.")
        if fields.get("title") is not None:
            updated.title = clean_text(fields["title"], MAX_TITLE, "Title")
        if fields.get("note") is not None:
            updated.note = clean_body(fields["note"], MAX_TEXT, "Text")
        if fields.get("size") is not None:
            if fields["size"] not in SIZES:
                raise EditError("size must be 'half' or 'full'.")
            updated.size = fields["size"]
        if updated == tile:
            raise EditError("Nothing to change: give at least one new value.")
        dashboard.tiles[dashboard.tiles.index(tile)] = updated
        return Change("updated", updated.title or "Text", updated.id)
    if fields.get("title") is not None:
        updated.title = clean_text(fields["title"], MAX_TITLE, "Title") or tile.title
    if fields.get("note") is not None:
        updated.note = clean_text(fields["note"], MAX_NOTE, "Note")
    if fields.get("size") is not None:
        if fields["size"] not in SIZES:
            raise EditError("size must be 'half' or 'full'.")
        updated.size = fields["size"]
    if fields.get("sql") is not None or fields.get("chart") is not None:
        updated.chart = fields.get("chart") or tile.chart
        updated.sql, _, _, _ = check_tile(db, fields.get("sql") or tile.sql, updated.chart)
    if updated == tile:
        raise EditError("Nothing to change: give at least one new value.")
    dashboard.tiles[dashboard.tiles.index(tile)] = updated
    return Change("updated", updated.title, updated.id)


def remove_tile(dashboard: Dashboard, tile_id: Any) -> Change:
    tile = dashboard.tile(str(tile_id))
    dashboard.tiles.remove(tile)
    return Change("removed", tile.title, tile.id)


def move_tile(dashboard: Dashboard, tile_id: Any, position: Any) -> Change:
    tile = dashboard.tile(str(tile_id))
    dashboard.tiles.remove(tile)
    dashboard.tiles.insert(_position(position, len(dashboard.tiles)), tile)
    return Change("moved", tile.title, tile.id)


def update_details(dashboard: Dashboard, title: Any = None, description: Any = None) -> Change:
    if title is None and description is None:
        raise EditError("Give a new title or description.")
    if title is not None:
        dashboard.title = clean_text(title, MAX_TITLE, "Title") or dashboard.title
    if description is not None:
        dashboard.description = clean_text(description, MAX_DESCRIPTION, "Description")
    return Change("renamed", dashboard.title)


def set_filters(dashboard: Dashboard, filters: dict[str, str], label: str) -> Change:
    """Replace the dashboard's saved filters. ``filters`` must already be validated."""
    dashboard.filters = {k: v for k, v in filters.items() if v}
    return Change("filtered", label)


def summarize(changes: list[Change]) -> str:
    """'Added 2 tiles, removed "Orders by weekday"' style summary of a set of changes."""
    if not changes:
        return "No changes"
    parts = []
    for kind, verb in (("added", "Added"), ("updated", "Updated"), ("removed", "Removed"), ("moved", "Moved")):
        items = [c for c in changes if c.kind == kind]
        if len(items) == 1:
            parts.append(f"{verb} “{items[0].title}”")
        elif items:
            parts.append(f"{verb} {len(items)} tiles")
    if any(c.kind == "renamed" for c in changes):
        parts.append("Renamed the dashboard")
    filtered = [c for c in changes if c.kind == "filtered"]
    if filtered:
        parts.append(f"Set the filters to {filtered[-1].title}")
    text = ", ".join(parts)
    return text[0].upper() + text[1:]
