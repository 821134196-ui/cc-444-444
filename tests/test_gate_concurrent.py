"""并发放行：两处闸口同时扫描同一候船号，只能放行一次。"""
from __future__ import annotations

import threading
import unittest

from app.service import BoatError
from tests.helpers import _assign_vests, make_service


class GateConcurrentTest(unittest.TestCase):
    def test_two_gates_concurrent_scan_only_one_pass(self):
        svc = make_service()
        boat = svc.conn.execute(
            "SELECT * FROM boats WHERE name='青鸾号'").fetchone()
        t = svc.find_ticket("A-001")  # 张一家 2 人
        batch = svc.create_batch(boat["id"], [t["id"]])
        _assign_vests(svc, batch["id"])

        results = []
        barrier = threading.Barrier(2)

        def scan(gate):
            barrier.wait()  # 两线程同时发起
            try:
                results.append(svc.gate_scan("A-001", gate))
            except BoatError as e:
                results.append({"allowed": False, "error": str(e)})

        threads = [threading.Thread(target=scan, args=(g,))
                   for g in ("G1", "G2")]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        allowed = [r for r in results if r.get("allowed")]
        replay = [r for r in results if r.get("replay")]
        self.assertEqual(len(results), 2)
        self.assertEqual(len(allowed), 1, f"应只放行一次，实际：{results}")
        self.assertEqual(len(replay), 1)
        self.assertIn("已在另一闸口", replay[0]["message"])

        # 票据终态唯一
        t2 = svc.find_ticket("A-001")
        self.assertEqual(t2["status"], "BOARDING")
        gates = {r["gate"] for r in svc.conn.execute(
            "SELECT DISTINCT gate FROM boardings WHERE ticket_id=? AND gate IS NOT NULL",
            (t2["id"],))}
        self.assertEqual(gates, {allowed[0].get("gate")})

        # 每个登船人的扫描次数都是 2（两个闸口各一次），2 人合计 4
        total_scans = svc.conn.execute(
            "SELECT COALESCE(SUM(scan_count),0) c FROM boardings WHERE ticket_id=?",
            (t2["id"],)).fetchone()["c"]
        self.assertEqual(total_scans, 4)

    def test_third_scan_still_blocked(self):
        svc = make_service()
        boat = svc.conn.execute(
            "SELECT * FROM boats WHERE name='白鹭号'").fetchone()
        t = svc.find_ticket("A-002")
        batch = svc.create_batch(boat["id"], [t["id"]])
        _assign_vests(svc, batch["id"])
        self.assertTrue(svc.gate_scan("A-002", "G1")["allowed"])
        r2 = svc.gate_scan("A-002", "G2")
        r3 = svc.gate_scan("A-002", "G1")
        self.assertFalse(r2["allowed"])
        self.assertTrue(r2["replay"])
        self.assertFalse(r3["allowed"])
        self.assertTrue(r3["replay"])

    def test_gate_blocks_before_vest_confirmed(self):
        svc = make_service()
        boat = svc.conn.execute(
            "SELECT * FROM boats WHERE name='青鸾号'").fetchone()
        t = svc.find_ticket("A-001")
        svc.create_batch(boat["id"], [t["id"]])
        with self.assertRaisesRegex(BoatError, "救生衣"):
            svc.gate_scan("A-001", "G1")


if __name__ == "__main__":
    unittest.main()
