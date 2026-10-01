"""src/foundation/sysview.py - AURIX Systems: one live snapshot of ALL of AURIX.

Maps the real subsystems onto the body-system metaphor from the activation handoff (section 6):
skeletal=identity, nervous=input/scheduling, brain=models, circulatory=resources, immune=gate+audit,
muscular=sandbox/coding agent, memory=stores, endocrine=schedules/digest. `snapshot()` returns
plain JSON for the dashboard (static/sysview.html); every organ is computed independently, so one
failing probe greys out that organ instead of breaking the page.

Read-only, except do_stop(), which is exactly the owner's `stop` command.
Probes are injectable (tests use fakes); live_probes() wires the real ones with tight timeouts.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.foundation import audit, identity
from src.foundation import mission as ms
from src.foundation import standing as st

OK, WARN, BAD, IDLE = "ok", "warn", "bad", "idle"
_verify_cache: Dict[str, Any] = {"key": None, "at": 0.0, "value": None}


def _safe(fn: Callable[[], Any], default: Any) -> Any:
    try:
        return fn()
    except Exception as e:                                   # one broken probe must not blank the page
        return default if not isinstance(default, dict) else {**default, "error": f"{type(e).__name__}: {e}"[:160]}


def _verify_cached(max_age: float = 20.0):
    """audit.verify() walks the whole file; the page polls every few seconds, so cache it."""
    path = audit.audit_path()
    try:
        s = path.stat()
        key = (str(path), s.st_size, s.st_mtime_ns)
    except OSError:
        key = (str(path), 0, 0)
    now = time.time()
    if _verify_cache["key"] == key and now - _verify_cache["at"] < max_age:
        return _verify_cache["value"]
    value = audit.verify()
    _verify_cache.update(key=key, at=now, value=value)
    return value


def render_page(template: str, nonce: str) -> str:
    """The app's Content-Security-Policy only runs inline scripts that carry the per-request nonce
    (script-src 'nonce-...'); static pages mark theirs with {{CSP_NONCE}} and the server fills it in
    (app._serve_html_with_nonce). Without this the page renders its shell and never fetches data."""
    return (template or "").replace("{{CSP_NONCE}}", nonce or "")


def dir_size_mb(path: Path, limit_files: int = 20000) -> float:
    total, n = 0, 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                n += 1
                if n > limit_files:
                    return round(total / 1e6, 1)
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return round(total / 1e6, 1)


# ---------------------------------------------------------------------------
# live probes (each returns a small dict; never raises)
# ---------------------------------------------------------------------------

def disk_usage(path) -> dict:
    """Disk numbers the way `df` reports them: percent = used / (used + available to us).

    NOT used / total: ext4 reserves ~5% for root, so on a nearly full disk used/total can read 90% while `df` (and the
    space we can really write) says 96%. That gap once hid a 96%-full disk from the 92% watchdog alert."""
    du = shutil.disk_usage(str(path))
    denom = du.used + du.free
    return {"total_gb": round(du.total / 1e9, 1), "used_gb": round(du.used / 1e9, 1), "free_gb": round(du.free / 1e9, 1),
            "pct": round(du.used / denom * 100, 1) if denom else 0.0}


def probe_system() -> dict:
    out: Dict[str, Any] = {}
    try:
        import psutil
        out["cpu_pct"] = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        out["mem_pct"], out["mem_gb"] = vm.percent, round(vm.total / 1e9, 1)
        sw = psutil.swap_memory()
        if sw.total:
            out["swap_pct"], out["swap_gb"] = sw.percent, round(sw.total / 1e9, 1)
        out["uptime_h"] = round((time.time() - psutil.boot_time()) / 3600, 1)
    except Exception:
        pass
    try:
        d = disk_usage(ms.data_dir() if ms.data_dir().exists() else "/")
        out["disk_pct"], out["disk_free_gb"] = d["pct"], d["free_gb"]
        out["disk_total_gb"], out["disk_used_gb"] = d["total_gb"], d["used_gb"]
    except OSError:
        pass
    try:
        out["load1"] = round(os.getloadavg()[0], 2)
    except (AttributeError, OSError):
        pass
    return out


def _ollama_root(base_url: str) -> str:
    u = (base_url or "").strip().rstrip("/")
    for suffix in ("/v1/chat/completions", "/chat/completions", "/v1"):
        if u.endswith(suffix):
            u = u[: -len(suffix)]
    return u


def _probe_one_endpoint(ep: dict, timeout: float = 2.0) -> dict:
    root = _ollama_root(ep.get("base_url", ""))
    res = {"name": ep.get("name") or ep.get("id"), "url": root, "ok": False, "loaded": [], "models": None}
    if not root.lower().startswith(("http://", "https://")):      # urllib would also open file:/ftp: URLs
        return res
    try:
        with urllib.request.urlopen(root + "/api/ps", timeout=timeout) as r:
            data = json.loads(r.read())
        res["ok"] = True
        res["loaded"] = [{"name": m.get("name"), "vram_gb": round((m.get("size_vram") or 0) / 1e9, 1),
                          "size_gb": round((m.get("size") or 0) / 1e9, 1)} for m in data.get("models", [])]
        return res
    except Exception:
        pass
    try:                                                    # not Ollama: try the OpenAI-compatible listing
        with urllib.request.urlopen(root + "/v1/models", timeout=timeout) as r:
            data = json.loads(r.read())
        res["ok"], res["models"] = True, len(data.get("data", []))
    except Exception:
        pass
    return res


def probe_endpoints() -> List[dict]:
    from core.database import ModelEndpoint, SessionLocal
    db = SessionLocal()
    try:
        eps = [{"id": e.id, "name": e.name, "base_url": e.base_url} for e in
               db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True).all()]  # noqa: E712
    finally:
        db.close()
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(_probe_one_endpoint, eps))


def probe_sandbox() -> dict:
    from src.foundation import capabilities as cap
    from src.foundation import sandbox
    if not sandbox.available():
        return {"available": False}
    bins = sorted({c.check for c in cap.REGISTRY.values() if c.kind == "binary"})
    mods = sorted({c.check for c in cap.REGISTRY.values() if c.kind == "python_pkg"})
    found = sandbox.probe(bins, mods)
    have_b = sum(1 for v in found.get("bins", {}).values() if v)
    have_m = sum(1 for v in found.get("modules", {}).values() if v)
    return {"available": True, "tools_present": have_b + have_m, "tools_total": len(bins) + len(mods),
            "openhands": bool(found.get("modules", {}).get("openhands.sdk")),
            "missing": sorted([b for b, v in found.get("bins", {}).items() if not v]
                              + [m for m, v in found.get("modules", {}).items() if not v])[:12]}


def probe_telegram() -> dict:
    try:
        from services.telegram import listener as tl
        obj = getattr(tl, "_listener_singleton", None)
    except Exception:
        obj = None
    if obj is None:
        return {"configured": bool(os.environ.get("TELEGRAM_BOT_TOKEN")), "listener_alive": False,
                "scheduler_alive": False}
    last = getattr(obj, "_last_poll_ok", None)
    return {"configured": True, "listener_alive": bool(obj._task and not obj._task.done()),
            "scheduler_alive": bool(obj._sched_task and not obj._sched_task.done()),
            "in_flight": len(obj._in_flight),
            "last_poll_age_s": None if last is None else round(time.time() - last, 1)}


TELEGRAM_READY_MAX_POLL_AGE_S = 180.0     # a healthy long-poll (25 s) answers far more often than this


def telegram_readiness(probe: Optional[dict] = None, env: Optional[dict] = None) -> tuple:
    """(ready, body) for the deploy readiness check. Honest by construction: READY means the listener task is running AND Telegram
    itself answered one of its polls recently. If Telegram is not configured at all there is nothing to check, and that is said.
    The body holds only booleans and an age - no token, chat id or message content."""
    env = os.environ if env is None else env
    configured = bool(env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID")) and \
        env.get("TELEGRAM_ENABLED", "true").lower() == "true"
    if not configured:
        return True, {"ready": True, "telegram": "not configured", "checked": False}
    p = probe_telegram() if probe is None else probe
    age = p.get("last_poll_age_s")
    alive = bool(p.get("listener_alive"))
    polled = age is not None and age <= TELEGRAM_READY_MAX_POLL_AGE_S
    reason = "ok" if alive and polled else ("listener task not running" if not alive else
                                            "no successful Telegram poll yet" if age is None else f"last good poll {age:.0f}s ago")
    return alive and polled, {"ready": alive and polled, "telegram": reason, "checked": True,
                              "listener_alive": alive, "last_poll_age_s": age}


def probe_governor() -> dict:
    """The resource governor lives in the root repo's cortex/ (not always importable from the
    odysseus container); fall back to the shared mode file, then to 'n/a'."""
    try:
        from cortex.resource_governor import get_mode
        return {"mode": get_mode()}
    except Exception:
        pass
    for root in (os.environ.get("AURIX_PROJECT_ROOT", "/app"), "/app", "/aurix"):
        try:
            data = json.loads((Path(root) / "runtime" / "resource_mode.json").read_text(encoding="utf-8"))
            return {"mode": data.get("mode", "n/a")}
        except (OSError, ValueError):
            continue
    return {"mode": "n/a"}


def live_probes() -> Dict[str, Callable[[], Any]]:
    return {"system": probe_system, "endpoints": probe_endpoints, "sandbox": probe_sandbox,
            "telegram": probe_telegram, "governor": probe_governor}


# ---------------------------------------------------------------------------
# the snapshot
# ---------------------------------------------------------------------------

def _worst(*statuses: str) -> str:
    order = {IDLE: 0, OK: 1, WARN: 2, BAD: 3}
    return max(statuses, key=lambda s: order[s])


def _events(n: int = 25) -> List[dict]:
    out = []
    for rec in audit.recent(n):
        subject = rec.get("mission") or rec.get("standing") or rec.get("tool") or rec.get("id") or rec.get("name") or ""
        detail = rec.get("preview") or rec.get("reason") or rec.get("objective") or rec.get("note") or ""
        out.append({"seq": rec.get("seq"), "ts": str(rec.get("ts", ""))[11:19], "event": rec.get("event", "?"),
                    "subject": str(subject)[:30], "detail": " ".join(str(detail).split())[:90]})
    return list(reversed(out))                               # newest first


def snapshot(probes: Optional[Dict[str, Callable[[], Any]]] = None, now: Optional[float] = None,
             raw: Optional[dict] = None) -> dict:
    """`raw`, if given, is filled with the probe results so a caller (the Command Center) can reuse them without probing
    twice. It is never part of the returned document, so /api/systems does not start exposing endpoint URLs."""
    probes = probes if probes is not None else live_probes()
    now = now or time.time()
    p = {k: _safe(fn, {}) for k, fn in probes.items()}
    p.setdefault("endpoints", [])
    if isinstance(p.get("endpoints"), dict):                # a failed endpoints probe returns {'error': ...}
        p["endpoints"] = []
    if raw is not None:
        raw.update(p)

    store, standing_store = ms.MissionStore(), st.StandingStore()
    missions = _safe(store.all, [])
    standing = _safe(standing_store.all, [])
    active = next((m for m in missions if m.status == ms.MissionStatus.ACTIVE), None)
    ident = _safe(identity.load_identity, None)

    try:
        from src import approval_gate as ag
        pending = [{"id": q.id, "tool": q.tool, "preview": " ".join(q.preview.split())[:120],
                    "age_s": int(now - q.created), "risk": q.risk[:80]} for q in ag._pending.values()]
        gate_mode, stop_on = ag.gate_mode(), ag.stop_engaged()
    except Exception:
        pending, gate_mode, stop_on = [], "unknown", False

    v = _safe(_verify_cached, None)
    seq, head = _safe(audit.head, (0, ""))
    refusals = sum(1 for r in audit.recent(2000) if r.get("reason") == "protected_component")
    chain_ok = bool(v and v.ok)

    # -- organs -------------------------------------------------------------------------------
    sys_ = p.get("system") or {}
    sys_status = _worst(OK, WARN if (sys_.get("mem_pct", 0) > 90 or sys_.get("disk_pct", 0) > 90) else OK)
    endpoints = p["endpoints"]
    any_ep = any(e.get("ok") for e in endpoints)
    tg = p.get("telegram") or {}
    sb = p.get("sandbox") or {}
    active_standing = [s for s in standing if s.status == st.StandingStatus.ACTIVE]

    organs = {
        "skeletal": {"title": "Skeleton", "sub": "identity & structure", "status": OK if ident else BAD,
                     "metrics": {"name": getattr(ident, "name", "?"), "owner": getattr(ident, "owner_name", "?"),
                                 "constitution": getattr(ident, "constitution_version", "?"),
                                 "protected components": len(identity.PROTECTED_COMPONENTS)}},
        "nervous": {"title": "Nervous system", "sub": "Telegram & schedulers",
                    "status": OK if tg.get("listener_alive") else (WARN if tg.get("configured") else IDLE),
                    "metrics": {"telegram listener": "alive" if tg.get("listener_alive") else "not running",
                                "standing scheduler": "alive" if tg.get("scheduler_alive") else "not running",
                                "in flight": tg.get("in_flight", 0)}},
        "brain": {"title": "Brain", "sub": "models & routing", "status": OK if any_ep else (BAD if endpoints else IDLE),
                  "metrics": {"governor mode": (p.get("governor") or {}).get("mode", "?"),
                              **{e["name"]: (("up, " + ", ".join(f"{m['name']} ({m['vram_gb']} GB VRAM)" for m in e["loaded"]))
                                             if e.get("ok") and e.get("loaded") else ("up, idle" if e.get("ok") else "DOWN"))
                                 for e in endpoints}}},
        "circulatory": {"title": "Circulation", "sub": "compute & resources", "status": sys_status,
                        "metrics": {k: v for k, v in (("cpu %", sys_.get("cpu_pct")), ("memory %", sys_.get("mem_pct")),
                                                       ("swap %", sys_.get("swap_pct")),
                                                       ("disk %", sys_.get("disk_pct")), ("disk free GB", sys_.get("disk_free_gb")),
                                                       ("load", sys_.get("load1")), ("uptime h", sys_.get("uptime_h"))) if v is not None}},
        "immune": {"title": "Immune system", "sub": "gate, risk & audit",
                   "status": BAD if not chain_ok else (WARN if stop_on else OK),
                   "metrics": {"gate mode": gate_mode, "STOP engaged": "YES" if stop_on else "no",
                               "waiting on you": len(pending),
                               "audit chain": ((f"OK, {v.records} records" + (f" (+{len(v.acknowledged)} acknowledged fork)" if v.acknowledged else ""))
                                               if chain_ok else "TAMPERING DETECTED"),
                               "audit head": f"#{seq} {head[:10]}", "protected-file attempts refused": refusals}},
        "muscular": {"title": "Muscles", "sub": "sandbox & coding agent",
                     "status": OK if sb.get("available") else WARN,
                     "metrics": ({"sandbox": "isolated, running", "tools": f"{sb.get('tools_present')}/{sb.get('tools_total')}",
                                  "OpenHands": "installed" if sb.get("openhands") else "not installed",
                                  **({"missing": ", ".join(sb["missing"])} if sb.get("missing") else {})}
                                 if sb.get("available") else {"sandbox": "UNREACHABLE"})},
        "memory": {"title": "Memory", "sub": "records & workspaces", "status": OK,
                   "metrics": {"missions": len(missions), "standing missions": len(standing),
                               "audit records": v.records if v else "?",
                               "workspaces MB": dir_size_mb(ms.data_dir() / "workspace")}},
        "endocrine": {"title": "Endocrine", "sub": "rhythm & digest", "status": OK if active_standing else IDLE,
                      "metrics": {"timezone": os.environ.get("AURIX_TIMEZONE") or "UTC (unset!)",
                                  "morning digest": os.environ.get("AURIX_DIGEST_AT") or "off",
                                  "standing active": len(active_standing),
                                  **{s.id: f"next {st.next_run_text(s, now)}" for s in active_standing[:4]}}},
    }
    return {
        "ts": now, "local_time": st.local_now(now).strftime("%a %b %d %H:%M:%S"), "organs": organs,
        "missions": [{"id": m.id, "status": m.status.value, "objective": m.objective[:90], "sandboxed": m.sandboxed,
                      "step": m.current_step, "steps": len(m.steps), "tool_calls": m.usage.tool_calls,
                      "current": (m.steps[m.current_step].title if m.current_step < len(m.steps) else "verifying")}
                     for m in sorted(missions, key=lambda x: -x.created_at)[:8]],
        "standing": [{"id": s.id, "status": s.status.value, "title": s.title[:60], "schedule": s.schedule.describe(),
                      "runs": s.runs, "failing": s.consecutive_failures} for s in standing[:8]],
        "approvals": pending, "events": _events(25),
        "audit": {"ok": chain_ok, "records": v.records if v else 0, "seq": seq, "head": head[:12]},
        "active": active.id if active else None, "stop": stop_on,
    }


async def do_stop() -> str:
    """Exactly the owner's `stop` command (mission, approvals, standing, sandbox processes, in-flight chat)."""
    try:
        from services.telegram import listener as tl
        obj = getattr(tl, "_listener_singleton", None)
    except Exception:
        obj = None
    if obj is not None:
        return await obj._foundation_obj().handle("stop")
    from src.foundation.commands import Foundation

    async def _noop(*_a, **_k):
        return ""
    return await Foundation(_noop, _noop).handle("stop")
