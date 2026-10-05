"""src/startup_optimizer.py - hardware-aware resource mode, used by app.py's startup_event() to decide whether to
skip the heavier warmup steps (tool-index embedding, etc.) on a memory-constrained box.

Trimmed down from a larger speculative draft (a generic lazy-loading task-orchestrator) that duplicated what
app.py's real startup_event() already does, successfully, in production: MCP connections, tool-index warmup,
endpoint warmup are all already real, working, fire-and-forget background tasks there. Re-implementing them
behind a second abstraction added no value and risked diverging from the tested path. The one genuinely new,
non-redundant idea was resource detection, so that is what stayed.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def detect_resource_mode() -> str:
    """'ultra-low' (<4GB RAM) | 'low' (4-8GB) | 'normal' (8-16GB) | 'gpu' (>=16GB with a local CUDA GPU) |
    'high' (>=16GB, no local GPU). Never raises: psutil is a hard dependency (requirements.txt) but a local GPU
    is not expected on this container - the real GPU lives on a separate machine, reached over the network."""
    try:
        import psutil

        total_mem_gb = psutil.virtual_memory().total / (1024 ** 3)
    except Exception:
        # Contract is "never raises": a missing/broken psutil must not take down startup.
        logger.warning("psutil unavailable; defaulting resource mode to 'normal'")
        return "normal"

    has_gpu = False
    try:
        import torch
        has_gpu = torch.cuda.is_available()
    except Exception:
        pass

    if total_mem_gb < 4:
        mode = "ultra-low"
    elif total_mem_gb < 8:
        mode = "low"
    elif total_mem_gb < 16:
        mode = "normal"
    elif has_gpu:
        mode = "gpu"
    else:
        mode = "high"

    logger.info(f"Resource mode: {mode} (RAM: {total_mem_gb:.1f}GB, GPU: {has_gpu})")
    return mode
