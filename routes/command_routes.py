# routes/command_routes.py
"""The Command Center: one page for level/XP, what needs you, missions, health, memory + graph, skills.

    GET  /command                       the page (through auth; the CSP nonce is filled in per request)
    GET  /api/command                   one JSON document (admin)
    POST /api/command/approvals/{id}    approve/deny a pending approval from the web
                                        - needs header X-AURIX-Confirm: APPROVE|DENY
                                        - needs a REAL logged-in admin: the agent's internal loopback token is refused,
                                          otherwise an agent could approve its own request (see human_admin_ok)
"""
import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from core.middleware import INTERNAL_TOOL_HEADER, require_admin
from src.foundation import command_center, sysview

_PAGE = Path(__file__).resolve().parent.parent / "static" / "command.html"


def _sibling_url(request: Request, port: str) -> str:
    """Another service on the same machine the owner is already browsing (e.g. over Tailscale), by port."""
    host = request.url.hostname or "localhost"
    if ":" in host and not host.startswith("["):          # bare IPv6
        host = f"[{host}]"
    return f"{request.url.scheme}://{host}:{port}/"


def _godseye_url(request: Request) -> str:
    return _sibling_url(request, os.environ.get("GODSEYE_PORT", "4173"))


def _kuma_url(request: Request) -> str:
    return _sibling_url(request, os.environ.get("KUMA_PORT", "3001"))


def _extras_links(request: Request) -> dict:
    """FileBrowser / ReClip links, only when the compose "extras" profile is on (they are opt-in containers)."""
    profiles = {p.strip() for p in os.environ.get("COMPOSE_PROFILES", "").split(",")}
    if "extras" not in profiles:
        return {}
    return {"files": _sibling_url(request, os.environ.get("FILEBROWSER_PORT", "8088")),
            "reclip": _sibling_url(request, os.environ.get("RECLIP_PORT", "8899"))}


def _require_human_admin(request: Request) -> None:
    auth_mgr = getattr(request.app.state, "auth_manager", None)
    user = getattr(request.state, "current_user", None)
    ok = command_center.human_admin_ok(
        has_internal_token=bool(request.headers.get(INTERNAL_TOOL_HEADER)),
        current_user=user,
        auth_configured=bool(auth_mgr and getattr(auth_mgr, "is_configured", False)),
        is_admin=bool(auth_mgr and user and auth_mgr.is_admin(user)),
    )
    if not ok:
        raise HTTPException(403, "Approvals can only be decided by a logged-in admin")


def setup_command_routes() -> APIRouter:
    router = APIRouter(tags=["command"])

    @router.get("/command", response_class=HTMLResponse)
    @router.get("/dashboard", response_class=HTMLResponse)          # the same page: "the main dashboard"
    async def page(request: Request):
        require_admin(request)
        try:
            html = sysview.render_page(_PAGE.read_text(encoding="utf-8"), getattr(request.state, "csp_nonce", ""))
        except OSError:
            raise HTTPException(404, "command.html is missing")
        return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    @router.get("/api/command", dependencies=[Depends(require_admin)])
    async def api(request: Request):
        return await asyncio.to_thread(command_center.snapshot, None, None, None, _godseye_url(request), _kuma_url(request),
                                       _extras_links(request))

    @router.post("/api/command/action")
    async def action(request: Request):
        """One-tap dashboard buttons. A fixed list (actions.ACTION_NAMES); a REAL logged-in admin plus X-AURIX-Confirm: DO."""
        _require_human_admin(request)
        if request.headers.get("X-AURIX-Confirm", "").strip().upper() != "DO":
            raise HTTPException(400, "missing X-AURIX-Confirm: DO")
        from src.foundation import actions
        try:
            body = await request.json()
        except Exception:
            body = {}
        name = str((body or {}).get("action", ""))
        if name not in actions.ACTION_NAMES:
            raise HTTPException(400, "unknown action")
        return await asyncio.to_thread(actions.run_action, name, str((body or {}).get("arg", "")))

    @router.post("/api/command/run-all")
    async def run_all(request: Request):
        """The one push: re-check every monitor and add-on, ask the game PC for fresh facts. Read-only apart from that request flag.
        Same authority rule as the other POSTs: a real logged-in admin plus X-AURIX-Confirm: RUN."""
        _require_human_admin(request)
        if request.headers.get("X-AURIX-Confirm", "").strip().upper() != "RUN":
            raise HTTPException(400, "missing X-AURIX-Confirm: RUN")
        from src.foundation import addons
        return await asyncio.to_thread(addons.run_all)

    @router.get("/api/command/links", dependencies=[Depends(require_admin)])
    async def links(request: Request):
        """Where the sibling views live (the rail buttons use this; God's Eye is a separate container on GODSEYE_PORT)."""
        return {"godseye": _godseye_url(request), "command": "/command", "systems": "/systems", "kuma": _kuma_url(request),
                **_extras_links(request)}

    @router.get("/api/command/skills/{name}", dependencies=[Depends(require_admin)])
    async def skill(name: str):
        """A forged skill's code and tests, so it can be read BEFORE it is approved."""
        detail = await asyncio.to_thread(command_center.skill_detail, name)
        if detail is None:
            raise HTTPException(404, "no such skill")
        return detail

    @router.post("/api/command/skills/{name}/{verb}")
    async def skill_decide(name: str, verb: str, request: Request):
        """approve | deny | retire a forged skill. Same authority rule as approvals: a REAL logged-in admin (never the
        agent's loopback token, or an agent could approve its own skill) plus X-AURIX-Confirm: <VERB>."""
        _require_human_admin(request)
        if verb not in command_center.SKILL_VERBS or request.headers.get("X-AURIX-Confirm", "").strip().upper() != verb.upper():
            raise HTTPException(400, f"missing X-AURIX-Confirm: {verb.upper()}")
        result = await asyncio.to_thread(command_center.skill_action, name, verb)
        return result

    @router.post("/api/command/approvals/{approval_id}")
    async def decide(approval_id: str, request: Request):
        _require_human_admin(request)
        verb = command_center.parse_decision(request.headers.get("X-AURIX-Confirm", ""))
        if verb is None:
            raise HTTPException(400, "missing X-AURIX-Confirm: APPROVE or DENY")
        if not command_center.APPROVAL_ID_RE.match(approval_id or ""):
            raise HTTPException(400, "bad approval id")
        from src import approval_gate as ag
        return {"ok": True, "message": ag.handle_command(verb, approval_id, resolver="web")}

    return router
