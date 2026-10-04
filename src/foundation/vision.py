"""src/foundation/vision.py - "look at this": a photo you send, or what is on the PC screen, answered by a LOCAL vision model.

The idea of nazirlouis/ada (live camera / screen context for the assistant), kept private the Aurix way: images go to an
Ollama vision model on your own GPU PC, never to a cloud API.

    AURIX_VISION_MODEL  an Ollama vision model, e.g. qwen2.5vl:3b (fits a 6 GB card) or llava:7b; unset = vision is off
    AURIX_VISION_URL    Ollama base URL; default http://<AURIX_GPU_HOST>:11434, else http://localhost:11434

Two ways in:
  - send a photo in Telegram (caption = your question; no caption = "what is this?")
  - `look` / `look: what level am I on?` - the current GamePilot frame. GamePilot only streams while a game or Steam is the
    foreground window on the PC (its own privacy rule), so this never sees your desktop, mail or browser.
If the GPU PC is asleep, a wake-on-LAN packet is sent (wake.py's cooldown applies) and you are told to try again shortly.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from typing import Callable, Optional, Tuple

from src.foundation import audit

MAX_IMAGE_BYTES = 8 * 1024 * 1024
TIMEOUT = 180
FRAME_WAIT_SECONDS = 8.0
FRAME_MAX_AGE = 10.0
SYSTEM = ("You are looking at an image for the owner of this home server. Answer the question directly and briefly; say "
          "plainly when something is unclear or not visible instead of guessing. Never read out passwords, keys or card numbers.")

Post = Callable[[str, dict, int], dict]


def model() -> str:
    return os.environ.get("AURIX_VISION_MODEL", "").strip()


def base_url() -> str:
    url = os.environ.get("AURIX_VISION_URL", "").strip()
    if url:
        return url.rstrip("/")
    host = os.environ.get("AURIX_GPU_HOST", "").strip()
    return f"http://{host}:11434" if host else "http://localhost:11434"


def _post(url: str, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def ask(image: bytes, question: str = "", post: Optional[Post] = None) -> str:
    """The owner-facing answer about one image."""
    post = post or _post
    if not model():
        return ("👁️ Vision is off. Set AURIX_VISION_MODEL in .env to an Ollama vision model on your GPU PC "
                "(e.g. <code>qwen2.5vl:3b</code>, then <code>ollama pull qwen2.5vl:3b</code> there).")
    if not image:
        return "👁️ No image."
    if len(image) > MAX_IMAGE_BYTES:
        return f"👁️ That image is {len(image) // 1_000_000} MB; the limit is {MAX_IMAGE_BYTES // 1_000_000} MB."
    q = " ".join((question or "").split())[:1000] or "What is this? Describe what matters in it."
    body = {"model": model(), "stream": False, "options": {"temperature": 0.2},
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": q, "images": [base64.b64encode(image).decode()]}]}
    t0 = time.time()
    try:
        data = post(base_url() + "/api/chat", body, TIMEOUT)
    except urllib.error.HTTPError as ex:
        if ex.code == 404:
            return f"👁️ The GPU PC does not have <code>{model()}</code> yet: run <code>ollama pull {model()}</code> there."
        return f"👁️ The vision model refused that (HTTP {ex.code})."
    except (urllib.error.URLError, OSError, ValueError) as ex:
        from src.foundation import wake
        if wake.wake_gpu_box(reason="vision"):
            return "👁️ The GPU PC's Ollama is not answering - I sent it a wake-up packet; try again in a minute or two."
        return f"👁️ The GPU PC's Ollama is not answering ({type(ex).__name__})."
    text = str(((data or {}).get("message") or {}).get("content") or "").strip()
    audit.append("vision_asked", model=model(), seconds=round(time.time() - t0, 1), chars=len(text))
    return ("👁️ " + text) if text else "👁️ The vision model gave an empty answer."


def latest_frame(wait: float = FRAME_WAIT_SECONDS, sleep: Callable[[float], None] = time.sleep) -> Tuple[Optional[bytes], str]:
    """A fresh GamePilot frame, asking the PC agent for frames if none is recent. (jpeg, "") or (None, why)."""
    from src.foundation import gamepilot
    gamepilot.note_viewer()                                    # tells the PC agent someone is watching: it starts sending frames
    deadline = time.time() + wait
    while True:
        with gamepilot._lock:
            jpeg, at = gamepilot._frame.get("jpeg") or b"", float(gamepilot._frame.get("at") or 0)
        if jpeg and time.time() - at <= FRAME_MAX_AGE:
            return jpeg, ""
        if time.time() >= deadline:
            return None, ("no fresh frame from the PC: GamePilot only streams while a game or Steam is in front, and the "
                          "PC agent has to be running")
        sleep(0.5)


def look(question: str = "", post: Optional[Post] = None, wait: float = FRAME_WAIT_SECONDS) -> str:
    if not model():
        return ask(b"", question, post)
    jpeg, why = latest_frame(wait)
    if jpeg is None:
        return "👁️ " + why + "."
    return ask(jpeg, question or "What is happening on this screen?", post)


def photo(file_id: str, caption: str = "", post: Optional[Post] = None) -> str:
    """A Telegram photo: download it (the bot's own getFile), ask the local model, keep nothing on disk."""
    import tempfile
    from pathlib import Path
    from src.foundation import transcription
    if not model():
        return ask(b"", caption, post)
    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / "photo.jpg"
        why = transcription._download(file_id, dest)
        if why:
            return "👁️ Could not fetch the photo: " + why + "."
        return ask(dest.read_bytes(), caption, post)
