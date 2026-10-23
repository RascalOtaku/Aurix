"""economy/panel.py - the Economic Engine on the web command center (Money tab). Read-only: every decision stays a Telegram command,
where the sender is verified as the owner.

The command center polls every few seconds, so this never creates or seeds the database (an engine the owner has not opened shows a
one-line hint instead) and caches its result until the database file changes, at most CACHE_S seconds.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Tuple

from src.foundation.economy import ENGINE_VERSION, freedom, ledger, scorecard, store, validation

CACHE_S = 30.0
_cache: Dict[str, Tuple[Tuple[float, float], Dict[str, Any]]] = {}


def _build(now: float) -> Dict[str, Any]:
    c = freedom.compute(now)
    t = ledger.totals(now)
    built = store.one("SELECT COUNT(*) AS n FROM opportunities WHERE stage IN ('built', 'launched', 'measured', 'scaled', 'modified')")["n"]
    opps = []
    for r in scorecard.ranked():
        row = {"id": r["id"], "name": r["name"], "stage": r["stage"], "score": r["score"], "demand": r["demand"], "blocked": r["blocked"],
               "startup_cash": r["startup_cash"], "owner_hours": r["owner_hours"], "next_test": scorecard.next_test(r)}
        if r["stage"] == "researched":
            row["checklist"] = [{"label": label, "done": done} for label, done in validation.checklist(r)]
        tot = ledger.venture_totals(r["id"])
        if tot["collected"] or tot["costs"] or tot["hours"]:
            row["actual"] = {k: tot[k] for k in ("sales", "collected", "costs", "net", "delivery_per_hour", "per_hour")}
        opps.append(row)
    pending = [{"id": a["id"], "opp_id": a["opp_id"], "action": a["action"], "cost": a["cost"]}
               for a in store.rows("SELECT * FROM approvals WHERE status = 'pending' ORDER BY created")]
    return {"opened": True, "version": ENGINE_VERSION, "milestone": freedom.milestone(c), "placeholders": freedom.placeholders(),
            "history_months": ledger.history_months(now),
            "freedom": {k: c[k] for k in ("monthly_costs", "target", "dependable", "gap", "emergency_fund", "saved",
                                          "breathing_room", "optionality", "independence")},
            "progress": {"built": built, "engineering_hours": t["engineering_hours"], "sales": t["sales"], "collected": t["collected"],
                         "costs": t["costs"], "delivery_per_hour": t["delivery_per_hour"], "per_hour": t["per_hour"]},
            "opps": opps, "pending": pending}


def panel(now: float = None) -> Dict[str, Any]:
    path = store.db_path()
    if not path.exists():
        return {"opened": False}
    now = now or time.time()
    key = str(path)
    stamp = path.stat().st_mtime
    hit = _cache.get(key)
    if hit and hit[0][0] == stamp and now - hit[0][1] < CACHE_S:
        return hit[1]
    data = _build(now)
    _cache[key] = ((stamp, now), data)
    return data
