"""Per-call context for agents: which model to think with, and whose session they act for.

Agents are registered once at startup and shared by every chat session, so anything that differs
per request (the session's own model endpoint, the session id the approval gate keys on, the
owner) travels in context variables. They are task-local, so two chats running at once never
see each other's values.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Awaitable, Callable, Dict, List, Optional

# async (messages) -> reply text. The same callable chat uses for the session's model.
LLMCallable = Callable[[List[Dict[str, str]]], Awaitable[str]]

current_llm: ContextVar[Optional[LLMCallable]] = ContextVar("aurix_agent_llm", default=None)
current_session_id: ContextVar[Optional[str]] = ContextVar("aurix_agent_session", default=None)
current_owner: ContextVar[Optional[str]] = ContextVar("aurix_agent_owner", default=None)


@contextmanager
def agent_context(llm: Optional[LLMCallable] = None, session_id: Optional[str] = None,
                  owner: Optional[str] = None):
    """Bind the model, session and owner for agent calls made inside this block."""
    tokens = (current_llm.set(llm), current_session_id.set(session_id), current_owner.set(owner))
    try:
        yield
    finally:
        current_owner.reset(tokens[2])
        current_session_id.reset(tokens[1])
        current_llm.reset(tokens[0])
