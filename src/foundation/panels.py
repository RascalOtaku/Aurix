"""src/foundation/panels.py - the dashboard's "see it" panels: your games, paper trading, lessons and the self-check detail.

Pure reads of records that already exist (the PC's fact report, the trading agent's report, lesson files, eval history). Nothing here writes,
changes or asks anything; every function returns plain, small, JSON-safe data that the page draws with textContent.
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from src.foundation import audit  # noqa: F401  (kept so the module is importable in the same shape as its siblings)


def games() -> Dict[str, Any]:
    """Every game the PC reported: size, mod health, recent crashes, and whether a fix is waiting."""
    from src.foundation import gaming
    rep = gaming.load_report()
    if not rep:
        return {"age_h": None, "rows": [], "libraries": [], "note": "No report from your PC yet."}
    pending = {p.get("appid") for p in gaming.pending()}
    rows: List[Dict[str, Any]] = []
    for g in rep.get("games", []):
        pl = g.get("plugins")
        bad = len(gaming.broken_plugins(pl)) if pl else 0
        crashes = [x for x in g.get("crash_events", []) if x.get("when")]
        recent = 0
        for x in crashes:
            try:
                if time.time() - datetime.fromisoformat(x["when"]).timestamp() <= 14 * 86400:
                    recent += 1
            except (ValueError, TypeError):
                pass
        state = "bad" if bad else ("warn" if recent or str(g.get("appid")) in pending else "ok")
        rows.append({"name": g.get("name", "?"), "appid": str(g.get("appid", "")), "size_gb": g.get("size_gb"), "state": state,
                     "plugins": (len(pl.get("enabled", [])) if pl else None), "broken": bad, "crashes": recent, "fix_waiting": str(g.get("appid")) in pending,
                     "known": bool(g.get("known", True))})
    rows.sort(key=lambda r: ({"bad": 0, "warn": 1, "ok": 2}[r["state"]], r["name"].lower()))
    age = (time.time() - float(rep.get("generated_ts", 0))) / 3600 if rep.get("generated_ts") else None
    return {"age_h": round(age, 1) if age is not None else None, "rows": rows,
            "libraries": [{"path": l.get("path"), "free_gb": l.get("free_gb"), "total_gb": l.get("total_gb")} for l in rep.get("libraries", [])], "note": ""}


def trading() -> Dict[str, Any]:
    """Paper trading at a glance: the lifeforce bar, how it does against SPY, what it holds."""
    from src.foundation import qol
    rt = Path(os.environ.get("AURIX_BRAIN", "/aurix")) / "runtime"
    rep = qol._trade_report(rt)
    if not rep:
        return {"have": False}
    pos = [{"symbol": str(p.get("symbol", "?")), "core": bool(p.get("core")), "pnl_pct": p.get("pnl_pct"), "qty": p.get("qty"), "entry": p.get("entry")} for p in rep.get("open_positions", [])][:30]
    return {"have": True, "dead": bool(rep.get("dead")), "killed": bool(rep.get("killed")), "lifeforce_pct": rep.get("lifeforce_pct"),
            "return_pct": rep.get("return_pct"), "spy_return_pct": rep.get("spy_return_pct"), "excess_pct": rep.get("excess_pct"),
            "drawdown_pct": rep.get("drawdown_from_peak_pct"), "dies_at_pct": rep.get("dies_at_drawdown_pct"), "equity": rep.get("equity"),
            "start": rep.get("bankroll_start"), "trades": rep.get("trades_closed"), "win_rate_pct": rep.get("win_rate_pct"),
            "last_scan": str(rep.get("last_scan") or "")[:16].replace("T", " "), "positions": pos}


def lessons() -> List[Dict[str, Any]]:
    from src.foundation import teacher
    out = []
    for les in teacher.all_lessons():
        out.append({"id": les.get("id"), "title": str(les.get("title", ""))[:90], "status": les.get("status", "?"), "diagnosis": str(les.get("diagnosis", ""))[:160]})
    order = {"pending": 0, "active": 1, "denied": 2, "retired": 3}
    return sorted(out, key=lambda x: order.get(x["status"], 9))[:12]


def checks() -> Dict[str, Any]:
    """The latest self-check exam, per tier, with the names of what failed (so a red number has a reason next to it)."""
    from src.foundation import evals
    hist = evals.load_history(20)
    if not hist:
        return {"have": False}
    tiers: Dict[str, Any] = {}
    for h in reversed(hist):                             # a code-only run must not hide the planning result from an earlier run
        for k, v in h.get("tiers", {}).items():
            tiers.setdefault(k, {"passed": v.get("passed"), "total": v.get("total", 0), "failed": list(v.get("failed", []))[:6], "ts": str(h.get("ts", ""))[:16].replace("T", " ")})
    return {"have": True, "ts": str(hist[-1].get("ts", ""))[:16].replace("T", " "), "tiers": tiers}
