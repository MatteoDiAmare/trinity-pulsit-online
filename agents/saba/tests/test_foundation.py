import asyncio
import io
import unittest
from core.exploration_policy import Opportunity, rank
from core.task_scheduler import TaskScheduler
from memory.experience_store import ExperienceStore
from security.permissions import Permissions, PermissionDenied
from security.audit import Audit
from tools.trading_adapter import PaperTradingAdapter


class FoundationTests(unittest.TestCase):
    def test_policy(self):
        ranked = rank([Opportunity("a", 2, 0, 1), Opportunity("b", 5, 0, 1)])
        self.assertEqual(ranked[0].name, "b")

    def test_scheduler(self):
        async def job():
            return 7
        self.assertEqual(asyncio.run(TaskScheduler().run([job, job])), [7, 7])

    def test_memory(self):
        store = ExperienceStore(":memory:")
        store.add("test", {"ok": True})
        self.assertEqual(store.recent()[0]["payload"]["ok"], True)
        store.close()

    def test_default_deny(self):
        with self.assertRaises(PermissionDenied):
            Permissions().require("live_trade")

    def test_audit(self):
        sink = io.StringIO()
        Audit(sink).record("demo", capability="paper")
        self.assertIn("demo", sink.getvalue())

    def test_paper_only(self):
        self.assertEqual(PaperTradingAdapter().submit("TEST", "buy", 1)["mode"], "paper")


if __name__ == "__main__":
    unittest.main()
