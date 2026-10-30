"""SQLite persistence: task queue, collected items, per-provider health and statistics.

Only redacted text is stored. No author names or IDs are ever written.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    family TEXT,
    status TEXT NOT NULL DEFAULT 'pending',     -- pending | in_progress | done | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    next_eligible_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_pick ON tasks(status, next_eligible_at, priority);

CREATE TABLE IF NOT EXISTS items (
    uid TEXT PRIMARY KEY,                       -- platform-native id, e.g. reddit:t1_abc
    source TEXT NOT NULL,
    kind TEXT NOT NULL,                         -- post | comment | letter | ...
    url TEXT NOT NULL,
    thread_uid TEXT,
    parent_uid TEXT,
    community TEXT,                             -- subreddit / site / board (never a person)
    text TEXT NOT NULL,                         -- redacted
    date TEXT,                                  -- ISO 8601 UTC or NULL
    tier TEXT NOT NULL,                         -- relevant | borderline | irrelevant
    own_relevant INTEGER NOT NULL DEFAULT 0,    -- relevant on its own merits (no thread context)
    score REAL NOT NULL DEFAULT 0,
    entity_hit INTEGER NOT NULL DEFAULT 0,
    reasons TEXT,
    text_hash TEXT,
    flags TEXT,
    found_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_tier ON items(tier);
CREATE INDEX IF NOT EXISTS idx_items_thread ON items(thread_uid);
CREATE INDEX IF NOT EXISTS idx_items_hash ON items(text_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_url ON items(url);

CREATE TABLE IF NOT EXISTS item_methods (
    uid TEXT NOT NULL,
    provider TEXT NOT NULL,
    method TEXT NOT NULL,
    PRIMARY KEY (uid, provider, method)
);

CREATE TABLE IF NOT EXISTS threads (
    thread_uid TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    url TEXT,
    title TEXT,
    community TEXT,
    relevant INTEGER NOT NULL DEFAULT 0,
    expanded INTEGER NOT NULL DEFAULT 0,
    discovered INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS urls_seen (
    url TEXT PRIMARY KEY,
    domain TEXT,
    query TEXT,
    status TEXT NOT NULL DEFAULT 'new',         -- new | handled | skipped
    first_seen REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS stats (
    provider TEXT NOT NULL,
    method TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    restricted INTEGER NOT NULL DEFAULT 0,
    items_seen INTEGER NOT NULL DEFAULT 0,
    items_new INTEGER NOT NULL DEFAULT 0,
    relevant_new INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    PRIMARY KEY (provider, method)
);

CREATE TABLE IF NOT EXISTS provider_health (
    provider TEXT PRIMARY KEY,
    last_success REAL,
    last_failure REAL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    newest_item_ts REAL,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    ended_at REAL,
    runtime_seconds REAL NOT NULL DEFAULT 0,
    stop_reason TEXT,
    config_json TEXT
);

CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0);
"""


def task_key(task_type: str, payload: dict) -> str:
    blob = task_type + "|" + json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


class Database:
    def __init__(self, path: str | Path, clock=time.time):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.clock = clock

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    # ------------------------------------------------------------------ meta / counters
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def bump_counter(self, name: str, n: int = 1) -> None:
        self.conn.execute(
            "INSERT INTO counters(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=value+excluded.value",
            (name, n),
        )

    def counters(self) -> dict[str, int]:
        return {r["name"]: r["value"] for r in self.conn.execute("SELECT name,value FROM counters")}

    # ------------------------------------------------------------------ tasks
    def add_task(self, task_type: str, payload: dict, priority: int = 0, family: str | None = None,
                 next_eligible_at: float = 0.0) -> bool:
        now = self.clock()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO tasks(key,type,payload,priority,family,next_eligible_at,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (task_key(task_type, payload), task_type, json.dumps(payload, sort_keys=True, default=str),
             priority, family, next_eligible_at, now, now),
        )
        return cur.rowcount > 0

    def add_tasks(self, tasks: Iterable[tuple[str, dict, int, str | None]]) -> int:
        n = 0
        for t in tasks:
            n += 1 if self.add_task(*t) else 0
        self.conn.commit()
        return n

    def reset_in_progress(self) -> int:
        cur = self.conn.execute("UPDATE tasks SET status='pending' WHERE status='in_progress'")
        self.conn.commit()
        return cur.rowcount

    def ready_tasks(self, now: float, limit: int = 300, per_family: int = 25) -> list[sqlite3.Row]:
        """Highest-priority ready tasks, at most `per_family` from each task family.

        Taking the top rows per family (instead of the global top N) keeps slow domains from hiding work that
        belongs to other, currently idle domains, so different sites progress in parallel.
        """
        return self.conn.execute(
            "SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY COALESCE(family,'') "
            "ORDER BY priority DESC, id ASC) AS rn FROM tasks WHERE status='pending' AND next_eligible_at<=?) "
            "WHERE rn<=? ORDER BY priority DESC, id ASC LIMIT ?", (now, per_family, limit)).fetchall()

    def earliest_pending_eligible(self) -> float | None:
        row = self.conn.execute("SELECT MIN(next_eligible_at) AS m FROM tasks WHERE status='pending'").fetchone()
        return row["m"] if row and row["m"] is not None else None

    def pending_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM tasks WHERE status='pending'").fetchone()[0]

    def mark_in_progress(self, task_id: int) -> None:
        self.conn.execute("UPDATE tasks SET status='in_progress', updated_at=? WHERE id=?", (self.clock(), task_id))
        self.conn.commit()

    def complete_task(self, task_id: int) -> None:
        self.conn.execute("UPDATE tasks SET status='done', last_error=NULL, updated_at=? WHERE id=?",
                          (self.clock(), task_id))
        self.conn.commit()

    def retry_task(self, task_id: int, error: str, retry_at: float, *, count_attempt: bool = True,
                   final: bool = False) -> None:
        self.conn.execute(
            "UPDATE tasks SET status=?, attempts=attempts+?, next_eligible_at=?, last_error=?, updated_at=? WHERE id=?",
            ("failed" if final else "pending", 1 if count_attempt else 0, retry_at, error[:500], self.clock(), task_id))
        self.conn.commit()

    def task_counts(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in
                self.conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")}

    def task_counts_by_type(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT type, status, COUNT(*) AS n FROM tasks GROUP BY type, status ORDER BY type, status").fetchall()

    # ------------------------------------------------------------------ items
    def item_row(self, uid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM items WHERE uid=?", (uid,)).fetchone()

    def hash_owner(self, text_hash: str) -> str | None:
        row = self.conn.execute("SELECT uid FROM items WHERE text_hash=? LIMIT 1", (text_hash,)).fetchone()
        return row["uid"] if row else None

    def url_owner(self, url: str) -> str | None:
        row = self.conn.execute("SELECT uid FROM items WHERE url=?", (url,)).fetchone()
        return row["uid"] if row else None

    def insert_item(self, **f: Any) -> bool:
        cols = ["uid", "source", "kind", "url", "thread_uid", "parent_uid", "community", "text", "date", "tier",
                "own_relevant", "score", "entity_hit", "reasons", "text_hash", "flags"]
        vals = [f.get(c) for c in cols]
        cur = self.conn.execute(
            f"INSERT OR IGNORE INTO items({','.join(cols)},found_at) VALUES({','.join('?' * len(cols))},?)",
            (*vals, self.clock()))
        return cur.rowcount > 0

    def add_method(self, uid: str, provider: str, method: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO item_methods(uid,provider,method) VALUES(?,?,?)",
                          (uid, provider, method))

    def update_item_score(self, uid: str, tier: str, own_relevant: int, score: float, entity_hit: int,
                          reasons: str) -> None:
        self.conn.execute(
            "UPDATE items SET tier=?, own_relevant=?, score=?, entity_hit=?, reasons=? WHERE uid=?",
            (tier, own_relevant, score, entity_hit, reasons, uid))

    def tier_counts(self) -> dict[str, int]:
        return {r["tier"]: r["n"] for r in self.conn.execute("SELECT tier, COUNT(*) AS n FROM items GROUP BY tier")}

    # ------------------------------------------------------------------ threads
    def thread_row(self, thread_uid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM threads WHERE thread_uid=?", (thread_uid,)).fetchone()

    def thread_relevant(self, thread_uid: str | None) -> bool:
        if not thread_uid:
            return False
        row = self.thread_row(thread_uid)
        return bool(row and row["relevant"])

    def upsert_thread(self, thread_uid: str, source: str, *, url: str | None = None, title: str | None = None,
                      community: str | None = None, relevant: bool = False, discovered: bool = False) -> bool:
        """Returns True if this call newly marked the thread relevant."""
        row = self.thread_row(thread_uid)
        if row is None:
            self.conn.execute(
                "INSERT INTO threads(thread_uid,source,url,title,community,relevant,discovered) VALUES(?,?,?,?,?,?,?)",
                (thread_uid, source, url, title, community, int(relevant), int(discovered)))
            return relevant
        newly = relevant and not row["relevant"]
        self.conn.execute(
            "UPDATE threads SET url=COALESCE(?,url), title=COALESCE(?,title), community=COALESCE(?,community),"
            " relevant=MAX(relevant,?), discovered=MAX(discovered,?) WHERE thread_uid=?",
            (url, title, community, int(relevant), int(discovered), thread_uid))
        return newly

    def mark_thread_expanded(self, thread_uid: str) -> None:
        self.conn.execute("UPDATE threads SET expanded=1 WHERE thread_uid=?", (thread_uid,))

    def clear_thread_relevance(self) -> None:
        self.conn.execute("UPDATE threads SET relevant=0")

    # ------------------------------------------------------------------ urls seen (search-engine discovery)
    def add_url_seen(self, url: str, domain: str, query: str) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO urls_seen(url,domain,query,first_seen) VALUES(?,?,?,?)",
            (url, domain, query, self.clock()))
        return cur.rowcount > 0

    def urls_seen_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM urls_seen").fetchone()[0]

    # ------------------------------------------------------------------ stats / health
    def bump_stat(self, provider: str, method: str, **deltas: int) -> None:
        self.conn.execute("INSERT OR IGNORE INTO stats(provider,method) VALUES(?,?)", (provider, method))
        sets = ", ".join(f"{k}={k}+?" for k in deltas)
        if sets:
            self.conn.execute(f"UPDATE stats SET {sets} WHERE provider=? AND method=?",
                              (*deltas.values(), provider, method))

    def set_stat_error(self, provider: str, method: str, error: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO stats(provider,method) VALUES(?,?)", (provider, method))
        self.conn.execute("UPDATE stats SET last_error=? WHERE provider=? AND method=?",
                          (error[:300], provider, method))

    def health_success(self, provider: str, newest_item_ts: float | None = None) -> None:
        self.conn.execute("INSERT OR IGNORE INTO provider_health(provider) VALUES(?)", (provider,))
        self.conn.execute(
            "UPDATE provider_health SET last_success=?, consecutive_failures=0,"
            " newest_item_ts=CASE WHEN ? IS NOT NULL AND (newest_item_ts IS NULL OR ?>newest_item_ts) THEN ? ELSE newest_item_ts END"
            " WHERE provider=?", (self.clock(), newest_item_ts, newest_item_ts, newest_item_ts, provider))

    def health_failure(self, provider: str, error: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO provider_health(provider) VALUES(?)", (provider,))
        self.conn.execute(
            "UPDATE provider_health SET last_failure=?, consecutive_failures=consecutive_failures+1, last_error=?"
            " WHERE provider=?", (self.clock(), error[:300], provider))

    # ------------------------------------------------------------------ sessions
    def start_session(self, config: dict) -> int:
        cur = self.conn.execute("INSERT INTO sessions(started_at,config_json) VALUES(?,?)",
                                (self.clock(), json.dumps(config, default=str)))
        self.conn.commit()
        return int(cur.lastrowid)

    def end_session(self, session_id: int, runtime_seconds: float, stop_reason: str) -> None:
        self.conn.execute("UPDATE sessions SET ended_at=?, runtime_seconds=?, stop_reason=? WHERE id=?",
                          (self.clock(), runtime_seconds, stop_reason, session_id))
        self.conn.commit()

    def total_runtime_seconds(self) -> float:
        row = self.conn.execute("SELECT COALESCE(SUM(runtime_seconds),0) AS s FROM sessions").fetchone()
        return float(row["s"])

    def commit(self) -> None:
        self.conn.commit()
