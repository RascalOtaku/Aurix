"""
The Aurix Experience Layer — what makes someone go "whoa, this is different"

The goal: frictionless first contact, instant delight, and a sense of control.
No loading spinners, no confusing menus, no "just one more setup step."
You open it. It works. You're amazed.
"""

import asyncio
import json
import logging
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass
from enum import Enum
from datetime import datetime
import random

logger = logging.getLogger(__name__)


# ============================================================================
# The Onboarding Magic
# ============================================================================

class OnboardingMoment:
    """
    Turn the first 30 seconds into delight, not friction.
    
    Instead of "configure your API key" — show what Aurix CAN DO
    while they're deciding what model to use.
    """
    
    DEMO_INTERACTIONS = [
        {
            "title": "Ask Aurix Anything",
            "example": "What are the top 5 ways to improve my productivity?",
            "icon": "💭",
            "capability": "memory",
            "description": "Aurix learns about you over time",
        },
        {
            "title": "Run Tasks Autonomously",
            "example": "Search the web for Python performance tips and summarize them",
            "icon": "🤖",
            "capability": "agent",
            "description": "Hand it a complex task. It figures it out.",
        },
        {
            "title": "Deep Research on Any Topic",
            "example": "Research the history of AI and create a visual timeline",
            "icon": "📚",
            "capability": "research",
            "description": "Multi-step research with sources and synthesis",
        },
        {
            "title": "Compare Models Blind",
            "example": "Ask both Claude and Llama the same question",
            "icon": "⚖️",
            "capability": "compare",
            "description": "No bias. Just pure model quality comparison.",
        },
        {
            "title": "Write with AI Assistance",
            "example": "Draft an email, then let AI refine it",
            "icon": "✍️",
            "capability": "documents",
            "description": "You write. AI assists. Not the other way around.",
        },
        {
            "title": "Manage Everything (Email, Calendar, Tasks)",
            "example": "What's on my calendar today? Any urgent emails?",
            "icon": "📅",
            "capability": "integrations",
            "description": "Your life, organized by AI",
        },
    ]
    
    @staticmethod
    def get_welcome_screen(resource_mode: str) -> Dict[str, Any]:
        """The first screen someone sees."""
        return {
            "title": "Welcome to Aurix",
            "subtitle": "Your self-hosted AI workspace — local-first, privacy-first, no telemetry",
            "tagline": "Chat. Agents. Research. Email. Calendar. Memory. All on your hardware.",
            "visual": "✨ ࣪ ˖ ૮( ˶ᵔ ᵕ ᵔ˶ )っ",
            "features": OnboardingMoment.DEMO_INTERACTIONS,
            "cta": {
                "primary": "Add Your First Model",
                "secondary": "Explore Demo (No Setup Required)",
            },
            "resource_mode": resource_mode,
            "resource_message": {
                "ultra-low": "📱 Running in ultra-light mode — perfect for older hardware",
                "low": "💾 Running in low-resource mode — lighter on memory",
                "balanced": "⚖️ Running in balanced mode — all features available",
                "full": "🚀 Running in full mode — all features, maximum performance",
            }.get(resource_mode, ""),
        }
    
    @staticmethod
    def get_demo_session() -> Dict[str, Any]:
        """A fully interactive demo without requiring setup."""
        return {
            "id": "demo_session",
            "name": "Aurix Demo",
            "is_demo": True,
            "intro_message": "Hi! I'm Aurix, your self-hosted AI assistant. You can try me out without any setup. Here are some things you can ask me:",
            "suggested_prompts": [
                "Tell me about your capabilities",
                "Show me what you can remember",
                "Run an autonomous web search",
                "Compare models side by side",
                "Help me write something",
            ],
            "capabilities": {
                "chat": True,
                "memory": True,
                "research": False,  # Limited in demo
                "tools": False,     # Limited in demo
                "code": False,      # Limited in demo
            },
        }


# ============================================================================
# The "Wow" Moments — Smart Defaults & Surprises
# ============================================================================

class WowMoment:
    """
    Delight through smart, unexpected behavior.
    Not complicated. Just thoughtful.
    """
    
    @staticmethod
    async def first_message_magic(message: str, user_history_empty: bool) -> Optional[str]:
        """
        If the user's first message is a common question,
        show them something impressive before answering.
        """
        wow_patterns = {
            "what can you do": "capability_demo",
            "how are you different": "differentiator_explainer",
            "what's special about": "feature_highlight",
            "can you": "capability_check",
            "remember": "memory_intro",
        }
        
        for pattern, wow_type in wow_patterns.items():
            if pattern.lower() in message.lower() and user_history_empty:
                return wow_type
        
        return None
    
    @staticmethod
    def capability_demo() -> str:
        """Show what Aurix does, in one elegant response."""
        return """
🎯 Here's what makes Aurix different:

**Chat** — Talk to any model (local or API). No friction.
**Agents** — Give me a complex task. I'll break it down, use tools, and solve it.
**Memory** — I remember what matters. Next time you mention it, I already know.
**Deep Research** — I don't just search. I read, compare, synthesize sources.
**Documents** — You write. I assist. Not the other way around.
**Email & Calendar** — Triage, remind, organize. All with AI.
**Compare** — Test models blind. No bias, just raw quality.

Try asking me to:
- Search the web and summarize
- Create a structured document with AI suggestions
- Remember something important
- Run a complex autonomous task

Everything stays on your hardware. No telemetry. No corporate AI.
        """
    
    @staticmethod
    def differentiator_explainer() -> str:
        """Why Aurix matters."""
        return """
🔓 **Aurix is different because:**

✅ **It's yours.** Runs on your hardware. Your data never leaves.
✅ **It's flexible.** Swap models, add tools, customize everything.
✅ **It's honest.** Shows sources. Admits limits. No fluff.
✅ **It's ambitious.** Full agents, deep research, autonomous tasks — all local.
✅ **It's thoughtful.** Memory that actually helps. Tools that matter.

**Compare this to:**
- ChatGPT: Powerful but you lose privacy and control
- LLaMA/Ollama: Great models but boring UI
- Other self-hosted: Too technical, too slow, limited tools

Aurix aims to be **ChatGPT's features + your control**.
        """


# ============================================================================
# Smart Defaults That Feel Like Magic
# ============================================================================

class SmartDefaults:
    """
    Aurix should anticipate what you want, not ask.
    """
    
    @staticmethod
    def suggest_model_for_task(task_type: str, available_models: List[str]) -> Optional[str]:
        """
        Route tasks to the right model automatically.
        User never has to think about it.
        """
        task_routing = {
            "creative": ["mistral", "llama2", "neural-chat"],
            "coding": ["deepseek", "granite", "codellama"],
            "research": ["qwen", "mistral-large"],
            "quick": ["phi", "tinyllama"],  # Fast & efficient
            "reasoning": ["llama2", "neural-chat"],
        }
        
        candidates = task_routing.get(task_type, available_models)
        for model in candidates:
            if any(m for m in available_models if model.lower() in m.lower()):
                return model
        
        return available_models[0] if available_models else None
    
    @staticmethod
    def auto_enable_features(resource_mode: str) -> Dict[str, bool]:
        """
        Smart feature toggles based on what the hardware can handle.
        No manual configuration needed.
        """
        return {
            "ultra-low": {
                "chat": True,
                "memory": False,      # ChromaDB is heavy
                "research": False,
                "web_search": False,
                "document_editing": False,
                "email": False,
                "calendar": False,
                "mcp": False,
            },
            "low": {
                "chat": True,
                "memory": True,       # But lightweight
                "research": False,
                "web_search": True,
                "document_editing": True,
                "email": False,
                "calendar": False,
                "mcp": False,
            },
            "balanced": {
                "chat": True,
                "memory": True,
                "research": True,     # Single-step only
                "web_search": True,
                "document_editing": True,
                "email": True,
                "calendar": True,
                "mcp": True,
            },
            "full": {
                "chat": True,
                "memory": True,
                "research": True,     # Full multi-step
                "web_search": True,
                "document_editing": True,
                "email": True,
                "calendar": True,
                "mcp": True,
            },
        }.get(resource_mode, {})
    
    @staticmethod
    def auto_select_preset(message: str) -> Optional[str]:
        """
        Guess the user's intent and suggest a preset.
        They can still override, but we make a smart guess.
        """
        intent_map = {
            "creative": ["write", "story", "poem", "create", "imagine", "invent"],
            "technical": ["code", "debug", "error", "function", "script", "python", "javascript"],
            "research": ["explain", "summarize", "research", "how does", "why", "what is"],
            "casual": ["hey", "hi", "what's up", "tell me about", "who is"],
            "analytical": ["analyze", "compare", "evaluate", "pros and cons"],
        }
        
        message_lower = message.lower()
        for preset, keywords in intent_map.items():
            if any(kw in message_lower for kw in keywords):
                return preset
        
        return None


# ============================================================================
# The Experience — Seamless Transitions
# ============================================================================

class SeamlessExperience:
    """
    Micro-interactions that make Aurix feel polished.
    """
    
    @staticmethod
    def get_context_aware_help(current_view: str, user_action: str) -> Optional[str]:
        """
        Show help only when actually helpful, not intrusive.
        """
        help_map = {
            "chat_empty": "Try one of these prompts or describe what you want",
            "memory_empty": "Memories appear here as you chat. Aurix learns what matters to you.",
            "first_attachment": "Tip: You can attach images, PDFs, or files. Aurix can analyze them.",
            "first_agent_run": "Agent is working... You can watch it think and adjust if needed.",
            "compare_mode": "Ask the same thing to multiple models. See the differences.",
            "deep_research": "This takes a moment. Deep research reads sources and synthesizes.",
        }
        
        return help_map.get(current_view)
    
    @staticmethod
    def get_ambient_status_messages(resource_mode: str, uptime_seconds: int) -> List[str]:
        """
        Soft, non-intrusive status updates. More like vibes than logs.
        """
        messages = {
            "startup_complete": "✨ Aurix is ready",
            "memory_loaded": "💭 Memory loaded",
            "model_warming": "🔥 Model warming up...",
            "agent_thinking": "🤔 Thinking...",
            "research_gathering": "📚 Gathering sources...",
            "research_synthesizing": "🔗 Connecting the dots...",
        }
        
        if resource_mode == "ultra-low":
            return ["Aurix is ready (lightweight mode)"]
        elif resource_mode == "low":
            return ["Aurix is ready (resource-conscious)"]
        
        return list(messages.values())[:3]


# ============================================================================
# Mobile-First Gestures & Interactions
# ============================================================================

class MobileExperience:
    """
    Make mobile feel like a first-class experience, not a squeezed desktop.
    """
    
    MOBILE_GESTURES = {
        "swipe_left": "previous_session",
        "swipe_right": "next_session",
        "swipe_up": "scroll_to_top",
        "long_press_message": "show_context_menu",
        "double_tap_heart": "like_message",
        "pull_down": "refresh",
    }
    
    @staticmethod
    def get_mobile_nav() -> Dict[str, Any]:
        """Bottom tab bar optimized for thumb reach."""
        return {
            "layout": "bottom_tabs",
            "tabs": [
                {
                    "id": "chat",
                    "icon": "💬",
                    "label": "Chat",
                    "action": "navigate_to_chat",
                },
                {
                    "id": "memory",
                    "icon": "💭",
                    "label": "Memory",
                    "action": "navigate_to_memory",
                },
                {
                    "id": "tools",
                    "icon": "🛠️",
                    "label": "Tools",
                    "action": "navigate_to_tools",
                },
                {
                    "id": "settings",
                    "icon": "⚙️",
                    "label": "Settings",
                    "action": "navigate_to_settings",
                },
            ],
            "gesture_hints": MobileExperience.MOBILE_GESTURES,
        }
    
    @staticmethod
    def get_touch_optimized_controls() -> Dict[str, Any]:
        """Buttons sized for fingers, not mice."""
        return {
            "min_tap_target": 48,  # pixels
            "spacing": 16,         # between controls
            "corner_radius": 12,   # rounded
            "feedback": "haptic",  # vibration
        }


# ============================================================================
# The Status Glow — Real-Time Feedback Without Noise
# ============================================================================

class StatusGlow:
    """
    Show what's happening without cluttering the interface.
    A gentle glow tells the story.
    """
    
    STATUS_VISUALS = {
        "idle": {"color": "var(--success)", "icon": "●", "pulse": False},
        "thinking": {"color": "var(--info)", "icon": "◆", "pulse": True},
        "loading": {"color": "var(--info)", "icon": "◇", "pulse": True},
        "working": {"color": "var(--primary)", "icon": "⟳", "pulse": True},
        "warning": {"color": "var(--warning)", "icon": "⚠", "pulse": False},
        "error": {"color": "var(--danger)", "icon": "✕", "pulse": False},
        "success": {"color": "var(--success)", "icon": "✓", "pulse": False},
    }
    
    @staticmethod
    def get_status_indicator(state: str) -> Dict[str, Any]:
        """Visual indicator for current state."""
        visual = StatusGlow.STATUS_VISUALS.get(state, StatusGlow.STATUS_VISUALS["idle"])
        return {
            "state": state,
            **visual,
            "css_class": f"status-glow status-{state}",
        }
    
    @staticmethod
    def get_ambient_animations() -> str:
        """Subtle CSS animations that feel alive."""
        return """
/* Status Glow Animations */
@keyframes pulse-soft {
    0%, 100% { opacity: 0.6; }
    50% { opacity: 1; }
}

@keyframes rotate-slow {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
}

.status-glow {
    display: inline-block;
    padding: 4px 12px;
    border-radius: 20px;
    background: var(--status-bg, rgba(255,255,255,0.1));
    border: 1px solid currentColor;
    font-size: 12px;
    font-weight: 500;
}

.status-glow.status-thinking .icon,
.status-glow.status-loading .icon,
.status-glow.status-working .icon {
    animation: pulse-soft 1.5s ease-in-out infinite;
}

.status-glow.status-working {
    animation: rotate-slow 2s linear infinite;
}

/* Quick feedback */
.message.new-message {
    animation: slideInUp 0.3s ease-out;
}

@keyframes slideInUp {
    from { transform: translateY(20px); opacity: 0; }
    to { transform: translateY(0); opacity: 1; }
}
        """


# ============================================================================
# Quick-Action Surfaces
# ============================================================================

class QuickActions:
    """
    Powerful things should be one tap away.
    No digging through menus.
    """
    
    @staticmethod
    def get_chat_quick_actions(session_state: Dict[str, Any]) -> List[Dict[str, str]]:
        """Context-aware buttons for chat."""
        actions = []
        
        if session_state.get("has_attachments"):
            actions.append({"text": "📎 Clear attachments", "action": "clear_attachments"})
        
        if not session_state.get("use_web"):
            actions.append({"text": "🔍 Enable web search", "action": "enable_web_search"})
        
        if session_state.get("message_count", 0) > 0:
            actions.append({"text": "📋 Copy conversation", "action": "copy_conversation"})
            actions.append({"text": "💾 Export as document", "action": "export_as_doc"})
        
        actions.append({"text": "⚙️ Settings", "action": "open_settings"})
        
        return actions
    
    @staticmethod
    def get_empty_state_suggestions(user_profile: Optional[Dict[str, Any]] = None) -> List[str]:
        """What should the user try next?"""
        base_suggestions = [
            "💭 Search your memories",
            "🤖 Run an autonomous task",
            "📚 Deep research a topic",
            "✍️ Start a new document",
            "⚖️ Compare models",
        ]
        
        if user_profile and user_profile.get("frequent_tasks"):
            return [
                f"🎯 {task}" for task in user_profile["frequent_tasks"][:3]
            ] + base_suggestions[:2]
        
        return base_suggestions


# ============================================================================
# The Reveal — Progressive Feature Discovery
# ============================================================================

class FeatureDiscovery:
    """
    Don't overwhelm with all features at once.
    Reveal them as the user naturally explores.
    """
    
    FEATURE_MILESTONES = {
        "first_message": ["chat_basics"],
        "after_5_messages": ["web_search", "presets"],
        "after_10_messages": ["memory_features", "research_mode"],
        "after_3_sessions": ["agents", "compare_mode"],
        "after_1_week": ["deep_research", "skills", "integrations"],
        "after_5_documents": ["advanced_editing", "templates"],
    }
    
    @staticmethod
    def should_reveal_feature(user_activity: Dict[str, Any], feature: str) -> bool:
        """Decide if now is the right time to show this feature."""
        milestones = FeatureDiscovery.FEATURE_MILESTONES
        
        for milestone, features in milestones.items():
            if feature not in features:
                continue
            
            # Parse milestone condition
            if "message" in milestone:
                count = int(milestone.split("_")[1])
                if user_activity.get("message_count", 0) >= count:
                    return True
            elif "session" in milestone:
                count = int(milestone.split("_")[1])
                if user_activity.get("session_count", 0) >= count:
                    return True
            elif "document" in milestone:
                count = int(milestone.split("_")[1])
                if user_activity.get("document_count", 0) >= count:
                    return True
            elif "week" in milestone:
                days = int(milestone.split("_")[1])
                if (datetime.utcnow() - user_activity.get("created_at", datetime.utcnow())).days >= days:
                    return True
        
        return False
    
    @staticmethod
    def get_feature_tooltip(feature: str) -> Optional[str]:
        """Gentle intro to a feature when it's revealed."""
        tooltips = {
            "web_search": "💡 Search the web and include results in responses",
            "memory": "💭 Aurix remembers what matters. It's persistent.",
            "research": "📚 Multi-step research that reads sources and synthesizes",
            "agents": "🤖 Hand Aurix a task. It breaks it down and solves it.",
            "compare": "⚖️ Test models blind. See which is actually better.",
            "deep_research": "🔬 Deep research goes further: planning, searching, extracting, synthesizing",
        }
        return tooltips.get(feature)


logger.info("✨ Aurix experience layer loaded — onboarding, wow moments, smart defaults, mobile magic, status glow, quick actions, feature discovery")
