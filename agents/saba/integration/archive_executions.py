#!/usr/bin/env python3
"""Archive Saba's Trinity execution logs (her reasoning text, every tool call and result) before Trinity's retention window removes them.

Trinity keeps execution logs only for a limited time (execution_log_retention_days, currently 5). This copies each finished execution
of the agent into an append-only local archive:

  <out>/<execution_id>.json.gz   the masked log (mode 600)
  <out>/manifest.jsonl           one line per file: sha256 of the uncompressed bytes, size, status; new lines are hash-chained
  <out>/status.json              last attempt / last success / result, for monitoring
  <out>/ALERT                    exists only while the last run failed or the archive is stale (the visible failure signal)

Authentication: the dedicated read-only ops key (~/.config/saba-trinity/archive.hdr, see trinity_archive_key.sh) when it exists, otherwise the
operator login token. Errors name the cause (key revoked / route refused / login expired).

Modes (one per invocation):
  (default)            archive new finished executions
  --verify             integrity check of the archive on disk (hashes, chain, orphans, permissions, secret scan); exit 1 on any problem
  --status [--max-age-hours 36]   print status.json; exit 1 if the last run failed or the last success is older than the limit

Safety: the login token is read from the header file by integration/trinity_api.sh (never from the command line). Secrets are masked
before anything is written, and `--verify` re-scans the stored text for secret patterns. Existing files are never overwritten; nothing in
Trinity is changed. A lock file prevents parallel runs. Failures write the ALERT file and a syslog line (logger -t saba-audit).

Usage: integration/archive_executions.py [--agent saba] [--out ~/.local/share/saba-audit/trinity-executions] [--limit 500] [--if-stale HOURS]
"""
import argparse
import fcntl
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Masking: known token formats, JWTs, private keys and key/value assignments of secrets. Over-masking is acceptable in an audit copy.
MASKS = [
    (re.compile(r"(?<![A-Za-z0-9])(ghp_|gho_|ghs_|github_pat_|sk-ant-|sk-|xox[bap]-|trinity_mcp_|AKIA)[A-Za-z0-9_\-\.]{8,}"), lambda m: m.group(1) + "***"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.=]{8,}", re.I), lambda m: m.group(1) + "***"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), lambda m: "eyJ***.jwt"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S), lambda m: "-----PRIVATE KEY REDACTED-----"),
    (re.compile(r"((?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}\\]{12,}", re.I),
     lambda m: m.group(1) + "***"),
]
RUNNING = ("running", "pending", "queued")
GENESIS = "0" * 64


def mask(text: str) -> str:
    for rx, repl in MASKS:
        text = rx.sub(repl, text)
    return text


def find_secrets(text: str) -> list:
    """Patterns that are still present after masking (a masked value like 'ghp_***' no longer matches the 8+ character requirement)."""
    return [i for i, (rx, _) in enumerate(MASKS) if rx.search(text)]


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class ArchiveError(RuntimeError):
    pass


ARCHIVE_KEY_DEFAULT = "~/.config/saba-trinity/archive.hdr"
KEY_ROTATE_DAYS = 90


def auth_source(explicit=None):
    """(path, kind): the dedicated read-only ops key when it exists (kind 'archive_key'), otherwise the operator's login token (kind 'admin_jwt')."""
    for cand in (explicit, os.environ.get("SABA_ARCHIVE_KEY_FILE"), ARCHIVE_KEY_DEFAULT):
        if cand and Path(cand).expanduser().is_file():
            return str(Path(cand).expanduser()), "archive_key"
    return None, "admin_jwt"


def key_info(path=None):
    """Age of the archive key from its (secret-free) metadata file, or None."""
    meta = Path(path or os.environ.get("SABA_ARCHIVE_KEY_FILE") or ARCHIVE_KEY_DEFAULT).expanduser().with_suffix(".meta")
    try:
        m = json.loads(meta.read_text())
        return {"key_id": m["key_id"], "age_days": (_now() - datetime.fromisoformat(m["created_at"])).days}
    except (OSError, ValueError, KeyError):
        return None


def api(path, auth_file=None):
    key_path, kind = auth_source(auth_file)
    env = {**os.environ, **({"SABA_AUTH_FILE": key_path} if key_path else {})}
    r = subprocess.run([str(HERE / "trinity_api.sh"), "GET", path], capture_output=True, text=True, timeout=120, env=env)
    if r.returncode:
        err = r.stderr.strip()[:200]
        if "HTTP 401" in err or "not logged in" in err:
            if kind == "archive_key":
                raise ArchiveError("Trinity rejected the archive key (revoked, owner suspended or key replaced): create a new one with integration/trinity_archive_key.sh create")
            raise ArchiveError("Trinity login expired or missing: run integration/trinity_login.sh (token lasts 7 days, ends at backend restart)")
        if "HTTP 403" in err:
            if kind == "archive_key":
                raise ArchiveError("Trinity refused this route for the ops key: the backend must run with OPS_LOG_READ_AGENTS=<agent> (restart needed) or the ops allowlist changed")
            raise ArchiveError("Trinity refused the request (HTTP 403)")
        raise ArchiveError(f"{path}: {err}")
    return json.loads(r.stdout)


# ---------------------------------------------------------------------------------------------------------------- state
def _now():
    return datetime.now(timezone.utc)


def read_status(out: Path) -> dict:
    try:
        return json.loads((out / "status.json").read_text())
    except (OSError, ValueError):
        return {}


def write_status(out: Path, **kw):
    st = {**read_status(out), **kw}
    tmp = out / "status.json.tmp"
    tmp.write_text(json.dumps(st, indent=1, sort_keys=True))
    os.chmod(tmp, 0o600)
    os.replace(tmp, out / "status.json")


def raise_alert(out: Path, message: str):
    (out / "ALERT").write_text(f"{_now().isoformat()} {message}\n")
    os.chmod(out / "ALERT", 0o600)
    try:
        subprocess.run(["logger", "-p", "user.err", "-t", "saba-audit", f"execution-log archiving: {message}"], timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def clear_alert(out: Path):
    (out / "ALERT").unlink(missing_ok=True)


def prepare(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    for d in {out, out.parent}:
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass


def manifest_lines(out: Path) -> list:
    m = out / "manifest.jsonl"
    return m.read_text().splitlines() if m.exists() else []


# ---------------------------------------------------------------------------------------------------------------- archive
def archive(a, out: Path) -> dict:
    manifest = out / "manifest.jsonl"
    lines = manifest_lines(out)
    seen = {json.loads(l)["execution_id"] for l in lines}
    prev = _sha(("\n".join(lines) + ("\n" if lines else "")).encode()) if lines and "prev" not in json.loads(lines[-1]) else \
        (json.loads(lines[-1])["entry_sha256"] if lines else GENESIS)
    rows = api(f"/api/agents/{a.agent}/executions?limit={a.limit}")
    new = skipped = still_running = 0
    failures = []
    for ex in rows:
        eid = ex["id"]
        if eid in seen:
            skipped += 1
            continue
        if ex.get("status") in RUNNING:
            still_running += 1
            continue
        try:
            log = api(f"/api/agents/{a.agent}/executions/{eid}/log")
        except ArchiveError as exc:
            if "login expired" in str(exc) or "archive key" in str(exc) or "ops key" in str(exc):
                raise
            failures.append(f"{eid}: {exc}")
            continue
        text = mask(json.dumps({"execution": {k: ex.get(k) for k in ("id", "schedule_id", "status", "started_at", "completed_at", "duration_ms", "cost",
                                                                       "message")}, "log": log}, ensure_ascii=False, sort_keys=True))
        if find_secrets(text):
            failures.append(f"{eid}: secret pattern survived masking; not archived")
            continue
        raw = text.encode("utf-8")
        target = out / f"{eid}.json.gz"
        if target.exists():                                       # present but not in the manifest: never overwrite, report
            failures.append(f"{eid}: file exists but is not in the manifest")
            continue
        fd, tmp = tempfile.mkstemp(dir=out, prefix=".tmp-", suffix=".gz")
        try:
            with os.fdopen(fd, "wb") as raw_fh, gzip.GzipFile(fileobj=raw_fh, mode="wb", mtime=0) as gz:
                gz.write(raw)
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)                                # atomic: a crash leaves no half-written archive file
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        entry = {"execution_id": eid, "status": ex.get("status"), "started_at": ex.get("started_at"), "bytes": len(raw), "sha256": _sha(raw),
                 "file": target.name, "prev": prev}
        entry["entry_sha256"] = _sha(json.dumps({k: v for k, v in entry.items()}, sort_keys=True).encode())
        with manifest.open("a") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        os.chmod(manifest, 0o600)
        prev = entry["entry_sha256"]
        new += 1
    summary = {"archived_new": new, "already_archived": skipped, "still_running": still_running, "failed": len(failures), "failures": failures[:10],
               "listed": len(rows), "limit_reached": len(rows) >= a.limit, "directory": str(out)}
    return summary


# ---------------------------------------------------------------------------------------------------------------- verify
def verify(out: Path) -> dict:
    problems = []
    lines = manifest_lines(out)
    files = {p.name for p in out.glob("*.json.gz")}
    listed, prev, chained = set(), None, False
    for n, line in enumerate(lines, 1):
        try:
            e = json.loads(line)
        except ValueError:
            problems.append(f"manifest line {n} is not valid JSON")
            continue
        listed.add(e["file"])
        p = out / e["file"]
        if not p.exists():
            problems.append(f"{e['file']}: file missing")
        else:
            try:
                raw = gzip.open(p, "rb").read()
            except (OSError, EOFError) as exc:
                problems.append(f"{e['file']}: unreadable ({exc})")
                continue
            if _sha(raw) != e["sha256"] or len(raw) != e["bytes"]:
                problems.append(f"{e['file']}: content differs from manifest hash")
            if find_secrets(raw.decode("utf-8", "replace")):
                problems.append(f"{e['file']}: secret pattern found in stored text")
            if (p.stat().st_mode & 0o077):
                problems.append(f"{e['file']}: permissions too open ({oct(p.stat().st_mode & 0o777)})")
        if "prev" in e:                                           # chained entries (legacy lines before the first chained one carry no link)
            body = {k: v for k, v in e.items() if k != "entry_sha256"}
            if _sha(json.dumps(body, sort_keys=True).encode()) != e.get("entry_sha256"):
                problems.append(f"manifest line {n}: entry hash mismatch")
            expected = prev if chained else _sha(("\n".join(lines[:n - 1]) + ("\n" if n > 1 else "")).encode()) if n > 1 else GENESIS
            if e["prev"] != expected:
                problems.append(f"manifest line {n}: chain broken (an earlier line was removed or changed)")
            prev, chained = e["entry_sha256"], True
        elif chained:
            problems.append(f"manifest line {n}: unchained line after the chain started")
    for orphan in sorted(files - listed):
        problems.append(f"{orphan}: archive file not listed in the manifest")
    for name in ("manifest.jsonl", "status.json"):
        f = out / name
        if f.exists() and f.stat().st_mode & 0o077:
            problems.append(f"{name}: permissions too open ({oct(f.stat().st_mode & 0o777)})")
    for d in (out, out.parent):
        if d.stat().st_mode & 0o077:
            problems.append(f"directory {d} permissions too open ({oct(d.stat().st_mode & 0o777)})")
    return {"ok": not problems, "files": len(files), "manifest_lines": len(lines), "problems": problems[:25]}


def status(out: Path, max_age_hours: float) -> dict:
    st = read_status(out)
    problems = []
    if not st.get("last_success"):
        problems.append("no successful run recorded")
    else:
        age = _now() - datetime.fromisoformat(st["last_success"])
        if age > timedelta(hours=max_age_hours):
            problems.append(f"last success is {age.total_seconds() / 3600:.1f} h old (limit {max_age_hours} h)")
    if st.get("last_result") == "failed":
        problems.append(f"last run failed: {st.get('last_error')}")
    if (out / "ALERT").exists():
        problems.append("ALERT file present: " + (out / "ALERT").read_text().strip()[:200])
    _, kind = auth_source()
    ki = key_info() if kind == "archive_key" else None
    if kind == "archive_key" and ki and ki["age_days"] > KEY_ROTATE_DAYS:
        problems.append(f"archive key is {ki['age_days']} days old (rotate after {KEY_ROTATE_DAYS}): integration/trinity_archive_key.sh")
    return {"ok": not problems, "problems": problems, "auth": kind, "key": ki,
            **{k: st.get(k) for k in ("last_attempt", "last_success", "last_result", "archived_total")}}


# ---------------------------------------------------------------------------------------------------------------- main
def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--agent", default="saba")
    p.add_argument("--out", default="~/.local/share/saba-audit/trinity-executions")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--verify", action="store_true")
    p.add_argument("--status", action="store_true")
    p.add_argument("--max-age-hours", type=float, default=36.0)
    p.add_argument("--if-stale", type=float, default=None, metavar="HOURS", help="catch-up run: do nothing if the last success is younger than this")
    a = p.parse_args(argv)
    out = Path(a.out).expanduser()
    prepare(out)
    if a.status:
        res = status(out, a.max_age_hours)
        print(json.dumps(res, indent=1))
        return 0 if res["ok"] else 1
    if a.verify:
        res = verify(out)
        print(json.dumps(res, indent=1))
        return 0 if res["ok"] else 1
    lock = open(out / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({"skipped": "another archiving run is in progress"}))
        return 0
    st = read_status(out)
    if a.if_stale is not None and st.get("last_success") and _now() - datetime.fromisoformat(st["last_success"]) < timedelta(hours=a.if_stale):
        print(json.dumps({"skipped": "last success is recent", "last_success": st["last_success"]}))
        return 0
    write_status(out, last_attempt=_now().isoformat())
    try:
        summary = archive(a, out)
        check = verify(out)                                         # every run re-checks the whole archive on disk
        if not check["ok"]:
            raise ArchiveError("archive verification failed: " + "; ".join(check["problems"][:3]))
        if summary["failed"]:
            raise ArchiveError(f"{summary['failed']} execution(s) could not be archived: {'; '.join(summary['failures'][:3])}")
        if summary["limit_reached"]:
            raise ArchiveError(f"the listing hit --limit {a.limit}: older executions may be missing; raise the limit or run more often")
        write_status(out, last_success=_now().isoformat(), last_result="ok", last_error=None, archived_total=check["files"])
        clear_alert(out)
        print(json.dumps({**summary, "verified_files": check["files"]}))
        return 0
    except ArchiveError as exc:
        write_status(out, last_result="failed", last_error=str(exc)[:500])
        raise_alert(out, str(exc)[:500])
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    except Exception as exc:                                        # unexpected: still visible
        write_status(out, last_result="failed", last_error=f"{type(exc).__name__}: {exc}"[:500])
        raise_alert(out, f"{type(exc).__name__}: {exc}"[:500])
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
