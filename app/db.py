"""SQLite 连接、建库与演示数据。

所有线程共享同一连接(进程内应用),写操作串行化 + busy_timeout,
关键唯一性约束(ticket 唯一、支付幂等键唯一、闸口 ticket 唯一)由数据库兜底。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "data", "boat.db")
PHOTO_DIR = os.path.join(BASE_DIR, "data", "photos")
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def get_conn(db_path: str | None = None) -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            path = db_path or DEFAULT_DB
            os.makedirs(os.path.dirname(path), exist_ok=True)
            os.makedirs(PHOTO_DIR, exist_ok=True)
            conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            _conn = conn
        return _conn


def reset_for_test(db_path: str) -> sqlite3:
    """测试用:重置全局连接并重新建库。"""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
        for suffix in ("", "-wal", "-shm"):
            p = db_path + suffix
            if os.path.exists(p):
                os.remove(p)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        _conn = conn
        with open(SCHEMA_PATH, encoding="utf-8") as f:
            conn.executescript(f.read())
        seed_demo_data(conn)
        return conn


def init_db() -> sqlite3.Connection:
    conn = get_conn()
    with _lock:
        with open(SCHEMA_PATH, encoding="utf-8") as f:
            conn.executescript(f.read())
        seeded = conn.execute("SELECT COUNT(*) c FROM boat_types").fetchone()["c"]
        if seeded == 0:
            seed_demo_data(conn)
    return conn


def seed_demo_data(conn: sqlite3.Connection) -> None:
    now = time.time()
    # 船型:脚踏船 4 人、电动船 6 人、画舫 10 人
    types = [
        ("脚踏船", 4, 60, 60.0, 100.0),
        ("电动船", 6, 60, 120.0, 150.0),
        ("画舫", 10, 90, 200.0, 200.0),
    ]
    conn.executemany(
        "INSERT INTO boat_types(name, capacity, duration_min, hourly_rate, deposit_pp)"
        " VALUES (?,?,?,?,?)",
        types,
    )
    # 船只
    boats = [
        (1, "PD-01", "青鳞号"), (1, "PD-02", "白鹭号"), (1, "PD-03", "翠羽号"),
        (2, "EB-01", "凌波号"), (2, "EB-02", "乘风号"),
        (3, "HF-01", "听雨舫"),
    ]
    conn.executemany(
        "INSERT INTO boats(boat_type_id, code, name, status) VALUES (?,?,?,'available')",
        boats,
    )
    # 救生衣库存(儿童尺码故意偏少,用于演示"儿童尺码不足")
    jackets = [
        ("CXS", "幼儿码(15kg以下)", 6),
        ("CS", "儿童码(15-35kg)", 4),
        ("S", "成人 S", 12),
        ("M", "成人 M", 16),
        ("L", "成人 L", 12),
        ("XL", "成人 XL", 6),
    ]
    conn.executemany(
        "INSERT INTO life_jacket_stock(size_code, label, total) VALUES (?,?,?)", jackets
    )
    # 演示游客
    visitors = [
        ("王芳", "138-0000-0001", "常来的熟客"),
        ("李强", "138-0000-0002", "带两个孩子"),
        ("赵敏", "138-0000-0003", None),
        ("陈杰", "138-0000-0004", "团体出行"),
        ("周婷", "138-0000-0005", None),
    ]
    conn.executemany(
        "INSERT INTO visitors(name, phone, note) VALUES (?,?,?)", visitors
    )
    # 天气初始适航
    conn.execute(
        "INSERT INTO weather_state(id, status, message, changed_at)"
        " VALUES (1,'fine','湖面风浪 1 级,适航',?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO broadcasts(level, message, created_at) VALUES ('info','系统开启,当前适航',?)",
        (now,),
    )
    _seed_demo_tickets(conn, now)


def _seed_demo_tickets(conn: sqlite3.Connection, now: float) -> None:
    """写入可直接演示的样例:候船队列、过号号码、一个在航航次。"""
    # (queue_no, visitor, 联系人, 船型, 成人, 儿童, 状态)
    demo = [
        ("A001", 1, "王芳", 1, 2, 1, "waiting"),
        ("A002", 5, "周婷", 1, 4, 0, "waiting"),
        ("B001", 2, "李强", 2, 2, 2, "waiting"),
        ("C001", 4, "陈杰", 3, 8, 2, "waiting"),
        ("B002", 3, "赵敏", 2, 2, 0, "missed"),
    ]
    for qno, vid, name, tid, adults, children, status in demo:
        bt = conn.execute("SELECT * FROM boat_types WHERE id=?", (tid,)).fetchone()
        party = adults + children
        amount = round(bt["hourly_rate"] * party * bt["duration_min"] / 60, 2)
        deposit = round(bt["deposit_pp"] * party, 2)
        conn.execute(
            "INSERT INTO tickets(queue_no, visitor_id, contact_name, boat_type_id,"
            " party_size, adults, children, status, created_at, requeued_at,"
            " ticket_amount, deposit_total)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (qno, vid, name, tid, party, adults, children,
             status, now - 600 if status == "waiting" else now - 300,
             now - 300 if status == "missed" else None, amount, deposit))

    # 一个在航航次(电动船 EB-02 / 乘风号),出航 40 分钟,20 分钟后预计回港
    bt = conn.execute("SELECT * FROM boat_types WHERE id=2").fetchone()
    boat = conn.execute("SELECT * FROM boats WHERE code='EB-02'").fetchone()
    sailed = now - 40 * 60
    expected = now + 20 * 60
    conn.execute(
        "INSERT INTO batches(code, boat_type_id, boat_id, status, party_count,"
        " created_at, locked_at, sailed_at, expected_return_at, notes)"
        " VALUES ('B-DEMO-01',2,?,'sailing',3,?,?,?,?,'样例航次')",
        (boat["id"], now - 45 * 60, now - 42 * 60, sailed, expected))
    bid = conn.execute("SELECT id FROM batches WHERE code='B-DEMO-01'").fetchone()["id"]
    # 游客票:赵敏 3 人(2 大 1 小)
    conn.execute(
        "INSERT INTO tickets(queue_no, visitor_id, contact_name, boat_type_id,"
        " party_size, adults, children, status, created_at, batch_id,"
        " ticket_amount, deposit_total)"
        " VALUES ('B003',3,'赵敏',2,3,2,1,'sailing',?,?,?,?)",
        (now - 45 * 60, bid,
         round(bt["hourly_rate"] * 3, 2), round(bt["deposit_pp"] * 3, 2)))
    ticket_id = conn.execute("SELECT id FROM tickets WHERE queue_no='B003'").fetchone()["id"]
    conn.execute("INSERT INTO batch_tickets(batch_id, ticket_id) VALUES(?,?)",
                 (bid, ticket_id))
    conn.execute("UPDATE boats SET status='sailing' WHERE id=?", (boat["id"],))
    conn.execute("INSERT INTO gate_scans(ticket_id, gate, scanned_at) VALUES(?, 'gate-1', ?)",
                 (ticket_id, now - 42 * 60))
    for idx, (pname, size) in enumerate(
            [("赵敏-1", "M"), ("赵敏-2", "S"), ("赵敏-3", "CS")], start=1):
        cur = conn.execute(
            "INSERT INTO life_jacket_assignments(batch_id, ticket_id, person_idx,"
            " person_name, size_code, assigned_at) VALUES(?,?,?,?,?,?)",
            (bid, ticket_id, idx, pname, size, now - 41 * 60))
        conn.execute(
            "INSERT INTO life_jacket_holds(size_code, ref_type, ref_id, created_at)"
            " VALUES(?,'assignment',?,?)", (size, cur.lastrowid, now - 41 * 60))
