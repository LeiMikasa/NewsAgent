"""python -m backend: one process owns the single-user scheduler."""

import argparse
import sys
from pathlib import Path

import uvicorn

from agent import load_settings
from .app import create_app


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="NewsAgent 单用户 API 与定时任务后端")
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--cors-origin", action="append", help="允许的前端来源，可重复设置")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    settings = load_settings(args.env_file, workdir=args.workdir)
    uvicorn.run(create_app(settings, cors_origins=args.cors_origin), host=args.host, port=args.port, workers=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
