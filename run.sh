#!/usr/bin/env bash
# 一条命令启动:自动建库、写入样例数据并开启 Web 服务与后台任务
set -e
cd "$(dirname "$0")"

PORT="${1:-8000}"

# 首次运行自动准备样例数据库(已存在则保留)
python3 - "$PORT" <<'PY'
import os, sys
sys.path.insert(0, os.getcwd())
from app.db import init_db
init_db()
print("✓ 数据库与样例数据已就绪 (app/data/boat.db)")
PY

echo "✓ 启动公园游船排队与救生衣核验平台  http://127.0.0.1:${PORT}"
exec python3 run.py "${PORT}"
