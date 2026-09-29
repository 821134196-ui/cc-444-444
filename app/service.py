"""业务服务层。

所有状态变更都在进程内写锁 + SQLite IMMEDIATE 事务中完成，关键关口
（闸口放行、组批、换船、出航、结算）再用条件 UPDATE 做 CAS 抢占，
保证多线程/两闸口并发下只成功一次。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .adapters import PaymentAdapter, WeatherAdapter
from .db import write_lock

VEST_SIZES = ["S", "M", "L", "XL"]           # S 为儿童尺码
SIZE_LABEL = {"S": "S（儿童）", "M": "M", "L": "L", "XL": "XL"}
DEPOSIT_PER_PERSON = 20000                    # 押金 200 元/人（分）
DAMAGE_FEE_EACH = 5000                        # 刮碰每单扣 50 元（分）
VEST_FEE_EACH = 8000                          # 救生衣遗失每件 80 元（分）
TRIP_MINUTES = 60                             # 标准航次 60 分钟
REMIND_BEFORE_MIN = 5                         # 回港前 5 分钟提醒
QUEUE_LETTER = {"PEDAL": "A", "ELECTRIC": "B", "PADDLE": "C"}


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def ts(dt: datetime) -> str:
    return dt.isoformat(sep=" ")


def parse(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class BoatError(Exception):
    """业务规则冲突（超载/停航/重复放行等）。"""


class Service:
    def __init__(self, conn: sqlite3.Connection, weather: WeatherAdapter,
                 payments: PaymentAdapter, photo_dir: Path):
        self.conn = conn
        self.weather = weather
        self.payments = payments
        self.photo_dir = Path(photo_dir)
        self.photo_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 基础工具 ----------
    def _log(self, kind: str, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO events(kind,payload,created_at) VALUES(?,?,?)",
            (kind, json.dumps(payload, ensure_ascii=False), ts(now())))

    def _get(self, sql: str, args=()) -> sqlite3.Row:
        row = self.conn.execute(sql, args).fetchone()
        if not row:
            raise BoatError("记录不存在")
        return row

    def _suspend_guard(self) -> None:
        if self.weather.is_suspended():
            raise BoatError("天气停航中，不能创建新的出航批次")

    # ---------- 售票 ----------
    def sell_ticket(self, *, name: str, phone: str, party_size: int,
                    boat_pref: str, has_child: bool = False,
                    children_count: int = 0,
                    deposit: int | None = None) -> dict:
        if party_size < 1 or party_size > 12:
            raise BoatError("人数需在 1-12 之间")
        if children_count > party_size:
            raise BoatError("儿童数不能超过总人数")
        bt = self._get("SELECT * FROM boat_types WHERE code=?", (boat_pref,))
        if party_size > bt["capacity"]:
            raise BoatError(f"{party_size} 人超出 {bt['name']} 单船上限 {bt['capacity']} 人")
        with write_lock():
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                t = now()
                cur = self.conn.execute(
                    "INSERT INTO visitors(name,phone,party_size,has_child,"
                    "children_count,created_at) VALUES(?,?,?,?,?,?)",
                    (name, phone, party_size, int(has_child or children_count > 0),
                     children_count, ts(t)))
                visitor_id = cur.lastrowid
                seq = self.conn.execute(
                    "SELECT COALESCE(MAX(queue_seq),0)+1 FROM tickets").fetchone()[0]
                letter = QUEUE_LETTER[boat_pref]
                ticket_no = f"{letter}-{seq:03d}"
                dep = deposit if deposit is not None else DEPOSIT_PER_PERSON * party_size
                cur = self.conn.execute(
                    "INSERT INTO tickets(ticket_no,visitor_id,party_size,boat_pref,"
                    "has_child,children_count,status,enqueued_at,deposit,queue_seq)"
                    " VALUES(?,?,?,?,?,?,'WAITING',?,?,?)",
                    (ticket_no, visitor_id, party_size, boat_pref,
                     int(has_child or children_count > 0), children_count,
                     ts(t), dep, seq))
                ticket_id = cur.lastrowid
                pay = self.payments.charge(
                    idempotency_key=f"dep:{ticket_id}", amount=dep, kind="DEPOSIT")
                self.conn.execute(
                    "INSERT INTO payments(ticket_id,kind,amount,status,provider,"
                    "reference,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (ticket_id, "DEPOSIT", dep, pay["status"], "MOCK",
                     pay["reference"], f"dep:{ticket_id}", ts(now())))
                self._log("TICKET_SOLD", {"ticket_no": ticket_no,
                                          "party_size": party_size})
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.ticket(ticket_id)

    def ticket(self, ticket_id: int) -> dict:
        row = self._get(
            "SELECT t.*, v.name, v.phone FROM tickets t "
            "JOIN visitors v ON v.id=t.visitor_id WHERE t.id=?", (ticket_id,))
        return dict(row)

    def find_ticket(self, ticket_no: str) -> dict:
        return dict(self._get(
            "SELECT t.*, v.name, v.phone FROM tickets t "
            "JOIN visitors v ON v.id=t.visitor_id WHERE t.ticket_no=?",
            (ticket_no.strip(),)))

    # ---------- 候船队列 / 叫号 ----------
    def queue_overview(self) -> dict:
        w = self.weather.current()
        rows = self.conn.execute(
            "SELECT * FROM tickets WHERE status IN ('WAITING','CALLED','MISSED','READY')"
            " ORDER BY COALESCE(priority_seq,queue_seq)").fetchall()
        items, waiting_n = [], 0
        t0 = now()
        for r in rows:
            d = dict(r)
            if d["status"] in ("WAITING", "MISSED"):
                waiting_n += 1
                # 预计等待：前面每单约 8 分钟
                d["eta_minutes"] = max(0, waiting_n * 8 - 2)
            else:
                d["eta_minutes"] = 0
            items.append(d)
        head = next((d for d in items if d["status"] in ("CALLED", "READY")), None)
        last_bc = self.conn.execute(
            "SELECT * FROM broadcasts ORDER BY id DESC LIMIT 1").fetchone()
        return {"weather": w.as_dict(), "queue": items, "waiting_count": waiting_n,
                "now": ts(t0), "calling": head,
                "broadcast": dict(last_bc) if last_bc else None}

    def call_next(self, boat_type: str | None = None) -> dict | None:
        """叫下一号（默认按全局顺序，可限定船型）。"""
        with write_lock():
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                args: list = []
                # 只叫活跃等待号码；过号号码需显式“过号重排”才回队尾
                row = self.conn.execute(
                    "SELECT * FROM tickets WHERE status='WAITING'"
                    + (" AND boat_pref=?" if boat_type else "")
                    + " ORDER BY COALESCE(priority_seq,queue_seq) LIMIT 1",
                    args + ([boat_type] if boat_type else [])).fetchone()
                if not row:
                    self.conn.commit()
                    return None
                self.conn.execute(
                    "UPDATE tickets SET status='CALLED', called_at=? WHERE id=?",
                    (ts(now()), row["id"]))
                self._log("NUMBER_CALLED", {"ticket_no": row["ticket_no"]})
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.ticket(row["id"])

    def mark_missed(self, ticket_id: int) -> dict:
        """过号：叫号后未到，转入 MISSED。"""
        with write_lock():
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                t = self._get("SELECT * FROM tickets WHERE id=?", (ticket_id,))
                if t["status"] != "CALLED":
                    raise BoatError("当前状态不能标记过号")
                self.conn.execute(
                    "UPDATE tickets SET status='MISSED', missed_at=? WHERE id=?",
                    (ts(now()), ticket_id))
                self._log("NUMBER_MISSED", {"ticket_no": t["ticket_no"]})
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.ticket(ticket_id)

    def requeue_missed(self, ticket_id: int) -> dict:
        """过号恢复：重排到队尾（priority_seq 取当前最大值之后）。"""
        with write_lock():
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                t = self._get("SELECT * FROM tickets WHERE id=?", (ticket_id,))
                if t["status"] != "MISSED":
                    raise BoatError("只有过号号码可以重排")
                nxt = self.conn.execute(
                    "SELECT COALESCE(MAX(COALESCE(priority_seq,queue_seq)),0)+1"
                    " FROM tickets").fetchone()[0]
                self.conn.execute(
                    "UPDATE tickets SET status='WAITING', priority_seq=?,"
                    " missed_at=NULL WHERE id=?", (nxt, ticket_id))
                self._log("NUMBER_REQUEUED", {"ticket_no": t["ticket_no"],
                                              "new_seq": nxt})
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.ticket(ticket_id)

    # ---------- 组批工作台 ----------
    def batch_workbench(self) -> dict:
        boats = [dict(r) for r in self.conn.execute(
            "SELECT b.*, bt.name type_name, bt.capacity, bt.color "
            "FROM boats b JOIN boat_types bt ON bt.code=b.type_code "
            "ORDER BY b.type_code, b.id").fetchall()]
        pool = [dict(r) for r in self.conn.execute(
            "SELECT t.*, v.name FROM tickets t JOIN visitors v ON v.id=t.visitor_id"
            " WHERE t.status IN ('WAITING','CALLED','MISSED','READY')"
            " ORDER BY COALESCE(priority_seq,queue_seq)").fetchall()]
        active = [dict(r) for r in self.conn.execute(
            "SELECT ba.*, bo.name boat_name FROM batches ba "
            "JOIN boats bo ON bo.id=ba.boat_id "
            "WHERE ba.status IN ('CREATED','BOARDING') ORDER BY ba.id").fetchall()]
        for b in active:
            b["tickets"] = [dict(r) for r in self.conn.execute(
                "SELECT t.*, v.name FROM tickets t JOIN visitors v ON v.id=t.visitor_id"
                " WHERE t.batch_id=? ORDER BY t.queue_seq", (b["id"],)).fetchall()]
        return {"boats": boats, "pool": pool, "active_batches": active,
                "suspended": self.weather.is_suspended()}

    def _vest_stock(self, type_code: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT size,total FROM vest_inventory WHERE type_code=?",
            (type_code,)).fetchall()
        return {r["size"]: r["total"] for r in rows}

    def create_batch(self, boat_id: int, ticket_ids: list[int]) -> dict:
        if not ticket_ids:
            raise BoatError("至少选择一个候船号")
        with write_lock():
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self._suspend_guard()
                boat = self._get(
                    "SELECT b.*, bt.capacity, bt.name type_name FROM boats b "
                    "JOIN boat_types bt ON bt.code=b.type_code WHERE b.id=?",
                    (boat_id,))
                # CAS：只有 DOCKED 的船能开新批次（防止同船并发两批）
                cur = self.conn.execute(
                    "UPDATE boats SET status='LOCKED' WHERE id=? AND status='DOCKED'",
                    (boat_id,))
                if cur.rowcount == 0:
                    raise BoatError(f"{boat['name']} 当前不可用（已锁定或出航中）")
                headcount = 0
                tid_rows = []
                for tid in ticket_ids:
                    t = self._get("SELECT * FROM tickets WHERE id=?", (tid,))
                    if t["status"] not in ("WAITING", "CALLED", "MISSED"):
                        raise BoatError(f"{t['ticket_no']} 不在可组批状态")
                    if t["boat_pref"] != boat["type_code"]:
                        raise BoatError(
                            f"{t['ticket_no']} 预约船型与 {boat['type_name']} 不符")
                    headcount += t["party_size"]
                    tid_rows.append(t)
                if headcount > boat["capacity"]:
                    raise BoatError(
                        f"超载：合计 {headcount} 人 > {boat['type_name']} 上限 "
                        f"{boat['capacity']} 人")
                # 儿童尺码库存预检
                child_n = sum(t["children_count"] or (t["party_size"] if t["has_child"] else 0)
                              for t in tid_rows)
                stock = self._vest_stock(boat["type_code"])
                if stock.get("S", 0) < child_n:
                    raise BoatError(
                        f"儿童救生衣（S）不足：需要 {child_n} 件，库存 {stock.get('S',0)} 件")
                t0 = now()
                cur = self.conn.execute(
                    "INSERT INTO batches(boat_id,type_code,status,headcount,"
                    "created_at,note) VALUES(?,?, 'CREATED', ?, ?, ?)",
                    (boat_id, boat["type_code"], headcount, ts(t0),
                     f"{headcount}/{boat['capacity']} 人"))
                batch_id = cur.lastrowid
                for t in tid_rows:
                    self.conn.execute(
                        "UPDATE tickets SET status='READY', batch_id=?, ready_at=?"
                        " WHERE id=?", (batch_id, ts(t0), t["id"]))
                    for i in range(t["party_size"]):
                        self.conn.execute(
                            "INSERT INTO boardings(batch_id,ticket_id,person_idx)"
                            " VALUES(?,?,?)", (batch_id, t["id"], i))
                self.conn.execute(
                    "UPDATE boats SET active_trip=NULL WHERE id=?", (boat_id,))
                self._log("BATCH_CREATED", {"batch_id": batch_id,
                                            "boat": boat["name"],
                                            "headcount": headcount})
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.batch_detail(batch_id)

    def batch_detail(self, batch_id: int) -> dict:
        b = dict(self._get(
            "SELECT ba.*, bo.name boat_name, bt.capacity, bt.name type_name "
            "FROM batches ba JOIN boats bo ON bo.id=ba.boat_id "
            "JOIN boat_types bt ON bt.code=ba.type_code WHERE ba.id=?",
            (batch_id,)))
        people = [dict(r) for r in self.conn.execute(
            "SELECT bd.*, t.ticket_no, v.name visitor_name FROM boardings bd "
            "JOIN tickets t ON t.id=bd.ticket_id "
            "JOIN visitors v ON v.id=t.visitor_id "
            "WHERE bd.batch_id=? ORDER BY t.queue_seq, bd.person_idx",
            (batch_id,)).fetchall()]
        b["people"] = people
        b["checked_count"] = sum(1 for p in people if p["checked"])
        return b

    def change_boat(self, batch_id: int, new_boat_id: int) -> dict:
        """临时换船：可换任意可用船型，但新船不得突破人数上限。"""
        with write_lock():
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                batch = self._get("SELECT * FROM batches WHERE id=?", (batch_id,))
                if batch["status"] not in ("CREATED", "BOARDING"):
                    raise BoatError("批次已出航，不能换船")
                old = self._get(
                    "SELECT b.*, bt.name type_name FROM boats b "
                    "JOIN boat_types bt ON bt.code=b.type_code WHERE b.id=?",
                    (batch["boat_id"],))
                new = self._get(
                    "SELECT b.*, bt.capacity, bt.name type_name FROM boats b "
                    "JOIN boat_types bt ON bt.code=b.type_code WHERE b.id=?",
                    (new_boat_id,))
                if new_boat_id == old["id"]:
                    raise BoatError("新船与当前船只相同")
                # 容量约束：换船不得突破人数上限
                if batch["headcount"] > new["capacity"]:
                    raise BoatError(
                        f"换船失败：{batch['headcount']} 人超过 {new['name']}"
                        f"（{new['type_name']}）上限 {new['capacity']} 人")
                cur = self.conn.execute(
                    "UPDATE boats SET status='LOCKED' WHERE id=? AND status='DOCKED'",
                    (new_boat_id,))
                if cur.rowcount == 0:
                    raise BoatError(f"{new['name']} 当前不可用")
                self.conn.execute(
                    "UPDATE boats SET status='DOCKED', active_trip=NULL WHERE id=?",
                    (old["id"],))
                self.conn.execute(
                    "UPDATE batches SET boat_id=?, type_code=?, note=? WHERE id=?",
                    (new_boat_id, new["type_code"],
                     f"由 {old['name']}（{old['type_name']}）临时换至 "
                     f"{new['name']}（{new['type_name']}）", batch_id))
                self._log("BOAT_CHANGED", {"batch_id": batch_id,
                                           "old": old["name"], "new": new["name"]})
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.batch_detail(batch_id)

    def disband_batch(self, batch_id: int, reason: str = "") -> dict:
        """解散未出航批次（停航中断时调用），游客退回候船队列。"""
        with write_lock():
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                batch = self._get("SELECT * FROM batches WHERE id=?", (batch_id,))
                if batch["status"] not in ("CREATED", "BOARDING"):
                    raise BoatError("批次已出航，不能解散")
                boat = self._get("SELECT * FROM boats WHERE id=?", (batch["boat_id"],))
                self.conn.execute(
                    "UPDATE boats SET status='DOCKED' WHERE id=?", (boat["id"],))
                tids = [r["id"] for r in self.conn.execute(
                    "SELECT id FROM tickets WHERE batch_id=?", (batch_id,)).fetchall()]
                for tid in tids:
                    self.conn.execute(
                        "UPDATE tickets SET status='WAITING', batch_id=NULL WHERE id=?",
                        (tid,))
                self.conn.execute(
                    "UPDATE batches SET status='SUSPENDED', suspended_at=?, note=?"
                    " WHERE id=?",
                    (ts(now()), reason or "天气停航，批次解散", batch_id))
                self._log("BATCH_SUSPENDED", {"batch_id": batch_id,
                                              "tickets": tids, "reason": reason})
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return {"ok": True, "batch_id": batch_id}

    # ---------- 登船核验 ----------
    def boarding_view(self, batch_id: int) -> dict:
        b = self.batch_detail(batch_id)
        b["stock"] = self._vest_stock(b["type_code"])
        return b

    def confirm_vest(self, boarding_id: int, vest_size: str) -> dict:
        """逐人确认救生衣尺码。"""
        if vest_size not in VEST_SIZES:
            raise BoatError("尺码无效")
        with write_lock():
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                bd = self._get("SELECT * FROM boardings WHERE id=?", (boarding_id,))
                batch = self._get("SELECT * FROM batches WHERE id=?", (bd["batch_id"],))
                if batch["status"] not in ("CREATED", "BOARDING"):
                    raise BoatError("批次不在登船阶段")
                # 救生衣为全局实物库存：统计所有未完结批次（含湖面航次）
                # 已发放的该尺码数量；本行当前占用不计入（允许改码）
                stock = self._vest_stock(batch["type_code"])
                used = self.conn.execute(
                    "SELECT COUNT(*) c FROM boardings bd "
                    "JOIN batches ba ON ba.id=bd.batch_id "
                    "WHERE ba.type_code=? AND bd.checked=1 AND bd.vest_size=? "
                    "AND ba.status IN ('CREATED','BOARDING','DEPARTED')"
                    " AND bd.id<>?",
                    (batch["type_code"], vest_size, boarding_id)).fetchone()["c"]
                if used >= stock.get(vest_size, 0):
                    raise BoatError(f"{SIZE_LABEL[vest_size]} 救生衣库存不足")
                self.conn.execute(
                    "UPDATE boardings SET vest_size=?, checked=1, checked_at=?,"
                    " scan_count=scan_count WHERE id=?",
                    (vest_size, ts(now()), boarding_id))
                if batch["status"] == "CREATED":
                    self.conn.execute(
                        "UPDATE batches SET status='BOARDING' WHERE id=?",
                        (batch["id"],))
                self._log("VEST_CONFIRMED", {"boarding_id": boarding_id,
                                             "size": vest_size})
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.boarding_view(bd["batch_id"])

    def gate_scan(self, ticket_no: str, gate: str) -> dict:
        """闸口扫描候船号放行。

        两处闸口同时扫描同一号码：条件 UPDATE 只允许一次成功；
        重复扫描返回 replay=True，不重复放行。
        """
        gate = gate.upper()
        if gate not in ("G1", "G2"):
            raise BoatError("闸口无效")
        with write_lock():
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                t = self._get("SELECT * FROM tickets WHERE ticket_no=?", (ticket_no,))
                if t["batch_id"] is None or t["status"] not in ("READY", "BOARDING"):
                    raise BoatError(f"{ticket_no} 尚未分配批次，不能放行")
                batch = self._get("SELECT * FROM batches WHERE id=?", (t["batch_id"],))
                if batch["status"] not in ("CREATED", "BOARDING"):
                    raise BoatError("批次不在登船阶段")
                unchecked = self.conn.execute(
                    "SELECT COUNT(*) c FROM boardings WHERE ticket_id=? AND checked=0",
                    (t["id"],)).fetchone()["c"]
                if unchecked:
                    raise BoatError(f"仍有 {unchecked} 人未确认救生衣，不能放行")
                # CAS：READY -> BOARDING，仅一个闸口成功
                cur = self.conn.execute(
                    "UPDATE tickets SET status='BOARDING' WHERE id=? AND status='READY'",
                    (t["id"],))
                allowed = cur.rowcount == 1
                # 重复扫描只累加次数，不覆盖首个放行闸口
                if allowed:
                    self.conn.execute(
                        "UPDATE boardings SET gate=?, scan_count=scan_count+1"
                        " WHERE ticket_id=?", (gate, t["id"]))
                else:
                    self.conn.execute(
                        "UPDATE boardings SET scan_count=scan_count+1 WHERE ticket_id=?",
                        (t["id"],))
                if not allowed:
                    self._log("GATE_DUPLICATE_SCAN",
                              {"ticket_no": ticket_no, "gate": gate})
                    self.conn.commit()
                    return {"allowed": False, "replay": True,
                            "message": f"{ticket_no} 已在另一闸口放行，请勿重复通过",
                            "ticket_no": ticket_no}
                self._log("GATE_PASS", {"ticket_no": ticket_no, "gate": gate})
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return {"allowed": True, "replay": False, "gate": gate,
                "message": f"{ticket_no} 放行成功（{gate}）",
                "ticket_no": ticket_no}

    # ---------- 出航 / 湖面计时 ----------
    def departable_batches(self) -> list[dict]:
        out = []
        for r in self.conn.execute(
                "SELECT ba.*, bo.name boat_name FROM batches ba "
                "JOIN boats bo ON bo.id=ba.boat_id "
                "WHERE ba.status IN ('CREATED','BOARDING') ORDER BY ba.id"):
            d = dict(r)
            pending = self.conn.execute(
                "SELECT COUNT(*) c FROM tickets WHERE batch_id=?"
                " AND status IN ('READY')", (d["id"],)).fetchone()["c"]
            d["pending_tickets"] = pending
            out.append(d)
        return out

    def depart(self, batch_id: int, minutes: int = TRIP_MINUTES) -> dict:
        with write_lock():
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self._suspend_guard()  # 天气突变：停航期间不能出航
                batch = self._get("SELECT * FROM batches WHERE id=?", (batch_id,))
                if batch["status"] not in ("CREATED", "BOARDING"):
                    raise BoatError("批次状态不能出航")
                pending = self.conn.execute(
                    "SELECT COUNT(*) c FROM tickets WHERE batch_id=?"
                    " AND status NOT IN ('BOARDING')", (batch_id,)).fetchone()["c"]
                if pending:
                    raise BoatError(f"仍有 {pending} 个候船号未通过闸口")
                t0 = now()
                due = t0 + timedelta(minutes=minutes)
                cur = self.conn.execute(
                    "INSERT INTO trips(batch_id,boat_id,departed_at,expected_back,"
                    "due_back,status) VALUES(?,?,?,?,?, 'OUT')",
                    (batch_id, batch["boat_id"], ts(t0),
                     ts(due), ts(due)))
                trip_id = cur.lastrowid
                self.conn.execute(
                    "UPDATE batches SET status='DEPARTED', departed_at=?,"
                    " expected_back=?, due_back=? WHERE id=?",
                    (ts(t0), ts(due), ts(due), batch_id))
                self.conn.execute(
                    "UPDATE boats SET status='OUT', active_trip=? WHERE id=?",
                    (trip_id, batch["boat_id"]))
                self.conn.execute(
                    "UPDATE tickets SET status='ON_WATER' WHERE batch_id=?",
                    (batch_id,))
                self._log("DEPARTED", {"batch_id": batch_id, "trip_id": trip_id,
                                       "due_back": ts(due)})
                self.conn.commit()
                view = self.lake_view()
                view["trip_id"] = trip_id
                view["batch_id"] = batch_id
            except Exception:
                self.conn.rollback()
                raise
        return view

    def lake_view(self) -> dict:
        trips = []
        t0 = now()
        for r in self.conn.execute(
                "SELECT tr.*, bo.name boat_name, ba.headcount, bt.name type_name "
                "FROM trips tr JOIN boats bo ON bo.id=tr.boat_id "
                "JOIN batches ba ON ba.id=tr.batch_id "
                "JOIN boat_types bt ON bt.code=ba.type_code "
                "WHERE tr.status IN ('OUT','OVERDUE') ORDER BY tr.departed_at"):
            d = dict(r)
            due = parse(d["due_back"])
            d["remaining_min"] = int((due - t0).total_seconds() // 60)
            d["overdue_min"] = max(0, int((t0 - due).total_seconds() // 60))
            d["overdue"] = t0 > due
            trips.append(d)
        return {"trips": trips, "now": ts(t0),
                "suspended": self.weather.is_suspended(),
                "weather": self.weather.current().as_dict()}

    def recall_boat(self, trip_id: int) -> dict:
        """超时/天气突变时人工呼叫回港，记一条事件。"""
        with write_lock():
            tr = self._get("SELECT * FROM trips WHERE id=?", (trip_id,))
            self._log("RECALL", {"trip_id": trip_id,
                                 "reason": "超时未归或天气突变，呼叫回港"})
            self.conn.commit()
        return {"ok": True}

    # ---------- 回港检查 ----------
    def dock_view(self) -> dict:
        rows = [dict(r) for r in self.conn.execute(
            "SELECT tr.*, bo.name boat_name, ba.headcount FROM trips tr "
            "JOIN boats bo ON bo.id=tr.boat_id "
            "JOIN batches ba ON ba.id=tr.batch_id "
            "WHERE tr.status IN ('OUT','OVERDUE','RETURNED') "
            "AND EXISTS (SELECT 1 FROM tickets t WHERE t.batch_id=tr.batch_id"
            " AND t.status != 'SETTLED') "
            "ORDER BY tr.departed_at")]
        return {"trips": rows, "now": ts(now())}

    def trip_detail(self, trip_id: int) -> dict:
        tr = dict(self._get(
            "SELECT tr.*, bo.name boat_name, ba.headcount "
            "FROM trips tr JOIN boats bo ON bo.id=tr.boat_id "
            "JOIN batches ba ON ba.id=tr.batch_id WHERE tr.id=?", (trip_id,)))
        tr["tickets"] = [dict(r) for r in self.conn.execute(
            "SELECT t.id, t.ticket_no, t.deposit, v.name, t.party_size, "
            "s.deducted settled_deducted FROM tickets t "
            "JOIN visitors v ON v.id=t.visitor_id "
            "LEFT JOIN settlements s ON s.ticket_id=t.id "
            "WHERE t.batch_id=? ORDER BY t.queue_seq", (tr["batch_id"],)).fetchall()]
        tr["photos"] = [dict(r) for r in self.conn.execute(
            "SELECT * FROM hull_photos WHERE trip_id=?", (trip_id,)).fetchall()]
        return tr

    def add_hull_photo(self, trip_id: int, filename: str, data: bytes,
                       label: str = "船体照片") -> dict:
        """保存船体刮碰照片到本地目录，库存相对路径。"""
        trip = self._get("SELECT * FROM trips WHERE id=?", (trip_id,))
        safe = Path(filename).name.replace(" ", "_")
        name = f"trip{trip_id}_{len(list(self.photo_dir.glob('trip*')))}_{safe}"
        path = self.photo_dir / name
        path.write_bytes(data)
        with write_lock():
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self.conn.execute(
                    "INSERT INTO hull_photos(trip_id,path,label,created_at)"
                    " VALUES(?,?,?,?)",
                    (trip_id, f"photos/{name}", label, ts(now())))
                self._log("HULL_PHOTO", {"trip_id": trip_id, "path": name})
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return {"id": cur.lastrowid, "path": f"photos/{name}"}

    def return_check(self, trip_id: int, *, hull_ok: bool, vest_missing: int,
                     note: str = "") -> dict:
        """完成回港检查：登记船体/救生衣状况并计算各单应扣金额。

        必须先完成检查，押金结算才能进行（settlements 关联 trip 检查结果）。
        """
        with write_lock():
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                tr = self._get("SELECT * FROM trips WHERE id=?", (trip_id,))
                if tr["status"] not in ("OUT", "OVERDUE", "RETURNED"):
                    raise BoatError("航次状态异常")
                t0 = now()
                tids = [r["id"] for r in self.conn.execute(
                    "SELECT id FROM tickets WHERE batch_id=? ORDER BY queue_seq",
                    (tr["batch_id"],)).fetchall()]
                n = len(tids)
                damage_each = DAMAGE_FEE_EACH if not hull_ok else 0
                # 救生衣缺失赔偿由同船各单均摊
                vest_total = vest_missing * VEST_FEE_EACH
                self.conn.execute(
                    "UPDATE trips SET status='RETURNED', returned_at=?, hull_ok=?,"
                    " vest_ok=?, damage_fee_each=?, vest_fee_total=?, inspect_note=? "
                    "WHERE id=?",
                    (ts(t0), int(hull_ok), int(vest_missing == 0),
                     damage_each, vest_total, note, trip_id))
                self.conn.execute(
                    "UPDATE boats SET status='INSPECTING' WHERE id=?", (tr["boat_id"],))
                for tid in tids:
                    self.conn.execute(
                        "UPDATE tickets SET status='RETURNED' WHERE id=?", (tid,))
                self._log("RETURN_CHECK", {"trip_id": trip_id, "hull_ok": hull_ok,
                                           "vest_missing": vest_missing,
                                           "damage_each": damage_each})
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.trip_detail(trip_id)

    def confirm_vest_return(self, trip_id: int) -> dict:
        """救生衣全部归还确认（检查通过后的归还登记），订单进入待结算。"""
        with write_lock():
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                tr = self._get("SELECT * FROM trips WHERE id=?", (trip_id,))
                if tr["status"] != "RETURNED":
                    raise BoatError("请先完成回港检查")
                tids = [r["id"] for r in self.conn.execute(
                    "SELECT id FROM tickets WHERE batch_id=? AND status='RETURNED'",
                    (tr["batch_id"],)).fetchall()]
                for tid in tids:
                    self.conn.execute(
                        "UPDATE tickets SET status='VEST_RETURNED' WHERE id=?", (tid,))
                self.conn.execute(
                    "UPDATE boats SET status='DOCKED', active_trip=NULL WHERE id=?",
                    (tr["boat_id"],))
                # 批次完结，救生衣实物库存随之释放
                self.conn.execute(
                    "UPDATE batches SET status='COMPLETED' WHERE id=?",
                    (tr["batch_id"],))
                self._log("VEST_RETURNED_ALL", {"trip_id": trip_id})
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.trip_detail(trip_id)

    # ---------- 押金复核 ----------
    def deposit_view(self) -> dict:
        rows = [dict(r) for r in self.conn.execute(
            "SELECT t.id, t.ticket_no, t.deposit, t.status, v.name, v.party_size,"
            " tr.id trip_id, tr.hull_ok, tr.vest_ok, tr.damage_fee_each,"
            " tr.vest_fee_total, tr.returned_at, s.deducted already_deducted,"
            " (SELECT COUNT(*) FROM tickets t2 WHERE t2.batch_id=t.batch_id) order_n"
            " FROM tickets t JOIN visitors v ON v.id=t.visitor_id "
            "JOIN batches ba ON ba.id=t.batch_id "
            "JOIN trips tr ON tr.batch_id=ba.id "
            "LEFT JOIN settlements s ON s.ticket_id=t.id "
            "WHERE t.status IN ('RETURNED','VEST_RETURNED','SETTLED') "
            "ORDER BY tr.id, t.queue_seq").fetchall()]
        for r in rows:
            r["suggested_deduct"] = self._suggested_deduction(r)
        return {"items": rows, "now": ts(now())}

    def _suggested_deduction(self, row: sqlite3.Row | dict) -> int:
        d = dict(row) if isinstance(row, sqlite3.Row) else row
        if d.get("already_deducted") is not None:
            return d["already_deducted"]
        n = d.get("order_n", 1) or 1
        total = (d.get("damage_fee_each") or 0)
        if d.get("vest_fee_total"):
            total += d["vest_fee_total"] // n
        return min(total, d["deposit"])

    def settle(self, ticket_id: int, deduct_override: int | None = None) -> dict:
        """押金复核结算（幂等）。

        - 必须关联已完成的回港检查；
        - settlements 对 ticket_id UNIQUE，重复提交返回首笔，绝不重复扣款；
        - 扣款走支付渠道幂等键 ded:<ticket_id>。
        """
        with write_lock():
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                t = self._get("SELECT * FROM tickets WHERE id=?", (ticket_id,))
                # 已有结算：直接回放（重复提交不重复扣款）
                old = self.conn.execute(
                    "SELECT * FROM settlements WHERE ticket_id=?", (ticket_id,)).fetchone()
                if old:
                    self.conn.commit()
                    return {"idempotent": True, "settlement": dict(old),
                            "message": "该单已结算，返回首次结算结果"}
                if t["status"] not in ("RETURNED", "VEST_RETURNED"):
                    raise BoatError("订单尚未完成回港检查与救生衣归还")
                tr = self._get(
                    "SELECT * FROM trips WHERE batch_id=?", (t["batch_id"],))
                if tr["status"] != "RETURNED":
                    raise BoatError("回港检查未完成，不能结算押金")
                n = self.conn.execute(
                    "SELECT COUNT(*) c FROM tickets WHERE batch_id=?",
                    (t["batch_id"],)).fetchone()["c"]
                suggested = (tr["damage_fee_each"] or 0)
                if tr["vest_fee_total"]:
                    suggested += tr["vest_fee_total"] // n
                deduct = suggested if deduct_override is None else deduct_override
                if deduct < 0 or deduct > t["deposit"]:
                    raise BoatError("扣款金额超出押金范围")
                refunded = t["deposit"] - deduct
                pay = self.payments.charge(
                    idempotency_key=f"ded:{ticket_id}", amount=deduct, kind="DEDUCT")
                cur = self.conn.execute(
                    "INSERT INTO settlements(ticket_id,trip_id,deducted,refunded,"
                    "reason,settled_at) VALUES(?,?,?,?,?,?)",
                    (ticket_id, tr["id"], deduct, refunded,
                     ("船体刮碰/救生衣缺失" if deduct else "无损坏，全额退还"),
                     ts(now())))
                if deduct:
                    self.conn.execute(
                        "INSERT INTO payments(ticket_id,kind,amount,status,provider,"
                        "reference,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (ticket_id, "DEDUCT", deduct, pay["status"], "MOCK",
                         pay["reference"], f"ded:{ticket_id}", ts(now())))
                self.conn.execute(
                    "UPDATE tickets SET status='SETTLED' WHERE id=?", (ticket_id,))
                self._log("SETTLED", {"ticket_id": ticket_id, "deduct": deduct,
                                      "replayed_payment": pay["replayed"]})
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return {"idempotent": False,
                "settlement": dict(self._get(
                    "SELECT * FROM settlements WHERE ticket_id=?", (ticket_id,)))}

    # ---------- 天气 / 广播 ----------
    def set_weather(self, level: str) -> dict:
        self.weather.force(level)
        w = self.weather.current()
        with write_lock():
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    "INSERT INTO broadcasts(level,message,created_at) VALUES(?,?,?)",
                    (w.level, w.message, ts(now())))
                affected = []
                if level == "SUSPEND":
                    # 停航中断：已组批未出航的批次一律解散回队列
                    rows = self.conn.execute(
                        "SELECT id FROM batches WHERE status IN ('CREATED','BOARDING')"
                    ).fetchall()
                    for r in rows:
                        self.conn.execute(
                            "UPDATE boats SET status='DOCKED' WHERE id="
                            "(SELECT boat_id FROM batches WHERE id=?)", (r["id"],))
                        for t in self.conn.execute(
                                "SELECT id FROM tickets WHERE batch_id=?",
                                (r["id"],)).fetchall():
                            self.conn.execute(
                                "UPDATE tickets SET status='WAITING', batch_id=NULL"
                                " WHERE id=?", (t["id"],))
                        self.conn.execute(
                            "UPDATE batches SET status='SUSPENDED', suspended_at=?,"
                            " note='天气突变停航，批次自动解散' WHERE id=?",
                            (ts(now()), r["id"]))
                        affected.append(r["id"])
                    # 湖面上的船标记催促回港（事件由调度器/人工跟进）
                    for tr in self.conn.execute(
                            "SELECT id FROM trips WHERE status IN ('OUT','OVERDUE')"
                    ).fetchall():
                        self._log("RECALL", {"trip_id": tr["id"],
                                             "reason": "天气突变，催促回港"})
                self._log("WEATHER_SET", {"level": level, "affected_batches": affected})
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return {"weather": w.as_dict(), "suspended_batches": affected}

    # ---------- 调度器扫描：过号 + 超时 ----------
    def scheduler_tick(self) -> dict:
        """进程内定时任务：叫号超时自动过号；湖面船只超时升级 OVERDUE。"""
        reminded, missed = [], []
        with write_lock():
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                t0 = now()
                for r in self.conn.execute(
                        "SELECT * FROM tickets WHERE status='CALLED'").fetchall():
                    if t0 - parse(r["called_at"]) >= timedelta(minutes=3):
                        self.conn.execute(
                            "UPDATE tickets SET status='MISSED', missed_at=? WHERE id=?",
                            (ts(t0), r["id"]))
                        missed.append(r["ticket_no"])
                        self._log("AUTO_MISSED", {"ticket_no": r["ticket_no"]})
                for r in self.conn.execute(
                        "SELECT * FROM trips WHERE status IN ('OUT','OVERDUE')"
                ).fetchall():
                    due = parse(r["due_back"])
                    remind_at = due - timedelta(minutes=REMIND_BEFORE_MIN)
                    if t0 >= remind_at and r["reminded_at"] is None:
                        self.conn.execute(
                            "UPDATE trips SET reminded_at=? WHERE id=?",
                            (ts(t0), r["id"]))
                        reminded.append(r["id"])
                        self._log("RETURN_REMIND", {"trip_id": r["id"]})
                    if t0 > due and r["status"] == "OUT":
                        self.conn.execute(
                            "UPDATE trips SET status='OVERDUE' WHERE id=?", (r["id"],))
                        self._log("OVERDUE", {"trip_id": r["id"]})
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return {"reminded": reminded, "missed": missed, "at": ts(t0)}
