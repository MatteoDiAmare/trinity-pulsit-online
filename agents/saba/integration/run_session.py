#!/usr/bin/env python3
"""Runs consecutive /academy-cycle tasks on the existing Saba through Trinity for a fixed total time.

* One task at a time, each with max_turns (default 300). The next task continues from the saved research (notebook, lessons, registry).
* Before every start it asks the Academy bridge for status: stops if the kill-switch is set or the daily cycle budget is used up
  (budgets are NOT changed - the stop reason is logged). It never creates or enables a schedule and changes no Trinity setting.
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


def api(method, path, body=None):
    cmd = [API, method, path] + ([json.dumps(body)] if body is not None else [])
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:400])
    return json.loads(out.stdout) if out.stdout.strip() else {}


def bridge_status(container):
    p = subprocess.run(["docker", "exec", "-i", container, "bash", "-lc", "~/.local/share/saba/academy-bridge"],
                       input='{"command":"status"}', capture_output=True, text=True, timeout=120)
    return json.loads(p.stdout)


def now():
    return dt.datetime.now(dt.timezone.utc)


def log(fh, **ev):
    ev["t"] = now().isoformat(timespec="seconds")
    line = json.dumps(ev, ensure_ascii=False)
    print(line, flush=True)
    fh.write(line + "\n")
    fh.flush()


BRIEF = """Session brief from the operator (Matteo). This is an extended autonomous learning session on the Academy.
- Session: task {n} of a session that ends at {end} UTC (about {left} minutes of working time remain; plan to finish this task, with finish_cycle and a short report, before {task_end} UTC). Further tasks may follow and will continue from your saved research, so keep your notebook/lessons accurate.
- Goal: find out whether you can improve your out-of-sample hit rate and expected net result per trade. You choose hypotheses, instruments, strategies and experiments yourself. Use and extend your research memory (notebook, lessons, dead ends); build on earlier findings rather than repeating them.
- Quality bar: judge results net of costs and out of sample. Lucky validation passes are noise (you have seen this twice); every registered experiment counts as a trial and raises the luck benchmark; final tests are scarce and can only confirm, never tune.
- Bridge notes: send {{"command":"help"}} for exact JSON of every command; start_cycle returns a briefing; register_hypothesis accepts run_in_cycle; add_lesson accepts conclude_hypothesis; test_strategy shows the TRAIN portion only (validation is reachable only via registered, counted experiments). Experiments are accepted within 1800 s after start_cycle (check status/budget); the daily cycle budget is enforced by the bridge. If a budget stops you, use the remaining time on analysis, strategy development on train data and consolidating lessons, and say so in your report.
- Rules: simulation only, no live trading or accounts, no schedules, do not change Trinity or global settings. Report at the end: what you tested, what you learned, and whether anything you learned changed your later experiments.
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=120)
    ap.add_argument("--max-turns", type=int, default=300)
    ap.add_argument("--agent", default="saba")
    ap.add_argument("--container", default="agent-saba")
    ap.add_argument("--log", required=True)
    ap.add_argument("--min-task-minutes", type=float, default=10)
    ap.add_argument("--max-cost-usd", type=float, default=0, help="stop launching tasks once the (API-equivalent) cost reaches this; 0 = no cap. "
                    "Tasks that Trinity records no cost for (failed ones) are estimated at --est-usd-per-min")
    ap.add_argument("--est-usd-per-min", type=float, default=0.125)
    a = ap.parse_args()
    start = now()
    end = start + dt.timedelta(minutes=a.minutes)
    fh = open(a.log, "a")
    log(fh, event="session_start", end=end.isoformat(timespec="seconds"), minutes=a.minutes, max_turns=a.max_turns)
    n = 0
    spent = 0.0
    stop_reason = "time"
    while True:
        remaining = (end - now()).total_seconds()
        if a.max_cost_usd and spent >= a.max_cost_usd:
            stop_reason = f"COST CAP: ${spent:.2f} >= ${a.max_cost_usd:.2f}"
            break
        if remaining < a.min_task_minutes * 60:
            stop_reason = f"only {remaining / 60:.1f} min left (< {a.min_task_minutes:g})"
            break
        try:
            st = bridge_status(a.container)
        except Exception as exc:
            stop_reason = f"cannot read bridge status: {exc}"
            break
        log(fh, event="pre_check", halted=st.get("halted"), cycles_last_24h=st.get("cycles_last_24h"), budget=st.get("budget"))
        if st.get("halted"):
            stop_reason = f"kill-switch set: {st['halted']}"
            break
        if st["cycles_last_24h"] >= st["budget"]["max_cycles_per_day"]:
            stop_reason = f"BUDGET: cycle budget used ({st['cycles_last_24h']}/{st['budget']['max_cycles_per_day']} in 24 h); budgets were not changed"
            break
        n += 1
        task_end = min(end, now() + dt.timedelta(minutes=55))
        msg = "/academy-cycle\n\n" + BRIEF.format(n=n, end=end.strftime("%H:%M"), left=int(remaining / 60), task_end=task_end.strftime("%H:%M"))
        resp = api("POST", f"/api/agents/{a.agent}/task", {"message": msg, "async_mode": True, "max_turns": a.max_turns})
        eid = resp.get("execution_id") or resp.get("id")
        t0 = now()
        log(fh, event="task_started", n=n, execution_id=eid)
        status = "running"
        terminated = False
        while True:
            time.sleep(20)
            try:
                ex = api("GET", f"/api/agents/{a.agent}/executions/{eid}")
            except Exception as exc:
                log(fh, event="poll_error", error=str(exc)[:200])
                continue
            status = ex.get("status")
            if status not in ("running", "queued", "pending", "started"):
                break
            if now() >= end and not terminated:
                try:
                    api("POST", f"/api/agents/{a.agent}/executions/{eid}/terminate")
                    terminated = True
                    log(fh, event="terminate_requested_at_deadline", n=n)
                except Exception as exc:
                    log(fh, event="terminate_failed", error=str(exc)[:200])
        minutes = (now() - t0).total_seconds() / 60
        spent += ex.get("cost") if ex.get("cost") is not None else minutes * a.est_usd_per_min
        log(fh, event="task_finished", n=n, spent_usd_so_far=round(spent, 2), execution_id=eid, status=status, minutes=round((now() - t0).total_seconds() / 60, 1),
            error=(ex.get("error") or "")[:300], cost=ex.get("cost"), terminated_at_deadline=terminated,
            response_tail=(ex.get("response") or "")[-1500:])
    log(fh, event="session_end", tasks=n, reason=stop_reason, elapsed_minutes=round((now() - start).total_seconds() / 60, 1))


if __name__ == "__main__":
    sys.exit(main())
