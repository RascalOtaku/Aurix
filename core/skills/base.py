"""Base Skill class and registry."""

import asyncio
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, List
import logging

from .types import SkillCategory, SkillExecutionContext, SkillMetadata

logger = logging.getLogger(__name__)


class Skill(ABC):
    """Base skill class."""

    def __init__(self, metadata: SkillMetadata):
        self.metadata = metadata
        self.execution_history: List[Dict[str, Any]] = []

    @abstractmethod
    async def execute(self, context: SkillExecutionContext) -> Dict[str, Any]:
        """Execute skill with given context."""
        pass

    async def run(
        self,
        agent_id: str,
        inputs: Dict[str, Any],
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        """Run skill with error handling and timeout."""
        context = SkillExecutionContext(
            skill_name=self.metadata.name,
            agent_id=agent_id,
            inputs=inputs,
            timeout=timeout,
        )

        try:
            result = await asyncio.wait_for(
                self.execute(context),
                timeout=timeout
            )
            self.execution_history.append({
                "status": "success",
                "result": result,
                "context": context.to_dict() if hasattr(context, 'to_dict') else {},
            })
            return result
        except asyncio.TimeoutError:
            logger.error(f"Skill {self.metadata.name} timed out")
            raise
        except Exception as e:
            logger.error(f"Skill {self.metadata.name} failed: {e}")
            self.execution_history.append({
                "status": "error",
                "error": str(e),
            })
            raise

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        """Validate input parameters."""
        required = set(self.metadata.inputs.keys())
        provided = set(inputs.keys())
        return required.issubset(provided)


class SkillRegistry:
    """Registry for managing skills (inspired by OpenClaw pattern)."""

    def __init__(self):
        self.skills: Dict[str, Skill] = {}
        self.categories: Dict[SkillCategory, List[str]] = {
            cat: [] for cat in SkillCategory
        }

    def register(self, skill: Skill) -> None:
        """Register skill."""
        self.skills[skill.metadata.name] = skill
        self.categories[skill.metadata.category].append(skill.metadata.name)
        logger.info(f"Registered skill: {skill.metadata.name}")

    def unregister(self, skill_name: str) -> None:
        """Unregister skill."""
        if skill_name in self.skills:
            skill = self.skills.pop(skill_name)
            self.categories[skill.metadata.category].remove(skill_name)
            logger.info(f"Unregistered skill: {skill_name}")

    def get(self, skill_name: str) -> Optional[Skill]:
        """Get skill by name."""
        return self.skills.get(skill_name)

    def get_by_category(self, category: SkillCategory) -> List[Skill]:
        """Get all skills in category."""
        skill_names = self.categories.get(category, [])
        return [self.skills[name] for name in skill_names if name in self.skills]

    def list_all(self) -> List[Dict[str, Any]]:
        """List all registered skills with metadata."""
        return [
            {
                "name": skill.metadata.name,
                "category": skill.metadata.category.value,
                "description": skill.metadata.description,
                "version": skill.metadata.version,
            }
            for skill in self.skills.values()
        ]
