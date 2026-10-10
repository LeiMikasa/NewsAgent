"""Serve only built public assets; user memory, briefs and .env stay private."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles


FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"


def mount_frontend(app: FastAPI) -> None:
    assets = FRONTEND_DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="frontend-assets")

    @app.get("/", include_in_schema=False)
    def index():
        # Hash navigation needs only this route; unknown /api routes remain 404.
        if (FRONTEND_DIST / "index.html").is_file():
            return FileResponse(FRONTEND_DIST / "index.html", headers={"Cache-Control": "no-cache"})
        return HTMLResponse(
            '<html lang="zh-CN"><meta charset="utf-8"><title>NewsAgent</title>'
            '<h1>NewsAgent 前端尚未构建</h1><p>在 frontend 目录执行 '
            '<code>npm install</code> 和 <code>npm run build</code>，然后重启后端。</p>'
            '<p><a href="/docs">查看后端 API</a></p></html>', status_code=503)
