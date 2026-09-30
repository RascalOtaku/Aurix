"""Model-routing helpers inspired by multi-agent orchestration patterns.

These helpers are deliberately lightweight and side-effect-free so they can be
used in the existing Aurix agent loop and task scheduler without replacing the
current endpoint model.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional


MODEL_ROUTER_PROFILES = {
    "coding": {
        "preferred": ["gpt-4.1", "claude-sonnet-4", "o3", "gpt-5.2"],
        "fallback": ["gpt-4o-mini", "deepseek-chat", "llama-4-scout-17b-16e-instruct"],
    },
    "research": {
        "preferred": ["o3", "deepseek-reasoner", "claude-sonnet-4", "gpt-5.2-pro"],
        "fallback": ["gpt-4.1", "gpt-4o-mini"],
    },
    "utility": {
        "preferred": ["gpt-4o-mini", "claude-haiku-4", "gpt-4.1-mini"],
        "fallback": ["gpt-4o", "deepseek-chat"],
    },
    "vision": {
        "preferred": ["gpt-4o", "gemini-2.5-pro", "claude-sonnet-4"],
        "fallback": ["gpt-4o-mini", "grok-4.3"],
    },
    "default": {
        "preferred": ["gpt-4o-mini", "gpt-4o", "claude-sonnet-4"],
        "fallback": ["deepseek-chat", "llama-3.3-70b-versatile"],
    },
}


# Zero-cost routes, per the free coding agents this borrows from (freebuff, coding-agent-free,
# codex-model-router): OpenRouter's `:free` variants and its `openrouter/free` auto-router, and OpenCode
# Zen's free tier (ids carrying `-free`, plus `big-pickle`). Matching is by marker, not a fixed list,
# because the free line-ups change weekly.
FREE_MODEL_MARKERS = (":free", "-free", "openrouter/free", "big-pickle")

# Within the free pool, models known to handle tool calls and code well come first for coding work.
FREE_CODING_HINTS = ("coder", "code", "qwen3", "deepseek", "gpt-oss", "kimi", "glm", "nemotron", "devstral")

STRATEGIES = ("balanced", "free")


def is_free_model(model: Any) -> bool:
    name = str(model or "").strip().lower()
    return any(marker in name for marker in FREE_MODEL_MARKERS)


def _normalise_model_name(model: Any) -> str:
    return (str(model or "")).strip()


def _coerce_models(models: Optional[Iterable[Any]]) -> List[str]:
    if not models:
        return []
    out = []
    for model in models:
        value = _normalise_model_name(model)
        if value:
            out.append(value)
    return out


def infer_task_kind(prompt: str) -> str:
    text = (prompt or "").lower()
    if any(k in text for k in ["fix", "bug", "patch", "implement", "code", "refactor", "debug", "error"]):
        return "coding"
    if any(k in text for k in ["research", "analyze", "compare", "summarize", "find", "market", "pricing"]):
        return "research"
    if any(k in text for k in ["image", "vision", "ocr", "describe photo", "analyze screenshot"]):
        return "vision"
    if any(k in text for k in ["short", "summarize", "title", "classify", "name", "tag"]):
        return "utility"
    return "default"


def choose_model_for_task(prompt: str, available_models: Optional[Iterable[Any]] = None, strategy: str = "balanced") -> str:
    """Choose the best model for a task using a simple cost/quality profile.

    This intentionally mirrors the routing patterns used in multi-agent tools:
    code work prefers a stronger coding model; research prefers reasoning models;
    cheap utility tasks use smaller models.
    """
    models = _coerce_models(available_models)
    if not models:
        return ""

    kind = infer_task_kind(prompt)
    if strategy == "free":
        free = [m for m in models if is_free_model(m)]
        if free:
            return _pick_free(free, kind)
        # No free model on offer: fall through to the normal profile rather than failing the task.

    profile = MODEL_ROUTER_PROFILES.get(kind, MODEL_ROUTER_PROFILES["default"])
    preferred = list(profile.get("preferred", []))
    fallback = list(profile.get("fallback", []))

    # Prefer explicitly named best-fit models, but keep all others as fallbacks.
    for candidate in preferred + fallback:
        for model in models:
            if model == candidate or model.startswith(candidate):
                return model

    # If the model list is custom, prefer the first compatible chat model.
    for model in models:
        if not any(token in model.lower() for token in ["embedding", "tts", "whisper", "dall-e", "moderation"]):
            return model
    return models[0]


def _pick_free(free: List[str], kind: str) -> str:
    if kind == "coding":
        for hint in FREE_CODING_HINTS:
            for model in free:
                if hint in model.lower():
                    return model
    # The OpenRouter auto-router spreads load across whatever free models are up right now.
    for model in free:
        if model.lower() == "openrouter/free":
            return model
    return free[0]


def free_fallback_chain(available_models: Optional[Iterable[Any]] = None, prompt: str = "") -> List[str]:
    """Every free model, best first for the task: the order to walk when one returns 429."""
    models = [m for m in _coerce_models(available_models) if is_free_model(m)]
    if not models:
        return []
    first = _pick_free(models, infer_task_kind(prompt))
    return [first] + [m for m in models if m != first]


def route_model_for_role(role: str, available_models: Optional[Iterable[Any]] = None) -> str:
    model = choose_model_for_task(f"{role} task", available_models=available_models, strategy="balanced")
    return model


def model_router_payload(prompt: str, available_models: Optional[Iterable[Any]] = None, strategy: str = "balanced") -> Dict[str, Any]:
    """Return a routing descriptor that callers can persist or display.

    Shape is intentionally simple: route_kind + chosen_model + available_models.
    """
    models = _coerce_models(available_models)
    kind = infer_task_kind(prompt)
    chosen = choose_model_for_task(prompt, models, strategy=strategy)
    return {
        "task_kind": kind,
        "strategy": strategy,
        "selected_model": chosen,
        "available_models": models,
    }
