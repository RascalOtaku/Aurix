"""Memory type definitions."""

from enum import Enum
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from datetime import datetime


class MemoryType(str, Enum):
    """Types of memory."""
    SHORT_TERM = "short_term"    # Current session
    LONG_TERM = "long_term"      # Persistent
    EPISODIC = "episodic"        # Event-based
    SEMANTIC = "semantic"        # Knowledge-based
    WORKING = "working"          # Active processing


@dataclass
class MemoryEntry:
    """Single memory entry."""
    key: str
    value: Any
    memory_type: MemoryType
    agent_id: str
    timestamp: datetime = field(default_factory=datetime.now)
    ttl: Optional[float] = None  # Time to live in seconds
    tags: list = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_expired(self) -> bool:
        """Check if entry has expired."""
        if self.ttl is None:
            return False
        elapsed = (datetime.now() - self.timestamp).total_seconds()
        return elapsed > self.ttl
