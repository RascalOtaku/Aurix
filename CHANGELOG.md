# Changelog

All notable changes to Aurix are documented here. The version lives in
`core/constants.py` (`APP_VERSION`, served at `/api/version`) and is mirrored
in `pyproject.toml` and `package.json`.

## [Unreleased]

### Added
- **Economic Engine** (`src/foundation/economy/`, spec in `docs/ECONOMIC_ENGINE.md`): the freedom number and milestones,
  an evidence-gated opportunity pipeline (discovered → … → scaled) stored in SQLite, one scorecard with hard limits first,
  owner approval cards before any money, launch or scale-up, a ledger that keeps projected, booked and collected revenue
  apart (with a tax reserve), a forecast-vs-actual learning loop, and a weekly review and monthly close via the outbox.
  Telegram: `freedom`, `opps`, `opp <id>`, `evidence`, `spent`/`booked`/`hours`, `review`, `help economy`.
- Build-pipeline approval cards (`aurix_approval.py`) are delivered to Telegram through the outbox
  (`src/foundation/build_approvals.py`); `yes build <id>` / `no build <id>` resolves them.
- The outbox keeps a held card's buttons: it arrives whole after the morning summary, still one tap.

### Fixed
- `aurix_approval.ApprovalManager` now reads the shared state on disk before its in-memory copy, so a decision made in another
  process (the Telegram listener) reaches a polling pipeline.
- Removed a private hostname and address from comments in `aurix_build_brain.py`.
- `GET /api/health/aggregate` — aggregated degraded-state reporting across
  ChromaDB, SearXNG, email, ntfy, and configured model providers. Each probe is
  hard-capped at 3s; per-service `{status, latency_ms, error, detail}` plus a
  top-level `ok`. Read-only probes only (no notifications published, no IMAP
  logins).
- UI status strip (`static/js/healthStrip.js`) — green/amber/red/grey dot in the
  chat top bar fed by `/api/health/aggregate`, with per-service tooltip,
  click-to-repoll, and 60s polling.
- `static/js/api.js` — Aurix-native centralized frontend API client (no
  external dependencies): base-path-aware URLs, `same-origin` credentials,
  consistent `ApiError` parsing, 401 → login redirect (per-call opt-out),
  per-call timeouts. Concept ported from upstream odysseus PR #4379, minus the
  Axios CDN dependency. Existing `fetch()` call sites migrate incrementally.

### Changed
- Rebrand Odysseus → Aurix across UI, storage keys, and user-facing strings
  (69 files). Browser storage keys migrated `odysseus-*` → `aurix-*` on first
  load via a permanent `migrateLegacyKeys()` shim in `static/js/storage.js`;
  legacy keys are left in place for rollback. Deliberately untouched:
  `ODYSSEUS_*` env vars, compose service name, session cookie, Chroma
  collections, `X-Odysseus-*` headers, and CLI script names.
- Docs rebrand: README, `docs/index.html`, CONTRIBUTING, ROADMAP, SECURITY,
  `setup.py`, `.env.example` now say Aurix (upstream attribution retained).
- `AURIX_FIXES.md` corrected to describe the actual wiring (`is_free_model`
  in `model_discovery.py`/`spend_ledger.py`, MCP discovery in
  `builtin_mcp.py::_start_env_servers`) and the real test location
  (`tests/test_starred_integrations.py`).
- `pyproject.toml` and `package.json` now carry `name = "aurix"` /
  `version = "0.9.1"`, matching `core.constants.APP_VERSION`.

## [0.9.1] — 2026-10-01

- Public release lineage from the Odysseus fork (RascalOtaku/Aurix).
- Model routing helpers (`src/model_router.py`), environment-driven MCP
  discovery (`src/mcp_auto_discover.py`), hardened CI (secret guard, CodeQL,
  Docker build+boot smoke tests, compose validation).
