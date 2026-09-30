# Starred repositories → Aurix

What each of the owner's starred GitHub repositories (37 as of 2026-09-30) means for Aurix, and where it is wired in.
Everything new is **opt-in**: nothing below starts, downloads or calls out until you turn it on in `.env`.

## Wired in

| Starred repo | What Aurix does with it | Where | Turn it on |
|---|---|---|---|
| [DrOetker747/codex-model-router](https://github.com/DrOetker747/codex-model-router) | Multi-key quota rotation: on 401/402/429 the key cools down for 10 min and the next key is used | `src/key_rotation.py`, hooked into every call in `src/llm_core.py` | `OPENROUTER_API_KEYS=k1,k2` / `OPENCODE_ZEN_API_KEYS=…` |
| same + OpenCode Zen | OpenCode Zen as a provider: detected by URL, listed in model discovery with free models first | `src/llm_core.py`, `src/model_discovery.py`, `routes/model_routes.py` | `OPENCODE_ZEN_API_KEY`, endpoint `https://opencode.ai/zen/v1` |
| [CodebuffAI/freebuff](https://github.com/CodebuffAI/freebuff), [maz557/coding-agent-free](https://github.com/maz557/coding-agent-free) | `strategy="free"` in the model router: only `:free` / `-free` / `openrouter/free` / `big-pickle` models, coding-capable ones first for code; `free_fallback_chain()` gives the order to walk on 429 | `src/model_router.py` | pass `strategy="free"` |
| [ahujasid/mcp-for-blender](https://github.com/ahujasid/mcp-for-blender) | MCP preset `blender` (`uvx mcp-for-blender`), safe mode **on** and telemetry off by default | `src/mcp_presets.py` | `AURIX_MCP_PRESETS=blender` |
| [mksglu/context-mode](https://github.com/mksglu/context-mode) | MCP preset `context-mode` (`npx -y context-mode`): sandboxes/indexes bulky tool output | `src/mcp_presets.py` | `AURIX_MCP_PRESETS=context-mode` |
| — | `MCP_SERVERS` / `MCP_SERVER_URL` auto-discovery (already written, previously never called) now connects at startup | `src/builtin_mcp.py` → `src/mcp_auto_discover.py` | `MCP_SERVERS='[…]'` |
| [tt-a1i/archify](https://github.com/tt-a1i/archify) | Skill `archify` + new `diagrams` domain pack (model first, then render, then verify) | `src/foundation/skills.py`, `src/foundation/capabilities.py` | automatic for "architecture/sequence/… diagram" goals |
| [ayghri/i-have-adhd](https://github.com/ayghri/i-have-adhd) | Skill `adhd`: answer-first replies | `src/foundation/skills.py`, `src/agent_loop.py` | `AURIX_OUTPUT_STYLE=adhd` |
| [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail) | Already wired: coding rules for `software_dev` / `self_improve` | `src/foundation/skills.py` | automatic |
| [heygen-com/hyperframes](https://github.com/heygen-com/hyperframes) | Capability `hyperframes` (HTML → MP4); the social-content pack's asset step suggests it | `src/foundation/capabilities.py` | `npm install -g hyperframes` |
| [alibaba/open-code-review](https://github.com/alibaba/open-code-review) | Capability `open-code-review` (`ocr`); the software-dev review step runs it when installed | `src/foundation/capabilities.py` | `npm install -g @alibaba-group/open-code-review` |
| [gtsteffaniak/filebrowser](https://github.com/gtsteffaniak/filebrowser) | Compose service `filebrowser`: browse the AURIX brain, **read-only** by default; dashboard link | `docker-compose.yml`, `routes/command_routes.py`, `static/command.html` | `COMPOSE_PROFILES=extras` |
| [averygan/reclip](https://github.com/averygan/reclip) | Compose service `reclip`, built from a pinned commit **with an option-injection fix** (URLs are passed after `--`, http(s) only); dashboard link | `reclip/Dockerfile`, `docker-compose.yml` | `COMPOSE_PROFILES=extras` |
| [bilawalsidhu/gods-eye-view](https://github.com/bilawalsidhu/gods-eye-view) | Already wired: isolated container + dashboard link | `gods_eye/` | — |
| [OpenHands/OpenHands](https://github.com/OpenHands/OpenHands) | Already wired: coding executor inside the mission sandbox | `src/foundation/openhands.py`, `mission_sandbox/` | — |
| [odysseus-dev/odysseus](https://github.com/odysseus-dev/odysseus) | Aurix's upstream | — | — |

## Overlapping projects: the best idea from each, added

| Starred repo | Idea taken | Where | Turn it on |
|---|---|---|---|
| [paperclipai/paperclip](https://github.com/paperclipai/paperclip), [chaitanyagiri/munder-difflin](https://github.com/chaitanyagiri/munder-difflin) | Durable spend ledger + monthly circuit breaker. Paid (hosted, non-free) tokens are recorded per model; at the cap, paid calls fail fast with 402 and the existing fallback chains carry on with local/free models. "Model spend" card on the dashboard | `src/spend_ledger.py`, `src/llm_core.py`, `static/command.html` | `AURIX_MONTHLY_PAID_TOKEN_CAP=2000000` (ledger always on) |
| [chaitanyagiri/munder-difflin](https://github.com/chaitanyagiri/munder-difflin) | steer → constrain → stop ladder: a looping agent is first told to try something different (2 useless rounds), then forced to answer (4) | `src/agent_loop.py` | automatic |
| [affaan-m/ECC](https://github.com/affaan-m/ECC) (AgentShield), clawdefender / agent-hardening from [awesome-openclaw-skills](https://github.com/VoltAgent/awesome-openclaw-skills) | Static safety scan of skills and MCP configs: prompt-injection, pipe-to-shell, secret exfiltration, hidden Unicode, attacks on the approval gate/audit. High findings refuse the skill save / MCP start / absorbed checkout | `src/agent_shield.py`, `routes/skills_routes.py`, `src/builtin_mcp.py`, `src/foundation/skills.py` | automatic |
| [VectifyAI/PageIndex](https://github.com/VectifyAI/PageIndex) | Vectorless, reasoning-based retrieval: the wiki's own folder/heading tree is the index. `wiki_outline` gives a ranked table of contents with node ids, `wiki_read` returns exactly one section | `src/page_index.py`, `mcp_servers/rag_server.py` | automatic (built-in RAG MCP server); `AURIX_PAGE_INDEX_DIRS` to change roots |

Already covered by Aurix, so nothing new was needed: Paperclip/OpenClaw heartbeats (standing missions + scheduled
tasks), approval gates and immutable audit (approval gate + hash-chained audit), OpenClaw DM pairing (Telegram
already answers only `TELEGRAM_CHAT_ID`, stricter than pairing), ECC instincts (teacher lessons), Firstmate
worktree crews (OpenHands missions in the sandbox), Pixel Agents' live activity (dashboard "running now").

## From the lists

| List | What was taken | Where |
|---|---|---|
| [public-apis/public-apis](https://github.com/public-apis/public-apis) | 14 keyless HTTPS APIs as one-click Integration presets (URL pre-filled): Open-Meteo weather + geocoding, Nager.Date holidays, Frankfurter FX, CoinGecko, USGS earthquakes, Sunrise-Sunset, OpenAlex, arXiv, Semantic Scholar, Wikipedia, Free Dictionary, Open Library, Hacker News | `src/public_api_presets.py` (Settings → Integrations) |
| [VoltAgent/awesome-openclaw-skills](https://github.com/VoltAgent/awesome-openclaw-skills) | The free research skills (OpenAlex/arXiv paper digests) became the presets above; `adguard` became an AdGuard Home preset; `clawdefender`/`agent-hardening` informed the agent shield. The rest target macOS apps or paid SaaS | `src/public_api_presets.py`, `src/agent_shield.py` |
| [sindresorhus/awesome](https://github.com/sindresorhus/awesome) | Pointers worth mining next: [awesome-selfhosted](https://github.com/awesome-selfhosted/awesome-selfhosted) (more compose extras), [awesome-home-assistant](https://github.com/frenck/awesome-home-assistant), [awesome-security](https://github.com/sbilly/awesome-security), [awesome-fastapi](https://github.com/mjhea0/awesome-fastapi) | — |
| [rohitg00/ai-engineering-from-scratch](https://github.com/rohitg00/ai-engineering-from-scratch), [AMAI-GmbH/AI-Expert-Roadmap](https://github.com/AMAI-GmbH/AI-Expert-Roadmap), [practical-tutorials/project-based-learning](https://github.com/practical-tutorials/project-based-learning), [MunGell/awesome-for-beginners](https://github.com/MunGell/awesome-for-beginners) | Learning material, no code to wire. Feed individual lessons to the reading library with `learn: <link>` (the MCP track, phase 13, and agent engineering, phase 14, of ai-engineering-from-scratch map directly onto Aurix) | reading library |

## Not wired (and why)

| Starred repo | Reason |
|---|---|
| [NVIDIA/OpenShell](https://github.com/NVIDIA/OpenShell) | A sandbox runtime. Aurix already has an isolated mission sandbox with its own gate; swapping runtimes is a design decision, not a wiring job. |
| [openclaw/openclaw](https://github.com/openclaw/openclaw), [VoltAgent/awesome-openclaw-skills](https://github.com/VoltAgent/awesome-openclaw-skills), [affaan-m/ECC](https://github.com/affaan-m/ECC) | Skill collections for other agent harnesses. Aurix stores skills as `SKILL.md` (Skills UI: add a skill, paste its markdown, then run its test/audit); pick individual skills that way rather than bulk-importing thousands of unreviewed prompts. |
| [kunchenguid/firstmate](https://github.com/kunchenguid/firstmate), [pixel-agents-hq/pixel-agents](https://github.com/pixel-agents-hq/pixel-agents) | Their core ideas are already in Aurix (see above); Pixel Agents' pixel-art office is a VS Code/desktop view of Claude Code sessions, not something the server can host. |
| [oblien/openship](https://github.com/oblien/openship) | Deployment platform; Aurix deploys with compose + `scripts/aurix_deploy.sh`. |
| [debpalash/VoiceStudio](https://github.com/debpalash/VoiceStudio) | Heavy GPU app; no stable HTTP API to point `services/tts` at yet. |
| [anthropics/financial-services](https://github.com/anthropics/financial-services) | Claude-specific plugins; the trading pack stays paper-only by design. |
| [nazirlouis/ada](https://github.com/nazirlouis/ada), [hwx3z/Jarvis-by-Nazlouis](https://github.com/hwx3z/Jarvis-by-Nazlouis) | Standalone voice assistants; Aurix already has STT/TTS services. |
| [InkboxSoftware/pokemonMiniAmbulation](https://github.com/InkboxSoftware/pokemonMiniAmbulation), [InkboxSoftware/tronForDOS](https://github.com/InkboxSoftware/tronForDOS) | Retro assembly games. |
