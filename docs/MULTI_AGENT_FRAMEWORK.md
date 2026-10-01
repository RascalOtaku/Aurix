"""Multi-agent framework integration documentation.

## Quick Start

### 1. Basic Usage

```python
from core.agents import Agent, AgentConfig, AgentManager
from core.agents.types import AgentRole

# Create manager
manager = AgentManager(name="Aurix")

# Create agent
config = AgentConfig(
    name="CodeExpert",
    role=AgentRole.SPECIALIST,
    model="gpt-4"
)
agent = YourAgentImpl(config)  # Subclass of Agent
manager.register_agent(agent)

# Dispatch task
task_id = await manager.dispatch_task(
    task_description="Review this code",
    agent_id=agent.id
)
```

### 2. Integration with FastAPI

Add to `app.py`:
```python
from routes.agent_routes import setup_agent_routes
app.include_router(setup_agent_routes(app.state))
```

Then use the API:
```bash
# Dispatch task
curl -X POST http://localhost:8000/api/agents/task/dispatch \
  -d 'task_description=Analyze code' \
  -d 'priority=1'

# Get status
curl http://localhost:8000/api/agents/status

# List agents
curl http://localhost:8000/api/agents/list
```

### 3. Using LLM-Powered Agents

```python
from core.agents.implementations import ClaudeAgent, OpenAIAgent, LocalLLMAgent

# Claude
agent = ClaudeAgent(config, api_key="sk-ant-...")

# OpenAI
agent = OpenAIAgent(config, api_key="sk-...")

# Local LLM (Ollama, etc)
agent = LocalLLMAgent(config, base_url="http://localhost:11434/v1")
```

### 4. Skills System

```python
from core.skills import Skill, SkillRegistry
from core.skills.types import SkillCategory, SkillMetadata, SkillExecutionContext

# Create skill
class MySkill(Skill):
    async def execute(self, context: SkillExecutionContext):
        result = context.inputs.get("data")
        return {"processed": True, "result": result}

# Register
registry = SkillRegistry()
registry.register(MySkill(SkillMetadata(
    name="my_skill",
    category=SkillCategory.CUSTOM,
    description="My custom skill"
)))

# Use
skill = registry.get("my_skill")
result = await skill.run(agent_id="agent-1", inputs={"data": [...]})
```

### 5. Memory Management

```python
from core.memory import MemoryStore
from core.memory.types import MemoryType

store = MemoryStore()

# Store memory
store.store(
    key="user_preference",
    value="likes concise responses",
    memory_type=MemoryType.LONG_TERM,
    agent_id="agent-1",
    ttl=86400  # 24 hours
)

# Retrieve
value = store.retrieve(
    key="user_preference",
    agent_id="agent-1"
)

# Search
results = store.search(
    query="user",
    agent_id="agent-1",
    memory_type=MemoryType.LONG_TERM
)
```

## Architecture

- **Agents**: Autonomous entities with roles (orchestrator, specialist, reviewer, etc)
- **Manager**: Coordinates multi-agent workflows and task dispatch
- **Skills**: Composable capabilities (code, data, research, communication, etc)
- **Memory**: Multi-tier storage (short-term, long-term, episodic, semantic)
- **LLM Integration**: Support for Claude, OpenAI, local models

## Patterns

### Multi-Agent Collaboration
```python
results = await manager.collaborate(
    task_description="Build and test a feature",
    agent_roles=[AgentRole.SPECIALIST, AgentRole.REVIEWER]
)
```

### Task Dependency Chain
```python
task1 = await manager.dispatch_task("Generate code", priority=1)
task2 = await manager.dispatch_task(
    "Review code",
    priority=2,
    dependencies=[task1]  # Wait for task1
)
```

### Agent Subscription
```python
async def on_agent_event(event):
    print(f"Event: {event['event']}, Data: {event['data']}")

agent.subscribe(on_agent_event)
```

## Integration Points

1. **Existing Chat Handler**: Call agent.think() from chat routes
2. **Research Handler**: Use WebSearch skill for background research
3. **Session Manager**: Store agent state per session
4. **Memory Manager**: Integrate with existing memory system
5. **Skills Manager**: Merge with existing skills loading

## Future Enhancements

- [ ] Tool use / function calling via LLM
- [ ] Streaming responses
- [ ] Agent-to-agent communication
- [ ] Distributed execution (multiple workers)
- [ ] Persistence (save/load agent state)
- [ ] Evaluation metrics (latency, success rate)
- [ ] Agent learning (feedback loops)

## API Reference

### Agent
- `async think(task: str) -> str`: Process task
- `async execute(action: str, params) -> Any`: Execute action
- `add_capability(capability)`: Register capability
- `get_capabilities()`: List capabilities
- `subscribe(listener)`: Listen to events
- `get_state()`: Current state dict

### AgentManager
- `register_agent(agent) -> str`: Register agent, return ID
- `get_agent(agent_id) -> Agent`: Get agent by ID
- `get_agents_by_role(role) -> List[Agent]`: Filter by role
- `get_agents_by_capability(name) -> List[Agent]`: Filter by capability
- `async dispatch_task(description, agent_id?, priority?) -> str`: Dispatch task, return task ID
- `async collaborate(description, roles) -> Dict`: Multi-agent collaboration
- `get_fleet_status() -> Dict`: Fleet overview
- `export_workflow() -> str`: Export as JSON

### SkillRegistry
- `register(skill)`: Register skill
- `unregister(name)`: Unregister skill
- `get(name) -> Skill`: Get skill
- `get_by_category(category) -> List[Skill]`: Filter by category
- `list_all() -> List[Dict]`: List all with metadata

### MemoryStore
- `store(key, value, memory_type, agent_id, ttl?, tags?) -> str`: Store entry
- `retrieve(key, agent_id, memory_type?) -> Any`: Get entry
- `search(query, agent_id?, memory_type?) -> List[MemoryEntry]`: Search
- `delete(key, agent_id, memory_type?) -> bool`: Delete entry
- `clear(agent_id?)`: Clear entries
- `get_stats() -> Dict`: Storage stats
"""
