"""LLM-powered agent implementations.

Every agent thinks with a model and acts only through ``run_gated_action``: its actions go
through the real tool executor and the owner approval gate, never a canned "executed".

- ``ChatLLMAgent`` thinks with the chat session's own model (the callable bound in
  ``core.agents.context``), so it works with whatever endpoint the owner picked: local Ollama,
  OpenAI-compatible or Anthropic. This is the agent chat uses.
- ``ClaudeAgent``, ``OpenAIAgent`` and ``LocalLLMAgent`` call one provider directly.
"""

import logging
import os
from typing import Any, Dict, List, Optional

from core.agents.base import Agent, AgentConfig
from core.agents.context import current_llm, current_owner, current_session_id
from core.agents.execution import run_gated_action

logger = logging.getLogger(__name__)

# Current Claude model; AURIX_CLAUDE_MODEL overrides it.
DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
_GENERIC_CONFIG_MODEL = AgentConfig.__dataclass_fields__["model"].default  # "gpt-4"


def _system_prompt(config: AgentConfig) -> str:
    return config.system_prompt or f"You are {config.name}, a {config.role.value} agent."


class GatedAgent(Agent):
    """Agent whose actions run for real, through the owner approval gate."""

    async def _execute_action(self, action: str, params: Dict[str, Any]) -> Any:
        outcome = await run_gated_action(
            action, params, session_id=current_session_id.get(), owner=current_owner.get())
        self.task_history.append({"action": action, "status": outcome["status"]})
        return outcome


class ChatLLMAgent(GatedAgent):
    """Thinks with the model bound for this call (the chat session's), or a default one."""

    def __init__(self, config: AgentConfig, llm=None):
        super().__init__(config)
        self.llm = llm

    async def _process_task(self, task: str) -> str:
        llm = current_llm.get() or self.llm
        if llm is None:
            raise RuntimeError(f"{self.config.name} has no model to think with")
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": _system_prompt(self.config)},
            {"role": "user", "content": task},
        ]
        reply = await llm(messages)
        self.task_history.append({"task": task[:200], "status": "answered"})
        return (reply or "").strip()


class ClaudeAgent(GatedAgent):
    """Agent powered by the Claude API (async client, current model)."""

    def __init__(self, config: AgentConfig, api_key: Optional[str] = None, client=None):
        super().__init__(config)
        self.api_key = api_key
        self._client = client
        configured = config.model if config.model and config.model != _GENERIC_CONFIG_MODEL else ""
        self.model = configured or os.environ.get("AURIX_CLAUDE_MODEL") or DEFAULT_CLAUDE_MODEL

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.AsyncAnthropic(api_key=self.api_key) if self.api_key \
                else anthropic.AsyncAnthropic()
        return self._client

    async def _process_task(self, task: str) -> str:
        client = self._get_client()
        # No temperature: current Claude models reject sampling parameters. If the model declines,
        # the server retries on its default fallback model inside the same call.
        message = await client.beta.messages.create(
            model=self.model,
            max_tokens=self.config.max_tokens or 16000,
            system=_system_prompt(self.config),
            messages=[{"role": "user", "content": task}],
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if getattr(message, "stop_reason", None) == "refusal":
            raise RuntimeError("Claude declined this task")
        return "".join(
            getattr(block, "text", "") for block in message.content
            if getattr(block, "type", None) == "text"
        ).strip()


class OpenAIAgent(GatedAgent):
    """Agent powered by an OpenAI-compatible API (async client)."""

    def __init__(self, config: AgentConfig, api_key: Optional[str] = None, client=None):
        super().__init__(config)
        self.api_key = api_key
        self._client = client
        self.model = config.model or "gpt-4-turbo"

    async def _process_task(self, task: str) -> str:
        if self._client is None:
            import openai
            self._client = openai.AsyncOpenAI(api_key=self.api_key)
        # The system prompt is a message; chat.completions has no system_prompt argument.
        response = await self._client.chat.completions.create(
            model=self.model,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            messages=[
                {"role": "system", "content": _system_prompt(self.config)},
                {"role": "user", "content": task},
            ],
        )
        return response.choices[0].message.content


class LocalLLMAgent(GatedAgent):
    """Agent powered by a local LLM (Ollama, llama.cpp, ...) on an OpenAI-compatible endpoint."""

    def __init__(self, config: AgentConfig, base_url: str = "http://127.0.0.1:11434/v1"):
        super().__init__(config)
        self.base_url = base_url
        self.model = config.model or "llama2"

    async def _process_task(self, task: str) -> str:
        import httpx

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": _system_prompt(self.config)},
                        {"role": "user", "content": task},
                    ],
                    "temperature": self.config.temperature,
                    "max_tokens": self.config.max_tokens,
                },
            )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
