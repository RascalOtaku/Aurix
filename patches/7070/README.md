# Code Review Fixes — 2026-10-03

In order of importance. Patch files are unified diffs against the 7070's code.
Apply with `patch -p1 < <patchfile>` from the repo root, then run the
related tests.

## 1. Conductor driver rejects 7B model output (P1) — PATCH READY

**File:** `01-conductor-driver-7b-fallback.patch`

**Problem:** `validate_todos()` rejects the local 7B model's planning output.
The `hostile()` regexes `_MONEY` and `_CONTACT` false-positive on natural
language ("pay attention to X" → `spend_money`, "send a test" → `contact_people`),
and the strict JSON requirement fails when the 7B writes plain lists.

**Fix:** New `validate_todos_with_fallback()` tries strict validation first.
On *format* failures only (`not_json`, `item_length`, `too_few`, etc.), it
extracts list-like lines and applies security-critical checks only (control
chars, emails, URLs, commands/injection, registry syntax). Security rejections
are never softened or retried.

**To wire in:** find the `plan_project` handler that calls `validate_todos`
and switch it to `validate_todos_with_fallback`. The return signature is
identical: `(items, "")` or `(None, reason)`.

**Verified 2026-10-03:** `tests/test_fallback_logic.py` passes all 5 checks:
7B plain list salvaged (4 items, no false positives), injection blocked,
unknown-host URL blocked, too-few detected, numbered list extracted.

## 2. Upgrade lane discards 54/67 drafts (P1) — PATCH READY

**File:** `02-upgrade-lane-repair-loop.patch`

**Problem:** The upgrade lane's `build_changes()` rejects 80% of drafts.
Most rejections are fixable format issues: `find` text matched N times
(model picked a non-unique snippet), missing test file, bad JSON — not bad
ideas.

**Fix:** New `build_changes_with_repair()` wraps the validator with a repair
loop. On a repairable rejection, it sends the model a focused prompt naming
the exact error and how to fix it (shorter snippet, add the test file, valid
JSON only). Up to 2 retries. Security rejections (risky calls, protected
paths) and judgement calls (too big, nothing worth changing) are never
retried.

**To wire in:** find the caller of `build_changes()` in the draft flow and
switch to `build_changes_with_repair(obj, task_text, raw_reply, _call_engineer)`.
Returns `(changes, why, tries_used)`.

## 2. LandPilot mission m-2c74b3 still looping (P0) — MANUAL STEP

**Problem:** The mission keeps issuing multiple tool calls per message and
looping on web searches. The `4fe3046` code fix (one tool per round, drop
stale summary) is deployed, but this specific mission instance is stuck.

**Fix (manual):** Stop the mission. It cannot be fixed by code changes —
the instance state is the problem. Then either:
- Re-run it fresh (it will pick up the fixed turn logic), or
- Leave it stopped; Land Watch covers the deterministic finding path.

No patch — this needs a human or Claude with write access to stop the mission.

## 3. Model-failure resilience unmeasured (P1) — MANUAL STEP

**Problem:** Circuit breaker, cooldowns, and failover shipped, but nobody has
checked whether the failure rate actually dropped (the Groq 403 issue, the
upgrade lane discarding 54/67 drafts).

**Fix (manual):** Check `routing_stats` / the audit log for:
- Provider failure rate before vs. after the resilience deploy
- Upgrade lane draft acceptance rate (was 54/67 discarded)
- Whether the Groq 403s stopped after the User-Agent fix (`7b25e90`)

No patch — this is a measurement task, not a code change.

## 4. GitHub/7070 fork (P1) — DECISION NEEDED

The live line (now `8241a9e`) and GitHub main (`e8300d9`) forked at `562af63`.
`src/agent_loop.py` has my 13 bug fixes on GitHub and your resilience layer
on the 7070. Do not auto-merge. Options:
- (a) Leave the fork; port selected GitHub improvements to the 7070 later
- (b) Merge GitHub → 7070
- (c) Merge 7070 → GitHub

## 5. Podcast prospect finder lead quality (P2) — NO PATCH

~1 usable lead per search because hosting-platform relay addresses don't
count as contacts. This needs a product decision (what counts as a contact?)
before a code fix. Not patched.

## 6. Land Watch is thin (P2) — NO PATCH

Working as designed (tripwire, not pipeline). The parcel-level deepening is
already on Claude's build list as item 2. Not patched.
