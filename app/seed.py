"""演示样例数据：船型、船只、救生衣库存、游客与支付记录（由支付适配器模拟）。"""
from __future__ import annotations

from .service import Service

BOAT_TYPES = [
    ("PEDAL", "脚踏船", 4, "#0E7C86", 6000),
    ("ELECTRIC", "电动船", 8, "#0B5F66", 12000),
    ("PADDLE", "手划船", 2, "#17A2A8", 4000),
]

BOATS = [
    ("青鸾号", "PEDAL"), ("白鹭号", "PEDAL"),
    ("湖风号", "ELECTRIC"),
    ("小桨-1", "PADDLE"), ("小桨-2", "PADDLE"),
]

# (船型, 尺码, 数量) —— 脚踏船 S 仅 2 件，便于演示儿童尺码不足
VESTS = [
    ("PEDAL", "S", 2), ("PEDAL", "M", 6), ("PEDAL", "L", 6), ("PEDAL", "XL", 2),
    ("ELECTRIC", "S", 6), ("ELECTRIC", "M", 12), ("ELECTRIC", "L", 12),
    ("ELECTRIC", "XL", 6),
    ("PADDLE", "S", 2), ("PADDLE", "M", 3), ("PADDLE", "L", 3), ("PADDLE", "XL", 1),
]

DEMO_VISITORS = [
    # 姓名, 电话, 人数, 船型, 儿童数
    ("张一家", "138****0001", 2, "PEDAL", 1),
    ("李二同", "139****0002", 2, "PEDAL", 0),
    ("王三行", "137****0003", 4, "PEDAL", 2),
    ("赵四客", "136****0004", 6, "ELECTRIC", 1),
    ("孙五游", "135****0005", 2, "PADDLE", 0),
    ("周六帆", "134****0006", 1, "PADDLE", 0),
    ("钱七浪", "133****0007", 3, "PEDAL", 0),
]


def seed(svc: Service) -> int:
    n = svc.conn.execute("SELECT COUNT(*) c FROM boat_types").fetchone()["c"]
    if n:
        return 0
    for code, name, cap, color, fee in BOAT_TYPES:
        svc.conn.execute(
            "INSERT INTO boat_types(code,name,capacity,color,hourly_fee)"
            " VALUES(?,?,?,?,?)", (code, name, cap, color, fee))
    for name, code in BOATS:
        svc.conn.execute(
            "INSERT INTO boats(name,type_code) VALUES(?,?)", (name, code))
    for tc, size, total in VESTS:
        svc.conn.execute(
            "INSERT INTO vest_inventory(type_code,size,total) VALUES(?,?,?)",
            (tc, size, total))
    svc.conn.commit()
    count = 0
    for name, phone, size, pref, children in DEMO_VISITORS:
        svc.sell_ticket(name=name, phone=phone, party_size=size,
                        boat_pref=pref, children_count=children)
        count += 1
    svc.weather.force("NORMAL")
    return count
