"""LLM-powered agent implementations using Claude, OpenAI, or local models."""

import asyncio
from typing import Any, Dict, Optional
import logging

from core.agents.base import Agent, AgentConfig
from core.agents.types import AgentRole

logger = logging.getLogger(__name__)


class ClaudeAgent(Agent):
    """Agent powered by Claude API."""
    
    def __init__(self, config: AgentConfig, api_key: str = None):
        super().__init__(config)
        self.api_key = api_key
        self.model = config.model or "claude-3-5-sonnet-20241022"
    
    async def _process_task(self, task: str) -> str:
        """Process task using Claude API."""
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=self.api_key)
            
            message = client.messages.create(
                model=self.model,
                max_tokens=self.config.max_tokens,
                temperature=self.config.temperature,
                system=self.config.system_prompt or f"You are {self.config.name}, a {self.config.role.value} agent.",
                messages=[
                    {"role": "user", "content": task}
                ]
            )
            
            return message.content[0].text
        except Exception as e:
            logger.error(f"Claude API error: {e}")
            raise
    
    async def _execute_action(self, action: str, params: Dict[str, Any]) -> Any:
        """Execute action (tool use)."""
        # TODO: Implement tool use via Claude API
        return {"action": action, "params": params, "status": "not_implemented"}


class OpenAIAgent(Agent):
    """Agent powered by OpenAI API."""
    
    def __init__(self, config: AgentConfig, api_key: str = None):
        super().__init__(config)
        self.api_key = api_key
        self.model = config.model or "gpt-4-turbo"
    
    async def _process_task(self, task: str) -> str:
        """Process task using OpenAI API."""
        try:
            import openai
            client = openai.OpenAI(api_key=self.api_key)
            
            response = client.chat.completions.create(
                model=self.model,
                max_tokens=self.config.max_tokens,
                temperature=self.config.temperature,
                system_prompt=self.config.system_prompt or f"You are {self.config.name}, a {self.config.role.value} agent.",
                messages=[
                    {"role": "user", "content": task}
                ]
            )
            
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"OpenAI API error: {e}")
            raise
    
    async def _execute_action(self, action: str, params: Dict[str, Any]) -> Any:
        """Execute action (tool use)."""
        # TODO: Implement function calling via OpenAI API
        return {"action": action, "params": params, "status": "not_implemented"}


class LocalLLMAgent(Agent):
    """Agent powered by local LLM (Ollama, llama.cpp, etc)."""
    
    def __init__(
        self,
        config: AgentConfig,
        base_url: str = "http://127.0.0.1:11434/v1"
    ):
        super().__init__(config)
        self.base_url = base_url
        self.model = config.model or "llama2"
    
    async def _process_task(self, task: str) -> str:
        """Process task using local LLM."""
        try:
            import httpx
            
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": self.config.system_prompt or f"You are {self.config.name}"},
                            {"role": "user", "content": task}
                        ],
                        "temperature": self.config.temperature,
                        "max_tokens": self.config.max_tokens,
                    }
                )
            
            result = response.json()
            return result["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error(f"Local LLM error: {e}")
            raise
    
    async def _execute_action(self, action: str, params: Dict[str, Any]) -> Any:
        """Execute action."""
        return {"action": action, "params": params, "status": "executed"}
