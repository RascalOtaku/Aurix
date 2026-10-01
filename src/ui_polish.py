"""
UI Polish Layer — Status indicators, compact controls, friction reduction.

Provides a cohesive feedback system and streamlines navigation/settings.
"""

import asyncio
import json
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from enum import Enum
from datetime import datetime

logger = logging.getLogger(__name__)


# ============================================================================
# System Status Tracking
# ============================================================================

class SystemStatusLevel(str, Enum):
    """System health status levels."""
    HEALTHY = "healthy"      # All systems ready
    DEGRADED = "degraded"    # Some systems offline but functional
    LIMITED = "limited"      # Core features only, hardware constrained
    CRITICAL = "critical"    # Errors or major issues


class FeatureStatus(str, Enum):
    """Individual feature availability."""
    READY = "ready"
    LOADING = "loading"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


@dataclass
class SystemStatus:
    """Real-time system health snapshot."""
    level: SystemStatusLevel
    timestamp: str
    features: Dict[str, FeatureStatus]
    resource_mode: str
    memory_percent: float
    cpu_percent: float
    warnings: List[str]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            **asdict(self),
            "level": self.level.value,
            "features": {k: v.value for k, v in self.features.items()},
        }


class SystemStatusMonitor:
    """Continuous system health monitoring."""
    
    def __init__(self, resource_mode: str):
        self.resource_mode = resource_mode
        self.status = SystemStatus(
            level=SystemStatusLevel.HEALTHY,
            timestamp=datetime.utcnow().isoformat(),
            features={
                "chat": FeatureStatus.READY,
                "memory": FeatureStatus.LOADING,
                "rag": FeatureStatus.LOADING,
                "tools": FeatureStatus.LOADING,
                "research": FeatureStatus.LOADING,
                "mcp": FeatureStatus.LOADING,
            },
            resource_mode=resource_mode,
            memory_percent=0.0,
            cpu_percent=0.0,
            warnings=[],
        )
    
    async def update_feature(self, feature: str, status: FeatureStatus, error: Optional[str] = None) -> None:
        """Update a feature's status."""
        self.status.features[feature] = status
        if error:
            self.status.warnings.append(f"{feature}: {error}")
        self._compute_overall_status()
    
    def _compute_overall_status(self) -> None:
        """Compute overall system status from feature statuses."""
        self.status.timestamp = datetime.utcnow().isoformat()
        
        # Update resource metrics
        try:
            import psutil
            self.status.memory_percent = psutil.virtual_memory().percent
            self.status.cpu_percent = psutil.cpu_percent(interval=0.1)
        except Exception:
            pass
        
        # Determine overall level
        statuses = list(self.status.features.values())
        
        if FeatureStatus.ERROR in statuses:
            self.status.level = SystemStatusLevel.CRITICAL
        elif FeatureStatus.UNAVAILABLE in statuses and len([s for s in statuses if s == FeatureStatus.READY]) < 3:
            self.status.level = SystemStatusLevel.DEGRADED
        elif self.resource_mode in ("ultra-low", "low") and self.status.memory_percent > 80:
            self.status.level = SystemStatusLevel.LIMITED
        else:
            self.status.level = SystemStatusLevel.HEALTHY
        
        # Add memory pressure warning
        if self.status.memory_percent > 85:
            if "High memory pressure" not in self.status.warnings:
                self.status.warnings.append("High memory pressure — consider closing other apps")
    
    async def get_status(self) -> Dict[str, Any]:
        """Get current status as JSON-serializable dict."""
        return self.status.to_dict()


# ============================================================================
# Compact Control Framework
# ============================================================================

@dataclass
class CompactControl:
    """Minimal, space-efficient UI control definition."""
    id: str
    label: str
    icon: str
    action: str
    tooltip: Optional[str] = None
    hidden: bool = False
    disabled: bool = False
    badge: Optional[str] = None  # Small indicator (e.g., "2" for 2 notifications)


class CompactControlSet:
    """Manages a group of compact controls for a specific context."""
    
    def __init__(self, context: str):
        self.context = context
        self.controls: Dict[str, CompactControl] = {}
    
    def add_control(
        self,
        id: str,
        label: str,
        icon: str,
        action: str,
        tooltip: Optional[str] = None,
        badge: Optional[str] = None,
    ) -> None:
        """Add a compact control."""
        self.controls[id] = CompactControl(
            id=id,
            label=label,
            icon=icon,
            action=action,
            tooltip=tooltip,
            badge=badge,
        )
    
    def get_visible_controls(self) -> List[Dict[str, Any]]:
        """Get non-hidden, non-disabled controls."""
        return [
            {
                "id": c.id,
                "label": c.label,
                "icon": c.icon,
                "action": c.action,
                "tooltip": c.tooltip,
                "badge": c.badge,
            }
            for c in self.controls.values()
            if not c.hidden and not c.disabled
        ]


def create_chat_controls() -> CompactControlSet:
    """Chat screen compact controls."""
    controls = CompactControlSet("chat")
    
    controls.add_control(
        id="attach",
        label="Attach",
        icon="📎",
        action="open_file_picker",
        tooltip="Attach files, images, or links",
    )
    
    controls.add_control(
        id="settings",
        label="Settings",
        icon="⚙️",
        action="open_settings_modal",
        tooltip="Chat settings & model selection",
    )
    
    controls.add_control(
        id="web_search",
        label="Web",
        icon="🔍",
        action="toggle_web_search",
        tooltip="Include web search in response",
    )
    
    controls.add_control(
        id="research",
        label="Research",
        icon="📚",
        action="toggle_research_mode",
        tooltip="Deep research & sources",
    )
    
    return controls


def create_session_controls() -> CompactControlSet:
    """Session list compact controls."""
    controls = CompactControlSet("sessions")
    
    controls.add_control(
        id="new",
        label="New",
        icon="➕",
        action="create_new_session",
        tooltip="Start a new conversation",
    )
    
    controls.add_control(
        id="search",
        label="Search",
        icon="🔍",
        action="open_session_search",
        tooltip="Search sessions by content",
    )
    
    controls.add_control(
        id="sort",
        label="Sort",
        icon="↕️",
        action="toggle_sort_order",
        tooltip="Sort by date or name",
    )
    
    return controls


def create_sidebar_tools() -> CompactControlSet:
    """Sidebar tool shortcuts."""
    controls = CompactControlSet("sidebar")
    
    controls.add_control(
        id="memory",
        label="Memory",
        icon="💭",
        action="navigate_to_memory",
        tooltip="View & manage memories",
    )
    
    controls.add_control(
        id="notes",
        label="Notes",
        icon="📝",
        action="navigate_to_notes",
        tooltip="Notes & to-do lists",
    )
    
    controls.add_control(
        id="calendar",
        label="Calendar",
        icon="📅",
        action="navigate_to_calendar",
        tooltip="Calendar & scheduling",
    )
    
    controls.add_control(
        id="email",
        label="Email",
        icon="✉️",
        action="navigate_to_email",
        tooltip="Email integration",
    )
    
    controls.add_control(
        id="cookbook",
        label="Skills",
        icon="👨‍🍳",
        action="navigate_to_skills",
        tooltip="Saved skills & workflows",
    )
    
    controls.add_control(
        id="library",
        label="Library",
        icon="📚",
        action="navigate_to_library",
        tooltip="Documents & files",
    )
    
    return controls


# ============================================================================
# First-Run Setup Experience
# ============================================================================

@dataclass
class SetupStep:
    """A step in the first-run setup flow."""
    id: str
    title: str
    description: str
    action: str
    skip_allowed: bool = True
    estimated_seconds: int = 30


class FirstRunSetupFlow:
    """Minimal, guided first-run setup."""
    
    def __init__(self, resource_mode: str):
        self.resource_mode = resource_mode
        self.steps: List[SetupStep] = [
            SetupStep(
                id="welcome",
                title="Welcome to Aurix",
                description="Your AI assistant with memory, skills, and research.",
                action="show_welcome",
                skip_allowed=False,
                estimated_seconds=10,
            ),
            SetupStep(
                id="api_key",
                title="Connect an LLM",
                description="Add your first LLM endpoint (OpenAI, Ollama, etc.)",
                action="prompt_model_endpoint",
                skip_allowed=False,
                estimated_seconds=60,
            ),
            SetupStep(
                id="theme",
                title="Pick a Theme",
                description="Choose your preferred color scheme & fonts.",
                action="show_theme_selector",
                skip_allowed=True,
                estimated_seconds=20,
            ),
        ]
        
        # Skip memory/RAG setup on ultra-low hardware
        if resource_mode not in ("ultra-low", "low"):
            self.steps.insert(
                2,
                SetupStep(
                    id="memory",
                    title="Enable Memory",
                    description="Aurix remembers key facts from your chats.",
                    action="enable_memory_vector",
                    skip_allowed=True,
                    estimated_seconds=15,
                ),
            )
    
    def get_steps(self) -> List[Dict[str, Any]]:
        """Get setup steps as JSON."""
        return [
            {
                "id": step.id,
                "title": step.title,
                "description": step.description,
                "action": step.action,
                "skipAllowed": step.skip_allowed,
                "estimatedSeconds": step.estimated_seconds,
            }
            for step in self.steps
        ]
    
    def get_progress_percent(self, completed_steps: List[str]) -> int:
        """Calculate setup progress."""
        return int((len(completed_steps) / len(self.steps)) * 100)


# ============================================================================
# Settings Streamlining
# ============================================================================

class CompactSettings:
    """Minimal, organized settings structure for faster UX."""
    
    ESSENTIAL_KEYS = {
        "model_endpoint",
        "api_key",
        "theme",
        "language",
        "auto_memory",
    }
    
    ADVANCED_KEYS = {
        "temperature",
        "max_tokens",
        "context_length",
        "memory_retention_days",
        "research_depth",
        "enable_web_search",
        "enable_rag",
    }
    
    SYSTEM_KEYS = {
        "resource_mode",
        "log_level",
        "cache_dir",
        "data_retention_days",
    }
    
    @staticmethod
    def get_essentials_only(settings: Dict[str, Any]) -> Dict[str, Any]:
        """Extract only essential settings for quick display."""
        return {k: v for k, v in settings.items() if k in CompactSettings.ESSENTIAL_KEYS}
    
    @staticmethod
    def organize_by_group(settings: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """Organize settings into logical groups."""
        return {
            "essentials": {k: v for k, v in settings.items() if k in CompactSettings.ESSENTIAL_KEYS},
            "advanced": {k: v for k, v in settings.items() if k in CompactSettings.ADVANCED_KEYS},
            "system": {k: v for k, v in settings.items() if k in CompactSettings.SYSTEM_KEYS},
        }


# ============================================================================
# Mobile & Responsive Helpers
# ============================================================================

class ResponsiveLayout:
    """Layout hints for responsive UI rendering."""
    
    @staticmethod
    def get_breakpoints() -> Dict[str, int]:
        """CSS breakpoints for responsive design."""
        return {
            "mobile": 480,
            "tablet": 768,
            "desktop": 1024,
            "wide": 1440,
        }
    
    @staticmethod
    def should_use_bottom_sheet(viewport_width: int) -> bool:
        """Use bottom sheet modals on narrow screens."""
        return viewport_width < 768
    
    @staticmethod
    def should_collapse_sidebar(viewport_width: int) -> bool:
        """Hide sidebar on narrow screens."""
        return viewport_width < 1024
    
    @staticmethod
    def get_grid_columns(viewport_width: int) -> int:
        """Dynamic grid column count based on viewport."""
        if viewport_width < 480:
            return 1
        elif viewport_width < 768:
            return 2
        elif viewport_width < 1440:
            return 3
        else:
            return 4


# ============================================================================
# Smooth Transitions & Loading States
# ============================================================================

class LoadingState:
    """Minimal, non-blocking loading indicators."""
    
    @staticmethod
    def get_skeleton_config() -> Dict[str, Any]:
        """Skeleton screen configuration for various content types."""
        return {
            "chat_message": {
                "lines": 3,
                "line_widths": [100, 100, 70],
                "animation": "pulse",
            },
            "session_item": {
                "lines": 2,
                "line_widths": [80, 60],
                "animation": "pulse",
            },
            "settings_page": {
                "sections": 3,
                "lines_per_section": 2,
                "animation": "pulse",
            },
        }
    
    @staticmethod
    def get_transition_timings() -> Dict[str, float]:
        """Smooth transition durations (ms)."""
        return {
            "short": 150,      # Hover effects, icon changes
            "medium": 300,     # Panel slides, modal opens
            "long": 500,       # Page transitions
        }


# ============================================================================
# Friction Reduction Utilities
# ============================================================================

class FrictionReducer:
    """One-click/auto-complete patterns to reduce user friction."""
    
    @staticmethod
    def should_auto_attach(previous_action: str) -> bool:
        """Auto-open file picker if user is likely to attach."""
        return previous_action in ("attach_requested", "image_mentioned")
    
    @staticmethod
    def should_suggest_preset(chat_content: str) -> Optional[str]:
        """Suggest a preset based on message content."""
        keywords_map = {
            "creative": ["write", "story", "poem", "generate", "imagine"],
            "technical": ["code", "debug", "error", "function", "script"],
            "research": ["explain", "summarize", "research", "find", "source"],
            "casual": ["hey", "how are you", "what's up", "tell me about"],
        }
        
        content_lower = chat_content.lower()
        for preset, keywords in keywords_map.items():
            if any(kw in content_lower for kw in keywords):
                return preset
        
        return None
    
    @staticmethod
    def get_quick_actions() -> List[Dict[str, str]]:
        """Quick action suggestions for empty chat."""
        return [
            {
                "text": "📝 Start a new note",
                "action": "create_note",
            },
            {
                "text": "🔍 Search your memory",
                "action": "search_memory",
            },
            {
                "text": "📚 Browse skills",
                "action": "browse_skills",
            },
            {
                "text": "⚙️ Adjust settings",
                "action": "open_settings",
            },
        ]


# ============================================================================
# Accessibility & Keyboard Navigation
# ============================================================================

class KeyboardShortcuts:
    """Keyboard shortcuts for power users."""
    
    SHORTCUTS = {
        "focus_input": ("Ctrl+J", "Focus message input"),
        "send_message": ("Ctrl+Enter", "Send message"),
        "new_session": ("Ctrl+N", "New conversation"),
        "search_sessions": ("Ctrl+Shift+F", "Search sessions"),
        "open_settings": ("Ctrl+,", "Open settings"),
        "toggle_sidebar": ("Ctrl+B", "Toggle sidebar"),
        "attach_file": ("Ctrl+Shift+A", "Attach file"),
        "clear_context": ("Ctrl+Shift+C", "Clear chat context"),
    }
    
    @staticmethod
    def get_shortcuts() -> Dict[str, tuple]:
        """Get all keyboard shortcuts."""
        return KeyboardShortcuts.SHORTCUTS
    
    @staticmethod
    def get_help_text() -> str:
        """Generate keyboard shortcuts help text."""
        lines = ["Keyboard Shortcuts:\n"]
        for action, (keys, desc) in KeyboardShortcuts.SHORTCUTS.items():
            lines.append(f"{keys:15} — {desc}")
        return "\n".join(lines)


logger.info("UI polish layer loaded (status, controls, setup, settings, responsive, friction reduction, a11y)")
