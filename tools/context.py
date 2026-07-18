"""HostContext — typed-ish bag for tool registration."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class SupportsGoals(Protocol):
    def list_all(self, status: str | None = None) -> list: ...
    def top_open(self) -> dict | None: ...


@dataclass
class HostContext:
    """Explicit host services exposed to tool modules."""

    soul: dict
    save_soul: Callable[[dict], None]
    read_episodes: Callable[..., list]
    append_episode: Callable[[dict], None]
    load_notes: Callable[[], dict]
    save_notes: Callable[[dict], None]
    state_lock: Any
    hypotheses: Any
    goals: Any
    call_tool: Callable[..., str]
    engine: Any
    dream: Any
    memetic: Any
    prober: Any
    selfmod: Any
    chrono: Any
    hermes_bridge: Any
    registry: Any
    WORKSPACE: Any
    EPISODES_PATH: Any = None
    DREAM_LOG: Any = None
    _now_iso: Callable[[], str] | None = None
    _start_time: float | None = None
    extras: dict = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        return self.extras.get(key, default)


def ctx_get(ctx: Any, key: str) -> Any:
    """Canonical accessor — replaces per-module `_c` copies over time."""
    if hasattr(ctx, key):
        return getattr(ctx, key)
    if isinstance(ctx, dict):
        return ctx[key]
    raise KeyError(key)
