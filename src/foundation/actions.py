"""src/foundation/actions.py - the buttons on the web dashboard.

A short, fixed list of one-tap actions; each is a thin wrapper over the SAME functions the typed commands use (so it has exactly the same
effect and the same audit trail), with its argument validated. The web route only calls this for a REAL logged-in admin (never the agent's own
loopback token) with X-AURIX-Confirm: DO. Nothing here can approve a mission, touch credentials, change the approval policy or move money.

  run_action(name, arg) -> {"ok": bool, "message": str}
  decisions()           what is waiting on you (game fixes, lessons) as cards with Yes/No buttons
  background()          long jobs started from a button (self-check, teaching) and when they began
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
from typing import Any, Dict, List

_BG: Dict[str, float] = {}
_LOCK = threading.Lock()
GID = re.compile(r"g-[0-9a-f]{6}")
LID = re.compile(r"l-[0-9a-f]{6}")
RID = re.compile(r"r-[0-9a-f]{6}")
KID = re.compile(r"k-[0-9a-f]{6}")
UPID = re.compile(r"u-[0-9a-f]{6}")
MEMID = re.compile(r"[0-9a-f]{8}")
FID = re.compile(r"f-[0-9a-f]{6}")
CID = re.compile(r"c-[0-9a-f]{6}")
NID = re.compile(r"n-[0-9a-f]{6}")
AID = re.compile(r"a-[0-9a-f]{6}")
EVID = re.compile(r"e-[0-9a-f]{6}")


def _plain(t: Any) -> str:
    return re.sub(r"<[^>]+>", "", str(t)).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&#x27;", "'")


def background() -> Dict[str, float]:
    with _LOCK:
        return dict(_BG)


def _start_bg(name: str, coro_factory) -> Dict[str, Any]:
    with _LOCK:
        if name in _BG:
            return {"ok": False, "message": f"{name} is already running (started {int(time.time() - _BG[name])} s ago)."}
        _BG[name] = time.time()

    def work():
        try:
            asyncio.run(coro_factory())
        except Exception:
            pass
        finally:
            with _LOCK:
                _BG.pop(name, None)
    threading.Thread(target=work, daemon=True, name=f"aurix-{name}").start()
    return {"ok": True, "message": "Started - the dashboard updates when it finishes (a few minutes)."}


def _evals(tiers):
    async def go():
        from src.foundation import evals, sandbox
        from src.foundation.llm_bridge import default_llm
        await evals.run_and_record(tiers, default_llm, await asyncio.to_thread(sandbox.available), presence_kw=sandbox.presence_kw())
    return go


def _teach():
    async def go():
        from src.foundation import evals
        from src.foundation.llm_bridge import default_llm
        await evals.teach_failures(default_llm)
    return go


def run_action(name: str, arg: str = "") -> Dict[str, Any]:
    from src.foundation import announce, audit, content, fastlane, forge, freelance, gamepilot, gaming, learning, money, repos, shards, teacher, upgrades
    from src.foundation.land import hub as land
    from src.foundation import memory as fmemory
    from src.foundation import evolve
    arg = (arg or "").strip()
    simple = {
        "fix_yes": (GID, gaming.approve), "fix_no": (GID, gaming.decline), "fix_undo": (GID, gaming.undo),
        "repo_yes": (RID, repos.approve), "repo_no": (RID, repos.decline),
        "upg_yes": (UPID, upgrades.approve), "upg_no": (UPID, upgrades.decline), "upg_undo": (UPID, upgrades.undo),
        "mem_yes": (KID, fmemory.approve), "mem_no": (KID, fmemory.decline),
        "mem_forget": (MEMID, fmemory.forget), "mem_pin": (MEMID, lambda a: fmemory.pin(a, True)), "mem_unpin": (MEMID, lambda a: fmemory.pin(a, False)),
        "lesson_approve": (LID, teacher.approve), "lesson_deny": (LID, teacher.deny), "lesson_retire": (LID, teacher.retire),
        "freelance_yes": (FID, freelance.approve), "freelance_no": (FID, freelance.decline),
        "content_yes": (CID, content.approve), "content_no": (CID, content.decline),
        "learn_yes": (NID, learning.approve), "learn_no": (NID, learning.decline),
        "approve_skill": (forge.NAME_RE, forge.approve), "deny_skill": (forge.NAME_RE, forge.deny),
        "land_yes": (AID, land.approve), "land_no": (AID, land.decline),
        "evolve_yes": (EVID, evolve.approve_any), "evolve_no": (EVID, evolve.decline_any),
    }
    try:
        if name in simple:
            rx, fn = simple[name]
            if not rx.fullmatch(arg):
                return {"ok": False, "message": "bad id"}
            return {"ok": True, "message": _plain(fn(arg))}
        if name == "night_now":
            from src.foundation import nightshift
            res = nightshift.run_shift(None, True)
            return {"ok": "skipped" not in res, "message": _plain(res.get("skipped") or f"Shift done: {res.get('gifts', 0)} things ready.")}
        if name in ("shard_pause", "shard_resume"):
            if arg != "all" and arg not in shards.IDS:
                return {"ok": False, "message": "unknown helper"}
            return {"ok": True, "message": _plain(shards.set_paused(arg, name == "shard_pause"))}
        if name == "gp_arm":
            if arg and not arg.isdigit():
                return {"ok": False, "message": "that needs to be a number of minutes."}
            return {"ok": True, "message": _plain(gamepilot.arm(int(arg) if arg.isdigit() else gamepilot.ARM_DEFAULT_MIN))}
        if name == "gp_disarm":
            return {"ok": True, "message": _plain(gamepilot.disarm())}
        if name == "gp_unpair":
            return {"ok": True, "message": _plain(gamepilot.unpair_all())}
        if name == "gp_pair":
            return {"ok": True, "message": _plain(gamepilot.confirm_pairing(arg))}
        if name == "money_set":
            ident, _, status = arg.partition(":")
            return {"ok": True, "message": _plain(money.set_status(ident, status))}
        if name == "upg_add":
            return {"ok": True, "message": _plain(upgrades.add_idea(arg))}
        if name == "upg_on":
            return {"ok": True, "message": _plain(upgrades.set_config(True, int(arg) if arg.isdigit() else 3))}
        if name == "upg_off":
            return {"ok": True, "message": _plain(upgrades.set_config(False, 0))}
        if name == "upg_now":
            ok, why = upgrades.can_draft()
            if not ok:
                return {"ok": False, "message": _plain(why)}
            threading.Thread(target=upgrades.run_once, kwargs={"notify_no_result": True}, daemon=True, name="aurix-upgrade-now").start()
            return {"ok": True, "message": "Working on it - drafting and testing takes a few minutes."}
        if name == "mem_add":
            if not (4 <= len(arg) <= 400):
                return {"ok": False, "message": "Write what to remember (4 to 400 characters)."}
            return {"ok": True, "message": _plain(fmemory.remember(arg, "owner:dashboard"))}
        if name == "mem_unforget":
            return {"ok": True, "message": _plain(fmemory.unforget())}
        if name == "repo_add":
            if len(arg) > 300 or not repos.find_repo(arg):
                return {"ok": False, "message": "That is not a github.com/owner/repo link."}
            return {"ok": True, "message": _plain(repos.request(arg))}
        if name == "fastlane_on":
            return {"ok": True, "message": _plain(fastlane.set_enabled(True))}
        if name == "fastlane_off":
            return {"ok": True, "message": _plain(fastlane.set_enabled(False))}
        if name == "teacher_on":
            if arg and not arg.isdigit():
                return {"ok": False, "message": "that needs to be a number of calls per day."}
            n = int(arg) if arg.isdigit() else 5
            return {"ok": True, "message": _plain(teacher.set_config(True, n))}
        if name == "teacher_off":
            return {"ok": True, "message": _plain(teacher.set_config(False, 0))}
        if name == "games_refresh":
            return {"ok": True, "message": _plain(gaming.request_refresh())}
        if name == "ack":
            return {"ok": True, "message": _plain(announce.acknowledge())}
        if name == "evals_plan":
            return _start_bg("evals", _evals(("plan",)))
        if name == "evals_all":
            return _start_bg("evals", _evals(("plan", "code")))
        if name == "teach":
            ok, why = teacher.can_call()
            return _start_bg("teach", _teach()) if ok else {"ok": False, "message": _plain(why)}
    except Exception as e:                                              # a button must answer, never 500
        return {"ok": False, "message": f"{type(e).__name__}: {str(e)[:120]}"}
    if name in ACTION_NAMES:                                            # allow-listed by the route but not wired up here: our bug, not the owner's
        audit.append("action_not_wired", name=name)
        return {"ok": False, "message": f"'{name}' is allow-listed but not wired up yet - this is a bug on my end, not something you did wrong."}
    return {"ok": False, "message": "unknown action"}


ACTION_NAMES = ("fix_yes", "fix_no", "fix_undo", "lesson_approve", "lesson_deny", "lesson_retire", "fastlane_on", "fastlane_off", "teacher_on",
                "teacher_off", "games_refresh", "ack", "evals_plan", "evals_all", "teach", "repo_add", "repo_yes", "repo_no", "mem_yes", "mem_no", "mem_forget", "mem_pin", "mem_unpin", "mem_add", "mem_unforget",
                "upg_yes", "upg_no", "upg_undo", "upg_add", "upg_on", "upg_off", "upg_now", "money_set", "gp_arm", "gp_disarm", "gp_unpair", "gp_pair", "shard_pause", "shard_resume", "night_now",
                "freelance_yes", "freelance_no", "content_yes", "content_no", "learn_yes", "learn_no", "approve_skill", "deny_skill",
                "land_yes", "land_no", "evolve_yes", "evolve_no")


def decisions() -> List[Dict[str, Any]]:
    """Cards for everything waiting on the owner that a button can decide."""
    from src.foundation import content, forge, freelance, gaming, learning, repos, teacher, upgrades
    from src.foundation.land import hub as land
    from src.foundation import memory as fmemory
    from src.foundation import evolve
    out: List[Dict[str, Any]] = []
    for u in [x for x in upgrades.all_proposals() if x["status"] == "review"]:
        v = u.get("verify", {})
        out.append({"kind": "upgrade", "id": u["id"], "icon": "🛠️", "title": "Upgrade: " + u["title"],
                    "lines": [_plain(u.get("why", "")), "Changes: " + ", ".join(c["path"] for c in u["changes"]),
                              f"Tested in the sandbox: {v.get('after_ran', '?')} tests pass (+{v.get('added_tests', '?')} new); nothing that passed before fails. Risk: {u.get('risk', '')}."],
                    "risk": "rebuilds the app with a health check; rolls itself back if it does not come up",
                    "buttons": [{"label": "Approve & deploy", "action": "upg_yes", "arg": u["id"], "style": "approve"}, {"label": "Reject", "action": "upg_no", "arg": u["id"], "style": "deny"}]})
    for p in fmemory.pending():
        forget = p.get("kind") == "forget"
        out.append({"kind": "memory", "id": p["id"], "icon": "🗑️" if forget else "🧠", "title": ("Forget this memory?" if forget else "Remember this?"),
                    "lines": [_plain(p.get("text", "")), _plain(p.get("why", ""))], "risk": "you can undo a forget; nothing is remembered without your yes",
                    "buttons": [{"label": "Forget it" if forget else "Remember", "action": "mem_yes", "arg": p["id"], "style": "deny" if forget else "approve"},
                                {"label": "Keep it" if forget else "No", "action": "mem_no", "arg": p["id"], "style": "approve" if forget else "deny"}]})
    for r in repos.pending():
        v = r.get("verdict") or {}
        out.append({"kind": "repo", "id": r["id"], "icon": {"ok": "🟢", "careful": "🟠", "stop": "🔴"}.get(v.get("level"), "📥"), "title": f"Absorb {r['owner']}/{r['repo']}?",
                    "lines": [_plain(v.get("what", ""))] + [_plain(x) for x in (v.get("reasons") or [])[:4]] + ["If you say yes: stored inert in the library. Nothing runs or installs."],
                    "risk": "stored, never executed",
                    "buttons": [{"label": "Absorb it", "action": "repo_yes", "arg": r["id"], "style": "approve"}, {"label": "Skip", "action": "repo_no", "arg": r["id"], "style": "deny"}]})
    for p in gaming.pending():
        out.append({"kind": "game_fix", "id": p["id"], "icon": "🎮", "title": p["title"], "lines": [_plain(x) for x in p["evidence"][:4]] + [_plain(p["fix"]["summary"])],
                    "risk": _plain(p["fix"]["risk"]),
                    "buttons": [{"label": "Yes, fix it", "action": "fix_yes", "arg": p["id"], "style": "approve"}, {"label": "No", "action": "fix_no", "arg": p["id"], "style": "deny"}]})
    for les in teacher.all_lessons():
        if les.get("status") == "pending":
            out.append({"kind": "lesson", "id": les["id"], "icon": "🎓", "title": les["title"], "lines": [_plain(les.get("diagnosis", ""))] + [_plain(g) for g in les["guidance"][:3]],
                        "risk": les.get("evidence") or "advice only; nothing runs differently until you keep it",
                        "buttons": [{"label": "Keep it", "action": "lesson_approve", "arg": les["id"], "style": "approve"}, {"label": "Discard", "action": "lesson_deny", "arg": les["id"], "style": "deny"}]})
    for j in freelance.pending():
        out.append({"kind": "freelance", "id": j["id"], "icon": "🧰", "title": j["title"],
                    "lines": [_plain(j.get("explanation", "")), j["filename"]],
                    "risk": "keeping it does not deliver it; recording, editing and publishing stay yours",
                    "buttons": [{"label": "Keep it", "action": "freelance_yes", "arg": j["id"], "style": "approve"}, {"label": "Discard", "action": "freelance_no", "arg": j["id"], "style": "deny"}]})
    for p in content.pending():
        out.append({"kind": "content", "id": p["id"], "icon": "📝", "title": p["title"],
                    "lines": [f"{p['words']} words" + (f" · {p['verify_flags']} claim(s) marked [VERIFY]" if p["verify_flags"] else "")],
                    "risk": "keeping it does not publish it; recording, editing and publishing stay yours",
                    "buttons": [{"label": "Keep it", "action": "content_yes", "arg": p["id"], "style": "approve"}, {"label": "Discard", "action": "content_no", "arg": p["id"], "style": "deny"}]})
    for r in learning.pending():
        out.append({"kind": "learning", "id": r["id"], "icon": "📚", "title": r["title"],
                    "lines": [_plain(r.get("summary", "")), _plain(r.get("takeaway", ""))] if r.get("takeaway") else [_plain(r.get("summary", ""))],
                    "risk": "keeping it just files it in your library; nothing fetched is ever run",
                    "buttons": [{"label": "Keep it", "action": "learn_yes", "arg": r["id"], "style": "approve"}, {"label": "Discard", "action": "learn_no", "arg": r["id"], "style": "deny"}]})
    for s in forge.pending():
        out.append({"kind": "skill", "id": s["name"], "icon": "🛠️", "title": "New skill: " + s["name"],
                    "lines": [_plain(s.get("wanted", "")), f"Passed its tests in the sandbox (attempt {s.get('attempts', '?')})."],
                    "risk": "runs only in the sandbox, only on your say, and only as plain JSON in/out - no files, network or shell",
                    "buttons": [{"label": "Approve", "action": "approve_skill", "arg": s["name"], "style": "approve"}, {"label": "Deny", "action": "deny_skill", "arg": s["name"], "style": "deny"}]})
    for c in land.pending():
        out.append({"kind": "land", "id": c["id"], "icon": "🏞️", "title": "Land: " + _plain(c.get("label", c.get("property", ""))),
                    "lines": [_plain(c.get("action", "")), f"Maximum TRUE exposure: ${c.get('max_usd', 0):,.0f} · dossier {c.get('dossier_version', '')[:12]}"],
                    "risk": "records only your go-ahead within that maximum; you sign and pay, AURIX does neither",
                    "buttons": [{"label": "Approve", "action": "land_yes", "arg": c["id"], "style": "approve"}, {"label": "Reject", "action": "land_no", "arg": c["id"], "style": "deny"}]})
    for c in evolve.all_pending():
        out.append({"kind": "evolve", "id": c["id"], "icon": "🧬", "title": f"Evolved {c['domain']}: fitness {c['fitness']:.3f}",
                    "lines": [_plain(c.get("description", "")), f"generation {c.get('generation', '?')}"],
                    "risk": "a sandboxed/backtested result only; approving makes it the live default for that domain",
                    "buttons": [{"label": "Approve", "action": "evolve_yes", "arg": c["id"], "style": "approve"}, {"label": "Reject", "action": "evolve_no", "arg": c["id"], "style": "deny"}]})
    return out
