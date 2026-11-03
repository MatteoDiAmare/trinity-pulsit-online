#!/usr/bin/env python3
"""Archive Saba's Trinity execution logs (her reasoning text, every tool call and result) before Trinity's retention window removes them.

Trinity keeps execution logs only for a limited time (execution_log_retention_days, currently 5). This copies each finished execution
of the agent into an append-only local archive:  <out>/<execution_id>.json.gz  +  <out>/manifest.jsonl (sha256, size, status, cost).
Existing files are never overwritten. Secrets are masked before writing. Nothing in Trinity is changed; the login token is read from
the header file by integration/trinity_api.sh (never from the command line).

Usage: integration/archive_executions.py [--agent saba] [--out ~/.local/share/saba-audit/trinity-executions] [--limit 200]
Run it at least every few days (cron on the host, or by hand); it is idempotent.
"""
import argparse
import gzip
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MASK = re.compile(r"(ghp_|gho_|github_pat_|sk-ant-|trinity_mcp_|Bearer\s+)[A-Za-z0-9_\-\.]{8,}")


def api(path):
    r = subprocess.run([str(HERE / "trinity_api.sh"), "GET", path], capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise RuntimeError(f"{path}: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--agent", default="saba")
    p.add_argument("--out", default="~/.local/share/saba-audit/trinity-executions")
    p.add_argument("--limit", type=int, default=200)
    a = p.parse_args(argv)
    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    manifest = out / "manifest.jsonl"
    seen = {json.loads(l)["execution_id"] for l in manifest.read_text().splitlines()} if manifest.exists() else set()
    rows = api(f"/api/agents/{a.agent}/executions?limit={a.limit}")
    new = skipped = failed = 0
    for ex in rows:
        eid = ex["id"]
        if eid in seen or ex.get("status") in ("running", "pending", "queued"):
            skipped += 1
            continue
        try:
            log = api(f"/api/agents/{a.agent}/executions/{eid}/log")
        except RuntimeError as exc:
            print(f"skip {eid}: {exc}", file=sys.stderr)
            failed += 1
            continue
        text = MASK.sub(lambda m: m.group(1) + "***", json.dumps({"execution": {k: ex.get(k) for k in ("id", "schedule_id", "status", "started_at", "completed_at",
                                                                                                           "duration_ms", "cost", "message")},
                                                                "log": log}, ensure_ascii=False, sort_keys=True))
        target = out / f"{eid}.json.gz"
        if target.exists():
            skipped += 1
            continue
        raw = text.encode("utf-8")
        with gzip.open(target, "wb") as fh:
            fh.write(raw)
        target.chmod(0o600)
        with manifest.open("a") as fh:
            fh.write(json.dumps({"execution_id": eid, "status": ex.get("status"), "started_at": ex.get("started_at"), "bytes": len(raw),
                                 "sha256": hashlib.sha256(raw).hexdigest(), "file": target.name}) + "\n")
        new += 1
    print(json.dumps({"archived_new": new, "already_archived_or_running": skipped, "failed": failed, "directory": str(out)}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
