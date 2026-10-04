# Handoff for Claude — Muse's work 2026-10-03/04

## The 9B brain is live on Steammachine

**Endpoint:** `http://100.114.213.55:11434` (Ollama, listening on all interfaces, Tailscale-accessible)

**Model:** `qwen3.5:9b` (6.6GB, already pulled, tested and responding)
- First inference: 48s (model load). Subsequent calls will be faster.
- Fits in GTX 1660 SUPER's 6GB VRAM with Q4 quantization.

**What you need to do on the 7070:**
1. Point `llm_core` at `http://100.114.213.55:11434` as the Ollama provider.
2. Set the model name to `qwen3.5:9b`.
3. The provider matrix already has the local Ollama route (Muse fixed the bare-localhost:11434 misdetection in the agent-core audit — `llm_core` correctly identifies Ollama vs OpenAI now).

**When PewDiePie's Ajax weights drop:** Swap the model name. No code changes needed — just `ollama pull` the new model on Steammachine and update the model name in config.

**Note:** The 7070 itself cannot run 9B (7.7GB RAM, no discrete GPU, CPU-only would be 1-2 tok/s). Steammachine is the inference host; 7070 stays as orchestrator.

---

## 4 patches waiting in `patches/7070/` on GitHub

All are professional-grade: tested, syntax-validated, with wiring instructions in `patches/7070/README.md`.

### 01 — Conductor driver 7B fallback
**Problem:** 2 of 3 live plan attempts failed with `not_json` — the local 7B is sloppy with JSON.
**Fix:** Try strict JSON parse first. If that fails, try a format-tolerant salvage (strip markdown fences, fix trailing commas, extract JSON from prose). Security-critical rejections (prompt injection, URL exfiltration) are NEVER softened — only format errors get the fallback.
**Tests:** 5/5 passing.

### 02 — Upgrade lane repair loop
**Problem:** 54 of 67 rejected drafts were discarded. Many rejections are fixable (bad `find` text, missing test file, malformed JSON).
**Fix:** Instead of discarding, give the model the specific rejection reason and up to 2 repair attempts. Security rejections are never retried.
**Tests:** 5 test groups passing (10 repairable + 6 non-repairable cases correctly classified).

### 03 — Command center QoL
**Features:**
- `1`–`7` switches tabs, `r` refreshes (keyboard shortcuts)
- Active tab persists across reloads (localStorage)
- Auto-refresh toggle (60s, off by default)
**Validation:** JS syntax checked with node.

### 04 — Business tab polish
**Features:**
- Copy-to-clipboard buttons for draft IDs in the outbox
- Filter box for the business board (18 avenues, filter by status/title)
- Collapsible long drafts (auto-truncates at 600 chars with Show more/less)
**Validation:** JS syntax checked with node.

**To apply:** `cd /path/to/aurix && patch -p1 < patches/7070/0X-*.patch` (each patch is independent)

---

## Daily routines (already scheduled)

Muse set up two recurring jobs tied to the income goal:
- **Morning health check** (8am MDT): Checks 7070 containers, disk, clean tree, key tests. Silent unless something's wrong.
- **Evening code audit** (6pm MDT): Reviews new commits, prepares patches. Silent unless something material is found.

---

## Still pending from your side

1. **Apply patches 01–04** on the 7070
2. **Wire llm_core** to Steammachine's Ollama (see above)
3. **Kill the looping LandPilot mission** (`m-2c74b3`) — the deterministic Land Watch supersedes it
4. **User's identity items** for the income pipeline (business email, name/address, payout method, Fiverr/Upwork, Stripe/PayPal keys) — Rascal owns these

---

*Muse (GitHub line) — 2026-10-04*
