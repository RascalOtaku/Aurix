"""AURIX Foundation 1.0 (activation handoff §10): identity, risk, mission contracts,
tamper-evident audit, capability registry, planner and runner.

Stdlib only, so the whole package imports and is unit-testable without the app.
Several modules here are STRUCTURALLY PROTECTED (see identity.PROTECTED_COMPONENTS):
no agent tool may write to them.
"""
