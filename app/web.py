"""HTTP 服务与页面(仅用标准库)。

页面:
  /board      候船叫号屏
  /workbench  组批工作台
  /boarding   登船核验(救生衣逐人 + 双闸口)
  /lake       湖面计时
  /return     回港检查(船体照片)
  /deposit    押金复核
另含售票表单与天气控制。
"""
from __future__ import annotations

import html
import json
import os
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import services
from .db import PHOTO_DIR, get_conn, init_db

# ---------- 配色:湖水青 / 救生衣橙 / 深灰字 ----------
TEAL = "#1f8a96"
TEAL_DARK = "#166b75"
TEAL_SOFT = "#e8f4f6"
ORANGE = "#f26a13"
ORANGE_SOFT = "#fff0e4"
INK = "#2b2f33"
GREY = "#666b70"

CSS = f"""
* {{ box-sizing: border-box; }}
body {{ margin:0; font-family: "PingFang SC","Microsoft YaHei",system-ui,sans-serif;
       color:{INK}; background:#f4f8f9; }}
header.top {{ background:{TEAL}; color:#fff; padding:10px 22px; display:flex;
       align-items:center; gap:22px; flex-wrap:wrap; }}
header.top a {{ color:#eafafa; text-decoration:none; font-size:15px; }}
header.top a:hover, header.top a.active {{ color:#fff; font-weight:700; }}
.brand {{ font-size:19px; font-weight:800; letter-spacing:1px; }}
.weather {{ margin-left:auto; font-size:14px; padding:4px 12px; border-radius:14px;
       background:rgba(255,255,255,.18); }}
.weather.storm {{ background:{ORANGE}; }}
.wrap {{ max-width:1180px; margin:18px auto; padding:0 18px; }}
.grid {{ display:grid; gap:16px; }}
.cols-2 {{ grid-template-columns: 1fr 1fr; }}
@media (max-width:900px) {{ .cols-2 {{ grid-template-columns:1fr; }} }}
.card {{ background:#fff; border:1px solid #d8e6e8; border-radius:12px; padding:16px 18px;
       box-shadow:0 1px 3px rgba(20,60,70,.06); }}
.card h2 {{ margin:0 0 12px; font-size:17px; color:{TEAL_DARK}; }}
table {{ width:100%; border-collapse:collapse; }}
th,td {{ padding:10px 12px; text-align:left; border-bottom:1px solid #e6eff0;
       font-size:14px; white-space:nowrap; }}
th {{ background:{TEAL_SOFT}; color:{TEAL_DARK}; font-weight:700; }}
tr:hover td {{ background:#fafdfd; }}
.btn {{ display:inline-block; border:none; cursor:pointer; border-radius:8px;
       padding:10px 18px; font-size:15px; font-weight:700; background:{TEAL};
       color:#fff; text-decoration:none; }}
.btn:hover {{ background:{TEAL_DARK}; }}
.btn.big {{ font-size:18px; padding:14px 30px; border-radius:10px; }}
.btn.orange {{ background:{ORANGE}; }}
.btn.orange:hover {{ background:#d45a0c; }}
.btn.ghost {{ background:#fff; color:{TEAL_DARK}; border:1.5px solid {TEAL}; }}
.btn.small {{ font-size:13px; padding:6px 12px; }}
.btn.danger {{ background:#b4443a; }}
.btn:disabled {{ background:#b9c6c8; cursor:not-allowed; }}
input,select,textarea {{ border:1.5px solid #b9d3d6; border-radius:8px; padding:8px 10px;
       font-size:14px; font-family:inherit; background:#fff; color:{INK}; }}
input:focus,select:focus {{ outline:2px solid rgba(31,138,150,.35); border-color:{TEAL}; }}
label.fld {{ display:flex; flex-direction:column; gap:5px; font-size:13px; color:{GREY}; }}
.flash {{ padding:10px 14px; border-radius:8px; margin-bottom:14px; font-size:14px; }}
.flash.ok {{ background:{TEAL_SOFT}; color:{TEAL_DARK}; border:1px solid #bfe0e3; }}
.flash.err {{ background:{ORANGE_SOFT}; color:#a4440c; border:1px solid #f6c8a3; }}
.tag {{ display:inline-block; padding:2px 10px; border-radius:12px; font-size:12px;
       font-weight:700; }}
.tag.waiting {{ background:{TEAL_SOFT}; color:{TEAL_DARK}; }}
.tag.called {{ background:{ORANGE_SOFT}; color:{ORANGE}; }}
.tag.missed {{ background:#eee; color:{GREY}; }}
.tag.boarding {{ background:#e4f0ff; color:#2459a6; }}
.tag.sailing {{ background:#e2f6e6; color:#1f7a3c; }}
.tag.overdue {{ background:#fde7e7; color:#b3261e; }}
.tag.returned,.tag.closed {{ background:#ece9ff; color:#5142b0; }}
.tag.cancelled {{ background:#eee; color:#999; }}
/* 候船大屏 */
.board-screen {{ background:linear-gradient(160deg,#0f5f69,#1f8a96 55%,#2aa0ad);
       min-height:calc(100vh - 52px); color:#fff; padding:26px 30px; }}
.board-screen .big-no {{ font-size:64px; font-weight:900; line-height:1;
       letter-spacing:3px; color:#ffd9bd; text-shadow:0 3px 10px rgba(0,0,0,.25); }}
.board-row {{ display:flex; align-items:center; gap:26px; padding:20px 26px;
       border-bottom:1px solid rgba(255,255,255,.15); }}
.board-row .tn {{ font-size:30px; font-weight:800; min-width:150px; }}
.board-row .ty {{ font-size:20px; opacity:.92; min-width:110px; }}
.board-row .wt {{ margin-left:auto; font-size:22px; font-weight:700; }}
.called-banner {{ background:{ORANGE}; border-radius:14px; padding:16px 24px;
       font-size:24px; font-weight:800; margin-bottom:18px; }}
/* 湖面计时:低密度 */
.lake-card {{ padding:22px 26px; margin-bottom:14px; border-radius:14px;
       border:2px solid #d8e6e8; display:flex; align-items:baseline; gap:26px; }}
.lake-card .code {{ font-size:26px; font-weight:800; color:{TEAL_DARK}; }}
.lake-card .meta {{ color:{GREY}; font-size:15px; }}
.lake-card .clock {{ margin-left:auto; font-size:18px; font-weight:700; }}
/* 超时:边框脉冲(仅边框/阴影,不做大面积闪烁) */
@keyframes borderPulse {{
  0%,100% {{ border-color:#b3261e; box-shadow:0 0 0 0 rgba(179,38,30,.35); }}
  50% {{ border-color:#e2574c; box-shadow:0 0 0 6px rgba(179,38,30,0); }}
}}
.overdue-pulse {{ animation:borderPulse 1.4s ease-in-out infinite; }}
.overdue-pulse .clock {{ color:#b3261e; }}
.muted {{ color:{GREY}; font-size:13px; }}
.money {{ font-weight:800; color:{ORANGE}; }}
.inline {{ display:flex; gap:8px; align-items:center; }}
.right {{ text-align:right; }}
.person-row {{ display:flex; gap:8px; align-items:center; margin:6px 0;
       padding:8px 10px; background:#f7fbfb; border-radius:8px; }}
.person-row .idx {{ font-weight:800; color:{TEAL_DARK}; min-width:48px; }}
.gatebtns {{ display:flex; gap:10px; }}
"""


def esc(v) -> str:
    return html.escape("" if v is None else str(v))


def fmt_time(ts) -> str:
    if not ts:
        return "—"
    return time.strftime("%H:%M", time.localtime(float(ts)))


def fmt_dt(ts) -> str:
    if not ts:
        return "—"
    return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))


def status_tag(status: str) -> str:
    labels = {"waiting": "候船", "called": "叫号中", "missed": "过号",
              "batched": "已组批", "released": "已放行", "boarding": "登船中",
              "sailing": "出航中", "returned": "已回港", "inspected": "已检查",
              "settled": "已结算", "closed": "已关闭", "forming": "组批中",
              "cancelled": "已取消"}
    return f'<span class="tag {esc(status)}">{labels.get(status, status)}</span>'


PAGE_HEAD = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · 湖湾公园游船平台</title><style>{css}</style>{refresh}</head>"""


def layout(title: str, body: str, active: str = "", refresh: int = 0,
           full_bleed: bool = False) -> bytes:
    w = services.current_weather()
    wlabel = {"fine": "适航", "windy": "大风预警", "storm": "停航"}[w["status"]]
    nav = [
        ("/board", "候船叫号屏", "board"),
        ("/workbench", "组批工作台", "workbench"),
        ("/boarding", "登船核验", "boarding"),
        ("/lake", "湖面计时", "lake"),
        ("/return", "回港检查", "return"),
        ("/deposit", "押金复核", "deposit"),
    ]
    links = "".join(
        f'<a href="{u}" class="{"active" if k == active else ""}">{n}</a>'
        for u, n, k in nav)
    head_top = (f'<header class="top"><span class="brand">🚣 湖湾公园游船平台</span>'
                f'{links}'
                f'<span class="weather {"storm" if w["status"]=="storm" else ""}">'
                f'天气:{esc(wlabel)} · {esc(w["message"])}</span></header>')
    if full_bleed:
        content = body
    else:
        content = f'<div class="wrap">{body}</div>'
    page = (PAGE_HEAD.format(title=esc(title), css=CSS,
                             refresh=f'<meta http-equiv="refresh" content="{refresh}">'
                             if refresh else "")
            + f"<body>{head_top}{content}</body></html>")
    return page.encode("utf-8")


def flash(req_args) -> str:
    msg, err = req_args.get("msg"), req_args.get("err")
    if msg:
        return f'<div class="flash ok">{esc(msg)}</div>'
    if err:
        return f'<div class="flash err">{esc(err)}</div>'
    return ""


# ---------- 极简 multipart 解析(回港检查上传船体照片) ----------

def parse_multipart(body: bytes, content_type: str):
    fields: dict[str, list[str]] = {}
    files: dict[str, tuple[str, bytes]] = {}
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            boundary = part[len("boundary="):].strip('"')
    if not boundary:
        return fields, files
    delim = b"--" + boundary.encode()
    for chunk in body.split(delim):
        if not chunk or chunk in (b"--", b"--\r\n", b"\r\n"):
            continue
        if b"\r\n\r\n" not in chunk:
            continue
        head, _, data = chunk[2 if chunk.startswith(b"\r\n") else 0:].partition(b"\r\n\r\n")
        data = data.rstrip(b"\r\n")
        headers = head.decode("utf-8", "replace")
        name = None
        filename = None
        for hline in headers.split("\r\n"):
            if hline.lower().startswith("content-disposition"):
                for kv in hline.split(";"):
                    kv = kv.strip()
                    if kv.startswith("name="):
                        name = kv[5:].strip('"')
                    if kv.startswith("filename="):
                        filename = kv[9:].strip('"')
        if name is None:
            continue
        if filename is not None:
            if filename and data:
                files[name] = (filename, data)
        else:
            fields.setdefault(name, []).append(data.decode("utf-8", "replace"))
    return fields, files


# ---------- 页面 ----------

def page_home(args) -> bytes:
    conn = get_conn()
    types = conn.execute("SELECT * FROM boat_types ORDER BY id").fetchall()
    type_opts = "".join(
        f'<option value="{t["id"]}">{esc(t["name"])}（上限 {t["capacity"]} 人'
        f'／{int(t["hourly_rate"])}元/小时·人）</option>' for t in types)
    counts = {
        "waiting": conn.execute("SELECT COUNT(*) c FROM tickets WHERE status IN ('waiting','called')").fetchone()["c"],
        "sailing": conn.execute("SELECT COUNT(*) c FROM batches WHERE status='sailing'").fetchone()["c"],
        "forming": conn.execute("SELECT COUNT(*) c FROM batches WHERE status IN ('forming','boarding')").fetchone()["c"],
        "overdue": conn.execute("SELECT COUNT(*) c FROM overdue_alerts").fetchone()["c"],
    }
    weather_form = """
    <div class="card"><h2>天气控制（模拟突变 / 停航演练）</h2>
      <div class="inline">
      <form method="post" action="/weather"><input type="hidden" name="status" value="fine">
      <button class="btn ghost small">恢复适航</button></form>
      <form method="post" action="/weather"><input type="hidden" name="status" value="windy">
      <button class="btn ghost small">大风预警</button></form>
      <form method="post" action="/weather"><input type="hidden" name="status" value="storm">
      <button class="btn orange small">发布停航</button></form>
      </div><p class="muted">停航期间不能创建新批次、不能锁船出航；组批中的批次可就地解散。</p>
    </div>"""
    body = f"""
    <h1>游船调度总览</h1>
    <div class="grid cols-2">
      <div class="card"><h2>快速售票（演示数据）</h2>
        {flash(args)}
        <form method="post" action="/sell" class="grid" style="gap:10px">
          <label class="fld">联系人姓名<input name="contact_name" required placeholder="如：王芳" value="王芳"></label>
          <div class="grid cols-2" style="gap:10px">
            <label class="fld">成人<input type="number" name="adults" value="2" min="1"></label>
            <label class="fld">儿童<input type="number" name="children" value="1" min="0"></label>
          </div>
          <label class="fld">选择船型<select name="boat_type_id">{type_opts}</select></label>
          <button class="btn big">售票并入候船队列</button>
        </form>
      </div>
      <div class="grid" style="gap:16px">
        <div class="card"><h2>实时态势</h2>
          <table>
            <tr><td>候船号码</td><td class="right"><b>{counts["waiting"]}</b> 组</td></tr>
            <tr><td>组批中/登船中批次</td><td class="right"><b>{counts["forming"]}</b> 个</td></tr>
            <tr><td>湖面在航</td><td class="right"><b>{counts["sailing"]}</b> 艘</td></tr>
            <tr><td>累计超时提醒</td><td class="right"><b style="color:#b3261e">{counts["overdue"]}</b> 次</td></tr>
          </table>
          <p class="muted">游客资料与支付记录均为演示数据；支付结果由模拟适配器生成。</p>
        </div>
        {weather_form}
      </div>
    </div>"""
    return layout("总览", body)


def page_board(args) -> bytes:
    data = services.queue_board()
    called = ""
    if data["called"]:
        chips = "　".join(
            f'{esc(c["queue_no"])}（{esc(c["type_name"])}，剩余 {c["remain_sec"]} 秒）'
            for c in data["called"])
        called = f'<div class="called-banner">📢 请立即到登船口:{chips}</div>'
    columns = ""
    for t in data["types"]:
        rows = ""
        for i, item in enumerate(t["queue"][:8]):
            big = "big-no" if i == 0 and item["status"] == "waiting" else ""
            rows += (f'<div class="board-row"><span class="tn {big}">{esc(item["queue_no"])}</span>'
                     f'<span class="ty">{esc(t["name"])} · {item["party_size"]}人'
                     f'{"（含儿童"+str(item["children"])+"）" if item["children"] else ""}</span>'
                     f'<span>{status_tag(item["status"])}'
                     f'{"（过号重排第"+str(item["requeue_count"])+"次）" if item["requeue_count"] else ""}</span>'
                     f'<span class="wt">预计等待 ≈ {item["est_wait_min"]} 分钟</span></div>')
        if not rows:
            rows = '<div class="board-row muted">暂无候船号码</div>'
        columns += (f'<div class="card" style="background:rgba(255,255,255,.10);border-color:rgba(255,255,255,.2)">'
                    f'<h2 style="color:#fff">{esc(t["name"])} · 上限 {t["capacity"]} 人</h2>{rows}</div>')
    bcs = "".join(
        f'<div class="muted" style="color:#cfeef0">🔊 {esc(b["message"])} · {fmt_time(b["created_at"])}</div>'
        for b in data["broadcasts"][:3])
    body = f"""
    <div class="board-screen">
      {called}
      <div class="grid cols-2">{columns}</div>
      <div style="margin-top:18px">{bcs}</div>
      <p class="muted" style="color:#bfe0e3">本页每 5 秒自动刷新 · {time.strftime("%H:%M:%S")}</p>
    </div>"""
    return layout("候船叫号屏", body, active="board", refresh=5, full_bleed=True)


def page_workbench(args) -> bytes:
    conn = get_conn()
    types = conn.execute("SELECT * FROM boat_types ORDER BY id").fetchall()

    # 候船名单 + 叫号/重排
    type_sections = ""
    for t in types:
        services.expire_called()
        q = conn.execute(
            "SELECT * FROM tickets WHERE boat_type_id=? AND status IN ('waiting','called','missed')"
            " ORDER BY COALESCE(requeued_at, created_at)", (t["id"],)).fetchall()
        rows = ""
        for r in q:
            acts = ""
            if r["status"] == "missed":
                acts = (f'<form method="post" action="/queue/requeue" class="inline">'
                        f'<input type="hidden" name="ticket_id" value="{r["id"]}">'
                        f'<button class="btn small orange">过号重排</button></form>')
            batch_opts = ""
            for b in services.list_batches(("forming",)):
                if b["boat_type_id"] == t["id"]:
                    batch_opts += f'<option value="{b["id"]}">{esc(b["code"])}</option>'
            if batch_opts:
                acts += (f'<form method="post" action="/batch/add" class="inline">'
                         f'<input type="hidden" name="ticket_id" value="{r["id"]}">'
                         f'<select name="batch_id">{batch_opts}</select>'
                         f'<button class="btn small">加入批次</button></form>')
            rows += (f"<tr><td><b>{esc(r['queue_no'])}</b></td><td>{esc(r['contact_name'])}</td>"
                     f"<td>{r['party_size']}（{r['adults']}大{r['children']}小）</td>"
                     f"<td>{status_tag(r['status'])}</td>"
                     f"<td>{acts if acts else '<span class=muted>—</span>'}</td></tr>")
        if not rows:
            rows = '<tr><td colspan="5" class="muted">无候船号码</td></tr>'
        type_sections += f"""
        <div class="card">
          <h2>{esc(t["name"])}队列（上限 {t["capacity"]} 人）
            <form method="post" action="/queue/call" style="display:inline;float:right">
              <input type="hidden" name="boat_type_id" value="{t["id"]}">
              <button class="btn small ghost">叫下一号</button>
            </form>
          </h2>
          <table><thead><tr><th>候船号</th><th>联系人</th><th>人数</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>{rows}</tbody></table>
        </div>"""

    # 批次
    batch_cards = ""
    for b in services.list_batches(("forming", "boarding")):
        d = services.batch_detail(b["id"])
        cap = b["capacity"]
        pct = min(100, b["party_count"] / cap * 100)
        trows = ""
        for t in d["tickets"]:
            rm = ""
            if b["status"] == "forming":
                rm = (f'<form method="post" action="/batch/remove" class="inline">'
                      f'<input type="hidden" name="batch_id" value="{b["id"]}">'
                      f'<input type="hidden" name="ticket_id" value="{t["id"]}">'
                      f'<button class="btn small danger">移出</button></form>')
            trows += (f"<tr><td>{esc(t['queue_no'])}</td><td>{t['party_size']}</td>"
                      f"<td>{status_tag(t['status'])}</td><td>{rm}</td></tr>")
        lock = ""
        if b["status"] == "forming":
            boats = conn.execute(
                "SELECT * FROM boats WHERE boat_type_id=? AND status='available' ORDER BY code",
                (b["boat_type_id"],)).fetchall()
            opts = "".join(f'<option value="{x["id"]}">{esc(x["code"])} {esc(x["name"])}</option>'
                           for x in boats)
            lock = (f'<form method="post" action="/batch/lock" class="inline">'
                    f'<input type="hidden" name="batch_id" value="{b["id"]}">'
                    f'<select name="boat_id">{opts}</select>'
                    f'<button class="btn orange big">锁定船只</button></form>') if opts else \
                   '<span class="flash err" style="display:inline-block">无可用船只</span>'
        else:
            lock = (f'<a class="btn big" href="/boarding?id={b["id"]}">前往登船核验 →</a>'
                    f' <span class="muted">已锁定 {esc(d.get("boat_code") or "")}</span>')
        cancel = (f'<form method="post" action="/batch/cancel" style="display:inline" '
                  f'onsubmit="return confirm(\'解散批次?号码将回到队尾\')">'
                  f'<input type="hidden" name="batch_id" value="{b["id"]}">'
                  f'<button class="btn small danger">解散(停航中断)</button></form>')
        batch_cards += f"""
        <div class="card">
          <h2>{esc(b["code"])} · {esc(b["type_name"])} {status_tag(b["status"])}
            <span style="float:right">{cancel}</span></h2>
          <div class="inline" style="margin-bottom:8px">
            <div style="flex:1;background:#e6eff0;border-radius:8px;height:14px;overflow:hidden">
              <div style="width:{pct:.0f}%;height:100%;background:{ORANGE if b["party_count"]>=cap else TEAL}"></div>
            </div>
            <b>{b["party_count"]} / {cap} 人</b>
          </div>
          <table><thead><tr><th>号码</th><th>人数</th><th>状态</th><th></th></tr></thead>
          <tbody>{trows or '<tr><td colspan=4 class=muted>尚未加入号码</td></tr>'}</tbody></table>
          <div style="margin-top:12px">{lock}</div>
        </div>"""
    create_forms = "".join(
        f'<form method="post" action="/batch/create" style="display:inline">'
        f'<input type="hidden" name="boat_type_id" value="{t["id"]}">'
        f'<button class="btn ghost small">新建{esc(t["name"])}批次</button></form>'
        for t in types)
    body = f"""
    {flash(args)}
    <div class="grid cols-2">
      <div>{type_sections}</div>
      <div>
        <div class="card"><h2>出航批次</h2>
          <div class="inline" style="flex-wrap:wrap;gap:8px">{create_forms}</div>
          <p class="muted">按船型组批,加入号码时实时校验载客上限,超载直接拒绝。</p>
        </div>
        {batch_cards}
      </div>
    </div>"""
    return layout("组批工作台", body, active="workbench")


JACKET_SIZES = ["CXS", "CS", "S", "M", "L", "XL"]


def page_boarding(args) -> bytes:
    conn = get_conn()
    bid = args.get("id")
    if not bid:
        cards = ""
        for b in services.list_batches(("boarding", "forming")):
            cards += (f'<p><a class="btn ghost" href="/boarding?id={b["id"]}">'
                      f'{esc(b["code"])} · {esc(b["type_name"])} '
                      f'{b["party_count"]}人 {status_tag(b["status"])}</a></p>')
        return layout("登船核验", f'<div class="card"><h2>选择登船批次</h2>{cards or "<p class=muted>暂无批次</p>"}</div>',
                      active="boarding")
    d = services.batch_detail(int(bid))
    stock_line = "　".join(
        f'{esc(s["size_code"])}:<b style="color:{ORANGE if s["free"]==0 else TEAL_DARK}">{s["free"]}</b>/{s["total"]}'
        for s in d["jacket_stock"])
    tickets_html = ""
    for t in d["tickets"]:
        done = len(t["assignments"]) >= t["party_size"]
        assigned_ids = {a["person_idx"]: a for a in t["assignments"]}
        people = ""
        for i in range(1, t["party_size"] + 1):
            a = assigned_ids.get(i)
            default_size = "CS" if i <= t["children"] else "M"
            val = a["size_code"] if a else default_size
            opts = "".join(
                f'<option value="{s}" {"selected" if s==val else ""}>{s}</option>'
                for s in JACKET_SIZES)
            pname = esc(a["person_name"] if a else f'{t["contact_name"]}-{i}')
            people += f"""
            <form method="post" action="/jacket/assign" class="person-row">
              <input type="hidden" name="batch_id" value="{d["id"]}">
              <input type="hidden" name="ticket_id" value="{t["id"]}">
              <input type="hidden" name="person_idx" value="{i}">
              <span class="idx">#{i}</span>
              <input name="person_name" value="{pname}" style="width:130px">
              <select name="size_code">{opts}</select>
              <button class="btn small {"orange" if not a else "ghost"}">{"重新确认" if a else "确认尺码"}</button>
              <span class="muted">{esc(a["size_label"]) if a else "未核验"}</span>
            </form>"""
        gate = ""
        if d["status"] == "boarding":
            if t["scanned"]:
                gate = '<span class="tag closed">已放行</span>'
            else:
                gate_disp = "可过闸" if done else "救生衣未齐"
                gate = f"""
                <div class="gatebtns">
                  <form method="post" action="/gate/scan">
                    <input type="hidden" name="ticket_id" value="{t["id"]}">
                    <input type="hidden" name="gate" value="gate-1">
                    <button class="btn big {"orange" if done else ""}" {"disabled" if not done else ""}>闸口 1 扫码</button>
                  </form>
                  <form method="post" action="/gate/scan">
                    <input type="hidden" name="ticket_id" value="{t["id"]}">
                    <input type="hidden" name="gate" value="gate-2">
                    <button class="btn big {"orange" if done else ""}" {"disabled" if not done else ""}>闸口 2 扫码</button>
                  </form>
                  <span class="muted">{gate_disp}</span>
                </div>"""
        tickets_html += f"""
        <div class="card" style="margin-bottom:12px">
          <h2>{esc(t["queue_no"])} · {esc(t["contact_name"])} · {t["party_size"]}人
            {"✅ 救生衣已齐" if done else '<span class=tag overdue>缺救生衣</span>'} {status_tag(t["status"])}</h2>
          {people}
          <div style="margin-top:10px">{gate}</div>
        </div>"""
    # 换船
    boats = conn.execute(
        "SELECT * FROM boats WHERE boat_type_id=? AND status='available' ORDER BY code",
        (d["boat_type_id"],)).fetchall()
    switch = ""
    if d["status"] == "boarding" and boats:
        opts = "".join(f'<option value="{x["id"]}">{esc(x["code"])} {esc(x["name"])}</option>'
                       for x in boats)
        switch = (f'<form method="post" action="/batch/switch" class="inline">'
                  f'<input type="hidden" name="batch_id" value="{d["id"]}">'
                  f'<select name="new_boat_id">{opts}</select>'
                  f'<button class="btn ghost small">临时换船</button></form>')
    depart = ""
    if d["status"] == "boarding":
        depart = (f'<form method="post" action="/batch/depart" style="float:right">'
                  f'<input type="hidden" name="batch_id" value="{d["id"]}">'
                  f'<button class="btn big orange">全员核验完毕 · 出航</button></form>')
    elif d["status"] == "sailing":
        depart = f'<a class="btn big" href="/lake">已出航 · 查看湖面计时 →</a>'
    body = f"""
    {flash(args)}
    <div class="card">
      <h2>批次 {esc(d["code"])} · {esc(d["type_name"])} · 船 {esc(d.get("boat_code") or "未锁定")}
        {status_tag(d["status"])}</h2>
      <div class="inline" style="justify-content:space-between">
        <div>{switch}</div><div>{depart}</div>
      </div>
      <p class="muted" style="margin-top:8px">救生衣实时余量　{stock_line}</p>
    </div>
    {tickets_html}"""
    return layout("登船核验", body, active="boarding")


def page_lake(args) -> bytes:
    data = services.lake_view()
    cards = ""
    for t in data["trips"]:
        if t["overdue"]:
            clock = f'超时 {t["late_min"]} 分钟 ⚠'
            cls = "lake-card overdue-pulse"
        elif t["status"] == "returned":
            clock = "已回港"
            cls = "lake-card"
        else:
            remain = max(0, int((t["expected_return_at"] - data["now"]) / 60))
            clock = f'距预计回港 {remain} 分钟'
            cls = "lake-card"
        cards += f"""
        <div class="{cls}">
          <span class="code">{esc(t["boat_code"])}</span>
          <span class="meta">{esc(t["type_name"])} · 批次 {esc(t["code"])} · {t["party_count"]} 人</span>
          <span class="meta">出航 {fmt_time(t["sailed_at"])} → 预计回港 {fmt_time(t["expected_return_at"])}</span>
          <span class="clock">{clock}</span>
        </div>"""
    if not cards:
        cards = '<div class="card muted">当前湖面无在航船只</div>'
    stop_banner = ""
    if data["weather"]["status"] == "storm":
        stop_banner = ('<div class="flash err" style="font-size:18px;padding:16px">'
                       '⛈ 停航令生效中:已禁止一切新出航,请通知在航船只即刻回港。</div>')
    body = f"""
    {stop_banner}
    <h1 style="font-weight:800">湖面计时</h1>
    <p class="muted">页面保持低信息密度;超时船只仅以边框脉冲提示,不使用大面积闪烁。</p>
    {cards}"""
    return layout("湖面计时", body, active="lake", refresh=5)


def page_return(args) -> bytes:
    conn = get_conn()
    bid = args.get("id")
    if not bid:
        rows = conn.execute(
            "SELECT b.*, bt.name type_name, bo.code boat_code FROM batches b"
            " JOIN boat_types bt ON bt.id=b.boat_type_id"
            " LEFT JOIN boats bo ON bo.id=b.boat_id"
            " WHERE b.status IN ('sailing','returned') ORDER BY b.sailed_at DESC").fetchall()
        items = "".join(
            f'<p><a class="btn {"orange" if r["status"]=="sailing" else "ghost"}" href="/return?id={r["id"]}">'
            f'{esc(r["code"])} · {esc(r["boat_code"])} · {status_tag(r["status"])}</a></p>'
            for r in rows)
        return layout("回港检查",
                      f'<div class="card"><h2>选择回港批次</h2>{items or "<p class=muted>暂无待检查批次</p>"}</div>',
                      active="return")
    d = services.inspection_detail(int(bid))
    if d["status"] not in ("sailing", "returned"):
        return layout("回港检查",
                      f'<div class="card"><p class="flash err">该批次不在待检查状态</p>'
                      f'<a class="btn" href="/return">返回</a></div>', active="return")
    if d.get("inspection_id"):
        hull_labels = {"ok": "正常", "scratch": "刮碰(扣 120 元/船)",
                       "serious": "严重损伤(扣 400 元,船只送修)"}
        hull_label = hull_labels.get(d["hull_result"], d["hull_result"])
        photos = "".join(
            f'<li><a href="/{esc(p["path"])}" target="_blank">船体照片 {i+1}</a></li>'
            for i, p in enumerate(d["photos"]))
        body = f"""
        <div class="card">
          <h2>批次 {esc(d["code"])} 已完成回港检查</h2>
          <table>
            <tr><th>船体结果</th><td>{esc(hull_label)}</td></tr>
            <tr><th>救生衣</th><td>{"全部归还" if d["jackets_ok"] else f'缺失 {len(d["missing_fees"])} 件'}</td></tr>
            <tr><th>检查员</th><td>{esc(d["inspector"])}</td></tr>
            <tr><th>备注</th><td>{esc(d["note"])}</td></tr>
            <tr><th>照片</th><td><ul>{photos or "无"}</ul></td></tr>
          </table>
          <a class="btn big orange" href="/deposit?id={d["id"]}">前往押金复核 →</a>
        </div>"""
        return layout("回港检查", body, active="return")

    # 检查表单:逐件救生衣勾选"缺失"(未还)
    assigns = conn.execute(
        "SELECT a.*, t.queue_no FROM life_jacket_assignments a JOIN tickets t ON t.id=a.ticket_id"
        " WHERE a.batch_id=? ORDER BY a.ticket_id, a.person_idx", (bid,)).fetchall()
    checkboxes = "".join(
        f'<label class="inline" style="margin-right:18px"><input type="checkbox" name="missing" value="{a["id"]}">'
        f'{esc(a["queue_no"])} #{a["person_idx"]} {esc(a["person_name"])} ({a["size_code"]})</label>'
        for a in assigns)
    body = f"""
    {flash(args)}
    <div class="card">
      <h2>回港检查 · 批次 {esc(d["code"])} / 船 {esc(d["boat_code"])}</h2>
      <form method="post" action="/inspection/create" enctype="multipart/form-data" class="grid" style="gap:12px">
        <input type="hidden" name="batch_id" value="{d["id"]}">
        <label class="fld">船体状况
          <select name="hull_result">
            <option value="ok">正常</option>
            <option value="scratch">轻微刮碰（分摊扣款 120 元/船）</option>
            <option value="serious">严重损伤（分摊扣款 400 元,船只送修）</option>
          </select></label>
        <div><b>救生衣归还</b>（勾选=缺失未还,每件扣 80 元）
          <div style="margin-top:8px;line-height:2.1">{checkboxes or "无分配记录"}</div>
        </div>
        <label class="fld">检查员<input name="inspector" value="码头安全员" required></label>
        <label class="fld">检查备注<textarea name="note" rows="2" placeholder="刮碰部位、处理经过等"></textarea></label>
        <label class="fld">船体照片（保存到本地目录,可选;不上传将使用模拟占位图）
          <input type="file" name="photo" accept="image/*"></label>
        <button class="btn big orange">提交回港检查</button>
      </form>
    </div>"""
    return layout("回港检查", body, active="return")


def page_deposit(args) -> bytes:
    conn = get_conn()
    bid = args.get("id")
    nav = conn.execute(
        "SELECT b.*, bo.code boat_code FROM batches b LEFT JOIN boats bo ON bo.id=b.boat_id"
        " WHERE b.status IN ('returned','closed') ORDER BY b.actual_return_at DESC").fetchall()
    nav_html = "".join(
        f'<a class="btn small {"orange" if str(r["id"])==str(bid) else "ghost"}" href="/deposit?id={r["id"]}">'
        f'{esc(r["code"])} {esc(r["boat_code"])}</a> '
        for r in nav)
    if not bid:
        return layout("押金复核",
                      f'<div class="card"><h2>选择已回港批次</h2><div class="inline" style="flex-wrap:wrap">'
                      f'{nav_html or "<span class=muted>暂无已回港批次</span>"}</div></div>',
                      active="deposit")
    d = services.inspection_detail(int(bid))
    hull_labels = {"ok": "正常", "scratch": "刮碰", "serious": "严重损伤"}
    hull_label = hull_labels.get(d["hull_result"], "—")
    rows = ""
    for t in d["tickets"]:
        pays = conn.execute("SELECT * FROM payments WHERE ticket_id=? ORDER BY id",
                            (t["id"],)).fetchall()
        pay_str = "；".join(
            f'{ {"ticket":"票款","hold":"押金授权","deduct":"押金扣除","refund":"押金退还"}[p["kind"]] }'
            f' {p["amount"]:.2f}' for p in pays)
        if t["status"] == "settled":
            action = (f'<span class="tag closed">已结算</span> '
                      f'<span class="muted">扣 <b class=money>{t["deposit_deducted"]:.2f}</b>'
                      f' / 退 <b class=money>{t["deposit_refunded"]:.2f}</b></span>')
        else:
            action = (f'<form method="post" action="/deposit/settle">'
                      f'<input type="hidden" name="ticket_id" value="{t["id"]}">'
                      f'<button class="btn big orange">确认结算（重复提交不会重复扣款）</button></form>')
        rows += (f"<tr><td><b>{esc(t['queue_no'])}</b></td><td>{esc(t['contact_name'])}</td>"
                 f"<td>{t['party_size']}</td><td class=money>{t['deposit_total']:.2f}</td>"
                 f"<td class='muted' style='white-space:normal;max-width:320px'>{esc(pay_str)}</td>"
                 f"<td>{action}</td></tr>")
    body = f"""
    {flash(args)}
    <div class="card"><h2>押金复核 · 批次 {esc(d["code"])}</h2>
      <p class="muted">船体:{esc(hull_label)}
      　检查员:{esc(d["inspector"])}　备注:{esc(d["note"])}
      　押金扣除必须关联回港检查记录。</p>
      <div class="inline" style="flex-wrap:wrap">{nav_html}</div>
    </div>
    <div class="card" style="margin-top:14px">
      <table><thead><tr><th>候船号</th><th>联系人</th><th>人数</th><th>押金</th><th>支付流水(演示)</th><th>操作</th></tr></thead>
      <tbody>{rows}</tbody></table>
    </div>"""
    return layout("押金复核", body, active="deposit")


# ---------- Handler ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "LakeBoat/1.0"

    def log_message(self, fmt, *args):
        pass  # 安静运行

    def _send(self, content: bytes, status: int = 200, ctype="text/html; charset=utf-8",
              extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(content)

    def _redirect(self, path: str):
        self._send(b"", 303, extra_headers=[("Location", path)])

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        args = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        try:
            if path == "/" or path == "/index.html":
                self._send(page_home(args))
            elif path == "/board":
                self._send(page_board(args))
            elif path == "/workbench":
                self._send(page_workbench(args))
            elif path == "/boarding":
                self._send(page_boarding(args))
            elif path == "/lake":
                self._send(page_lake(args))
            elif path == "/return":
                self._send(page_return(args))
            elif path == "/deposit":
                self._send(page_deposit(args))
            elif path.startswith("/photos/"):
                self._serve_photo(path)
            elif path == "/health":
                self._send(b"ok", 200, "text/plain")
            else:
                self._send(layout("未找到", '<div class="card">404 页面不存在</div>'), 404)
        except services.ServiceError as e:
            back = f"/"
            self._send(layout("操作失败",
                              f'<div class="card"><div class="flash err">{esc(str(e))}</div>'
                              f'<a class="btn" href="{back}">返回</a></div>'))
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self._send(layout("服务器错误",
                              f'<div class="card"><div class="flash err">{esc(str(e))}</div></div>'),
                       500)

    def _serve_photo(self, path: str):
        name = os.path.basename(urllib.parse.unquote(path))
        full = os.path.join(PHOTO_DIR, name)
        if not os.path.isfile(full):
            self._send(b"not found", 404, "text/plain")
            return
        ctype = "image/svg+xml" if name.endswith(".svg") else "application/octet-stream"
        with open(full, "rb") as f:
            self._send(f.read(), 200, ctype)

    def _read_form(self):
        length = int(self.headers.get("Content-Length", 0))
        ctype = self.headers.get("Content-Type", "")
        body = self.rfile.read(length)
        if ctype.startswith("multipart/form-data"):
            return parse_multipart(body, ctype)
        parsed = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
        fields = {k: v for k, v in parsed.items()}
        return fields, {}

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            fields, files = self._read_form()
            f = lambda k, d="": fields.get(k, [d])[0] if fields.get(k) else d
            if path == "/sell":
                r = services.sell_ticket(
                    f("contact_name") or "散客",
                    int(f("boat_type_id")), int(f("adults", "1")),
                    int(f("children", "0")))
                self._redirect(f"/?msg=售票成功:候船号 {urllib.parse.quote(r['queue_no'])}"
                               f"（票款 {r['amount']} 元,押金预授权 {r['deposit']} 元已模拟完成）")
            elif path == "/queue/call":
                r = services.call_next(int(f("boat_type_id")))
                self._redirect(f"/workbench?msg=已叫号 {urllib.parse.quote(r['queue_no'])}，{r['window_sec']} 秒内未到将过号")
            elif path == "/queue/requeue":
                r = services.requeue(int(f("ticket_id")))
                self._redirect(f"/workbench?msg=号码 {urllib.parse.quote(r['queue_no'])} 已重排到队尾（第 {r['requeue_count']} 次）")
            elif path == "/batch/create":
                r = services.create_batch(int(f("boat_type_id")))
                self._redirect(f"/workbench?msg=已创建批次 {urllib.parse.quote(r['code'])}")
            elif path == "/batch/add":
                services.add_ticket_to_batch(int(f("batch_id")), int(f("ticket_id")))
                self._redirect(f"/workbench?msg=已加入批次")
            elif path == "/batch/remove":
                bid = f("batch_id")
                services.remove_ticket_from_batch(int(bid), int(f("ticket_id")))
                self._redirect(f"/workbench?msg=已移出批次")
            elif path == "/batch/cancel":
                bid = f("batch_id")
                services.cancel_batch(int(bid))
                self._redirect(f"/workbench?msg=批次已解散,号码回到候船队尾")
            elif path == "/batch/lock":
                bid = int(f("batch_id"))
                boat_id = f("boat_id")
                services.lock_boat(bid, int(boat_id) if boat_id else None)
                self._redirect(f"/boarding?id={bid}&msg=船只已锁定,开始逐人核验救生衣")
            elif path == "/batch/switch":
                bid = int(f("batch_id"))
                r = services.switch_boat(bid, int(f("new_boat_id")))
                self._redirect(f"/boarding?id={bid}&msg=已临时换船至 {urllib.parse.quote(r['boat_code'])}（人数上限不变）")
            elif path == "/jacket/assign":
                bid = int(f("batch_id"))
                services.assign_jacket(bid, int(f("ticket_id")), int(f("person_idx")),
                                       f("person_name") or "游客", f("size_code"))
                self._redirect(f"/boarding?id={bid}")
            elif path == "/gate/scan":
                tid = int(f("ticket_id"))
                r = services.scan_gate(tid, f("gate"))
                bid = get_conn().execute("SELECT batch_id FROM tickets WHERE id=?",
                                         (tid,)).fetchone()["batch_id"]
                if r["allowed"]:
                    self._redirect(f"/boarding?id={bid}&msg={urllib.parse.quote(r['queue_no'])} 在 {r['gate']} 放行成功")
                else:
                    self._redirect(f"/boarding?id={bid}&err={urllib.parse.quote(r['reason'])}")
            elif path == "/batch/depart":
                bid = int(f("batch_id"))
                services.depart(bid)
                self._redirect(f"/lake?msg=批次已出航,开始湖面计时")
            elif path == "/inspection/create":
                bid = int(f("batch_id"))
                photo_paths = []
                if "photo" in files:
                    filename, data = files["photo"]
                    if data:
                        photo_paths.append(services.save_photo(filename, data))
                if not photo_paths:
                    photo_paths.append(services.make_demo_photo(bid))
                missing = [int(x) for x in fields.get("missing", [])]
                services.create_inspection(
                    bid, f("hull_result", "ok"), f("inspector", "安全员"),
                    f("note"), photo_paths, missing)
                self._redirect(f"/return?id={bid}&msg=回港检查已提交,可进行押金复核")
            elif path == "/deposit/settle":
                tid = int(f("ticket_id"))
                r = services.settle_ticket_deposit(tid)
                bid = get_conn().execute("SELECT batch_id FROM tickets WHERE id=?",
                                         (tid,)).fetchone()["batch_id"]
                if r.get("idempotent"):
                    self._redirect(f"/deposit?id={bid}&msg={urllib.parse.quote(r['queue_no'])} 此前已结算,本次为重复提交,未重复扣款")
                else:
                    self._redirect(f"/deposit?id={bid}&msg={urllib.parse.quote(r['queue_no'])} 结算完成:扣除 {r['deducted']} 元,退还 {r['refunded']} 元")
            elif path == "/weather":
                services.set_weather(f("status"), source="人工调度")
                self._redirect("/workbench?msg=天气状态已更新并广播")
            else:
                self._send(b"not found", 404, "text/plain")
        except services.ServiceError as e:
            referer = self.headers.get("Referer", "/")
            sep = "&" if "?" in referer else "?"
            self._redirect(f"{referer}{sep}err={urllib.parse.quote(str(e))}")
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self._send(f"500 {esc(str(e))}".encode(), 500, "text/html; charset=utf-8")


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    init_db()
    from .tasks import BackgroundTasks
    bg = BackgroundTasks()
    bg.start()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"🚣 湖湾公园游船平台已启动: http://{host}:{port}")
    print("   候船叫号屏 /board · 组批 /workbench · 登船 /boarding · 湖面 /lake · 回港 /return · 押金 /deposit")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        bg.stop()
        print("\n已停止")
