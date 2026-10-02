"""Diagnostics routes — /api/db/stats, /api/rag/stats, /api/test/youtube, /api/test-research, /api/health/aggregate."""

import asyncio
import logging
import socket
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, HTTPException, Form

from core.constants import DEFAULT_HOST, SEARXNG_INSTANCE

logger = logging.getLogger(__name__)

# --- Aggregated degraded-state health probes ---------------------------------
#
# Each probe is a plain sync function with injectable dependencies so tests
# can mock them. Probes run concurrently in threads and each is hard-capped
# by PROBE_TIMEOUT_S, so one slow or hung service can never stall the whole
# aggregate response. Per-service result shape:
#   {"status": "ok"|"degraded"|"down", "latency_ms": N, "error": "...", "detail": {...}}
# "error"/"detail" are present only when meaningful.

#: Hard per-probe ceiling in seconds (applies on top of each probe's own
#: internal httpx/socket timeouts).
PROBE_TIMEOUT_S = 3.0

_SERVICE_ORDER = ("chromadb", "searxng", "email", "ntfy", "providers")

_HEALTH_UA = {"User-Agent": "Aurix/1.0 (health-check)"}


def _result(
    status: str,
    latency_ms: int,
    error: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {"status": status, "latency_ms": latency_ms}
    if error:
        out["error"] = error
    if detail:
        out["detail"] = detail
    return out


def _run_probe(fn: Callable, *args: Any) -> Dict[str, Any]:
    """Run one sync probe, timing it; any exception becomes status "down"."""
    start = time.monotonic()
    try:
        res = fn(*args)
        res["latency_ms"] = int((time.monotonic() - start) * 1000)
        return res
    except Exception as e:  # noqa: BLE001 - a health probe must never raise
        latency_ms = int((time.monotonic() - start) * 1000)
        logger.warning("health probe %s failed: %s", getattr(fn, "__name__", fn), e)
        return _result("down", latency_ms, error=str(e)[:200])


# --- Individual probes --------------------------------------------------------


def probe_chromadb(rag_manager: Any, rag_available: bool) -> Dict[str, Any]:
    """ChromaDB-backed RAG vector store health via the RAG manager's stats."""
    if not (rag_available and rag_manager):
        return _result("degraded", 0, error="RAG vector store not available")
    stats = rag_manager.get_stats() or {}
    if stats.get("error"):
        return _result("down", 0, error=str(stats["error"])[:200])
    detail = {k: stats[k] for k in ("document_count", "collection_name") if k in stats}
    return _result("ok", 0, detail=detail or None)


def probe_searxng(instance_url: str = SEARXNG_INSTANCE, http_get: Optional[Callable] = None) -> Dict[str, Any]:
    """SearXNG reachability: a plain GET on the instance root."""
    get = http_get or httpx.get
    r = get(instance_url.rstrip("/") + "/", timeout=2.5, headers=_HEALTH_UA)
    if r.status_code < 400:
        return _result("ok", 0, detail={"instance": instance_url})
    return _result("down", 0, error=f"SearXNG returned HTTP {r.status_code}")


def probe_email(get_accounts: Optional[Callable[[], List[Tuple[str, str, int]]]] = None) -> Dict[str, Any]:
    """Email health: configured accounts, with IMAP reachability (TCP only, no login)."""
    accounts = get_accounts() if get_accounts else _load_email_accounts()
    if not accounts:
        return _result("degraded", 0, error="no email accounts configured")
    reachable: List[str] = []
    for _name, host, port in accounts:
        try:
            with socket.create_connection((host, port), timeout=2):
                reachable.append(_name)
        except OSError:
            pass
    detail = {"accounts": len(accounts), "reachable": len(reachable)}
    if len(reachable) == len(accounts):
        return _result("ok", 0, detail=detail)
    if reachable:
        return _result(
            "degraded", 0,
            error=f"{len(accounts) - len(reachable)} of {len(accounts)} IMAP hosts unreachable",
            detail=detail,
        )
    return _result("down", 0, error="no IMAP host reachable", detail=detail)


def probe_ntfy(
    get_integrations: Optional[Callable[[], List[Dict[str, str]]]] = None,
    http_get: Optional[Callable] = None,
) -> Dict[str, Any]:
    """ntfy health: GET the server root. Never publishes anything."""
    items = get_integrations() if get_integrations else _load_ntfy_integrations()
    if not items:
        return _result("degraded", 0, error="ntfy not configured")
    get = http_get or httpx.get
    up: List[str] = []
    errs: List[str] = []
    for it in items:
        try:
            r = get(it["base_url"].rstrip("/") + "/", timeout=2.5, headers=_HEALTH_UA)
            if r.status_code < 500:
                up.append(it["name"])
            else:
                errs.append(f"{it['name']}: HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001 - per-server errors are data
            errs.append(f"{it['name']}: {str(e)[:80]}")
    detail = {"servers": len(items), "reachable": len(up)}
    if len(up) == len(items):
        return _result("ok", 0, detail=detail)
    if up:
        return _result("degraded", 0, error="; ".join(errs)[:200], detail=detail)
    return _result("down", 0, error="; ".join(errs)[:200] or "unreachable", detail=detail)


def probe_providers(
    get_endpoints: Optional[Callable[[], List[Tuple[str, str, Optional[str]]]]] = None,
    ping_fn: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Configured model endpoints (ModelEndpoint rows), pinged like the model picker does."""
    endpoints = get_endpoints() if get_endpoints else _load_model_endpoints()
    if not endpoints:
        return _result("degraded", 0, error="no model endpoints configured")
    if ping_fn is None:
        from routes.model_routes import _ping_endpoint  # lazy: same pattern as get_detailed_stats below
        ping_fn = _ping_endpoint
    details: List[Dict[str, Any]] = []
    for name, base_url, api_key in endpoints:
        try:
            res = ping_fn(base_url, api_key, timeout=2.0)
            ok = bool(res.get("reachable"))
            details.append({"name": name, "status": "ok" if ok else "down", "error": res.get("error")})
        except Exception as e:  # noqa: BLE001 - per-endpoint errors are data
            details.append({"name": name, "status": "down", "error": str(e)[:120]})
    ok_count = sum(1 for d in details if d["status"] == "ok")
    detail = {"endpoints": details}
    if ok_count == len(details):
        return _result("ok", 0, detail=detail)
    errs = "; ".join(f"{d['name']}: {d['error'] or 'unreachable'}" for d in details if d["status"] != "ok")[:200]
    if ok_count:
        return _result("degraded", 0, error=errs, detail=detail)
    return _result("down", 0, error=errs, detail=detail)


# --- DB/config loaders (only called inside probe threads) ---------------------


def _load_email_accounts() -> List[Tuple[str, str, int]]:
    from core.database import SessionLocal, EmailAccount

    db = SessionLocal()
    try:
        rows = db.query(EmailAccount).filter(EmailAccount.enabled == True).all()  # noqa: E712
        return [(r.name or r.id, r.imap_host, r.imap_port or 993) for r in rows if r.imap_host]
    finally:
        db.close()


def _load_ntfy_integrations() -> List[Dict[str, str]]:
    from src.integrations import load_integrations

    out = []
    for it in load_integrations():
        if it.get("preset") == "ntfy" and it.get("enabled", True) and it.get("base_url"):
            out.append({"name": it.get("name") or "ntfy", "base_url": it["base_url"]})
    return out


def _load_model_endpoints() -> List[Tuple[str, str, Optional[str]]]:
    from core.database import SessionLocal, ModelEndpoint

    db = SessionLocal()
    try:
        rows = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True).all()  # noqa: E712
        return [(r.name or r.id, r.base_url, r.api_key) for r in rows if r.base_url]
    finally:
        db.close()


# --- Aggregate ---------------------------------------------------------------


async def build_health_aggregate(
    rag_manager: Any = None,
    rag_available: bool = False,
    searxng_instance: Optional[str] = None,
    get_email_accounts: Optional[Callable] = None,
    get_ntfy_integrations: Optional[Callable] = None,
    get_model_endpoints: Optional[Callable] = None,
    http_get: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Run every probe concurrently; each is hard-capped so one hung service
    can never stall the aggregate response."""
    specs = {
        "chromadb": (probe_chromadb, (rag_manager, rag_available)),
        "searxng": (probe_searxng, (searxng_instance or SEARXNG_INSTANCE, http_get)),
        "email": (probe_email, (get_email_accounts,)),
        "ntfy": (probe_ntfy, (get_ntfy_integrations, http_get)),
        "providers": (probe_providers, (get_model_endpoints,)),
    }

    async def _one(name: str) -> Tuple[str, Dict[str, Any]]:
        fn, args = specs[name]
        try:
            res = await asyncio.wait_for(
                asyncio.to_thread(_run_probe, fn, *args), timeout=PROBE_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            res = _result(
                "down", int(PROBE_TIMEOUT_S * 1000),
                error=f"probe timed out after {PROBE_TIMEOUT_S:g}s",
            )
        return name, res

    services = dict(await asyncio.gather(*(_one(n) for n in _SERVICE_ORDER)))
    return {"ok": all(s["status"] == "ok" for s in services.values()), "services": services}


def setup_diagnostics_routes(
    rag_manager,
    rag_available: bool,
    research_handler,
) -> APIRouter:
    router = APIRouter(tags=["diagnostics"])

    @router.get("/api/db/stats")
    async def get_database_stats() -> Dict[str, Any]:
        try:
            from core.database import get_detailed_stats
            return get_detailed_stats()
        except Exception as e:
            logger.error(f"DB stats error: {e}")
            raise HTTPException(500, "Failed to retrieve database statistics")

    @router.get("/api/rag/stats")
    async def get_rag_stats() -> Dict[str, Any]:
        if rag_available and rag_manager:
            return rag_manager.get_stats()
        return {"error": "RAG system not available"}

    @router.get("/api/health/aggregate")
    async def get_health_aggregate() -> Dict[str, Any]:
        """Aggregated degraded-state report: one cheap probe per integration.

        Probes run concurrently and each is hard-capped by PROBE_TIMEOUT_S,
        so a hung service can never stall the response. Per-service shape:
        {"status": "ok"|"degraded"|"down", "latency_ms": N, "error": "..."}.
        """
        return await build_health_aggregate(
            rag_manager=rag_manager,
            rag_available=rag_available,
        )

    @router.get("/api/test/youtube")
    async def test_youtube(url: str) -> Dict[str, Any]:
        try:
            from services.youtube.youtube_handler import (  # lazy: keep module import light
                extract_youtube_id,
                extract_transcript_async,
            )
            video_id = extract_youtube_id(url)
            if not video_id:
                return {"error": "Invalid YouTube URL"}

            data = await extract_transcript_async(url, video_id)
            return {
                "video_id": video_id,
                "transcript_success": data.get("success", False),
                "transcript_length": len(data.get("transcript", "")) if data.get("success") else 0,
                "transcript_preview": (data.get("transcript", "")[:500] + "...")
                    if data.get("success") and len(data.get("transcript", "")) > 500
                    else data.get("transcript", ""),
                "error": data.get("error") if not data.get("success") else None,
            }
        except Exception as e:
            return {"error": str(e)}

    @router.post("/api/test-research")
    async def test_research(query: str = Form("What is machine learning?")) -> Dict[str, Any]:
        try:
            endpoint = f"http://{DEFAULT_HOST}:8000/v1/chat/completions"
            model = "gpt-oss-120b"
            result = await research_handler.call_research_service(query, endpoint, model)
            return {
                "status": "success",
                "query": query,
                "result_preview": result[:200] + "..." if len(result) > 200 else result,
                "result_length": len(result),
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "query": query}

    return router
