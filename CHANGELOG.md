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
- Economic Engine 1.1: `econ check` (post-deploy verification incl. a scratch-ledger bookkeeping self-test and a live
  approval round-trip), refunds, net earned per hour of owner time, engineering / commercial / freedom progress shown
  separately, research and competitor evidence must cite a source to open a stage, and `hypothesis` evidence that never counts.
- Economic Engine 1.2, the validation gate: `sample` records a measured sample deliverable (owner and machine minutes, cost,
  sourced findings, open questions, decision value vs free tools); `opp <id> experiment $cap: plan` proposes one capped
  validation experiment as an approval card (only once a sample, a differentiator, an offer and a legal check exist);
  `opp <id> result:` ends it, and no demand points to closing or the next-best opportunity. researched → validated now needs
  all seven checklist items. Hours carry a category (production, acquisition, revision, support, admin). Two variants of the
  property offer (basic screen, ongoing monitoring) are added for comparison.
- Economic Engine 1.3: delivery economics ((revenue − direct costs) ÷ production, revision and support hours) and all-in
  economics ((revenue − all costs) ÷ every hour incl. finding customers and failed pitches) are reported separately; costs carry
  `direct` / `acquisition` (acquisition by default while a demand experiment runs); `engineering` hours (building AURIX) are
  shown but never counted as commercial.
- The web command center's Money tab shows the Economic Engine (read-only): freedom number and milestones, engineering vs commercial
  progress, delivery and all-in $/h, every opportunity with its score, blockers, next test and validation gate, and approvals
  waiting on Telegram. It never creates or seeds the engine and caches between polls.
- LandPilot `land buyer <property>`: a customer-facing buyer's dossier in seven sections that separates confirmed, indicated and
  open findings, names who resolves each open question, flags apparent-but-unrecorded access, states its scope at top and bottom,
  and measures itself for the `sample` record.
- The outbox keeps a held card's buttons: it arrives whole after the morning summary, still one tap.
- **Restore helper** (`scripts/aurix_restore.py`, documented in `docs/OPERATIONS.md`): `list` / `verify` / `restore`
  for the nightly snapshots, stdlib only. Verification opens every SQLite file read-only with `integrity_check`;
  `restore` refuses a corrupt snapshot, previews unless `--apply`, never deletes anything in the destination, and
  with `--overwrite` first copies each replaced file to `<destination>/.aurix-restore-pre/<stamp>/`. Default
  destination is a staging folder next to the snapshots, so the plain command never writes into live data.
  Covered by `tests/test_aurix_restore.py` (20 cases, temp trees only).
- `tests/test_js_syntax.py`: every shipped `static/js/**/*.js` (vendored `lib/` excluded) must parse. It runs
  `node --check` on each module and skips when node is absent — the regression guard for the settings/admin breakage
  documented under Fixed.

### Fixed
- **`static/js/settings.js` and `static/js/admin.js` did not parse** (broken by the 2026-10-02 api.js migration,
  `e02830e`), so the browser refused to evaluate them and every module importing settings.js (chat renderer, model
  picker, slash commands, email library, the admin panel) failed with them. Restored the deleted `try {` in the
  logout handler and the missing `} finally {` around two endpoint-form buttons.
- Deleting one integration in Settings no longer clears **every** integration type: the api.js migration dropped the
  `if (type === 'api') … else if (…)` guards, leaving calendar/contacts/email/MCP/vault calls to run unconditionally.
  The chain is restored, one call per type, as before.
- Telegram digests escape skill names everywhere they appear (`waiting_for_you` and `pulse_digest`), so a name
  containing HTML can no longer break the message; `pulse_digest` had also computed `html.escape` and never used it.
- `tests/test_ada_integrations.py` supplies its own `requests` stub instead of depending on a transitive package
  (caldav now uses `niquests`), so `pip install -r requirements.txt` alone runs the whole suite.
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
- `pyproject.toml` pins `asyncio_default_fixture_loop_scope = "function"`, the behaviour pytest-asyncio currently only
  warns about, so a future release cannot silently change fixture loop scoping.
- Dead code removed where it was doing nothing: an unused `real_len`, `touched`, `provider`, a duplicated `os` import,
  an unused lambda, and f-strings with no placeholders (pyflakes is now clean for `app.py`, `core/`, `src/`, `routes/`,
  `services/`, `mcp_servers/` apart from unused imports).
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
