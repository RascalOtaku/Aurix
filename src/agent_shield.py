"""src/agent_shield.py - static scan of skill text and MCP server configs before Aurix trusts them.

The best of ECC's AgentShield ("scan prompts, hooks, MCP configurations and agent files for injection and
misconfiguration before execution") and OpenClaw-community scanners like clawdefender, cut down to what Aurix
actually loads: SKILL.md text (Skills UI, absorbed checkouts) and MCP server definitions (env / presets).

Deterministic regexes, no model call: cheap enough to run on every save, and a skill cannot talk its way past it.
  high   -> refuse (the skill is not saved, the MCP server is not started, the checkout file is not loaded)
  medium -> allowed, but reported so the owner sees it

Findings describe the pattern, never echo long matches (a hostile skill should not get its payload reprinted).
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List

_I = re.IGNORECASE

TEXT_RULES = [
    # (rule id, severity, regex, what it means)
    ("instruction-override", "high",
     re.compile(r"\b(ignore|disregard|forget|override)\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier|system|safety)\s+"
                r"(instructions?|rules?|prompts?|guidelines?|polic(y|ies))", _I),
     "tries to override the system prompt"),
    ("persona-jailbreak", "high",
     re.compile(r"\b(you are now|act as|enter)\s+(DAN|an? unrestricted|developer mode|jailbroken?)\b", _I),
     "jailbreak persona"),
    ("conceal-from-owner", "high",
     re.compile(r"\b(do not|don't|never)\s+(tell|inform|show|mention|reveal)\s+(this\s+)?(to\s+)?(the\s+)?(user|owner|human)\b", _I),
     "asks to hide actions from the owner"),
    ("pipe-to-shell", "high",
     re.compile(r"\b(curl|wget|iwr|Invoke-WebRequest)\b[^\n|]{0,300}\|\s*(sudo\s+)?(ba|z|da)?sh\b", _I),
     "downloads and executes a remote script"),
    ("secret-exfiltration", "high",
     re.compile(r"\b(curl|wget|nc|ncat|Invoke-WebRequest|requests\.post|fetch)\b[^\n]{0,200}"
                r"(\$\{?[A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*|\.ssh/|id_rsa|id_ed25519|\.env\b|/etc/shadow)", _I),
     "sends secrets or key files over the network"),
    ("secret-read", "high",
     re.compile(r"\b(cat|type|Get-Content|less|head|tail)\s+[^\n]{0,80}(\.ssh/id_|id_ed25519|/etc/shadow|\.env\b|credentials\.json|api_keys\.json)", _I),
     "reads credential files"),
    ("protected-components", "high",
     re.compile(r"\b(disable|bypass|skip|turn off|remove|edit|patch|tamper with)\s+(the\s+)?"
                r"(approval[- ]gate|approval checks?|audit[- ](chain|log|trail))\b", _I),
     "targets Aurix's approval gate / audit / contracts"),
    ("destructive-rm", "high",
     re.compile(r"\brm\s+-[a-z]*r[a-z]*f?[a-z]*\s+(--no-preserve-root\s+)?(/|~|\$HOME|/\*)(\s|$)", _I),
     "recursive delete of / or home"),
    ("hidden-unicode", "high",
     re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿]|[\U000e0000-\U000e007f]"),
     "invisible / bidi / tag characters (Unicode smuggling)"),
    ("encoded-payload", "medium",
     re.compile(r"(base64\s+(-d|--decode)|b64decode|FromBase64String|atob\()", _I),
     "decodes an embedded payload"),
    ("privilege", "medium",
     re.compile(r"\bsudo\b|\bchmod\s+(-R\s+)?777\b|--privileged\b", _I),
     "asks for elevated privileges"),
    ("skip-verification", "medium",
     re.compile(r"--no-verify\b|--insecure\b|verify\s*=\s*False|-k\s+https://", _I),
     "turns off TLS or hook verification"),
]

_SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", _I)
_SHELL = {"sh", "bash", "zsh", "dash", "cmd", "cmd.exe", "powershell", "pwsh"}


def scan_text(text: str, source: str = "") -> List[Dict[str, str]]:
    findings = []
    for rule, severity, rx, why in TEXT_RULES:
        if rx.search(text or ""):
            findings.append({"rule": rule, "severity": severity, "why": why, "source": source})
    return findings


def scan_mcp_server(server: Dict[str, Any]) -> List[Dict[str, str]]:
    """Risky shapes in an MCP server definition (from MCP_SERVERS, MCP_SERVER_URL or a preset)."""
    name = str(server.get("name") or server.get("id") or "mcp")
    out: List[Dict[str, str]] = []

    def add(rule, severity, why):
        out.append({"rule": rule, "severity": severity, "why": why, "source": name})

    command = str(server.get("command") or "")
    args = [str(a) for a in (server.get("args") or [])]
    base = command.replace("\\", "/").rsplit("/", 1)[-1].lower()
    joined = " ".join([command] + args)
    if base in _SHELL and any(a in ("-c", "/c", "-Command", "-EncodedCommand") for a in args):
        add("shell-wrapper", "high", "runs an inline shell command instead of an MCP server binary")
    for f in scan_text(joined, name):
        if f["severity"] == "high":
            out.append(f)
    url = str(server.get("url") or "")
    if url.lower().startswith("http://"):
        from src.spend_ledger import is_local
        if not is_local(url):
            add("plaintext-remote", "medium", "remote MCP server over plain http")
    env = server.get("env") or {}
    leaked = sorted(k for k in env if _SECRET_ENV.search(str(k)))
    if leaked and not str(server.get("id", "")).startswith("builtin_preset_"):
        add("secrets-in-env", "medium", f"passes secret-looking variables to the server: {', '.join(leaked)[:120]}")
    if base in ("npx", "uvx", "pipx") and args and not any("@" in a.lstrip("@") or "==" in a for a in args if not a.startswith("-")):
        add("unpinned-package", "medium", "package version is not pinned (runs whatever is newest)")
    return out


def blocking(findings: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    return [f for f in findings if f.get("severity") == "high"]


def summary(findings: Iterable[Dict[str, str]]) -> str:
    return "; ".join(f"{f['severity']}: {f['why']} [{f['rule']}]" for f in findings)
