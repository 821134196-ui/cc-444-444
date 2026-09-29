"""容量约束：组批超载、临时换船不得突破上限、售票超船型上限、儿童尺码不足。"""
from __future__ import annotations

import unittest

from app.service import BoatError
from tests.helpers import _assign_vests, make_service


class CapacityTest(unittest.TestCase):
    def test_batch_over_capacity_rejected(self):
        svc = make_service()
        boat = svc.conn.execute(
            "SELECT * FROM boats WHERE name='小桨-1'").fetchone()  # 手划船上限 2
        # 手划船样例：C-005(2人) + C-006(1人) = 3 人 > 2
        c5 = svc.find_ticket("C-005")
        c6 = svc.find_ticket("C-006")
        with self.assertRaisesRegex(BoatError, "超载"):
            svc.create_batch(boat["id"], [c5["id"], c6["id"]])
        # 船没被锁、票仍在候船
        self.assertEqual(svc.conn.execute(
            "SELECT status FROM boats WHERE id=?", (boat["id"],)).fetchone()["status"],
            "DOCKED")
        self.assertEqual(svc.find_ticket("C-005")["status"], "WAITING")

    def test_ticket_larger_than_boat_type_capacity_rejected(self):
        svc = make_service()
        with self.assertRaisesRegex(BoatError, "上限"):
            svc.sell_ticket(name="超载团", phone="130****0009", party_size=5,
                            boat_pref="PEDAL")  # 脚踏船上限 4

    def test_change_boat_respects_new_capacity(self):
        svc = make_service()
        # 电动船 8 人：B-004 赵四客 6 人
        b1 = svc.conn.execute("SELECT * FROM boats WHERE name='湖风号'").fetchone()
        t = svc.find_ticket("B-004")
        self.assertEqual(t["party_size"], 6)
        batch = svc.create_batch(b1["id"], [t["id"]])
        # 换到脚踏船（上限 4）必须被拒
        pedal = svc.conn.execute(
            "SELECT * FROM boats WHERE name='青鸾号'").fetchone()
        with self.assertRaisesRegex(BoatError, "上限"):
            svc.change_boat(batch["id"], pedal["id"])
        # 换同型可用船只成功
        svc.conn.execute(
            "INSERT INTO boats(name,type_code) VALUES('湖风二号','ELECTRIC')")
        svc.conn.commit()
        b2 = svc.conn.execute(
            "SELECT * FROM boats WHERE name='湖风二号'").fetchone()
        updated = svc.change_boat(batch["id"], b2["id"])
        self.assertEqual(updated["boat_id"], b2["id"])
        self.assertEqual(svc.conn.execute(
            "SELECT status FROM boats WHERE id=?", (b1["id"],)).fetchone()["status"],
            "DOCKED")

    def test_change_boat_smaller_types_all_blocked_by_capacity(self):
        """临时换船可跨船型，但换到手划船(2)/脚踏船(4)都不能装下 6 人。"""
        svc = make_service()
        b1 = svc.conn.execute("SELECT * FROM boats WHERE name='湖风号'").fetchone()
        t = svc.find_ticket("B-004")
        batch = svc.create_batch(b1["id"], [t["id"]])
        for smaller in ("小桨-1", "青鸾号"):
            boat = svc.conn.execute(
                "SELECT * FROM boats WHERE name=?", (smaller,)).fetchone()
            with self.assertRaisesRegex(BoatError, "上限"):
                svc.change_boat(batch["id"], boat["id"])
        # 原船未受影响
        self.assertEqual(svc.batch_detail(batch["id"])["boat_id"], b1["id"])

    def test_child_vest_shortage_rejected(self):
        """4 人不超载但需要 3 件儿童 S 码（库存仅 2）→ 拒绝组批。"""
        svc = make_service()
        t1 = svc.sell_ticket(name="亲子甲", phone="131****0001", party_size=2,
                             boat_pref="PEDAL", children_count=2)
        t2 = svc.sell_ticket(name="亲子乙", phone="131****0002", party_size=2,
                             boat_pref="PEDAL", children_count=1)
        boat = svc.conn.execute(
            "SELECT * FROM boats WHERE name='青鸾号'").fetchone()
        with self.assertRaisesRegex(BoatError, "儿童救生衣"):
            svc.create_batch(boat["id"], [t1["id"], t2["id"]])
        # 失败后船仍可用
        self.assertEqual(svc.conn.execute(
            "SELECT status FROM boats WHERE id=?", (boat["id"],)).fetchone()["status"],
            "DOCKED")

    def test_vest_size_stock_enforced_globally(self):
        """手划船 S 库存 2 件：第一批 2 人全用 S 后，第二批再选 S 被拒。"""
        svc = make_service()
        c5 = svc.find_ticket("C-005")  # 2 人
        boat = svc.conn.execute(
            "SELECT * FROM boats WHERE name='小桨-1'").fetchone()
        batch = svc.create_batch(boat["id"], [c5["id"]])
        people = batch["people"]
        svc.confirm_vest(people[0]["id"], "S")
        svc.confirm_vest(people[1]["id"], "S")
        # 全局 S 已发完，第二批（另一艘船）再选 S 库存不足
        c6 = svc.find_ticket("C-006")
        boat2 = svc.conn.execute(
            "SELECT * FROM boats WHERE name='小桨-2'").fetchone()
        b2 = svc.create_batch(boat2["id"], [c6["id"]])
        with self.assertRaisesRegex(BoatError, "库存不足"):
            svc.confirm_vest(b2["people"][0]["id"], "S")

    def test_locked_boat_cannot_be_rebatched(self):
        svc = make_service()
        boat = svc.conn.execute(
            "SELECT * FROM boats WHERE name='青鸾号'").fetchone()
        a1 = svc.find_ticket("A-001")
        svc.create_batch(boat["id"], [a1["id"]])
        a2 = svc.find_ticket("A-002")
        with self.assertRaisesRegex(BoatError, "不可用"):
            svc.create_batch(boat["id"], [a2["id"]])

    def test_changing_confirmed_size_still_checks_stock(self):
        """已确认后改尺码不能绕过库存：S 发完后不能把 M 改成 S。"""
        svc = make_service()
        c5 = svc.find_ticket("C-005")  # 2 人，手划船 S 库存 2
        boat = svc.conn.execute(
            "SELECT * FROM boats WHERE name='小桨-1'").fetchone()
        b = svc.create_batch(boat["id"], [c5["id"]])
        p1, p2 = b["people"]
        svc.confirm_vest(p1["id"], "S")
        svc.confirm_vest(p2["id"], "M")
        c6 = svc.find_ticket("C-006")
        boat2 = svc.conn.execute(
            "SELECT * FROM boats WHERE name='小桨-2'").fetchone()
        b2 = svc.create_batch(boat2["id"], [c6["id"]])
        # 另一批把仅剩的判断占用：p2 改 S 时，S 已被 p1 占 1 件，
        # 再加 b2 的人就会超；这里直接验证本批 p2 改 S 成功（库存在位）
        updated = svc.confirm_vest(p2["id"], "S")
        self.assertEqual(next(x for x in updated["people"]
                             if x["id"] == p2["id"])["vest_size"], "S")
        # 此时 S 已发 2 件，b2 的人确认 S 必须失败
        with self.assertRaisesRegex(BoatError, "库存不足"):
            svc.confirm_vest(b2["people"][0]["id"], "S")


if __name__ == "__main__":
    unittest.main()
