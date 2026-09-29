-- 公园游船排队与救生衣核验平台 数据结构

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 船型
CREATE TABLE IF NOT EXISTS boat_types (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,              -- 脚踏船 / 电动船 / 画舫
    capacity      INTEGER NOT NULL,           -- 载客上限
    duration_min  INTEGER NOT NULL DEFAULT 60,-- 标准航时(分钟)
    hourly_rate   REAL NOT NULL,              -- 每小时票价(演示)
    deposit_pp    REAL NOT NULL               -- 每人押金
);

-- 船只
CREATE TABLE IF NOT EXISTS boats (
    id             INTEGER PRIMARY KEY,
    boat_type_id   INTEGER NOT NULL REFERENCES boat_types(id),
    code           TEXT NOT NULL UNIQUE,      -- PD-01 / EB-01 / HF-01
    name           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'available'
                   -- available 可用 / locked 已锁定 / sailing 出航中 / inspection 回港检查中 / repair 维修
);

-- 救生衣库存(按尺码)
CREATE TABLE IF NOT EXISTS life_jacket_stock (
    size_code  TEXT PRIMARY KEY,             -- CXS 幼儿 / CS 儿童 / S / M / L / XL
    label      TEXT NOT NULL,
    total      INTEGER NOT NULL
);

-- 救生衣实际占用(分配中/未归还的占用一条),用触发器在应用层维护更直观,这里直接记账
CREATE TABLE IF NOT EXISTS life_jacket_holds (
    id          INTEGER PRIMARY KEY,
    size_code   TEXT NOT NULL REFERENCES life_jacket_stock(size_code),
    ref_type    TEXT NOT NULL,               -- assignment
    ref_id      INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    UNIQUE(ref_type, ref_id)
);

-- 游客(演示资料)
CREATE TABLE IF NOT EXISTS visitors (
    id     INTEGER PRIMARY KEY,
    name   TEXT NOT NULL,
    phone  TEXT NOT NULL,
    note   TEXT
);

-- 门票 / 候船号
CREATE TABLE IF NOT EXISTS tickets (
    id               INTEGER PRIMARY KEY,
    queue_no         TEXT NOT NULL UNIQUE,    -- 候船号,如 A032
    visitor_id       INTEGER REFERENCES visitors(id),
    contact_name     TEXT NOT NULL,           -- 联系人(演示)
    boat_type_id     INTEGER NOT NULL REFERENCES boat_types(id),
    party_size       INTEGER NOT NULL,        -- 总人数
    adults           INTEGER NOT NULL,
    children         INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'waiting',
                     -- waiting 候船 / called 已叫号 / missed 过号 / batched 已组批 /
                     -- released 已放行 / sailing 出航中 / returned 已回港 /
                     -- inspected 已检查 / settled 已结算 / cancelled 已取消
    requeue_count    INTEGER NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL,
    requeued_at      REAL,                    -- 过号重排时间(决定队尾位置)
    called_at        REAL,
    call_deadline    REAL,                    -- 叫号有效截止
    batch_id         INTEGER REFERENCES batches(id),
    ticket_amount    REAL NOT NULL DEFAULT 0,
    deposit_total    REAL NOT NULL DEFAULT 0,
    deposit_deducted REAL NOT NULL DEFAULT 0,
    deposit_refunded REAL NOT NULL DEFAULT 0,
    settle_inspection_id INTEGER
);

-- 登船批次(= 航次)
CREATE TABLE IF NOT EXISTS batches (
    id                INTEGER PRIMARY KEY,
    code              TEXT NOT NULL UNIQUE,   -- B20260929-01
    boat_type_id      INTEGER NOT NULL REFERENCES boat_types(id),
    boat_id           INTEGER REFERENCES boats(id),
    status            TEXT NOT NULL DEFAULT 'forming',
                      -- forming 组批中 / boarding 登船核验 / sailing 出航中 /
                      -- returned 已回港 / inspected 已检查 / closed 已关闭 / cancelled 已取消
    party_count       INTEGER NOT NULL DEFAULT 0,
    created_at        REAL NOT NULL,
    locked_at         REAL,
    sailed_at         REAL,
    expected_return_at REAL,
    actual_return_at  REAL,
    closed_at         REAL,
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS batch_tickets (
    batch_id  INTEGER NOT NULL REFERENCES batches(id),
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    PRIMARY KEY (batch_id, ticket_id)
);

-- 救生衣逐人分配
CREATE TABLE IF NOT EXISTS life_jacket_assignments (
    id          INTEGER PRIMARY KEY,
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    ticket_id   INTEGER NOT NULL REFERENCES tickets(id),
    person_idx  INTEGER NOT NULL,            -- 同行第几位
    person_name TEXT NOT NULL,
    size_code   TEXT NOT NULL REFERENCES life_jacket_stock(size_code),
    status      TEXT NOT NULL DEFAULT 'assigned', -- assigned 已发放 / returned 已归还 / missing 缺失
    assigned_at REAL NOT NULL,
    returned_at REAL
);

-- 闸口扫描记录:同一候船号全局唯一 -> 两处闸口只能放行一次
CREATE TABLE IF NOT EXISTS gate_scans (
    id          INTEGER PRIMARY KEY,
    ticket_id   INTEGER NOT NULL UNIQUE REFERENCES tickets(id),
    gate        TEXT NOT NULL,               -- gate-1 / gate-2
    scanned_at  REAL NOT NULL
);

-- 回港检查
CREATE TABLE IF NOT EXISTS return_inspections (
    id           INTEGER PRIMARY KEY,
    batch_id     INTEGER NOT NULL UNIQUE REFERENCES batches(id),
    hull_result  TEXT NOT NULL,              -- ok 正常 / scratch 刮碰 / serious 严重损伤
    jackets_ok   INTEGER NOT NULL DEFAULT 1, -- 救生衣是否全部归还
    note         TEXT,
    inspector    TEXT NOT NULL,
    inspected_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS inspection_photos (
    id            INTEGER PRIMARY KEY,
    inspection_id INTEGER NOT NULL REFERENCES return_inspections(id),
    path          TEXT NOT NULL,
    uploaded_at   REAL NOT NULL
);

-- 支付流水(演示)。幂等键唯一,重复提交绝不重复扣款
CREATE TABLE IF NOT EXISTS payments (
    id              INTEGER PRIMARY KEY,
    ticket_id       INTEGER NOT NULL REFERENCES tickets(id),
    kind            TEXT NOT NULL,           -- ticket 票款 / hold 押金预授权 / deduct 押金扣除 / refund 押金退还
    amount          REAL NOT NULL,           -- 正数
    idem_key        TEXT NOT NULL UNIQUE,
    provider_ref    TEXT NOT NULL,           -- 模拟支付渠道流水
    status          TEXT NOT NULL DEFAULT 'ok',
    related_inspection_id INTEGER REFERENCES return_inspections(id),
    created_at      REAL NOT NULL
);

-- 天气(单行 current)
CREATE TABLE IF NOT EXISTS weather_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    status     TEXT NOT NULL,                -- fine 适航 / windy 大风预警 / storm 停航
    message    TEXT NOT NULL,
    changed_at REAL NOT NULL
);

-- 广播(停航/复航/提醒)
CREATE TABLE IF NOT EXISTS broadcasts (
    id         INTEGER PRIMARY KEY,
    level      TEXT NOT NULL,                -- info / warning / stop
    message    TEXT NOT NULL,
    created_at REAL NOT NULL
);

-- 超时未归告警(每个批次仅一条)
CREATE TABLE IF NOT EXISTS overdue_alerts (
    id         INTEGER PRIMARY KEY,
    batch_id   INTEGER NOT NULL UNIQUE REFERENCES batches(id),
    fired_at   REAL NOT NULL,
    message    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_batches_status ON batches(status);
