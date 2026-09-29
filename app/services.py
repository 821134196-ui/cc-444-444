"""业务服务层:候船队列、组批、登船核验、航次、回港检查、押金结算。

写操作统一走进程内锁串行化,并由 SQLite 唯一约束兜底:
- gate_scans.ticket_id 唯一  -> 同一候船号只能放行一次
- payments.idem_key 唯一     -> 押金扣除重复提交不重复扣款
"""
from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
import uuid

from .adapters import PaymentAdapter, WeatherAdapter
from .db import PHOTO_DIR, get_conn

CALL_WINDOW_SEC = 120          # 叫号有效时长
HULL_FEE = {"ok": 0, "scratch": 120, "serious": 400}
JACKET_MISSING_FEE = 80
OVERDUE_PER_MIN_PER_PERSON = 1.0

_TYPE_PREFIX = {1: "A", 2: "B", 3: "C"}

_lock = threading.RLock()
payments = PaymentAdapter()
weather_adapter = WeatherAdapter()


class ServiceError(Exception):
    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code


# ---------- 基础工具 ----------

def _row(query: str, args=()) -> sqlite3.Row | None:
    return get_conn().execute(query, args).fetchone()


def _all(query: str, args=()) -> list[sqlite3.Row]:
    return get_conn().execute(query, args).fetchall()


def _jacket_available(size_code: str) -> int:
    r = _row(
        "SELECT s.total - COALESCE((SELECT COUNT(*) FROM life_jacket_holds h"
        " WHERE h.size_code = s.size_code),0) AS free"
        " FROM life_jacket_stock s WHERE s.size_code = ?",
        (size_code,),
    )
    return int(r["free"]) if r else 0


def _batch_party_count(conn, batch_id: int) -> int:
    return int(conn.execute(
        "SELECT COALESCE(SUM(t.party_size),0) c FROM batch_tickets bt"
        " JOIN tickets t ON t.id = bt.ticket_id WHERE bt.batch_id = ?",
        (batch_id,),
    ).fetchone()["c"])


# ---------- 售票 / 候船队列 ----------

def sell_ticket(contact_name: str, boat_type_id: int, adults: int,
                children: int, visitor_id: int | None = None) -> dict:
    """售票 -> 生成候船号并入队,票款与押金预授权走模拟支付。"""
    if adults < 0 or children < 0 or adults + children <= 0:
        raise ServiceError("人数至少为 1 人", "bad_party")
    with _lock:
        conn = get_conn()
        bt = _row("SELECT * FROM boat_types WHERE id = ?", (boat_type_id,))
        if bt is None:
            raise ServiceError("船型不存在", "bad_type")
        party = adults + children
        if party > bt["capacity"]:
            raise ServiceError(
                f"该团 {party} 人超过{bt['name']}单船上限 {bt['capacity']} 人,请拆分购票",
                "over_capacity")
        prefix = _TYPE_PREFIX.get(boat_type_id, "T")
        seq = conn.execute(
            "SELECT COUNT(*) c FROM tickets WHERE boat_type_id = ?", (boat_type_id,)
        ).fetchone()["c"] + 1
        queue_no = f"{prefix}{seq:03d}"
        now = time.time()
        amount = round(bt["hourly_rate"] * party * bt["duration_min"] / 60, 2)
        deposit = round(bt["deposit_pp"] * party, 2)
        cur = conn.execute(
            "INSERT INTO tickets(queue_no, visitor_id, contact_name, boat_type_id,"
            " party_size, adults, children, status, created_at,"
            " ticket_amount, deposit_total)"
            " VALUES (?,?,?,?,?,?,?,'waiting',?,?,?)",
            (queue_no, visitor_id, contact_name, boat_type_id, party,
             adults, children, now, amount, deposit),
        )
        tid = cur.lastrowid
        payments.charge(amount, f"ticket-{tid}", "ticket")
        payments.hold(deposit, f"hold-{tid}")
        conn.execute(
            "INSERT INTO payments(ticket_id, kind, amount, idem_key, provider_ref,"
            " status, created_at) VALUES (?,?,?,?,?, 'ok', ?)",
            (tid, "ticket", amount, f"ticket-{tid}",
             payments._ref(f"ticket-{tid}"), now))
        conn.execute(
            "INSERT INTO payments(ticket_id, kind, amount, idem_key, provider_ref,"
            " status, created_at) VALUES (?,?,?,?,?, 'ok', ?)",
            (tid, "hold", deposit, f"hold-{tid}",
             payments._ref(f"hold-{tid}"), now))
        return {"ticket_id": tid, "queue_no": queue_no,
                "amount": amount, "deposit": deposit}


def queue_board() -> dict:
    """候船叫号屏数据:各船型候船队列 + 预计等待分钟。"""
    now = time.time()
    expire_called(now=now)
    out = {"types": [], "called": [], "broadcasts": []}
    for bt in _all("SELECT * FROM boat_types ORDER BY id"):
        q = _all(
            "SELECT * FROM tickets WHERE boat_type_id = ? AND status IN ('waiting','called')"
            " ORDER BY COALESCE(requeued_at, created_at)",
            (bt["id"],))
        boat_n = _row("SELECT COUNT(*) c FROM boats WHERE boat_type_id = ?",
                      (bt["id"],))["c"]
        busy = _row(
            "SELECT COUNT(*) c FROM boats WHERE boat_type_id = ? AND status IN ('locked','sailing','inspection')",
            (bt["id"],))["c"]
        free_boats = max(0, boat_n - busy)
        wait_each = round(bt["duration_min"] / max(1, boat_n))
        items = []
        for i, t in enumerate(q):
            pos = i + 1
            est = max(0, (math.ceil(pos / max(1, free_boats or 1)) - 1)) * wait_each + wait_each
            items.append({
                "queue_no": t["queue_no"], "contact": t["contact_name"],
                "party_size": t["party_size"], "children": t["children"],
                "status": t["status"], "requeue_count": t["requeue_count"],
                "est_wait_min": est,
            })
        out["types"].append({"id": bt["id"], "name": bt["name"],
                             "capacity": bt["capacity"], "queue": items})
        for t in q:
            if t["status"] == "called":
                out["called"].append({
                    "queue_no": t["queue_no"], "type_name": bt["name"],
                    "remain_sec": max(0, int(t["call_deadline"] - now)),
                })
    out["broadcasts"] = [dict(r) for r in _all(
        "SELECT * FROM broadcasts ORDER BY id DESC LIMIT 5")]
    out["weather"] = dict(_row("SELECT * FROM weather_state WHERE id = 1"))
    return out


def waiting_tickets(boat_type_id: int | None = None) -> list[dict]:
    sql = ("SELECT t.*, bt.name type_name, bt.capacity FROM tickets t"
           " JOIN boat_types bt ON bt.id = t.boat_type_id"
           " WHERE t.status IN ('waiting','called','missed')"
           " ORDER BY COALESCE(t.requeued_at, t.created_at)")
    args = ()
    if boat_type_id:
        sql += " AND t.boat_type_id = ?"
        args = (boat_type_id,)
    return [dict(r) for r in _all(sql, args)]


def call_next(boat_type_id: int) -> dict:
    """叫号:队首候船号进入短暂叫号窗口。"""
    with _lock:
        conn = get_conn()
        expire_called()
        t = conn.execute(
            "SELECT * FROM tickets WHERE boat_type_id = ? AND status = 'waiting'"
            " ORDER BY COALESCE(requeued_at, created_at) LIMIT 1",
            (boat_type_id,)).fetchone()
        if t is None:
            raise ServiceError("该船型暂无候船号码", "empty_queue")
        now = time.time()
        conn.execute(
            "UPDATE tickets SET status='called', called_at=?, call_deadline=?"
            " WHERE id=?", (now, now + CALL_WINDOW_SEC, t["id"]))
        return {"queue_no": t["queue_no"], "window_sec": CALL_WINDOW_SEC}


def expire_called(now: float | None = None) -> int:
    """叫号超时 -> 过号。"""
    now = now or time.time()
    with _lock:
        cur = get_conn().execute(
            "UPDATE tickets SET status='missed' WHERE status='called'"
            " AND call_deadline < ?", (now,))
        return cur.rowcount


def requeue(ticket_id: int) -> dict:
    """过号重排:回到队尾(以 requeued_at 重新排位),保留号码。"""
    with _lock:
        conn = get_conn()
        t = _row("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        if t is None:
            raise ServiceError("门票不存在", "not_found")
        if t["status"] != "missed":
            raise ServiceError("仅过号号码可以重排", "not_missed")
        now = time.time()
        conn.execute(
            "UPDATE tickets SET status='waiting', requeue_count = requeue_count + 1,"
            " requeued_at=?, called_at=NULL, call_deadline=NULL WHERE id=?",
            (now, ticket_id))
        return {"queue_no": t["queue_no"],
                "requeue_count": t["requeue_count"] + 1}


# ---------- 组批 ----------

def list_batches(statuses: tuple[str, ...] | None = None) -> list[dict]:
    sql = ("SELECT b.*, bt.name type_name, bt.capacity,"
           " (SELECT COUNT(*) FROM batch_tickets WHERE batch_id=b.id) ticket_groups"
           " FROM batches b JOIN boat_types bt ON bt.id = b.boat_type_id")
    if statuses:
        sql += f" WHERE b.status IN ({','.join('?' * len(statuses))})"
        rows = _all(sql, statuses)
    else:
        rows = _all(sql)
    return [dict(r) for r in rows]


def create_batch(boat_type_id: int) -> dict:
    """创建出航批次(组批开始)。停航期间禁止新建。"""
    with _lock:
        conn = get_conn()
        w = _row("SELECT status FROM weather_state WHERE id=1")
        if w and w["status"] == "storm":
            raise ServiceError("天气停航中,不能创建新的出航批次", "suspended")
        bt = _row("SELECT * FROM boat_types WHERE id=?", (boat_type_id,))
        if bt is None:
            raise ServiceError("船型不存在", "bad_type")
        seq = conn.execute("SELECT COUNT(*) c FROM batches").fetchone()["c"] + 1
        code = f"B{time.strftime('%Y%m%d')}-{seq:02d}"
        now = time.time()
        cur = conn.execute(
            "INSERT INTO batches(code, boat_type_id, status, party_count,"
            " created_at) VALUES (?,?,'forming',0,?)",
            (code, boat_type_id, now))
        return {"batch_id": cur.lastrowid, "code": code,
                "type_name": bt["name"], "capacity": bt["capacity"]}


def batch_detail(batch_id: int) -> dict:
    b = _row(
        "SELECT b.*, bt.name type_name, bt.capacity, bo.code boat_code, bo.name boat_name"
        " FROM batches b JOIN boat_types bt ON bt.id=b.boat_type_id"
        " LEFT JOIN boats bo ON bo.id=b.boat_id WHERE b.id=?",
        (batch_id,))
    if b is None:
        raise ServiceError("批次不存在", "not_found")
    d = dict(b)
    tickets = []
    for t in _all(
        "SELECT t.* FROM batch_tickets bt JOIN tickets t ON t.id=bt.ticket_id"
        " WHERE bt.batch_id=? ORDER BY t.id", (batch_id,)):
        td = dict(t)
        td["assignments"] = [dict(a) for a in _all(
            "SELECT a.*, s.label size_label FROM life_jacket_assignments a"
            " JOIN life_jacket_stock s ON s.size_code=a.size_code"
            " WHERE a.ticket_id=? ORDER BY person_idx", (t["id"],))]
        td["scanned"] = _row(
            "SELECT gate, scanned_at FROM gate_scans WHERE ticket_id=?",
            (t["id"],)) is not None
        tickets.append(td)
    d["tickets"] = tickets
    d["jacket_stock"] = [dict(r) for r in _all(
        "SELECT s.*, s.total - COUNT(h.id) free FROM life_jacket_stock s"
        " LEFT JOIN life_jacket_holds h ON h.size_code=s.size_code"
        " GROUP BY s.size_code ORDER BY s.size_code")]
    return d


def add_ticket_to_batch(batch_id: int, ticket_id: int) -> dict:
    """把候船号加入批次;船型必须一致,且不得突破载客上限(超载拦截)。"""
    with _lock:
        conn = get_conn()
        b = _row("SELECT * FROM batches WHERE id=?", (batch_id,))
        if b is None:
            raise ServiceError("批次不存在", "not_found")
        if b["status"] != "forming":
            raise ServiceError("该批次已锁定,不能再加入", "batch_locked")
        t = _row("SELECT * FROM tickets WHERE id=?", (ticket_id,))
        if t is None:
            raise ServiceError("门票不存在", "not_found")
        if t["status"] not in ("waiting", "called", "missed"):
            raise ServiceError(f"号码 {t['queue_no']} 当前状态不可组批", "bad_state")
        if t["boat_type_id"] != b["boat_type_id"]:
            raise ServiceError("船型不一致,不能混入其他船型批次", "type_mismatch")
        exists = _row("SELECT 1 FROM batch_tickets WHERE batch_id=? AND ticket_id=?",
                      (batch_id, ticket_id))
        if exists:
            return {"batch_id": batch_id, "party_count": b["party_count"]}
        count = _batch_party_count(conn, batch_id)
        cap = _row("SELECT capacity FROM boat_types WHERE id=?",
                   (b["boat_type_id"],))["capacity"]
        if count + t["party_size"] > cap:
            raise ServiceError(
                f"超载:批次已有 {count} 人,加入 {t['party_size']} 人将超过上限 {cap} 人",
                "over_capacity")
        conn.execute("INSERT INTO batch_tickets(batch_id, ticket_id) VALUES(?,?)",
                     (batch_id, ticket_id))
        conn.execute(
            "UPDATE tickets SET status='batched', batch_id=? WHERE id=?",
            (batch_id, ticket_id))
        conn.execute("UPDATE batches SET party_count=? WHERE id=?",
                     (count + t["party_size"], batch_id))
        return {"batch_id": batch_id, "party_count": count + t["party_size"]}


def remove_ticket_from_batch(batch_id: int, ticket_id: int) -> None:
    with _lock:
        conn = get_conn()
        b = _row("SELECT * FROM batches WHERE id=?", (batch_id,))
        if b is None or b["status"] != "forming":
            raise ServiceError("仅组批中的批次可移除", "bad_state")
        cur = conn.execute("DELETE FROM batch_tickets WHERE batch_id=? AND ticket_id=?",
                           (batch_id, ticket_id))
        if cur.rowcount:
            t = _row("SELECT party_size FROM tickets WHERE id=?", (ticket_id,))
            conn.execute(
                "UPDATE tickets SET status='waiting', batch_id=NULL WHERE id=?",
                (ticket_id,))
            conn.execute("UPDATE batches SET party_count=? WHERE id=?",
                         (max(0, b["party_count"] - t["party_size"]), batch_id))


def cancel_batch(batch_id: int) -> None:
    """停航中断等场景:解散批次,号码回到候船队列。"""
    with _lock:
        conn = get_conn()
        b = _row("SELECT * FROM batches WHERE id=?", (batch_id,))
        if b is None:
            raise ServiceError("批次不存在", "not_found")
        if b["status"] not in ("forming", "boarding"):
            raise ServiceError("已出航批次不能解散", "bad_state")
        now = time.time()
        for t in _all("SELECT ticket_id FROM batch_tickets WHERE batch_id=?",
                      (batch_id,)):
            conn.execute(
                "UPDATE tickets SET status='waiting', batch_id=NULL, requeued_at=?"
                " WHERE id=?", (now, t["ticket_id"]))
        if b["boat_id"]:
            conn.execute("UPDATE boats SET status='available' WHERE id=?",
                         (b["boat_id"],))
        conn.execute("UPDATE batches SET status='cancelled', closed_at=? WHERE id=?",
                     (now, batch_id))


# ---------- 锁船 / 换船 ----------

def lock_boat(batch_id: int, boat_id: int | None = None) -> dict:
    """登船前锁定船只;停航期间禁止。"""
    with _lock:
        conn = get_conn()
        b = _row("SELECT * FROM batches WHERE id=?", (batch_id,))
        if b is None:
            raise ServiceError("批次不存在", "not_found")
        if b["status"] != "forming":
            raise ServiceError("批次不是组批中状态", "bad_state")
        w = _row("SELECT status FROM weather_state WHERE id=1")
        if w["status"] == "storm":
            raise ServiceError("天气停航中,禁止锁定船只出航", "suspended")
        if _batch_party_count(conn, batch_id) == 0:
            raise ServiceError("空批次不能锁船", "empty_batch")
        if boat_id is None:
            boat = conn.execute(
                "SELECT * FROM boats WHERE boat_type_id=? AND status='available'"
                " ORDER BY code LIMIT 1", (b["boat_type_id"],)).fetchone()
        else:
            boat = _row("SELECT * FROM boats WHERE id=?", (boat_id,))
            if boat and boat["boat_type_id"] != b["boat_type_id"]:
                raise ServiceError("船只与批次船型不符", "type_mismatch")
        if boat is None:
            raise ServiceError("该船型暂无可分配船只", "no_boat")
        if boat["status"] != "available":
            raise ServiceError(f"船只 {boat['code']} 当前不可用({boat['status']})",
                               "boat_busy")
        now = time.time()
        conn.execute("UPDATE boats SET status='locked' WHERE id=?", (boat["id"],))
        conn.execute(
            "UPDATE batches SET boat_id=?, status='boarding', locked_at=? WHERE id=?",
            (boat["id"], now, batch_id))
        return {"boat_id": boat["id"], "boat_code": boat["code"],
                "boat_name": boat["name"]}


def switch_boat(batch_id: int, new_boat_id: int) -> dict:
    """临时换船:只能换同船型,且新船容量不得低于在船人数。"""
    with _lock:
        conn = get_conn()
        b = _row("SELECT * FROM batches WHERE id=?", (batch_id,))
        if b is None:
            raise ServiceError("批次不存在", "not_found")
        if b["status"] not in ("boarding", "sailing"):
            raise ServiceError("仅登船中/出航中的批次可以换船", "bad_state")
        nb = _row("SELECT * FROM boats WHERE id=?", (new_boat_id,))
        if nb is None:
            raise ServiceError("目标船只不存在", "not_found")
        if nb["id"] == b["boat_id"]:
            raise ServiceError("目标船只与当前船只相同", "same_boat")
        if nb["boat_type_id"] != b["boat_type_id"]:
            raise ServiceError("临时换船仅限同船型,船型不得变更", "type_mismatch")
        if nb["status"] != "available":
            raise ServiceError(f"目标船 {nb['code']} 不可用({nb['status']})",
                               "boat_busy")
        party = _batch_party_count(conn, batch_id)
        cap = _row("SELECT capacity FROM boat_types WHERE id=?",
                   (b["boat_type_id"],))["capacity"]
        if party > cap:
            # 同船型容量相同,此处理论上不会触发,但作为硬性兜底
            raise ServiceError(
                f"换船会突破人数上限:在船 {party} 人 > 上限 {cap} 人",
                "over_capacity")
        conn.execute("UPDATE boats SET status='available' WHERE id=?",
                     (b["boat_id"],))
        conn.execute("UPDATE boats SET status=? WHERE id=?",
                     (b["status"], new_boat_id))
        conn.execute(
            "UPDATE batches SET boat_id=?, notes=COALESCE(notes,'')||?"
            " WHERE id=?",
            (new_boat_id, f"[{time.strftime('%H:%M')} 临时换船 {nb['code']}]",
             batch_id))
        return {"boat_id": nb["id"], "boat_code": nb["code"]}


# ---------- 救生衣逐人核验 ----------

def assign_jacket(batch_id: int, ticket_id: int, person_idx: int,
                  person_name: str, size_code: str) -> dict:
    """登船前逐人确认救生衣尺码。儿童尺码不足时明确报错。"""
    with _lock:
        conn = get_conn()
        b = _row("SELECT * FROM batches WHERE id=?", (batch_id,))
        if b is None:
            raise ServiceError("批次不存在", "not_found")
        if b["status"] != "boarding":
            raise ServiceError("请先锁定船只,再逐人核验救生衣", "not_boarding")
        t = _row("SELECT * FROM tickets WHERE id=?", (ticket_id,))
        if t is None or t["batch_id"] != batch_id:
            raise ServiceError("该号码不属于此批次", "bad_ticket")
        if not (1 <= person_idx <= t["party_size"]):
            raise ServiceError("人员序号超出同行人数", "bad_idx")
        size = _row("SELECT * FROM life_jacket_stock WHERE size_code=?",
                    (size_code,))
        if size is None:
            raise ServiceError("尺码不存在", "bad_size")
        old = _row(
            "SELECT * FROM life_jacket_assignments WHERE ticket_id=? AND person_idx=?",
            (ticket_id, person_idx))
        if old is None and _jacket_available(size_code) <= 0:
            hint = ""
            if size_code in ("CXS", "CS"):
                hint = "儿童救生衣库存不足,请联系仓库调拨或为该团改签下一航次"
            raise ServiceError(
                f"{size['label']} 已无库存。{hint}".strip(), "jacket_shortage")
        now = time.time()
        if old is not None:
            conn.execute("DELETE FROM life_jacket_holds WHERE ref_type='assignment'"
                         " AND ref_id=?", (old["id"],))
            conn.execute(
                "UPDATE life_jacket_assignments SET size_code=?, person_name=?,"
                " assigned_at=? WHERE id=?",
                (size_code, person_name, now, old["id"]))
            aid = old["id"]
        else:
            cur = conn.execute(
                "INSERT INTO life_jacket_assignments(batch_id, ticket_id, person_idx,"
                " person_name, size_code, assigned_at) VALUES(?,?,?,?,?,?)",
                (batch_id, ticket_id, person_idx, person_name, size_code, now))
            aid = cur.lastrowid
        conn.execute(
            "INSERT INTO life_jacket_holds(size_code, ref_type, ref_id, created_at)"
            " VALUES(?,'assignment',?,?)", (size_code, aid, now))
        return {"assignment_id": aid, "size_code": size_code,
                "free": _jacket_available(size_code)}


def _ticket_jackets_complete(conn, ticket_id: int) -> bool:
    t = conn.execute("SELECT party_size FROM tickets WHERE id=?",
                     (ticket_id,)).fetchone()
    n = conn.execute(
        "SELECT COUNT(*) c FROM life_jacket_assignments WHERE ticket_id=?",
        (ticket_id,)).fetchone()["c"]
    return n >= t["party_size"]


# ---------- 闸口放行(双闸口只能放行一次) ----------

def scan_gate(ticket_id: int, gate: str) -> dict:
    """闸口扫描候船号。gate_scans.ticket_id 唯一约束保证只放行一次。"""
    with _lock:
        conn = get_conn()
        t = _row("SELECT * FROM tickets WHERE id=?", (ticket_id,))
        if t is None:
            raise ServiceError("候船号不存在", "not_found")
        # 唯一约束兜底:并发情况下第二个 INSERT 必失败
        try:
            now = time.time()
            conn.execute(
                "INSERT INTO gate_scans(ticket_id, gate, scanned_at) VALUES(?,?,?)",
                (ticket_id, gate, now))
        except sqlite3.IntegrityError:
            first = _row("SELECT * FROM gate_scans WHERE ticket_id=?", (ticket_id,))
            return {"allowed": False, "queue_no": t["queue_no"],
                    "reason": f"该号码已在 {first['gate']} 放行,禁止重复过闸",
                    "first_gate": first["gate"]}
        # 插入成功后做业务校验;不满足则回滚放行记录
        b = _row("SELECT * FROM batches WHERE id=?", (t["batch_id"],))
        problems = []
        if t["status"] not in ("batched", "released"):
            problems.append("号码未完成组批")
        if b is None or b["status"] != "boarding":
            problems.append("船只未处于登船状态")
        elif not _ticket_jackets_complete(conn, ticket_id):
            problems.append("救生衣未逐人核验齐全")
        if problems:
            conn.execute("DELETE FROM gate_scans WHERE ticket_id=?", (ticket_id,))
            raise ServiceError(";".join(problems), "not_ready")
        conn.execute("UPDATE tickets SET status='released' WHERE id=?", (ticket_id,))
        return {"allowed": True, "queue_no": t["queue_no"], "gate": gate}


# ---------- 出航 / 湖面计时 ----------

def depart(batch_id: int) -> dict:
    """出航:全员放行且救生衣齐备;停航期间禁止出航。记录预计回港时间。"""
    with _lock:
        conn = get_conn()
        b = _row("SELECT * FROM batches WHERE id=?", (batch_id,))
        if b is None:
            raise ServiceError("批次不存在", "not_found")
        if b["status"] != "boarding":
            raise ServiceError("批次不在登船状态,不能出航", "bad_state")
        w = _row("SELECT status FROM weather_state WHERE id=1")
        if w["status"] == "storm":
            raise ServiceError("天气停航中,本批次暂停出航,请候令复航", "suspended")
        tickets = _all("SELECT * FROM batch_tickets WHERE batch_id=?", (batch_id,))
        if not tickets:
            raise ServiceError("空批次不能出航", "empty_batch")
        for bt in tickets:
            t = _row("SELECT * FROM tickets WHERE id=?", (bt["ticket_id"],))
            if t["status"] != "released":
                raise ServiceError(f"号码 {t['queue_no']} 尚未过闸放行", "not_released")
            if not _ticket_jackets_complete(conn, t["id"]):
                raise ServiceError(f"号码 {t['queue_no']} 救生衣核验不齐", "jacket_short")
        bt_row = _row("SELECT * FROM boat_types WHERE id=?", (b["boat_type_id"],))
        now = time.time()
        expected = now + bt_row["duration_min"] * 60
        conn.execute(
            "UPDATE batches SET status='sailing', sailed_at=?, expected_return_at=?"
            " WHERE id=?", (now, expected, batch_id))
        conn.execute("UPDATE boats SET status='sailing' WHERE id=?", (b["boat_id"],))
        conn.execute("UPDATE tickets SET status='sailing' WHERE batch_id=?",
                     (batch_id,))
        return {"sailed_at": now, "expected_return_at": expected}


def lake_view(now: float | None = None) -> dict:
    """湖面计时页:低密度,只列在航批次与超时状态。"""
    now = now or time.time()
    items = []
    for b in _all(
        "SELECT b.*, bt.name type_name, bo.code boat_code, bo.name boat_name"
        " FROM batches b JOIN boat_types bt ON bt.id=b.boat_type_id"
        " LEFT JOIN boats bo ON bo.id=b.boat_id"
        " WHERE b.status IN ('sailing','returned') ORDER BY b.sailed_at"):
        overdue = b["expected_return_at"] < now and b["status"] == "sailing"
        late_sec = max(0, int(now - b["expected_return_at"])) if overdue else 0
        alert = _row("SELECT * FROM overdue_alerts WHERE batch_id=?", (b["id"],))
        items.append({
            "id": b["id"], "code": b["code"], "boat_code": b["boat_code"],
            "boat_name": b["boat_name"], "type_name": b["type_name"],
            "party_count": b["party_count"], "status": b["status"],
            "sailed_at": b["sailed_at"], "expected_return_at": b["expected_return_at"],
            "elapsed_min": int((now - b["sailed_at"]) / 60),
            "late_min": late_sec // 60, "overdue": overdue,
            "alerted": alert is not None,
        })
    weather = dict(_row("SELECT * FROM weather_state WHERE id=1"))
    return {"now": now, "weather": weather, "trips": items}


def check_overdue(now: float | None = None) -> list[int]:
    """后台任务:扫描超时未归批次,每批次只告警一次并广播。"""
    now = now or time.time()
    fired = []
    with _lock:
        conn = get_conn()
        rows = _all(
            "SELECT * FROM batches WHERE status='sailing' AND expected_return_at < ?",
            (now,))
        for b in rows:
            exists = _row("SELECT 1 FROM overdue_alerts WHERE batch_id=?", (b["id"],))
            if exists:
                continue
            late = int((now - b["expected_return_at"]) / 60)
            msg = f"超时提醒:批次 {b['code']} 已超出预计回港时间 {late} 分钟,请立即联系湖面"
            conn.execute(
                "INSERT INTO overdue_alerts(batch_id, fired_at, message) VALUES(?,?,?)",
                (b["id"], now, msg))
            conn.execute(
                "INSERT INTO broadcasts(level, message, created_at) VALUES('warning',?,?)",
                (msg, now))
            fired.append(b["id"])
    return fired


# ---------- 回港检查 ----------

def _require_sailing_returned(b) -> None:
    if b["status"] not in ("sailing", "returned"):
        raise ServiceError("批次未出航,不能做回港检查", "bad_state")


def create_inspection(batch_id: int, hull_result: str, inspector: str,
                      note: str, photo_paths: list[str],
                      missing_assignment_ids: list[int] | None = None) -> dict:
    """回港检查:登记船体结果、救生衣缺失、保存照片。完成后订单方可结算。"""
    if hull_result not in HULL_FEE:
        raise ServiceError("船体检查结果非法", "bad_result")
    if not inspector.strip():
        raise ServiceError("请填写检查员", "bad_inspector")
    with _lock:
        conn = get_conn()
        b = _row("SELECT * FROM batches WHERE id=?", (batch_id,))
        if b is None:
            raise ServiceError("批次不存在", "not_found")
        _require_sailing_returned(b)
        if _row("SELECT id FROM return_inspections WHERE batch_id=?", (batch_id,)):
            raise ServiceError("该批次已完成回港检查", "dup_inspection")
        missing_assignment_ids = missing_assignment_ids or []
        now = time.time()
        cur = conn.execute(
            "INSERT INTO return_inspections(batch_id, hull_result, jackets_ok,"
            " note, inspector, inspected_at) VALUES(?,?,?,?,?,?)",
            (batch_id, hull_result, 0 if missing_assignment_ids else 1,
             note, inspector, now))
        insp_id = cur.lastrowid
        for p in photo_paths:
            conn.execute(
                "INSERT INTO inspection_photos(inspection_id, path, uploaded_at)"
                " VALUES(?,?,?)", (insp_id, p, now))
        # 救生衣归还:全部归还;勾选项按缺失处理(计费),均释放库存占用
        assigns = _all(
            "SELECT id FROM life_jacket_assignments WHERE batch_id=?", (batch_id,))
        misset = set(missing_assignment_ids)
        for a in assigns:
            status = "missing" if a["id"] in misset else "returned"
            conn.execute(
                "UPDATE life_jacket_assignments SET status=?, returned_at=? WHERE id=?",
                (status, now, a["id"]))
            conn.execute("DELETE FROM life_jacket_holds WHERE ref_type='assignment'"
                         " AND ref_id=?", (a["id"],))
        conn.execute(
            "UPDATE batches SET status='returned', actual_return_at=? WHERE id=?",
            (now, batch_id))
        conn.execute("UPDATE tickets SET status='returned' WHERE batch_id=?",
                     (batch_id,))
        # 严重损伤船只送修;轻微刮碰登记后仍可运营(由押金承担费用)
        boat_status = "repair" if hull_result == "serious" else "inspection"
        conn.execute("UPDATE boats SET status=? WHERE id=?",
                     (boat_status, b["boat_id"]))
        return {"inspection_id": insp_id, "hull_result": hull_result}


def save_photo(filename: str, data: bytes) -> str:
    """船体照片保存到本地目录,返回相对路径。"""
    safe = f"{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}_{filename}"
    safe = safe.replace("/", "_").replace("\\", "_")
    path = os.path.join(PHOTO_DIR, safe)
    with open(path, "wb") as f:
        f.write(data)
    return f"photos/{safe}"


def make_demo_photo(batch_id: int) -> str:
    """无上传文件时生成一张本地占位照片(SVG),保证样例流程可走通。"""
    os.makedirs(PHOTO_DIR, exist_ok=True)
    name = f"{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}_hull.svg"
    path = os.path.join(PHOTO_DIR, name)
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="300">'
        '<rect width="480" height="300" fill="#e8f4f6"/>'
        '<path d="M40 180 Q240 240 440 180 L400 220 Q240 260 80 220 Z" fill="#1f8a96"/>'
        f'<text x="240" y="120" font-size="26" text-anchor="middle" fill="#2b2f33">批次 {batch_id} 船体检查照片</text>'
        '<text x="240" y="280" font-size="14" text-anchor="middle" fill="#666">模拟拍照占位图</text>'
        '</svg>')
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)
    return f"photos/{name}"


# ---------- 押金复核(幂等) ----------

def inspection_detail(batch_id: int) -> dict:
    b = _row(
        "SELECT b.*, bt.name type_name, bo.code boat_code,"
        " i.id inspection_id, i.hull_result, i.jackets_ok, i.note, i.inspector,"
        " i.inspected_at FROM batches b JOIN boat_types bt ON bt.id=b.boat_type_id"
        " LEFT JOIN boats bo ON bo.id=b.boat_id"
        " LEFT JOIN return_inspections i ON i.batch_id=b.id WHERE b.id=?",
        (batch_id,))
    if b is None:
        raise ServiceError("批次不存在", "not_found")
    d = dict(b)
    if d.get("inspection_id"):
        d["photos"] = [dict(r) for r in _all(
            "SELECT * FROM inspection_photos WHERE inspection_id=?",
            (d["inspection_id"],))]
        d["missing_fees"] = [dict(r) for r in _all(
            "SELECT a.id assignment_id, a.person_name, a.size_code, t.queue_no"
            " FROM life_jacket_assignments a JOIN tickets t ON t.id=a.ticket_id"
            " WHERE a.batch_id=? AND a.status='missing'", (batch_id,))]
    tickets = []
    for t in _all(
        "SELECT t.* FROM batch_tickets bt JOIN tickets t ON t.id=bt.ticket_id"
        " WHERE bt.batch_id=? ORDER BY t.id", (batch_id,)):
        tickets.append(dict(t))
    d["tickets"] = tickets
    return d


def settle_ticket_deposit(ticket_id: int) -> dict:
    """押金结算:必须先有回港检查;按 船体刮碰/救生衣缺失/超时 计扣,余额退还。

    幂等:扣款与退款流水以 ticket+inspection 为幂等键,重复提交直接返回
    既有结果,绝不重复扣款。
    """
    with _lock:
        conn = get_conn()
        t = _row("SELECT * FROM tickets WHERE id=?", (ticket_id,))
        if t is None:
            raise ServiceError("门票不存在", "not_found")
        if t["status"] not in ("returned", "settled"):
            raise ServiceError("订单尚未完成回港检查,不能结算押金", "not_returned")
        b = _row("SELECT * FROM batches WHERE id=?", (t["batch_id"],))
        insp = _row("SELECT * FROM return_inspections WHERE batch_id=?",
                    (b["id"],))
        if insp is None:
            # 硬性要求:押金扣除必须关联回港检查
            raise ServiceError("缺少回港检查记录,押金不得结算", "no_inspection")
        deduct_key = f"deduct-{ticket_id}-{insp['id']}"
        refund_key = f"refund-{ticket_id}-{insp['id']}"
        prior = _row("SELECT * FROM payments WHERE idem_key=?", (deduct_key,))
        if prior is not None or t["status"] == "settled":
            return {"idempotent": True, "ticket_id": ticket_id,
                    "queue_no": t["queue_no"],
                    "deducted": t["deposit_deducted"],
                    "refunded": t["deposit_refunded"]}

        # ---- 计算应扣金额 ----
        party_total = max(1, b["party_count"])
        hull_share = round(
            HULL_FEE[insp["hull_result"]] * t["party_size"] / party_total, 2)
        missing_n = _row(
            "SELECT COUNT(*) c FROM life_jacket_assignments"
            " WHERE ticket_id=? AND status='missing'", (ticket_id,))["c"]
        jacket_fee = JACKET_MISSING_FEE * missing_n
        late_min = 0
        if b["actual_return_at"] and b["expected_return_at"]:
            late_min = max(0, math.ceil(
                (b["actual_return_at"] - b["expected_return_at"]) / 60))
        overdue_fee = round(late_min * OVERDUE_PER_MIN_PER_PERSON * t["party_size"], 2)
        raw_deduct = round(hull_share + jacket_fee + overdue_fee, 2)
        deduct = round(min(t["deposit_total"], raw_deduct), 2)
        refund = round(t["deposit_total"] - deduct, 2)
        now = time.time()

        # ---- 落流水(唯一约束兜底幂等) ----
        if deduct > 0:
            r = payments.charge(deduct, deduct_key, "deduct")
            try:
                conn.execute(
                    "INSERT INTO payments(ticket_id, kind, amount, idem_key,"
                    " provider_ref, status, related_inspection_id, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (ticket_id, "deduct", deduct, deduct_key, r["provider_ref"],
                     "ok", insp["id"], now))
            except sqlite3.IntegrityError:
                # 并发重复提交:另一线程已扣,返回其结果,不再扣款
                pass
        if refund > 0:
            r = payments.refund(refund, refund_key)
            try:
                conn.execute(
                    "INSERT INTO payments(ticket_id, kind, amount, idem_key,"
                    " provider_ref, status, related_inspection_id, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (ticket_id, "refund", refund, refund_key, r["provider_ref"],
                     "ok", insp["id"], now))
            except sqlite3.IntegrityError:
                pass
        conn.execute(
            "UPDATE tickets SET deposit_deducted=?, deposit_refunded=?,"
            " status='settled', settle_inspection_id=? WHERE id=?",
            (deduct, refund, insp["id"], ticket_id))

        # 批次全部结算 -> 关闭,船只恢复可用(严重损伤已在检查时送修)
        unsettled = _row(
            "SELECT COUNT(*) c FROM batch_tickets bt JOIN tickets t ON t.id=bt.ticket_id"
            " WHERE bt.batch_id=? AND t.status!='settled'", (b["id"],))["c"]
        if unsettled == 0:
            conn.execute(
                "UPDATE batches SET status='closed', closed_at=? WHERE id=?",
                (now, b["id"]))
            boat = _row("SELECT * FROM boats WHERE id=?", (b["boat_id"],))
            if boat and boat["status"] == "inspection":
                conn.execute("UPDATE boats SET status='available' WHERE id=?",
                             (b["boat_id"],))
        return {"idempotent": False, "ticket_id": ticket_id,
                "queue_no": t["queue_no"], "hull_share": hull_share,
                "jacket_fee": jacket_fee, "overdue_fee": overdue_fee,
                "late_min": late_min, "deducted": deduct, "refunded": refund}


# ---------- 天气 / 停航 ----------

def current_weather() -> dict:
    return dict(_row("SELECT * FROM weather_state WHERE id=1"))


def set_weather(status: str, message: str | None = None,
                source: str = "manual") -> dict:
    """天气突变处理:更新天气并广播;storm 停航,期间禁止新建批次/出航。"""
    from .adapters import WEATHER_MESSAGES
    if status not in WEATHER_MESSAGES:
        raise ServiceError("天气状态非法", "bad_weather")
    with _lock:
        conn = get_conn()
        old = _row("SELECT * FROM weather_state WHERE id=1")
        message = message or WEATHER_MESSAGES[status]
        now = time.time()
        conn.execute(
            "UPDATE weather_state SET status=?, message=?, changed_at=? WHERE id=1",
            (status, message, now))
        if status == "storm":
            level, text = "stop", f"【停航通知】{message}。所有新批次暂停,在航船只请即刻回港"
        elif status == "windy":
            level, text = "warning", f"【大风预警】{message},谨慎出航"
        else:
            if old and old["status"] == "storm":
                level, text = "info", "【复航通知】天气好转,恢复售票与出航"
            else:
                level, text = "info", f"天气更新:{message}"
        conn.execute(
            "INSERT INTO broadcasts(level, message, created_at) VALUES(?,?,?)",
            (level, f"[{source}] {text}", now))
        return {"status": status, "message": message}


def list_broadcasts(limit: int = 10) -> list[dict]:
    return [dict(r) for r in _all(
        "SELECT * FROM broadcasts ORDER BY id DESC LIMIT ?", (limit,))]


def payment_history(ticket_id: int | None = None) -> list[dict]:
    sql = ("SELECT p.*, t.queue_no FROM payments p JOIN tickets t ON t.id=p.ticket_id")
    if ticket_id:
        return [dict(r) for r in _all(sql + " WHERE p.ticket_id=? ORDER BY p.id",
                                      (ticket_id,))]
    return [dict(r) for r in _all(sql + " ORDER BY p.id DESC LIMIT 50")]
