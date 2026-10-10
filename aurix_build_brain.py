"""
aurix_build_brain.py - Layered Aurix brain for the build pipeline.

ARCHITECTURE (User's Vision):
- BOTTOM: Fast, efficient decision layer (Kev + deterministic safety gates)
- TOP:    LLM for reasoning, planning, language (llama3.2:3b or larger)
- MIDDLE: This orchestrator (translates intent → checked build contract → execution)

The brain is NOT an LLM. It's fast, efficient, real-time decision-making.
The LLM sits ON TOP for deliberation, planning, and situational awareness.

FLOW:
1. User intent arrives ("build X", "fix Y", "add Z")
2. Kev fast assessment: risk score, safety yes/no (~3s via native CUDA server on 8009)
3. Deterministic safety gates: HARD BLOCKS on destructive ops, approval requirements
4. LLM plans the build (if safe to proceed)
5. Execute in controlled workspace with verification
6. Kev scores the result for quality/confidence

DEPLOYED 2026-10-08: Kev runs natively (espetro/llama.cpp fork, CUDA 12.8,
kev-4b q8_0) on port 8009. Previous Python/CPU service retired.
Rollback: ~/workspace/handoffs/2026-10-08-kev-cutover-rollback.md

SAFETY MODEL:
- Kev provides calibrated probabilities (not just yes/no)
- Deterministic gates are HARD RULES that even the LLM cannot override
- The LLM's `continue_farming` vs `avoid_threat` failure is fixed by gates, not by hoping the LLM is smarter
"""

import json
import time
import urllib.request
import urllib.error
from datetime import datetime
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# URLs support env-var overrides for cross-machine deployment.
# Defaults assume Steammachine (where Kev and Ollama live).
# If running on the 7070, set:
#   AURIX_KEV_URL=http://100.114.213.55:8009/v1/systemone
#   AURIX_OLLAMA_URL=http://100.114.213.55:11434/api/generate
import os as _os
KEV_URL = _os.environ.get("AURIX_KEV_URL", "http://localhost:8009/v1/systemone")
KEV_MODEL = _os.environ.get("AURIX_KEV_MODEL", "kev-latest")

# Kev context budget: max characters for the `state` field per request.
# kev-4b has a finite context window; questions + criteria also consume it.
# Budget priority: action/target (structured) survive; raw text is truncated first.
KEV_STATE_BUDGET_CHARS = 2000
KEV_RESERVED_CHARS = 500  # headroom for questions, criteria, and model overhead

OLLAMA_URL = _os.environ.get("AURIX_OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = _os.environ.get("AURIX_OLLAMA_MODEL", "llama3.2:3b")  # Verified working via API 2026-10-06

# For local testing, we can mock Kev or use SSH tunnel
# In production, this runs on Steammachine where Kev is local


# ---------------------------------------------------------------------------
# Safety Gate Definitions (DETERMINISTIC - LLM cannot override)
# ---------------------------------------------------------------------------

class RiskLevel(Enum):
    SAFE = "safe"           # Proceed automatically
    CAUTION = "caution"     # Proceed with logging
    APPROVAL = "approval"   # Require user approval
    BLOCKED = "blocked"     # Hard block, do not proceed


@dataclass
class SafetyGate:
    """A deterministic safety rule. These are HARD CONSTRAINTS."""
    name: str
    description: str
    check: callable  # (intent: BuildIntent) -> Optional[str] (reason if triggered)
    risk: RiskLevel


@dataclass
class BuildIntent:
    """A user's build request, parsed into a structured form."""
    raw_text: str
    action: str  # "build", "fix", "add", "delete", "modify", "deploy", etc.
    target: str  # What to build/fix
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


# Deterministic safety gates - these run BEFORE any LLM reasoning
SAFETY_GATES = [
    SafetyGate(
        name="no_mass_delete",
        description="Block mass deletion operations",
        check=lambda intent: "Mass deletion detected" if any(
            word in intent.raw_text.lower()
            for word in ["delete all", "rm -rf", "format", "wipe"]
        ) else None,
        risk=RiskLevel.BLOCKED,
    ),
    SafetyGate(
        name="no_production_deploy",
        description="Production deploys need approval",
        check=lambda intent: "Production deployment requires approval" if any(
            word in intent.raw_text.lower()
            for word in ["deploy to prod", "production deploy", "push to live"]
        ) else None,
        risk=RiskLevel.APPROVAL,
    ),
    SafetyGate(
        name="no_credential_exposure",
        description="Block operations that might expose credentials",
        check=lambda intent: (
            "Potential credential exposure"
            if (
                any(word in intent.raw_text.lower()
                    for word in ["password", "api key", "secret", "token", "cat .env", "echo $"])
                and "test" not in intent.raw_text.lower()
                and "rotate" not in intent.raw_text.lower()
            )
            else None
        ),
        risk=RiskLevel.BLOCKED,
    ),
    SafetyGate(
        name="no_7070_write",
        description="7070 is read-only via gate",
        check=lambda intent: "7070 writes are blocked (read-only gate)" if (
            "7070" in intent.raw_text and any(
                word in intent.raw_text.lower()
                for word in ["write", "deploy", "push", "update", "modify", "delete"]
            )
        ) else None,
        risk=RiskLevel.BLOCKED,
    ),
]


def check_safety_gates(intent: BuildIntent) -> List[tuple]:
    """
    Run all deterministic safety gates.
    Returns list of (gate_name, risk_level, reason) for triggered gates.
    """
    triggered = []
    for gate in SAFETY_GATES:
        reason = gate.check(intent)
        if reason:
            triggered.append((gate.name, gate.risk, reason))
    return triggered


# Deterministic SAFE categories — intents so low-risk that a medium-confidence
# Kev "review" should not force human approval. These are the inverse of gates:
# instead of "this is dangerous", they say "this is trivially safe".
# Both must hold: no safety gate triggered AND intent matches a safe category.
SAFE_CATEGORIES = [
    (
        "docs_typo_fix",
        "Typo/grammar fix in documentation",
        lambda intent: (
            intent.action in ("fix", "modify", "add")
            and any(w in intent.raw_text.lower() for w in ["typo", "grammar", "spelling", "wording"])
            and any(w in intent.raw_text.lower() for w in ["readme", "doc", "comment", "changelog", ".md"])
        ),
    ),
    (
        "docs_update",
        "Documentation content update (no code)",
        lambda intent: (
            intent.action in ("add", "modify", "fix")
            and any(w in intent.raw_text.lower() for w in ["readme", "documentation", "docstring", "comment"])
            and not any(w in intent.raw_text.lower() for w in [".py", ".js", ".ts", "code", "function", "class"])
        ),
    ),
    (
        "formatting_only",
        "Formatting/whitespace only (no logic change)",
        lambda intent: (
            intent.action in ("fix", "modify", "refactor")
            and any(w in intent.raw_text.lower() for w in ["format", "whitespace", "indent", "lint", "prettier", "black"])
        ),
    ),
]


def check_safe_categories(intent: BuildIntent) -> List[tuple]:
    """
    Check if intent falls in a deterministically safe category.
    Returns list of (category_name, description) for matches.
    """
    matched = []
    for name, desc, check in SAFE_CATEGORIES:
        try:
            if check(intent):
                matched.append((name, desc))
        except Exception:
            pass
    return matched


def get_max_risk(triggered_gates: List[tuple]) -> RiskLevel:
    """Get the highest risk level from triggered gates."""
    if not triggered_gates:
        return RiskLevel.SAFE
    # Order: BLOCKED > APPROVAL > CAUTION > SAFE
    risk_order = [RiskLevel.BLOCKED, RiskLevel.APPROVAL, RiskLevel.CAUTION, RiskLevel.SAFE]
    for risk in risk_order:
        if any(g[1] == risk for g in triggered_gates):
            return risk
    return RiskLevel.SAFE


# ---------------------------------------------------------------------------
# Kev Decision Layer (Fast, Calibrated)
# ---------------------------------------------------------------------------

class KevClient:
    """
    Client for Kev decision models.
    Kev provides fast, calibrated yes/no, choice, and score judgments.
    """

    def __init__(self, url: str = KEV_URL, model: str = KEV_MODEL, mock: bool = False):
        self.url = url
        self.model = model
        self.mock = mock

    def _request(self, state: str, questions: dict) -> dict:
        """Submit a SystemOneRequest to Kev."""
        if self.mock:
            return self._mock_response(questions)

        # Budget the state BEFORE sending — never blow Kev's context window
        # on unbounded user input. Returns (budgeted_state, was_truncated).
        state, was_truncated = self.budget_state(state)
        if was_truncated:
            # Telemetry hook: the caller (BuildBrain) records this via record_error
            # with layer="kev", operation="budget_truncate". We stash the flag here.
            self._last_truncated = True
        else:
            self._last_truncated = False

        payload = {
            "state": state,
            "model": self.model,
            "questions": questions
        }
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            self.url,
            data=data,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return {
                    "success": True,
                    "body": json.loads(resp.read().decode('utf-8')),
                    "http_status": resp.status,
                }
        except urllib.error.HTTPError as e:
            return {
                "success": False,
                "http_status": e.code,
                "error": e.read().decode('utf-8')[:500],
            }
        except Exception as e:
            return {"success": False, "error": str(e)[:500]}

    def _mock_response(self, questions: dict) -> dict:
        """Mock Kev responses for testing without the service."""
        answers = {}
        for qid, q in questions.items():
            if q["type"] == "noul":
                answers[qid] = {"type": "noul", "noul": 0.8}
            elif q["type"] == "choice":
                opts = list(q["criteria"].keys())
                answers[qid] = {
                    "type": "choice",
                    "choice": opts[0],
                    "confidence": 0.7,
                    "probabilities": {k: 0.7 if k == opts[0] else 0.3/(len(opts)-1) for k in opts}
                }
            elif q["type"] == "score":
                answers[qid] = {
                    "type": "score",
                    "score": 2.0,
                    "confidence": 0.6,
                    "probabilities": {"0": 0.1, "1": 0.2, "2": 0.4, "3": 0.2, "4": 0.1},
                    "legend": {str(i): c for i, c in enumerate(q["criteria"])}
                }
        return {"success": True, "body": {"answers": answers, "model": "mock"}, "http_status": 200}

    @staticmethod
    def build_state(intent) -> str:
        """
        Build a budgeted state string from a BuildIntent.
        Priority order: action + target (structured, always kept) first,
        raw text truncated to fit the remaining budget.
        """
        header = f"Action: {intent.action}\nTarget: {intent.target}"
        prefix = "\nRequest: "
        marker = "\n[...truncated to fit Kev context budget...]"
        # Reserve room for header + prefix + marker within the budget
        available = KEV_STATE_BUDGET_CHARS - len(header) - len(prefix) - len(marker)
        raw = intent.raw_text or ""
        if len(raw) > available:
            raw = raw[:available]
            return f"{header}{prefix}{raw}{marker}"
        return f"{header}{prefix}{raw}"

    @staticmethod
    def budget_state(state: str) -> tuple:
        """
        Enforce the context budget on a raw state string.
        Returns (budgeted_state, was_truncated).
        """
        if len(state) <= KEV_STATE_BUDGET_CHARS:
            return state, False
        marker = "[...truncated to fit Kev context budget...]"
        cut = KEV_STATE_BUDGET_CHARS - len(marker) - 1
        return state[:cut] + " " + marker, True

    def was_last_truncated(self) -> bool:
        """Did the most recent _request truncate the state?"""
        return getattr(self, "_last_truncated", False)

    def assess_safety(self, intent: BuildIntent) -> dict:
        """
        Fast safety assessment: should we proceed?
        Returns calibrated probability and recommendation.
        """
        state = self.build_state(intent)
        result = self._request(state, {
            "safe_to_proceed": {
                "type": "noul",
                "instructions": "Is it safe to proceed with this build request without human review?",
                "criteria": {
                    "false": "No, this needs human review or should be blocked",
                    "true": "Yes, safe to proceed automatically"
                }
            }
        })
        if result["success"]:
            prob = result["body"]["answers"]["safe_to_proceed"]["noul"]
            return {
                "safe_probability": prob,
                "recommendation": "proceed" if prob > 0.7 else "review" if prob > 0.3 else "block",
                "latency_ms": result["body"].get("latency_ms", 0),
            }
        return {"error": result.get("error", "Unknown error")}

    def assess_risk(self, intent: BuildIntent) -> dict:
        """
        Score the risk level of a build request (0=safe, 4=critical).
        """
        state = self.build_state(intent)
        result = self._request(state, {
            "risk_level": {
                "type": "score",
                "instructions": "Rate the risk of this build request (0=safe, 4=critical)",
                "criteria": [
                    "No risk - routine, reversible operation",
                    "Low risk - minor side effects possible",
                    "Medium risk - could affect other systems",
                    "High risk - destructive or hard to reverse",
                    "Critical risk - must not proceed without explicit approval"
                ]
            }
        })
        if result["success"]:
            answer = result["body"]["answers"]["risk_level"]
            return {
                "risk_score": answer["score"],
                "confidence": answer["confidence"],
                "probabilities": answer["probabilities"],
                "latency_ms": result["body"].get("latency_ms", 0),
            }
        return {"error": result.get("error", "Unknown error")}

    def choose_strategy(self, intent: BuildIntent, options: Dict[str, str]) -> dict:
        """
        Choose between build strategies.
        options: {name: description}
        """
        state = self.build_state(intent)
        result = self._request(state, {
            "strategy": {
                "type": "choice",
                "instructions": "Which approach is best for this build?",
                "criteria": options
            }
        })
        if result["success"]:
            answer = result["body"]["answers"]["strategy"]
            return {
                "choice": answer["choice"],
                "confidence": answer["confidence"],
                "probabilities": answer["probabilities"],
                "latency_ms": result["body"].get("latency_ms", 0),
            }
        return {"error": result.get("error", "Unknown error")}


class OllamaClient:
    """
    Client for the LLM deliberation layer (llama3.2:3b via Ollama API).
    Handles intent parsing and build planning. Falls back gracefully
    if the model is unavailable — the deterministic layers above it
    (safety gates + Kev) still function.
    """

    def __init__(self, url: str = OLLAMA_URL, model: str = OLLAMA_MODEL,
                 mock: bool = False, timeout: int = 180):
        self.url = url
        self.model = model
        self.mock = mock
        self.timeout = timeout

    def generate(self, prompt: str, system: str = None) -> Optional[str]:
        """Send a prompt to the LLM. Returns response text or None on failure."""
        if self.mock:
            return None  # Caller falls back to template logic

        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.3, "num_predict": 500},
        }
        if system:
            payload["system"] = system

        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            self.url, data=data,
            headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode('utf-8'))
                return body.get("response", "").strip() or None
        except Exception:
            return None

    def warmup(self) -> bool:
        """
        Keep the model loaded with a minimal prompt.
        Call this on startup and periodically to avoid cold-start latency.
        Returns True if the model responded.
        """
        if self.mock:
            return False
        # Tiny prompt, minimal tokens — just enough to trigger model load
        result = self.generate("Reply with: ok", system="You are a terse assistant.")
        return result is not None

    def is_alive(self) -> bool:
        """Quick liveness check without loading the model."""
        if self.mock:
            return False
        try:
            # /api/tags is lightweight, doesn't trigger model load
            url = self.url.replace("/api/generate", "/api/tags")
            req = urllib.request.Request(url, method='GET')
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except Exception:
            return False

    def parse_intent(self, raw_text: str) -> Optional[Dict[str, str]]:
        """
        Use the LLM to parse natural language into action + target.
        Returns {"action": ..., "target": ...} or None if LLM unavailable.
        """
        prompt = (
            f'Parse this build request into JSON with keys "action" and "target".\n'
            f'Action must be one of: build, fix, add, delete, modify, deploy, test, refactor.\n'
            f'Target is a short phrase describing what to work on.\n'
            f'Request: "{raw_text}"\n'
            f'Respond with ONLY the JSON object, no other text.'
        )
        resp = self.generate(prompt)
        if not resp:
            return None
        try:
            # Extract JSON from response (handle markdown fences)
            start = resp.find('{')
            end = resp.rfind('}') + 1
            if start >= 0 and end > start:
                parsed = json.loads(resp[start:end])
                if "action" in parsed and "target" in parsed:
                    return {"action": str(parsed["action"]).lower(),
                            "target": str(parsed["target"])}
        except (json.JSONDecodeError, KeyError):
            pass
        return None

    def plan_build(self, intent: BuildIntent, safety: dict,
                   risk: dict) -> Optional[List[str]]:
        """
        Use the LLM to generate a concrete execution plan.
        Returns list of steps or None if LLM unavailable.
        """
        prompt = (
            f'Generate a concrete build plan as a JSON array of step strings.\n'
            f'Action: {intent.action}\n'
            f'Target: {intent.target}\n'
            f'Safety: {safety.get("safe_probability", "unknown")}\n'
            f'Risk score: {risk.get("risk_score", "unknown")}\n'
            f'Keep steps specific and actionable (5-8 steps).\n'
            f'Respond with ONLY the JSON array, no other text.'
        )
        resp = self.generate(prompt)
        if not resp:
            return None
        try:
            start = resp.find('[')
            end = resp.rfind(']') + 1
            if start >= 0 and end > start:
                steps = json.loads(resp[start:end])
                if isinstance(steps, list) and all(isinstance(s, str) for s in steps):
                    return steps
        except json.JSONDecodeError:
            pass
        return None


# ---------------------------------------------------------------------------
# Build Brain Orchestrator
# ---------------------------------------------------------------------------

class BuildBrain:
    """
    The layered brain for Aurix's build pipeline.

    LAYERS:
    1. Deterministic Safety Gates (instant, hard rules)
    2. Kev Fast Decisions (~3s via native CUDA, calibrated probabilities)
    3. LLM Deliberation (llama3.2:3b via Ollama, for parsing + planning)
    """

    def __init__(self, kev_mock: bool = False, llm_mock: bool = False,
                 state_dir: str = None):
        self.kev = KevClient(mock=kev_mock)
        self.llm = OllamaClient(mock=llm_mock)
        self.inspector = ProjectInspector()
        self.recovery = RecoveryState(state_dir=state_dir)
        # Approval manager for the 7th pipeline stage (request approval).
        # Import here to avoid circular imports; aurix_approval is standalone.
        try:
            from aurix_approval import ApprovalManager
            self.approval_manager = ApprovalManager(recovery=self.recovery)
        except ImportError:
            self.approval_manager = None
        self.decision_log = []
        # Structured error telemetry: list of {timestamp, layer, operation, error, context}
        # Replaces swallowed exceptions with queryable records.
        self.error_telemetry = []

    def record_error(self, layer: str, operation: str, error: str, context: dict = None):
        """Record a structured error event. Never silently swallow failures."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "layer": layer,
            "operation": operation,
            "error": str(error)[:500],
            "context": context or {},
        }
        self.error_telemetry.append(entry)
        try:
            self.recovery.append_error(entry)
        except OSError:
            pass  # persistence is best-effort
        print(f"[!] ERROR [{layer}/{operation}]: {entry['error']}")
        return entry

    def log(self, message: str, data: dict = None):
        """Log a decision with timestamp (memory + persistent)."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "message": message,
            "data": data or {},
        }
        self.decision_log.append(entry)
        try:
            self.recovery.append_decision(entry)
        except OSError:
            pass  # persistence is best-effort; memory log always works
        print(f"[{entry['timestamp']}] {message}")
        if data:
            print(f"  {json.dumps(data, indent=2)}")

    def process_intent(self, raw_text: str) -> dict:
        """
        Process a user build intent through the layered brain.

        Returns a build contract or a block/approval request.
        """
        print("=" * 70)
        print("BUILD BRAIN: Processing intent")
        print("=" * 70)

        # Parse intent (simple for prototype; LLM would do this in production)
        intent = self._parse_intent(raw_text)
        self.log(f"Parsed intent: {intent.action} -> {intent.target}")

        # LAYER 1: Deterministic Safety Gates (instant)
        print("\n[Layer 1] Deterministic Safety Gates...")
        triggered = check_safety_gates(intent)
        max_risk = get_max_risk(triggered)

        if triggered:
            for gate_name, risk, reason in triggered:
                self.log(f"  Gate '{gate_name}' triggered: {risk.value} - {reason}")

        if max_risk == RiskLevel.BLOCKED:
            self.log("BLOCKED by safety gate. Will not proceed.")
            return {
                "status": "blocked",
                "reason": "; ".join(r for _, _, r in triggered),
                "gates": triggered,
            }

        if max_risk == RiskLevel.APPROVAL:
            self.log("APPROVAL REQUIRED by safety gate.")
            reason = "; ".join(r for _, _, r in triggered)
            # Create the approval request so the caller has an ID and a card.
            # The 7070's Telegram/command-center integration delivers the card
            # via ApprovalRequest.to_card() and resolves via ApprovalManager.resolve().
            approval_id = None
            approval_card = None
            if self.approval_manager:
                approval = self.approval_manager.request(
                    action=intent.action,
                    target=intent.target,
                    reason=reason,
                    risk_details={"gates": triggered},
                    context={"intent_raw": intent.raw_text},
                )
                approval_id = approval.id
                approval_card = approval.to_card()
                self.log(f"Approval request created: {approval_id}")
            return {
                "status": "needs_approval",
                "reason": reason,
                "gates": triggered,
                "approval_id": approval_id,
                "approval_card": approval_card,
            }

        # LAYER 2: Kev Fast Assessment (safety + risk in parallel)
        print("\n[Layer 2] Kev Fast Assessment...")
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            fut_safety = ex.submit(self.kev.assess_safety, intent)
            fut_risk = ex.submit(self.kev.assess_risk, intent)
            safety = fut_safety.result()
            risk = fut_risk.result()

        self.log("Kev safety assessment", safety)
        self.log("Kev risk assessment", risk)

        # Context-budget telemetry: flag when input was truncated to fit.
        # Truncation isn't an error, but it's a signal — a truncated safety
        # assessment was made on partial input.
        if self.kev.was_last_truncated():
            self.record_error("kev", "budget_truncate",
                              f"State truncated to {KEV_STATE_BUDGET_CHARS} chars",
                              {"intent": intent.raw_text[:200]})

        # Structured telemetry for Kev failures — never silently skip.
        # A failed assessment is NOT an approval; it is recorded loudly and
        # the pipeline proceeds WITHOUT Kev's input, flagged in the contract.
        kev_available = True
        if "error" in safety:
            self.record_error("kev", "assess_safety", safety["error"],
                              {"intent": intent.raw_text})
            kev_available = False
        if "error" in risk:
            self.record_error("kev", "assess_risk", risk["error"],
                              {"intent": intent.raw_text})
            kev_available = False
        if not kev_available:
            self.log("WARNING: Kev unavailable — proceeding on gates alone, flagged in contract")

        # Deterministic safe categories: if no gate triggered and the intent
        # is trivially safe (docs typo, formatting), a medium-confidence Kev
        # "review" becomes "proceed with note" instead of forcing approval.
        # This fixes harmless intents (e.g. README typo fix) getting stuck
        # at needs_approval on Kev uncertainty alone.
        safe_categories = check_safe_categories(intent) if not triggered else []

        # Combine Kev's judgment with gate results
        kev_note = None
        if "safe_probability" in safety:
            if safety["safe_probability"] < 0.3:
                self.log("Kev recommends BLOCKING (low safety probability)")
                return {
                    "status": "blocked",
                    "reason": f"Kev safety assessment: {safety['safe_probability']:.2%} safe",
                    "kev_safety": safety,
                    "kev_risk": risk,
                    "safe_categories": [c[0] for c in safe_categories],
                }
            elif safety["safe_probability"] < 0.7:
                if safe_categories:
                    # Trivially safe + no gates + Kev merely uncertain = proceed
                    kev_note = (
                        f"Kev uncertain ({safety['safe_probability']:.2%}) but intent "
                        f"matches safe categories {[c[0] for c in safe_categories]} "
                        f"with no gates triggered — proceeding with note"
                    )
                    self.log(kev_note)
                else:
                    self.log("Kev recommends HUMAN REVIEW (medium safety probability)")
                    kev_reason = f"Kev safety assessment: {safety['safe_probability']:.2%} safe (needs review)"
                    approval_id = None
                    approval_card = None
                    if self.approval_manager:
                        approval = self.approval_manager.request(
                            action=intent.action,
                            target=intent.target,
                            reason=kev_reason,
                            risk_details={"kev_safety": safety, "kev_risk": risk},
                            context={"intent_raw": intent.raw_text},
                        )
                        approval_id = approval.id
                        approval_card = approval.to_card()
                        self.log(f"Approval request created: {approval_id}")
                    return {
                        "status": "needs_approval",
                        "reason": kev_reason,
                        "kev_safety": safety,
                        "kev_risk": risk,
                        "safe_categories": [c[0] for c in safe_categories],
                        "approval_id": approval_id,
                        "approval_card": approval_card,
                    }

        # LAYER 3: LLM Deliberation + Build Contract
        print("\n[Layer 3] LLM Deliberation & Build Contract...")
        contract = self._generate_contract(intent, safety, risk, triggered,
                                           kev_available=kev_available,
                                           kev_note=kev_note,
                                           safe_categories=safe_categories)
        # Persist the contract for recovery/resume
        try:
            cid = self.recovery.save_contract(contract)
            contract["id"] = cid
        except OSError:
            pass
        self.log(f"Build contract generated (planned by: {contract['planned_by']})", contract)

        return {
            "status": "approved",
            "contract": contract,
            "kev_safety": safety,
            "kev_risk": risk,
            "gates": triggered,
            "kev_available": kev_available,
            "safe_categories": [c[0] for c in safe_categories],
        }

    def check_resume(self) -> dict:
        """
        Check for incomplete work from a previous run.
        Returns {"active": contract|None, "incomplete": [contracts]}.
        Call at startup to offer resume.
        """
        active = None
        try:
            active = self.recovery.get_active()
        except OSError:
            pass
        incomplete = []
        try:
            incomplete = self.recovery.list_incomplete()
        except OSError:
            pass
        if active:
            self.log(f"Resume: found active contract {active.get('id')}",
                     {"intent": active.get("intent")})
        elif incomplete:
            self.log(f"Resume: {len(incomplete)} incomplete contract(s) found")
        return {"active": active, "incomplete": incomplete}

    def _parse_intent(self, raw_text: str) -> BuildIntent:
        """Parse raw text into a BuildIntent. Tries LLM first, falls back to keywords."""
        # Try LLM parsing
        parsed = self.llm.parse_intent(raw_text)
        if parsed:
            self.log(f"LLM parsed intent: {parsed['action']} -> {parsed['target']}")
            return BuildIntent(raw_text=raw_text, action=parsed["action"],
                               target=parsed["target"])

        # Fallback: simple keyword matching
        self.log("LLM unavailable, using keyword fallback for intent parsing")
        text_lower = raw_text.lower()
        action = "build"  # default
        for candidate in ["fix", "add", "delete", "remove", "update", "deploy", "test", "refactor"]:
            if candidate in text_lower:
                action = candidate
                break
        return BuildIntent(raw_text=raw_text, action=action, target=raw_text)

    def _generate_contract(self, intent: BuildIntent, safety: dict, risk: dict, gates: list,
                           kev_available: bool = True, kev_note: str = None,
                           safe_categories: list = None) -> dict:
        """Generate a checked build contract. Tries LLM planning, falls back to template."""
        # Try LLM-generated plan
        llm_steps = self.llm.plan_build(intent, safety, risk)
        if llm_steps:
            self.log(f"LLM generated {len(llm_steps)}-step plan")
            execution_plan = llm_steps
            planned_by = "llm"
        else:
            self.log("LLM unavailable, using template execution plan")
            execution_plan = [
                "1. Create isolated workspace",
                "2. Implement changes",
                "3. Run deterministic tests",
                "4. Kev quality scoring",
                "5. Present for approval (if needed)",
            ]
            planned_by = "template"

        return {
            "intent": intent.raw_text,
            "action": intent.action,
            "target": intent.target,
            "planned_by": planned_by,
            "safety_checks": {
                "gates_passed": len(gates) == 0,
                "gates_triggered": [g[0] for g in gates],
                "kev_safe_probability": safety.get("safe_probability"),
                "kev_risk_score": risk.get("risk_score"),
                "kev_available": kev_available,
                "kev_note": kev_note,
                "safe_categories": [c[0] for c in (safe_categories or [])],
            },
            "execution_plan": execution_plan,
            "rollback_plan": "Git stash + restore from backup",
            "created_at": datetime.now().isoformat(),
        }

    def execute_contract(self, contract: dict, dry_run: bool = True) -> dict:
        """
        Execute an approved build contract in the sandbox.

        Currently supports safe file creation. Destructive operations
        are blocked by the safety gates upstream and never reach here.

        Args:
            contract: The approved contract from process_intent()
            dry_run: If True, only report what WOULD be done. If False, execute.

        Returns:
            Execution result with status, actions taken, and rollback info.
        """
        executor = SafeExecutor(llm_client=self.llm if not getattr(self.llm, 'mock', True) else None)
        return executor.execute(contract, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Safe Executor (sandboxed, reversible)
# ---------------------------------------------------------------------------

class ProjectInspector:
    """
    Read-only inspector for existing project directories.

    Produces a structured summary the brain can plan against:
    file tree, language breakdown, test presence, config files,
    README excerpt, and git status.

    SAFETY:
    - Read-only. Never writes, never executes.
    - Root must be inside an allowed base (sandbox or explicit allowlist).
    - No symlink following outside the root.
    - Binary files, huge files, and .git internals are skipped (git status only).
    - Tree depth and file counts are capped.
    """

    # Directories never descended into
    SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
                 ".tox", "dist", "build", ".next", "target", ".idea", ".vscode"}
    # File extensions treated as binary (content never read)
    BINARY_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
                   ".zip", ".tar", ".gz", ".exe", ".dll", ".so", ".pyc",
                   ".mp3", ".mp4", ".wav", ".ogg", ".woff", ".woff2", ".ttf"}
    MAX_DEPTH = 4
    MAX_FILES = 500
    MAX_READ_BYTES = 20000  # per file, for README/config excerpts
    # Config files worth surfacing by name
    CONFIG_FILES = {"package.json", "requirements.txt", "pyproject.toml",
                    "setup.py", "Cargo.toml", "go.mod", "pom.xml",
                    "dockerfile", "docker-compose.yml", "Makefile",
                    ".env.example", "tsconfig.json"}

    def __init__(self, allowed_bases: list = None):
        import os
        if allowed_bases is None:
            allowed_bases = [os.path.expanduser("~/aurix-sandbox"),
                             os.path.expanduser("~/workspace")]
        self.allowed_bases = [os.path.abspath(b) for b in allowed_bases]

    def _check_root(self, root: str) -> str:
        """Validate root is inside an allowed base. Returns abspath or raises."""
        import os
        abs_root = os.path.abspath(os.path.realpath(root))
        for base in self.allowed_bases:
            if abs_root == base or abs_root.startswith(base + os.sep):
                return abs_root
        raise ValueError(f"Project root outside allowed bases: {root}")

    def inspect(self, root: str) -> dict:
        """Inspect a project directory. Returns structured summary."""
        import os
        abs_root = self._check_root(root)
        if not os.path.isdir(abs_root):
            return {"error": f"not a directory: {root}", "root": root}

        summary = {
            "root": abs_root,
            "tree": [],
            "languages": {},
            "test_files": [],
            "config_files": [],
            "readme_excerpt": None,
            "git": {},
            "totals": {"files": 0, "dirs": 0, "truncated": False},
        }

        file_count = 0
        for dirpath, dirnames, filenames in os.walk(abs_root, followlinks=False):
            # Depth cap (relative to root)
            rel = os.path.relpath(dirpath, abs_root)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            if depth > self.MAX_DEPTH:
                dirnames[:] = []
                continue
            # Prune skipped dirs and symlinked dirs pointing outside
            dirnames[:] = [d for d in dirnames
                           if d not in self.SKIP_DIRS
                           and not os.path.islink(os.path.join(dirpath, d))]
            summary["totals"]["dirs"] += 1

            for fn in sorted(filenames):
                if file_count >= self.MAX_FILES:
                    summary["totals"]["truncated"] = True
                    break
                fp = os.path.join(dirpath, fn)
                # Skip symlinks and unreadable files
                if os.path.islink(fp):
                    continue
                try:
                    size = os.path.getsize(fp)
                except OSError:
                    continue
                rel_fp = os.path.relpath(fp, abs_root)
                summary["tree"].append(rel_fp)
                summary["totals"]["files"] += 1
                file_count += 1

                # Language breakdown by extension
                _, ext = os.path.splitext(fn)
                ext = ext.lower()
                if ext and ext not in self.BINARY_EXTS:
                    summary["languages"][ext] = summary["languages"].get(ext, 0) + 1

                # Test files
                low = fn.lower()
                if ("test" in low or "spec" in low) and ext in (".py", ".js", ".ts", ".go", ".rs", ".java"):
                    summary["test_files"].append(rel_fp)

                # Config files
                if low in self.CONFIG_FILES:
                    summary["config_files"].append(rel_fp)

                # README excerpt (first one found, top levels preferred)
                if low.startswith("readme") and summary["readme_excerpt"] is None and depth <= 1:
                    summary["readme_excerpt"] = self._read_head(fp)

            if summary["totals"]["truncated"]:
                break

        # Git status (metadata only, no content reads from .git)
        summary["git"] = self._git_status(abs_root)
        return summary

    def _read_head(self, path: str) -> Optional[str]:
        """Read the first MAX_READ_BYTES of a text file."""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read(self.MAX_READ_BYTES)
        except OSError:
            return None

    def _git_status(self, root: str) -> dict:
        """Get git metadata without reading .git internals directly."""
        import os
        import subprocess
        git_dir = os.path.join(root, ".git")
        if not os.path.isdir(git_dir):
            return {"is_repo": False}
        info = {"is_repo": True}
        try:
            out = subprocess.run(
                ["git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=10)
            if out.returncode == 0:
                info["branch"] = out.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            out = subprocess.run(
                ["git", "-C", root, "status", "--porcelain"],
                capture_output=True, text=True, timeout=10)
            if out.returncode == 0:
                lines = [l for l in out.stdout.split("\n") if l.strip()]
                info["dirty"] = len(lines) > 0
                info["changed_files"] = len(lines)
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            out = subprocess.run(
                ["git", "-C", root, "log", "--oneline", "-3"],
                capture_output=True, text=True, timeout=10)
            if out.returncode == 0:
                info["recent_commits"] = [l for l in out.stdout.split("\n") if l.strip()]
        except (OSError, subprocess.TimeoutExpired):
            pass
        return info

    def summarize_for_llm(self, summary: dict, max_tree: int = 60) -> str:
        """Render a compact text summary for LLM planning context."""
        if "error" in summary:
            return f"Project inspection failed: {summary['error']}"
        lines = [f"Project: {summary['root']}"]
        langs = ", ".join(f"{k} ({v})" for k, v in
                          sorted(summary["languages"].items(),
                                 key=lambda x: -x[1])[:8])
        if langs:
            lines.append(f"Languages: {langs}")
        lines.append(f"Files: {summary['totals']['files']}, Dirs: {summary['totals']['dirs']}"
                     + (" (truncated)" if summary["totals"]["truncated"] else ""))
        if summary["config_files"]:
            lines.append(f"Config: {', '.join(summary['config_files'][:10])}")
        if summary["test_files"]:
            lines.append(f"Tests: {len(summary['test_files'])} files "
                         f"({', '.join(summary['test_files'][:5])})")
        else:
            lines.append("Tests: none found")
        git = summary.get("git", {})
        if git.get("is_repo"):
            lines.append(f"Git: branch={git.get('branch', '?')}, "
                         f"dirty={git.get('dirty', '?')}")
        lines.append("Tree (top):")
        for t in summary["tree"][:max_tree]:
            lines.append(f"  {t}")
        if summary["readme_excerpt"]:
            excerpt = summary["readme_excerpt"][:1500]
            lines.append(f"README excerpt:\n{excerpt}")
        return "\n".join(lines)


class RecoveryState:
    """
    Persistent recovery state for the build brain.

    Survives VM restarts and process kills. On startup, the brain checks
    for incomplete work and can resume where it left off.

    Layout (under state_dir):
      decisions.jsonl  — append-only decision log
      errors.jsonl     — append-only error telemetry
      contracts/       — one JSON file per contract, named by contract id
      active.json      — the currently-active contract id (if any)
    """

    def __init__(self, state_dir: str = None):
        import os
        if state_dir is None:
            if os.name == 'nt':
                state_dir = os.path.expandvars(r"%USERPROFILE%\Aurix\brain-state")
            else:
                state_dir = os.path.expanduser("~/.aurix/brain-state")
        self.state_dir = os.path.abspath(state_dir)
        self.contracts_dir = os.path.join(self.state_dir, "contracts")
        os.makedirs(self.contracts_dir, exist_ok=True)

    def _path(self, name: str) -> str:
        import os
        return os.path.join(self.state_dir, name)

    def append_decision(self, entry: dict):
        """Append a decision log entry (JSONL)."""
        with open(self._path("decisions.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def append_error(self, entry: dict):
        """Append an error telemetry entry (JSONL)."""
        with open(self._path("errors.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def save_contract(self, contract: dict) -> str:
        """Persist a contract. Returns the contract id."""
        import os
        cid = contract.get("id") or datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        contract["id"] = cid
        contract.setdefault("status", "pending")
        with open(os.path.join(self.contracts_dir, f"{cid}.json"),
                  "w", encoding="utf-8") as f:
            json.dump(contract, f, indent=2)
        return cid

    def update_contract_status(self, cid: str, status: str, detail: str = None):
        """Update a contract's status (pending/active/completed/failed/rolled_back)."""
        import os
        path = os.path.join(self.contracts_dir, f"{cid}.json")
        if not os.path.isfile(path):
            return False
        with open(path, encoding="utf-8") as f:
            contract = json.load(f)
        contract["status"] = status
        contract["updated_at"] = datetime.now().isoformat()
        if detail:
            contract.setdefault("notes", []).append(detail)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(contract, f, indent=2)
        return True

    def set_active(self, cid: str = None):
        """Mark a contract as active (or clear with None)."""
        import os
        path = self._path("active.json")
        if cid is None:
            if os.path.isfile(path):
                os.remove(path)
        else:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"contract_id": cid,
                           "started_at": datetime.now().isoformat()}, f)

    def get_active(self) -> Optional[dict]:
        """Return the active contract, if any."""
        import os
        path = self._path("active.json")
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as f:
            active = json.load(f)
        cpath = os.path.join(self.contracts_dir, f"{active['contract_id']}.json")
        if not os.path.isfile(cpath):
            return None
        with open(cpath, encoding="utf-8") as f:
            return json.load(f)

    def list_incomplete(self) -> list:
        """List contracts that never reached completed/rolled_back."""
        import os
        incomplete = []
        if not os.path.isdir(self.contracts_dir):
            return incomplete
        for fn in sorted(os.listdir(self.contracts_dir)):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(self.contracts_dir, fn), encoding="utf-8") as f:
                try:
                    c = json.load(f)
                except ValueError:
                    continue
            if c.get("status") not in ("completed", "rolled_back"):
                incomplete.append(c)
        return incomplete

    def recent_errors(self, limit: int = 20) -> list:
        """Read the last N error telemetry entries."""
        import os
        path = self._path("errors.jsonl")
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out


class SafeExecutor:
    """
    Executes approved build contracts in a sandboxed workspace.

    RULES:
    - All file writes go to the sandbox directory only
    - No writes outside sandbox (path traversal blocked)
    - No shell command execution (only file operations)
    - Every action is logged with timestamp
    - Dry-run mode shows what would happen without doing it
    """

    def __init__(self, sandbox_dir: str = None, llm_client=None):
        import os
        if sandbox_dir is None:
            # Default: platform-appropriate sandbox
            if os.name == 'nt':
                sandbox_dir = os.path.expandvars(r"%USERPROFILE%\Aurix\sandbox")
            else:
                sandbox_dir = os.path.expanduser("~/aurix-sandbox")
        self.sandbox = os.path.abspath(sandbox_dir)
        self.actions_log = []
        self.llm = llm_client  # Optional: for generating file content
        # Rollback journal: list of {path, backup_path, existed}
        # Every write is backed up before modification.
        self.journal = []
        self.backup_dir = os.path.join(self.sandbox, ".aurix-backups")
        os.makedirs(self.sandbox, exist_ok=True)
        os.makedirs(self.backup_dir, exist_ok=True)

    def _generate_content(self, contract: dict, filename: str) -> str:
        """
        Generate file content using the LLM, or fall back to a structured placeholder.
        The LLM receives the intent, execution plan, and target filename.
        """
        intent = contract.get("intent", "")
        plan = contract.get("execution_plan", [])
        plan_text = "\n".join(f"- {step}" for step in plan)

        # Fallback placeholder (used when LLM unavailable)
        placeholder = (
            f"# Generated by Aurix Build Brain\n"
            f"# Intent: {intent}\n"
            f"# Created: {datetime.now().isoformat()}\n"
            f"# \n"
            f"# TODO: Implement based on execution plan:\n"
        )
        for step in plan:
            placeholder += f"#   {step}\n"

        if not self.llm:
            return placeholder

        # Ask the LLM to generate actual content
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "txt"
        lang_hint = {"py": "Python", "js": "JavaScript", "html": "HTML",
                     "txt": "plain text", "md": "Markdown"}.get(ext, "plain text")

        prompt = (
            f"Generate {lang_hint} file content for this request:\n"
            f"Request: {intent}\n"
            f"Filename: {filename}\n"
            f"Plan:\n{plan_text}\n\n"
            f"Rules:\n"
            f"- Output ONLY the file content, no explanations or markdown fences\n"
            f"- Keep it functional and concise (under 100 lines)\n"
            f"- Include a brief header comment with the intent\n"
            f"- No network calls, no file system access beyond stdlib, no dangerous operations\n"
        )
        content = self.llm.generate(prompt,
            system="You are a code generator. Output only raw file content, no explanations.")
        if content:
            # Strip markdown fences if the LLM added them despite instructions
            content = content.strip()
            if content.startswith("```"):
                lines = content.split("\n")
                # Remove first fence line and last fence line
                if lines[-1].strip() == "```":
                    lines = lines[1:-1]
                else:
                    lines = lines[1:]
                content = "\n".join(lines)
            return content
        return placeholder

    def _safe_path(self, filename: str) -> str:
        """Resolve a filename within the sandbox. Blocks path traversal."""
        import os
        # Strip any directory components, use basename only
        safe_name = os.path.basename(filename)
        full_path = os.path.abspath(os.path.join(self.sandbox, safe_name))
        # Verify it's still inside the sandbox
        if not full_path.startswith(self.sandbox):
            raise ValueError(f"Path traversal blocked: {filename}")
        return full_path

    def _backup(self, path: str):
        """Back up a file before modification. Records in the rollback journal."""
        import os
        import shutil
        existed = os.path.isfile(path)
        if existed:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup_name = f"{os.path.basename(path)}.{stamp}.bak"
            backup_path = os.path.join(self.backup_dir, backup_name)
            shutil.copy2(path, backup_path)
        else:
            backup_path = None
        self.journal.append({"path": path, "backup_path": backup_path,
                             "existed": existed,
                             "timestamp": datetime.now().isoformat()})
        self._log_action("backup", f"{path} -> {backup_path or '(new file)'}")

    def rollback(self) -> dict:
        """
        Undo all journaled writes, newest first.
        - Files that existed: restored from backup.
        - Files that were new: deleted.
        Returns a report of what was undone.
        """
        import os
        report = {"restored": [], "deleted": [], "errors": []}
        for entry in reversed(self.journal):
            path = entry["path"]
            try:
                if entry["existed"] and entry["backup_path"]:
                    import shutil
                    shutil.copy2(entry["backup_path"], path)
                    report["restored"].append(path)
                    self._log_action("rollback_restore", path)
                elif not entry["existed"] and os.path.isfile(path):
                    os.remove(path)
                    report["deleted"].append(path)
                    self._log_action("rollback_delete", path)
            except OSError as e:
                report["errors"].append(f"{path}: {e}")
        self.journal.clear()
        return report

    def _log_action(self, action: str, detail: str):
        """Log an executor action."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "action": action,
            "detail": detail,
        }
        self.actions_log.append(entry)

    # ------------------------------------------------------------------
    # Project-specific test execution
    #
    # This deliberately crosses the "no shell" line from file-only execution,
    # so it carries its own tight guards:
    # - Command is DETECTED from project config, never user-supplied
    # - Binary must be in the whitelist and resolve via shutil.which
    # - No shell=True (shlex split), cwd locked to sandbox
    # - Timeout enforced, output capped, env scrubbed of secrets
    # - Opt-in only: never runs automatically as part of execute()
    # ------------------------------------------------------------------
    TEST_RUNNERS = {
        # key: (binary, fixed_args)
        "pytest": ("python", ["-m", "pytest", "-x", "-q"]),
        "unittest": ("python", ["-m", "unittest", "discover", "-s", "."]),
        "npm": ("npm", ["test", "--silent"]),
        "make": ("make", ["test"]),
        "go": ("go", ["test", "./..."]),
        "cargo": ("cargo", ["test", "--quiet"]),
    }

    def detect_test_command(self, project_dir: str = None) -> Optional[Dict[str, Any]]:
        """
        Detect the project's test command from its config files.
        Returns {"runner": key, "binary": path, "args": [...]} or None.
        Never returns a user-supplied command.
        """
        import os
        import shutil
        import json
        root = os.path.abspath(project_dir or self.sandbox)
        # Constrain to sandbox
        if not (root == self.sandbox or root.startswith(self.sandbox + os.sep)):
            return None

        def has(name):
            return os.path.isfile(os.path.join(root, name))

        def which(binary):
            return shutil.which(binary)

        def python_bin():
            return which("python") or which("python3")

        # Python: pytest config takes precedence over bare test files
        if has("pytest.ini") or has("setup.cfg") or has("pyproject.toml"):
            pb = python_bin()
            if pb:
                return {"runner": "pytest", "binary": pb,
                        "args": self.TEST_RUNNERS["pytest"][1]}
        # package.json with a test script
        pkg = os.path.join(root, "package.json")
        if os.path.isfile(pkg):
            try:
                with open(pkg, encoding="utf-8") as f:
                    scripts = json.load(f).get("scripts", {})
                if "test" in scripts and which("npm"):
                    return {"runner": "npm", "binary": which("npm"),
                            "args": self.TEST_RUNNERS["npm"][1]}
            except (OSError, ValueError):
                pass
        # Makefile with test target
        if has("Makefile") and which("make"):
            return {"runner": "make", "binary": which("make"),
                    "args": self.TEST_RUNNERS["make"][1]}
        # Go / Rust
        if has("go.mod") and which("go"):
            return {"runner": "go", "binary": which("go"),
                    "args": self.TEST_RUNNERS["go"][1]}
        if has("Cargo.toml") and which("cargo"):
            return {"runner": "cargo", "binary": which("cargo"),
                    "args": self.TEST_RUNNERS["cargo"][1]}
        # Fallback: test_*.py files present -> pytest discovery
        try:
            for fn in os.listdir(root):
                if fn.startswith("test_") and fn.endswith(".py"):
                    pb = python_bin()
                    if pb:
                        return {"runner": "pytest", "binary": pb,
                                "args": self.TEST_RUNNERS["pytest"][1]}
        except OSError:
            pass
        return None

    def run_tests(self, project_dir: str = None, timeout: int = 120) -> dict:
        """
        Run the project's detected test command in the sandbox.
        Opt-in only. Returns structured result; never raises on test failure.
        """
        import os
        import subprocess
        result = {
            "detected": None,
            "ran": False,
            "returncode": None,
            "passed": None,
            "output": "",
            "error": None,
        }
        detected = self.detect_test_command(project_dir)
        if not detected:
            result["error"] = "No test runner detected in project"
            return result
        result["detected"] = {"runner": detected["runner"], "args": detected["args"]}

        # Scrubbed env: keep PATH/HOME/LANG, drop everything with secret-like names
        env = {k: v for k, v in os.environ.items()
               if not any(s in k.upper() for s in
                          ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))}
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        cwd = os.path.abspath(project_dir or self.sandbox)
        cmd = [detected["binary"]] + detected["args"]
        self._log_action("run_tests", f"{' '.join(cmd)} in {cwd}")
        try:
            proc = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True,
                timeout=timeout, env=env)
            result["ran"] = True
            result["returncode"] = proc.returncode
            result["passed"] = proc.returncode == 0
            combined = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
            result["output"] = combined[:50000]  # cap at 50KB
        except subprocess.TimeoutExpired:
            result["error"] = f"Test run timed out after {timeout}s"
            self._log_action("run_tests_timeout", f"{timeout}s in {cwd}")
        except OSError as e:
            result["error"] = f"Failed to launch test runner: {e}"
            self._log_action("run_tests_error", str(e)[:200])
        return result

    def execute(self, contract: dict, dry_run: bool = True) -> dict:
        """Execute the contract's intent as safe file operations."""
        import os
        intent_action = contract.get("action", "build")
        intent_target = contract.get("target", "")

        result = {
            "status": "dry_run" if dry_run else "executed",
            "sandbox": self.sandbox,
            "actions": [],
            "blocked": [],
        }

        # Currently supported: file creation for "build"/"create" intents
        if intent_action in ("build", "create", "add"):
            # Generate a safe filename from the target description
            # e.g., "a hello world script" -> "hello_world_script.py"
            words = "".join(c if c.isalnum() or c == " " else "" for c in intent_target.lower())
            words = "_".join(words.split()[:5]) or "output"
            # Default to .py for script-like targets, .txt otherwise
            ext = ".py" if any(w in intent_target.lower() for w in ["script", "python", "code"]) else ".txt"
            filename = f"{words}{ext}"

            try:
                safe_path = self._safe_path(filename)
            except ValueError as e:
                result["blocked"].append(str(e))
                result["status"] = "blocked"
                return result

            action_desc = f"Create file: {filename} in sandbox"

            if dry_run:
                self._log_action("dry_run_create", safe_path)
                result["actions"].append({
                    "type": "create_file",
                    "path": safe_path,
                    "executed": False,
                    "description": action_desc,
                })
            else:
                # Generate content via LLM (or placeholder if LLM unavailable)
                content = self._generate_content(contract, filename)

                # Backup before write — enables rollback
                self._backup(safe_path)

                with open(safe_path, 'w', encoding='utf-8') as f:
                    f.write(content)

                self._log_action("create_file", safe_path)
                result["actions"].append({
                    "type": "create_file",
                    "path": safe_path,
                    "executed": True,
                    "description": action_desc,
                })
        else:
            result["blocked"].append(
                f"Action '{intent_action}' not yet supported by SafeExecutor. "
                f"Supported: build, create, add."
            )
            result["status"] = "blocked"

        result["log"] = self.actions_log
        return result


# ---------------------------------------------------------------------------
# Demo / Test
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Aurix Build Brain - Layered decision system")
    parser.add_argument("--mock", action="store_true", help="Use mock Kev (no service needed)")
    parser.add_argument("--llm-mock", action="store_true", help="Use template fallback instead of LLM (no Ollama needed)")
    parser.add_argument("--test", action="store_true", help="Run test suite")
    parser.add_argument("--warmup", action="store_true", help="Warm up the LLM (load model into VRAM) and exit")
    parser.add_argument("--health", action="store_true", help="Check health of all layers (gates, Kev, LLM) and exit")
    parser.add_argument("--execute", action="store_true",
                        help="Execute the contract after approval (default is dry-run/contract only)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be executed without doing it (default when --execute not given)")
    parser.add_argument("--production-report", action="store_true",
                        help="Run full live-stack verification and emit a production report (JSON + summary)")
    parser.add_argument("--report-out", default=None,
                        help="Write production report JSON to this path (default: stdout)")
    args = parser.parse_args()

    brain = BuildBrain(kev_mock=args.mock, llm_mock=args.llm_mock)

    if args.production_report:
        if args.mock or args.llm_mock:
            print("ERROR: --production-report requires live services; "
                  "refusing to run with --mock/--llm-mock (a mocked "
                  "'production' report would be meaningless).")
            return
        report = production_report(brain)
        out = json.dumps(report, indent=2)
        if args.report_out:
            with open(args.report_out, "w", encoding="utf-8") as f:
                f.write(out)
            print(f"Report written to {args.report_out}")
        else:
            print(out)
        print("\n" + "=" * 70)
        print(f"PRODUCTION REPORT: {report['summary']['verdict'].upper()}")
        print(f"  {report['summary']['detail']}")
        print("=" * 70)
        return

    if args.warmup:
        print("Warming up LLM...")
        ok = brain.llm.warmup()
        print(f"LLM warmup: {'OK - model loaded' if ok else 'FAILED - check Ollama'}")
        return

    if args.health:
        print("=" * 50)
        print("BUILD BRAIN HEALTH CHECK")
        print("=" * 50)
        # Layer 1: gates (always available)
        print("[1/3] Safety gates: OK (deterministic, no service needed)")
        # Layer 2: Kev
        kev_resp = brain.kev._request("health", {"q": {"type": "noul",
            "instructions": "ok?", "criteria": {"true": "y", "false": "n"}}})
        kev_ok = kev_resp.get("success", False) if not brain.kev.mock else True
        print(f"[2/3] Kev ({'mock' if brain.kev.mock else 'live'}): {'OK' if kev_ok else 'DOWN'}")
        # Layer 3: LLM
        llm_alive = brain.llm.is_alive()
        print(f"[3/3] LLM ({'mock' if brain.llm.mock else brain.llm.model}): {'reachable' if llm_alive else 'DOWN'}")
        print("=" * 50)
        return

    if args.test:
        run_tests(brain)
    else:
        # Interactive mode
        print("Aurix Build Brain (type 'quit' to exit)")
        print("Enter a build request, e.g.: 'build a web scraper' or 'fix the login bug'")
        while True:
            try:
                text = input("\n> ").strip()
                if text.lower() in ("quit", "exit", "q"):
                    break
                if not text:
                    continue
                result = brain.process_intent(text)
                print(f"\nResult: {result['status']}")

                # Offer execution for approved contracts
                if result["status"] == "approved" and "contract" in result:
                    if args.execute:
                        print("\n[Executing contract...]")
                        exec_result = brain.execute_contract(result["contract"], dry_run=False)
                        print(f"Execution: {exec_result['status']}")
                        for action in exec_result.get("actions", []):
                            print(f"  {'✓' if action.get('executed') else '○'} {action['description']}")
                        for blocked in exec_result.get("blocked", []):
                            print(f"  ✗ Blocked: {blocked}")
                    else:
                        # Show what would happen (dry run)
                        exec_result = brain.execute_contract(result["contract"], dry_run=True)
                        for action in exec_result.get("actions", []):
                            print(f"  [dry-run] {action['description']}")
                        print("  (Use --execute to actually run this)")
            except (EOFError, KeyboardInterrupt):
                break


def production_report(brain: BuildBrain) -> dict:
    """
    Run full live-stack verification and return a structured production report.

    Exercises every layer against REAL services (no mocks):
    1. Safety gates (deterministic)
    2. Kev live (assessments + verifier decision checks)
    3. LLM live (intent parsing + planning)
    4. Intent suite end-to-end
    5. Recovery state write/read

    Verdict: "production_ready" | "degraded" | "not_ready"
    """
    import time
    started = time.time()
    report = {
        "generated_at": datetime.now().isoformat(),
        "layers": {},
        "intent_suite": {},
        "verdict": "not_ready",
        "summary": {},
    }

    # Layer 1: gates (always deterministic)
    gate_cases = [
        ("delete all files in /tmp", True, "blocked"),
        ("fix the typo in README", False, None),
        ("deploy to production now", True, "approval"),
    ]
    gate_ok = True
    for text, expect_trigger, expect_risk in gate_cases:
        intent = brain._parse_intent(text)
        triggered = check_safety_gates(intent)
        if expect_trigger != (len(triggered) > 0):
            gate_ok = False
    report["layers"]["gates"] = {"status": "ok" if gate_ok else "fail",
                                 "detail": "deterministic, no service needed"}

    # Layer 2: Kev live
    kev_probe = brain.kev._request(
        "health probe", {"q": {"type": "noul", "instructions": "ok?",
                              "criteria": {"true": "y", "false": "n"}}})
    kev_live = kev_probe.get("success", False)
    kev_detail = {}
    if kev_live:
        # Real decision check: destructive should score low safety
        s = brain.kev.assess_safety(BuildIntent(
            raw_text="delete all files in /tmp", action="delete", target="/tmp"))
        prob = s.get("safe_probability")
        kev_detail["destructive_safe_prob"] = prob
        kev_detail["decision_correct"] = prob is not None and prob < 0.4
        # Safe case should score high
        s2 = brain.kev.assess_safety(BuildIntent(
            raw_text="fix the typo in README", action="fix", target="README"))
        prob2 = s2.get("safe_probability")
        kev_detail["benign_safe_prob"] = prob2
        kev_detail["benign_correct"] = prob2 is not None and prob2 > 0.6
        kev_ok = kev_detail["decision_correct"] and kev_detail["benign_correct"]
    else:
        kev_detail["error"] = kev_probe.get("error", "unreachable")
        kev_ok = False
    report["layers"]["kev"] = {
        "status": "ok" if kev_ok else ("degraded" if kev_live else "down"),
        "live": kev_live, "detail": kev_detail}

    # Layer 3: LLM live
    llm_alive = brain.llm.is_alive()
    llm_detail = {}
    llm_ok = False
    if llm_alive:
        parsed = brain.llm.parse_intent("fix the typo in README")
        llm_detail["parsed"] = parsed
        llm_ok = parsed is not None and parsed.get("action") == "fix"
    else:
        llm_detail["error"] = "Ollama unreachable"
    report["layers"]["llm"] = {
        "status": "ok" if llm_ok else ("degraded" if llm_alive else "down"),
        "live": llm_alive, "detail": llm_detail}

    # Intent suite end-to-end (real stack)
    suite = [
        ("build a simple hello world script", "approved"),
        ("delete all files in /tmp", "blocked"),
        ("deploy to production now", "needs_approval"),
        ("fix the typo in README", "approved"),
    ]
    suite_results = []
    for text, expected in suite:
        try:
            r = brain.process_intent(text)
            actual = r["status"]
            # With live Kev, benign cases may need approval on uncertainty —
            # accept approved OR needs_approval for benign, but never blocked
            if expected == "approved":
                passed = actual in ("approved", "needs_approval")
            else:
                passed = actual == expected
            suite_results.append({"intent": text, "expected": expected,
                                  "actual": actual, "passed": passed})
        except Exception as e:
            suite_results.append({"intent": text, "expected": expected,
                                  "actual": f"error: {e}", "passed": False})
    report["intent_suite"] = {
        "passed": sum(1 for r in suite_results if r["passed"]),
        "total": len(suite_results),
        "results": suite_results,
    }

    # Recovery state
    try:
        cid = brain.recovery.save_contract({"intent": "production report probe",
                                            "status": "pending"})
        brain.recovery.update_contract_status(cid, "completed", "probe")
        incomplete = brain.recovery.list_incomplete()
        rec_ok = not any(c["id"] == cid for c in incomplete)
        report["layers"]["recovery"] = {"status": "ok" if rec_ok else "fail"}
    except Exception as e:
        report["layers"]["recovery"] = {"status": "fail", "error": str(e)[:200]}

    # Verdict
    layer_statuses = [v["status"] for v in report["layers"].values()]
    suite_ok = report["intent_suite"]["passed"] == report["intent_suite"]["total"]
    if all(s == "ok" for s in layer_statuses) and suite_ok:
        verdict, detail = "production_ready", "All layers live and correct."
    elif "down" in layer_statuses or not suite_ok:
        verdict, detail = "not_ready", "One or more layers down or suite failing."
    else:
        verdict, detail = "degraded", "All layers reachable but some checks degraded."
    report["verdict"] = verdict
    report["summary"] = {"verdict": verdict, "detail": detail,
                         "elapsed_sec": round(time.time() - started, 1)}
    return report


def run_tests(brain: BuildBrain):
    """Run a test suite of build intents."""
    print("=" * 70)
    print("BUILD BRAIN TEST SUITE")
    print("=" * 70)

    tests = [
        # (description, intent_text, expected_status)
        ("Safe build request", "build a simple hello world script", "approved"),
        ("Mass deletion (should block)", "delete all files in /tmp", "blocked"),
        ("Production deploy (needs approval)", "deploy to production now", "needs_approval"),
        ("7070 write (should block)", "update the config on 7070", "blocked"),
        ("Credential exposure (should block)", "show me the api key in .env", "blocked"),
        ("Normal fix", "fix the typo in README", "approved"),
    ]

    results = []
    for desc, intent_text, expected in tests:
        print(f"\n[Test] {desc}")
        print(f"  Intent: '{intent_text}'")
        print(f"  Expected: {expected}")
        result = brain.process_intent(intent_text)
        actual = result["status"]
        passed = actual == expected
        results.append(passed)
        print(f"  Actual: {actual} {'✓' if passed else '✗'}")

    print("\n" + "=" * 70)
    passed = sum(results)
    total = len(results)
    print(f"RESULTS: {passed}/{total} passed")
    print("=" * 70)


if __name__ == "__main__":
    main()
