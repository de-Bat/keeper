"""SQLite storage: items, tags, and a full-text index for retrieval."""

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    status          TEXT NOT NULL,          -- processing | ready | error
    error           TEXT,
    image_file      TEXT NOT NULL,
    note            TEXT,
    category        TEXT,                   -- movie | tv_show | github_repo | recipe | ...
    source_platform TEXT,                   -- facebook | instagram | web | ...
    title           TEXT,
    subtitle        TEXT,
    summary         TEXT,
    canonical_url   TEXT,
    image_url       TEXT,                   -- poster / cover / preview image
    metadata        TEXT NOT NULL DEFAULT '{}',
    links           TEXT NOT NULL DEFAULT '[]',
    analysis        TEXT                    -- raw model output, kept for debugging / re-enrichment
);
CREATE INDEX IF NOT EXISTS items_category ON items(category);
CREATE INDEX IF NOT EXISTS items_created ON items(created_at);

CREATE TABLE IF NOT EXISTS tags (
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    tag     TEXT NOT NULL,
    PRIMARY KEY (item_id, tag)
);
CREATE INDEX IF NOT EXISTS tags_tag ON tags(tag);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    item_id UNINDEXED, title, summary, tags, body, tokenize='unicode61 remove_diacritics 2'
);
"""

JSON_COLUMNS = ("metadata", "links", "analysis")
EDITABLE_COLUMNS = {
    "status", "error", "note", "category", "source_platform", "title", "subtitle",
    "summary", "canonical_url", "image_url", "metadata", "links", "analysis",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_tag(tag: str) -> str:
    tag = re.sub(r"\s+", "-", tag.strip().lower().lstrip("#"))
    return re.sub(r"[^\w\-+.]", "", tag)[:40]


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)

    # ---- items -------------------------------------------------------------

    def create_item(self, image_file: str, note: str | None = None, tags: list[str] | None = None) -> dict:
        item_id = uuid.uuid4().hex[:12]
        ts = now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO items (id, created_at, updated_at, status, image_file, note) VALUES (?, ?, ?, 'processing', ?, ?)",
                (item_id, ts, ts, image_file, note),
            )
        if tags:
            self.set_tags(item_id, tags)
        self._reindex(item_id)
        return self.get_item(item_id)

    def update_item(self, item_id: str, **fields: Any) -> dict | None:
        fields = {k: v for k, v in fields.items() if k in EDITABLE_COLUMNS}
        if fields:
            for col in JSON_COLUMNS:
                if col in fields and not isinstance(fields[col], str) and fields[col] is not None:
                    fields[col] = json.dumps(fields[col], ensure_ascii=False)
            fields["updated_at"] = now()
            assignments = ", ".join(f"{k} = ?" for k in fields)
            with self.conn:
                self.conn.execute(f"UPDATE items SET {assignments} WHERE id = ?", (*fields.values(), item_id))
            self._reindex(item_id)
        return self.get_item(item_id)

    def get_item(self, item_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return self._row_to_item(row) if row else None

    def delete_item(self, item_id: str) -> dict | None:
        item = self.get_item(item_id)
        if item:
            with self.conn:
                self.conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
                self.conn.execute("DELETE FROM items_fts WHERE item_id = ?", (item_id,))
        return item

    def list_items(
        self,
        q: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict]:
        where, params = [], []
        order = "i.created_at DESC"
        join = ""
        match = fts_query(q) if q else None
        if match:
            join = "JOIN items_fts f ON f.item_id = i.id"
            where.append("items_fts MATCH ?")
            params.append(match)
            order = "bm25(items_fts), i.created_at DESC"
        if category:
            where.append("i.category = ?")
            params.append(category)
        for tag in tags or []:
            where.append("EXISTS (SELECT 1 FROM tags t WHERE t.item_id = i.id AND t.tag = ?)")
            params.append(normalize_tag(tag))
        sql = f"SELECT i.* FROM items i {join}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
        rows = self.conn.execute(sql, (*params, limit, offset)).fetchall()
        return [self._row_to_item(r) for r in rows]

    # ---- tags --------------------------------------------------------------

    def set_tags(self, item_id: str, tags: list[str]) -> list[str]:
        clean = sorted({t for t in (normalize_tag(t) for t in tags) if t})
        with self.conn:
            self.conn.execute("DELETE FROM tags WHERE item_id = ?", (item_id,))
            self.conn.executemany("INSERT INTO tags (item_id, tag) VALUES (?, ?)", [(item_id, t) for t in clean])
        self._reindex(item_id)
        return clean

    def add_tags(self, item_id: str, tags: list[str]) -> list[str]:
        return self.set_tags(item_id, self.get_tags(item_id) + list(tags))

    def get_tags(self, item_id: str) -> list[str]:
        rows = self.conn.execute("SELECT tag FROM tags WHERE item_id = ? ORDER BY tag", (item_id,)).fetchall()
        return [r["tag"] for r in rows]

    def tag_counts(self) -> list[dict]:
        rows = self.conn.execute("SELECT tag, COUNT(*) AS count FROM tags GROUP BY tag ORDER BY count DESC, tag").fetchall()
        return [dict(r) for r in rows]

    def category_counts(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT category, COUNT(*) AS count FROM items WHERE category IS NOT NULL GROUP BY category ORDER BY count DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- internals ---------------------------------------------------------

    def _row_to_item(self, row: sqlite3.Row) -> dict:
        item = dict(row)
        for col in JSON_COLUMNS:
            if item.get(col):
                try:
                    item[col] = json.loads(item[col])
                except json.JSONDecodeError:
                    pass
        item.setdefault("metadata", {})
        item["tags"] = self.get_tags(item["id"])
        return item

    def _reindex(self, item_id: str) -> None:
        row = self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return
        tags = " ".join(self.get_tags(item_id))
        body_parts = [row["subtitle"], row["note"], row["category"], row["source_platform"], row["canonical_url"]]
        try:
            meta = json.loads(row["metadata"] or "{}")
            body_parts.extend(_flatten_text(meta))
        except json.JSONDecodeError:
            pass
        body = " ".join(str(p) for p in body_parts if p)
        with self.conn:
            self.conn.execute("DELETE FROM items_fts WHERE item_id = ?", (item_id,))
            self.conn.execute(
                "INSERT INTO items_fts (item_id, title, summary, tags, body) VALUES (?, ?, ?, ?, ?)",
                (item_id, row["title"] or "", row["summary"] or "", tags.replace("-", " ") + " " + tags, body),
            )


def _flatten_text(value: Any) -> list[str]:
    """Collect searchable strings from metadata (skipping URLs, which only add noise)."""
    out: list[str] = []
    if isinstance(value, dict):
        for v in value.values():
            out.extend(_flatten_text(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(_flatten_text(v))
    elif isinstance(value, str) and not value.startswith("http"):
        out.append(value)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        out.append(str(value))
    return out


def fts_query(q: str) -> str | None:
    """Turn free text into a safe FTS5 query: every word must match, as a prefix."""
    words = re.findall(r"\w+", q, flags=re.UNICODE)
    if not words:
        return None
    return " ".join(f'"{w}"*' for w in words)
