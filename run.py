#!/usr/bin/env python3
"""本地启动入口:python3 run.py [port]"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.web import serve  # noqa: E402

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "127.0.0.1")
    serve(host=host, port=port)
