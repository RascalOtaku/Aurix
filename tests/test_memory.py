"""Tests for memory system."""

import pytest
from core.memory import MemoryStore
from core.memory.types import MemoryType


def test_memory_store():
    store = MemoryStore()
    agent_id = "agent-1"
    
    # Store entry
    entry_id = store.store(
        key="test_key",
        value="test_value",
        memory_type=MemoryType.SHORT_TERM,
        agent_id=agent_id,
    )
    
    assert entry_id is not None
    
    # Retrieve entry
    value = store.retrieve(
        key="test_key",
        agent_id=agent_id,
        memory_type=MemoryType.SHORT_TERM,
    )
    assert value == "test_value"


def test_memory_search():
    store = MemoryStore()
    agent_id = "agent-1"
    
    store.store(
        key="task_1",
        value="Generate API",
        memory_type=MemoryType.EPISODIC,
        agent_id=agent_id,
    )
    store.store(
        key="task_2",
        value="Review Code",
        memory_type=MemoryType.EPISODIC,
        agent_id=agent_id,
    )
    
    results = store.search("Generate", agent_id=agent_id)
    assert len(results) == 1
    assert results[0].key == "task_1"


def test_memory_delete():
    store = MemoryStore()
    agent_id = "agent-1"
    
    store.store(
        key="test",
        value="data",
        memory_type=MemoryType.SHORT_TERM,
        agent_id=agent_id,
    )
    
    deleted = store.delete(
        key="test",
        agent_id=agent_id,
        memory_type=MemoryType.SHORT_TERM,
    )
    assert deleted
    
    value = store.retrieve(
        key="test",
        agent_id=agent_id,
    )
    assert value is None
