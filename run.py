"""一条命令启动带样例的完整应用：python run.py [--fresh] [--port 8000]"""
from __future__ import annotations

import argparse
from pathlib import Path

from app.db import connect, init_db
from app.seed import seed
from app.server import App
from http.server import ThreadingHTTPServer
from app.server import make_handler


def main() -> None:
    ap = argparse.ArgumentParser(description="公园游船排队与救生衣核验平台")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--db", default="data/lake.db")
    ap.add_argument("--fresh", action="store_true", help="清空后重新生成样例")
    args = ap.parse_args()

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if args.fresh and db_path.exists():
        for p in db_path.parent.glob(db_path.name + "*"):
            p.unlink()

    app = App(db_path)
    n = seed(app.svc)
    if n:
        print(f"已载入 {n} 张演示票（含模拟押金支付记录）")
    app.start()
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    print(f"🚣 公园游船平台已启动：http://{args.host}:{args.port}/screen")
    print("   候船叫号屏 /screen · 组批工作台 /workbench · 登船核验 /boarding")
    print("   湖面计时 /lake · 回港检查 /dock · 押金复核 /deposit")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        app.scheduler.stop()


if __name__ == "__main__":
    main()
