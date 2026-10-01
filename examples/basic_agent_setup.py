"""Example: Basic multi-agent setup."""

import asyncio
from core.agents import Agent, AgentConfig, AgentManager
from core.agents.types import AgentRole, AgentCapability


class SimpleAgent(Agent):
    """Simple agent implementation."""

    async def _process_task(self, task: str) -> str:
        # Simulate processing
        await asyncio.sleep(0.1)
        return f"Processed: {task}"

    async def _execute_action(self, action: str, params):
        await asyncio.sleep(0.1)
        return {"action": action, "params": params, "status": "executed"}


async def main():
    # Create agent manager
    manager = AgentManager(name="Aurix-Demo")

    # Create and register agents
    orchestrator_config = AgentConfig(
        name="Orchestrator",
        role=AgentRole.ORCHESTRATOR,
        description="Coordinates multi-agent workflows",
        capabilities=[
            AgentCapability(
                name="task_delegation",
                description="Delegate tasks to specialist agents",
                tags=["coordination", "orchestration"]
            )
        ]
    )

    specialist_config = AgentConfig(
        name="CodeSpecialist",
        role=AgentRole.SPECIALIST,
        description="Specialized in code generation and review",
        capabilities=[
            AgentCapability(
                name="code_generation",
                description="Generate code from specifications",
                tags=["coding"]
            ),
            AgentCapability(
                name="code_review",
                description="Review and analyze code",
                tags=["review"]
            )
        ]
    )

    # Instantiate agents
    orchestrator = SimpleAgent(orchestrator_config)
    specialist = SimpleAgent(specialist_config)

    # Register with manager
    orch_id = manager.register_agent(orchestrator)
    spec_id = manager.register_agent(specialist)

    print("🤖 Multi-Agent System Initialized")
    print(f"Orchestrator: {orch_id}")
    print(f"Specialist: {spec_id}")

    # Dispatch tasks
    task1 = await manager.dispatch_task(
        "Analyze codebase structure",
        agent_id=spec_id,
        priority=1
    )

    task2 = await manager.dispatch_task(
        "Generate API documentation",
        agent_id=spec_id,
        priority=2
    )

    print(f"\n📋 Tasks dispatched:")
    print(f"  Task 1: {task1}")
    print(f"  Task 2: {task2}")

    # Get fleet status
    status = manager.get_fleet_status()
    print(f"\n📊 Fleet Status:")
    import json
    print(json.dumps(status, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
