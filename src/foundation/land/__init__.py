"""src/foundation/land - LandPilot: AURIX's land intelligence capability (docs/land/).

It is a capability of AURIX, not a separate system. It owns only what AURIX did not already have: the property dossier, evidence
grading, the kill list, deal economics and the acquisition/utilization gates. Everything else is the Foundation's own:
  owner auth        commands.py via the Telegram listener (sender verified)     approvals   hub.py proposals, same pattern as freelance/content
  audit             audit.py hash chain                                           web work    missions under the land_intelligence pack (capabilities.py)
  memory            memory.py proposals (owner preferences, postmortem lessons)   income      earnings.py (`earned <amount> land`)
  untrusted text    memory.SENSITIVE + src/prompt_security.py                     protection  identity.PROTECTED_COMPONENTS (src/foundation/land/, data/land/)

Automation levels from the project brief, expressed in AURIX's own mechanisms:
  L0 research / L1 recommend   no gated tools: dossiers, screening, reports (this package today)
  L2 prepare, approve to send  a proposal the owner approves (hub.py), or a mission contract for web research
  L3 routine pre-approved      a standing mission, only if the pack is marked standing_ok (it is not)
  L4/L5 transactions, legal    never delegated: AURIX records the owner's decision and does not execute
"""
