"""测试夹具：内存 SQLite + 模拟适配器 + 样例数据。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.adapters import PaymentAdapter, WeatherAdapter  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.seed import seed  # noqa: E402
from app.service import Service  # noqa: E402

VEST_SIZES = ["S", "M", "L", "XL"]


def make_service():
    conn = connect(":memory:")
    init_db(conn)
    photo_dir = Path(tempfile.mkdtemp()) / "photos"
    svc = Service(conn, WeatherAdapter(), PaymentAdapter(), photo_dir)
    seed(svc)
    svc.weather.force("NORMAL")
    return svc


def first_ticket(svc, ticket_no: str):
    return svc.find_ticket(ticket_no)


def _assign_vests(svc, batch_id: int):
    """按儿童数发 S，其余 M，逐人确认。"""
    b = svc.batch_detail(batch_id)
    child_left = {r["id"]: r["children_count"] or 0 for r in svc.conn.execute(
        "SELECT id, children_count, has_child, party_size FROM tickets"
        " WHERE batch_id=?", (batch_id,))}
    for p in b["people"]:
        left = child_left.get(p["ticket_id"], 0)
        size = "S" if left > 0 else "M"
        child_left[p["ticket_id"]] = left - 1
        svc.confirm_vest(p["id"], size)


def board_and_depart(svc, *, boat_id: int, ticket_nos: list[str],
                     minutes: int = 60):
    """完整走通：组批 → 救生衣 → 两... 闸口 → 出航，返回 batch。"""
    ids = [svc.find_ticket(no)["id"] for no in ticket_nos]
    b = svc.create_batch(boat_id, ids)
    _assign_vests(svc, b["id"])
    for no in ticket_nos:
        assert svc.gate_scan(no, "G1")["allowed"] is True
    svc.depart(b["id"], minutes=minutes)
    return svc.batch_detail(b["id"])


def docked_boat(svc, name: str):
    return svc.conn.execute(
        "SELECT * FROM boats WHERE name=?", (name,)).fetchone()
