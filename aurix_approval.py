"""
aurix_approval.py - Approval request component for the Aurix build pipeline.

This is the 7th stage of the north-star pipeline:
  inspect → reason → plan → build → test → REQUEST APPROVAL → recover/rollback

When a safety gate flags RiskLevel.APPROVAL, the pipeline pauses here.
This component:
  1. Formats the approval request as a one-tap card (user's established pattern)
  2. Persists it via RecoveryState so it survives restarts
  3. Waits for the user's decision (approve/deny)
  4. Returns the decision so the pipeline can resume or abort

Integration: The 7070's notification system (Telegram/command center) delivers
the card to the user. This component provides the structured data and the
resolution mechanism. Claude owns the 7070-side delivery integration.
"""

import json
import time
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any
from dataclasses import dataclass, field


def _sanitize_for_json(obj):
    """
    Recursively convert Enum instances to their values for JSON serialization.
    Prevents TypeError when risk_details contains RiskLevel enums from safety gates.
    """
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(v) for v in obj]
    return obj


@dataclass
class ApprovalRequest:
    """A structured approval request awaiting user decision."""
    id: str
    action: str              # What the pipeline wants to do
    target: str              # What it acts on
    reason: str              # Why approval is needed (gate trigger)
    risk_details: Dict[str, Any] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    status: str = "pending"  # pending | approved | denied | expired
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None

    def to_card(self) -> dict:
        """
        Format as a one-tap decision card.
        Follows the user's established pattern: fully loaded, one glowing button,
        tapping IS the decision. No decline, no snooze, no "mark done".
        """
        return {
            "type": "approval_request",
            "id": self.id,
            "header": f"Approval needed: {self.action}",
            "body": {
                "action": self.action,
                "target": self.target,
                "why": self.reason,
                "details": self.risk_details,
            },
            "buttons": [
                {
                    "label": f"Approve: {self.action}",
                    "action": "approve",
                    "request_id": self.id,
                    "style": "primary",  # The glowing button
                },
                {
                    "label": "Deny",
                    "action": "deny",
                    "request_id": self.id,
                    "style": "secondary",
                },
            ],
            "created_at": self.created_at,
            # No expiry by default; caller can set one if the decision is time-sensitive
        }

    def to_dict(self) -> dict:
        return _sanitize_for_json({
            "id": self.id,
            "action": self.action,
            "target": self.target,
            "reason": self.reason,
            "risk_details": self.risk_details,
            "context": self.context,
            "created_at": self.created_at,
            "status": self.status,
            "resolved_at": self.resolved_at,
            "resolved_by": self.resolved_by,
        })

    @classmethod
    def from_dict(cls, data: dict) -> "ApprovalRequest":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class ApprovalManager:
    """
    Manages approval requests for the build pipeline.

    Usage in BuildBrain.process_intent():
        if max_risk == RiskLevel.APPROVAL:
            approval = self.approval_manager.request(
                action=intent.action,
                target=intent.target,
                reason="; ".join(r for _, _, r in triggered),
                risk_details={"gates": triggered},
                context={"intent": intent.raw_text},
            )
            return {"status": "needs_approval", "approval_id": approval.id, ...}

    The pipeline then polls approval_manager.check(approval_id) or waits
    for the 7070's callback to call approval_manager.resolve(approval_id, decision).
    """

    def __init__(self, recovery=None):
        """
        Args:
            recovery: RecoveryState instance for persistence. If None,
                     approvals are kept in memory only (not recommended).
        """
        self.recovery = recovery
        self._memory: Dict[str, ApprovalRequest] = {}

    def _persist(self, approval: ApprovalRequest):
        """Persist approval to RecoveryState if available."""
        self._memory[approval.id] = approval
        if self.recovery:
            try:
                # Store as a contract with approval metadata
                self.recovery.save_contract({
                    "id": f"approval-{approval.id}",
                    "type": "approval_request",
                    "status": approval.status,
                    "approval": approval.to_dict(),
                })
            except OSError:
                pass  # persistence is best-effort

    def _load(self, approval_id: str) -> Optional[ApprovalRequest]:
        """Load approval from memory or RecoveryState."""
        if approval_id in self._memory:
            return self._memory[approval_id]
        if self.recovery:
            try:
                import os
                path = os.path.join(
                    self.recovery.contracts_dir, f"approval-{approval_id}.json"
                )
                if os.path.isfile(path):
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                    approval = ApprovalRequest.from_dict(data["approval"])
                    self._memory[approval_id] = approval
                    return approval
            except (OSError, KeyError, json.JSONDecodeError):
                pass
        return None

    def request(self, action: str, target: str, reason: str,
                risk_details: dict = None, context: dict = None) -> ApprovalRequest:
        """
        Create a new approval request. Returns the request with its ID.
        The caller is responsible for delivering the card to the user
        (via 7070's Telegram/command center integration).
        """
        approval_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        approval = ApprovalRequest(
            id=approval_id,
            action=action,
            target=target,
            reason=reason,
            risk_details=risk_details or {},
            context=context or {},
        )
        self._persist(approval)
        return approval

    def check(self, approval_id: str) -> Optional[ApprovalRequest]:
        """Check the current status of an approval request."""
        return self._load(approval_id)

    def resolve(self, approval_id: str, decision: str,
                resolved_by: str = "user") -> Optional[ApprovalRequest]:
        """
        Resolve an approval request.

        Args:
            approval_id: The request ID
            decision: "approved" or "denied"
            resolved_by: Who made the decision (default: "user")

        Returns:
            The resolved ApprovalRequest, or None if not found.
        """
        if decision not in ("approved", "denied"):
            raise ValueError(f"Decision must be 'approved' or 'denied', got '{decision}'")

        approval = self._load(approval_id)
        if not approval:
            return None
        if approval.status != "pending":
            return approval  # Already resolved; idempotent

        approval.status = decision
        approval.resolved_at = datetime.now().isoformat()
        approval.resolved_by = resolved_by
        self._persist(approval)

        # Update the contract status in RecoveryState
        if self.recovery:
            try:
                self.recovery.update_contract_status(
                    f"approval-{approval_id}",
                    "completed" if decision == "approved" else "failed",
                    f"User {decision} the request",
                )
            except OSError:
                pass

        return approval

    def wait_for_decision(self, approval_id: str, timeout: int = 3600,
                          poll_interval: int = 5) -> Optional[ApprovalRequest]:
        """
        Block waiting for a decision. For use in synchronous pipeline flows.

        Args:
            approval_id: The request ID
            timeout: Max seconds to wait (default: 1 hour)
            poll_interval: Seconds between checks (default: 5)

        Returns:
            The resolved ApprovalRequest, or None on timeout.
        """
        start = time.time()
        while time.time() - start < timeout:
            approval = self._load(approval_id)
            if approval and approval.status != "pending":
                return approval
            time.sleep(poll_interval)
        return None

    def pending_requests(self) -> list:
        """List all pending approval requests (for dashboard/sync)."""
        # Check memory first, then scan RecoveryState contracts
        pending = [a for a in self._memory.values() if a.status == "pending"]
        if self.recovery:
            try:
                import os
                for fname in os.listdir(self.recovery.contracts_dir):
                    if fname.startswith("approval-") and fname.endswith(".json"):
                        aid = fname[len("approval-"):-len(".json")]
                        if aid not in self._memory:
                            approval = self._load(aid)
                            if approval and approval.status == "pending":
                                pending.append(approval)
            except OSError:
                pass
        return pending
