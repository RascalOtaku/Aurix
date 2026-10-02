# Changelog

All notable changes to Aurix are documented here. The version lives in
`core/constants.py` (`APP_VERSION`, served at `/api/version`) and is mirrored
in `pyproject.toml` and `package.json`.

## [Unreleased]

### Added
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
