"""Durable, tamper-evident audit trail of every Academy bridge call (Saba's tool calls and their answers).

Why: session files and Trinity execution logs are retained for a limited time. The bridge is the one place every research action passes
through, so it records each call itself in `<root>/academy_audit.sqlite3` on the persistent data volume:

* append-only: SQL triggers refuse UPDATE and DELETE;
* hash-chained: every entry hashes (previous entry hash + its own content), so removal or editing of any earlier entry is detected by `verify`;
* complete: command, full request (her hypotheses, lessons, forecasts, strategy code references), the answer (or its truncated text plus the
  hash of the full answer), success/error, duration and, when the runtime provides it, the Trinity execution id.

Secrets are masked before storing. Recording never changes or blocks the bridge's answer: if the log cannot be written the call still
returns, and the failure is printed to stderr.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MASK = re.compile(r"(ghp_|gho_|github_pat_|sk-ant-|trinity_mcp_|Bearer\s+)[A-Za-z0-9_\-\.]{8,}")
MAX_REQUEST = 200_000
MAX_RESPONSE = 400_000
GENESIS = "0" * 64
EXECUTION_ENV = ("TRINITY_EXECUTION_ID", "EXECUTION_ID", "CLAUDE_EXECUTION_ID")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def mask(text: str) -> str:
    return MASK.sub(lambda m: m.group(1) + "***", text)


class AuditLog:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS calls (
          id INTEGER PRIMARY KEY, ts TEXT NOT NULL, command TEXT, execution_id TEXT, ok INTEGER NOT NULL, error TEXT,
          duration_ms INTEGER, request_json TEXT NOT NULL, request_sha256 TEXT NOT NULL, response_json TEXT NOT NULL,
          response_sha256 TEXT NOT NULL, response_truncated INTEGER NOT NULL DEFAULT 0, prev_hash TEXT NOT NULL, entry_hash TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS calls_no_update BEFORE UPDATE ON calls BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS calls_no_delete BEFORE DELETE ON calls BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    @staticmethod
    def _entry_hash(prev: str, row: dict) -> str:
        body = json.dumps({k: row[k] for k in ("ts", "command", "execution_id", "ok", "error", "duration_ms", "request_sha256", "response_sha256",
                                               "response_truncated")}, sort_keys=True)
        return _sha(prev + body)

    def append(self, *, command, request, response: dict, ok: bool, duration_ms: int, execution_id: str | None = None) -> int:
        req_text = mask(json.dumps(request, sort_keys=True, default=str, ensure_ascii=False))
        resp_full = mask(json.dumps(response, sort_keys=True, default=str, ensure_ascii=False))
        truncated = len(resp_full) > MAX_RESPONSE
        row = {"ts": datetime.now(timezone.utc).isoformat(), "command": command if isinstance(command, str) else None,
               "execution_id": execution_id or next((os.environ[k] for k in EXECUTION_ENV if os.environ.get(k)), None),
               "ok": 1 if ok else 0, "error": mask(str(response.get("error")))[:1000] if not ok else None, "duration_ms": int(duration_ms),
               "request_sha256": _sha(req_text), "response_sha256": _sha(resp_full), "response_truncated": 1 if truncated else 0}
        self.db.execute("BEGIN IMMEDIATE")                         # the chain must be extended by one writer at a time
        try:
            last = self.db.execute("SELECT entry_hash FROM calls ORDER BY id DESC LIMIT 1").fetchone()
            prev = last[0] if last else GENESIS
            cur = self.db.execute(
                "INSERT INTO calls(ts,command,execution_id,ok,error,duration_ms,request_json,request_sha256,response_json,response_sha256,"
                "response_truncated,prev_hash,entry_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["ts"], row["command"], row["execution_id"], row["ok"], row["error"], row["duration_ms"], req_text[:MAX_REQUEST],
                 row["request_sha256"], resp_full[:MAX_RESPONSE], row["response_sha256"], row["response_truncated"], prev, self._entry_hash(prev, row)))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return int(cur.lastrowid)

    def verify(self) -> dict:
        """Recompute the chain. `ok` is False at the first entry whose link or content hash does not match."""
        prev, n = GENESIS, 0
        self.db.row_factory = sqlite3.Row
        try:
            rows = self.db.execute("SELECT * FROM calls ORDER BY id").fetchall()
        finally:
            self.db.row_factory = None
        for r in rows:
            d = dict(r)
            n += 1
            problems = []
            if d["prev_hash"] != prev:
                problems.append("chain broken (an earlier entry is missing or changed)")
            if self._entry_hash(d["prev_hash"], d) != d["entry_hash"]:
                problems.append("entry content does not match its hash")
            if _sha(d["request_json"]) != d["request_sha256"] and len(d["request_json"]) < MAX_REQUEST:
                problems.append("request text does not match its hash")
            if not d["response_truncated"] and _sha(d["response_json"]) != d["response_sha256"]:
                problems.append("response text does not match its hash")
            if problems:
                return {"ok": False, "entries": len(rows), "first_bad_id": d["id"], "problems": problems}
            prev = d["entry_hash"]
        return {"ok": True, "entries": n, "head": prev}

    def entries(self, *, limit: int = 50, command: str | None = None, since_id: int = 0) -> list[dict]:
        self.db.row_factory = sqlite3.Row
        try:
            q, a = "SELECT id,ts,command,execution_id,ok,error,duration_ms,request_json,response_json FROM calls WHERE id>?", [since_id]
            if command:
                q, a = q + " AND command=?", a + [command]
            return [dict(r) for r in self.db.execute(q + " ORDER BY id LIMIT ?", (*a, limit))]
        finally:
            self.db.row_factory = None

    def export_jsonl(self, path) -> int:
        """Plain-text copy for archiving (one JSON object per line, including the chain hashes)."""
        self.db.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in self.db.execute("SELECT * FROM calls ORDER BY id")]
        finally:
            self.db.row_factory = None
        Path(path).write_text("".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        return len(rows)
