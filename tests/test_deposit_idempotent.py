"""押金幂等：结算关联回港检查；重复提交不重复扣款。"""
from __future__ import annotations

import unittest

from app.service import BoatError, DAMAGE_FEE_EACH
from tests.helpers import board_and_depart, make_service


def _return_trip(svc, batch_id, *, hull_ok=True, vest_missing=0):
    trip = svc.conn.execute(
        "SELECT * FROM trips WHERE batch_id=?", (batch_id,)).fetchone()
    svc.return_check(trip["id"], hull_ok=hull_ok,
                     vest_missing=vest_missing, note="测试")
    svc.confirm_vest_return(trip["id"])
    return trip


class DepositIdempotentTest(unittest.TestCase):
    def test_settle_requires_return_check(self):
        svc = make_service()
        boat = svc.conn.execute(
            "SELECT * FROM boats WHERE name='青鸾号'").fetchone()
        b = board_and_depart(svc, boat_id=boat["id"], ticket_nos=["A-001"])
        tid = svc.find_ticket("A-001")["id"]
        with self.assertRaisesRegex(BoatError, "回港"):
            svc.settle(tid)

    def test_settle_clean_trip_full_refund_idempotent(self):
        svc = make_service()
        boat = svc.conn.execute(
            "SELECT * FROM boats WHERE name='青鸾号'").fetchone()
        b = board_and_depart(svc, boat_id=boat["id"], ticket_nos=["A-001"])
        _return_trip(svc, b["id"])
        tid = svc.find_ticket("A-001")["id"]
        deposit = svc.find_ticket("A-001")["deposit"]

        r1 = svc.settle(tid)
        self.assertFalse(r1["idempotent"])
        self.assertEqual(r1["settlement"]["deducted"], 0)
        self.assertEqual(r1["settlement"]["refunded"], deposit)

        # 重复提交：回放首笔，不新增扣款
        r2 = svc.settle(tid)
        r3 = svc.settle(tid)
        self.assertTrue(r2["idempotent"])
        self.assertTrue(r3["idempotent"])
        self.assertEqual(r2["settlement"]["id"], r1["settlement"]["id"])

        n_settlements = svc.conn.execute(
            "SELECT COUNT(*) c FROM settlements WHERE ticket_id=?",
            (tid,)).fetchone()["c"]
        n_deduct_pay = svc.conn.execute(
            "SELECT COUNT(*) c FROM payments WHERE ticket_id=? AND kind='DEDUCT'",
            (tid,)).fetchone()["c"]
        self.assertEqual(n_settlements, 1)
        self.assertEqual(n_deduct_pay, 0)  # 无损坏，零扣款

    def test_damage_fee_deducted_once_under_repeated_submit(self):
        svc = make_service()
        boat = svc.conn.execute(
            "SELECT * FROM boats WHERE name='青鸾号'").fetchone()
        # 同船两单：A-001(2人) + A-002(2人) = 4 人，脚踏船上限 4
        b = board_and_depart(svc, boat_id=boat["id"],
                             ticket_nos=["A-001", "A-002"])
        _return_trip(svc, b["id"], hull_ok=False)  # 刮碰，每单扣 50

        t1 = svc.find_ticket("A-001")
        r1 = svc.settle(t1["id"])
        self.assertEqual(r1["settlement"]["deducted"], DAMAGE_FEE_EACH)
        self.assertEqual(r1["settlement"]["refunded"],
                         t1["deposit"] - DAMAGE_FEE_EACH)

        # 再点三次：金额不变、支付流水不新增
        for _ in range(3):
            r = svc.settle(t1["id"])
            self.assertTrue(r["idempotent"])
            self.assertEqual(r["settlement"]["deducted"], DAMAGE_FEE_EACH)

        rows = svc.conn.execute(
            "SELECT * FROM payments WHERE ticket_id=? AND kind='DEDUCT'",
            (t1["id"],)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["idempotency_key"], f"ded:{t1['id']}")

        # 第二单独立结算，同样只扣一次
        t2 = svc.find_ticket("A-002")
        svc.settle(t2["id"])
        svc.settle(t2["id"])
        self.assertEqual(svc.conn.execute(
            "SELECT COUNT(*) c FROM payments WHERE ticket_id=? AND kind='DEDUCT'",
            (t2["id"],)).fetchone()["c"], 1)

    def test_vest_loss_shared_across_orders(self):
        svc = make_service()
        boat = svc.conn.execute(
            "SELECT * FROM boats WHERE name='青鸾号'").fetchone()
        b = board_and_depart(svc, boat_id=boat["id"],
                             ticket_nos=["A-001", "A-002"])
        _return_trip(svc, b["id"], hull_ok=True, vest_missing=1)
        t1 = svc.find_ticket("A-001")
        r = svc.settle(t1["id"])
        # 80 元两单均摊 = 每单 40
        self.assertEqual(r["settlement"]["deducted"], 4000)

    def test_order_full_lifecycle_statuses(self):
        svc = make_service()
        boat = svc.conn.execute(
            "SELECT * FROM boats WHERE name='青鸾号'").fetchone()
        b = board_and_depart(svc, boat_id=boat["id"], ticket_nos=["A-001"])
        self.assertEqual(svc.find_ticket("A-001")["status"], "ON_WATER")
        _return_trip(svc, b["id"])
        self.assertEqual(svc.find_ticket("A-001")["status"], "VEST_RETURNED")
        tid = svc.find_ticket("A-001")["id"]
        svc.settle(tid)
        self.assertEqual(svc.find_ticket("A-001")["status"], "SETTLED")
        self.assertEqual(svc.conn.execute(
            "SELECT status FROM boats WHERE id=?", (boat["id"],)).fetchone()["status"],
            "DOCKED")


if __name__ == "__main__":
    unittest.main()
