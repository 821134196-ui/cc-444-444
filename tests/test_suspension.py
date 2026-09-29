"""停航中断：停航不能开批/出航，已组批自动解散回队列，恢复后可继续。"""
from __future__ import annotations

import unittest

from app.service import BoatError
from tests.helpers import _assign_vests, make_service


class SuspensionTest(unittest.TestCase):
    def test_suspended_blocks_new_batch(self):
        svc = make_service()
        svc.set_weather("SUSPEND")
        boat = svc.conn.execute(
            "SELECT * FROM boats WHERE name='青鸾号'").fetchone()
        a1 = svc.find_ticket("A-001")
        with self.assertRaisesRegex(BoatError, "停航"):
            svc.create_batch(boat["id"], [a1["id"]])

    def test_suspended_blocks_departure(self):
        svc = make_service()
        boat = svc.conn.execute(
            "SELECT * FROM boats WHERE name='青鸾号'").fetchone()
        a1 = svc.find_ticket("A-001")
        b = svc.create_batch(boat["id"], [a1["id"]])
        _assign_vests(svc, b["id"])
        svc.gate_scan("A-001", "G1")
        svc.set_weather("SUSPEND")
        with self.assertRaisesRegex(BoatError, "停航"):
            svc.depart(b["id"])

    def test_suspend_disbands_pending_batches_back_to_queue(self):
        svc = make_service()
        boat = svc.conn.execute(
            "SELECT * FROM boats WHERE name='青鸾号'").fetchone()
        a1 = svc.find_ticket("A-001")
        b = svc.create_batch(boat["id"], [a1["id"]])
        _assign_vests(svc, b["id"])  # 已部分登船也要解散

        r = svc.set_weather("SUSPEND")
        self.assertIn(b["id"], r["suspended_batches"])

        # 批次停航中止、票回队列、船释放
        self.assertEqual(svc.find_ticket("A-001")["status"], "WAITING")
        self.assertEqual(svc.conn.execute(
            "SELECT status FROM boats WHERE id=?", (boat["id"],)).fetchone()["status"],
            "DOCKED")
        self.assertEqual(svc.conn.execute(
            "SELECT status FROM batches WHERE id=?", (b["id"],)).fetchone()["status"],
            "SUSPENDED")

        # 广播已落库
        bc = svc.conn.execute(
            "SELECT * FROM broadcasts WHERE level='SUSPEND' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(bc)
        self.assertIn("停航", bc["message"])

        # 恢复后游客可重新组批走完流程
        svc.set_weather("NORMAL")
        b2 = svc.create_batch(boat["id"], [a1["id"]])
        _assign_vests(svc, b2["id"])
        self.assertTrue(svc.gate_scan("A-001", "G1")["allowed"])
        svc.depart(b2["id"], minutes=60)
        self.assertEqual(svc.find_ticket("A-001")["status"], "ON_WATER")

    def test_suspend_recalls_boats_on_water(self):
        svc = make_service()
        boat = svc.conn.execute(
            "SELECT * FROM boats WHERE name='青鸾号'").fetchone()
        a1 = svc.find_ticket("A-001")
        b = svc.create_batch(boat["id"], [a1["id"]])
        _assign_vests(svc, b["id"])
        svc.gate_scan("A-001", "G1")
        svc.depart(b["id"], minutes=60)
        trip = svc.conn.execute(
            "SELECT * FROM trips WHERE batch_id=?", (b["id"],)).fetchone()

        svc.set_weather("SUSPEND")
        ev = svc.conn.execute(
            "SELECT COUNT(*) c FROM events WHERE kind='RECALL' AND payload LIKE ?",
            (f'%"trip_id": {trip["id"]}%',)).fetchone()["c"]
        self.assertGreaterEqual(ev, 1)
        # 船仍在湖面（回港检查是另一个人工动作）
        self.assertEqual(svc.find_ticket("A-001")["status"], "ON_WATER")

    def test_manual_disband_returns_tickets_to_queue(self):
        svc = make_service()
        boat = svc.conn.execute(
            "SELECT * FROM boats WHERE name='青鸾号'").fetchone()
        a1 = svc.find_ticket("A-001")
        b = svc.create_batch(boat["id"], [a1["id"]])
        svc.disband_batch(b["id"], reason="临时取消")
        self.assertEqual(svc.find_ticket("A-001")["status"], "WAITING")
        self.assertIsNone(svc.find_ticket("A-001")["batch_id"])


if __name__ == "__main__":
    unittest.main()
