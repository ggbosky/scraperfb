"""SQLite vrstva: ukládání příspěvků, deduplikace, správa leadů, zdrojů a klíčových slov.

Databázi současně používá worker (`main.py`) i dashboard (`app.py`) – jde o
dva procesy, proto běží v režimu WAL a každé volání si otevírá vlastní spojení.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from config import (
    DEFAULT_DEMAND_KEYWORDS,
    DEFAULT_NEGATIVE_KEYWORDS,
    DEFAULT_SEARCH_QUERIES,
    DEFAULT_TOPIC_KEYWORDS,
    LEAD_STATUSES,
)

KEYWORD_KINDS = ("topic", "demand", "negative", "search")

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    post_key          TEXT NOT NULL UNIQUE,      -- fb:<post_id> | url:<url> | hash:<sha1>
    url               TEXT,
    source_type       TEXT,                      -- group | search
    source_name       TEXT,
    source_url        TEXT,
    author            TEXT,
    text              TEXT NOT NULL,
    content_hash      TEXT NOT NULL,             -- otisk normalizovaného textu (cross-post dedupe)
    posted_at         TEXT,                      -- ISO 8601 UTC, NULL = neznámé
    scraped_at        TEXT NOT NULL,
    analysis_status   TEXT NOT NULL DEFAULT 'pending',  -- pending | done | skipped | error
    analysis_attempts INTEGER NOT NULL DEFAULT 0,
    reject_reason     TEXT,
    intent            TEXT,
    is_active         INTEGER,
    score             INTEGER,
    project_type      TEXT,
    platform          TEXT,
    budget            TEXT,
    deadline          TEXT,
    urgency           TEXT,
    location          TEXT,
    summary           TEXT,
    reasoning         TEXT,
    llm_model         TEXT,
    is_lead           INTEGER NOT NULL DEFAULT 0,
    duplicate_of      INTEGER REFERENCES posts(id),
    lead_status       TEXT NOT NULL DEFAULT 'Nekontaktováno',
    notes             TEXT,
    notified_at       TEXT,
    status_changed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_content_hash ON posts(content_hash);
CREATE INDEX IF NOT EXISTS idx_posts_is_lead ON posts(is_lead, posted_at);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(analysis_status);

CREATE TABLE IF NOT EXISTS sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    type            TEXT NOT NULL DEFAULT 'group',
    name            TEXT NOT NULL,
    url             TEXT NOT NULL UNIQUE,
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_scraped_at TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS keywords (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    kind  TEXT NOT NULL,
    value TEXT NOT NULL,
    UNIQUE(kind, value)
);

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    sources_scraped INTEGER DEFAULT 0,
    posts_found     INTEGER DEFAULT 0,
    posts_new       INTEGER DEFAULT 0,
    llm_calls       INTEGER DEFAULT 0,
    leads_found     INTEGER DEFAULT 0,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS page_loads (
    id  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts  TEXT NOT NULL,
    url TEXT
);
CREATE INDEX IF NOT EXISTS idx_page_loads_ts ON page_loads(ts);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class PostRecord:
    post_key: str
    text: str
    content_hash: str
    url: str | None = None
    source_type: str | None = None
    source_name: str | None = None
    source_url: str | None = None
    author: str | None = None
    posted_at: datetime | None = None
    scraped_at: datetime = field(default_factory=utcnow)


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    # ------------------------------------------------------------------ core
    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def seed_defaults(self) -> None:
        """Při prvním spuštění vloží výchozí klíčová slova a vyhledávací dotazy."""
        if self.get_kv("seeded") == "1":
            return
        defaults = {
            "topic": DEFAULT_TOPIC_KEYWORDS,
            "demand": DEFAULT_DEMAND_KEYWORDS,
            "negative": DEFAULT_NEGATIVE_KEYWORDS,
            "search": DEFAULT_SEARCH_QUERIES,
        }
        for kind, values in defaults.items():
            self.set_keywords(kind, values)
        self.set_kv("seeded", "1")

    # ----------------------------------------------------------------- posts
    def known_keys(self, keys: Iterable[str]) -> set[str]:
        keys = list(dict.fromkeys(keys))
        if not keys:
            return set()
        found: set[str] = set()
        with self.connect() as conn:
            for i in range(0, len(keys), 500):
                chunk = keys[i : i + 500]
                marks = ",".join("?" * len(chunk))
                rows = conn.execute(f"SELECT post_key FROM posts WHERE post_key IN ({marks})", chunk)
                found.update(r["post_key"] for r in rows)
        return found

    def insert_post(self, post: PostRecord) -> int | None:
        """Vloží příspěvek. Vrací ID, nebo None pokud už existuje (deduplikace dle post_key)."""
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO posts
                   (post_key, url, source_type, source_name, source_url, author, text,
                    content_hash, posted_at, scraped_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    post.post_key, post.url, post.source_type, post.source_name, post.source_url,
                    post.author, post.text, post.content_hash, to_iso(post.posted_at),
                    to_iso(post.scraped_at),
                ),
            )
            return cur.lastrowid if cur.rowcount else None

    def find_by_content_hash(self, content_hash: str, exclude_id: int | None = None) -> dict | None:
        """Najde dříve LLM-analyzovaný příspěvek se stejným textem (cross-post do více skupin)."""
        with self.connect() as conn:
            row = conn.execute(
                """SELECT * FROM posts WHERE content_hash = ? AND id != ? AND duplicate_of IS NULL
                   AND analysis_status = 'done' ORDER BY id LIMIT 1""",
                (content_hash, exclude_id or -1),
            ).fetchone()
        return dict(row) if row else None

    def mark_skipped(self, post_id: int, reason: str, duplicate_of: int | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE posts SET analysis_status='skipped', reject_reason=?, duplicate_of=?
                   WHERE id=?""",
                (reason, duplicate_of, post_id),
            )

    def mark_analysis_error(self, post_id: int, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE posts SET analysis_status='error', reject_reason=?,
                   analysis_attempts = analysis_attempts + 1 WHERE id=?""",
                (f"llm_error: {error[:300]}", post_id),
            )

    def save_analysis(self, post_id: int, analysis: dict[str, Any], is_lead: bool,
                      reject_reason: str | None, model: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE posts SET
                     analysis_status='done', analysis_attempts = analysis_attempts + 1,
                     intent=?, is_active=?, score=?, project_type=?, platform=?, budget=?,
                     deadline=?, urgency=?, location=?, summary=?, reasoning=?, llm_model=?,
                     is_lead=?, reject_reason=?
                   WHERE id=?""",
                (
                    analysis.get("intent"), int(bool(analysis.get("is_active"))),
                    analysis.get("relevance_score"), analysis.get("project_type"),
                    analysis.get("platform"), analysis.get("budget"), analysis.get("deadline"),
                    analysis.get("urgency"), analysis.get("location"), analysis.get("summary"),
                    analysis.get("reasoning"), model, int(is_lead), reject_reason, post_id,
                ),
            )

    def posts_for_reanalysis(self, max_attempts: int = 3, limit: int = 50) -> list[dict]:
        """Příspěvky, u kterých selhalo LLM (např. Ollama neběžela) – zkusí se znovu."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM posts WHERE analysis_status IN ('pending', 'error')
                   AND analysis_attempts < ? ORDER BY id LIMIT ?""",
                (max_attempts, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_post(self, post_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        return dict(row) if row else None

    def mark_notified(self, post_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE posts SET notified_at=? WHERE id=?", (to_iso(utcnow()), post_id))

    # ----------------------------------------------------------------- leads
    def get_leads(self, statuses: list[str] | None = None, min_score: int = 0,
                  since: datetime | None = None, search: str = "", limit: int = 1000) -> list[dict]:
        sql = "SELECT * FROM posts WHERE is_lead = 1 AND COALESCE(score, 0) >= ?"
        params: list[Any] = [min_score]
        if statuses:
            sql += f" AND lead_status IN ({','.join('?' * len(statuses))})"
            params += statuses
        if since:
            sql += " AND COALESCE(posted_at, scraped_at) >= ?"
            params.append(to_iso(since))
        if search:
            sql += " AND (text LIKE ? OR summary LIKE ? OR author LIKE ? OR source_name LIKE ?)"
            params += [f"%{search}%"] * 4
        sql += " ORDER BY COALESCE(posted_at, scraped_at) DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_posts(self, only_rejected: bool = False, limit: int = 300, search: str = "") -> list[dict]:
        sql = "SELECT * FROM posts WHERE 1=1"
        params: list[Any] = []
        if only_rejected:
            sql += " AND is_lead = 0"
        if search:
            sql += " AND text LIKE ?"
            params.append(f"%{search}%")
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def update_lead_status(self, post_id: int, status: str, notes: str | None = None) -> None:
        if status not in LEAD_STATUSES:
            raise ValueError(f"Neznámý stav: {status}")
        with self.connect() as conn:
            if notes is None:
                conn.execute(
                    "UPDATE posts SET lead_status=?, status_changed_at=? WHERE id=?",
                    (status, to_iso(utcnow()), post_id),
                )
            else:
                conn.execute(
                    "UPDATE posts SET lead_status=?, notes=?, status_changed_at=? WHERE id=?",
                    (status, notes, to_iso(utcnow()), post_id),
                )

    def set_manual_lead(self, post_id: int, is_lead: bool) -> None:
        """Ruční přepnutí (false negative / false positive) z dashboardu."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE posts SET is_lead=?, reject_reason=? WHERE id=?",
                (int(is_lead), None if is_lead else "manual_reject", post_id),
            )

    def stats(self) -> dict[str, Any]:
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            leads = conn.execute("SELECT COUNT(*) FROM posts WHERE is_lead=1").fetchone()[0]
            analyzed = conn.execute(
                "SELECT COUNT(*) FROM posts WHERE analysis_status='done'").fetchone()[0]
            by_status = {
                r["lead_status"]: r["n"]
                for r in conn.execute(
                    "SELECT lead_status, COUNT(*) n FROM posts WHERE is_lead=1 GROUP BY lead_status")
            }
            since = to_iso(utcnow() - timedelta(days=1))
            leads_24h = conn.execute(
                "SELECT COUNT(*) FROM posts WHERE is_lead=1 AND scraped_at >= ?", (since,)
            ).fetchone()[0]
        return {"total_posts": total, "leads": leads, "analyzed": analyzed,
                "by_status": by_status, "leads_24h": leads_24h}

    # --------------------------------------------------------------- sources
    def list_sources(self, enabled_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM sources"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY COALESCE(last_scraped_at, '') ASC, id ASC"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql).fetchall()]

    def add_source(self, name: str, url: str, type_: str = "group") -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sources (type, name, url, enabled, created_at) VALUES (?,?,?,1,?)",
                (type_, name.strip(), url.strip(), to_iso(utcnow())),
            )

    def update_source(self, source_id: int, **fields: Any) -> None:
        allowed = {"name", "url", "enabled", "last_scraped_at", "last_error", "type"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.connect() as conn:
            conn.execute(f"UPDATE sources SET {sets} WHERE id=?", (*fields.values(), source_id))

    def delete_source(self, source_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM sources WHERE id=?", (source_id,))

    # -------------------------------------------------------------- keywords
    def get_keywords(self, kind: str) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT value FROM keywords WHERE kind=? ORDER BY id", (kind,))
            return [r["value"] for r in rows]

    def set_keywords(self, kind: str, values: Iterable[str]) -> None:
        if kind not in KEYWORD_KINDS:
            raise ValueError(f"Neznámý typ klíčových slov: {kind}")
        clean = list(dict.fromkeys(v.strip() for v in values if v and v.strip()))
        with self.connect() as conn:
            conn.execute("DELETE FROM keywords WHERE kind=?", (kind,))
            conn.executemany(
                "INSERT OR IGNORE INTO keywords (kind, value) VALUES (?, ?)",
                [(kind, v) for v in clean],
            )

    # ---------------------------------------------------- rate limiting log
    def record_page_load(self, url: str) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO page_loads (ts, url) VALUES (?, ?)", (to_iso(utcnow()), url))
            cutoff = to_iso(utcnow() - timedelta(days=7))
            conn.execute("DELETE FROM page_loads WHERE ts < ?", (cutoff,))

    def count_page_loads(self, since: datetime) -> int:
        with self.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM page_loads WHERE ts >= ?", (to_iso(since),)
            ).fetchone()[0]

    # ------------------------------------------------------------------ runs
    def start_run(self) -> int:
        with self.connect() as conn:
            cur = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (to_iso(utcnow()),))
            return cur.lastrowid

    def finish_run(self, run_id: int, **stats: Any) -> None:
        allowed = {"sources_scraped", "posts_found", "posts_new", "llm_calls", "leads_found", "error"}
        stats = {k: v for k, v in stats.items() if k in allowed}
        stats["finished_at"] = to_iso(utcnow())
        sets = ", ".join(f"{k}=?" for k in stats)
        with self.connect() as conn:
            conn.execute(f"UPDATE runs SET {sets} WHERE id=?", (*stats.values(), run_id))

    def recent_runs(self, limit: int = 30) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(r) for r in rows]

    # -------------------------------------------------------------------- kv
    def get_kv(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_kv(self, key: str, value: str | None) -> None:
        with self.connect() as conn:
            if value is None:
                conn.execute("DELETE FROM kv WHERE key=?", (key,))
            else:
                conn.execute(
                    "INSERT INTO kv (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
