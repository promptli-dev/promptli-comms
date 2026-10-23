"""Durable local state (sqlite): Agent Link cursor, processed event ids, per-contact runtime sessions."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
create table if not exists kv (k text primary key, v text not null);
create table if not exists processed (event_id text primary key, at real not null);
create table if not exists sessions (
  session_key text primary key, runtime_session_id text, updated_at real not null
);
"""


class GatewayState:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("pragma journal_mode=wal")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    # ---- cursor -------------------------------------------------------------------------------
    def cursor(self) -> int:
        row = self._db.execute("select v from kv where k='cursor'").fetchone()
        return int(row[0]) if row else 0

    def set_cursor(self, seq: int) -> None:
        with self._lock:
            if seq > self.cursor():
                self._db.execute(
                    "insert into kv(k,v) values('cursor',?) on conflict(k) do update set v=excluded.v",
                    (str(seq),),
                )

    # ---- dedupe -------------------------------------------------------------------------------
    def seen(self, event_id: str) -> bool:
        return (
            self._db.execute("select 1 from processed where event_id=?", (event_id,)).fetchone() is not None
        )

    def mark_processed(self, event_id: str) -> None:
        with self._lock:
            self._db.execute(
                "insert or ignore into processed(event_id, at) values(?,?)", (event_id, time.time())
            )

    def prune(self, older_than_days: int = 14) -> None:
        with self._lock:
            self._db.execute("delete from processed where at < ?", (time.time() - older_than_days * 86400,))

    # ---- sessions -----------------------------------------------------------------------------
    def session(self, key: str) -> str | None:
        row = self._db.execute(
            "select runtime_session_id from sessions where session_key=?", (key,)
        ).fetchone()
        return str(row[0]) if row and row[0] else None

    def set_session(self, key: str, runtime_session_id: str | None) -> None:
        with self._lock:
            self._db.execute(
                "insert into sessions(session_key, runtime_session_id, updated_at) values(?,?,?) "
                "on conflict(session_key) do update set runtime_session_id=excluded.runtime_session_id, "
                "updated_at=excluded.updated_at",
                (key, runtime_session_id, time.time()),
            )

    def sessions(self) -> dict[str, str | None]:
        return {k: v for k, v in self._db.execute("select session_key, runtime_session_id from sessions")}

    def close(self) -> None:
        self._db.close()
