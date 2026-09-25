"""src/foundation/identity.py - AURIX's persistent identity (activation handoff §10.3).

Typed, independent of any model or machine. Also owns the definition of what is
STRUCTURALLY PROTECTED (§2): the eight components an agent must never be able to
edit, by any combination of tools. risk.py turns a match here into a hard deny.
"""
from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional


class TrustTier(str, Enum):
    OWNER = "owner"
    DELEGATED = "delegated"
    UNTRUSTED = "untrusted"


# Spec §2: "Eight components are structurally protected". Patterns are matched against
# normalised paths ("/" separators). A pattern with a "/" is a path fragment; one
# without is a basename glob. Deliberately over-inclusive: it also covers backups.
PROTECTED_COMPONENTS: Dict[str, List[str]] = {
    "sentinel_safety_logic": ["src/foundation/risk.py", "src/foundation/__init__.py"],
    "owner_authority": [
        "src/foundation/identity.py", "src/foundation/commands.py",
        "services/telegram/listener.py", "core/auth.py", "core/middleware.py",
        "data/identity.json", "data/authorizations.json",
    ],
    "approval_gate": [
        "src/approval_gate.py", "src/tool_execution.py", "src/tool_security.py",
        "src/foundation/mission.py", "src/foundation/runner.py", "src/foundation/standing.py",
        "src/foundation/sandbox.py", "src/foundation/forge.py", "data/missions/", "data/standing/", "data/forge/",
        # 2026-09-20: what may start without a tap (fastlane), the frontier teacher + its approved lessons, and the exam itself
        # (an agent must not edit its own approval policy, its own teacher, or its own scoreboard)
        "src/foundation/fastlane.py", "src/foundation/teacher.py", "src/foundation/evals.py", "evals/",
        "src/foundation/gaming.py", "data/gaming/", "gaming/",
        "src/foundation/repos.py", "scripts/aurix_absorb_agent.py", "data/repos/", "src/foundation/memory.py", "data/memory/",
        "src/foundation/shards.py", "data/shards/", "src/foundation/gamepilot.py", "routes/gamepilot_routes.py", "data/gamepilot/", "src/foundation/money.py", "money/", "data/money/", "src/foundation/upgrades.py", "scripts/aurix_upgrade_agent.py", "scripts/aurix_deploy.sh", "data/upgrades/",
        "data/fastlane.json", "data/teacher.json", "data/teacher_usage.json", "data/lessons/", "data/evals/",
    ],
    "credential_custody": [".env", "*.env", ".env.*", "data/auth.json", "data/ssh/"],
    "audit_history": ["src/foundation/audit.py", "src/foundation/watchdog.py", "data/audit.jsonl", "data/audit.jsonl.lock",
                      "data/audit_ack.json", "data/approvals.jsonl", "data/watchdog.json"],
    "recovery_mechanisms": ["docker-compose.yml", "Dockerfile", "docker/entrypoint.sh"],
    "constitution": ["constitution/", "data/constitution/"],
    "financial_ledger": ["data/ledger/", "runtime/trade_state.json", "runtime/trade_history.json"],
}

# Reading these is not "editing a protected component" but is still credential exposure.
SECRET_PATH_PATTERNS: List[str] = [
    ".env", "*.env", ".env.*", "auth.json", "id_rsa*", "id_ed25519*", "*.pem", "*.key",
    "credentials*", ".ssh/", "data/ssh/", "/proc/*/environ", "/proc/self/environ",
]


def _norm(path: str) -> str:
    p = str(path).replace("\\", "/").strip().strip("'\"")
    return "/" + p.lstrip("/")


def _match(path: str, patterns: List[str]) -> Optional[str]:
    norm = _norm(path)
    base = norm.rsplit("/", 1)[-1]
    for pat in patterns:
        if "/" in pat.strip("/") or pat.endswith("/") or pat.startswith("/"):
            frag = "/" + pat.lstrip("/")
            if "*" in frag:
                if fnmatch.fnmatch(norm, frag) or fnmatch.fnmatch(norm, "*" + frag):
                    return pat
            elif frag in norm or (pat.endswith("/") and (norm + "/").startswith(frag)):
                return pat
        elif fnmatch.fnmatch(base, pat):
            return pat
    return None


def protected_component_for(path: str) -> Optional[str]:
    """Name of the protected component `path` belongs to, or None."""
    for name, patterns in PROTECTED_COMPONENTS.items():
        if _match(path, patterns):
            return name
    return None


def is_protected_path(path: str) -> bool:
    return protected_component_for(path) is not None


def is_secret_path(path: str) -> bool:
    return _match(path, SECRET_PATH_PATTERNS) is not None


@dataclass
class AurixIdentity:
    name: str = "AURIX"
    owner_name: str = "Rascal"
    owner_channels: Dict[str, str] = field(default_factory=dict)   # channel -> sender id
    constitution_version: str = "activation-handoff-2026-09"
    prime_directive: str = ("Advance the owner's real interests continuously, pursuing outcomes "
                            "rather than literal instructions, while remaining accountable to him "
                            "as final authority on anything consequential.")

    def tier_for(self, channel: str, sender_id: str) -> TrustTier:
        """Only an authenticated owner channel is OWNER tier; everything else is UNTRUSTED."""
        expected = self.owner_channels.get(channel, "")
        if expected and str(sender_id) == str(expected):
            return TrustTier.OWNER
        return TrustTier.UNTRUSTED

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def identity_path() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "identity.json"


def load_identity() -> AurixIdentity:
    """data/identity.json if present, else built from the environment."""
    try:
        raw = json.loads(identity_path().read_text(encoding="utf-8"))
        return AurixIdentity(**{k: v for k, v in raw.items() if k in AurixIdentity.__dataclass_fields__})
    except (OSError, ValueError, TypeError):
        pass
    channels = {}
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if chat_id:
        channels["telegram"] = chat_id
    return AurixIdentity(owner_channels=channels)
