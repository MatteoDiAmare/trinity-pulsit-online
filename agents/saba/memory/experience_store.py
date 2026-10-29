"""Local SQLite experiment journal; no credentials or personal data."""
import json
import sqlite3
from datetime import datetime, timezone


class ExperienceStore:
    def __init__(self, path="saba_experiences.sqlite3"):
        self.connection = sqlite3.connect(path)
        self.connection.execute("CREATE TABLE IF NOT EXISTS experiences (id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL)")
        self.connection.commit()

    def add(self, kind, payload):
        if not kind:
            raise ValueError("kind is required")
        cursor = self.connection.execute("INSERT INTO experiences (recorded_at, kind, payload) VALUES (?, ?, ?)", (datetime.now(timezone.utc).isoformat(), kind, json.dumps(payload)))
        self.connection.commit()
        return cursor.lastrowid

    def recent(self, limit=20):
        rows = self.connection.execute("SELECT recorded_at, kind, payload FROM experiences ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"recorded_at": t, "kind": k, "payload": json.loads(p)} for t, k, p in rows]

    def close(self):
        self.connection.close()
