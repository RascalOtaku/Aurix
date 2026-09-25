# routes/sysview_routes.py
"""AURIX Systems: a live, read-only picture of every AURIX subsystem, plus a STOP button.

    GET  /systems             the dashboard page (served through auth, not /static)
    GET  /api/systems         snapshot JSON (polled every few seconds by the page)
    POST /api/systems/stop    the owner's `stop` command; needs header X-AURIX-Confirm: STOP

Both data routes are admin-only. The page never sends anything but STOP, and STOP can only
make AURIX quieter (it is the most conservative action there is), so it needs no approval.
"""
import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from core.middleware import require_admin
from src.foundation import sysview

_PAGE = Path(__file__).resolve().parent.parent / "static" / "sysview.html"


def setup_sysview_routes() -> APIRouter:
    router = APIRouter(tags=["sysview"])

    @router.get("/systems", response_class=HTMLResponse)
    async def page(request: Request):
        require_admin(request)
        try:
            html = sysview.render_page(_PAGE.read_text(encoding="utf-8"), getattr(request.state, "csp_nonce", ""))
            return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})
        except OSError:
            raise HTTPException(404, "sysview.html is missing")

    @router.get("/api/systems", dependencies=[Depends(require_admin)])
    async def api_snapshot():
        # probes do blocking network/file work with short timeouts: keep them off the event loop
        return await asyncio.to_thread(sysview.snapshot)

    @router.post("/api/systems/stop")
    async def api_stop(request: Request):
        require_admin(request)
        if request.headers.get("X-AURIX-Confirm", "") != "STOP":     # a stray request cannot stop AURIX
            raise HTTPException(400, "missing X-AURIX-Confirm: STOP")
        return {"ok": True, "message": await sysview.do_stop()}

    return router
