# BUSY Bar backend for Capri's status display

Status: spec, not built. Written 2026-11-10 from public docs. Capri ships with the on-screen
display first; this backend plugs into the same `StatusDisplay` interface once a BUSY Bar is on the desk.

## What the TikTok build does

Huw Prosser's assistant "Rose" drives a BUSY Bar (Flipper Devices' desk display) over its local
HTTP API. Capri gets the same: her state (idle, listening, thinking, speaking, error, focus timer)
shows on a physical 72x16 RGB LED panel, with a small mono screen on the back.

## Interface (shared with the on-screen backend)

```python
class StatusDisplay(Protocol):
    def show_state(self, state: str, text: str = "", color: str | None = None) -> None: ...
    def show_text(self, text: str, color: str = "#00FFFFFF", scroll: bool = True, timeout_s: int = 0) -> None: ...
    def show_countdown(self, until_epoch: int, label: str = "") -> None: ...
    def clear(self) -> None: ...
    def health(self) -> dict: ...   # never raises; {"ok": bool, "detail": str}
```

Pick the backend with one env var: `CAPRI_DISPLAY=screen|busybar|both` (default `screen`).
The BUSY backend must never block or crash Capri: 2 s timeouts, failures logged and dropped,
and `both` keeps the on-screen display working when the Bar is offline.

## Connecting

| Path | Base URL | Auth |
|---|---|---|
| USB (Virtual Ethernet) | `http://10.0.4.20/api` | none |
| Wi-Fi (LAN) | `http://<bar-ip>/api` | header `X-API-Token: <password>` |
| Internet (cloud) | `https://api.busy.app/busybar` | `Authorization: Bearer <token>` |

- Wi-Fi API access is off by default. Turn it on once over USB: open `http://10.0.4.20`, Network tab,
  enable "HTTP API access", "Set password and enable". The Bar's IP is under Settings > Wi-Fi > (network) > View IP Address.
- Use the LAN path for Capri. Keep the password in `.env` as `BUSYBAR_TOKEN`, host in `BUSYBAR_HOST`.
  Never the cloud path: it adds an account dependency and sends Capri's state off the network.
- Without a valid token the Bar answers `403 Forbidden`.

## Calls Capri needs

Paths are relative to the base URL. Verified from the official docs: `GET status`, `POST display/draw`
(with a `text` field), `POST assets/upload`. The rest, and every body shape below, come from a
third-party Rust client (busylib 0.0.11 by foresterre, built against API 1.1.1) and must be checked
against the Bar's own reference at `http://10.0.4.20/docs` before shipping (step 1 of the build plan).

| Need | Call |
|---|---|
| Draw text, icons, countdown | `POST display/draw` (JSON, below) |
| Clear Capri's elements | `DELETE display/draw?application_name=capri` |
| Upload an icon/sprite | `POST assets/upload?application_name=capri&file=<name>.png` (raw bytes) |
| Health / status | `GET status`, `GET status/firmware` |
| Brightness | `GET display/brightness`, `POST display/brightness?value=<n>` |
| Focus timer (BUSY mode) | `GET busy/snapshot`, `PUT busy/snapshot` (body unconfirmed) |

### Draw body

```json
{
  "application_name": "capri",
  "priority": 50,
  "elements": [
    {
      "id": "capri-state",
      "type": "text",
      "text": "THINKING",
      "font": "small",
      "color": "#FF2BD6FF",
      "align": "center",
      "display": "front",
      "timeout": 0,
      "scroll_rate": 600
    },
    {
      "id": "capri-face",
      "type": "image",
      "path": "capri_think.png",
      "display": "back",
      "timeout": 0
    }
  ]
}
```

- Element types: `text`, `image`, `animation`, `countdown`, `rectangle`.
- Text is printable ASCII only (bitmap fonts: tiny, small, normal, condensed, bold, large, extra_large).
- Colors are `#RRGGBBAA`. `timeout` 0 means stay until replaced; or `display_until` (epoch, as a string).
- `priority` 1-100. Keep Capri at 50 so system alerts (90) still win.
- Countdown: `{"type":"countdown","timestamp":"<epoch>","direction":"time_left","show_hours":"when_non_zero"}`.
- Back-screen images are 160x80 PNG, max 255 KB. Front images must fit 72x16.
- Re-sending the same `id` replaces that element, so each state change is one POST, no clear needed.

## State map (cyberpunk palette, same on both backends)

| Capri state | Front text | Color | Back image |
|---|---|---|---|
| idle | `CAPRI` (static) | cyan `#00F0FFFF` | `capri_idle.png` |
| listening | `LISTENING` | green `#39FF14FF` | `capri_listen.png` |
| thinking | `THINKING` (scroll) | magenta `#FF2BD6FF` | `capri_think.png` |
| speaking | first ~60 chars of reply, scrolling | white `#FFFFFFFF` | `capri_talk.png` |
| focus | countdown to end | amber `#FFB000FF` | `capri_focus.png` |
| error | `ERR` + short code | red `#FF003CFF` | `capri_error.png` |

Non-ASCII in replies is stripped or transliterated before sending.

## Build plan once a Bar exists

1. Plug in over USB and save the reference: `curl -s http://10.0.4.20/docs -o busybar-docs.html`
   (also check for an OpenAPI file it links). Fix any field name in this spec that differs.
2. Add `capri/display/busybar.py` implementing `StatusDisplay` with `requests` (no extra library needed).
3. One-time asset push: the 6 back images via `assets/upload`.
4. Enable Wi-Fi API access, set `BUSYBAR_HOST` and `BUSYBAR_TOKEN`, then `CAPRI_DISPLAY=both`.
5. Smoke test: `python -m capri.display.busybar --demo` cycles every state for 2 s each, then clears.
6. Tests: a fake HTTP server asserting the exact JSON per state, the 2 s timeout, and that a dead
   Bar never raises into Capri.

## Sources

- Official: https://busy.app/ , https://docs.busy.app/bar/dev/http-api , https://docs.busy.app/bar/dev/api-tokens
- Wire format (third-party, unverified): https://docs.rs/crate/busylib/0.0.11
- Review with price (~$200) and API notes: https://www.matteralpha.com/review/flipper-busy-bar-review
