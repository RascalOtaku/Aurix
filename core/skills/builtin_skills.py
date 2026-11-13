"""Built-in skills library (inspired by OpenClaw 5k+ skills).

This module provides ready-made skills for common tasks.
Add skills here and register them via SkillRegistry.
"""

import asyncio
import logging
from typing import Any, Callable, Dict, Optional

from core.skills.base import Skill
from core.skills.types import SkillCategory, SkillExecutionContext, SkillMetadata

logger = logging.getLogger(__name__)


class CodeAnalysisSkill(Skill):
    """Analyze code structure and quality."""
    
    async def execute(self, context: SkillExecutionContext) -> Dict[str, Any]:
        code = context.inputs.get("code", "")
        return {
            "lines": len(code.split("\n")),
            "functions": code.count("def "),
            "classes": code.count("class "),
            "imports": code.count("import "),
        }


def _default_web_search(query: str, max_pages: int):
    """The same search chat's web_search tool uses (src/search): returns (text, sources)."""
    from src.search import comprehensive_web_search
    return comprehensive_web_search(query, max_pages=max_pages, return_sources=True)


class WebSearchSkill(Skill):
    """Search the web for information, through AURIX's own search providers."""

    def __init__(self, metadata: SkillMetadata, search_fn: Optional[Callable] = None):
        super().__init__(metadata)
        self._search = search_fn or _default_web_search

    async def execute(self, context: SkillExecutionContext) -> Dict[str, Any]:
        query = (context.inputs.get("query") or "").strip()
        if not query:
            return {"query": query, "summary": "", "results": [], "error": "empty query"}
        max_pages = int(context.inputs.get("max_pages", 3))
        try:
            found = await asyncio.to_thread(self._search, query, max_pages)
        except Exception as e:
            logger.warning("web_search skill failed for %r: %r", query, e)
            return {"query": query, "summary": "", "results": [], "error": str(e)}
        summary, sources = found if isinstance(found, tuple) else (found, [])
        return {"query": query, "summary": summary or "", "results": list(sources or [])}


class DataProcessingSkill(Skill):
    """Process structured data."""
    
    async def execute(self, context: SkillExecutionContext) -> Dict[str, Any]:
        data = context.inputs.get("data", [])
        return {
            "rows": len(data),
            "columns": len(data[0]) if data else 0,
            "processed": True,
        }


class DocumentGenerationSkill(Skill):
    """Generate documents (markdown, reports, etc)."""
    
    async def execute(self, context: SkillExecutionContext) -> Dict[str, Any]:
        template = context.inputs.get("template", "")
        content = context.inputs.get("content", "")
        return {
            "template": template,
            "document_length": len(content),
            "generated": True,
        }


def register_builtin_skills(registry) -> None:
    """Register all built-in skills."""
    
    registry.register(CodeAnalysisSkill(SkillMetadata(
        name="code_analysis",
        category=SkillCategory.CODE,
        description="Analyze code structure and quality metrics",
        version="1.0.0",
        inputs={"code": str},
        outputs={"lines": int, "functions": int, "classes": int},
    )))
    
    registry.register(WebSearchSkill(SkillMetadata(
        name="web_search",
        category=SkillCategory.RESEARCH,
        description="Search the web for information",
        version="1.0.0",
        inputs={"query": str},
        outputs={"summary": str, "results": list},
    )))
    
    registry.register(DataProcessingSkill(SkillMetadata(
        name="data_processing",
        category=SkillCategory.DATA,
        description="Process structured data",
        version="1.0.0",
        inputs={"data": list},
        outputs={"rows": int, "columns": int},
    )))
    
    registry.register(DocumentGenerationSkill(SkillMetadata(
        name="document_generation",
        category=SkillCategory.CODE,
        description="Generate formatted documents",
        version="1.0.0",
        inputs={"template": str, "content": str},
        outputs={"generated": bool},
    )))
    
    logger.info(f"Registered {registry.list_all().__len__()} built-in skills")
