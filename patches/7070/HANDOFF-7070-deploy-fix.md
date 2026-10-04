# HANDOFF: Fix Interrupted Deploy on 7070

**Date:** 2026-10-04 ~07:45 MDT
**From:** Muse (read-only gate)
**For:** Claude's session (has write access)

## What happened

Claude was running `bash scripts/aurix_deploy.sh` (or similar) on 2026-10-03 around
23:04 MDT. The deploy log (`logs/deploy.log`) shows:

```
2026-10-03T23:04:01-06:00 Saved the running build as rollback-20261003-230401
2026-10-03T23:04:01-06:00 Building and starting the new code...
```

Then nothing. No "DEPLOY OK" line. The deploy was interrupted halfway through —
during the build/start phase. This is why the web UI is down.

## Current state (verified via read-only gate, 2026-10-04 07:45 MDT)

- **All 13 containers report "Up"** but the main app (`odysseus-odysseus-1`) was
  restarted ~4 minutes before the check (likely someone already tried a restart)
- **Web UI not responding** on ports 7000 (app port per docker-compose.yml),
  8000, 8080, 3000, 5000, 9000 — all connection refused
- **Tests pass:** `test_conductor.py` 103 passed, `test_buttons_allowed.py` 5 passed
- **Working tree clean:** 0 uncommitted files
- **HEAD:** `97d4226` — "fix: clear a crashed learner's stale lock before each start"
- **Disk:** 63% (84G free), **Memory:** 7.7G total, ~4.3G available — both fine

## What Claude was working on (from gate access logs)

Before the crash, Claude was reading:
- `src/foundation/upgrades.py` (multiple reads, 00:53 UTC)
- `src/foundation/gaming.py`
- `static/command.html` (extensive reads, 01:01 UTC)
- `scripts/aurix_upgrade_agent.py`

Recent commits suggest an upgrade-lane or command-center update was in progress.

## Pick-up-and-go fix steps

### 1. Check container logs (diagnose)
```bash
docker logs odysseus-odysseus-1 --tail 100
```
Look for: Python tracebacks, import errors, port binding failures, missing
dependencies. The most likely cause is a build that didn't finish — half-written
image or a container started from a broken build.

### 2. Check if the image built correctly
```bash
docker images | grep odysseus
docker ps -a | grep odysseus
```
If the image timestamp is from 23:04 on Oct 3, it's the broken build.

### 3. Rollback (safest first move)
The deploy script saves rollback builds. From the log:
```bash
bash scripts/aurix_deploy.sh rollback
```
This restores `rollback-20261003-230401` (the last known-good build from 22:45,
which reported DEPLOY OK). Verify the web UI responds on port 7000 after rollback.

### 4. If rollback works, re-attempt the deploy cleanly
```bash
bash scripts/aurix_deploy.sh
```
Watch `logs/deploy.log` in real-time to catch where it fails:
```bash
tail -f logs/deploy.log
```

### 5. If rollback doesn't work, check deeper
- `docker-compose.yml` — verify port mappings and volume mounts
- `.env` — check `APP_PORT` (defaults to 7000)
- `data/` directory — check for corrupted state files
- The "crashed learner's stale lock" fix (97d4226) — verify the lock file was
  actually cleared; a stale lock could prevent startup

## Files to check for the interrupted update

Claude was likely modifying one of these when the session died:
- `src/foundation/upgrades.py` — upgrade lane logic
- `static/command.html` — command center UI
- `src/foundation/gaming.py` — game profiles feature

Working tree is clean (0 uncommitted), so either the changes were committed
or they were lost. Check `git log --oneline -5` and `git show HEAD --stat`
to see what the last commit actually changed.

## What NOT to do

- Don't run `git reset --hard` — working tree is clean, nothing to recover there
- Don't prune Docker images/volumes — the rollback images are needed
- Don't modify `.env` unless you've verified the current values are wrong

## After fixing

1. Verify `curl localhost:7000/api/health` returns 200 from inside the 7070
2. Run the test suite: `python -m pytest tests/test_conductor.py tests/test_buttons_allowed.py`
3. Update the deploy log with the fix
4. Report back to Rascal what broke and what fixed it
