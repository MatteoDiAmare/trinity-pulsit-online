#!/usr/bin/env python3
"""Runs consecutive /academy-cycle tasks on the existing Saba through Trinity for a fixed total time.

* One task at a time, each with max_turns (default 300). The next task continues from the saved research (notebook, lessons, registry).
* Before every start it asks the Academy bridge for status: stops if the kill-switch is set or the daily cycle budget is used up
  (budgets are NOT changed by this script - the stop reason is logged). It never creates or enables a schedule and changes no Trinity setting.
* Cost cap (--max-cost-usd, total for the session): Trinity records a task's cost only when it finishes normally, so the cost of a RUNNING task is
  measured from Saba's own Claude session file (token usage x the price formula calibrated on tasks with a recorded cost) every minute; the task is
  terminated as soon as spent + running reaches the cap, and no task is started unless spent + a reserve fits under the cap.
* A task still running at the deadline is terminated through Trinity's terminate endpoint.
Needs a prior integration/trinity_login.sh. Log: one JSON line per event.
"""
import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
API = str(HERE / "trinity_api.sh")
# USD per million tokens; calibrated: reproduces the recorded cost of two tasks within 0.1 % (1.662 vs 1.663, 1.747 vs 1.75)
RATES = {"input_tokens": 3.0, "output_tokens": 15.0, "cache_read_input_tokens": 0.30, "cache_creation_input_tokens": 6.0}
LAUNCH_RESERVE_USD = 2.0

SESSION_COST_SCRIPT = r'''
import glob, json, os, sys
since = sys.argv[1]
cands = []
for f in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
    recs = []
    for l in open(f):
        try:
            recs.append(json.loads(l))
        except Exception:
            pass
    first = next((r for r in recs if r.get("type") == "user"), None)
    ts = [r.get("timestamp", "") for r in recs if r.get("timestamp")]
    if first and "academy-cycle" in json.dumps(first)[:800] and ts and ts[0] >= since:
        cands.append((ts[0], recs))
if not cands:
    print(json.dumps({})); sys.exit()
recs = min(cands, key=lambda c: c[0])[1]      # the task that started FIRST at/after `since` (the one just launched)
seen = {}
for r in recs:
    m = r.get("message") or {}
    if r.get("type") == "assistant" and isinstance(m, dict) and m.get("usage"):
        seen[m.get("id") or r.get("uuid")] = m["usage"]
tot = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
for u in seen.values():
    for k in tot:
        tot[k] += u.get(k, 0) or 0
print(json.dumps(tot))
'''


def usage_cost(usage: dict) -> float:
    return sum((usage.get(k, 0) or 0) * v / 1e6 for k, v in RATES.items())


def can_launch(spent: float, cap: float, reserve: float = LAUNCH_RESERVE_USD) -> bool:
    return not cap or spent + reserve <= cap


def must_stop(spent: float, running: float, cap: float) -> bool:
    return bool(cap) and spent + running >= cap


def bridge_status(container):
    p = subprocess.run(["docker", "exec", "-i", container, "bash", "-lc", "~/.local/share/saba/academy-bridge"],
                       input='{"command":"status"}', capture_output=True, text=True, timeout=120)
    return json.loads(p.stdout)


def running_cost(container, since_iso):
    """API-equivalent cost of the newest academy task that started at/after since_iso, from Saba's own session file; None if unreadable."""
    try:
        p = subprocess.run(["docker", "exec", "-i", container, "python3", "-", since_iso], input=SESSION_COST_SCRIPT, capture_output=True,
                           text=True, timeout=60)
        usage = json.loads(p.stdout or "{}")
        return usage_cost(usage) if usage else None
    except Exception:
        return None


def api(method, path, body=None):
    cmd = [API, method, path] + ([json.dumps(body)] if body is not None else [])
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:400])
    return json.loads(out.stdout) if out.stdout.strip() else {}


def now():
    return dt.datetime.now(dt.timezone.utc)


BRIEF = """Session brief from the operator (Matteo). This is an extended autonomous learning session on the Academy.
- Session: task {n} of a session that ends at {end} UTC (about {left} minutes of working time remain; plan to finish this task, with finish_cycle and a short report, before {task_end} UTC). Further tasks may follow and will continue from your saved research, so keep your notebook/lessons accurate.
- Goal: find out whether you can improve your out-of-sample hit rate and expected net result per trade. You choose hypotheses, instruments, strategies and experiments yourself. Use and extend your research memory (notebook, lessons, dead ends); build on earlier findings rather than repeating them.
- Quality bar: judge results net of costs and out of sample. Lucky validation passes are noise (you have seen this twice); every registered experiment counts as a trial and raises the luck benchmark; final tests are scarce and can only confirm, never tune.
- Bridge notes: send {{"command":"help"}} for exact JSON of every command; start_cycle returns a briefing; register_hypothesis accepts run_in_cycle; add_lesson accepts conclude_hypothesis; test_strategy shows the TRAIN portion only (validation is reachable only via registered, counted experiments). Experiments are accepted within 1800 s after start_cycle (check status/budget); the daily cycle budget is enforced by the bridge. If a budget stops you, use the remaining time on analysis, strategy development on train data and consolidating lessons, and say so in your report.
- Rules: simulation only, no live trading or accounts, no schedules, do not change Trinity or global settings. Report at the end: what you tested, what you learned, and whether anything you learned changed your later experiments.
"""


def run_session(a, deps):
    """Core loop with injectable dependencies (so the cost guard can be tested with simulated time)."""
    start = deps["now"]()
    end = start + dt.timedelta(minutes=a.minutes)
    fh = deps["logfile"]

    def log(**ev):
        ev["t"] = deps["now"]().isoformat(timespec="seconds")
        line = json.dumps(ev, ensure_ascii=False)
        deps["print"](line)
        fh.write(line + "\n")
        fh.flush()

    log(event="session_start", end=end.isoformat(timespec="seconds"), minutes=a.minutes, max_turns=a.max_turns, max_cost_usd=a.max_cost_usd)
    n, spent, stop_reason, quick_fails = 0, 0.0, "time", 0
    while True:
        remaining = (end - deps["now"]()).total_seconds()
        if not can_launch(spent, a.max_cost_usd):
            stop_reason = f"COST CAP: spent ${spent:.2f}, cap ${a.max_cost_usd:.2f} (next task needs a ${LAUNCH_RESERVE_USD:.2f} reserve)"
            break
        if remaining < a.min_task_minutes * 60:
            stop_reason = f"only {remaining / 60:.1f} min left (< {a.min_task_minutes:g})"
            break
        try:
            st = deps["bridge_status"](a.container)
        except Exception as exc:
            stop_reason = f"cannot read bridge status: {exc}"
            break
        log(event="pre_check", halted=st.get("halted"), cycles_last_24h=st.get("cycles_last_24h"), max_cycles_per_day=st["budget"]["max_cycles_per_day"])
        if st.get("halted"):
            stop_reason = f"kill-switch set: {st['halted']}"
            break
        if st["cycles_last_24h"] >= st["budget"]["max_cycles_per_day"]:
            stop_reason = f"BUDGET: cycle budget used ({st['cycles_last_24h']}/{st['budget']['max_cycles_per_day']} in 24 h); budgets were not changed"
            break
        n += 1
        task_end = min(end, deps["now"]() + dt.timedelta(minutes=55))
        msg = "/academy-cycle\n\n" + BRIEF.format(n=n, end=end.strftime("%H:%M"), left=int(remaining / 60), task_end=task_end.strftime("%H:%M"))
        resp = deps["api"]("POST", f"/api/agents/{a.agent}/task", {"message": msg, "async_mode": True, "max_turns": a.max_turns})
        eid = resp.get("execution_id") or resp.get("id")
        t0 = deps["now"]()
        since = t0.strftime("%Y-%m-%dT%H:%M:%S")
        log(event="task_started", n=n, execution_id=eid, spent_usd=round(spent, 2))
        terminated, why, last_cost_check, running, last_running_log = False, None, 0.0, 0.0, 0.0
        while True:
            deps["sleep"](20)
            try:
                ex = deps["api"]("GET", f"/api/agents/{a.agent}/executions/{eid}")
            except Exception as exc:
                log(event="poll_error", error=str(exc)[:200])
                continue
            status = ex.get("status")
            if status not in ("running", "queued", "pending", "started"):
                break
            elapsed = (deps["now"]() - t0).total_seconds()
            if elapsed - last_cost_check >= 60:
                last_cost_check = elapsed
                measured = deps["running_cost"](a.container, since)
                # if the session file is unreadable fall back to a time-based estimate so the cap can never go unenforced
                running = measured if measured is not None else elapsed / 60 * a.est_usd_per_min
                if elapsed - last_running_log >= 300:
                    last_running_log = elapsed
                    log(event="running_cost", n=n, running_usd=round(running, 2), total_usd=round(spent + running, 2), measured=measured is not None)
            if not terminated and (must_stop(spent, running, a.max_cost_usd) or deps["now"]() >= end):
                why = "cost_cap" if must_stop(spent, running, a.max_cost_usd) else "deadline"
                try:
                    deps["api"]("POST", f"/api/agents/{a.agent}/executions/{eid}/terminate")
                    terminated = True
                    log(event="terminate_requested", n=n, reason=why, running_usd=round(running, 2), total_usd=round(spent + running, 2))
                except Exception as exc:
                    log(event="terminate_failed", error=str(exc)[:200])
        minutes = (deps["now"]() - t0).total_seconds() / 60
        final_measured = deps["running_cost"](a.container, since)
        if ex.get("cost") is not None:
            cost, source = ex["cost"], "recorded"
        elif final_measured is not None:
            cost, source = final_measured, "measured_from_session"
        else:
            cost, source = minutes * a.est_usd_per_min, "estimated_from_time"
        spent += cost
        log(event="task_finished", n=n, execution_id=eid, status=status, minutes=round(minutes, 1), cost_usd=round(cost, 2), cost_source=source,
            spent_usd_so_far=round(spent, 2), error=(ex.get("error") or "")[:300], terminated=terminated, terminated_reason=why,
            response_tail=(ex.get("response") or "")[-1500:])
        if why == "cost_cap":
            stop_reason = f"COST CAP reached during task {n}: ${spent:.2f} >= ${a.max_cost_usd:.2f}"
            break
        err = (ex.get("error") or "").lower()
        if "usage limit" in err or "session limit" in err or "rate limit" in err:
            stop_reason = f"SUBSCRIPTION LIMIT: {(ex.get('error') or '')[:160]}"      # not something to retry in a loop
            break
        quick_fails = quick_fails + 1 if (status == "failed" and minutes < 1.0) else 0
        if quick_fails >= 3:
            stop_reason = f"{quick_fails} consecutive tasks failed immediately: {(ex.get('error') or '')[:160]}"
            break
    log(event="session_end", tasks=n, reason=stop_reason, spent_usd=round(spent, 2), elapsed_minutes=round((deps["now"]() - start).total_seconds() / 60, 1))
    return {"tasks": n, "spent": spent, "reason": stop_reason}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=120)
    ap.add_argument("--max-turns", type=int, default=300)
    ap.add_argument("--agent", default="saba")
    ap.add_argument("--container", default="agent-saba")
    ap.add_argument("--log", required=True)
    ap.add_argument("--min-task-minutes", type=float, default=10)
    ap.add_argument("--max-cost-usd", type=float, default=0, help="total API-equivalent cost cap for the whole session; 0 = no cap")
    ap.add_argument("--est-usd-per-min", type=float, default=0.125, help="fallback estimate if cost can neither be recorded nor measured")
    a = ap.parse_args()
    deps = {"now": now, "sleep": time.sleep, "api": api, "bridge_status": bridge_status, "running_cost": running_cost, "print": lambda s: print(s, flush=True),
            "logfile": open(a.log, "a")}
    run_session(a, deps)


if __name__ == "__main__":
    sys.exit(main())
