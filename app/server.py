"""标准库 HTTP 服务：页面/静态资源/JSON API/SSE 实时推送。"""
from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .adapters import PaymentAdapter, WeatherAdapter
from .db import connect, init_db
from .events import EventBus
from .scheduler import Scheduler
from .service import BoatError, Service

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
PHOTO_DIR = Path(__file__).resolve().parent.parent / "photos"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
}


class App:
    def __init__(self, db_path: str, photo_dir: Path = PHOTO_DIR):
        self.conn = connect(db_path)
        init_db(self.conn)
        self.weather = WeatherAdapter()
        self.payments = PaymentAdapter()
        self.svc = Service(self.conn, self.weather, self.payments, photo_dir)
        self.bus = EventBus()
        self.scheduler = Scheduler(self.svc, self.bus)

    def start(self) -> None:
        self.scheduler.start()


def make_handler(app: App):
    svc = app.svc

    class Handler(BaseHTTPRequestHandler):
        server_version = "LakeBoat/1.0"

        def log_message(self, fmt, *args):  # 安静一点
            pass

        # ---------- 响应工具 ----------
        def _json(self, obj, status: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            n = int(self.headers.get("Content-Length", 0))
            if not n:
                return {}
            raw = self.rfile.read(n)
            try:
                return json.loads(raw.decode())
            except json.JSONDecodeError:
                raise BoatError("请求体不是合法 JSON")

        def _serve_file(self, path: Path) -> None:
            if not path.is_file():
                self.send_error(404)
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type",
                             CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _run(self, fn, *args, **kwargs):
            try:
                result = fn(*args, **kwargs)
                app.bus.publish("change")
                self._json(result if result is not None else {"ok": True})
            except BoatError as e:
                self._json({"error": str(e)}, 409)
            except KeyError as e:
                self._json({"error": f"缺少参数 {e}"}, 400)

        # ---------- 路由 ----------
        def do_GET(self) -> None:
            u = urlparse(self.path)
            p, q = u.path, u.query
            try:
                if p == "/" or p == "/screen":
                    return self._serve_file(STATIC_DIR / "screen.html")
                if p == "/workbench":
                    return self._serve_file(STATIC_DIR / "workbench.html")
                if p == "/boarding":
                    return self._serve_file(STATIC_DIR / "boarding.html")
                if p == "/lake":
                    return self._serve_file(STATIC_DIR / "lake.html")
                if p == "/dock":
                    return self._serve_file(STATIC_DIR / "dock.html")
                if p == "/deposit":
                    return self._serve_file(STATIC_DIR / "deposit.html")
                if p.startswith("/static/"):
                    return self._serve_file(STATIC_DIR / p[len("/static/"):])
                if p.startswith("/photos/"):
                    return self._serve_file(PHOTO_DIR / p[len("/photos/"):])

                if p == "/api/queue":
                    return self._json(svc.queue_overview())
                if p == "/api/workbench":
                    return self._json(svc.batch_workbench())
                if p == "/api/departures":
                    return self._json({"batches": svc.departable_batches(),
                                       "suspended": app.weather.is_suspended()})
                if p == "/api/lake":
                    return self._json(svc.lake_view())
                if p == "/api/dock":
                    return self._json(svc.dock_view())
                if p == "/api/deposits":
                    return self._json(svc.deposit_view())
                if p == "/api/payments":
                    rows = svc.conn.execute(
                        "SELECT p.*, t.ticket_no FROM payments p "
                        "JOIN tickets t ON t.id=p.ticket_id "
                        "ORDER BY p.id DESC LIMIT 100").fetchall()
                    return self._json({"items": [dict(r) for r in rows]})
                if p == "/api/weather":
                    return self._json({"weather": app.weather.current().as_dict()})
                if p.startswith("/api/batches/"):
                    bid = int(p.rsplit("/", 1)[-1])
                    return self._json(svc.batch_detail(bid))
                if p.startswith("/api/trips/"):
                    tid = int(p.rsplit("/", 1)[-1])
                    return self._json(svc.trip_detail(tid))
                if p == "/api/events":
                    return self._sse()
                self.send_error(404)
            except BoatError as e:
                self._json({"error": str(e)}, 409)
            except ValueError:
                self.send_error(404)

        def do_POST(self) -> None:
            p = urlparse(self.path).path
            body = self._read_json()
            try:
                if p == "/api/tickets":
                    return self._run(svc.sell_ticket,
                                     name=body["name"], phone=body["phone"],
                                     party_size=int(body["party_size"]),
                                     boat_pref=body["boat_pref"],
                                     children_count=int(body.get("children_count", 0)))
                if p == "/api/call":
                    return self._run(svc.call_next, body.get("boat_type"))
                if p == "/api/requeue":
                    return self._run(svc.requeue_missed, int(body["ticket_id"]))
                if p == "/api/missed":
                    return self._run(svc.mark_missed, int(body["ticket_id"]))
                if p == "/api/batches":
                    ids = [int(x) for x in body["ticket_ids"]]
                    return self._run(svc.create_batch,
                                     int(body["boat_id"]), ids)
                if p == "/api/weather":
                    return self._run(svc.set_weather, body["level"])
                if p.startswith("/api/batches/"):
                    rest = p[len("/api/batches/"):]
                    parts = rest.split("/")
                    bid = int(parts[0])
                    if len(parts) == 2 and parts[1] == "change-boat":
                        return self._run(svc.change_boat, bid,
                                         int(body["new_boat_id"]))
                    if len(parts) == 2 and parts[1] == "disband":
                        return self._run(svc.disband_batch, bid,
                                         body.get("reason", ""))
                    if len(parts) == 2 and parts[1] == "depart":
                        return self._run(svc.depart, bid,
                                         int(body.get("minutes", 60)))
                if p.startswith("/api/boardings/"):
                    rest = p[len("/api/boardings/"):]
                    parts = rest.split("/")
                    bd_id = int(parts[0])
                    if len(parts) == 2 and parts[1] == "vest":
                        return self._run(svc.confirm_vest, bd_id, body["size"])
                if p == "/api/gate/scan":
                    return self._run(svc.gate_scan, body["ticket_no"],
                                     body.get("gate", "G1"))
                if p.startswith("/api/trips/"):
                    rest = p[len("/api/trips/"):]
                    tid, action = rest.split("/")
                    tid = int(tid)
                    if action == "photo":
                        data = base64.b64decode(body["data"])
                        return self._run(svc.add_hull_photo, tid,
                                         body.get("filename", "photo.jpg"),
                                         data, body.get("label", "船体照片"))
                    if action == "return-check":
                        return self._run(
                            svc.return_check, tid,
                            hull_ok=bool(body["hull_ok"]),
                            vest_missing=int(body.get("vest_missing", 0)),
                            note=body.get("note", ""))
                    if action == "vest-return":
                        return self._run(svc.confirm_vest_return, tid)
                    if action == "recall":
                        return self._run(svc.recall_boat, tid)
                if p.startswith("/api/tickets/") and p.endswith("/settle"):
                    tid = int(p.split("/")[3])
                    override = body.get("deduct")
                    return self._run(svc.settle, tid,
                                     int(override) if override is not None else None)
                self.send_error(404)
            except BoatError as e:
                self._json({"error": str(e)}, 409)
            except (KeyError, ValueError, TypeError) as e:
                self._json({"error": f"参数错误：{e}"}, 400)

        # ---------- SSE ----------
        def _sse(self) -> None:
            sid = app.bus.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self.wfile.write(b"retry: 3000\n\n")
                self.wfile.flush()
                while True:
                    app.bus.wait(25.0)
                    self.wfile.write(b"event: update\ndata: {}\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                app.bus.unsubscribe(sid)

    return Handler


def serve(db_path: str = "data/lake.db", host: str = "127.0.0.1",
          port: int = 8000) -> None:
    app = App(db_path)
    app.start()
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"🚣 公园游船平台已启动：http://{host}:{port}/screen（候船叫号屏）")
    print(f"   组批工作台 /workbench · 登船核验 /boarding · 湖面计时 /lake")
    print(f"   回港检查 /dock · 押金复核 /deposit")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        app.scheduler.stop()
