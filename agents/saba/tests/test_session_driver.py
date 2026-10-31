import datetime as dt
import importlib.util
import io
import json
import types
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("run_session", Path(__file__).resolve().parents[1] / "integration" / "run_session.py")
rs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rs)


class World:
    """Simulated Trinity + clock. A task runs for `minutes`, costs `rate` USD per minute (as measured from the session file),
    and Trinity records its cost only if it ends normally."""

    def __init__(self, task_minutes, rate, cycles_used=0, max_cycles=12, measurable=True, record_cost=True, halted=None):
        self.t = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
        self.task_minutes, self.rate, self.measurable, self.record_cost = task_minutes, rate, measurable, record_cost
        self.cycles_used, self.max_cycles, self.halted = cycles_used, max_cycles, halted
        self.execs, self.calls, self.log = {}, [], io.StringIO()
        self.current = None

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += dt.timedelta(seconds=s)

    def minutes_run(self, e):
        end = e.get("terminated_at") or self.t
        return min((end - e["start"]).total_seconds() / 60, self.task_minutes)

    fail_with = None

    def api(self, method, path, body=None):
        self.calls.append((method, path))
        if method == "POST" and path.endswith("/task"):
            assert body["max_turns"] == 300 and body["async_mode"] is True
            self.messages = getattr(self, "messages", []) + [body["message"]]
            eid = f"e{len(self.execs) + 1}"
            self.execs[eid] = {"start": self.t}
            self.current = eid
            self.cycles_used += 1
            return {"execution_id": eid}
        if method == "POST" and path.endswith("/terminate"):
            self.execs[path.split("/")[-2]]["terminated_at"] = self.t
            return {}
        if method == "GET":
            eid = path.split("/")[-1]
            e = self.execs[eid]
            if self.fail_with:
                return {"status": "failed", "error": self.fail_with, "cost": None}
            done = self.minutes_run(e) >= self.task_minutes or "terminated_at" in e
            if not done:
                return {"status": "running"}
            if "terminated_at" in e:
                return {"status": "terminated", "cost": None}
            return {"status": "success", "cost": round(self.task_minutes * self.rate, 4) if self.record_cost else None, "response": "done"}
        raise AssertionError((method, path))

    def running_cost(self, container, since):
        if not self.measurable or self.current is None:
            return None
        return self.minutes_run(self.execs[self.current]) * self.rate

    def status(self, container):
        return {"halted": self.halted, "cycles_last_24h": self.cycles_used,
                "budget": {"max_cycles_per_day": self.max_cycles, "max_experiments_per_cycle": 20, "max_seconds_per_cycle": 1800}}

    def run(self, minutes=87.5, cap=15.0, est=0.125):
        a = types.SimpleNamespace(minutes=minutes, max_turns=300, agent="saba", container="agent-saba", min_task_minutes=10, max_cost_usd=cap,
                                  est_usd_per_min=est, max_tasks=getattr(self, "max_tasks", 0))
        deps = {"now": self.now, "sleep": self.sleep, "api": self.api, "bridge_status": self.status, "running_cost": self.running_cost,
                "print": lambda s: None, "logfile": self.log, "brief": getattr(self, "brief", None)}
        out = rs.run_session(a, deps)
        out["events"] = [json.loads(l) for l in self.log.getvalue().splitlines()]
        return out


class CostFormulaTests(unittest.TestCase):
    def test_formula_reproduces_recorded_costs(self):
        t1 = {"input_tokens": 42, "output_tokens": 38349, "cache_read_input_tokens": 2124997, "cache_creation_input_tokens": 75039}
        t2 = {"input_tokens": 49, "output_tokens": 33921, "cache_read_input_tokens": 2678141, "cache_creation_input_tokens": 72430}
        self.assertAlmostEqual(rs.usage_cost(t1), 1.663, delta=0.005)         # recorded by Trinity: 1.6630941
        self.assertAlmostEqual(rs.usage_cost(t2), 1.75, delta=0.01)           # recorded: 1.7469843
        self.assertEqual(rs.usage_cost({}), 0)

    def test_decision_functions(self):
        self.assertTrue(rs.can_launch(10, 15))
        self.assertTrue(rs.can_launch(13, 15))
        self.assertFalse(rs.can_launch(13.5, 15))          # needs the reserve
        self.assertTrue(rs.can_launch(999, 0))             # 0 = no cap
        self.assertFalse(rs.must_stop(5, 4.99, 10))
        self.assertTrue(rs.must_stop(5, 5.0, 10))
        self.assertFalse(rs.must_stop(100, 100, 0))


class GuardTests(unittest.TestCase):
    def test_cap_is_enforced_during_a_running_task(self):
        w = World(task_minutes=10, rate=0.4)                   # 4 USD per task
        out = w.run(cap=15.0)
        self.assertEqual(out["tasks"], 4)
        self.assertIn("COST CAP", out["reason"])
        term = [e for e in out["events"] if e["event"] == "terminate_requested"]
        self.assertEqual(len(term), 1)
        self.assertEqual(term[0]["reason"], "cost_cap")
        self.assertLessEqual(out["spent"], 15.0 + 0.5)         # overshoot bounded by one 60 s measuring interval at 0.4 USD/min
        self.assertGreaterEqual(out["spent"], 15.0)
        first = [e for e in out["events"] if e["event"] == "task_finished"][0]
        self.assertEqual((first["cost_source"], first["cost_usd"]), ("recorded", 4.0))
        last = [e for e in out["events"] if e["event"] == "task_finished"][-1]
        self.assertEqual(last["cost_source"], "measured_from_session")   # terminated task: Trinity records no cost, we measured it

    def test_no_new_task_without_reserve(self):
        w = World(task_minutes=10, rate=0.675)                 # 6.75 per task: 6.75, 13.5 -> 13.5 + 2 > 15
        out = w.run(cap=15.0)
        self.assertEqual(out["tasks"], 2)
        self.assertIn("COST CAP", out["reason"])
        self.assertFalse([e for e in out["events"] if e["event"] == "terminate_requested"])

    def test_unreadable_session_falls_back_to_time_estimate_and_cap_still_holds(self):
        w = World(task_minutes=60, rate=0.4, measurable=False, record_cost=False)
        out = w.run(minutes=87.5, cap=3.0, est=0.125)          # 3.0 USD at 0.125/min = 24 min
        term = [e for e in out["events"] if e["event"] == "terminate_requested"][0]
        self.assertEqual(term["reason"], "cost_cap")
        self.assertAlmostEqual(term["running_usd"], 3.0, delta=0.2)
        fin = [e for e in out["events"] if e["event"] == "task_finished"][0]
        self.assertEqual(fin["cost_source"], "estimated_from_time")

    def test_pending_cost_of_failed_task_is_counted_from_the_measurement(self):
        w = World(task_minutes=15, rate=0.15, record_cost=False)       # finished but Trinity recorded nothing
        out = w.run(cap=15.0)
        fins = [e for e in out["events"] if e["event"] == "task_finished"]
        self.assertTrue(all(f["cost_source"] == "measured_from_session" for f in fins))
        self.assertAlmostEqual(fins[0]["cost_usd"], 15 * 0.15, delta=0.01)

    def test_deadline_terminates_and_ends_the_session(self):
        w = World(task_minutes=120, rate=0.01)
        out = w.run(minutes=25, cap=15.0)
        self.assertEqual(out["tasks"], 1)
        term = [e for e in out["events"] if e["event"] == "terminate_requested"][0]
        self.assertEqual(term["reason"], "deadline")
        self.assertLessEqual((w.t - dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)).total_seconds(), 25 * 60 + 90)

    def test_budget_and_killswitch_stop_before_any_task(self):
        out = World(10, 0.1, cycles_used=12, max_cycles=12).run()
        self.assertEqual(out["tasks"], 0)
        self.assertIn("BUDGET", out["reason"])
        out = World(10, 0.1, halted={"reason": "operator"}).run()
        self.assertEqual(out["tasks"], 0)
        self.assertIn("kill-switch", out["reason"])

    def test_stops_when_too_little_time_is_left(self):
        w = World(task_minutes=10, rate=0.05)
        out = w.run(minutes=25, cap=0)
        self.assertEqual(out["tasks"], 2)                       # 10 + 10 min, 5 min left < 10
        self.assertIn("min left", out["reason"])

    def test_max_tasks_and_custom_brief(self):
        w = World(task_minutes=10, rate=0.05)
        w.max_tasks, w.brief = 1, "CUSTOM {n} {end} {left} {task_end}"
        out = w.run(minutes=87.5)
        self.assertEqual(out["tasks"], 1)
        self.assertIn("max_tasks=1", out["reason"])
        self.assertTrue(w.messages[0].startswith("/academy-cycle\n\nCUSTOM 1 "))
        w2 = World(task_minutes=10, rate=0.05)
        w2.run(minutes=25)
        self.assertIn("Session brief from the operator", w2.messages[0])          # the default brief is unchanged

    def test_subscription_limit_stops_the_session_instead_of_looping(self):
        w = World(task_minutes=10, rate=0.1)
        w.fail_with = "Subscription usage limit: You've hit your session limit"
        out = w.run(minutes=87.5)
        self.assertEqual(out["tasks"], 1)
        self.assertIn("SUBSCRIPTION LIMIT", out["reason"])

    def test_repeated_instant_failures_stop_the_session(self):
        w = World(task_minutes=10, rate=0.1)
        w.fail_with = "agent crashed"
        out = w.run(minutes=87.5)
        self.assertEqual(out["tasks"], 3)
        self.assertIn("consecutive tasks failed immediately", out["reason"])

    def test_driver_only_uses_task_and_execution_endpoints(self):
        w = World(task_minutes=10, rate=0.1)
        w.run(minutes=30)
        for method, path in w.calls:
            self.assertRegex(path, r"^/api/agents/saba/(task|executions/e\d+(/terminate)?)$")
            self.assertNotIn("schedule", path)
            self.assertNotIn("guardrails", path)
            self.assertNotIn("settings", path)


if __name__ == "__main__":
    unittest.main()
