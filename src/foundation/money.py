"""src/foundation/money.py - the money hub: an honest ranked catalog of ways to make or save money, and the paper strategy lab's leaderboard.

Two rules shape everything here. (1) AURIX never moves real money and never creates an account: every idea lists what AURIX does and what only YOU can do (the
"gate"). (2) Numbers are estimates and are labelled as such; the ranking formula is printed so you can argue with it:

    score = (middle of the estimated monthly range) x confidence x risk factor / hours per week (at least 0.5)

so a small sure thing that takes no time beats a big long shot that eats your week. The strategy lab (tasks/strategy_lab.py, cron on the host) writes
runtime/strategy_lab.json; this module only reads it.
"""
from __future__ import annotations

import html
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.foundation import audit

e = html.escape

STATUSES = ("idea", "exploring", "active", "paused", "rejected", "done")
RISK_FACTOR = {"low": 1.0, "medium": 0.7, "high": 0.3}
ID_RX = re.compile(r"^[a-z][a-z0-9_]{2,30}$")


def catalog_path() -> Path:
    return Path(os.environ.get("AURIX_MONEY_CATALOG") or (Path(__file__).resolve().parents[2] / "money" / "opportunities.json"))


def _data() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "money"


def _atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_catalog() -> Dict[str, Any]:
    try:
        return json.loads(catalog_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"about": "", "items": []}


def statuses() -> Dict[str, dict]:
    try:
        v = json.loads((_data() / "status.json").read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def score(item: dict) -> float:
    mid = (float(item.get("est_low", 0)) + float(item.get("est_high", 0))) / 2
    if item.get("est_high", 0) <= 0:
        return 0.0
    return round(mid * float(item.get("confidence", 0.3)) * RISK_FACTOR.get(item.get("legal_risk", "high"), 0.3) / max(float(item.get("hours_per_week", 1)), 0.5), 1)


def ranked() -> List[dict]:
    st = statuses()
    out = []
    for it in load_catalog().get("items", []):
        row = dict(it)
        row["score"] = score(it)
        row["status"] = st.get(it["id"], {}).get("status", "active" if it["id"] == "strategy_lab" else "idea")
        out.append(row)
    live = [r for r in out if r["status"] != "rejected"]
    dead = [r for r in out if r["status"] == "rejected"]
    return sorted(live, key=lambda r: (-r["score"], r["id"])) + sorted(dead, key=lambda r: r["id"])


def get(ident: str) -> Optional[dict]:
    return next((r for r in ranked() if r["id"] == ident), None)


def set_status(ident: str, status: str, now: Optional[float] = None) -> str:
    ident, status = (ident or "").strip().lower(), (status or "").strip().lower()
    status = {"start": "active", "explore": "exploring", "pause": "paused", "reject": "rejected", "stop": "paused"}.get(status, status)
    it = get(ident)
    if it is None:
        return f"No money idea called <code>{e(ident)}</code>. <code>money</code> lists them."
    if status not in STATUSES:
        return f"Status must be one of: {', '.join(STATUSES)}."
    if ident == "crypto_yield" and status in ("active", "exploring"):
        return "Real-money crypto is refused by AURIX's rules. Marking it rejected keeps it that way."
    st = statuses()
    st[ident] = {"status": status, "at": now or time.time()}
    _atomic(_data() / "status.json", st)
    audit.append("money_status", id=ident, status=status)
    return f"💰 <b>{e(it['name'])}</b> is now <b>{status}</b>."


def _money(lo: float, hi: float) -> str:
    return "n/a" if hi <= 0 else f"${lo:,.0f}-{hi:,.0f}/mo"


def list_text(limit: int = 9) -> str:
    rows = ranked()
    if not rows:
        return "The money catalog is empty."
    lines = ["💰 <b>Ways to make or save money</b> (ranked by expected $ per hour of your time)",
             "<i>Estimates only, from general knowledge: check current rates. AURIX never moves real money or opens accounts.</i>"]
    for i, r in enumerate(rows[:limit], 1):
        lines.append(f"{i}. <b>{e(r['name'])}</b> · {_money(r['est_low'], r['est_high'])} · {r['hours_per_week']:g} h/wk · risk {e(r['legal_risk'])} · <i>{e(r['status'])}</i> <code>{r['id']}</code>")
    if len(rows) > limit:
        lines.append(f"<i>...and {len(rows) - limit} more.</i>")
    lines.append("<code>money &lt;id&gt;</code> for the plan · <code>money &lt;id&gt; start|pause|reject</code> · <code>lab</code> for the paper strategy leaderboard")
    return "\n".join(lines)


def _gpu_estimate() -> Optional[str]:
    """Reads the owner's actual GPU from the game doctor's last PC report (gaming/agent's gpu_info()), so gpu_rental never has to
    ask a question AURIX can look up itself. This is NOT a live market-price lookup - just an honest, general-knowledge tier guess,
    clearly labelled as one; it never overstates what a card without tensor cores earns."""
    try:
        from src.foundation import gaming
        rep = gaming.load_report()
    except Exception:                                                          # noqa: BLE001 - a broken/missing report is not fatal here
        return None
    gpus = (rep or {}).get("gpus") or []
    if not gpus or not gpus[0].get("name"):
        return None
    name = gpus[0]["name"]
    if re.search(r"\bRTX\s?(30|40|50)\d\d|\bA\d00\b|\bH100\b|\bL40\b", name, re.I):
        tier = "a strong, modern card - the higher end of the catalog estimate is plausible on a marketplace like Vast.ai."
    elif re.search(r"\bGTX\s?(10|16)\d\d|\bRTX\s?20\d\d|\bGT\s?\d{3,4}\b", name, re.I):
        tier = ("an older card without the tensor cores marketplaces like Vast.ai/Salad pay well for. Realistic income is likely "
                "near the bottom of the catalog estimate, or close to nothing after electricity - check real listings before counting on a number.")
    else:
        tier = "worth checking current Vast.ai/Salad listings for this exact card before counting on a number."
    return f"Your PC reports: <b>{e(name)}</b>. {tier}"


def detail_text(ident: str) -> str:
    r = get((ident or "").strip().lower())
    if r is None:
        return f"No money idea called <code>{e(ident)}</code>. <code>money</code> lists them."
    first_step = e(r["first_step"])
    if r["id"] == "gpu_rental":
        real = _gpu_estimate()
        if real:
            first_step = real
    return "\n".join([
        f"💰 <b>{e(r['name'])}</b> · <i>{e(r['status'])}</i> · <code>{r['id']}</code>", e(r["summary"]),
        f"<b>Estimate:</b> {_money(r['est_low'], r['est_high'])} (confidence {r['confidence']:.0%}) · <b>Time:</b> {r['hours_per_week']:g} h/wk · first dollar ~{r['months_to_first_dollar']} mo · <b>Start-up:</b> ${r['capital']:,.0f} · <b>Legal risk:</b> {e(r['legal_risk'])}",
        f"<b>AURIX does:</b> {e(r['aurix_does'])}", f"<b>You do:</b> {e(r['you_do'])}", f"<b>Gate:</b> {e(r['gate'])}", f"<b>First step:</b> {first_step}",
        f"<code>money {r['id']} start</code> · <code>pause</code> · <code>reject</code>"])


# ---------------------------------------------------------------------------------------------------------------------------------
# the paper strategy lab (read-only view of runtime/strategy_lab.json)
# ---------------------------------------------------------------------------------------------------------------------------------

def lab_report() -> Optional[dict]:
    rt = Path(os.environ.get("AURIX_LAB_RUNTIME") or (Path(os.environ.get("AURIX_BRAIN", "/aurix")) / "runtime"))
    try:
        v = json.loads((rt / "strategy_lab.json").read_text(encoding="utf-8"))
        return v if isinstance(v, dict) and "strategies" in v else None
    except (OSError, ValueError):
        return None


def lab_text() -> str:
    rep = lab_report()
    if not rep:
        return "The strategy lab has not produced a report yet. It runs on the server after each market close (Mon-Fri)."
    lines = [f"🧪 <b>Paper strategy lab</b> · {rep['strategies_tried']} strategies · {rep['span'][0]} to {rep['span'][1]} · live since {rep['inception']} ({rep['live_days']} trading days)",
             "<i>Simulation with 0.05% costs and no look-ahead. Not advice. Trying many ideas makes the best one look better than it is; the LIVE column is the honest one.</i>"]
    for s in sorted(rep["strategies"], key=lambda x: -((x.get("full") or {}).get("sharpe", -9))):
        f, o, lv = s.get("full") or {}, s.get("out_of_sample") or {}, s.get("live")
        live = f" · live {lv['total']:+.1f}%" if lv else ""
        lines.append(f"• <b>{e(s['name'])}</b>: {f.get('cagr')}%/yr, Sharpe {f.get('sharpe')}, worst drop {f.get('max_dd')}% (out-of-sample {o.get('cagr')}%/yr){live}\n   <i>{e(s['verdict'])}</i>")
    return "\n".join(lines)


def panel() -> Dict[str, Any]:
    rep = lab_report()
    lab = None
    if rep:
        lab = {"generated": rep.get("generated"), "span": rep.get("span"), "inception": rep.get("inception"), "live_days": rep.get("live_days", 0), "tried": rep.get("strategies_tried"),
               "rows": [{"id": s["id"], "name": s["name"], "idea": s["idea"], "cagr": (s.get("full") or {}).get("cagr"), "sharpe": (s.get("full") or {}).get("sharpe"),
                         "max_dd": (s.get("full") or {}).get("max_dd"), "oos_cagr": (s.get("out_of_sample") or {}).get("cagr"), "live": (s.get("live") or {}).get("total"),
                         "verdict": s.get("verdict", ""), "weights": s.get("weights_now", {})}
                        for s in sorted(rep["strategies"], key=lambda x: -((x.get("full") or {}).get("sharpe", -9)))]}
    return {"lab": lab, "items": [{"id": r["id"], "name": r["name"], "status": r["status"], "range": _money(r["est_low"], r["est_high"]), "hours": r["hours_per_week"], "risk": r["legal_risk"],
                                   "gate": r["gate"], "summary": r["summary"], "first_step": r["first_step"], "score": r["score"]} for r in ranked()]}
