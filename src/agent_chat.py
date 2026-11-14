"""Glue between chat and AURIX's agents (core/agents).

- ``session_llm``: the callable an agent thinks with when chat calls it: the session's own model.
- ``default_agent_llm``: the fallback for agents called outside a chat (AURIX's default endpoint).
- ``agent_sse``: the SSE events /api/chat_stream sends for an agent's reply (the same "delta" /
  "[DONE]" shape the web UI and the Telegram listener already read).
"""

import json
import logging
from typing import AsyncGenerator, Dict, List, Optional

logger = logging.getLogger(__name__)


def session_llm(sess, temperature: Optional[float] = None, max_tokens: Optional[int] = None):
    """An async (messages) -> text callable bound to this chat session's endpoint and model."""
    from src.llm_core import llm_call_async

    async def _call(messages: List[Dict[str, str]]) -> str:
        kwargs = {"headers": getattr(sess, "headers", None)}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        return await llm_call_async(sess.endpoint_url, sess.model, messages, **kwargs)

    return _call


async def default_agent_llm(messages: List[Dict[str, str]]) -> str:
    """Think with AURIX's default endpoint (used when no chat session is bound)."""
    from src.foundation.llm_bridge import default_llm

    system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
    prompt = "\n\n".join(m["content"] for m in messages if m.get("role") != "system")
    return await default_llm(system, prompt, max_tokens=2000)


async def agent_sse(reply: str, saved_id: Optional[str] = None) -> AsyncGenerator[str, None]:
    """Stream an agent's finished reply in the shape chat clients already parse."""
    yield f"data: {json.dumps({'delta': reply})}\n\n"
    if saved_id:
        yield f"data: {json.dumps({'type': 'message_saved', 'id': saved_id})}\n\n"
    yield "data: [DONE]\n\n"
