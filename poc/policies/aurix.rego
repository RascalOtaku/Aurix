# AURIX approval policy for Open Policy Agent (Activation Handoff §10 task 4).
#
# It mirrors, rule for rule, what src/approval_gate.enforce decides today, so the two can be compared in "shadow" mode
# (src/foundation/policy_opa.py) before OPA is ever allowed to decide anything:
#
#   STOP engaged                                   -> deny   (STOP is absolute, spec §2)
#   CRITICAL risk / a protected component          -> deny   (no approval or contract unlocks it)
#   LOW risk                                       -> allow  (audited)
#   MEDIUM risk covered by an approved contract    -> allow  (the contract decides: tool list, ceiling, prohibited patterns)
#   everything else (HIGH, MEDIUM uncovered,
#                    unknown tier / anything odd)  -> ask    (unclassified = maximum risk until authorised, spec §2)
#
# Input:  {"stop": bool, "tier": "LOW|MEDIUM|HIGH|CRITICAL", "contract_covers": bool}
# Output: data.aurix.approval.decision = {"action": "allow|ask|deny", "reason": "..."}
#
# FAIL CLOSED: a missing or malformed input falls through to the default, which asks. This policy can never say "allow"
# unless the input explicitly proves it.
package aurix.approval

import rego.v1

default decision := {"action": "ask", "reason": "unclassified or unrecognised input: maximum risk until the owner authorises"}

decision := {"action": "deny", "reason": "STOP is absolute"} if {
	input.stop == true
} else := {"action": "deny", "reason": "critical risk or protected component: never allowed, with or without approval"} if {
	input.tier == "CRITICAL"
} else := {"action": "allow", "reason": "low risk: runs and is audited"} if {
	input.stop == false
	input.tier == "LOW"
} else := {"action": "allow", "reason": "medium risk covered by an approved mission contract"} if {
	input.stop == false
	input.tier == "MEDIUM"
	input.contract_covers == true
} else := {"action": "ask", "reason": "needs the owner's approval"} if {
	input.stop == false
	input.tier in {"MEDIUM", "HIGH"}
}
