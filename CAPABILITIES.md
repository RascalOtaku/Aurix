# Capabilities

Aurix is the base Odysseus platform (Chat/Agent/Cookbook/Deep Research/Compare/Documents/Email/Notes/Calendar - see
README) **plus** a substantial custom layer in `src/foundation/`, built specifically for one person's daily use. This
page exists so a contributor - human or AI - checks here and greps `src/foundation/` *before* proposing something new.
If an idea sounds like "agent orchestration", "a skill system", "self-improvement", or "status/health reporting",
it almost certainly already exists below in a more specific, safety-checked form than a fresh generic implementation
would be. Extend what's here instead of adding a parallel version.

## Mission orchestration (NOT a generic multi-agent framework - don't add one)
- `src/foundation/mission.py`, `runner.py` - a mission's steps run under real resource budgets (tool calls, model
  calls, wall-clock minutes), status tracked end to end, nothing silently retried past its limits.
- `src/foundation/standing.py` - recurring ("standing") missions on a schedule, with pause/resume/retire and a
  failure-streak auto-pause so a broken schedule can't run forever unattended.
- `src/approval_gate.py`, `src/tool_execution.py`, `src/tool_security.py` - every tool call is gated; protected
  paths can never be unlocked by a mission's own contract; fail-closed on any internal error.

## Skills (NOT a generic skill registry - the sandbox + approval are the point)
- `src/foundation/forge.py` - an owner request becomes a drafted skill + its own unit tests, checked statically,
  run in an isolated sandbox, and only usable after an explicit owner approval. Hash-pinned once approved.
- `src/foundation/evolve.py`, `evolve_domains.py` - a general PBT/GA search engine: any domain (so far, forge's own
  guidance prompt) evolves via real measured fitness (e.g. real sandbox pass rate), never a self-report, and a
  winning candidate only goes live after owner approval. Built to be reused for other domains, not one-off.
- `src/foundation/upgrades.py` - a separate lane for AURIX proposing small, signed, sandbox-tested edits to its own
  code, with automatic rollback if a deploy doesn't come up healthy.

## Status / health (NOT a new status-tracking layer - this already reports in real time)
- `src/foundation/command_center.py`, `sysview.py` - the real dashboard snapshot: service health, running missions,
  memory, skills, trust/growth stats, all probed live, not cached claims.
- `src/foundation/heartbeat.py` - `waiting_for_you()` (what needs a yes/no right now), `overnight_digest()` (once a
  day), `pulse_digest()` (every couple hours, quiet-hours aware) - all built from the same real snapshot data.
- `src/foundation/watchdog.py`, `audit.py` - a hash-chained audit log (tamper-evident: each record's hash depends on
  the previous one) and a watchdog that reports a broken/forked chain honestly.

## Domain-specific money/research work (real, paper-only, never real money)
- `src/foundation/land/` (LandPilot) - property research + an acquisition approval gate; AURIX never signs or pays.
- `tasks/trade_agent.py`, `tasks/strategy_evolve.py` (outside odysseus/, different runtime) - a paper-only Alpaca
  trading agent whose strategy parameters are themselves evolved (see evolve.py above) and gated behind a real
  30-day live-paper probation before promotion. No order-posting code exists anywhere in this path.
- `tasks/strategy_lab.py` - 11 textbook strategies backtested honestly (in-sample/out-of-sample/live, with costs,
  no look-ahead) side by side against SPY.
- `src/foundation/freelance.py`, `content.py`, `learning.py`, `subscriptions.py`, `earnings.py`, `transcription.py` -
  other real money/research workstreams, each with the same draft-then-owner-decides shape.

## Resource awareness
- `src/startup_optimizer.py` - `detect_resource_mode()`, used by `app.py`'s real startup to decide whether to skip
  heavier warmup steps on constrained hardware. (A larger draft of this file duplicated startup work `app.py`
  already does live in production - MCP connections, tool-index warmup - so only the genuinely new resource
  detection piece was kept; see that file's own docstring.)

## What this means in practice
Before adding a new top-level module for "orchestration", "skills", "status", or "self-improvement": read the
relevant file above, and either extend it or explain in the PR why the existing system doesn't fit. A new file that
duplicates one of these without ever being imported by the real app (check: is anything outside its own test file
importing it?) will be reverted.
