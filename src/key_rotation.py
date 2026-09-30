"""src/key_rotation.py - multi-key quota rotation for hosted OpenAI-compatible providers.

Idea from codex-model-router (github.com/DrOetker747/codex-model-router, MIT): when one key hits its
limit (401 / 402 / 429) mark it spent, move to the next configured key, and let the spent one cool down
for 10 minutes before it is tried again.

Keys come from the environment only - never from request data:
    OPENROUTER_API_KEYS=key1,key2,...     (OPENROUTER_API_KEY is folded in as the first key)
    OPENCODE_ZEN_API_KEYS=key1,key2,...   (OPENCODE_ZEN_API_KEY likewise)

Wiring (src/llm_core.py):
  * `apply(provider, headers)` fills in `Authorization` when the endpoint has no key of its own, and swaps
    in a live key when the one configured on the endpoint is a pool key that is cooling down.
  * `report(provider, headers, status)` puts the key that was just used on cooldown after 401/402/429.
An endpoint with its own non-pool key is never touched.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Dict, List, Optional

COOLDOWN_SECONDS = 600.0
ROTATE_ON = frozenset({401, 402, 429})
MAX_KEYS = 5

_ENV = {
    "openrouter": ("OPENROUTER_API_KEYS", "OPENROUTER_API_KEY"),
    "opencode": ("OPENCODE_ZEN_API_KEYS", "OPENCODE_ZEN_API_KEY"),
}


class KeyPool:
    def __init__(self, keys: List[str], cooldown: float = COOLDOWN_SECONDS, clock=time.monotonic):
        seen: List[str] = []
        for k in keys:
            k = (k or "").strip()
            if k and k not in seen:
                seen.append(k)
        self.keys = seen[:MAX_KEYS]
        self.cooldown = cooldown
        self._clock = clock
        self._spent: Dict[str, float] = {}
        self._idx = 0
        self._lock = threading.Lock()

    def __contains__(self, key: str) -> bool:
        return key in self.keys

    def _live(self, key: str) -> bool:
        until = self._spent.get(key)
        return until is None or self._clock() >= until

    def current(self) -> Optional[str]:
        """The key to use now: the first live one from the current position, else the one that frees up soonest."""
        with self._lock:
            if not self.keys:
                return None
            n = len(self.keys)
            for i in range(n):
                k = self.keys[(self._idx + i) % n]
                if self._live(k):
                    self._idx = (self._idx + i) % n
                    return k
            return min(self.keys, key=lambda k: self._spent.get(k, 0.0))

    def is_cooling(self, key: str) -> bool:
        with self._lock:
            return key in self.keys and not self._live(key)

    def mark_spent(self, key: str) -> None:
        with self._lock:
            if key not in self.keys:
                return
            self._spent[key] = self._clock() + self.cooldown
            if self.keys[self._idx] == key:
                self._idx = (self._idx + 1) % len(self.keys)


_pools: Dict[str, Optional[KeyPool]] = {}
_pools_lock = threading.Lock()


def _keys_from_env(provider: str) -> List[str]:
    names = _ENV.get(provider)
    if not names:
        return []
    many, single = names
    keys = [os.environ.get(single, "")]
    keys += (os.environ.get(many, "") or "").split(",")
    return [k.strip() for k in keys if k and k.strip()]


def pool(provider: str) -> Optional[KeyPool]:
    """The provider's key pool, or None when no key is configured in the environment."""
    with _pools_lock:
        if provider not in _pools:
            keys = _keys_from_env(provider)
            _pools[provider] = KeyPool(keys) if keys else None
        return _pools[provider]


def reset() -> None:
    """Forget cached pools (tests, or after the environment changes)."""
    with _pools_lock:
        _pools.clear()


def _bearer(headers: Dict[str, str]) -> str:
    for name, value in headers.items():
        if name.lower() == "authorization" and isinstance(value, str) and value.lower().startswith("bearer "):
            return value[7:].strip()
    return ""


def _set_bearer(headers: Dict[str, str], key: str) -> None:
    for name in [n for n in headers if n.lower() == "authorization"]:
        del headers[name]
    headers["Authorization"] = f"Bearer {key}"


def apply(provider: str, headers: Dict[str, str]) -> Dict[str, str]:
    """Fill or swap the bearer key from the provider's pool (in place; also returned)."""
    p = pool(provider)
    if p is None:
        return headers
    used = _bearer(headers)
    if used and used not in p:
        return headers          # the endpoint's own key: not ours to rotate
    if not used or p.is_cooling(used):
        key = p.current()
        if key:
            _set_bearer(headers, key)
    return headers


def report(provider: str, headers: Dict[str, str], status: int) -> bool:
    """After an upstream error: cool the key that was used if the status means quota/auth. True if rotated."""
    if status not in ROTATE_ON:
        return False
    p = pool(provider)
    used = _bearer(headers or {})
    if p is None or not used or used not in p:
        return False
    p.mark_spent(used)
    return True
