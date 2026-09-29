"""过号恢复：叫号 → 过号 → 重排队尾 → 仍可正常组批登船。"""
from __future__ import annotations

import unittest

from app.service import BoatError
from tests.helpers import _assign_vests, make_service


class MissedRequeueTest(unittest.TestCase):
    def test_missed_then_requeue_to_tail(self):
        svc = make_service()
        t1 = svc.find_ticket("A-001")

        called = svc.call_next("PEDAL")
        self.assertEqual(called["ticket_no"], "A-001")
        svc.mark_missed(t1["id"])
        self.assertEqual(svc.find_ticket("A-001")["status"], "MISSED")

        # 过号后下一号顶上
        called2 = svc.call_next("PEDAL")
        self.assertEqual(called2["ticket_no"], "A-002")

        # A-001 重排：排到队尾
        r = svc.requeue_missed(t1["id"])
        self.assertEqual(r["status"], "WAITING")
        self.assertIsNotNone(r["priority_seq"])
        pool = svc.batch_workbench()["pool"]
        pos = {t["ticket_no"]: i for i, t in enumerate(pool)}
        self.assertGreater(pos["A-001"], pos["A-002"])

        # 重排后可正常走完组批
        boat = svc.conn.execute(
            "SELECT * FROM boats WHERE name='青鸾号'").fetchone()
        b = svc.create_batch(boat["id"], [t1["id"]])
        _assign_vests(svc, b["id"])
        self.assertTrue(svc.gate_scan("A-001", "G1")["allowed"])

    def test_requeue_requires_missed_state(self):
        svc = make_service()
        t = svc.find_ticket("A-001")  # WAITING
        with self.assertRaisesRegex(BoatError, "过号"):
            svc.requeue_missed(t["id"])

    def test_scheduler_auto_missed_after_timeout(self):
        """进程内任务：叫号后超时自动判过号。"""
        svc = make_service()
        svc.call_next("PEDAL")  # A-001
        svc.conn.execute(
            "UPDATE tickets SET called_at=datetime('now','-4 minutes')"
            " WHERE ticket_no='A-001'")
        svc.conn.commit()
        r = svc.scheduler_tick()
        self.assertIn("A-001", r["missed"])
        self.assertEqual(svc.find_ticket("A-001")["status"], "MISSED")


if __name__ == "__main__":
    unittest.main()
