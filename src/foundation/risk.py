"""src/foundation/risk.py - the risk model (activation handoff §2).

    Risk = Impact x Probability x (1 - Reversibility)

* Irreversible actions floor at HIGH regardless of apparent size.
* Anything not recognised ("unclassified / novel") defaults to MAXIMUM risk (HIGH,
  score 1.0) until the owner explicitly authorises it.
* Touching a structurally protected component (identity.PROTECTED_COMPONENTS) is
  CRITICAL: a hard deny that no approval or mission contract can unlock.

Tiers drive the gate: LOW runs (audited), MEDIUM needs a mission contract or a ping,
HIGH always needs the owner's individual approval, CRITICAL is refused.

Stdlib only. The bash analysis is deliberately conservative: anything it cannot parse
or does not positively recognise is HIGH, never LOW.
"""
from __future__ import annotations

import os
import posixpath
import re
import shlex
from dataclasses import dataclass
from enum import IntEnum
from typing import List, Optional, Tuple

from src.foundation import identity


class RiskTier(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


LOW_MAX_SCORE = 0.05
MEDIUM_MAX_SCORE = 0.25
IRREVERSIBLE_AT_OR_BELOW = 0.1

# (impact, probability, reversibility)
_LOW = (0.05, 1.0, 0.95)
_MEDIUM = (0.4, 0.8, 0.5)
_HIGH = (0.8, 0.9, 0.1)
_MAXIMUM = (1.0, 1.0, 0.0)          # unclassified


def score(impact: float, probability: float, reversibility: float) -> float:
    return impact * probability * (1.0 - reversibility)


def tier_for(impact: float, probability: float, reversibility: float) -> RiskTier:
    s = score(impact, probability, reversibility)
    tier = RiskTier.LOW if s < LOW_MAX_SCORE else RiskTier.MEDIUM if s < MEDIUM_MAX_SCORE else RiskTier.HIGH
    if reversibility <= IRREVERSIBLE_AT_OR_BELOW:
        tier = max(tier, RiskTier.HIGH)          # irreversible floors at HIGH
    return tier


@dataclass(frozen=True)
class RiskAssessment:
    tier: RiskTier
    impact: float
    probability: float
    reversibility: float
    reasons: Tuple[str, ...] = ()

    @property
    def score(self) -> float:
        return score(self.impact, self.probability, self.reversibility)

    @property
    def hard_deny(self) -> bool:
        return self.tier == RiskTier.CRITICAL

    def describe(self) -> str:
        return f"{self.tier.name} (score {self.score:.2f}): " + "; ".join(self.reasons[:3])


def _mk(profile: Tuple[float, float, float], reason: str, tier: Optional[RiskTier] = None) -> RiskAssessment:
    i, p, r = profile
    return RiskAssessment(tier or tier_for(i, p, r), i, p, r, (reason,))


def low(reason: str) -> RiskAssessment:
    return _mk(_LOW, reason)


def medium(reason: str) -> RiskAssessment:
    return _mk(_MEDIUM, reason)


def high(reason: str) -> RiskAssessment:
    return _mk(_HIGH, reason)


def unclassified(reason: str) -> RiskAssessment:
    return _mk(_MAXIMUM, "unclassified: " + reason)


def critical(reason: str) -> RiskAssessment:
    return _mk(_MAXIMUM, reason, RiskTier.CRITICAL)


def worst(items: List[RiskAssessment]) -> RiskAssessment:
    if not items:
        return low("nothing to do")
    top = max(items, key=lambda a: (a.tier, a.score))
    reasons: List[str] = []
    for a in sorted(items, key=lambda a: (-a.tier, -a.score)):
        for r in a.reasons:
            if r not in reasons:
                reasons.append(r)
    return RiskAssessment(top.tier, top.impact, top.probability, top.reversibility, tuple(reasons))


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

def within_workspace(path: str, workspace: Optional[str]) -> bool:
    """Absolute path strictly inside the mission workspace, with no '..' tricks."""
    if not workspace:
        return False
    raw = str(path).replace("\\", "/")
    absolute = raw.startswith("/") or re.match(r"^[A-Za-z]:/", raw) is not None
    if ".." in raw.split("/") or not absolute:
        return False
    p = posixpath.normpath(raw)
    ws = posixpath.normpath(str(workspace).replace("\\", "/"))
    return p == ws or p.startswith(ws.rstrip("/") + "/")


def _write_target(path: str, workspace: Optional[str], what: str) -> RiskAssessment:
    if identity.is_protected_path(path):
        return critical(f"{what} would modify protected component "
                        f"'{identity.protected_component_for(path)}' ({path})")
    if within_workspace(path, workspace):
        return medium(f"{what} inside mission workspace")
    return high(f"{what} outside the mission workspace ({path})")


# ---------------------------------------------------------------------------
# bash
# ---------------------------------------------------------------------------

_SPLIT_TOKENS = {";", "&&", "||", "|", "&", "|&", ";;", "(", ")"}
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_READ_VERBS = {
    "ls", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "df", "du", "free",
    "uptime", "uname", "date", "whoami", "id", "pwd", "hostname", "ps", "stat", "file", "which",
    "type", "sort", "uniq", "diff", "tree", "lsblk", "lscpu", "nproc", "basename", "dirname",
    "realpath", "echo", "printf", "true", "false", "test", "[", "nvidia-smi", "cd", "ffprobe",
    "sha256sum", "md5sum", "cut", "tr", "less", "more", "nl", "column", "jq", "seq", "sleep",
}
_PURE_READ = {"cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg", "ls", "stat",
              "wc", "diff", "file", "sha256sum", "md5sum", "cut", "nl", "column", "jq", "tree",
              "realpath", "basename", "dirname"}
_ALWAYS_HIGH = {
    "sudo", "su", "doas", "kill", "pkill", "killall", "systemctl", "service", "docker", "podman",
    "apt", "apt-get", "dpkg", "dnf", "yum", "snap", "rpm", "reboot", "shutdown", "poweroff",
    "mkfs", "dd", "shred", "truncate", "mount", "umount", "iptables", "ufw", "crontab", "at",
    "ssh", "scp", "sftp", "rsync", "nc", "ncat", "socat", "telnet", "ftp", "bash", "sh", "zsh",
    "eval", "exec", "source", ".", "xargs", "nohup", "env", "printenv", "awk", "gawk", "perl",
    "ruby", "node", "lua", "php", "chattr", "passwd", "useradd", "usermod", "visudo",
}
_GIT_LOW = {"status", "log", "diff", "show", "branch", "rev-parse", "ls-files", "remote", "blame",
            "shortlog", "describe", "tag", "config", "help", "version", "--version"}
_GIT_MEDIUM = {"add", "commit", "init", "clone", "fetch", "pull", "switch", "stash", "merge", "mv"}
_CURL_UPLOAD = {"-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "-F", "--form",
                "-T", "--upload-file", "--json"}
_CURL_UNSAFE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}


# Read-only forms of commands that are otherwise privileged (owner decision 2026-09-20: "commands we regularly run or can easily
# deem as safe"). Deliberately narrow and exact: only these verb+subcommand shapes, no shell metacharacters (the segment splitter has
# already removed those), and none that can print secrets (no `docker inspect`, `docker logs`, `systemctl show`, `printenv`).
_SYSTEMCTL_READ = {"status", "is-active", "is-enabled", "is-failed", "list-units", "list-timers", "list-unit-files", "list-sockets"}
_SYSTEMCTL_OK_FLAGS = {"--user", "--no-pager", "-l", "--full", "--failed", "--all", "-a", "--state", "--type", "-t", "--no-legend", "-n"}
_DOCKER_READ = {("ps",), ("images",), ("version",), ("info",), ("top",), ("port",), ("compose", "ps"), ("compose", "ls"),
                ("container", "ls"), ("image", "ls"), ("volume", "ls"), ("network", "ls"), ("system", "df")}
_DOCKER_OK_FLAGS = {"-a", "--all", "--no-trunc", "--format", "-q", "--quiet", "--filter", "-f", "--no-stream", "--size", "-s", "--version"}
_IP_READ = {"addr", "address", "a", "route", "r", "link", "l", "neigh", "n"}
_JOURNAL_BAD = ("--vacuum", "--rotate", "--flush", "--sync", "--relinquish", "--setup-keys", "--update-catalog", "--smart-relinquish")


def _readonly_privileged(verb: str, args: List[str]) -> Optional[str]:
    """A reason string if `verb args` is one of the known read-only forms of an otherwise-privileged command, else None."""
    pos = [a for a in args if not a.startswith("-")]
    flags = [a for a in args if a.startswith("-")]
    if verb == "systemctl":
        if pos and pos[0] in _SYSTEMCTL_READ and all(f.split("=", 1)[0] in _SYSTEMCTL_OK_FLAGS for f in flags):
            return f"systemctl {pos[0]} (read-only)"
    elif verb == "docker":
        key2, key1 = tuple(pos[:2]), tuple(pos[:1])
        if (key2 in _DOCKER_READ or key1 in _DOCKER_READ or (pos[:1] == ["stats"] and "--no-stream" in flags)) and all(
                f.split("=", 1)[0] in _DOCKER_OK_FLAGS for f in flags):
            return "docker " + " ".join(pos[:2]) + " (read-only)"
    elif verb == "crontab":
        if args == ["-l"]:
            return "crontab -l (read-only)"
    elif verb == "tailscale":
        if pos[:1] in (["status"], ["ip"], ["version"]) and len(pos) == 1:
            return f"tailscale {pos[0]} (read-only)"
    elif verb in ("ss", "netstat"):
        if all(f.lstrip("-").replace("=", "").isalnum() for f in flags):
            return f"{verb} (read-only)"
    elif verb == "ip":
        if pos[:1] and pos[0] in _IP_READ and (len(pos) == 1 or pos[1] in ("show", "list", "ls")):
            return "ip (read-only)"
    elif verb == "journalctl":
        if not any(f.startswith(_JOURNAL_BAD) for f in flags):
            return "journalctl (read-only)"
    return None


def _flags(words: List[str]) -> List[str]:
    return [w for w in words[1:] if w.startswith("-") and len(w) > 1]


def _positional(words: List[str]) -> List[str]:
    return [w for w in words[1:] if not (w.startswith("-") and len(w) > 1)]


def _assess_segment(words: List[str], workspace: Optional[str]) -> RiskAssessment:
    while words and _ASSIGN_RE.match(words[0]):
        words = words[1:]
    if not words:
        return low("assignment only")
    verb = os.path.basename(words[0])
    args = words[1:]
    flags = _flags(words)
    pos = _positional(words)

    # path scans (apply to every verb)
    found: List[RiskAssessment] = []
    for w in args:
        val = w.split("=", 1)[1] if w.startswith("-") and "=" in w else w
        if val.startswith("-"):
            continue
        if identity.is_protected_path(val) and verb not in _PURE_READ:
            found.append(critical(f"'{verb}' references protected component "
                                  f"'{identity.protected_component_for(val)}' ({val})"))
        elif identity.is_secret_path(val):
            found.append(high(f"'{verb}' touches credentials ({val})"))
    if any(a.tier == RiskTier.CRITICAL for a in found):
        return worst(found)

    safe_reason = _readonly_privileged(verb, args) if verb in ("systemctl", "docker", "crontab", "tailscale", "ss", "netstat", "ip",
                                                                "journalctl") else None
    if safe_reason:
        found.append(low(safe_reason))
    elif verb in _ALWAYS_HIGH:
        found.append(high(f"'{verb}' is privileged, remote, or runs arbitrary code"))
    elif verb in ("python", "python3", "py"):
        if any(f in ("--version", "-V") for f in flags) and len(args) == 1:
            found.append(low("python version"))
        elif args[:2] == ["-m", "pip"]:
            found.append(_assess_pip(["pip"] + args[2:]))
        else:
            found.append(high("runs arbitrary Python (no sandbox yet)"))
    elif verb in ("pip", "pip3"):
        found.append(_assess_pip(words))
    elif verb == "git":
        found.append(_assess_git(pos, flags))
    elif verb in ("curl", "wget"):
        found.append(_assess_net(verb, args, workspace))
    elif verb == "ollama":
        sub = pos[0] if pos else ""
        found.append(low("ollama read") if sub in ("list", "ps", "show") else
                     medium("ollama model download") if sub == "pull" else
                     high(f"ollama {sub or 'command'}"))
    elif verb == "ffmpeg":
        out = pos[-1] if pos else ""
        found.append(_write_target(out, workspace, "ffmpeg output") if out else medium("ffmpeg"))
    elif verb in ("mkdir", "touch", "tee", "ln", "chmod", "chown", "rmdir"):
        found.append(worst([_write_target(p, workspace, verb) for p in pos]) if pos
                     else medium(verb))
    elif verb == "cp":
        found.append(_write_target(pos[-1], workspace, "cp destination") if pos else medium("cp"))
    elif verb == "mv":
        found.append(worst([_write_target(p, workspace, "mv") for p in pos]) if pos else medium("mv"))
    elif verb == "rm":
        found.append(worst([_write_target(p, workspace, "rm") if within_workspace(p, workspace)
                            else high(f"rm outside the mission workspace ({p})") for p in pos])
                     if pos else high("rm"))
    elif verb == "find":
        bad = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf"}
        found.append(high("find can delete or execute") if bad & set(args) else low("find (read)"))
    elif verb == "sed":
        if any(f.startswith("-i") or f == "--in-place" for f in flags):
            found.append(worst([_write_target(p, workspace, "sed -i") for p in pos[1:]] or
                               [high("sed -i")]))
        else:
            found.append(low("sed (read)"))
    elif verb == "sort" and any(f == "-o" or f.startswith("--output") for f in flags):
        found.append(high("sort -o writes a file"))
    elif verb == "date" and any(f in ("-s", "--set") or f.startswith("--set=") for f in flags):
        found.append(high("date sets the system clock"))
    elif verb == "hostname" and pos:
        found.append(high("hostname with an argument changes the hostname"))
    elif verb in _READ_VERBS:
        found.append(low(f"{verb} (read-only)"))
    else:
        found.append(unclassified(f"unknown command '{verb}'"))
    return worst(found)


def _assess_pip(words: List[str]) -> RiskAssessment:
    args = words[1:]
    sub = next((a for a in args if not a.startswith("-")), "")
    if sub in ("list", "show", "freeze", "check", "--version"):
        return low("pip read")
    if sub in ("install", "download", "uninstall"):
        sketchy = [a for a in args if a in ("-e", "--editable", "-i", "--index-url", "--extra-index-url",
                                            "--trusted-host", "-r", "--requirement")
                   or "://" in a or a.startswith("git+")]
        if sketchy:
            return high("pip install from a custom source or URL (supply-chain risk)")
        return medium("pip package install/removal")
    return high(f"pip {sub or 'command'}")


def _assess_git(pos: List[str], flags: List[str]) -> RiskAssessment:
    sub = pos[0] if pos else (flags[0] if flags else "")
    if sub in _GIT_MEDIUM:
        return medium(f"git {sub}")
    if sub in _GIT_LOW:
        if sub in ("config",) or (sub in ("branch", "tag") and len(pos) > 1):
            return medium(f"git {sub} changes repository state")
        return low(f"git {sub} (read)")
    return high(f"git {sub or 'command'} may discard or publish work")


def _assess_net(verb: str, args: List[str], workspace: Optional[str]) -> RiskAssessment:
    out: List[RiskAssessment] = []
    upload = False
    for i, a in enumerate(args):
        base = a.split("=", 1)[0]
        if base in _CURL_UPLOAD or base in ("--post-data", "--post-file", "--body-data", "--body-file"):
            upload = True
        if base in ("-X", "--request", "--method"):
            method = (a.split("=", 1)[1] if "=" in a else (args[i + 1] if i + 1 < len(args) else "")).upper()
            if method in _CURL_UNSAFE_METHODS:
                upload = True
        if base in ("-o", "--output", "-O", "--output-document", "-P", "--directory-prefix"):
            tgt = a.split("=", 1)[1] if "=" in a else (args[i + 1] if i + 1 < len(args) else "")
            if tgt and not tgt.startswith("-"):
                out.append(_write_target(tgt, workspace, f"{verb} download target"))
    out.append(high(f"{verb} sends data to a remote server") if upload
               else medium(f"{verb} network read"))
    return worst(out)


def assess_bash(command: str, workspace: Optional[str] = None) -> RiskAssessment:
    text = (command or "").strip()
    if not text:
        return low("empty command")
    if "$(" in text or "`" in text or "<(" in text or ">(" in text:
        return high("command/process substitution is opaque to static analysis")
    if "<<" in text:
        return high("here-document is opaque to static analysis")
    results: List[RiskAssessment] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or (line.startswith("#") and not line.startswith("#!")):
            continue
        if line.startswith("#!"):          # e.g. the #!bg marker
            continue
        try:
            lex = shlex.shlex(line, posix=True, punctuation_chars=True)
            lex.whitespace_split = True
            lex.commenters = ""
            tokens = list(lex)
        except ValueError as e:
            return high(f"could not parse command ({e})")
        seg: List[str] = []
        redirects: List[Tuple[str, str]] = []
        i = 0

        def flush():
            nonlocal seg, redirects
            if seg:
                results.append(_assess_segment(seg, workspace))
            for op, target in redirects:
                if "<" in op:
                    if identity.is_secret_path(target):
                        results.append(high(f"reads credentials via redirect ({target})"))
                    continue
                if target in ("/dev/null", "/dev/stderr", "/dev/stdout"):
                    continue
                results.append(_write_target(target, workspace, "output redirect"))
            seg, redirects = [], []

        while i < len(tokens):
            t = tokens[i]
            if t in _SPLIT_TOKENS:
                flush()
            elif set(t) <= set("<>&|") and ("<" in t or ">" in t):
                nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
                if t.endswith("&") and nxt.isdigit():           # 2>&1 style fd duplication
                    i += 1
                elif nxt:
                    redirects.append((t, nxt))
                    i += 1
            else:
                seg.append(t)
            i += 1
        flush()
    return worst(results)


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

_HIGH_TOOLS = {
    "send_email": "sends email to the outside world (irreversible)",
    "reply_to_email": "sends email to the outside world (irreversible)",
    "api_call": "arbitrary API call",
    "app_api": "calls the app's own API (can reach privileged routes)",
    "manage_tasks": "creates persistent scheduled work",
    "manage_skills": "changes the agent's own persistent instructions",
    "manage_endpoints": "changes where models run",
    "manage_mcp": "adds or changes tool servers",
    "manage_webhooks": "creates inbound/outbound triggers",
    "manage_tokens": "creates credentials",
    "manage_settings": "changes app settings",
    "vault_get": "reads a stored secret",
    "vault_unlock": "unlocks the secret vault",
}
_MEDIUM_TOOLS = {
    "manage_contact": "edits contacts",
    "download_model": "large model download",
    "serve_model": "starts a model server",
    "serve_preset": "starts a model server",
    "stop_served_model": "stops a model server",
    "adopt_served_model": "changes model registry",
}
_MCP_READ = re.compile(r"(^|_)(list|read|get|search|query|recall|find|show|status|describe)(_|$)")
_MCP_MUTATE = re.compile(r"(delete|remove|send|write|exec|run|create|update|set|archive|move|reply|bulk|post|put|kill|drop)")


_SANDBOX_TOOLS = {"bash", "mcp__bash__bash", "python", "mcp__python__python"}


def _assess_sandboxed(tool: str, content: str, workspace: Optional[str]) -> RiskAssessment:
    """bash/python that will run INSIDE the isolated sandbox container (no secrets, no network,
    read-only root, only the mission workspace tree writable). Contained, so MEDIUM - but:
    * an attempt on a protected component stays CRITICAL (it signals an attack, and is denied);
    * touching paths outside THIS mission's workspace stays HIGH: other missions' files live
      in the same tree, so that is the one thing the container does not protect."""
    base = assess_action(tool, content, workspace, sandboxed=False)
    if base.tier == RiskTier.CRITICAL:
        return base
    if any("outside the mission workspace" in r for r in base.reasons):
        return high("touches paths outside this mission's workspace (other missions' files live there)")
    return medium("runs contained in the isolated sandbox (no secrets, no network)")


def assess_action(tool: str, content: str = "", workspace: Optional[str] = None,
                  sandboxed: bool = False) -> RiskAssessment:
    """Assess one tool call. Unknown tools are HIGH (maximum) until authorised.
    `sandboxed=True` applies only to bash/python routed into the sandbox container."""
    tool = (tool or "").strip()
    content = content or ""
    if sandboxed and tool in _SANDBOX_TOOLS:
        return _assess_sandboxed(tool, content, workspace)
    if tool == "bash" or tool == "mcp__bash__bash":
        return assess_bash(content, workspace)
    if tool in ("python", "mcp__python__python"):
        return high("runs arbitrary Python (no sandbox yet)")
    if tool in ("write_file", "mcp__filesystem__write_file"):
        path = content.split("\n", 1)[0].strip()
        if not path:
            return high("write_file with no path")
        return _write_target(path, workspace, "write_file")
    if tool in ("read_file", "mcp__filesystem__read_file"):
        path = content.split("\n", 1)[0].strip()
        if identity.is_secret_path(path):
            return high(f"read_file on credentials ({path})")
        return low("read_file")
    if tool == "web_search":
        return low("web search")
    if tool in _HIGH_TOOLS:
        return high(_HIGH_TOOLS[tool])
    if tool in _MEDIUM_TOOLS:
        return medium(_MEDIUM_TOOLS[tool])
    if tool.startswith("mcp__"):
        name = tool.split("__", 2)[-1].lower()
        if _MCP_READ.search(name) and not _MCP_MUTATE.search(name):
            return low(f"MCP read tool {tool}")
        return high(f"MCP tool {tool} may change state")
    return unclassified(f"unknown tool '{tool}'")
