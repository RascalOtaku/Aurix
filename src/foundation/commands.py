"""src/foundation/commands.py - the OWNER's control surface (via the authenticated Telegram path).

    mission: <goal>          plan it and show the contract for approval
    approve mission <id>     activate the contract and start working
    deny mission <id>
    stop                     STOP is absolute: halts everything immediately
    status                   heartbeat self-report
    resume                   continue a blocked/interrupted active mission
    authorize <name> [YYYY-MM-DD]   grant an owner authorization (e.g. patient-data-consent)
    revoke <name>

These are only ever parsed in services/telegram/listener.py AFTER it verified the sender
is the owner's chat id, and before anything reaches the agent - so the agent can never
issue them. Protected component (owner_authority).
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import re
import time
from typing import Awaitable, Callable, Dict, Optional, Tuple

from src.foundation import addons
from src.foundation import announce
from src.foundation import repos
from src.foundation import upgrades
from src.foundation import money
from src.foundation import nightshift
from src.foundation import transcription
from src.foundation import earnings
from src.foundation import freelance
from src.foundation import fabricate
from src.foundation import printer as fprinter
from src.foundation import smarthome
from src.foundation import vision
from src.foundation import frigate
from src.foundation.land import hub as land
from src.foundation import evolve
from src.foundation import evolve_domains  # noqa: F401 - import-time side effect: registers the "forge_guidance" domain
from src.foundation import workers as fworkers
from src.foundation import integrate
from src.foundation import versions
from src.foundation import content
from src.foundation import learning
from src.foundation import subscriptions
from src.foundation import gamepilot
from src.foundation import shards
from src.foundation import memory as fmemory
from src.foundation import experience
from src.foundation import growth
from src.foundation import audit
from src.foundation import buttons
from src.foundation import capabilities as cap
from src.foundation import evals
from src.foundation import fastlane
from src.foundation import forge
from src.foundation import gaming
from src.foundation import heartbeat
from src.foundation import mission as ms
from src.foundation import sysview
from src.foundation import planner
from src.foundation import projects
from src.foundation import qol
from src.foundation import sandbox
from src.foundation import teacher
from src.foundation import standing as st
from src.foundation import watchdog
from src.foundation import xp
from src.foundation.runner import MissionRunner

# Spec section 11: not yet. Refused in code even if the owner asks.
NOT_YET_AUTHORIZATIONS = {
    "live-trading": "Real-money trading stays disabled until the Foundation loop is trusted "
                    "(activation handoff section 11). Paper trading is unaffected.",
}

_SID = r"(sm-[0-9a-f]{6})"
_SKILL = r"([a-z][a-z0-9_]{2,30})"
_PATTERNS = [
    ("approve_standing", re.compile(rf"^\s*/?approve\s+standing\s+{_SID}\s*$", re.I)),
    ("deny_standing", re.compile(rf"^\s*/?deny\s+standing\s+{_SID}\s*$", re.I)),
    ("pause_standing", re.compile(rf"^\s*/?pause\s+standing\s+{_SID}\s*$", re.I)),
    ("resume_standing", re.compile(rf"^\s*/?resume\s+standing\s+{_SID}\s*$", re.I)),
    ("retire_standing", re.compile(rf"^\s*/?retire\s+standing\s+{_SID}\s*$", re.I)),
    ("standing_new", re.compile(r"^\s*/?standing\s*[:\-]\s*(\S.*)$", re.I | re.S)),
    ("standing_list", re.compile(r"^\s*/?standing(?:\s+list)?\s*$", re.I)),
    ("approve_mission", re.compile(r"^\s*/?approve\s+mission\s+(m-[0-9a-f]{6})\s*$", re.I)),
    ("deny_mission", re.compile(r"^\s*/?deny\s+mission\s+(m-[0-9a-f]{6})\s*$", re.I)),
    ("stop", re.compile(r"^\s*/?(?:stop|halt|abort)\s*[.!]*\s*$", re.I)),
    ("status", re.compile(r"^\s*/?(?:status|missions)\s*[?]*\s*$", re.I)),
    ("resume", re.compile(r"^\s*/?resume\s*$", re.I)),
    ("authorize", re.compile(r"^\s*/?authori[sz]e\s+([a-z0-9-]+)(?:\s+(\d{4}-\d{2}-\d{2}))?\s*$", re.I)),
    ("revoke", re.compile(r"^\s*/?revoke\s+([a-z0-9-]+)\s*$", re.I)),
    # quality-of-life (read-only unless noted); exact-match so ordinary chat is never swallowed
    ("help", re.compile(r"^\s*/?(?:help|commands|\?)\s*$", re.I)),
    ("history", re.compile(r"^\s*/?(?:history|recent missions)\s*$", re.I)),
    ("show", re.compile(r"^\s*/?show\s+((?:sm|m)-[0-9a-f]{6})\s*$", re.I)),
    ("approvals", re.compile(r"^\s*/?(?:approvals|pending)\s*$", re.I)),
    ("log", re.compile(r"^\s*/?log(?:\s+(\d{1,2}))?\s*$", re.I)),
    ("send_files", re.compile(r"^\s*/?send\s+files(?:\s+(m-[0-9a-f]{6}))?\s*$", re.I)),
    ("files", re.compile(r"^\s*/?files(?:\s+(m-[0-9a-f]{6}))?\s*$", re.I)),
    ("tools", re.compile(r"^\s*/?tools\s*$", re.I)),
    ("doctor", re.compile(r"^\s*/?(?:doctor|health|healthcheck)\s*$", re.I)),
    ("authorizations", re.compile(r"^\s*/?(?:authori[sz]ations|auths)\s*$", re.I)),
    ("ping", re.compile(r"^\s*/?ping\s*$", re.I)),
    ("level", re.compile(r"^\s*/?(?:level|xp|rank)\s*$", re.I)),
    ("disk", re.compile(r"^\s*/?(?:disk|disk\s+space|disk\s+usage|storage|df)\s*[?]*\s*$", re.I)),
    ("homelab", re.compile(r"^\s*/?(?:homelab|home\s+lab)\s*[?]*\s*$", re.I)),
    ("fastlane", re.compile(r"^\s*/?fast[\s-]*lane(?:\s+(on|off|status))?\s*$", re.I)),
    ("evals", re.compile(r"^\s*/?evals?(?:\s+(all|plan|code|last))?\s*$", re.I)),
    ("teacher", re.compile(r"^\s*/?teacher(?:\s+(on(?:\s+\d{1,2})?|off|status))?\s*$", re.I)),
    ("teach", re.compile(r"^\s*/?teach(?:\s+(failures))?\s*$", re.I)),
    ("lessons", re.compile(r"^\s*/?lessons\s*$", re.I)),
    # game doctor: ask about Steam games/mods, yes/no on clear-cut fix proposals
    ("timeline", re.compile(r"^\s*/?(?:timeline|growth|evolution|what'?s\s+new)\s*\??\s*$", re.I)),
    ("trust", re.compile(r"^\s*/?(?:trust|confidence|why\s+(?:can|should)\s+i\s+trust\s+(?:you|it|aurix))\s*\??\s*$", re.I)),
    ("ack", re.compile(r"^\s*/?(?:ack|acknowledge)\s*$", re.I)),
    ("menu", re.compile(r"^\s*/?(?:menu|buttons|start)\s*$", re.I)),
    ("dashboard", re.compile(r"^\s*/?(?:dashboard|monitors|add-?ons)\s*$", re.I)),
    ("run_all", re.compile(r"^\s*/?(?:all|check\s+all|check\s+everything|do\s+it\s+all|run\s+all)\s*[.!]*\s*$", re.I)),
    ("games_refresh", re.compile(r"^\s*/?(?:games|steam)\s+refresh\s*$", re.I)),
    ("games", re.compile(r"^\s*/?(?:games|steam|my\s+games)\s*$", re.I)),
    ("fixes", re.compile(r"^\s*/?(?:fixes|game\s+fixes)\s*$", re.I)),
    ("shards", re.compile(r"^\s*/?(?:shards|helpers|workers|what(?:'s| is)\s+running|autonomy)\s*\??\s*$", re.I)),
    ("shard_pause", re.compile(r"^\s*/?pause\s+(all|(?!standing\b)[a-z][a-z_]{2,20})\s*$", re.I)),
    ("shard_resume", re.compile(r"^\s*/?resume\s+(all|(?!standing\b)[a-z][a-z_]{2,20})\s*$", re.I)),
    ("gp_pair", re.compile(r"^\s*/?pair\s+(\d{6})\s*$", re.I)),
    ("gp_arm", re.compile(r"^\s*/?arm(?:\s+(\d{1,2}))?\s*(?:min(?:utes)?)?\s*$", re.I)),
    ("gp_disarm", re.compile(r"^\s*/?disarm\s*$", re.I)),
    ("gp_unpair", re.compile(r"^\s*/?unpair\s*$", re.I)),
    ("gp_status", re.compile(r"^\s*/?(?:gamepilot|game\s*pilot|firestick|fire\s*tv|gp)\s*$", re.I)),
    ("night_now", re.compile(r"^\s*/?(?:night|shift|overnight)\s+now\s*$", re.I)),
    ("unwrap", re.compile(r"^\s*/?(?:unwrap|overnight|night|nightshift|night\s+shift|while\s+i\s+was\s+away|what\s+did\s+you\s+do)\s*\??\s*$", re.I)),
    ("subscriptions_remove", re.compile(r"^\s*/?(?:subscriptions|subs)\s+remove\s+(s-[0-9a-f]{6})\s*$", re.I)),
    ("subscriptions_status", re.compile(r"^\s*/?(?:subscriptions|subs)\s*\??\s*$", re.I)),
    ("subscriptions_add", re.compile(r"^\s*/?(?:subscriptions|subs):?\s+(.{4,4000})$", re.I | re.S)),
    ("content_yes", re.compile(r"^\s*/?(?:yes|approve|keep)(?:\s+content)?\s+(c-[0-9a-f]{6})\s*[.!]*\s*$", re.I)),
    ("content_no", re.compile(r"^\s*/?(?:no|discard|deny)(?:\s+content)?\s+(c-[0-9a-f]{6})\s*[.!]*\s*$", re.I)),
    ("content_status", re.compile(r"^\s*/?(?:content|content\s+drafts?)\s*\??\s*$", re.I)),
    ("content", re.compile(r"^\s*/?content:?\s+(.{8,1000})$", re.I | re.S)),
    ("learn_yes", re.compile(r"^\s*/?(?:yes|approve|keep)(?:\s+learn)?\s+(n-[0-9a-f]{6})\s*[.!]*\s*$", re.I)),
    ("learn_no", re.compile(r"^\s*/?(?:no|discard|deny)(?:\s+learn)?\s+(n-[0-9a-f]{6})\s*[.!]*\s*$", re.I)),
    ("learn_status", re.compile(r"^\s*/?(?:learn|library|reading|reading\s+list)\s*\??\s*$", re.I)),
    ("learn", re.compile(r"^\s*/?learn:?\s+(.{4,4000})$", re.I | re.S)),
    ("freelance_yes", re.compile(r"^\s*/?(?:yes|approve|keep)(?:\s+freelance)?\s+(f-[0-9a-f]{6})\s*[.!]*\s*$", re.I)),
    ("freelance_no", re.compile(r"^\s*/?(?:no|discard|deny)(?:\s+freelance)?\s+(f-[0-9a-f]{6})\s*[.!]*\s*$", re.I)),
    ("freelance_status", re.compile(r"^\s*/?(?:freelance|freelance\s+drafts?)\s*\??\s*$", re.I)),
    ("fab_yes", re.compile(r"^\s*/?(?:yes|approve|print)\s+(d-[0-9a-f]{6})\s*[.!]*\s*$", re.I)),
    ("fab_no", re.compile(r"^\s*/?(?:no|discard|deny)\s+(d-[0-9a-f]{6})\s*[.!]*\s*$", re.I)),
    ("fab_status", re.compile(r"^\s*/?(?:parts|cad|fabricate|my\s+parts)\s*\??\s*$", re.I)),
    ("printer", re.compile(r"^\s*/?(?:printer|print\s+status|3d\s*printer)\s*\??\s*$", re.I)),
    ("look", re.compile(r"^\s*/?(?:look|look\s+at\s+(?:the\s+)?screen|what'?s\s+on\s+(?:the\s+|my\s+)?screen)(?:\s*:\s*(.{2,1000}))?\s*\??\s*$", re.I | re.S)),
    ("home", re.compile(r"^\s*/?(?:lights|home|smart\s*home|devices)\s*\??\s*$", re.I)),
    ("fab", re.compile(r"^\s*/?(?:cad|fabricate):?\s+(.{8,2000})$", re.I | re.S)),
    ("freelance_find", re.compile(r"^\s*/?(?:freelance\s+find|find\s+(?:me\s+)?(?:a\s+)?(?:freelance\s+)?(?:job|gig|lead))\s*\??\s*$", re.I)),
    ("freelance", re.compile(r"^\s*/?freelance:?\s+(.{8,4000})$", re.I | re.S)),
    ("earned", re.compile(r"^\s*/?earned\s+\$?(\d+(?:\.\d{1,2})?)\s+([a-z][a-z0-9_]{2,30})(?:\s+(.+))?\s*$", re.I)),
    ("earnings", re.compile(r"^\s*/?(?:earnings|ledger|income)\s*\??\s*$", re.I)),
    ("transcripts", re.compile(r"^\s*/?(?:transcripts?|transcriptions?)\s*$", re.I)),
    ("lab", re.compile(r"^\s*/?(?:lab|strategy\s+lab|strategies|leaderboard)\s*$", re.I)),
    ("money_set", re.compile(r"^\s*/?money\s+([a-z][a-z0-9_]{2,30})\s+(start|explore|pause|stop|reject|done|idea|active|exploring|paused|rejected)\s*$", re.I)),
    ("money_show", re.compile(r"^\s*/?money\s+([a-z][a-z0-9_]{2,30})\s*$", re.I)),
    ("money", re.compile(r"^\s*/?(?:money|income|earn|ways\s+to\s+(?:make|earn)\s+money)\s*\??\s*$", re.I)),
    ("workspace", re.compile(r"^\s*/?workspace\s+([A-Za-z0-9_\-]{4,80})\s*$", re.I)),
    ("upgrade_yes", re.compile(r"^\s*/?(?:yes|approve(?:\s+upgrade)?)\s+(u-[0-9a-f]{6})\s*[.!]*\s*$", re.I)),
    ("upgrade_no", re.compile(r"^\s*/?(?:no|deny|reject)(?:\s+upgrade)?\s+(u-[0-9a-f]{6})\s*[.!]*\s*$", re.I)),
    ("upgrade_undo", re.compile(r"^\s*/?undo\s+(u-[0-9a-f]{6})\s*$", re.I)),
    ("upgrade_diff", re.compile(r"^\s*/?(?:diff|show(?:\s+upgrade)?)\s+(u-[0-9a-f]{6})\s*$", re.I)),
    ("upgrade_now", re.compile(r"^\s*/?upgrade\s+now\s*$", re.I)),
    ("upgrade_openhands", re.compile(r"^\s*/?upgrade\s+openhands\s*$", re.I)),
    ("upgrades_cfg", re.compile(r"^\s*/?upgrades?\s+(on(?:\s+\d{1,2})?|off)\s*$", re.I)),
    ("upgrade_add", re.compile(r"^\s*/?upgrade\s*[:\-]\s*(.{8,600})$", re.I | re.S)),
    ("upgrades", re.compile(r"^\s*/?upgrades?\s*$", re.I)),
    ("mem_yes", re.compile(r"^\s*/?(?:yes|approve(?:\s+memory)?)\s+(k-[0-9a-f]{6})\s*[.!]*\s*$", re.I)),
    ("mem_no", re.compile(r"^\s*/?(?:no|deny(?:\s+memory)?)\s+(k-[0-9a-f]{6})\s*[.!]*\s*$", re.I)),
    ("unforget", re.compile(r"^\s*/?(?:unforget|undo\s+forget)\s*$", re.I)),
    ("forget", re.compile(r"^\s*/?forget\s+(?:about\s+)?(.{3,120}?)\s*$", re.I)),
    ("remember", re.compile(r"^\s*/?(?:remember|memorize|memorise)\s*(?:that\b\s*[:\-]?|[:\-])\s*(.{4,400})$", re.I | re.S)),
    ("recall", re.compile(r"^\s*/?(?:what\s+do\s+you\s+(?:remember|know)\s+about|recall|memory\s+of)\s+(.{2,80}?)\s*\??\s*$", re.I)),
    ("memory", re.compile(r"^\s*/?(?:memory|memories|my\s+memory|what\s+do\s+you\s+remember)\s*\??\s*$", re.I)),
    ("repo_yes", re.compile(r"^\s*/?(?:yes|absorb|approve(?:\s+repo)?)\s+(r-[0-9a-f]{6})\s*[.!]*\s*$", re.I)),
    ("repo_no", re.compile(r"^\s*/?(?:no|skip|deny(?:\s+repo)?)\s+(r-[0-9a-f]{6})\s*[.!]*\s*$", re.I)),
    ("repos", re.compile(r"^\s*/?(?:repos|repo\s+list|links)\s*$", re.I)),
    ("repo", re.compile(r"^\s*/?(?:repo|absorb|look\s+at|check\s+out)\s+(\S*github\.com/\S+.*)$", re.I)),
    ("fix_yes", re.compile(r"^\s*/?(?:yes|approve(?:\s+fix)?)\s+(g-[0-9a-f]{6})\s*[.!]*\s*$", re.I)),
    ("fix_no", re.compile(r"^\s*/?(?:no|deny(?:\s+fix)?)\s+(g-[0-9a-f]{6})\s*[.!]*\s*$", re.I)),
    ("fix_undo", re.compile(r"^\s*/?undo\s+(g-[0-9a-f]{6})\s*$", re.I)),
    ("game", re.compile(r"^\s*/?(?:game|mods?)\s+([A-Za-z0-9][^\n]{2,60}?)\s*$", re.I)),
    ("lesson_show", re.compile(r"^\s*/?show\s+lesson\s+(l-[0-9a-f]{6})\s*$", re.I)),
    ("approve_lesson", re.compile(r"^\s*/?approve\s+lesson\s+(l-[0-9a-f]{6})\s*$", re.I)),
    ("deny_lesson", re.compile(r"^\s*/?deny\s+lesson\s+(l-[0-9a-f]{6})\s*$", re.I)),
    ("retire_lesson", re.compile(r"^\s*/?retire\s+lesson\s+(l-[0-9a-f]{6})\s*$", re.I)),
    ("trades", re.compile(r"^\s*/?(?:trades|paper\s+trad(?:es|ing)|trading)\s*[?]*\s*$", re.I)),
    ("pulse", re.compile(r"^\s*/?pulse\s*\??\s*$", re.I)),
    # skill forge: AURIX drafts + tests a small tool in the sandbox; only `approve skill` makes it usable
    ("forge", re.compile(r"^\s*/?forge\s*[:\-]\s*(\S.*)$", re.I | re.S)),
    ("skills", re.compile(r"^\s*/?skills\s*$", re.I)),
    ("skill_show", re.compile(rf"^\s*/?show\s+skill\s+{_SKILL}\s*$", re.I)),
    ("approve_skill", re.compile(rf"^\s*/?approve\s+skill\s+{_SKILL}\s*$", re.I)),
    ("deny_skill", re.compile(rf"^\s*/?deny\s+skill\s+{_SKILL}\s*$", re.I)),
    ("retire_skill", re.compile(rf"^\s*/?retire\s+skill\s+{_SKILL}\s*$", re.I)),
    ("run_skill", re.compile(rf"^\s*/?run\s+skill\s+{_SKILL}(?:\s+(\{{.*\}}))?\s*$", re.I | re.S)),
    # projects registry + to-dos (bookkeeping only)
    ("projects", re.compile(r"^\s*/?projects\s*$", re.I)),
    ("project_act", re.compile(r"^\s*/?project\s+(note|pause|resume|done)\s+(p-[0-9a-f]{6})(?:\s+(\S.*))?$", re.I | re.S)),
    ("project_new", re.compile(r"^\s*/?project\s*[:\-]\s*(\S.*)$", re.I | re.S)),
    ("todos", re.compile(r"^\s*/?todos\s*$", re.I)),
    ("todo_new", re.compile(r"^\s*/?todo\s*[:\-]\s*(\S.*)$", re.I | re.S)),
    ("todo_done", re.compile(r"^\s*/?done\s+(t-[0-9a-f]{6})\s*$", re.I)),
    # LandPilot (src/foundation/land): research + the acquisition gate. A land card is only ever approved with its explicit id.
    ("land_yes", re.compile(r"^\s*/?(?:yes|approve)\s+land\s+(a-[0-9a-f]{6})\s*[.!]*\s*$", re.I)),
    ("land_no", re.compile(r"^\s*/?(?:no|deny|reject)\s+land\s+(a-[0-9a-f]{6})\s*[.!]*\s*$", re.I)),
    ("land_build", re.compile(r"^\s*/?land\s+build\s+([A-Za-z0-9_\-]{2,60})\s*$", re.I)),
    ("land_cap", re.compile(r"^\s*/?land\s+cap\s+(\S{2,60}\s+\$?[\d,]+(?:\.\d{1,2})?(?:\s+\$?[\d,]+(?:\.\d{1,2})?)?)\s*$", re.I)),
    ("land_promote", re.compile(r"^\s*/?land\s+promote\s+(m-[0-9a-f]{6}\s+[A-Za-z0-9_\-]{2,60})\s*$", re.I)),
    ("land_propose", re.compile(r"^\s*/?land\s+propose\s+(\S{2,60})\s*$", re.I)),
    ("land_show", re.compile(r"^\s*/?land\s+show\s+(\S{2,60})\s*$", re.I)),
    ("land_criteria", re.compile(r"^\s*/?land\s+criteria\s*[:\-]\s*(\S.{6,998})$", re.I | re.S)),
    ("land_leads", re.compile(r"^\s*/?land\s+leads\s+(m-[0-9a-f]{6})\s*$", re.I)),
    ("land", re.compile(r"^\s*/?(?:land|land\s*pilot|parcels?)\s*\??\s*$", re.I)),
    # evolve (src/foundation/evolve.py): GA/PBT search over a domain's genome; a candidate only ever goes live via `yes evolve e-xxxxxx`
    ("evolve_yes", re.compile(r"^\s*/?(?:yes|approve)\s+evolve\s+(e-[0-9a-f]{6})\s*[.!]*\s*$", re.I)),
    ("evolve_no", re.compile(r"^\s*/?(?:no|deny|decline)\s+evolve\s+(e-[0-9a-f]{6})\s*[.!]*\s*$", re.I)),
    ("evolve_run", re.compile(r"^\s*/?evolve\s+run(?:\s+([a-z_][a-z0-9_]{1,40}))?\s*$", re.I)),
    ("evolve_propose", re.compile(r"^\s*/?evolve\s+propose(?:\s+([a-z_][a-z0-9_]{1,40}))?\s*$", re.I)),
    ("evolve", re.compile(r"^\s*/?evolve\s*\??\s*$", re.I)),
    ("integ_adopt", re.compile(r"^\s*/?(?:adopt|yes\s+adopt)\s+(i-[0-9a-f]{6})\s*[.!]*\s*$", re.I)),
    ("integ_skip", re.compile(r"^\s*/?skip\s+(i-[0-9a-f]{6})\s*[.!]*\s*$", re.I)),
    ("version", re.compile(r"^\s*/?(?:version|versions|aurix\s+version|changelog)\s*\??\s*$", re.I)),
    ("reviewed", re.compile(r"^\s*/?reviewed\s+(v?\d+\.\d+\.\d+)\s*$", re.I)),
    ("integrations", re.compile(r"^\s*/?(?:integrations?|integration\s+plans?)\s*\??\s*$", re.I)),
    ("workers", re.compile(r"^\s*/?(?:router|models|worker\s+registry|who\s+does\s+what)\s*\??\s*$", re.I)),   # "workers" = shards
    ("new", re.compile(r"^\s*/?mission\s*[:\-]?\s+(\S.{2,})$", re.I | re.S)),
]

HELP = qol.HELP
_STARTED = time.time()


_GAMES_ASK = re.compile(
    r"^(?=.*\b(?:steam|games?|mods?|modded|vortex|skyrim|fallout|cyberpunk|elden ring|spider-?man|no man'?s sky|baldur|dark souls|marvel rivals)\b)"
    r"(?=.*\?|\s*(?:what|which|how|why|is|are|do|does|can|could|tell|show|list|should|any)\b)(.{8,400})$", re.I | re.S)


def _bare_ack(text: str) -> Optional[Tuple[str, str]]:
    """A bare `ok` / `okay` / `got it` answers a waiting "what's new" announcement; with none waiting it is never swallowed."""
    if re.match(r"^\s*(?:ok|okay|got it|noted)\s*[.!]*\s*$", text or "", re.I) and announce.pending_ack():
        return "ack", ""
    return None


def _bare_fix_decision(text: str) -> Optional[Tuple[str, str]]:
    """A bare `yes` / `no` answers the ONE thing (a game fix or a repo) that is waiting on you; with none or several waiting it is never swallowed."""
    m = re.match(r"^\s*(yes|no)\s*[.!]*\s*$", text or "", re.I)
    if not m:
        return None
    ps, rs, fs, cs, ns = gaming.pending(), repos.pending(), freelance.pending(), content.pending(), learning.pending()
    us = [u for u in upgrades.all_proposals() if u["status"] == "review"]
    if len(ps) + len(rs) + len(us) + len(fs) + len(cs) + len(ns) != 1:
        return None
    yes = m.group(1).lower() == "yes"
    if ps:
        return ("fix_yes" if yes else "fix_no"), ps[0]["id"]
    if us:
        return ("upgrade_yes" if yes else "upgrade_no"), us[0]["id"]
    if fs:
        return ("freelance_yes" if yes else "freelance_no"), fs[0]["id"]
    if cs:
        return ("content_yes" if yes else "content_no"), cs[0]["id"]
    if ns:
        return ("learn_yes" if yes else "learn_no"), ns[0]["id"]
    return ("repo_yes" if yes else "repo_no"), rs[0]["id"]


def _bare_repo_link(text: str) -> Optional[Tuple[str, str]]:
    """A short message that is really just a GitHub link means "look at this repo" (long chat about a repo is left to the normal assistant)."""
    t = (text or "").strip()
    if len(t) <= 300 and repos.find_repo(t) and not t.lower().startswith(("mission", "standing", "forge", "project", "todo")):
        return "repo", t
    return None


def observe_owner_text(text: str) -> int:
    """Called by the listener for every owner message that is not a command: files memory proposals (you approve them with a tap)."""
    return fmemory.observe(text)


def parse(text: str) -> Optional[Tuple[str, str]]:
    """(kind, argument) for an owner control command, else None."""
    for kind, rx in _PATTERNS:
        m = rx.match(text or "")
        if m:
            if kind == "authorize":
                return kind, (m.group(1).lower() + (" " + m.group(2) if m.group(2) else ""))
            if kind == "money_set":                           # "<id> <status>"
                return kind, m.group(1).lower() + " " + m.group(2).lower()
            if kind == "run_skill":                           # "<name> [json]"; the JSON keeps its case
                return kind, m.group(1).lower() + (" " + m.group(2).strip() if m.group(2) else "")
            if kind == "project_act":                         # "<action> <id> [text]"; text keeps its case
                return kind, f"{m.group(1).lower()} {m.group(2).lower()}" + (f" {m.group(3).strip()}" if m.group(3) else "")
            if kind == "earned":                             # "<amount> <source> [note]"; note keeps its case
                return kind, f"{m.group(1)} {m.group(2).lower()}" + (f" {m.group(3).strip()}" if m.group(3) else "")
            arg = (m.group(1) or "").strip() if m.groups() else ""
            lowered = ("approve_mission", "deny_mission", "revoke", "approve_standing", "deny_standing",
                       "pause_standing", "resume_standing", "retire_standing", "show", "files", "send_files",
                       "todo_done", "skill_show", "approve_skill", "deny_skill", "retire_skill",
                       "teacher", "lesson_show", "approve_lesson", "deny_lesson", "retire_lesson", "fix_yes", "fix_no", "fix_undo", "repo_yes", "repo_no", "mem_yes", "mem_no", "upgrade_yes", "upgrade_no", "upgrade_undo", "upgrade_diff", "upgrades_cfg", "money_set", "shard_pause", "shard_resume", "freelance_yes", "freelance_no", "fab_yes", "fab_no", "content_yes", "content_no", "learn_yes", "learn_no", "land_yes", "land_no", "integ_adopt", "integ_skip", "evolve_yes", "evolve_no", "evolve_run", "evolve_propose")
            return kind, (arg.lower() if kind in lowered else arg)
    bare = _bare_skill_decision(text) or _bare_fix_decision(text) or _bare_ack(text) or _bare_repo_link(text)
    if bare:
        return bare
    home = _home_switch(text) if smarthome.configured() else None   # only claimed when Home Assistant is set up; otherwise chat
    if home:
        return home
    m = _GAMES_ASK.match(text or "")
    if m and not (text or "").lstrip().lower().startswith(("mission", "standing", "forge", "project", "todo", "approve", "deny")):
        return "games_ask", m.group(1).strip()
    return None


_HOME_VERB_FIRST = re.compile(r"^\s*/?(?:turn|switch)\s+(on|off)\s+(?:the\s+)?(.{1,60}?)\s*[.!]*\s*$", re.I)
_HOME_VERB_LAST = re.compile(r"^\s*/?(?:turn|switch)\s+(?:the\s+)?(.{1,60}?)\s+(on|off)\s*[.!]*\s*$", re.I)
_HOME_TOGGLE = re.compile(r"^\s*/?toggle\s+(?:the\s+)?(.{1,60}?)\s*[.!]*\s*$", re.I)


def _home_switch(text: str) -> Optional[Tuple[str, str]]:
    """'turn on desk lamp' / 'turn the desk lamp off' / 'toggle fan' -> ("home_switch", "on desk lamp")."""
    m = _HOME_VERB_FIRST.match(text or "")
    if m:
        return "home_switch", f"{m.group(1).lower()} {m.group(2).strip()}"
    m = _HOME_VERB_LAST.match(text or "")
    if m:
        return "home_switch", f"{m.group(2).lower()} {m.group(1).strip()}"
    m = _HOME_TOGGLE.match(text or "")
    if m:
        return "home_switch", f"toggle {m.group(1).strip()}"
    return None


_BARE_SKILL = re.compile(rf"^\s*/?(approve|deny)\s+{_SKILL}\s*[.!]*\s*$", re.I)


def _bare_skill_decision(text: str) -> Optional[Tuple[str, str]]:
    """`approve reverse_text` (the natural way to say it, without the word 'skill') - but ONLY when a skill with exactly
    that name is waiting on a decision, so ordinary chat ("approve budget") is never swallowed. Hex approval ids never get
    here: the listener handles those before this parser runs, and a name shaped like one is refused (use `approve skill ...`)."""
    m = _BARE_SKILL.match(text or "")
    if not m:
        return None
    verb, name = m.group(1).lower(), m.group(2).lower()
    if re.fullmatch(r"[0-9a-f]{6}", name):
        return None
    s = forge.load(name)
    if s is None or s.get("status") not in ("pending", "draft"):
        return None
    return f"{verb}_skill", name


def _in_quiet_hours(ln) -> bool:
    """AURIX_PULSE_QUIET_HOURS (default "23:00-07:00", local time, wraps past midnight). An unparseable window
    is treated as "never quiet" rather than silently going quiet forever on a typo."""
    window = os.environ.get("AURIX_PULSE_QUIET_HOURS", "23:00-07:00").strip()
    try:
        start_s, end_s = window.split("-")
        sh, sm = (int(x) for x in start_s.split(":"))
        eh, em = (int(x) for x in end_s.split(":"))
    except ValueError:
        return False
    now_mins, start_mins, end_mins = ln.hour * 60 + ln.minute, sh * 60 + sm, eh * 60 + em
    if start_mins <= end_mins:
        return start_mins <= now_mins < end_mins
    return now_mins >= start_mins or now_mins < end_mins


class Foundation:
    def __init__(self, run_agent: Callable[[str], Awaitable[str]], notify: Callable[[str], Awaitable[None]],
                 llm=None, session_id: Optional[str] = None, store: Optional[ms.MissionStore] = None,
                 on_stop: Optional[Callable[[], None]] = None,
                 standing_store: Optional[st.StandingStore] = None,
                 send_file: Optional[Callable[[str, str], Awaitable[bool]]] = None,
                 registry: Optional[projects.Registry] = None, **presence_kw):
        self.run_agent, self.notify, self.llm = run_agent, notify, llm
        self.send_file = send_file
        self.registry = registry or projects.Registry()
        self.session_id = session_id
        self.store = store or ms.MissionStore()
        self.standing = standing_store or st.StandingStore()
        self.on_stop = on_stop
        self.presence_kw = presence_kw
        self.runner_task: Optional[asyncio.Task] = None

    # -- runner management -------------------------------------------------
    def running(self) -> bool:
        return self.runner_task is not None and not self.runner_task.done()

    async def _presence(self) -> Dict:
        """What is installed IN THE SANDBOX (when there is one), overridden by explicit kwargs."""
        return {**(await asyncio.to_thread(sandbox.presence_kw)), **self.presence_kw}

    async def _evals(self, which: str) -> str:
        """`evals [plan|code|last]`: the scoreboard. Runs in the background (minutes); the result arrives as a message."""
        if which == "last":
            return evals.render_latest()
        task = getattr(self, "eval_task", None)
        if task is not None and not task.done():
            return "An eval run is already in progress; I will send the scoreboard when it finishes."
        tiers = ("plan", "code") if which == "all" else (which,)
        n = sum(len(evals.load_tasks(t)) for t in tiers)
        self.eval_task = asyncio.create_task(self._run_evals(tiers))
        return (f"📊 Running {n} eval task(s) ({' + '.join(tiers)}). Nothing real is touched: plans are drafted but not stored, code runs in the "
                "sandbox. This takes a few minutes; I will send the scoreboard. <code>evals last</code> shows the previous one.")

    async def _run_evals(self, tiers) -> None:
        try:
            sandboxed = await asyncio.to_thread(sandbox.available)
            text = await evals.run_and_record(tiers, self.llm, sandboxed, presence_kw=await self._presence(),
                                              model=os.environ.get("AURIX_EVAL_MODEL_LABEL", ""))
        except Exception as e:                                  # an eval failure must never take the command loop down
            text = f"Eval run failed: {html.escape(repr(e)[:300])}"
        await self.notify_ui(text, "evals")

    async def _teach(self) -> str:
        """`teach`: ask the frontier teacher about the code tasks that failed in the latest eval run (background; capped, redacted)."""
        ok, why = teacher.can_call()
        if not ok:
            return why
        task = getattr(self, "teach_task", None)
        if task is not None and not task.done():
            return "A teaching run is already in progress; I will send the lessons when it finishes."
        self.teach_task = asyncio.create_task(self._run_teach())
        return "🎓 Asking the teacher about the failing eval tasks (capped, redacted, hidden test cases are never sent). I will send the lessons."

    async def _run_teach(self) -> None:
        try:
            text = await evals.teach_failures(self.llm)
        except Exception as e:
            text = f"Teaching run failed: {html.escape(repr(e)[:300])}"
        await self.notify_ui(text, "lessons")

    async def notify_ui(self, text: str, kind: str = "") -> None:
        """Send a message with the obvious next taps as buttons (when the transport supports them; tests' plain notify just gets the text)."""
        kb = buttons.for_reply(kind, text)
        if kb:
            try:
                await self.notify(text, kb)
                return
            except TypeError:
                pass
        await self.notify(text)

    async def _maybe_fast_lane(self, m) -> str:
        """Sandbox-only / read-only missions start at once (fastlane.check decides, in code); everything else waits for the owner."""
        from src import approval_gate as ag
        proposal = ms.render_proposal(m)
        if not fastlane.enabled() or ag.stop_engaged():
            return proposal
        ok, reasons = fastlane.check(m)
        if not ok:
            return proposal
        reply = self.store.activate(m.id, decided_by="policy:fast_lane")
        if "ACTIVE" not in reply:                                   # e.g. another mission is still running
            return proposal + "\n<i>Fast lane skipped: " + html.escape(reply) + "</i>"
        fastlane.announce(m, reasons)
        await self._start_runner(m.id)
        plan = "\n".join(f"{i}. {html.escape(s.title)}" for i, s in enumerate(m.steps[:8], 1))
        return (f"⚡ <b>Fast lane</b> - started <code>{m.id}</code> without waiting ({html.escape(', '.join(reasons[:2]))}).\n"
                f"<b>Goal:</b> {html.escape(m.objective[:300])}\n{plan}\n"
                "<i>Audited as policy:fast_lane. <code>status</code> any time, <code>stop</code> to kill it, "
                "<code>fast lane off</code> to always ask.</i>")

    async def _start_runner(self, mission_id: str) -> None:
        kw = await self._presence()
        runner = MissionRunner(mission_id, self.run_agent, self.notify, store=self.store, **kw)
        self.runner_task = asyncio.create_task(runner.run())

    async def tick_standing(self, now: Optional[float] = None) -> list:
        """One scheduler pass; the listener calls this about once a minute."""
        actions: list = []
        live = [s for s in self.standing.all() if s.status in (st.StandingStatus.ACTIVE, st.StandingStatus.PAUSED)]
        if live:
            sched = st.StandingScheduler(self.standing, self.store, self._start_runner, self.notify,
                                         now=(lambda: now) if now else time.time, **(await self._presence()))
            actions = await sched.tick()
        await self._maybe_digest(now or time.time())
        try:                                    # a more frequent, lighter update must never break scheduling either
            await self._maybe_pulse(now or time.time())
        except Exception:
            pass
        await self._maybe_nightshift(now or time.time())
        try:                                    # the nightly self-check must never break scheduling either
            await self._maybe_nightly_evals(now or time.time())
        except Exception:
            pass
        try:                                    # noticing a repeated gap must never break scheduling either
            await self._maybe_forge_autonomy(now or time.time())
        except Exception:
            pass
        try:                                    # LandPilot proposing its own research mission must never break scheduling either
            await self._maybe_land_research(now or time.time())
        except Exception:
            pass
        try:                                    # freelance checking its own job feed must never break scheduling either
            await self._maybe_freelance_search(now or time.time())
        except Exception:
            pass
        try:                                    # camera alerts (Frigate) must never break scheduling either
            if frigate.configured():
                for text in await asyncio.to_thread(frigate.poll, now, None, frigate.default_describer()):
                    await self.notify_ui(text, "frigate")
        except Exception:
            pass
        try:                                    # the learning flywheel + "what's new" (both read-only on existing records)
            experience.harvest()
            for text in announce.tick(now or time.time()):
                await self.notify_ui(text, "announce")
        except Exception:
            pass
        try:                                    # upgrade lane: new proposals, outcomes, and (when on) the next draft in a background thread
            for text in upgrades.tick(now):
                await self.notify_ui(text)
        except Exception:
            pass
        try:                                    # memory: things worth remembering / forgetting, waiting for your yes/no (small file reads, no thread hop)
            for text in fmemory.tick(now):
                await self.notify_ui(text)
        except Exception:
            pass
        try:                                    # repo links: new reports and the outcome of approvals (small file reads, no thread hop)
            for text in repos.tick(now):
                await self.notify_ui(text)
        except Exception:
            pass
        try:                                    # integration lane: announce finished plans; plan the next absorbed repo in a background thread
            for text in integrate.tick(now):
                await self.notify_ui(text)
        except Exception:
            pass
        try:                                    # game doctor: fix results + new proposals from a fresh PC report
            for text in gaming.tick(now):                       # small file reads only: no thread hop, so the tick adds no suspension point
                await self.notify_ui(text)
        except Exception:
            pass
        try:
            await self.tick_watchdog(now)
        except Exception:                       # the watchdog must never break scheduling
            pass
        try:
            announcement = xp.check_level_up()          # fast: cached read of the audit file, no thread hop
            if announcement:
                await self.notify(announcement)
        except Exception:                       # a scoreboard must never break scheduling either
            pass
        return actions

    async def tick_watchdog(self, now: Optional[float] = None) -> list:
        """Tell the owner about problems the immune system sees (see watchdog.py); throttled by AURIX_WATCHDOG_MINUTES."""
        mins = watchdog.interval_minutes()
        now = now or time.time()
        state = watchdog.load_state()
        if not mins or now - state.get("last_check", 0) < mins * 60:
            return []
        snap = await asyncio.to_thread(sysview.snapshot)
        try:                                                    # homelab reachability (cached ~15 s); never allowed to break the watchdog
            from src.foundation import command_center
            snap["homelab"] = await asyncio.to_thread(command_center.probe_homelab)
        except Exception:
            snap["homelab"] = []
        snap["backup"] = watchdog.read_backup_status()
        messages, new_state = watchdog.evaluate(snap, state, now, watchdog.stuck_info(self.store, now))
        boot_text, new_state["last_boot"] = watchdog.boot_check(snap, state, now)
        if boot_text:
            messages.insert(0, boot_text)
        watchdog.save_state(new_state)
        for text in messages:
            audit.append("watchdog_alert", preview=text[:120])
            await self.notify(text)
        return messages

    async def startup_notice(self, now: Optional[float] = None) -> Optional[str]:
        """After a restart: if a mission was mid-flight its runner died with the process; say so (max once per 10 min)."""
        now = now or time.time()
        state = watchdog.load_state()
        if now - state.get("last_startup_notice", 0) < 600:
            return None
        text = watchdog.startup_text(self.store, self.running())
        if text:
            state["last_startup_notice"] = now
            watchdog.save_state(state)
            await self.notify(text)
        return text

    async def _maybe_nightly_evals(self, now_ts: float) -> None:
        """AURIX checks itself while you sleep: rerun the evals once a day (AURIX_EVALS_AT, default 03:15; empty = off), only when no
        mission is running. Silent unless something got WORSE (or, with the teacher on, lessons were proposed for what failed)."""
        at = os.environ.get("AURIX_EVALS_AT", "03:15").strip()
        if not at:
            return
        try:
            hh, mm = (int(x) for x in at.split(":"))
        except ValueError:
            return
        ln = st.local_now(now_ts)
        today = ln.strftime("%Y-%m-%d")
        state = evals.eval_dir() / "nightly.json"
        try:
            last = json.loads(state.read_text(encoding="utf-8")).get("date", "")
        except (OSError, ValueError):
            last = ""
        task = getattr(self, "eval_task", None)
        if last == today or (ln.hour, ln.minute) < (hh, mm) or self.running() or (task is not None and not task.done()):
            return
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"date": today}), encoding="utf-8")
        self.eval_task = asyncio.create_task(self._nightly_evals())

    async def _nightly_evals(self) -> None:
        before = evals.load_history(1)
        try:
            sandboxed = await asyncio.to_thread(sandbox.available)
            text = await evals.run_and_record(("plan", "code"), self.llm, sandboxed, presence_kw=await self._presence(),
                                              model=os.environ.get("AURIX_EVAL_MODEL_LABEL", ""))
            now = evals.load_history(1)
            worse = evals.regressions(before[-1] if before else None, now[-1] if now else None)
            msgs = ["⚠️ <b>Overnight eval: something got worse</b> (" + html.escape(", ".join(worse)) + ")\n" + text] if worse else []
            if teacher.can_call()[0] and evals.load_failures():
                msgs.append(await evals.teach_failures(self.llm))
        except Exception as e:
            msgs = [f"Overnight eval failed: {html.escape(repr(e)[:300])}"]
        if msgs:
            await self.notify("\n\n".join(msgs))

    async def _maybe_nightshift(self, now_ts: float) -> None:
        """Once a night (AURIX_NIGHTSHIFT_AT, default 02:00) build the overnight gifts; free, local, read-only. Skipped while a mission runs."""
        at = os.environ.get("AURIX_NIGHTSHIFT_AT", "02:00").strip()
        if not at or self.running():
            return
        try:
            hh, mm = (int(x) for x in at.split(":"))
        except ValueError:
            return
        if (st.local_now(now_ts).hour, st.local_now(now_ts).minute) < (hh, mm):
            return
        try:
            await asyncio.to_thread(nightshift.run_shift, now_ts)
        except Exception as e:
            audit.append("nightshift_failed", why=repr(e)[:200])

    async def _maybe_forge_autonomy(self, now_ts: float) -> None:
        """Once a day, notice a REAL repeated gap (the same mission objective genuinely blocked more
        than once) and let the forge draft a candidate skill for it on its own - see
        forge.check_autonomous_trigger. Skipped while a mission runs (it would compete for the same
        sandbox). The result still only ever reaches `pending`: approval stays the owner's."""
        if self.running():
            return
        today = st.local_now(now_ts).strftime("%Y-%m-%d")
        state_path = forge.forge_dir() / "autonomy_check.json"
        try:
            last = json.loads(state_path.read_text(encoding="utf-8")).get("date", "")
        except (OSError, ValueError):
            last = ""
        if last == today:
            return
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"date": today}), encoding="utf-8")
        try:
            result = await forge.check_autonomous_trigger(self.llm)
        except Exception as e:
            audit.append("forge_autonomy_failed", why=repr(e)[:200])
            return
        if result:
            await self.notify_ui(result, "skills")   # the real tap-to-approve buttons were missing: plain notify() never attaches them

    async def _maybe_land_research(self, now_ts: float) -> None:
        """LandPilot's own autonomous trigger (see land/hub.py's check_research_trigger): at most once every
        RESEARCH_COOLDOWN_DAYS, if the owner has set search criteria, propose a real mission to go find leads.
        Skipped while a mission runs, same reasoning as forge autonomy above - never auto-started either way."""
        if self.running():
            return
        try:
            sandboxed = await asyncio.to_thread(sandbox.available)
            result = await land.check_research_trigger(self.llm, store_=self.store, session_id=self.session_id,
                                                        sandboxed=sandboxed, now=now_ts, **(await self._presence()))
        except Exception as e:
            audit.append("land_research_failed", why=repr(e)[:200])
            return
        if result:
            await self.notify_ui(result, "land")   # same bug as forge autonomy above - plain notify() never attaches the tap-to-approve buttons

    async def _maybe_freelance_search(self, now_ts: float) -> None:
        """2026-10-01, owner: "why aren't these running since approved" about the Money panel's active ideas -
        freelance.find_lead() already does the real work (checks a real public job feed, drafts a solution,
        never bids/messages/pays anyone) but was only ever triggered by a typed `freelance find` - nothing
        called it on its own. Once a day, same cooldown shape as forge/land autonomy above."""
        if self.running():
            return
        today = st.local_now(now_ts).strftime("%Y-%m-%d")
        state_path = freelance._data() / "autonomy_check.json"
        try:
            last = json.loads(state_path.read_text(encoding="utf-8")).get("date", "")
        except (OSError, ValueError):
            last = ""
        if last == today:
            return
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"date": today}), encoding="utf-8")
        try:
            result = await asyncio.to_thread(freelance.find_lead)
        except Exception as e:
            audit.append("freelance_autonomy_failed", why=repr(e)[:200])
            return
        if result.startswith("Could not check for real leads"):
            audit.append("freelance_autonomy_unreachable", why=result[:200])   # a feed outage is not worth a daily ping
            return
        if result and not result.startswith("Checked a real remote-jobs feed - nothing new"):
            await self.notify_ui(result, "freelance")

    async def _maybe_digest(self, now_ts: float) -> None:
        """Opt-in: AURIX_DIGEST_AT=07:00 sends one owner digest per local day."""
        at = os.environ.get("AURIX_DIGEST_AT", "").strip()
        if not at:
            return
        try:
            hh, mm = (int(x) for x in at.split(":"))
        except ValueError:
            return
        ln = st.local_now(now_ts)
        today = ln.strftime("%Y-%m-%d")
        state = self.standing.dir / "_digest.json"
        try:
            last = json.loads(state.read_text(encoding="utf-8")).get("date", "")
        except (OSError, ValueError):
            last = ""
        if last == today or (ln.hour, ln.minute) < (hh, mm):
            return
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"date": today}), encoding="utf-8")
        audit.append("digest_sent", date=today)
        if nightshift.unopened():
            await self.notify_ui(await asyncio.to_thread(nightshift.unwrap, None, True, False), "unwrap")
        await self.notify_ui(heartbeat.overnight_digest(self.store), "digest")

    async def _maybe_pulse(self, now_ts: float) -> None:
        """Opt-in, same convention as _maybe_digest: AURIX_PULSE_EVERY_MINUTES=120 ("every couple of hours",
        owner's explicit ask 2026-10-01) sends a lighter, more frequent companion to the once-a-day digest -
        self-improve/evolve progress, skill requests, LandPilot activity, project updates. Unset or 0 = off (no
        code-level default - a default here would silently add a notification to every other test file in this
        project that calls tick_standing without expecting one; this owner's deployment sets the env var itself).
        Never fires during AURIX_PULSE_QUIET_HOURS (default 23:00-07:00, local time): the owner is asleep while
        AURIX keeps ruminating/testing/reviewing in the background, not waiting on a ping every two hours."""
        try:
            minutes = int(os.environ.get("AURIX_PULSE_EVERY_MINUTES", "0").strip() or "0")
        except ValueError:
            return
        if minutes <= 0:
            return
        ln = st.local_now(now_ts)
        if _in_quiet_hours(ln):
            return
        state = self.standing.dir / "_pulse.json"
        try:
            last = json.loads(state.read_text(encoding="utf-8")).get("ts", 0)
        except (OSError, ValueError):
            last = 0
        if now_ts - last < minutes * 60:
            return
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"ts": now_ts}), encoding="utf-8")
        audit.append("pulse_sent", minutes=minutes)
        await self.notify_ui(heartbeat.pulse_digest(), "pulse")

    # -- dispatch -----------------------------------------------------------
    async def handle(self, kind: str, arg: str = "") -> str:
        from src import approval_gate as ag
        try:                                                # which commands are actually used feeds evolution (kinds only, never message text)
            experience.count_usage(kind)
        except Exception:
            pass
        if kind == "new":
            try:
                sandboxed = await asyncio.to_thread(sandbox.available)
                m = await planner.propose_mission(arg, llm=self.llm, session_id=self.session_id,
                                                  store=self.store, sandboxed=sandboxed,
                                                  **(await self._presence()))
            except ValueError:
                return "Give me a goal, e.g. `mission: convert my CT scan of a skull into an STL`."
            return await self._maybe_fast_lane(m)

        if kind == "approve_mission":
            reply = self.store.activate(arg, decided_by="owner")
            if "ACTIVE" not in reply:
                return reply
            ag.clear_stop()
            await self._start_runner(arg)
            return reply + " Starting now. Send `status` any time, or `stop`."

        if kind == "deny_mission":
            return self.store.deny(arg, decided_by="owner")

        if kind == "standing_new":
            try:
                sandboxed = await asyncio.to_thread(sandbox.available)
                sm = await st.propose_standing(arg, self.llm, self.session_id, self.standing,
                                               sandboxed=sandboxed, **(await self._presence()))
            except ValueError as e:
                return f"Could not set that up: {e}. Example: `standing: Reynolds Gang ongoing treasure hunt every day at 03:00`."
            except PermissionError as e:
                return f"Not as a standing mission: {e}"
            return st.render_standing(sm)

        if kind == "standing_list":
            return st.describe_all(self.standing)

        if kind in ("approve_standing", "deny_standing", "pause_standing", "resume_standing", "retire_standing"):
            action = {"approve_standing": self.standing.approve, "deny_standing": self.standing.deny,
                      "pause_standing": self.standing.pause, "resume_standing": self.standing.resume,
                      "retire_standing": self.standing.retire}[kind]
            return action(arg)

        if kind == "stop":
            ag.engage_stop(120)
            ag.handle_command("deny", "all", resolver="stop")
            paused = self.standing.pause_all("owner STOP")
            m = self.store.stop_active("owner STOP")
            killed = await asyncio.to_thread(sandbox.kill, m.id) if m else 0
            if self.runner_task and not self.runner_task.done():
                self.runner_task.cancel()
            if self.on_stop:
                try:
                    self.on_stop()
                except Exception:
                    pass
            audit.append("owner_stop", mission=m.id if m else None, standing_paused=paused,
                         sandbox_processes_killed=killed)
            return ("⛔ <b>STOPPED.</b> " + (f"Mission [{m.id}] halted. " if m else "")
                    + (f"{killed} running sandbox process(es) killed. " if killed else "")
                    + (f"Standing missions paused: {', '.join(paused)}. " if paused else "")
                    + "All pending approvals denied; nothing further will run for 2 minutes.")

        if kind == "status":
            return heartbeat.self_report(self.store)

        if kind == "resume":
            m = self.store.active()
            if m is None:
                return "No active mission to resume."
            if self.running():
                return f"Mission [{m.id}] is already running."
            ag.clear_stop()
            await self._start_runner(m.id)
            return f"Resuming mission [{m.id}] at step {m.current_step + 1}."

        if kind == "help":
            return qol.help_text()
        if kind == "ping":
            up = int((time.time() - _STARTED) / 60)
            return f"pong - up {up // 60}h{up % 60:02d}m" + (" · mission running" if self.running() else "")
        if kind == "level":
            return xp.render()
        if kind == "disk":
            return await asyncio.to_thread(qol.disk_text)
        if kind == "homelab":
            return await asyncio.to_thread(qol.homelab_text)
        if kind == "trades":
            return await asyncio.to_thread(qol.trades_text)
        if kind == "pulse":
            return await asyncio.to_thread(heartbeat.pulse_digest)
        if kind == "evals":
            return await self._evals((arg or "all").lower())
        if kind == "teacher":
            if arg.startswith("on"):
                n = int(arg.split()[1]) if len(arg.split()) > 1 else 5
                return teacher.set_config(True, n)
            if arg == "off":
                return teacher.set_config(False, 0)
            return teacher.status_text()
        if kind == "teach":
            return await self._teach()
        if kind == "lessons":
            return teacher.render_list()
        if kind == "lesson_show":
            return teacher.render_show(arg)
        if kind == "approve_lesson":
            return teacher.approve(arg)
        if kind == "deny_lesson":
            return teacher.deny(arg)
        if kind == "retire_lesson":
            return teacher.retire(arg)
        if kind == "timeline":
            return await asyncio.to_thread(growth.timeline_text)
        if kind == "trust":
            return await asyncio.to_thread(growth.trust_text)
        if kind == "ack":
            return announce.acknowledge()
        if kind == "menu":
            return "What would you like to do? Tap a button."
        if kind == "dashboard":
            def _dash():
                from src.foundation import command_center
                snap = command_center.snapshot()
                return addons.dashboard_text(snap["monitors"], snap["addons"])
            return await asyncio.to_thread(_dash)
        if kind == "run_all":
            return addons.render_run_all(await asyncio.to_thread(addons.run_all))
        if kind == "games":
            return await asyncio.to_thread(gaming.games_text)
        if kind == "games_refresh":
            return await asyncio.to_thread(gaming.request_refresh)
        if kind == "game":
            return await asyncio.to_thread(gaming.game_text, arg)
        if kind == "games_ask":
            return await gaming.ask(arg, self.llm)
        if kind == "shards":
            return await asyncio.to_thread(shards.text)
        if kind == "shard_pause":
            return await asyncio.to_thread(shards.set_paused, arg, True)
        if kind == "shard_resume":
            return await asyncio.to_thread(shards.set_paused, arg, False)
        if kind == "gp_pair":
            return await asyncio.to_thread(gamepilot.confirm_pairing, arg)
        if kind == "gp_arm":
            return await asyncio.to_thread(gamepilot.arm, int(arg) if arg.isdigit() else gamepilot.ARM_DEFAULT_MIN)
        if kind == "gp_disarm":
            return await asyncio.to_thread(gamepilot.disarm)
        if kind == "gp_unpair":
            return await asyncio.to_thread(gamepilot.unpair_all)
        if kind == "gp_status":
            return await asyncio.to_thread(gamepilot.status_text)
        if kind == "unwrap":
            return await asyncio.to_thread(nightshift.unwrap)
        if kind == "night_now":
            res = await asyncio.to_thread(nightshift.run_shift, None, True)
            return ("Could not run the shift: " + res["skipped"]) if res.get("skipped") else await asyncio.to_thread(nightshift.unwrap)
        if kind == "lab":
            return await asyncio.to_thread(money.lab_text)
        if kind == "transcripts":
            return await asyncio.to_thread(transcription.status_text)
        if kind == "freelance":
            return await asyncio.to_thread(freelance.request, arg)
        if kind == "freelance_yes":
            return await asyncio.to_thread(freelance.approve, arg)
        if kind == "freelance_no":
            return await asyncio.to_thread(freelance.decline, arg)
        if kind == "freelance_status":
            return await asyncio.to_thread(freelance.status_text)
        if kind == "land":
            return await asyncio.to_thread(land.overview)
        if kind == "version":
            return await asyncio.to_thread(versions.status_text)
        if kind == "reviewed":
            return await asyncio.to_thread(versions.mark_reviewed, arg)
        if kind == "integrations":
            return await asyncio.to_thread(integrate.list_text)
        if kind in ("integ_adopt", "integ_skip"):
            msg, idea = await asyncio.to_thread(integrate.decide, arg, kind == "integ_adopt")
            if idea is None:
                return msg
            plan, _ = integrate.find_idea(arg)
            spec = integrate.spec_for_lane(plan, idea)
            if idea["kind"] == "upgrade":                    # enters the upgrade lane: drafted by the coder worker, tested, then `yes u-...`
                return "✅ Adopted → upgrade lane.\n" + await asyncio.to_thread(upgrades.add_idea, spec)
            return "✅ Adopted → skill forge.\n" + await forge.propose(spec, self.llm)      # drafted + tested in the sandbox, then `approve skill`
        if kind == "workers":
            return await asyncio.to_thread(fworkers.status_text)
        if kind == "land_show":
            return await asyncio.to_thread(land.show, arg)
        if kind == "land_criteria":
            return await asyncio.to_thread(land.set_criteria, arg)
        if kind == "land_leads":
            leads = await asyncio.to_thread(land.leads, arg)
            if not leads:
                return f"No leads yet at <code>{arg}</code> (still running, found nothing, or not a lead-scan mission)."
            lines = [f"🗺️ {len(leads)} lead(s) from {arg}:"]
            for l in leads[:20]:
                lines.append(f"• {html.escape(str(l.get('address_or_parcel', '?')))} ({html.escape(str(l.get('county', '?')))}, "
                            f"{html.escape(str(l.get('state', '?')))}) - ${l.get('asking_price_usd', '?')} - {html.escape(str(l.get('why_promising', '')))[:120]}")
            return "\n".join(lines)
        if kind == "land_build":
            return await asyncio.to_thread(land.build, arg)
        if kind == "land_cap":
            return await asyncio.to_thread(land.cap_command, arg)
        if kind == "land_promote":
            mid, _, name = arg.partition(" ")
            return await asyncio.to_thread(land.promote, mid.lower(), name.strip())
        if kind == "land_propose":
            return await asyncio.to_thread(land.propose, arg)
        if kind == "land_yes":
            return await asyncio.to_thread(land.approve, arg)
        if kind == "land_no":
            return await asyncio.to_thread(land.decline, arg)
        if kind == "evolve":
            return await asyncio.to_thread(evolve.overview_text)
        if kind == "evolve_run":
            name = arg or (evolve.domains()[0] if len(evolve.domains()) == 1 else "")
            if not name:
                return "Which domain? " + ", ".join(evolve.domains())
            await evolve.advance(name, self.llm)
            return await asyncio.to_thread(evolve.status_text, name)
        if kind == "evolve_propose":
            name = arg or (evolve.domains()[0] if len(evolve.domains()) == 1 else "")
            if not name:
                return "Which domain? " + ", ".join(evolve.domains())
            return await asyncio.to_thread(evolve.propose_promotion, name)
        if kind == "evolve_yes":
            return await asyncio.to_thread(evolve.approve_any, arg)
        if kind == "evolve_no":
            return await asyncio.to_thread(evolve.decline_any, arg)
        if kind == "fab":
            return await asyncio.to_thread(fabricate.request, arg)
        if kind == "fab_yes":
            return await asyncio.to_thread(fabricate.approve, arg)
        if kind == "fab_no":
            return await asyncio.to_thread(fabricate.decline, arg)
        if kind == "fab_status":
            return await asyncio.to_thread(fabricate.status_text)
        if kind == "printer":
            return await asyncio.to_thread(fprinter.status_text)
        if kind == "look":
            return await asyncio.to_thread(vision.look, arg)
        if kind == "home":
            return await asyncio.to_thread(smarthome.overview)
        if kind == "home_switch":
            action, _, name = arg.partition(" ")
            return await asyncio.to_thread(smarthome.switch, action, name)
        if kind == "freelance_find":
            return await asyncio.to_thread(freelance.find_lead)
        if kind == "content":
            return await asyncio.to_thread(content.request, arg)
        if kind == "content_yes":
            return await asyncio.to_thread(content.approve, arg)
        if kind == "content_no":
            return await asyncio.to_thread(content.decline, arg)
        if kind == "content_status":
            return await asyncio.to_thread(content.status_text)
        if kind == "learn":
            return await asyncio.to_thread(learning.request, arg)
        if kind == "learn_yes":
            return await asyncio.to_thread(learning.approve, arg)
        if kind == "learn_no":
            return await asyncio.to_thread(learning.decline, arg)
        if kind == "learn_status":
            return await asyncio.to_thread(learning.status_text)
        if kind == "subscriptions_add":
            return await asyncio.to_thread(subscriptions.add, arg)
        if kind == "subscriptions_remove":
            return await asyncio.to_thread(subscriptions.remove, arg)
        if kind == "subscriptions_status":
            return await asyncio.to_thread(subscriptions.render_summary)
        if kind == "earnings":
            return await asyncio.to_thread(earnings.render)
        if kind == "earned":
            m = re.match(r"(\S+)\s+(\S+)\s*(.*)", arg, re.S)
            amount, source, note = (m.group(1), m.group(2), m.group(3)) if m else (arg, "", "")
            try:
                amt = float(amount)
            except ValueError:
                return "Say it like `earned 45 transcription` (amount, then a source)."
            return await asyncio.to_thread(earnings.record, amt, source, note)
        if kind == "money":
            return await asyncio.to_thread(money.list_text)
        if kind == "money_show":
            return await asyncio.to_thread(money.detail_text, arg)
        if kind == "money_set":
            ident, status = arg.split(None, 1)
            return await asyncio.to_thread(money.set_status, ident, status)
        if kind == "workspace":
            from src.foundation import teacher as _teacher
            return await asyncio.to_thread(_teacher.set_workspace, arg)
        if kind == "upgrade_add":
            return await asyncio.to_thread(upgrades.add_idea, arg)
        if kind == "upgrade_now":
            ok, why = upgrades.can_draft()
            if not ok:
                return why
            import threading as _th
            _th.Thread(target=upgrades.run_once, kwargs={"notify_no_result": True}, daemon=True, name="aurix-upgrade-now").start()
            return "🛠️ Working on the next upgrade now. Drafting and testing takes a few minutes; I will message you when there is something to approve (or why not)."
        if kind == "upgrade_openhands":
            ok, why = upgrades.can_draft()
            if not ok:
                return why
            import threading as _th
            _th.Thread(target=upgrades.run_once_openhands, daemon=True, name="aurix-upgrade-openhands").start()
            return "🛠️ Handing the next upgrade to OpenHands instead of my own prompt. Takes a few minutes; I will message you when there is something to approve (or why not)."
        if kind == "upgrades_cfg":
            parts = arg.lower().split()
            return await asyncio.to_thread(upgrades.set_config, parts[0] == "on", int(parts[1]) if len(parts) > 1 else 3)
        if kind == "upgrades":
            return await asyncio.to_thread(upgrades.status_text)
        if kind == "upgrade_yes":
            return await asyncio.to_thread(upgrades.approve, arg)
        if kind == "upgrade_no":
            return await asyncio.to_thread(upgrades.decline, arg)
        if kind == "upgrade_undo":
            return await asyncio.to_thread(upgrades.undo, arg)
        if kind == "upgrade_diff":
            return await asyncio.to_thread(upgrades.diff_text, arg)
        if kind == "remember":
            return await asyncio.to_thread(fmemory.remember, arg, "owner:telegram")
        if kind == "forget":
            return await asyncio.to_thread(fmemory.forget, arg)
        if kind == "unforget":
            return await asyncio.to_thread(fmemory.unforget)
        if kind == "recall":
            return await asyncio.to_thread(fmemory.recall_text, arg)
        if kind == "memory":
            return await asyncio.to_thread(fmemory.list_text)
        if kind == "mem_yes":
            return await asyncio.to_thread(fmemory.approve, arg)
        if kind == "mem_no":
            return await asyncio.to_thread(fmemory.decline, arg)
        if kind == "repo":
            return await asyncio.to_thread(repos.request, arg)
        if kind == "repos":
            return await asyncio.to_thread(repos.list_text)
        if kind == "repo_yes":
            return await asyncio.to_thread(repos.approve, arg)
        if kind == "repo_no":
            return await asyncio.to_thread(repos.decline, arg)
        if kind == "fixes":
            return await asyncio.to_thread(gaming.render_pending)
        if kind == "fix_yes":
            return await asyncio.to_thread(gaming.approve, arg)
        if kind == "fix_no":
            return await asyncio.to_thread(gaming.decline, arg)
        if kind == "fix_undo":
            return await asyncio.to_thread(gaming.undo, arg)
        if kind == "fastlane":
            if arg in ("on", "off"):
                return fastlane.set_enabled(arg == "on")
            return fastlane.status_text()
        if kind == "forge":
            return await forge.propose(arg, self.llm)
        if kind == "skills":
            return forge.render_list()
        if kind == "skill_show":
            return forge.render_show(arg)
        if kind == "approve_skill":
            return forge.approve(arg)
        if kind == "deny_skill":
            return forge.deny(arg)
        if kind == "retire_skill":
            return forge.retire(arg)
        if kind == "run_skill":
            name, _, args_json = arg.partition(" ")
            return await forge.run_skill(name, args_json)
        if kind == "history":
            return qol.missions_history(self.store)
        if kind == "show":
            return qol.show(self.store, self.standing, arg)
        if kind == "approvals":
            return qol.approvals_text()
        if kind == "log":
            return qol.log_text(int(arg) if arg else 12)
        if kind == "files":
            return qol.files_text(self.store, arg or None)
        if kind == "send_files":
            return await qol.deliver_files(self.store, self.send_file, arg or None)
        if kind == "tools":
            return qol.tools_text(**(await self._presence()))
        if kind == "doctor":
            return qol.doctor_text(await asyncio.to_thread(sysview.snapshot))
        if kind == "authorizations":
            return qol.authorizations_text()

        if kind == "projects":
            return self.registry.render_projects()
        if kind == "project_new":
            return self.registry.add_project(arg)
        if kind == "project_act":
            action, pid, *rest = arg.split(" ", 2)
            return self.registry.update_project(action, pid, rest[0] if rest else "")
        if kind == "todos":
            return self.registry.render_todos()
        if kind == "todo_new":
            return self.registry.add_todo(arg)
        if kind == "todo_done":
            return self.registry.complete_todo(arg)

        if kind == "authorize":
            name, _, expires = arg.partition(" ")
            return self.authorize(name, expires or None)

        if kind == "revoke":
            return self.revoke(arg)

        return HELP

    # -- owner authorizations ---------------------------------------------------
    def authorize(self, name: str, expires: Optional[str] = None) -> str:
        if name in NOT_YET_AUTHORIZATIONS:
            audit.append("authorization_refused", name=name)
            return NOT_YET_AUTHORIZATIONS[name]
        if name.startswith("worker-"):                         # one cloud model endpoint (workers.py): only a REAL one, never a pattern
            from src.foundation import workers as _w
            real = {f"worker-{_w._slug(w.title.split(' @ ', 1)[-1])}" for w in _w.registry() if w.locality == "cloud"}
            if name not in real:
                return (f"'{name}' is not one of your cloud model endpoints. Add the endpoint in Settings → Model Endpoints first; "
                        "then `models` shows the exact name to authorize." + (f" Known: {', '.join(sorted(real))}." if real else ""))
            granted = cap.load_authorizations()
            granted[name] = {"granted_by": "owner", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **({"expires": expires} if expires else {})}
            self._write_authorizations(granted)
            audit.append("authorization_granted", name=name, expires=expires)
            return (f"✅ Cloud worker <code>{name}</code> enabled for PUBLIC work. Add <code>authorize cloud-internal</code> to let it "
                    f"also take AURIX's own code and drafts. Private data never goes to it. <code>revoke {name}</code> turns it off.")
        c = cap.lookup(name)
        if c is None or c.kind != "authorization":
            known = ", ".join(sorted(x.id for x in cap.REGISTRY.values() if x.kind == "authorization"
                                     and x.id not in NOT_YET_AUTHORIZATIONS))
            return f"'{name}' is not an authorization I know. Known: {known}."
        granted = cap.load_authorizations()
        granted[c.id] = {"granted_by": "owner", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                         **({"expires": expires} if expires else {})}
        self._write_authorizations(granted)
        audit.append("authorization_granted", name=c.id, expires=expires)
        return f"Authorization '{c.id}' granted" + (f" until {expires}." if expires else ".")

    def revoke(self, name: str) -> str:
        c = cap.lookup(name)
        granted = cap.load_authorizations()
        key = c.id if c else name
        if key not in granted:
            return f"'{name}' is not currently granted."
        del granted[key]
        self._write_authorizations(granted)
        audit.append("authorization_revoked", name=key)
        return f"Authorization '{key}' revoked."

    @staticmethod
    def _write_authorizations(granted: Dict[str, dict]) -> None:
        path = cap.authorizations_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(granted, indent=2), encoding="utf-8")
        os.replace(tmp, path)
