# routes/gamepilot_routes.py
"""GamePilot relay (see src/foundation/gamepilot.py for the security model). These paths are exempt from the app's login because a Fire TV cannot log in
sensibly; each one authenticates itself instead:

    viewer  (the Fire TV browser)   GET  /gamepilot                   the page (contains nothing secret; shows a pairing code)
                                    POST /api/gamepilot/pair          ask for a pairing code (rate-limited by a small pending cap)
                                    GET  /api/gamepilot/pair/status   the page polls until the owner confirms the code in Telegram
                                    GET  /api/gamepilot/state         needs the session token
                                    GET  /api/gamepilot/frame.jpg     needs the session token
                                    POST /api/gamepilot/input         needs the session token AND the owner to have armed control
    agent   (the gaming PC)         POST /api/gamepilot/agent/poll    HMAC-signed by the shared key; the reply is signed back
                                    POST /api/gamepilot/agent/frame   HMAC-signed JPEG
"""
import asyncio
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from src.foundation import gamepilot

_PAGE = Path(__file__).resolve().parent.parent / "static" / "gamepilot.html"
_NOSTORE = {"Cache-Control": "no-store"}


def _token(request: Request) -> str:
    return request.headers.get("X-GP-Token", "") or request.query_params.get("t", "")


async def _agent_ok(request: Request, body: bytes) -> None:
    why = gamepilot.verify_agent(request.method, request.url.path, body, request.headers.get("X-GP-Ts", ""), request.headers.get("X-GP-Sig", ""))
    if why:
        raise HTTPException(401, why)


def setup_gamepilot_routes() -> APIRouter:
    router = APIRouter(tags=["gamepilot"])

    @router.get("/gamepilot", response_class=HTMLResponse)
    async def page(request: Request):
        try:
            html = _PAGE.read_text(encoding="utf-8").replace("{{CSP_NONCE}}", getattr(request.state, "csp_nonce", ""))
        except OSError:
            raise HTTPException(404, "gamepilot.html is missing")
        return HTMLResponse(html, headers={**_NOSTORE, "X-Frame-Options": "DENY"})

    @router.post("/api/gamepilot/pair")
    async def pair():
        res = await asyncio.to_thread(gamepilot.new_pairing)
        if "error" in res:
            raise HTTPException(429, res["error"])
        return JSONResponse(res, headers=_NOSTORE)

    @router.get("/api/gamepilot/pair/status")
    async def pair_status(poll: str = ""):
        return JSONResponse(await asyncio.to_thread(gamepilot.pair_status, poll), headers=_NOSTORE)

    @router.get("/api/gamepilot/state")
    async def state(request: Request):
        return JSONResponse(await asyncio.to_thread(gamepilot.viewer_state, _token(request)), headers=_NOSTORE)

    @router.get("/api/gamepilot/frame.jpg")
    async def frame(request: Request, after: int = 0):
        if gamepilot.session(_token(request)) is None:
            raise HTTPException(401, "not paired")
        gamepilot.note_viewer()
        deadline = time.time() + 1.2                                       # hold the request briefly so the viewer gets each new frame as soon as it exists
        while True:
            got = gamepilot.frame_after(after)
            if got:
                return Response(got[1], media_type="image/jpeg", headers={**_NOSTORE, "X-GP-Seq": str(got[0])})
            if time.time() > deadline:
                return Response(status_code=204, headers=_NOSTORE)
            await asyncio.sleep(0.05)

    @router.post("/api/gamepilot/input")
    async def send_input(request: Request):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "bad json")
        res = await asyncio.to_thread(gamepilot.submit_input, _token(request), (body or {}).get("events"))
        if not res.get("ok"):
            raise HTTPException(403 if res.get("error") in ("not paired", "not armed") else 400, res.get("error", "refused"))
        return JSONResponse(res, headers=_NOSTORE)

    @router.post("/api/gamepilot/agent/poll")
    async def agent_poll(request: Request):
        raw = await request.body()
        await _agent_ok(request, raw)
        try:
            import json
            report = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise HTTPException(400, "bad json")
        return JSONResponse(await asyncio.to_thread(gamepilot.agent_poll, report), headers=_NOSTORE)

    @router.post("/api/gamepilot/agent/frame")
    async def agent_frame(request: Request):
        raw = await request.body()
        if len(raw) > gamepilot.MAX_FRAME_BYTES:
            raise HTTPException(413, "frame too large")
        await _agent_ok(request, raw)
        seq = await asyncio.to_thread(gamepilot.put_frame, raw)
        if not seq:
            raise HTTPException(400, "not a usable JPEG frame")
        return {"ok": True, "seq": seq}

    return router
