"""src/foundation/command_center.py - everything the owner wants on one page, as one JSON document.

Reuses the system snapshot (organs, missions, standing, approvals, audit events) and adds what a "home screen" needs:
the agent's level/XP, memory + knowledge-graph numbers, the graph's knowledge gaps (pages the wiki links to that do not
exist yet - a ready-made research queue), forged skills, and links to the other views.

Every section is computed independently and fails soft: a stopped Neo4j or an unreachable RAG store blanks its own card
and nothing else. Probes are injectable so the whole thing is unit-tested without a network.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.foundation import mission as ms
from src.foundation import sysview, xp


def _count_md(directory: Path, cap: int = 5000) -> int:
    n = 0
    try:
        for _root, _dirs, files in os.walk(directory):
            n += sum(1 for f in files if f.lower().endswith(".md"))
            if n >= cap:
                return n
    except OSError:
        return 0
    return n


def probe_memory() -> Dict[str, Any]:
    """Numbers about the memory stack. Each store is optional; missing ones are simply absent."""
    out: Dict[str, Any] = {}
    brain = Path(os.environ.get("AURIX_BRAIN", "/aurix"))
    if (brain / "wiki").is_dir():
        out["wiki_pages"] = _count_md(brain / "wiki")
    if (brain / "memory" / "longterm").is_dir():
        out["longterm_notes"] = _count_md(brain / "memory" / "longterm")
    try:
        from src.rag_singleton import get_rag_manager
        rag = get_rag_manager()
        if rag is not None:
            out["rag_chunks"] = int(rag._collection.count())
    except Exception:
        pass
    try:
        from src.graph_memory import get_graph
        g = get_graph()
        if g is not None:
            rows = g.read("MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n")
            if rows is not None:
                counts = {r["label"]: r["n"] for r in rows}
                links = g.read("MATCH ()-[r:LINKS_TO]->() RETURN count(r) AS n")
                out["graph"] = {"pages": counts.get("Page", 0), "chunks": counts.get("Chunk", 0),
                                "tags": counts.get("Tag", 0), "links": (links or [{"n": 0}])[0]["n"]}
                gaps = g.read("MATCH (s:Page {stub:true})<-[:LINKS_TO]-(p:Page) "
                              "RETURN s.title AS title, count(p) AS refs ORDER BY refs DESC, title LIMIT 8")
                out["gaps"] = [{"title": r["title"], "refs": r["refs"]} for r in (gaps or [])]
    except Exception:
        pass
    return out


def forge_dir() -> Path:
    return ms.data_dir() / "forge"


_SKILL_ORDER = {"pending": 0, "active": 1, "draft": 2, "retired": 3, "denied": 4}


def probe_skills() -> Dict[str, Any]:
    """Forged skills on disk: data/forge/<name>/skill.json (written by the skill forge; empty until one exists).

    Skills waiting on the owner come first, then active ones; the page shows an Approve/Deny button on `pending`."""
    import json
    skills: List[Dict[str, Any]] = []
    d = forge_dir()
    try:
        for meta in sorted(d.glob("*/skill.json")):
            try:
                raw = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            tests = raw.get("test_result") if isinstance(raw.get("test_result"), dict) else {}
            skills.append({"name": raw.get("name", meta.parent.name), "status": raw.get("status", "unknown"),
                           "description": str(raw.get("description", ""))[:120], "uses": int(raw.get("uses", 0) or 0),
                           "tests_ran": int(tests.get("ran", 0) or 0), "attempts": int(raw.get("attempts", 0) or 0),
                           "created": str(raw.get("created", ""))[:16].replace("T", " ")})
    except OSError:
        pass
    skills.sort(key=lambda s: _SKILL_ORDER.get(s["status"], 5))
    return {"total": len(skills), "active": sum(1 for s in skills if s["status"] == "active"),
            "pending": sum(1 for s in skills if s["status"] == "pending"), "items": skills[:20]}


_TAG = re.compile(r"<[^>]+>")


def skill_detail(name: str) -> Optional[Dict[str, Any]]:
    """Everything the owner should read before approving a skill (its code and tests), or None."""
    from src.foundation import forge
    s = forge.load(name)
    if s is None:
        return None
    return {"name": s["name"], "status": s["status"], "description": s.get("description", ""), "wanted": s.get("wanted", ""),
            "code": s["code"], "tests": s["tests"], "sha256": str(s.get("sha256", ""))[:12], "uses": s.get("uses", 0),
            "integrity_ok": forge.integrity_ok(s), "test_result": s.get("test_result", {})}


SKILL_VERBS = {"approve": ("approve", "active"), "deny": ("deny", "denied"), "retire": ("retire", "retired")}


def skill_action(name: str, verb: str) -> Dict[str, Any]:
    """approve | deny | retire a skill through the forge's own checks (hash pinning, status rules).
    The route that calls this requires a real logged-in admin plus the confirm header; this only maps verbs."""
    from src.foundation import forge
    if verb not in SKILL_VERBS or not forge.NAME_RE.match(name or ""):
        return {"ok": False, "message": "unknown skill action"}
    fn = getattr(forge, SKILL_VERBS[verb][0])
    message = _TAG.sub("", fn(name))
    s = forge.load(name)
    return {"ok": bool(s and s["status"] == SKILL_VERBS[verb][1]), "message": message, "status": s["status"] if s else None}


def probe_tasks(fetch_scheduled: Optional[Callable[[], List[Dict[str, Any]]]] = None) -> Dict[str, Any]:
    """Open to-dos, live projects (the unfinished-projects registry) and Odysseus's own scheduled tasks."""
    from src.foundation import projects
    out: Dict[str, Any] = {"todos": [], "projects": [], "scheduled": []}
    try:
        data = projects.Registry().load()
        todos = sorted((t for t in data["todos"] if not t.done), key=lambda t: (t.due or "9999", t.created))
        out["todos"] = [{"id": t.id, "text": t.text[:120], "due": t.due, "project": t.project} for t in todos[:10]]
        out["todo_total"] = len(todos)
        live = [p for p in data["projects"] if p.status != "done"]
        out["projects"] = [{"id": p.id, "name": p.name[:80], "note": p.note[:100], "status": p.status} for p in live[:8]]
        out["project_total"] = len(live)
    except Exception:
        pass
    try:
        out["scheduled"] = (fetch_scheduled or _fetch_scheduled_tasks)()[:10]
    except Exception:
        pass
    return out


def _fetch_scheduled_tasks() -> List[Dict[str, Any]]:
    """Odysseus's recurring/one-off scheduled tasks (its own database), soonest first."""
    from core.database import ScheduledTask, SessionLocal
    db = SessionLocal()
    try:
        rows = db.query(ScheduledTask).filter(ScheduledTask.status.in_(["active", "paused"])).all()
        items = [{"name": str(r.name)[:70], "status": r.status, "schedule": r.cron_expression or r.schedule or r.trigger_type or "",
                  "next_run": r.next_run.strftime("%Y-%m-%d %H:%M") if r.next_run else "",
                  "last_run": r.last_run.strftime("%Y-%m-%d %H:%M") if r.last_run else "", "runs": int(r.run_count or 0)}
                 for r in rows]
    finally:
        db.close()
    items.sort(key=lambda i: (i["status"] != "active", i["next_run"] or "9999"))
    return items


# name, host env var, default host (the compose service name), port, what it does. TCP connect only: fast, side-effect free.
PEERS = [
    ("Sandbox gateway", "SANDBOX_GATEWAY_HOST", "sandbox-gateway", 8765, "isolated code execution"),
    ("LLM gateway", "LLM_GATEWAY_HOST", "llm-gateway", 11434, "inference-only model proxy for the sandbox"),
    ("ChromaDB", "CHROMA_HOST", "chromadb", 8000, "vector memory"),
    ("Neo4j", "NEO4J_HOST", "neo4j", 7687, "knowledge graph"),
    ("SearXNG", "SEARXNG_HOST", "searxng", 8080, "private web search"),
    ("ntfy", "NTFY_HOST", "ntfy", 80, "push notifications"),
]
# God's Eye is deliberately NOT in this list: it runs on its own isolated network (odysseus_gods_eye_net), so the app can
# never resolve `gods-eye`. It is checked at the published address the owner actually browses to (see probe_services).


def tcp_up(host: str, port: int, timeout: float = 1.0) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _host_port(url: str):
    from urllib.parse import urlsplit
    try:
        u = urlsplit(url)
        return (u.hostname, u.port or (443 if u.scheme == "https" else 80)) if u.hostname else None
    except ValueError:
        return None


def probe_services(raw: Dict[str, Any], check: Optional[Callable[[str, int], bool]] = None,
                   godseye_url: str = "") -> List[Dict[str, Any]]:
    """One row per thing that should be running: the app's own loops, the model endpoints, the sandbox, and the peer containers.
    `godseye_url` (the address the owner browses to) adds the God's Eye row; without it the row is left out rather than guessed."""
    from concurrent.futures import ThreadPoolExecutor
    check = check or tcp_up                                   # resolved at call time so tests can swap it
    tg = raw.get("telegram") or {}
    sb = raw.get("sandbox") or {}
    rows: List[Dict[str, Any]] = [
        {"name": "Odysseus app", "ok": True, "detail": "this page is served by it"},
        {"name": "Telegram listener", "ok": bool(tg.get("listener_alive")),
         "detail": "receiving your commands" if tg.get("listener_alive") else ("not running" if tg.get("configured") else "not configured")},
        {"name": "Standing scheduler", "ok": bool(tg.get("scheduler_alive")), "detail": "ticking about once a minute" if tg.get("scheduler_alive") else "not running"},
        {"name": "Mission sandbox", "ok": bool(sb.get("available")),
         "detail": (f"{sb.get('tools_present')}/{sb.get('tools_total')} tools" + (", OpenHands" if sb.get("openhands") else "")) if sb.get("available") else "unreachable"},
    ]
    eps = raw.get("endpoints") if isinstance(raw.get("endpoints"), list) else []
    for e in eps:
        loaded = ", ".join(f"{m['name']} ({m['vram_gb']} GB VRAM)" for m in e.get("loaded", []))
        rows.append({"name": f"Model: {e.get('name')}", "ok": bool(e.get("ok")),
                     "detail": (loaded or ("up, nothing loaded" if e.get("models") is None else f"{e.get('models')} models")) if e.get("ok") else "DOWN"})
    peers = [(n, os.environ.get(h, dh), p, d) for n, h, dh, p, d in PEERS]
    eye = _host_port(godseye_url)
    if eye:
        peers.append(("God's Eye", eye[0], eye[1], "3D globe viewer"))
    def is_up(peer) -> bool:
        try:
            return bool(check(peer[1], peer[2]))
        except Exception:                                     # a check that blows up means "down", not "hide the whole card"
            return False
    with ThreadPoolExecutor(max_workers=6) as pool:
        ups = list(pool.map(is_up, peers))
    rows += [{"name": n, "ok": up, "detail": d if up else "not answering"} for (n, _h, _p, d), up in zip(peers, ups)]
    return rows


# Other homelab dashboards/services worth one click from here. A retired machine's "Homepage" dashboard (port 3000 in the
# Homelab Ecosystem Plan) was the owner's overview of every service; it lived on another machine, so it was wired as a link
# with a live reachability dot. Override with AURIX_HOMELAB_LINKS="Name|http://host:port;Name 2|http://host2:port".
HOMELAB_DEFAULT = ("GPU box (Windows PC, Ollama)|http://100.64.0.10:11434|quiet;"
                   "Jellyfin (Pi)|http://10.0.0.75:8096;Pi-hole (Pi)|http://10.0.0.75/admin;"
                   "Vaultwarden (server)|http://100.64.0.20:8080;Uptime Kuma (server)|http://100.64.0.20:3001")
# Example addresses only - set AURIX_HOMELAB_LINKS in .env to your own. The Pi is on the LAN only (not on Tailscale),
# so its links open at home; the reachability check runs from wherever the app itself is hosted, on the same LAN.
# "quiet" = shown and checked, but never messaged about: a machine that sleeps at night must not page anyone.
MAX_HOMELAB_LINKS = 8


def parse_homelab_links(spec: Optional[str] = None) -> List[Dict[str, str]]:
    """[{name, url, host, port, quiet}] from 'Name|url[|quiet];Name|url'. Only http(s) URLs with a host survive: the page turns
    these into <a href>, so a 'javascript:' or 'data:' value must never get through, even from our own configuration.
    The optional third field `quiet` keeps a link on the dashboard but exempts it from watchdog messages."""
    from urllib.parse import urlsplit
    text = (os.environ.get("AURIX_HOMELAB_LINKS") if spec is None else spec)
    text = HOMELAB_DEFAULT if text is None else text
    out: List[Dict[str, Any]] = []
    for part in text.split(";"):
        name, sep, rest = part.partition("|")
        url, _, flags = rest.partition("|")
        quiet = flags.strip().lower() == "quiet"
        name, url = re.sub(r"\s+", " ", name).strip()[:60], url.strip()
        try:
            u = urlsplit(url)
            port = u.port                                     # raises on a junk port
        except ValueError:
            continue
        if not sep or not name or u.scheme not in ("http", "https") or not u.hostname or any(c in url for c in " \t\r\n<>\"'`"):
            continue
        out.append({"name": name, "url": url, "host": u.hostname, "port": str(port or (443 if u.scheme == "https" else 80)), "quiet": quiet})
        if len(out) >= MAX_HOMELAB_LINKS:
            break
    return out


def probe_homelab(check: Optional[Callable[[str, int], bool]] = None, links: Optional[List[Dict[str, str]]] = None) -> List[Dict[str, Any]]:
    """Each homelab link with whether it answers right now (TCP only: fast and side-effect free)."""
    from concurrent.futures import ThreadPoolExecutor
    cacheable = check is None and links is None               # the page polls every few seconds; an offline peer costs a full timeout
    check = check or tcp_up
    links = parse_homelab_links() if links is None else links
    key = tuple((l["host"], l["port"], l["name"]) for l in links)
    if cacheable and _homelab_cache["key"] == key and time.time() - _homelab_cache["at"] < HOMELAB_CACHE_SECONDS:
        return _homelab_cache["value"]

    def one(link: Dict[str, str]) -> Dict[str, Any]:
        try:
            up = bool(check(link["host"], int(link["port"])))
        except Exception:
            up = False
        return {"name": link["name"], "url": link["url"], "ok": up, "quiet": bool(link.get("quiet")),
                "detail": "answering" if up else "not answering" + (" (asleep or off? not alerted)" if link.get("quiet") else "")}
    if not links:
        return []
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(one, links))
    if cacheable:
        _homelab_cache.update(key=key, at=time.time(), value=result)
    return result


HOMELAB_CACHE_SECONDS = 15.0
_homelab_cache: Dict[str, Any] = {"key": None, "at": 0.0, "value": []}


SERVER_KEYS = ("cpu_pct", "mem_pct", "mem_gb", "swap_pct", "swap_gb", "disk_pct", "disk_free_gb", "disk_total_gb", "disk_used_gb",
               "uptime_h", "load1")
# thresholds the page colours by; they are the watchdog's own levels, so red here means "you are being paged"
from src.foundation import watchdog as _wd  # noqa: E402
SERVER_WARN = {"cpu_pct": 90.0, "mem_pct": 85.0, "swap_pct": 70.0, "disk_pct": _wd.DISK_WARN_PCT}
SERVER_BAD = {"cpu_pct": 98.0, "mem_pct": _wd.MEM_PCT_LIMIT, "swap_pct": _wd.SWAP_PCT_LIMIT, "disk_pct": _wd.DISK_PCT_LIMIT}


def server_stats(raw: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    """The box's own vitals (CPU, memory, disk the way `df` counts it, uptime) from the probe the health check already ran,
    plus the state of the nightly backup when backups are set up."""
    sysd = raw.get("system") or {}
    out: Dict[str, Any] = {k: sysd[k] for k in SERVER_KEYS if isinstance(sysd.get(k), (int, float))}
    out["warn"], out["bad"] = dict(SERVER_WARN), dict(SERVER_BAD)
    status = _wd.read_backup_status()
    if status is not None:
        age = _wd.backup_age_hours(status, now or time.time())
        out["backup"] = {"ok": status.get("ok") is not False, "age_h": None if age is None else round(age, 1),
                         "message": str(status.get("message", ""))[:160], "stale_h": _wd.BACKUP_STALE_HOURS}
    return out


def running_now(base: Dict[str, Any], raw: Dict[str, Any]) -> Dict[str, Any]:
    """What is actively working right now: the active mission and which models are loaded in memory."""
    mission = next((m for m in base["missions"] if m["status"] == "active"), None)
    models = [{"endpoint": e.get("name"), "model": m["name"], "vram_gb": m["vram_gb"]}
              for e in (raw.get("endpoints") if isinstance(raw.get("endpoints"), list) else []) for m in e.get("loaded", [])]
    tg = raw.get("telegram") or {}
    return {"mission": mission, "models": models, "in_flight": tg.get("in_flight", 0),
            "standing_active": sum(1 for s in base["standing"] if s["status"] == "active")}


def default_probes() -> Dict[str, Callable[[], Any]]:
    return {"memory": probe_memory, "skills": probe_skills, "tasks": probe_tasks}


def snapshot(system_probes: Optional[Dict[str, Callable[[], Any]]] = None,
             extra_probes: Optional[Dict[str, Callable[[], Any]]] = None, now: Optional[float] = None,
             godseye_url: str = "", kuma_url: str = "",
             extra_links: Optional[Dict[str, str]] = None,
             service_check: Optional[Callable[[str, int], bool]] = None) -> Dict[str, Any]:
    now = now or time.time()
    raw: Dict[str, Any] = {}                                  # the probe results behind the organs (services + running-now)
    base = sysview.snapshot(system_probes, now=now, raw=raw)
    extra = extra_probes if extra_probes is not None else default_probes()

    def safe(name, fn, default):
        try:
            return fn()
        except Exception:
            return default

    state = safe("xp", xp.summary, None)
    from src.foundation import actions, addons, experience, growth, panels, repos
    from src.foundation import memory as fmemory
    from src.foundation import upgrades
    from src.foundation import money
    from src.foundation import gamepilot
    from src.foundation import shards
    from src.foundation import nightshift
    from src.foundation import transcription
    from src.foundation import earnings
    from src.foundation import freelance
    from src.foundation import content
    from src.foundation import learning
    from src.foundation import subscriptions
    addon_list = safe("addons", addons.probe_addons, [])
    running_state = safe("running", lambda: running_now(base, raw), {"mission": None, "models": [], "in_flight": 0, "standing_active": 0})
    homelab_rows = safe("homelab", lambda: probe_homelab(service_check), [])
    organs = [{"key": k, "title": o["title"], "status": o["status"], "sub": o.get("sub", ""), "detail": o.get("detail", "")} for k, o in base["organs"].items()]
    worst = "ok"
    for o in organs:
        if o["status"] == "bad":
            worst = "bad"
        elif o["status"] == "warn" and worst != "bad":
            worst = "warn"
    return {
        "ts": now, "local_time": base["local_time"],
        "xp": state.as_dict() if state is not None else None,
        "health": {"overall": worst, "organs": organs, "audit_ok": base["audit"]["ok"], "stop": base["stop"]},
        "approvals": base["approvals"], "missions": base["missions"], "active": base["active"],
        "standing": base["standing"], "events": base["events"][:14],
        "memory": safe("memory", extra.get("memory", lambda: {}), {}),
        "skills": safe("skills", extra.get("skills", lambda: dict(_NO_SKILLS)), dict(_NO_SKILLS)),
        "tasks": safe("tasks", extra.get("tasks", lambda: dict(_NO_TASKS)), dict(_NO_TASKS)),
        "services": safe("services", lambda: probe_services(raw, service_check, godseye_url), []),
        "homelab": homelab_rows,
        "addons": addon_list,
        "alive": safe("alive", lambda: growth.probe_running(running_state, base["organs"]), []),
        "feed": safe("feed", lambda: growth.activity_feed(14), []),
        "growth": safe("growth", growth.growth_stats, {}),
        "timeline": safe("timeline", lambda: growth.timeline(60), []),
        "trust": safe("trust", growth.trust, {}),
        "learning": safe("learning", experience.stats, {}),
        "decisions": safe("decisions", actions.decisions, []),
        "background": safe("background", actions.background, {}),
        "games": safe("games", panels.games, {"rows": []}),
        "repos": safe("repos", repos.panel, []),
        "mind": safe("mind", fmemory.panel, {"total": 0, "rows": [], "pending": []}),
        "upgrades": safe("upgrades", upgrades.panel, {"enabled": False, "items": []}),
        "money": safe("money", money.panel, {"lab": None, "items": []}),
        "gamepilot": safe("gamepilot", gamepilot.panel, {"pc_online": False, "paired": 0, "armed": False}),
        "shards": safe("shards", shards.panel, []),
        "nightshift": safe("nightshift", nightshift.panel, {}),
        "transcription": safe("transcription", transcription.panel, {}),
        "earnings": safe("earnings", earnings.panel, {}),
        "freelance": safe("freelance", freelance.panel, {}),
        "content": safe("content", content.panel, {}),
        "library": safe("library", learning.panel, {}),
        "subscriptions": safe("subscriptions", subscriptions.panel, {}),
        "trading": safe("trading", panels.trading, {"have": False}),
        "lessons": safe("lessons", panels.lessons, []),
        "checks": safe("checks", panels.checks, {"have": False}),
        "monitors": safe("monitors", lambda: addons.probe_monitors(organs, homelab_rows, addon_list), []),
        "server": safe("server", lambda: server_stats(raw), {}),
        "running": safe("running", lambda: running_now(base, raw), {"mission": None, "models": [], "in_flight": 0, "standing_active": 0}),
        "links": {"godseye": godseye_url, "systems": "/systems", "chat": "/", "kuma": kuma_url, **(extra_links or {})},
    }


_NO_SKILLS = {"total": 0, "active": 0, "pending": 0, "items": []}
_NO_TASKS = {"todos": [], "projects": [], "scheduled": []}


# ---------------------------------------------------------------------------
# who may decide approvals from the web
# ---------------------------------------------------------------------------

APPROVAL_ID_RE = re.compile(r"[0-9a-f]{6}\Z")          # \Z, not $: "$" also matches before a trailing newline


def human_admin_ok(has_internal_token: bool, current_user: Optional[str], auth_configured: bool, is_admin: bool) -> bool:
    """May this request decide an approval? Only a REAL logged-in admin.

    The app's generic `require_admin` also admits the internal loopback token used by the agent's own tools (and
    AUTH_ENABLED=false). If approvals honoured that, an agent could approve its own pending action through the web
    API, which would collapse the whole approval gate. So: no internal token, an authenticated non-internal user,
    auth actually configured, and that user is an admin."""
    if has_internal_token:
        return False
    if not current_user or current_user == "internal-tool":
        return False
    return bool(auth_configured and is_admin)


def parse_decision(header_value: str) -> Optional[str]:
    """'APPROVE' / 'DENY' (the X-AURIX-Confirm header) -> 'approve' / 'deny'; anything else -> None."""
    v = (header_value or "").strip().upper()
    return {"APPROVE": "approve", "DENY": "deny"}.get(v)
