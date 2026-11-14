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

### 2. What is wired into AURIX today

**Agents start with AURIX and chat can call them.** There are still no `/api/agents/...` routes.

- At startup `src/app_initializer.py` builds `AgentManager(name="Aurix")`, a `SkillRegistry` with the
  built-in skills, and one agent per role (`core/agents/routing.py: build_default_fleet`):
  Planner (orchestrator), Researcher, Reviewer, Coder (specialist) and Executor.
- In chat (`/api/chat`, `/api/chat_stream`, so the web UI and Telegram), a message that explicitly
  asks for the agents goes to them instead of the normal chat path:
  - `@agent <ask>` or `agent: <ask>` (or "multi-agent", "agent mode") picks the agent by keywords;
  - `@planner`, `@researcher`, `@reviewer`, `@coder`, `@executor` pick one directly.
  Plain chat is unchanged. The turn is saved to the session like any other.
- Routing (first match wins): run/install/delete/send/deploy... -> Executor;
  review/critique/double-check... -> Reviewer; code/bug/traceback/refactor... -> Coder;
  research/look up/compare/find out... -> Researcher; anything else -> Planner.
  Executor is checked first so a mixed ask ("look into X and delete Y") lands with the gated agent.
- Agents think with the **session's own model** (`src/agent_chat.py: session_llm`), bound per call
  through `core/agents/context.py`; outside chat they use AURIX's default endpoint.
- The Researcher gets web results first (`web_search` skill -> `src/search`), passed to the model
  as untrusted data.

### Acting: always through the approval gate

`Agent.execute()` used to return a canned `"executed"` without running anything. Every agent now
acts through `core/agents/execution.py: run_gated_action`, which calls the real
`src.tool_execution.execute_tool_block` inside `approval_gate.force_gate()`. Inside that block every
consequential tool (shell, file writes, email, settings, ...) is held for the owner's approve/deny on
Telegram in **every session and gate mode, including `APPROVAL_GATE_MODE=off`**, and fails closed.
The result says what really happened: `executed`, `failed`, `denied` or `error`.

In chat the agents only think by default. The Executor drafts its steps as tool blocks and the
reply says nothing has run. With `AURIX_AGENT_EXECUTE=1` each step is sent through the gate in
order, and the run stops at the first step that is denied or fails.

### 3. Using LLM-Powered Agents

```python
from core.agents.implementations import ClaudeAgent, OpenAIAgent, LocalLLMAgent

# Claude (AsyncAnthropic; model defaults to claude-opus-5-5, AURIX_CLAUDE_MODEL overrides)
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

1. **Chat**: `ChatHandler.maybe_route_agent_task()` is called from `/api/chat` and `/api/chat_stream`
2. **Web search**: the `web_search` skill uses AURIX's own search providers (`src/search`)
3. **Session Manager**: Store agent state per session
4. **Memory Manager**: Integrate with existing memory system
5. **Skills Manager**: Merge with existing skills loading

## Future Enhancements

- [ ] HTTP routes for dispatching tasks and checking agent status
- [x] Call `maybe_route_agent_task()` from the chat flow
- [x] Gated tool execution for agent actions
- [ ] Native tool use / function calling via the LLM APIs
- [ ] Agents as thinkers inside the `do:` autonomy loop (reviewer for self-checks, researcher for re-planning)
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
