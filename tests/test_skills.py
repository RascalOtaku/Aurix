"""Tests for skills system."""

import pytest
from core.skills import Skill, SkillRegistry
from core.skills.types import SkillCategory, SkillMetadata, SkillExecutionContext


class TestSkill(Skill):
    """Test skill implementation."""

    async def execute(self, context: SkillExecutionContext):
        return {"status": "success", "input": context.inputs}


def test_skill_registry():
    registry = SkillRegistry()
    
    metadata = SkillMetadata(
        name="test_skill",
        category=SkillCategory.CODE,
        description="Test skill",
    )
    skill = TestSkill(metadata)
    
    registry.register(skill)
    assert "test_skill" in registry.skills
    
    retrieved = registry.get("test_skill")
    assert retrieved is not None
    assert retrieved.metadata.name == "test_skill"


def test_skill_category_filter():
    registry = SkillRegistry()
    
    code_metadata = SkillMetadata(
        name="code_skill",
        category=SkillCategory.CODE,
        description="Code skill",
    )
    data_metadata = SkillMetadata(
        name="data_skill",
        category=SkillCategory.DATA,
        description="Data skill",
    )
    
    registry.register(TestSkill(code_metadata))
    registry.register(TestSkill(data_metadata))
    
    code_skills = registry.get_by_category(SkillCategory.CODE)
    assert len(code_skills) == 1
    assert code_skills[0].metadata.name == "code_skill"
