"""超时未归与船体照片边界。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import board_and_depart, make_service


class OverdueTest(unittest.TestCase):
    def test_trip_becomes_overdue_after_due(self):
        svc = make_service()
        boat = svc.conn.execute(
            "SELECT * FROM boats WHERE name='青鸾号'").fetchone()
        b = board_and_depart(svc, boat_id=boat["id"], ticket_nos=["A-001"],
                             minutes=60)
        trip = svc.conn.execute(
            "SELECT * FROM trips WHERE batch_id=?", (b["id"],)).fetchone()

        # 回港前 5 分钟提醒
        svc.conn.execute(
            "UPDATE trips SET due_back=datetime('now','+2 minutes') WHERE id=?",
            (trip["id"],))
        svc.conn.commit()
        r = svc.scheduler_tick()
        self.assertIn(trip["id"], r["reminded"])

        # 超过应回港时间 -> OVERDUE
        svc.conn.execute(
            "UPDATE trips SET due_back=datetime('now','-1 minutes') WHERE id=?",
            (trip["id"],))
        svc.conn.commit()
        svc.scheduler_tick()
        view = svc.lake_view()
        self.assertTrue(view["trips"][0]["overdue"])
        self.assertEqual(view["trips"][0]["status"], "OVERDUE")
        self.assertGreaterEqual(view["trips"][0]["overdue_min"], 0)

    def test_overdue_boat_can_still_return_and_settle(self):
        svc = make_service()
        boat = svc.conn.execute(
            "SELECT * FROM boats WHERE name='青鸾号'").fetchone()
        b = board_and_depart(svc, boat_id=boat["id"], ticket_nos=["A-001"])
        trip = svc.conn.execute(
            "SELECT * FROM trips WHERE batch_id=?", (b["id"],)).fetchone()
        svc.conn.execute(
            "UPDATE trips SET status='OVERDUE', due_back=datetime('now','-10 minutes')"
            " WHERE id=?", (trip["id"],))
        svc.conn.commit()

        svc.return_check(trip["id"], hull_ok=True, vest_missing=0, note="超时但无损坏")
        svc.confirm_vest_return(trip["id"])
        tid = svc.find_ticket("A-001")["id"]
        r = svc.settle(tid)
        self.assertEqual(r["settlement"]["deducted"], 0)
        self.assertEqual(svc.find_ticket("A-001")["status"], "SETTLED")

    def test_hull_photo_saved_to_local_dir(self):
        tmp = Path(tempfile.mkdtemp())
        svc = make_service()
        svc.photo_dir = tmp / "photos"
        svc.photo_dir.mkdir()
        boat = svc.conn.execute(
            "SELECT * FROM boats WHERE name='青鸾号'").fetchone()
        b = board_and_depart(svc, boat_id=boat["id"], ticket_nos=["A-001"])
        trip = svc.conn.execute(
            "SELECT * FROM trips WHERE batch_id=?", (b["id"],)).fetchone()
        r = svc.add_hull_photo(trip["id"], "scratch.jpg", b"FAKEJPEG",
                               "刮碰存证")
        saved = r["path"].split("/", 1)[1]
        self.assertEqual((tmp / "photos" / saved).read_bytes(), b"FAKEJPEG")
        detail = svc.trip_detail(trip["id"])
        self.assertEqual(len(detail["photos"]), 1)
        self.assertEqual(detail["photos"][0]["label"], "刮碰存证")


if __name__ == "__main__":
    unittest.main()
