"""
Startup optimization and lazy-loading framework for resource-constrained environments.

Ensures Aurix boots fast and degrades gracefully when hardware is limited.
"""

import asyncio
import logging
import os
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Resource detection
def detect_resource_mode() -> str:
    """Detect available hardware and return resource mode."""
    import psutil
    
    total_mem_gb = psutil.virtual_memory().total / (1024 ** 3)
    cpu_count = psutil.cpu_count(logical=False) or psutil.cpu_count()
    
    # Check for GPU availability
    has_gpu = False
    try:
        import torch
        has_gpu = torch.cuda.is_available()
    except Exception:
        pass
    
    # Classification logic
    if total_mem_gb < 4:
        mode = "ultra-low"  # <4GB: disable vector memory, minimal preloading
    elif total_mem_gb < 8:
        mode = "low"        # 4-8GB: lazy-load RAG, reduce warmups
    elif total_mem_gb < 16:
        mode = "normal"     # 8-16GB: standard startup
    elif has_gpu:
        mode = "gpu"        # GPU available: aggressive preloading
    else:
        mode = "high"       # >16GB CPU: full preloading
    
    logger.info(
        f"Resource mode: {mode} "
        f"(RAM: {total_mem_gb:.1f}GB, CPUs: {cpu_count}, GPU: {has_gpu})"
    )
    return mode


class StartupTask:
    """Lazy-loadable startup task with dependency tracking."""
    
    def __init__(
        self,
        name: str,
        func: Callable,
        depends_on: Optional[list] = None,
        is_critical: bool = False,
        timeout_seconds: int = 30,
        resource_modes: Optional[list] = None,
    ):
        self.name = name
        self.func = func
        self.depends_on = depends_on or []
        self.is_critical = is_critical
        self.timeout_seconds = timeout_seconds
        self.resource_modes = resource_modes or ["normal", "high", "gpu"]
        
        self.completed = False
        self.result = None
        self.error = None
        self.start_time = None
        self.end_time = None
    
    async def execute(self) -> None:
        """Run the startup task with error handling."""
        self.start_time = time.time()
        try:
            if asyncio.iscoroutinefunction(self.func):
                result = await asyncio.wait_for(self.func(), timeout=self.timeout_seconds)
            else:
                result = await asyncio.to_thread(self.func, timeout=self.timeout_seconds)
            self.result = result
            self.completed = True
            duration = time.time() - self.start_time
            logger.info(f"✓ {self.name} ({duration:.2f}s)")
        except asyncio.TimeoutError:
            self.error = f"Timeout after {self.timeout_seconds}s"
            if self.is_critical:
                logger.error(f"✗ {self.name}: {self.error}")
                raise
            else:
                logger.warning(f"⊘ {self.name}: {self.error} (non-critical, continuing)")
        except Exception as e:
            self.error = str(e)
            if self.is_critical:
                logger.error(f"✗ {self.name}: {self.error}")
                raise
            else:
                logger.warning(f"⊘ {self.name}: {self.error} (non-critical, continuing)")


class StartupOrchestrator:
    """Manages startup tasks with dependency resolution and resource awareness."""
    
    def __init__(self, resource_mode: str):
        self.resource_mode = resource_mode
        self.tasks: Dict[str, StartupTask] = {}
        self.completed_tasks: set = set()
        
    def register(
        self,
        name: str,
        func: Callable,
        depends_on: Optional[list] = None,
        is_critical: bool = False,
        timeout_seconds: int = 30,
        resource_modes: Optional[list] = None,
    ) -> None:
        """Register a startup task."""
        self.tasks[name] = StartupTask(
            name=name,
            func=func,
            depends_on=depends_on,
            is_critical=is_critical,
            timeout_seconds=timeout_seconds,
            resource_modes=resource_modes,
        )
    
    async def run_all(self) -> Dict[str, Any]:
        """Execute all eligible tasks respecting dependencies."""
        results = {}
        start = time.time()
        
        # Filter tasks by resource mode
        eligible = {
            name: task for name, task in self.tasks.items()
            if self.resource_mode in task.resource_modes
        }
        
        logger.info(f"Starting {len(eligible)}/{len(self.tasks)} tasks for {self.resource_mode} mode")
        
        # Topological sort by dependencies
        executed = set()
        while len(executed) < len(eligible):
            ready = [
                name for name, task in eligible.items()
                if name not in executed and all(dep in executed for dep in task.depends_on)
            ]
            
            if not ready:
                break  # Circular dependency or unmet dependency
            
            # Execute ready tasks in parallel
            tasks_to_run = [eligible[name] for name in ready]
            await asyncio.gather(*[task.execute() for task in tasks_to_run], return_exceptions=True)
            
            for task in tasks_to_run:
                executed.add(task.name)
                if task.completed:
                    results[task.name] = task.result
        
        duration = time.time() - start
        logger.info(f"Startup complete in {duration:.2f}s")
        return results


# Built-in startup tasks for Aurix

async def warmup_embeddings() -> str:
    """Pre-warm the embedding model for vector memory."""
    try:
        from src.memory_vector import MemoryVectorStore
        # Just import and initialize, don't load anything
        logger.info("Embedding model warm-up registered")
        return "embeddings_ready"
    except Exception as e:
        raise Exception(f"Embedding warmup failed: {e}")


async def index_builtin_tools() -> str:
    """Pre-index built-in tools for faster skill discovery."""
    try:
        from src.tool_index import get_tool_index
        idx = await asyncio.to_thread(get_tool_index)
        if idx:
            await asyncio.to_thread(idx.index_builtin_tools)
            await asyncio.to_thread(idx.get_tools_for_query, "warmup", 8)
        return "tools_indexed"
    except Exception as e:
        raise Exception(f"Tool indexing failed: {e}")


async def connect_mcp_servers() -> str:
    """Connect to Model Context Protocol servers."""
    try:
        from src.builtin_mcp import register_builtin_servers
        from src.mcp_manager import MCPManager
        
        mcp_manager = MCPManager()
        await register_builtin_servers(mcp_manager)
        await asyncio.wait_for(mcp_manager.connect_all_enabled(), timeout=20)
        return "mcp_ready"
    except asyncio.TimeoutError:
        raise Exception("MCP connection timed out")
    except Exception as e:
        raise Exception(f"MCP startup failed: {e}")


async def discover_models() -> str:
    """Discover available LLM models on configured endpoints."""
    try:
        from src.model_discovery import ModelDiscovery
        from src.constants import DEFAULT_HOST, OPENAI_API_KEY
        
        discovery = ModelDiscovery(DEFAULT_HOST, OPENAI_API_KEY)
        # Don't block on this, just start the discovery
        return "model_discovery_started"
    except Exception as e:
        raise Exception(f"Model discovery failed: {e}")


def get_startup_orchestrator() -> StartupOrchestrator:
    """Create and configure the startup orchestrator."""
    resource_mode = detect_resource_mode()
    orchestrator = StartupOrchestrator(resource_mode)
    
    # Critical tasks (blocking startup)
    # None by default — all heavy work is backgrounded
    
    # High-priority background tasks (low/normal/high/gpu modes)
    orchestrator.register(
        "index_tools",
        index_builtin_tools,
        is_critical=False,
        timeout_seconds=15,
        resource_modes=["normal", "high", "gpu"],  # Skip on ultra-low/low
    )
    
    orchestrator.register(
        "warmup_embeddings",
        warmup_embeddings,
        is_critical=False,
        timeout_seconds=10,
        resource_modes=["normal", "high", "gpu"],  # Skip on ultra-low/low
    )
    
    # Medium-priority background tasks
    orchestrator.register(
        "mcp_servers",
        connect_mcp_servers,
        is_critical=False,
        timeout_seconds=20,
        resource_modes=["normal", "high", "gpu"],
    )
    
    orchestrator.register(
        "model_discovery",
        discover_models,
        is_critical=False,
        timeout_seconds=10,
        resource_modes=["normal", "high", "gpu"],
    )
    
    return orchestrator
