"""SQLite storage: items, tags, and a full-text index for retrieval."""

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
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
    analysis        TEXT,                   -- raw model output, kept for debugging / re-enrichment
    confidence      INTEGER,                -- 0-100, how sure the model is; 100 once the user corrects it
    confidence_reason TEXT,
    alternatives    TEXT NOT NULL DEFAULT '[]',  -- other things it might be: [{title, category, year, canonical_url, why}]
    corrected       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS items_category ON items(category);
CREATE INDEX IF NOT EXISTS items_created ON items(created_at);

CREATE TABLE IF NOT EXISTS tags (
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    tag     TEXT NOT NULL,
    PRIMARY KEY (item_id, tag)
);
CREATE INDEX IF NOT EXISTS tags_tag ON tags(tag);

-- Deleted item ids, so offline clients learn about deletions when they sync.
CREATE TABLE IF NOT EXISTS tombstones (
    id         TEXT PRIMARY KEY,
    deleted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tombstones_deleted ON tombstones(deleted_at);

-- What each analysis actually consumed and cost. Kept when items are deleted, for reporting.
CREATE TABLE IF NOT EXISTS analysis_runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id            TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    purpose            TEXT NOT NULL,         -- analyze | reanalyze | correct
    analyzer           TEXT NOT NULL,         -- claude | local | ocr
    model              TEXT,
    mode               TEXT,                  -- realtime | batch | local
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    web_searches       INTEGER NOT NULL DEFAULT 0,
    web_fetches        INTEGER NOT NULL DEFAULT 0,
    requests           INTEGER NOT NULL DEFAULT 0,
    duration_ms        INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL NOT NULL DEFAULT 0,
    ok                 INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS runs_item ON analysis_runs(item_id);
CREATE INDEX IF NOT EXISTS runs_created ON analysis_runs(created_at);

-- Claude requests waiting for / inside a Message Batch.
CREATE TABLE IF NOT EXISTS batch_jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    purpose      TEXT NOT NULL,
    params       TEXT NOT NULL,              -- Messages API request (JSON)
    context      TEXT NOT NULL,              -- OCR text, earlier runs, ... (JSON)
    batch_id     TEXT,                       -- set once submitted
    submitted_at TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    item_id UNINDEXED, title, summary, tags, body, tokenize='unicode61 remove_diacritics 2'
);
"""

JSON_COLUMNS = ("metadata", "links", "analysis", "alternatives")

# Columns added after the first release; created on startup for existing databases.
MIGRATIONS = {
    "confidence": "INTEGER",
    "confidence_reason": "TEXT",
    "alternatives": "TEXT NOT NULL DEFAULT '[]'",
    "corrected": "INTEGER NOT NULL DEFAULT 0",
}
# Below this confidence an identification is flagged for the user to check.
REVIEW_THRESHOLD = 60

EDITABLE_COLUMNS = {
    "status", "error", "note", "category", "source_platform", "title", "subtitle",
    "summary", "canonical_url", "image_url", "metadata", "links", "analysis",
    "confidence", "confidence_reason", "alternatives", "corrected",
}


def now() -> str:
    # Microsecond precision: sync cursors compare these strings.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


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
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(items)")}
        with self.conn:
            for column, ddl in MIGRATIONS.items():
                if column not in existing:
                    self.conn.execute(f"ALTER TABLE items ADD COLUMN {column} {ddl}")

    # ---- items -------------------------------------------------------------

    def create_item(
        self,
        image_file: str,
        note: str | None = None,
        tags: list[str] | None = None,
        item_id: str | None = None,
        created_at: str | None = None,
    ) -> dict:
        """Create an item. Clients may supply the id (so offline uploads can be retried safely)
        and the original capture time."""
        item_id = item_id or uuid.uuid4().hex[:12]
        ts = now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO items (id, created_at, updated_at, status, image_file, note) VALUES (?, ?, ?, 'processing', ?, ?)",
                (item_id, created_at or ts, ts, image_file, note),
            )
            self.conn.execute("DELETE FROM tombstones WHERE id = ?", (item_id,))
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
                self.conn.execute("INSERT OR REPLACE INTO tombstones (id, deleted_at) VALUES (?, ?)", (item_id, now()))
                self.conn.execute("DELETE FROM batch_jobs WHERE item_id = ? AND batch_id IS NULL", (item_id,))
        return item

    def list_items(
        self,
        q: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        needs_review: bool = False,
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
        if needs_review:
            where.append(f"i.status = 'ready' AND i.corrected = 0 AND i.confidence < {REVIEW_THRESHOLD}")
        for tag in tags or []:
            where.append("EXISTS (SELECT 1 FROM tags t WHERE t.item_id = i.id AND t.tag = ?)")
            params.append(normalize_tag(tag))
        sql = f"SELECT i.* FROM items i {join}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
        rows = self.conn.execute(sql, (*params, limit, offset)).fetchall()
        return [self._row_to_item(r) for r in rows]

    def is_deleted(self, item_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM tombstones WHERE id = ?", (item_id,)).fetchone() is not None

    def changes_since(self, since: str | None) -> dict:
        """Everything a client needs to catch up: items changed and ids deleted after `since`."""
        server_time = now()
        if since:
            rows = self.conn.execute(
                "SELECT * FROM items WHERE updated_at >= ? ORDER BY updated_at", (since,)
            ).fetchall()
            deleted = [r["id"] for r in self.conn.execute(
                "SELECT id FROM tombstones WHERE deleted_at >= ?", (since,)
            ).fetchall()]
        else:
            rows = self.conn.execute("SELECT * FROM items ORDER BY updated_at").fetchall()
            deleted = []
        return {"server_time": server_time, "items": [self._row_to_item(r) for r in rows], "deleted": deleted}

    # ---- usage ---------------------------------------------------------------

    def record_runs(self, item_id: str, runs: list[dict], purpose: str = "analyze") -> None:
        cols = ("analyzer", "model", "mode", "input_tokens", "output_tokens", "cache_read_tokens",
                "cache_write_tokens", "web_searches", "web_fetches", "requests", "duration_ms", "cost_usd", "ok")

        def value(run: dict, col: str):
            if col in ("analyzer", "model", "mode"):
                return run.get(col) or ("unknown" if col == "analyzer" else None)
            if col == "ok":
                return int(bool(run.get("ok", True)))
            return run.get(col) or 0

        with self.conn:
            self.conn.executemany(
                f"INSERT INTO analysis_runs (item_id, created_at, purpose, {', '.join(cols)}) "
                f"VALUES (?, ?, ?, {', '.join('?' for _ in cols)})",
                [(item_id, now(), purpose, *(value(r, c) for c in cols)) for r in runs],
            )

    def usage_report(self, days: int = 30) -> dict:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="microseconds")
        q = lambda sql, *a: [dict(r) for r in self.conn.execute(sql, (since, *a)).fetchall()]  # noqa: E731
        totals = q("""SELECT COUNT(DISTINCT item_id) AS screenshots, COUNT(*) AS runs,
                      COALESCE(SUM(cost_usd), 0) AS cost_usd, COALESCE(SUM(input_tokens), 0) AS input_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens, COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                      COALESCE(SUM(web_searches), 0) AS web_searches, COALESCE(SUM(web_fetches), 0) AS web_fetches
                      FROM analysis_runs WHERE created_at >= ?""")[0]
        by_analyzer = q("""SELECT analyzer, mode, model, COUNT(*) AS runs, COUNT(DISTINCT item_id) AS screenshots,
                           SUM(cost_usd) AS cost_usd, AVG(cost_usd) AS avg_cost_usd, SUM(input_tokens) AS input_tokens,
                           SUM(output_tokens) AS output_tokens, SUM(web_searches) AS web_searches,
                           AVG(duration_ms) AS avg_duration_ms, SUM(1 - ok) AS failures
                           FROM analysis_runs WHERE created_at >= ? GROUP BY analyzer, mode, model ORDER BY cost_usd DESC""")
        by_day = q("""SELECT substr(created_at, 1, 10) AS day, COUNT(DISTINCT item_id) AS screenshots, SUM(cost_usd) AS cost_usd
                      FROM analysis_runs WHERE created_at >= ? GROUP BY day ORDER BY day""")
        escalated = q("""SELECT COUNT(DISTINCT item_id) AS n FROM analysis_runs
                         WHERE created_at >= ? AND analyzer = 'claude'""")[0]["n"]
        n = totals["screenshots"] or 0
        per = totals["cost_usd"] / n if n else 0.0
        active_days = len(by_day) or 1
        return {
            "period_days": days,
            "totals": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in totals.items()},
            "per_screenshot_usd": round(per, 4),
            "claude_share": round(escalated / n, 3) if n else 0.0,
            "projected_30d_usd": round(totals["cost_usd"] / min(days, max(active_days, 1)) * 30, 2) if n else 0.0,
            "by_analyzer": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()} for r in by_analyzer],
            "by_day": [{**r, "cost_usd": round(r["cost_usd"], 4)} for r in by_day],
        }

    # ---- batch jobs ----------------------------------------------------------

    def add_batch_job(self, item_id: str, purpose: str, params: dict, context: dict) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM batch_jobs WHERE item_id = ? AND batch_id IS NULL", (item_id,))
            self.conn.execute(
                "INSERT INTO batch_jobs (item_id, created_at, purpose, params, context) VALUES (?, ?, ?, ?, ?)",
                (item_id, now(), purpose, json.dumps(params), json.dumps(context)),
            )

    def unsubmitted_jobs(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM batch_jobs WHERE batch_id IS NULL ORDER BY id")]

    def mark_submitted(self, job_ids: list[int], batch_id: str) -> None:
        with self.conn:
            self.conn.executemany("UPDATE batch_jobs SET batch_id = ?, submitted_at = ? WHERE id = ?",
                                  [(batch_id, now(), j) for j in job_ids])

    def open_batches(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT batch_id FROM batch_jobs WHERE batch_id IS NOT NULL")]

    def batch_job(self, job_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM batch_jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def delete_batch_job(self, job_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM batch_jobs WHERE id = ?", (job_id,))

    # ---- tags --------------------------------------------------------------

    def set_tags(self, item_id: str, tags: list[str]) -> list[str]:
        clean = sorted({t for t in (normalize_tag(t) for t in tags) if t})
        with self.conn:
            self.conn.execute("DELETE FROM tags WHERE item_id = ?", (item_id,))
            self.conn.executemany("INSERT INTO tags (item_id, tag) VALUES (?, ?)", [(item_id, t) for t in clean])
            self.conn.execute("UPDATE items SET updated_at = ? WHERE id = ?", (now(), item_id))
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
        item["corrected"] = bool(item.get("corrected"))
        item["needs_review"] = (
            item.get("status") == "ready" and not item["corrected"]
            and item.get("confidence") is not None and item["confidence"] < REVIEW_THRESHOLD
        )
        item["tags"] = self.get_tags(item["id"])
        cost = self.conn.execute(
            "SELECT COUNT(*) AS runs, COALESCE(SUM(cost_usd), 0) AS cost, "
            "COALESCE(SUM(web_searches), 0) AS searches, GROUP_CONCAT(DISTINCT analyzer || ':' || mode) AS how "
            "FROM analysis_runs WHERE item_id = ?", (item["id"],),
        ).fetchone()
        item["usage"] = {
            "cost_usd": round(cost["cost"], 4), "runs": cost["runs"], "web_searches": cost["searches"],
            "via": sorted((cost["how"] or "").split(",")) if cost["how"] else [],
        }
        item["batch_pending"] = self.conn.execute(
            "SELECT 1 FROM batch_jobs WHERE item_id = ? LIMIT 1", (item["id"],)).fetchone() is not None
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
