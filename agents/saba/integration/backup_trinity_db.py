#!/usr/bin/env python3
"""Verified host-side backup of Trinity's SQLite database, independent of Trinity's own scheduler.

Why: Trinity's nightly backup job runs 03:30 UTC with a 1-hour misfire grace. When the host is suspended at that time the run is skipped, not delayed
(log: "Run time of job ... was missed by 5:38:24"), so backups silently stop. This script takes a consistent ONLINE backup with SQLite's backup API inside
the backend container (no stop, no restart, nothing in the platform is changed), copies it to the host, and verifies it.

  <out>/trinity-db-YYYYMMDDTHHMMSSZ.db   the backup (mode 600; the directory is 700; it holds the platform's data - never commit or share it)
  <out>/status.json, <out>/ALERT          same monitoring contract as archive_executions.py

Modes: (default) take + verify a backup | --verify  re-verify every backup on disk | --status [--max-age-hours 36] | --if-stale HOURS (catch-up run)
Verification: transfer checksum (inside vs outside the container), PRAGMA integrity_check, same set of tables as the live database, row counts of the stable
tables identical and of the busy tables within a small tolerance, and the schema-migration version equal to the live one.
Retention: keeps the newest 14 and never deletes a backup newer than 3 days; removal only after a newer verified backup exists.
Usage: integration/backup_trinity_db.py [--container trinity-backend] [--db /data/trinity.db] [--out ~/.local/share/saba-audit/trinity-db-backups]
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("archive_executions", HERE / "archive_executions.py")
ax = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ax)          # reuse the status / alert / permission helpers

STABLE = ("users", "agent_ownership", "mcp_api_keys", "schema_migrations", "agent_permissions")   # must match exactly (tables that may not exist are skipped)
KEEP, KEEP_DAYS = 14, 3
INSIDE = r'''
import sqlite3, hashlib, json, sys, os
src_path, dst = sys.argv[1], sys.argv[2]
src = sqlite3.connect("file:%s?mode=ro" % src_path, uri=True, timeout=30)
dstc = sqlite3.connect(dst)
with dstc:
    src.backup(dstc)                      # consistent online snapshot (SQLite backup API)
dstc.close()
live = {t: src.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0] for (t,) in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
mig = None
try:
    mig = src.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
except Exception:
    pass
print(json.dumps({"sha256": hashlib.sha256(open(dst, "rb").read()).hexdigest(), "bytes": os.path.getsize(dst), "live_counts": live, "live_migration": mig}))
'''


def sh(*args, timeout=300):
    r = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise ax.ArchiveError(f"{' '.join(args[:3])}: {r.stderr.strip()[:240]}")
    return r.stdout


def verify_file(path: Path, live_counts=None, live_migration=None) -> list:
    problems = []
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        ok = db.execute("PRAGMA integrity_check").fetchall()
        if ok != [("ok",)]:
            problems.append(f"integrity_check: {ok[:2]}")
        counts = {t: db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        mig = None
        try:
            mig = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        except sqlite3.Error:
            pass
        db.close()
    except sqlite3.Error as exc:
        return [f"cannot open as SQLite: {exc}"]
    if not counts:
        problems.append("backup contains no tables")
    if live_counts is not None:
        if set(counts) != set(live_counts):
            problems.append(f"table set differs: {sorted(set(counts) ^ set(live_counts))[:5]}")
        for t in STABLE:
            if t in live_counts and t in counts and counts[t] != live_counts[t]:
                problems.append(f"stable table {t}: backup {counts[t]} rows, live {live_counts[t]}")
        for t, n in live_counts.items():
            if t in counts and t not in STABLE and abs(counts[t] - n) > max(25, 0.05 * n):
                problems.append(f"busy table {t}: backup {counts[t]} rows, live {n}")
        if live_migration is not None and mig != live_migration:
            problems.append(f"schema migration version {mig} != live {live_migration}")
    return problems


def take(a, out: Path) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = out / f"trinity-db-{stamp}.db"
    remote = f"/tmp/trinity-backup-{stamp}.db"
    try:
        info = json.loads(sh("docker", "exec", "-i", a.container, "python3", "-c", INSIDE, a.db, remote, timeout=600) or "{}")
    except ValueError:
        raise ax.ArchiveError("backup helper in the container returned no result")
    fd, tmp = tempfile.mkstemp(dir=out, prefix=".tmp-", suffix=".db")
    os.close(fd)
    try:
        with open(tmp, "wb") as fh:           # stream out of the container: `docker cp` cannot read the container's tmpfs /tmp
            r = subprocess.run(["docker", "exec", a.container, "cat", remote], stdout=fh, stderr=subprocess.PIPE, timeout=600)
        if r.returncode:
            raise ax.ArchiveError(f"could not read the backup out of the container: {r.stderr.decode()[:200]}")
        if hashlib.sha256(Path(tmp).read_bytes()).hexdigest() != info["sha256"]:
            raise ax.ArchiveError("checksum differs between the container copy and the host copy (transfer error)")
        problems = verify_file(Path(tmp), info["live_counts"], info["live_migration"])
        if problems:
            raise ax.ArchiveError("backup verification failed: " + "; ".join(problems[:3]))
        os.chmod(tmp, 0o600)
        os.replace(tmp, final)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
        subprocess.run(["docker", "exec", a.container, "rm", "-f", remote], capture_output=True, timeout=60)
    return {"file": final.name, "bytes": info["bytes"], "sha256": info["sha256"][:16], "tables": len(info["live_counts"]), "migration": info["live_migration"]}


def prune(out: Path):
    files = sorted(out.glob("trinity-db-*.db"))
    old = files[:-KEEP] if len(files) > KEEP else []
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    removed = []
    for p in old:
        if datetime.fromtimestamp(p.stat().st_mtime, timezone.utc) < cutoff:
            p.unlink()
            removed.append(p.name)
    return removed


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--container", default="trinity-backend")
    p.add_argument("--db", default="/data/trinity.db")
    p.add_argument("--out", default="~/.local/share/saba-audit/trinity-db-backups")
    p.add_argument("--verify", action="store_true")
    p.add_argument("--status", action="store_true")
    p.add_argument("--max-age-hours", type=float, default=36.0)
    p.add_argument("--if-stale", type=float, default=None, metavar="HOURS")
    a = p.parse_args(argv)
    out = Path(a.out).expanduser()
    ax.prepare(out)
    if a.status:
        res = ax.status(out, a.max_age_hours)
        res.pop("auth", None), res.pop("key", None)
        print(json.dumps(res, indent=1))
        return 0 if res["ok"] else 1
    if a.verify:
        files = sorted(out.glob("trinity-db-*.db"))
        bad = {f.name: verify_file(f) for f in files}
        bad = {k: v for k, v in bad.items() if v}
        perm = [f.name for f in files if f.stat().st_mode & 0o077]
        res = {"ok": bool(files) and not bad and not perm, "backups": len(files), "newest": files[-1].name if files else None, "problems": bad, "open_permissions": perm}
        print(json.dumps(res, indent=1))
        return 0 if res["ok"] else 1
    lock = open(out / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({"skipped": "another backup run is in progress"}))
        return 0
    st = ax.read_status(out)
    if a.if_stale is not None and st.get("last_success") and ax._now() - datetime.fromisoformat(st["last_success"]) < timedelta(hours=a.if_stale):
        print(json.dumps({"skipped": "last backup is recent", "last_success": st["last_success"]}))
        return 0
    ax.write_status(out, last_attempt=ax._now().isoformat())
    try:
        res = take(a, out)
        res["pruned"] = prune(out)
        files = sorted(out.glob("trinity-db-*.db"))
        ax.write_status(out, last_success=ax._now().isoformat(), last_result="ok", last_error=None, archived_total=len(files))
        ax.clear_alert(out)
        print(json.dumps({**res, "backups_on_disk": len(files), "directory": str(out)}))
        return 0
    except (ax.ArchiveError, subprocess.SubprocessError, OSError, KeyError) as exc:
        ax.write_status(out, last_result="failed", last_error=str(exc)[:500])
        ax.raise_alert(out, "Trinity DB backup: " + str(exc)[:480])
        print(json.dumps({"error": str(exc)[:500]}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
