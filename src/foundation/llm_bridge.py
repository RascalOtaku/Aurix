"""src/foundation/llm_bridge.py - the planner's model call, through the app's own routing.

Uses the configured default endpoint (the GPU box) with the configured fallback
chain (the CPU box's local CPU model), so planning degrades to slow-but-alive when the
gaming PC is off. Imports are lazy so the rest of the Foundation stays stdlib-only and
unit-testable.
"""
from __future__ import annotations


async def default_llm(system: str, prompt: str, max_tokens: int = 1500, temperature: float = 0.2) -> str:
    from src.endpoint_resolver import resolve_chat_fallback_candidates, resolve_endpoint
    from src.llm_core import llm_call_async_with_fallback

    url, model, headers = resolve_endpoint("default")
    candidates = [(url, model, headers)] + resolve_chat_fallback_candidates()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    return await llm_call_async_with_fallback(candidates, messages, temperature=temperature, max_tokens=max_tokens)
