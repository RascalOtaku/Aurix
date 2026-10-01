"""Memory storage implementation."""

from typing import Any, Dict, List, Optional
from .types import MemoryType, MemoryEntry
import json


class MemoryStore:
    """In-memory store for agent memory (can be extended with persistence)."""

    def __init__(self, max_size: int = 10000):
        self.max_size = max_size
        self.entries: Dict[str, MemoryEntry] = {}
        self._cleanup_expired()

    def store(
        self,
        key: str,
        value: Any,
        memory_type: MemoryType,
        agent_id: str,
        ttl: Optional[float] = None,
        tags: Optional[List[str]] = None,
    ) -> str:
        """Store memory entry."""
        entry = MemoryEntry(
            key=key,
            value=value,
            memory_type=memory_type,
            agent_id=agent_id,
            ttl=ttl,
            tags=tags or [],
        )

        # Enforce size limit
        if len(self.entries) >= self.max_size:
            self._evict_oldest()

        entry_id = f"{agent_id}:{key}:{memory_type.value}"
        self.entries[entry_id] = entry
        return entry_id

    def retrieve(
        self,
        key: str,
        agent_id: str,
        memory_type: Optional[MemoryType] = None,
    ) -> Optional[Any]:
        """Retrieve memory entry."""
        self._cleanup_expired()

        if memory_type:
            entry_id = f"{agent_id}:{key}:{memory_type.value}"
            entry = self.entries.get(entry_id)
        else:
            # Search across types
            entry = None
            for stored_id, stored_entry in self.entries.items():
                if stored_entry.agent_id == agent_id and stored_entry.key == key:
                    entry = stored_entry
                    break

        return entry.value if entry else None

    def search(
        self,
        query: str,
        agent_id: Optional[str] = None,
        memory_type: Optional[MemoryType] = None,
    ) -> List[MemoryEntry]:
        """Search memory entries."""
        self._cleanup_expired()

        results = []
        for entry in self.entries.values():
            if agent_id and entry.agent_id != agent_id:
                continue
            if memory_type and entry.memory_type != memory_type:
                continue
            if query.lower() in str(entry.key).lower() or query.lower() in str(entry.value).lower():
                results.append(entry)

        return results

    def delete(
        self,
        key: str,
        agent_id: str,
        memory_type: Optional[MemoryType] = None,
    ) -> bool:
        """Delete memory entry."""
        if memory_type:
            entry_id = f"{agent_id}:{key}:{memory_type.value}"
            if entry_id in self.entries:
                del self.entries[entry_id]
                return True
        else:
            # Delete across all types for this key
            to_delete = [
                eid for eid, entry in self.entries.items()
                if entry.agent_id == agent_id and entry.key == key
            ]
            for entry_id in to_delete:
                del self.entries[entry_id]
            return bool(to_delete)

        return False

    def _cleanup_expired(self) -> None:
        """Remove expired entries."""
        to_delete = [
            eid for eid, entry in self.entries.items()
            if entry.is_expired()
        ]
        for entry_id in to_delete:
            del self.entries[entry_id]

    def _evict_oldest(self) -> None:
        """Evict oldest entry when size limit reached."""
        if self.entries:
            oldest_id = min(
                self.entries.keys(),
                key=lambda x: self.entries[x].timestamp
            )
            del self.entries[oldest_id]

    def clear(self, agent_id: Optional[str] = None) -> None:
        """Clear all or agent-specific entries."""
        if agent_id:
            to_delete = [
                eid for eid, entry in self.entries.items()
                if entry.agent_id == agent_id
            ]
            for entry_id in to_delete:
                del self.entries[entry_id]
        else:
            self.entries.clear()

    def get_stats(self) -> Dict[str, Any]:
        """Get memory statistics."""
        by_type = {}
        by_agent = {}

        for entry in self.entries.values():
            mtype = entry.memory_type.value
            aid = entry.agent_id

            by_type[mtype] = by_type.get(mtype, 0) + 1
            by_agent[aid] = by_agent.get(aid, 0) + 1

        return {
            "total_entries": len(self.entries),
            "by_type": by_type,
            "by_agent": by_agent,
            "utilization": f"{(len(self.entries)/self.max_size)*100:.1f}%",
        }
