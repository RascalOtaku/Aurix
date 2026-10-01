"""Modular skills system (inspired by OpenClaw 5k+ skills pattern)."""

from .base import Skill, SkillRegistry
from .types import SkillCategory, SkillExecutionContext

__all__ = [
    "Skill",
    "SkillRegistry",
    "SkillCategory",
    "SkillExecutionContext",
]
