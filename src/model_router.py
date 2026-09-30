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
