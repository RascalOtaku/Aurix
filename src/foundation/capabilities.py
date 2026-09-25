"""src/foundation/capabilities.py - "what does this request actually need?"

A registry of capabilities (Python packages, binaries, credentials, services, and
OWNER AUTHORIZATIONS) plus DOMAIN PACKS: expert knowledge for a kind of work - the tools
it needs, the legal/ethical gates it has, and a sane default plan. Packs mean a weak local
model cannot forget a legal gate: whatever the LLM plans, the pack's requirements are
always merged in.

`identify(goal)` -> what is present, what AURIX can install itself (pip into the
persisted volume, MEDIUM risk), what only the owner can do (apt/winget/Dockerfile, or
credentials and authorizations), and what is unknown (treated as maximum risk).

Presence checks never print secret values and never touch the network unless a `tcp:`
check is explicitly given a probe.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class InstallPlan:
    where: str          # container_pip | dockerfile | host_apt | windows_winget | manual
    command: str
    note: str = ""

    @property
    def autonomous(self) -> bool:
        return self.where == "container_pip"


@dataclass(frozen=True)
class Capability:
    id: str
    description: str
    kind: str                       # builtin | python_pkg | binary | credential | authorization | service
    check: str = ""                 # module | binary | ENV_VAR | authorization key | tool name | tcp:host:port
    install: Optional[InstallPlan] = None
    aliases: Tuple[str, ...] = ()
    legal: str = ""


def _pip(pkg: str, module: str, desc: str, *aliases: str) -> Capability:
    return Capability(pkg, desc, "python_pkg", module,
                      InstallPlan("container_pip", f"pip install --user {pkg}"), tuple(aliases))


REGISTRY: Dict[str, Capability] = {c.id: c for c in [
    # built-in agent tools
    Capability("bash", "Run shell commands in the app container", "builtin", "bash"),
    Capability("write_file", "Write files", "builtin", "write_file"),
    Capability("read_file", "Read files", "builtin", "read_file"),
    Capability("web_search", "Search the web (SearXNG)", "builtin", "web_search", aliases=("search", "research")),
    Capability("browser", "Drive a headless browser", "builtin", "builtin_browser", aliases=("scrape",)),
    # medical imaging / 3D
    _pip("numpy", "numpy", "Array math"),
    _pip("scipy", "scipy", "Scientific computing"),
    _pip("pydicom", "pydicom", "Read DICOM CT/MRI series", "dicom"),
    _pip("SimpleITK", "SimpleITK", "Medical image loading, resampling, segmentation", "simpleitk", "sitk", "itk"),
    _pip("scikit-image", "skimage", "Marching cubes surface extraction", "skimage", "marching-cubes"),
    _pip("trimesh", "trimesh", "Mesh repair, watertight checks, STL export", "mesh"),
    _pip("numpy-stl", "stl", "STL read/write and volume", "stl"),
    _pip("pymeshlab", "pymeshlab", "MeshLab filters: hole filling, smoothing, decimation", "meshlab"),
    _pip("pillow", "PIL", "Image handling", "pil"),
    # media
    Capability("ffmpeg", "Transcode audio/video", "binary", "ffmpeg",
               InstallPlan("dockerfile", "add 'ffmpeg' to the apt-get line in odysseus/Dockerfile, then docker compose up -d --build",
                           "Or run on the GPU box: winget install Gyan.FFmpeg"), ("transcode", "transcoding", "ffprobe")),
    _pip("faster-whisper", "faster_whisper", "Speech-to-text (GPU on the GPU box, CPU fallback)", "whisper", "transcription", "stt"),
    # data / trading
    _pip("pandas", "pandas", "Tabular data"),
    _pip("alpaca-py", "alpaca", "Alpaca broker/market-data SDK (PAPER only for now)", "alpaca"),
    _pip("yfinance", "yfinance", "Market data", "market-data"),
    _pip("httpx", "httpx", "HTTP client"),
    Capability("alpaca-credentials", "Alpaca paper-trading API key + secret", "credential", "ALPACA_KEY",
               aliases=("alpaca-key",)),
    # security research (owner-installed: unclassified network tools default to max risk)
    Capability("nuclei", "Template-based vulnerability scanner", "binary", "nuclei",
               InstallPlan("host_apt", "go install -v github.com/projectdiscovery/nuclei/v3/cmd/nuclei@latest",
                           "Owner installs; every scan is HIGH risk and gated"), legal="Only against in-scope assets."),
    Capability("subfinder", "Passive subdomain enumeration", "binary", "subfinder",
               InstallPlan("host_apt", "go install -v github.com/projectdiscovery/subfinder/v2/cmd/subfinder@latest")),
    Capability("nmap", "Port scanner", "binary", "nmap", InstallPlan("host_apt", "sudo apt install nmap"),
               legal="Port scanning without authorization can be illegal."),
    # services / hardware
    Capability("gpu-box", "GPU inference on a dedicated machine (Ollama, e.g. over Tailscale)", "service",
               "tcp:" + os.environ.get("AURIX_GPU_HOST", "100.64.0.10:11434"), aliases=("gpu",)),
    Capability("claude-fallback", "Claude last-resort drafter (needs ANTHROPIC_API_KEY, off by default)",
               "credential", "ANTHROPIC_API_KEY", aliases=("claude",)),
    # coding agent: OpenHands' Python SDK, run INSIDE the sandbox (optional layer of its image)
    Capability("openhands", "OpenHands coding-agent SDK, runs inside the isolated sandbox", "python_pkg",
               "openhands.sdk",
               InstallPlan("dockerfile", "docker compose build sandbox  (optional layer: "
                                         "mission_sandbox/requirements-openhands.txt)"),
               ("openhands-sdk", "coding-agent")),
    # owner authorizations: only the owner can grant these (data/authorizations.json)
    Capability("patient-data-consent", "Owner confirms the scan is theirs / consented, and stays local",
               "authorization", "patient-data-consent"),
    Capability("bugbounty-scope", "A written program scope: in-scope assets, rules, rate limits, safe harbor",
               "authorization", "bugbounty-scope",
               legal="Testing anything outside written scope can be a crime (e.g. CFAA)."),
    Capability("live-trading", "Owner's explicit go-ahead for REAL-MONEY trading", "authorization", "live-trading",
               legal="Out of scope until the owner authorises it (activation handoff section 11)."),
    Capability("social-accounts", "Owner-provided social accounts and posting credentials", "authorization",
               "social-accounts"),
    Capability("real-estate-license", "A Colorado real-estate license / licensed-broker arrangement", "authorization",
               "real-estate-license",
               legal="Compensation for finding/steering buyers or sellers generally requires a license in Colorado."),
    Capability("content-rights", "Owner confirms they own or may process the media", "authorization", "content-rights"),
]}


def _norm(name: str) -> str:
    return re.sub(r"[\s_]+", "-", (name or "").strip().lower())


def lookup(name: str) -> Optional[Capability]:
    """Find a capability by id or alias; case-insensitive, '_' == '-' == ' '."""
    key = _norm(name)
    if not key:
        return None
    for cap in REGISTRY.values():
        if key == _norm(cap.id) or key in (_norm(a) for a in cap.aliases):
            return cap
    return None


# ---------------------------------------------------------------------------
# presence
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Presence:
    present: bool
    detail: str = ""


def authorizations_path() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "authorizations.json"


def load_authorizations() -> Dict[str, dict]:
    try:
        raw = json.loads(authorizations_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _authorized(key: str, granted: Dict[str, dict], now: float) -> Presence:
    entry = granted.get(key)
    if not isinstance(entry, dict):
        return Presence(False, "not granted by the owner")
    exp = entry.get("expires")
    if exp:
        try:
            if time.mktime(time.strptime(str(exp)[:10], "%Y-%m-%d")) < now:
                return Presence(False, "authorization expired")
        except ValueError:
            return Presence(False, "unreadable expiry")
    return Presence(True, "granted by the owner")


def presence(cap: Capability, *, env: Optional[Dict[str, str]] = None,
             which: Callable[[str], Optional[str]] = shutil.which,
             find_spec: Callable[[str], object] = importlib.util.find_spec,
             probe: Optional[Callable[[str, int], bool]] = None,
             authorizations: Optional[Dict[str, dict]] = None,
             tools: Optional[Iterable[str]] = None, now: Optional[float] = None) -> Presence:
    env = os.environ if env is None else env
    if cap.kind == "builtin":
        return Presence(True, "built-in tool") if tools is None or cap.check in set(tools) \
            else Presence(False, "tool not enabled")
    if cap.kind == "python_pkg":
        try:
            found = find_spec(cap.check) is not None
        except (ImportError, ValueError):
            found = False
        return Presence(found, "installed" if found else "not installed")
    if cap.kind == "binary":
        path = which(cap.check)
        return Presence(bool(path), "on PATH" if path else "not on PATH in this container")
    if cap.kind == "credential":
        return Presence(bool(env.get(cap.check)), "set" if env.get(cap.check) else f"{cap.check} is not set")
    if cap.kind == "authorization":
        granted = load_authorizations() if authorizations is None else authorizations
        return _authorized(cap.check, granted, now or time.time())
    if cap.kind == "service":
        if cap.check.startswith("tcp:") and probe is not None:
            _, host, port = cap.check.split(":")
            ok = probe(host, int(port))
            return Presence(ok, "reachable" if ok else "unreachable")
        return Presence(False, "not probed")
    return Presence(False, f"unknown kind {cap.kind}")


def tcp_probe(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# domain packs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PackStep:
    title: str
    description: str
    capabilities: Tuple[str, ...] = ()
    success_check: str = ""
    executor: str = "agent"         # "agent" = the chat agent loop; "openhands" = OpenHands SDK in the sandbox
    sandbox_note: str = ""          # appended to the description of SANDBOXED missions: which baked-in tool does this


@dataclass(frozen=True)
class DomainPack:
    id: str
    title: str
    triggers: Tuple[str, ...]
    capabilities: Tuple[str, ...]
    legal: Tuple[str, ...]
    steps: Tuple[PackStep, ...]
    success_criteria: Tuple[str, ...]
    prohibited: Tuple[str, ...] = ()
    prohibited_patterns: Tuple[str, ...] = ()
    max_wall_minutes: int = 180
    standing_ok: bool = False       # may run unattended on a schedule (low-risk, no outward effects)
    skills: Tuple[str, ...] = ()    # advisory prompt rule sets appended to this pack's steps (skills.py)

    def matches(self, goal: str) -> bool:
        return any(re.search(t, goal or "", re.IGNORECASE) for t in self.triggers)


PACKS: List[DomainPack] = [
    DomainPack(
        "ct_to_print", "CT scan -> clean 3D-print file + cheapest producer",
        (r"\bct\b.{0,12}scan", r"\bdicom\b", r"\b3d[- ]?print", r"\bstl\b", r"\bskull\b", r"\bmarching cubes\b"),
        ("pydicom", "SimpleITK", "numpy", "scikit-image", "trimesh", "pymeshlab", "numpy-stl", "web_search",
         "patient-data-consent"),
        ("Anatomical models from CT are for education/visualisation, NOT clinical or surgical use.",
         "CT data is personal health data: process it locally and never upload it without consent."),
        (PackStep("Locate and validate the DICOM series",
                  "Find the series, count slices, check modality/spacing/orientation, strip identifying metadata.",
                  ("pydicom", "bash"), "slice count and voxel spacing reported",
                  sandbox_note="TOOL: `python3 /opt/tools/ct_to_stl.py --input <scan dir or file> --output <workspace>/model.stl "
                               "--report <workspace>/report.json` performs steps 2-6 in one run (load in HU, bone threshold, "
                               "largest component, marching cubes, mesh repair, watertight STL). Options: --threshold 300 "
                               "--smooth 0.8 --target-faces 300000 --iso-mm 0.8 --preview <png> --series-id <id>. "
                               "`--selftest` proves the toolchain. Run it, then verify report.json (warnings, watertight, "
                               "volume_cm3, extents_mm) instead of re-implementing the pipeline."),
         PackStep("Load the volume in Hounsfield units", "Resample to isotropic spacing.", ("SimpleITK", "numpy")),
         PackStep("Segment bone", "Threshold (~300+ HU), keep the largest connected component, remove the "
                  "scanner table and noise.", ("SimpleITK", "scipy"), "single connected bone mask"),
         PackStep("Extract the surface", "Marching cubes at native spacing.", ("scikit-image", "numpy")),
         PackStep("Clean the mesh", "Fill holes, remove non-manifold edges, smooth, decimate to a printable size.",
                  ("trimesh", "pymeshlab"), "mesh is watertight"),
         PackStep("Validate printability and export STL", "Check watertight, wall thickness, scale, orientation.",
                  ("trimesh", "numpy-stl"), "STL exports and reloads with volume reported"),
         PackStep("Estimate print cost and gather quotes",
                  "From volume and material choice, price FDM/SLS/resin options from online services and local "
                  "makerspaces; rank cheapest including shipping.", ("web_search", "browser"),
                  "quote table with at least 3 sources",
                  sandbox_note="TOOL: `python3 /opt/tools/print_cost.py --report <workspace>/report.json --out <workspace>/cost.md` "
                               "gives ballpark FDM/SLA/SLS/MJF estimates (they shortlist options, they are not quotes). "
                               "Save real vendor quotes you find as <workspace>/quotes.json (a list of {vendor, tech, material, "
                               "price, shipping, lead_days, source}) and add `--quotes <workspace>/quotes.json` to rank them by "
                               "landed cost; rows without a source are rejected. The sandbox has no internet: collect quotes "
                               "with web_search, then run the tool."),
         PackStep("Report", "Files, quote table, assumptions, caveats.", ("write_file",))),
        ("watertight STL exported", "quote table with at least 3 sources", "dimensions and volume reported"),
        ("Do not upload the scan or mesh to any third party without the owner's approval.",),
        (r"(curl|wget).*(\s-T\s|--upload-file|\s-F\s|--data)",), 240),
    DomainPack(
        "transcription", "Transcoding and transcription",
        (r"transcri", r"\bwhisper\b", r"subtitle", r"captions?\b", r"transcod", r"\bffmpeg\b", r"speech[- ]to[- ]text"),
        ("ffmpeg", "faster-whisper", "gpu-box", "content-rights", "write_file"),
        ("Only process media you own or have written permission to transcribe.",
         "Many freelance platforms prohibit AI-only transcripts: check each platform's terms before selling."),
        (PackStep("Intake and validate media", "ffprobe each file: duration, codecs, audio quality.", ("ffmpeg",)),
         PackStep("Normalise audio", "ffmpeg to 16 kHz mono WAV.", ("ffmpeg",)),
         PackStep("Transcribe", "faster-whisper; use the GPU box when reachable, else a small CPU model.",
                  ("faster-whisper", "gpu-box"),
                  sandbox_note="TOOL: `python3 /opt/tools/transcribe.py --input <media file> --outdir <workspace>/out "
                               "[--model base] [--language en] [--formats srt,vtt,txt,json] [--glossary terms.txt]` does "
                               "the normalise, transcribe, glossary and subtitle steps in one run (offline, CPU, baked-in "
                               "base model) and writes a QA report listing low-confidence spans for a human spot-check. "
                               "A glossary file has one `wrong => right` per line."),
         PackStep("Post-process", "Punctuation, timestamps, speaker labels, custom glossary.", ("write_file",)),
         PackStep("Quality check", "Spot-check samples against the audio; flag low-confidence spans.", ()),
         PackStep("Deliver", "SRT/VTT/TXT with a time and cost log.", ("write_file",))),
        ("deliverable files produced", "spot-check passed", "processing time logged"),
        standing_ok=True),
    DomainPack(
        "trading_paper", "Daily stock trading (PAPER only)",
        (r"\btrad(e|ing|er)\b", r"\bstocks?\b", r"\balpaca\b", r"\bportfolio\b", r"\bmarkets?\b"),
        ("alpaca-py", "pandas", "yfinance", "alpaca-credentials", "web_search", "live-trading"),
        ("PAPER trading only. Real money is out of scope until the owner authorises it (handoff section 11).",
         "Not financial advice; judge results against a buy-and-hold benchmark over months, not days."),
        (PackStep("Refresh data", "Prices, fundamentals, news for the watchlist.", ("yfinance", "web_search")),
         PackStep("Screen and rank", "Apply stated signals; record the rationale for every candidate.", ("pandas",)),
         PackStep("Size within limits", "Max % per position, daily loss cap, kill switch.", ()),
         PackStep("Place PAPER orders", "Through the existing trade agent against the paper endpoint.",
                  ("alpaca-py", "alpaca-credentials")),
         PackStep("End-of-day report", "P&L vs SPY, rationale log, anomalies.", ("write_file",))),
        ("orders placed on the PAPER endpoint only", "P&L vs benchmark reported"),
        ("Never use the live Alpaca endpoint or set ALPACA_LIVE=true.",),
        (r"(?<!paper-)api\.alpaca\.markets", r"ALPACA_LIVE\s*=\s*true")),
    DomainPack(
        "bug_bounty", "Bug bounties",
        (r"bug[- ]?bount", r"hackerone", r"bugcrowd", r"intigriti", r"vulnerabilit", r"pen[- ]?test",
         r"responsible disclosure"),
        ("httpx", "web_search", "browser", "nuclei", "subfinder", "bugbounty-scope"),
        ("Test ONLY assets explicitly in scope of an active program with safe harbor; anything else can be illegal.",
         "Respect rate limits and rules of engagement; never access or exfiltrate real user data; stop at proof of concept.",
         "The owner reviews every report before it is submitted."),
        (PackStep("Choose a program and load its scope", "The owner supplies the written scope; AURIX never guesses.",
                  ("bugbounty-scope",), "scope document stored"),
         PackStep("Enumerate in-scope assets passively", "Public sources first; no active scanning yet.",
                  ("web_search", "subfinder")),
         PackStep("Targeted checks per in-scope vulnerability class", "Low-rate, non-destructive.", ("httpx", "nuclei")),
         PackStep("Verify and de-duplicate", "Reproduce; check disclosed reports for duplicates.", ("web_search",)),
         PackStep("Write the report", "Impact, reproduction steps, suggested fix; owner reviews before submission.",
                  ("write_file",))),
        ("every finding reproduced and in scope", "report drafted for owner review"),
        ("No testing outside listed scope.", "No denial-of-service, data exfiltration, or social engineering."),
        (r"\bsqlmap\b.*--(os-shell|dump)", r"\b(hydra|slowloris|hping3)\b")),
    DomainPack(
        "social_content", "Generate content for profit on socials",
        (r"\bsocials?\b", r"tiktok", r"instagram", r"youtube", r"\btwitter\b", r"content creat", r"monetiz",
         r"\breels?\b", r"\bshorts\b", r"newsletter"),
        ("web_search", "browser", "ffmpeg", "social-accounts"),
        ("Publishing is outward-facing: nothing posts without the owner approving it (or an approved schedule).",
         "Disclose AI-generated content where platforms require; avoid copyrighted material; monetisation "
         "depends on platform thresholds."),
        (PackStep("Niche and trend research", "Competitors, gaps, what performs.", ("web_search",)),
         PackStep("Content calendar and scripts", "A week at a time.", ("write_file",)),
         PackStep("Asset generation", "Images, video, voice.", ("ffmpeg",)),
         PackStep("Owner review queue", "Every item waits for approval.", ()),
         PackStep("Publish approved items", "Via approved accounts only.", ("social-accounts",)),
         PackStep("Analytics and iterate", "What worked; adjust the calendar.", ("web_search",))),
        ("drafts queued for owner approval", "analytics summary produced"),
        ("Nothing is published without owner approval.",)),
    DomainPack(
        "real_estate_leads", "Find homes for sale (lead research)",
        (r"homes? for sale", r"real[- ]estate", r"\blistings?\b", r"wholesal", r"\bflip", r"finder'?s fee",
         r"\bcommission\b", r"property lead"),
        ("web_search", "browser", "httpx", "pandas", "real-estate-license"),
        ("Colorado generally requires a real-estate license to be paid for finding or steering buyers/sellers: "
         "get advice from a Colorado attorney or licensed broker BEFORE taking any fee.",
         "Use public records and licensed data sources; obey listing-site terms; Fair Housing rules apply to outreach."),
        (PackStep("Define criteria", "Area, price band, condition, target discount.", ()),
         PackStep("Pull public data", "County assessor/recorder, foreclosure notices, permitted sources.",
                  ("web_search", "httpx")),
         PackStep("Score leads", "Comps, estimated value, discount, risk flags.", ("pandas",)),
         PackStep("Compile the lead sheet", "For the owner (and any licensed broker) to review.", ("write_file",))),
        ("scored lead sheet produced for owner review",),
        ("Do not contact sellers or buyers.", "Do not sign or bind anything.", "Do not collect any fee.")),
    DomainPack(
        "reynolds_research", "Reynolds Gang treasure hunt (research)",
        (r"reynolds", r"treasure", r"outlaw", r"ghost town", r"prospect", r"colorado gold"),
        ("web_search", "browser", "write_file"),
        ("Fieldwork on public land needs the right permits; metal detecting and digging are restricted on most "
         "federal land; private land needs the owner's permission.",),
        (PackStep("Gather sources", "Archives, newspapers, maps, court and land records.", ("web_search", "browser")),
         PackStep("Cross-reference", "Dates, places, names; note contradictions rather than resolving them.", ()),
         PackStep("Update the knowledge base", "Wiki notes with source and confidence.", ("write_file",)),
         PackStep("Rank hypotheses", "Evidence grade for each; propose next actions.", ())),
        ("wiki updated with sources", "hypotheses ranked with evidence grades"),
        standing_ok=True),
    DomainPack(
        "software_dev", "Software development (Claude Code-style)",
        (r"\b(?:write|build|implement|refactor|debug|fix|develop|code|program|create)\b.{0,50}"
         r"\b(?:code|script|function|app|application|module|program|bug|feature|tests?|cli|api|website|tool|"
         r"library|bot)\b", r"\bopenhands\b", r"\bpull request\b"),
        ("openhands", "bash", "write_file", "read_file", "web_search"),
        ("Code is written and run only inside the isolated sandbox. Nothing is pushed, published or "
         "deployed without the owner's approval.",),
        (PackStep("Understand the task and the code it touches",
                  "Read the relevant files, trace the real flow, and write down the plan and the acceptance "
                  "checks BEFORE changing anything.", ("read_file", "bash"), "plan and acceptance checks written"),
         PackStep("Implement with the coding agent",
                  "Delegate the change to OpenHands in the mission workspace, with the acceptance checks and the "
                  "coding rules.", ("openhands",), "the change exists in the workspace", executor="openhands"),
         PackStep("Verify", "Run the acceptance checks/tests in the sandbox and fix failures. Never weaken a test "
                  "to make it pass.", ("bash",), "acceptance checks pass"),
         PackStep("Review and summarise", "Show the diff, explain what changed and why, list risks and follow-ups. "
                  "Nothing is pushed or deployed.", ("write_file",))),
        ("acceptance checks pass in the sandbox", "diff and summary produced"),
        ("Never push, publish, deploy or open pull requests without the owner's approval.",
         "Never edit the AURIX approval gate, audit, contracts, credentials or identity."),
        (r"\bgit\s+push\b", r"\b(?:npm|twine|cargo)\s+publish\b", r"\bdocker\s+push\b"),
        240, skills=("ponytail",)),
    DomainPack(
        "self_improve", "AURIX self-improvement",
        (r"self[- ]?improv", r"improve (yourself|aurix)", r"fix (yourself|itself)", r"upgrade (aurix|yourself)"),
        ("bash", "write_file", "read_file", "claude-fallback"),
        ("AURIX can rewrite AURIX, but never the mechanism that decides what it may rewrite: the approval gate, "
         "audit, contracts, credentials and identity are protected components.",),
        (PackStep("Find the defect", "From logs and failing tests, not guesses.", ("read_file", "bash")),
         PackStep("Draft a minimal patch", "Local models first; Claude last resort if enabled.", ("write_file",)),
         PackStep("Test it", "Run the relevant tests in the workspace copy.", ("bash",)),
         PackStep("Propose for owner review", "Diff and test evidence; applied only after approval.", ())),
        ("tests pass", "diff reviewed by the owner"),
        ("Never modify protected components.",), skills=("ponytail",)),
]


def match_packs(goal: str) -> List[DomainPack]:
    return [p for p in PACKS if p.matches(goal)]


# ---------------------------------------------------------------------------
# requirements
# ---------------------------------------------------------------------------

@dataclass
class Requirements:
    packs: List[str] = field(default_factory=list)
    capabilities: List[str] = field(default_factory=list)
    present: List[str] = field(default_factory=list)
    installable: List[str] = field(default_factory=list)       # AURIX can pip-install these itself
    owner_install: List[str] = field(default_factory=list)     # "id: command" - the owner runs it
    manual: List[str] = field(default_factory=list)            # credentials / authorizations only the owner can give
    unknown: List[str] = field(default_factory=list)           # not in the registry: treated as maximum risk
    legal: List[str] = field(default_factory=list)
    install_commands: Dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("packs", "capabilities", "present", "installable",
                                              "owner_install", "manual", "unknown", "legal",
                                              "install_commands")}

    @property
    def ready(self) -> bool:
        return not (self.installable or self.owner_install or self.manual or self.unknown)


def resolve(cap_names: Iterable[str], *, sandbox: bool = False, **presence_kw) -> Requirements:
    """`sandbox=True`: code will run in the offline sandbox image, so a missing package or
    binary is fixed by adding it to that image (owner rebuilds), not by installing at run time."""
    req = Requirements()
    seen = set()
    for name in cap_names:
        cap = lookup(name)
        if cap is None:
            if name and name not in req.unknown:
                req.unknown.append(name)
            continue
        if cap.id in seen:
            continue
        seen.add(cap.id)
        req.capabilities.append(cap.id)
        p = presence(cap, **presence_kw)
        if p.present:
            req.present.append(cap.id)
        elif cap.kind == "authorization" or cap.kind == "credential" or cap.install is None:
            req.manual.append(f"{cap.id} ({cap.description})")
        elif cap.install.autonomous and sandbox:
            pkg = cap.install.command.split()[-1]
            req.owner_install.append(f"{cap.id}: add '{pkg}' to odysseus/mission_sandbox/requirements.txt, "
                                     "then docker compose build sandbox")
        elif cap.install.autonomous:
            req.installable.append(cap.id)
            req.install_commands[cap.id] = cap.install.command
        elif sandbox and cap.kind == "binary" and cap.install.where == "dockerfile":
            req.owner_install.append(f"{cap.id}: add '{cap.check}' to the apt-get line in "
                                     "odysseus/mission_sandbox/Dockerfile, then docker compose build sandbox")
        else:
            req.owner_install.append(f"{cap.id}: {cap.install.command}")
        if cap.legal and not p.present and cap.legal not in req.legal:
            req.legal.append(cap.legal)
    return req


def identify(goal: str, extra_capabilities: Iterable[str] = (), sandbox: bool = False,
             **presence_kw) -> Tuple[Requirements, List[DomainPack]]:
    """Everything a goal needs: domain-pack capabilities + legal gates, plus anything the
    planner added. Legal notes of every matched pack are always included."""
    packs = match_packs(goal)
    names: List[str] = []
    for p in packs:
        names.extend(p.capabilities)
        for s in p.steps:
            names.extend(s.capabilities)
    names.extend(extra_capabilities)
    req = resolve(names, sandbox=sandbox, **presence_kw)
    req.packs = [p.id for p in packs]
    for p in packs:
        for note in p.legal:
            if note not in req.legal:
                req.legal.append(note)
    return req, packs
