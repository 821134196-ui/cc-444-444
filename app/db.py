"""SQLite 连接与建表脚本。

所有写操作在进程内加一把可重入锁串行化，配合 IMMEDIATE 事务保证
两处闸口并发扫描同一候船号时只有一处能放行。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS boat_types (
    code        TEXT PRIMARY KEY,          -- PEDAL / ELECTRIC / PADDLE
    name        TEXT NOT NULL,
    capacity    INTEGER NOT NULL,
    color       TEXT NOT NULL,
    hourly_fee  INTEGER NOT NULL           -- 每小时费用（分）
);

CREATE TABLE IF NOT EXISTS boats (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    type_code    TEXT NOT NULL REFERENCES boat_types(code),
    status       TEXT NOT NULL DEFAULT 'DOCKED',
      -- DOCKED 停泊 / LOCKED 已锁定待登 / OUT 出航中 / INSPECTING 回港检查中
    active_trip  INTEGER REFERENCES trips(id)
);

-- 救生衣库存：按船型 x 尺码维护（儿童尺码不足即查这里）
CREATE TABLE IF NOT EXISTS vest_inventory (
    id         INTEGER PRIMARY KEY,
    type_code  TEXT NOT NULL REFERENCES boat_types(code),
    size       TEXT NOT NULL,              -- S(儿童) / M / L / XL
    total      INTEGER NOT NULL,
    UNIQUE(type_code, size)
);

CREATE TABLE IF NOT EXISTS visitors (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    phone       TEXT NOT NULL,
    party_size  INTEGER NOT NULL,
    has_child   INTEGER NOT NULL DEFAULT 0,
    children_count INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id            INTEGER PRIMARY KEY,
    ticket_no     TEXT NOT NULL UNIQUE,    -- 候船号，如 A-007
    visitor_id    INTEGER NOT NULL REFERENCES visitors(id),
    party_size    INTEGER NOT NULL,
    boat_pref     TEXT REFERENCES boat_types(code),
    has_child     INTEGER NOT NULL DEFAULT 0,
    children_count INTEGER NOT NULL DEFAULT 0,
    -- 队列状态：
    -- WAITING 候船中 / CALLED 已叫号 / MISSED 过号 / READY 已分配批次候登
    -- BOARDING 登船核验中 / ON_WATER 已出航
    -- RETURNED 已回港检查 / VEST_RETURNED 救生衣归还 / SETTLED 押金结算完成 / CANCELLED 已取消
    status        TEXT NOT NULL DEFAULT 'WAITING',
    enqueued_at   TEXT NOT NULL,
    called_at     TEXT,
    missed_at     TEXT,
    ready_at      TEXT,
    deposit       INTEGER NOT NULL,        -- 押金（分）
    batch_id      INTEGER REFERENCES batches(id),
    queue_seq     INTEGER NOT NULL,        -- 入队序号，决定叫号顺序
    priority_seq  INTEGER                  -- 过号重排后的新序号（NULL=未重排）
);

CREATE TABLE IF NOT EXISTS payments (
    id          INTEGER PRIMARY KEY,
    ticket_id   INTEGER NOT NULL REFERENCES tickets(id),
    kind        TEXT NOT NULL,             -- DEPOSIT 押金 / FEE 船费 / DEDUCT 扣款 / REFUND 退还
    amount      INTEGER NOT NULL,          -- 扣款/退还为正，由 kind 区分方向
    status      TEXT NOT NULL,             -- SUCCESS / FAILED
    provider    TEXT NOT NULL DEFAULT 'MOCK',
    reference   TEXT NOT NULL,             -- 支付渠道流水号
    idempotency_key TEXT UNIQUE,           -- 幂等键，防重复扣款
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id            INTEGER PRIMARY KEY,
    boat_id       INTEGER NOT NULL REFERENCES boats(id),
    type_code     TEXT NOT NULL REFERENCES boat_types(code),
    -- CREATED 已组批 / BOARDING 登船中 / DEPARTED 已出航
    -- SUSPENDED 天气停航中止 / COMPLETED 回港完成
    status        TEXT NOT NULL DEFAULT 'CREATED',
    headcount     INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    created_by    TEXT NOT NULL DEFAULT 'staff',
    departed_at   TEXT,
    expected_back TEXT,                    -- 预计回港时间
    due_back      TEXT,                    -- 超时判定时间
    returned_at   TEXT,
    suspended_at  TEXT,
    note          TEXT
);

-- 登船人员逐人核验救生衣尺码
CREATE TABLE IF NOT EXISTS boardings (
    id           INTEGER PRIMARY KEY,
    batch_id     INTEGER NOT NULL REFERENCES batches(id),
    ticket_id    INTEGER NOT NULL REFERENCES tickets(id),
    person_idx   INTEGER NOT NULL,         -- 该订单内第几位游客（0 起）
    vest_size    TEXT,                     -- S/M/L/XL
    checked      INTEGER NOT NULL DEFAULT 0,
    checked_at   TEXT,
    gate         TEXT,                     -- 放行闸口 G1/G2
    scan_count   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(batch_id, ticket_id, person_idx)
);

CREATE TABLE IF NOT EXISTS trips (
    id            INTEGER PRIMARY KEY,
    batch_id      INTEGER NOT NULL REFERENCES batches(id),
    boat_id       INTEGER NOT NULL REFERENCES boats(id),
    departed_at   TEXT NOT NULL,
    expected_back TEXT NOT NULL,
    due_back      TEXT NOT NULL,
    returned_at   TEXT,
    status        TEXT NOT NULL DEFAULT 'OUT',  -- OUT / OVERDUE / RETURNED
    reminded_at   TEXT,
    hull_ok       INTEGER,                 -- 回港检查：船体是否完好
    vest_ok       INTEGER,                 -- 救生衣是否全部归还
    damage_fee_each INTEGER DEFAULT 0,     -- 船体刮碰：每单扣款（分）
    vest_fee_total  INTEGER DEFAULT 0,     -- 救生衣缺失：该航次总扣款（分）
    inspect_note  TEXT
);

-- 船体照片（本地目录保存，库里只存路径）
CREATE TABLE IF NOT EXISTS hull_photos (
    id         INTEGER PRIMARY KEY,
    trip_id    INTEGER NOT NULL REFERENCES trips(id),
    path       TEXT NOT NULL,
    label      TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 押金结算（与回港检查关联，结算单号唯一，天然幂等）
CREATE TABLE IF NOT EXISTS settlements (
    id          INTEGER PRIMARY KEY,
    ticket_id   INTEGER NOT NULL UNIQUE REFERENCES tickets(id),
    trip_id     INTEGER NOT NULL REFERENCES trips(id),
    deducted    INTEGER NOT NULL DEFAULT 0,
    refunded    INTEGER NOT NULL DEFAULT 0,
    reason      TEXT,
    settled_at  TEXT NOT NULL
);

-- 天气停航广播
CREATE TABLE IF NOT EXISTS broadcasts (
    id         INTEGER PRIMARY KEY,
    level      TEXT NOT NULL,             -- NORMAL / WARNING / SUSPEND
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 业务事件流水（用于审计与测试断言）
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


_write_lock = threading.RLock()


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    with _write_lock:
        conn.executescript(SCHEMA)
        conn.commit()


def write_lock() -> threading.RLock:
    """返回进程内写锁，服务层所有状态变更持锁执行。"""
    return _write_lock
