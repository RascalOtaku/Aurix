"""src/remote_inference.py - model selection when Ollama runs on ANOTHER machine.

The resource governor sizes models from THIS host's RAM/VRAM. That is wrong once
inference is served elsewhere (e.g. the 3431 GPU box over Tailscale): the 7070
has plenty of RAM but no GPU, and the remote box may not have the model the
governor picks at all (it asked a 6GB-VRAM machine for qwen2.5:14b).

For a remote Ollama endpoint we therefore pick only from an explicit allowlist
of the models that machine actually has:

    AURIX_REMOTE_MODELS=qwen2.5:3b,qwen2.5:7b        (any order)

Stdlib only so it is trivially testable.
"""
from __future__ import annotations

import os
import re
from typing import List, Optional
from urllib.parse import urlparse

# Hosts that mean "Ollama on this machine / this compose stack".
LOCAL_HOSTS = {"", "127.0.0.1", "localhost", "::1", "0.0.0.0", "host.docker.internal", "ollama"}

# Below this significance a small/fast model is enough; at or above it (and always
# in agent mode) use the largest model the remote machine has.
LARGE_MODEL_SIGNIFICANCE = 0.35

_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*b\b", re.IGNORECASE)


def is_remote_ollama(endpoint_url: str) -> bool:
    """True for an Ollama-looking endpoint whose host is not this machine."""
    url = (endpoint_url or "").strip().lower()
    if "11434" not in url and "ollama" not in url:
        return False
    host = urlparse(url if "://" in url else f"http://{url}").hostname or ""
    return host not in LOCAL_HOSTS


def _size_b(tag: str) -> float:
    m = _SIZE_RE.search(tag.split(":", 1)[-1] if ":" in tag else tag)
    return float(m.group(1)) if m else 0.0


def remote_models() -> List[str]:
    """Configured remote model tags, smallest first (list order breaks ties)."""
    raw = [t.strip() for t in os.environ.get("AURIX_REMOTE_MODELS", "").split(",") if t.strip()]
    return [t for _, _, t in sorted((_size_b(t), i, t) for i, t in enumerate(raw))]


def pick_remote_model(significance: float, agent_mode: bool) -> Optional[str]:
    """Choose from AURIX_REMOTE_MODELS, or None if it is not configured."""
    models = remote_models()
    if not models:
        return None
    if agent_mode or significance >= LARGE_MODEL_SIGNIFICANCE:
        return models[-1]
    return models[0]
