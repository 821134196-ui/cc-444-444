"""业务测试:并发放行、过号恢复、容量约束、停航中断、押金幂等等关键场景。

运行:python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import services
from app.db import reset_for_test


class LakeTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test.db")
        reset_for_test(self.db_path)
        self.conn = services.get_conn()
        # 保留目录/船型/船只/库存等基础数据,清空运营数据,保证测试从确定状态开始
        self.conn.executescript(
            "DELETE FROM life_jacket_holds;"
            "DELETE FROM life_jacket_assignments;"
            "DELETE FROM gate_scans;"
            "DELETE FROM batch_tickets;"
            "DELETE FROM tickets;"
            "DELETE FROM batches;"
            "DELETE FROM overdue_alerts;"
            "UPDATE boats SET status='available';")
        # 照片写到临时目录,不污染本地样例目录
        services.PHOTO_DIR = os.path.join(self.tmpdir.name, "photos")

    def tearDown(self):
        self.conn.close()
        services._conn = None
        self.tmpdir.cleanup()

    # ---------- 流程辅助 ----------

    def sell(self, name, type_id, adults, children=0):
        return services.sell_ticket(name, type_id, adults, children)

    def assign_all_jackets(self, bid, ticket_row, child_size="CS"):
        for i in range(1, ticket_row["party_size"] + 1):
            size = child_size if i <= ticket_row["children"] else "M"
            services.assign_jacket(bid, ticket_row["id"], i,
                                   f"{ticket_row['contact_name']}-{i}", size)

    def sail(self, type_id=2, adults=2, children=1):
        """售票 -> 组批 -> 锁船 -> 救生衣 -> 放行 -> 出航。"""
        r = self.sell("出航团", type_id, adults, children)
        bid = services.create_batch(type_id)["batch_id"]
        t = [x for x in services.waiting_tickets(type_id)
             if x["queue_no"] == r["queue_no"]][0]
        services.add_ticket_to_batch(bid, t["id"])
        services.lock_boat(bid)
        self.assign_all_jackets(bid, t)
        services.scan_gate(t["id"], "gate-1")
        services.depart(bid)
        return bid, [self._ticket(t["id"])]

    def _ticket(self, tid):
        return self.conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()

    def _batch(self, bid):
        return self.conn.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()


class TestQueueAndMissed(LakeTestBase):
    def test_ticket_enters_queue_and_order(self):
        r1 = self.sell("游客甲", 1, 2)
        r2 = self.sell("游客乙", 1, 3)
        q = services.waiting_tickets(1)
        self.assertEqual([t["queue_no"] for t in q[:2]],
                         [r1["queue_no"], r2["queue_no"]])

    def test_missed_then_restore_to_tail(self):
        """过号重排:号码保留,但回到队尾。"""
        r1 = self.sell("甲", 1, 2)
        r2 = self.sell("乙", 1, 2)
        services.call_next(1)  # 甲被叫号
        # 超过叫号窗口 -> 过号
        services.expire_called(now=time.time() + 9999)
        t1 = self.conn.execute("SELECT * FROM tickets WHERE queue_no=?",
                               (r1["queue_no"],)).fetchone()
        self.assertEqual(t1["status"], "missed")
        # 此时乙仍在候船;甲重排后回到队尾,必须排在乙后面
        out = services.requeue(t1["id"])
        self.assertEqual(out["requeue_count"], 1)
        nxt = services.call_next(1)
        self.assertEqual(nxt["queue_no"], r2["queue_no"])
        services.expire_called(now=time.time() + 9999)
        # 乙过后号未重排,恢复后的甲成为唯一候船号,仍可正常叫号
        nxt2 = services.call_next(1)
        self.assertEqual(nxt2["queue_no"], r1["queue_no"])

    def test_requeue_only_for_missed(self):
        r = self.sell("甲", 1, 2)
        with self.assertRaises(services.ServiceError):
            services.requeue(self._ticket(
                self.conn.execute("SELECT id FROM tickets WHERE queue_no=?",
                                  (r["queue_no"],)).fetchone()["id"])["id"])


class TestCapacity(LakeTestBase):
    def test_sell_over_single_boat_capacity_rejected(self):
        with self.assertRaises(services.ServiceError) as cm:
            self.sell("大团", 1, 5)  # 脚踏船上限 4
        self.assertEqual(cm.exception.code, "over_capacity")

    def test_batch_overload_rejected(self):
        """两个 3 人团不能拼进上限 4 的脚踏船批次。"""
        self.sell("甲", 1, 3)
        self.sell("乙", 1, 3)
        b = services.create_batch(1)
        bid = b["batch_id"]
        ids = [t["id"] for t in services.waiting_tickets(1)]
        services.add_ticket_to_batch(bid, ids[0])
        with self.assertRaises(services.ServiceError) as cm:
            services.add_ticket_to_batch(bid, ids[1])
        self.assertEqual(cm.exception.code, "over_capacity")
        self.assertEqual(self._batch(bid)["party_count"], 3)

    def test_cannot_mix_boat_types(self):
        self.sell("甲", 1, 2)
        b = services.create_batch(2)  # 电动船批次
        tid = [t for t in services.waiting_tickets(1)][0]["id"]
        with self.assertRaises(services.ServiceError):
            services.add_ticket_to_batch(b["batch_id"], tid)

    def test_switch_boat_same_type_keeps_limit(self):
        """临时换船:同船型可换,人数上限不变;换异船型被拒。"""
        bid, tickets = self.sail(2)  # 锁到 EB-01,EB-02 可作为目标
        other = self.conn.execute(
            "SELECT id FROM boats WHERE code='EB-02'").fetchone()["id"]
        pedalo = self.conn.execute(
            "SELECT id FROM boats WHERE code='PD-01'").fetchone()["id"]
        # 换到其他船型 -> 拒绝(防止突破该船型人数约束)
        with self.assertRaises(services.ServiceError):
            services.switch_boat(bid, pedalo)
        # 同船型换船成功,旧船释放
        r = services.switch_boat(bid, other)
        self.assertEqual(r["boat_code"], "EB-02")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM boats WHERE code='EB-01'").fetchone()["status"],
            "available")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM boats WHERE code='EB-02'").fetchone()["status"],
            "sailing")

    def test_child_jacket_shortage(self):
        """儿童尺码库存不足时明确报错(CS 库存 4)。"""
        r = self.sell("亲子大团", 2, 1, 5)  # 5 名儿童,CS 仅 4 件
        b = services.create_batch(2)
        bid = b["batch_id"]
        t = [x for x in services.waiting_tickets(2)
             if x["queue_no"] == r["queue_no"]][0]
        services.add_ticket_to_batch(bid, t["id"])
        services.lock_boat(bid)
        for i in range(1, 5):
            services.assign_jacket(bid, t["id"], i, f"孩{i}", "CS")
        with self.assertRaises(services.ServiceError) as cm:
            services.assign_jacket(bid, t["id"], 5, "孩5", "CS")
        self.assertEqual(cm.exception.code, "jacket_shortage")
        # 成人陪同改用成人码,有库存,接口正常
        services.assign_jacket(bid, t["id"], 5, "成人陪同", "M")


class TestGateConcurrent(LakeTestBase):
    def _ready_ticket(self):
        """售票组批锁船并完成救生衣核验,得到可放行门票。"""
        r = self.sell("钱七", 2, 2, 1)
        b = services.create_batch(2)
        bid = b["batch_id"]
        t = [x for x in services.waiting_tickets(2)
             if x["queue_no"] == r["queue_no"]][0]
        services.add_ticket_to_batch(bid, t["id"])
        services.lock_boat(bid)
        self.assign_all_jackets(bid, t)
        return bid, t

    def test_scan_before_jackets_ready_rejected_and_recoverable(self):
        r = self.sell("孙八", 2, 2)
        b = services.create_batch(2)
        bid = b["batch_id"]
        t = [x for x in services.waiting_tickets(2)
             if x["queue_no"] == r["queue_no"]][0]
        services.add_ticket_to_batch(bid, t["id"])
        services.lock_boat(bid)
        with self.assertRaises(services.ServiceError):
            services.scan_gate(t["id"], "gate-1")
        # 失败不留扫描记录,补齐核验后可正常放行
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM gate_scans WHERE ticket_id=?",
            (t["id"],)).fetchone()["c"], 0)
        self.assign_all_jackets(bid, t)
        ok = services.scan_gate(t["id"], "gate-1")
        self.assertTrue(ok["allowed"])

    def test_two_gates_only_release_once_sequential(self):
        _bid, t = self._ready_ticket()
        first = services.scan_gate(t["id"], "gate-1")
        second = services.scan_gate(t["id"], "gate-2")
        self.assertTrue(first["allowed"])
        self.assertFalse(second["allowed"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM gate_scans WHERE ticket_id=?",
            (t["id"],)).fetchone()["c"], 1)

    def test_two_gates_concurrent_only_one_wins(self):
        """两处闸口同时扫描同一候船号:20 个并发请求只能放行一次。"""
        _bid, t = self._ready_ticket()
        results = []
        barrier = threading.Barrier(20)

        def worker(gate):
            barrier.wait()
            try:
                results.append(services.scan_gate(t["id"], gate))
            except services.ServiceError:
                results.append(None)

        threads = [threading.Thread(target=worker,
                                    args=(f"gate-{i % 2 + 1}",))
                   for i in range(20)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        allowed = [r for r in results if r and r.get("allowed")]
        self.assertEqual(len(allowed), 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM gate_scans WHERE ticket_id=?",
            (t["id"],)).fetchone()["c"], 1)


class TestSuspension(LakeTestBase):
    def test_storm_blocks_new_batch_and_departure(self):
        services.set_weather("storm", source="测试")
        self.sell("甲", 1, 2)
        with self.assertRaises(services.ServiceError) as cm:
            services.create_batch(1)
        self.assertEqual(cm.exception.code, "suspended")
        # 复航后可创建
        services.set_weather("fine", source="测试")
        b = services.create_batch(1)
        self.assertTrue(b["batch_id"])

    def test_storm_interrupts_boarding_batch(self):
        """停航中断:登船中批次不能出航,解散后号码回到候船队尾。"""
        r = self.sell("甲", 2, 2)
        b = services.create_batch(2)
        bid = b["batch_id"]
        t = [x for x in services.waiting_tickets(2)
             if x["queue_no"] == r["queue_no"]][0]
        services.add_ticket_to_batch(bid, t["id"])
        services.lock_boat(bid)
        self.assign_all_jackets(bid, t)
        services.scan_gate(t["id"], "gate-1")
        services.set_weather("storm", source="测试")
        with self.assertRaises(services.ServiceError) as cm:
            services.depart(bid)
        self.assertEqual(cm.exception.code, "suspended")
        services.cancel_batch(bid)
        self.assertEqual(self._batch(bid)["status"], "cancelled")
        self.assertEqual(self._ticket(t["id"])["status"], "waiting")
        # 锁定的船已释放
        boat = self.conn.execute(
            "SELECT bo.status FROM batches b JOIN boats bo ON bo.id=b.boat_id"
            " WHERE b.id=?", (bid,)).fetchone()
        self.assertEqual(boat["status"], "available")
        # 停航广播已发出
        bc = self.conn.execute(
            "SELECT COUNT(*) c FROM broadcasts WHERE level='stop'").fetchone()["c"]
        self.assertGreaterEqual(bc, 1)


class TestOverdue(LakeTestBase):
    def test_overdue_alert_fires_once(self):
        bid, _ = self.sail(2)
        future = time.time() + 61 * 60
        fired = services.check_overdue(now=future)
        self.assertIn(bid, fired)
        # 再次扫描不重复告警
        fired2 = services.check_overdue(now=future + 600)
        self.assertEqual(fired2, [])
        view = services.lake_view(now=time.time() + 125 * 60)
        trip = [x for x in view["trips"] if x["id"] == bid][0]
        self.assertTrue(trip["overdue"])
        self.assertGreaterEqual(trip["late_min"], 60)


class TestDepositIdempotency(LakeTestBase):
    def _returned_batch_with_fees(self):
        """两票拼船(3 人 + 2 人),出航超时 70 分钟,回港刮碰且缺 1 件救生衣。"""
        r1 = self.sell("赵家", 2, 2, 1)   # 3 人
        r2 = self.sell("钱家", 2, 2)      # 2 人
        b = services.create_batch(2)
        bid = b["batch_id"]
        t1 = [x for x in services.waiting_tickets(2)
              if x["queue_no"] == r1["queue_no"]][0]
        t2 = [x for x in services.waiting_tickets(2)
              if x["queue_no"] == r2["queue_no"]][0]
        services.add_ticket_to_batch(bid, t1["id"])
        services.add_ticket_to_batch(bid, t2["id"])
        services.lock_boat(bid)
        self.assign_all_jackets(bid, t1)
        self.assign_all_jackets(bid, t2)
        services.scan_gate(t1["id"], "gate-1")
        services.scan_gate(t2["id"], "gate-2")
        services.depart(bid)
        # 模拟超时:把预计回港时间提前 70 分钟
        self.conn.execute(
            "UPDATE batches SET expected_return_at=sailed_at+?",
            (-70 * 60,))
        assigns = self.conn.execute(
            "SELECT id FROM life_jacket_assignments WHERE ticket_id=?",
            (t1["id"],)).fetchall()
        missing = [assigns[0]["id"]]
        photo = services.make_demo_photo(bid)
        services.create_inspection(bid, "scratch", "安全员", "右舷轻微刮碰",
                                   [photo], missing)
        # 显式对齐回港时间:恰好在预计时间之后 70 分钟,避免受测试执行耗时影响
        self.conn.execute(
            "UPDATE batches SET actual_return_at=expected_return_at + 70*60"
            " WHERE id=?", (bid,))
        return bid, t1, t2

    def test_settle_requires_inspection(self):
        """押金扣除必须关联回港检查:无检查记录一律拒绝。"""
        bid, tickets = self.sail(2)
        tid = tickets[0]["id"]
        # 直接把票置为已回港但没有检查单
        self.conn.execute("UPDATE tickets SET status='returned' WHERE id=?",
                          (tid,))
        with self.assertRaises(services.ServiceError) as cm:
            services.settle_ticket_deposit(tid)
        self.assertEqual(cm.exception.code, "no_inspection")

    def test_settle_fees_and_idempotent_repeat(self):
        bid, t1, t2 = self._returned_batch_with_fees()
        r = services.settle_ticket_deposit(t1["id"])
        # 刮碰 120 按人数分摊:赵家 120*3/5=72;缺衣 80;超时 70*1*3=210
        self.assertAlmostEqual(r["hull_share"], 72.0, places=2)
        self.assertEqual(r["jacket_fee"], 80)
        self.assertEqual(r["overdue_fee"], 210)
        self.assertEqual(r["deducted"], 362.0)
        self.assertEqual(r["refunded"], 450.0 - 362.0)
        deduct_rows = self.conn.execute(
            "SELECT COUNT(*) c FROM payments WHERE ticket_id=? AND kind='deduct'",
            (t1["id"],)).fetchone()["c"]
        self.assertEqual(deduct_rows, 1)
        # 重复提交:幂等,不再扣款
        again = services.settle_ticket_deposit(t1["id"])
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["deducted"], 362.0)
        deduct_rows2 = self.conn.execute(
            "SELECT COUNT(*) c FROM payments WHERE ticket_id=? AND kind='deduct'",
            (t1["id"],)).fetchone()["c"]
        self.assertEqual(deduct_rows2, 1)
        row = self._ticket(t1["id"])
        self.assertEqual(row["status"], "settled")
        self.assertIsNotNone(row["settle_inspection_id"])
        # 另一票结算后批次关闭,船只恢复可用
        services.settle_ticket_deposit(t2["id"])
        self.assertEqual(self._batch(bid)["status"], "closed")

    def test_concurrent_settle_charges_once(self):
        """重复提交(并发)不重复扣款。"""
        bid, t1, _ = self._returned_batch_with_fees()
        outs = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            outs.append(services.settle_ticket_deposit(t1["id"]))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        n_deduct = self.conn.execute(
            "SELECT COUNT(*) c FROM payments WHERE ticket_id=? AND kind='deduct'",
            (t1["id"],)).fetchone()["c"]
        n_refund = self.conn.execute(
            "SELECT COUNT(*) c FROM payments WHERE ticket_id=? AND kind='refund'",
            (t1["id"],)).fetchone()["c"]
        self.assertEqual(n_deduct, 1)
        self.assertEqual(n_refund, 1)
        self.assertEqual(self._ticket(t1["id"])["deposit_deducted"], 362.0)

    def test_serious_damage_sends_boat_to_repair(self):
        bid, tickets = self.sail(2)
        boat_id = self._batch(bid)["boat_id"]
        services.create_inspection(
            bid, "serious", "安全员", "船体开裂",
            [services.make_demo_photo(bid)])
        self.assertEqual(self.conn.execute(
            "SELECT status FROM boats WHERE id=?", (boat_id,)).fetchone()["status"],
            "repair")


class TestEndToEnd(LakeTestBase):
    def test_happy_flow_closes_order(self):
        r = self.sell("全程团", 1, 3, 1)
        b = services.create_batch(1)
        bid = b["batch_id"]
        t = [x for x in services.waiting_tickets(1)
             if x["queue_no"] == r["queue_no"]][0]
        services.add_ticket_to_batch(bid, t["id"])
        with self.assertRaises(services.ServiceError):
            services.depart(bid)  # 未锁船不能出航
        services.lock_boat(bid)
        self.assign_all_jackets(bid, t)
        services.scan_gate(t["id"], "gate-1")
        services.depart(bid)
        services.create_inspection(
            bid, "ok", "安全员", "一切正常",
            [services.make_demo_photo(bid)])
        out = services.settle_ticket_deposit(t["id"])
        self.assertEqual(out["deducted"], 0)
        self.assertEqual(out["refunded"], 400.0)
        self.assertEqual(self._batch(bid)["status"], "closed")
        # 救生衣归还后库存释放
        free_cs = services._jacket_available("CS")
        stock = self.conn.execute(
            "SELECT total FROM life_jacket_stock WHERE size_code='CS'").fetchone()["total"]
        self.assertEqual(free_cs, stock)


if __name__ == "__main__":
    unittest.main(verbosity=2)
