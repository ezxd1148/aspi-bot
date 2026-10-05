"""Small persistent audit log; never stores confession bodies, URLs, or secrets."""

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import time


class AuditStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, time REAL NOT NULL, kind TEXT NOT NULL,
                submission_id TEXT, actor_id INTEGER, actor TEXT, source TEXT,
                detail TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS events_time ON events(time)")
            db.commit()
        os.chmod(path, 0o600)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        return db

    def record(self, kind, *, submission_id=None, actor=None, source="bot", detail=""):
        now = time.time()
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO events(time,kind,submission_id,actor_id,actor,source,detail) VALUES(?,?,?,?,?,?,?)",
                       (now, kind, submission_id, getattr(actor, "id", None),
                        getattr(actor, "full_name", "")[:128], source, detail[:300]))
            db.execute("DELETE FROM events WHERE time < ?", (now - 30 * 86400,))
            db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 10000)")

    def recent(self, limit=100):
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM events WHERE time >= ? ORDER BY id DESC LIMIT ?",
                (time.time() - 30 * 86400, min(max(limit, 1), 100)))]

    def counts(self, since):
        with closing(self._connect()) as db:
            return dict(db.execute("SELECT kind,COUNT(*) FROM events WHERE time >= ? GROUP BY kind", (since,)))

    def latest(self, kinds):
        with closing(self._connect()) as db:
            row = db.execute(f"SELECT * FROM events WHERE kind IN ({','.join('?' for _ in kinds)}) AND time >= ? ORDER BY id DESC LIMIT 1",
                             (*kinds, time.time() - 30 * 86400)).fetchone()
            return dict(row) if row else None
