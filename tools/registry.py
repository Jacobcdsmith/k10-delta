from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

# ── Tiers ──────────────────────────────────────────────────────────────────────
# core  — always included in tools/list (high-frequency, essential)
# domain — included when namespace-filtered or tier requested
# rare  — only included on explicit search or filter
VALID_TIERS = ("core", "domain", "rare")
DEFAULT_TIER = "domain"
MAX_DYNAMIC_TOOLS = 32

NAMESPACE_DESCRIPTIONS: dict[str, str] = {
    "memory": "Soul state, episodes, axioms, notes, and semantic memory.",
    "fs": "Workspace map, tree, search, read, write, and delete.",
    "md": "Markdown parse, TOC, extract, frontmatter, and authoring.",
    "exec": "Python and shell execution on the host.",
    "net": "HTTP fetch, web search, and network requests.",
    "mqtt": "Publish and subscribe to MQTT brokers.",
    "system": "Host info, time, and process control.",
    "cron": "Scheduled and repeating background tasks.",
    "hermes": "Delegation to the local Hermes agent.",
    "cognition": "Reflection, episode search, and introspection.",
    "context": "Environment, trajectory, and session context.",
    "emerge": "Hypothesis lifecycle and synthesis.",
    "dream": "Dream engine status, cycles, and logs.",
    "meme": "Axiom potentiation and decay tracking.",
    "probe": "Adversarial probing and vulnerability reports.",
    "self": "Source inspection, validation, patching, and hot-loaded tools.",
    "workflow": "Multi-step task orchestration and pipelines.",
    "skill": "Reusable skill definitions and invocation.",
    "sensorium": "Sensory input aggregation and perception.",
    "sentiment": "Affective state and sentiment analysis.",
    "k10": "K10-Δ core gateway and meta operations.",
    "goal": "Pursuit goals that drive outward action and closure.",
    "identity": "Self-narrative, identity thread, and current focus.",
    "creator": "Time-bounded creator directives and steering.",
}

# Default tier assignments per namespace — can be overridden per-tool
NAMESPACE_DEFAULT_TIERS: dict[str, str] = {
    "memory": "core",
    "fs": "core",
    "exec": "core",
    "system": "core",
    "cognition": "core",
    "context": "core",
    "net": "domain",
    "md": "domain",
    "emerge": "domain",
    "dream": "domain",
    "meme": "domain",
    "goal": "domain",
    "self": "domain",
    "identity": "domain",
    "sensorium": "domain",
    "hermes": "domain",
    "workflow": "rare",
    "skill": "rare",
    "sentiment": "rare",
    "probe": "rare",
    "k10": "rare",
    "creator": "rare",
    "mqtt": "rare",
    "cron": "rare",
}

Handler = Callable[[dict], str]
OnCall = Callable[[str, dict], None]
DispatchFn = Callable[[dict, dict], str]


class NamespaceProxy:
    """Maps namespace.verb(...) calls to tool dispatch in exec/chrono contexts."""

    def __init__(self, namespace: str, dispatch: DispatchFn) -> None:
        self._namespace = namespace
        self._dispatch = dispatch

    def __getattr__(self, verb: str) -> Callable[..., str]:
        tool_name = f"{self._namespace}.{verb}"

        def call(**kwargs: Any) -> str:
            return self._dispatch(tool_name, kwargs)

        call.__name__ = verb
        return call


def build_namespace_proxies(registry: "ToolRegistry", dispatch: DispatchFn) -> dict[str, NamespaceProxy]:
    namespaces = {name.split(".", 1)[0] for name in registry.all_names}
    return {ns: NamespaceProxy(ns, dispatch) for ns in namespaces}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, dict] = {}
        self._handlers: dict[str, Handler] = {}
        self._dynamic: set[str] = set()
        self._tiers: dict[str, str] = {}
        self._call_counts: dict[str, int] = {}
        self._last_used: dict[str, float] = {}
        self._on_call: OnCall | None = None

    def set_on_call(self, callback: OnCall | None) -> None:
        self._on_call = callback

    def register(
        self,
        name: str,
        description: str,
        inputSchema: dict,
        handler: Handler,
        dynamic: bool = False,
        tier: str | None = None,
    ) -> None:
        if not NAME_PATTERN.match(name):
            raise ValueError(f"Invalid tool name '{name}': expected namespace.verb")
        tool_def = {
            "name": name,
            "description": description,
            "inputSchema": inputSchema,
        }
        self._tools[name] = tool_def
        self._handlers[name] = handler
        if dynamic:
            self._dynamic.add(name)
        else:
            self._dynamic.discard(name)
        # Tier: explicit > existing > namespace default
        if tier and tier in VALID_TIERS:
            self._tiers[name] = tier
        elif name not in self._tiers:
            ns = name.split(".", 1)[0]
            self._tiers[name] = NAMESPACE_DEFAULT_TIERS.get(ns, DEFAULT_TIER)
        self._call_counts.setdefault(name, 0)

    def register_from_def(
        self,
        tool_def: dict,
        handler: Handler,
        dynamic: bool = False,
        tier: str | None = None,
    ) -> None:
        # Tier can come from explicit kwarg, tool_def dict, or namespace default
        effective_tier = tier or tool_def.get("tier")
        self.register(
            tool_def["name"],
            tool_def["description"],
            tool_def["inputSchema"],
            handler,
            dynamic=dynamic,
            tier=effective_tier,
        )

    def dispatch(self, name: str, args: dict | None = None) -> str:
        if name not in self._handlers:
            raise KeyError(f"Unknown tool: {name}")
        payload = args or {}
        if self._on_call:
            self._on_call(name, payload)
        self._call_counts[name] = self._call_counts.get(name, 0) + 1
        self._last_used[name] = time.time()
        return self._handlers[name](payload)

    # ── Listing with filtering / pagination ───────────────────────────────────

    def list_tools(
        self,
        namespace: str | None = None,
        tier: str | None = None,
        cursor: int | None = None,
        limit: int | None = None,
        tiers: list[str] | None = None,
    ) -> dict:
        """Return filtered/paginated tool list.

        Args:
            namespace: Filter to a single namespace (e.g. "memory").
            tier: Return only tools of this tier.
            cursor: Offset for pagination (0-indexed).
            limit: Max tools to return. None = no limit (but tier filtering still applies).
            tiers: List of tiers to include (overrides tier param).

        Returns:
            {"tools": [...], "nextCursor": int|None, "total": int}
        """
        names = sorted(self._tools.keys())

        # Filter by namespace
        if namespace:
            prefix = f"{namespace}."
            names = [n for n in names if n.startswith(prefix)]

        # Filter by tier(s)
        active_tiers = set(tiers or ([tier] if tier else []))
        if active_tiers:
            names = [n for n in names if self._tiers.get(n, DEFAULT_TIER) in active_tiers]

        total = len(names)

        # Paginate
        start = cursor or 0
        if limit is not None:
            page = names[start:start + limit]
            next_cursor = start + limit if start + limit < total else None
        else:
            page = names[start:]
            next_cursor = None

        return {
            "tools": [self._tools[n] for n in page],
            "nextCursor": next_cursor,
            "total": total,
        }

    def list_core(self) -> list[dict]:
        """Return all core-tier tools (always sent on initial tools/list)."""
        return [self._tools[n] for n in sorted(self._tools)
                if self._tiers.get(n, DEFAULT_TIER) == "core"]

    # ── Search ────────────────────────────────────────────────────────────────

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Fuzzy search over tool names and descriptions.

        Returns tool defs ranked by relevance (exact prefix match > substring > token).
        """
        q = query.lower().strip()
        if not q:
            return []

        exact: list[str] = []
        prefix: list[str] = []
        substring: list[str] = []
        token: list[str] = []

        q_tokens = set(q.split())

        for name, tool_def in self._tools.items():
            nl = name.lower()
            dl = tool_def["description"].lower()
            combined = f"{nl} {dl}"

            if nl == q or f"{nl} {dl}" == q:
                exact.append(name)
            elif nl.startswith(q) or dl.startswith(q):
                prefix.append(name)
            elif q in nl or q in dl:
                substring.append(name)
            elif q_tokens & set(combined.split()):
                token.append(name)

        ranked = exact + prefix + substring + token
        return [self._tools[n] for n in ranked[:limit]]

    # ── Namespace summaries ───────────────────────────────────────────────────

    def namespaces(self, include_tiers: bool = False) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for name in self._tools:
            ns, _ = name.split(".", 1)
            entry = result.setdefault(ns, {
                "count": 0,
                "tools": [],
                "description": NAMESPACE_DESCRIPTIONS.get(ns, ""),
            })
            entry["count"] += 1
            entry["tools"].append(name)

        if include_tiers:
            for ns, entry in result.items():
                tier_counts: dict[str, int] = {"core": 0, "domain": 0, "rare": 0}
                for tname in entry["tools"]:
                    t = self._tiers.get(tname, DEFAULT_TIER)
                    tier_counts[t] = tier_counts.get(t, 0) + 1
                entry["tiers"] = tier_counts

        for ns in NAMESPACE_DESCRIPTIONS:
            result.setdefault(ns, {
                "count": 0,
                "tools": [],
                "description": NAMESPACE_DESCRIPTIONS[ns],
            })
        return result

    def namespace_summary(self) -> list[dict]:
        """Compact namespace listing for two-phase discovery.

        Returns list sorted by tool count desc, each with name, description,
        count, tier breakdown, and sample tool names.
        """
        ns_map = self.namespaces(include_tiers=True)
        summaries = []
        for ns, entry in ns_map.items():
            if entry["count"] == 0:
                continue
            samples = sorted(entry["tools"])[:3]
            summaries.append({
                "namespace": ns,
                "description": entry["description"],
                "count": entry["count"],
                "tiers": entry.get("tiers", {}),
                "samples": samples,
            })
        summaries.sort(key=lambda s: s["count"], reverse=True)
        return summaries

    # ── Index ─────────────────────────────────────────────────────────────────

    def index(self) -> dict:
        namespaces: dict[str, dict] = {}
        for name, tool_def in self._tools.items():
            ns, verb = name.split(".", 1)
            ns_entry = namespaces.setdefault(ns, {
                "description": NAMESPACE_DESCRIPTIONS.get(ns, ""),
                "tools": {},
            })
            schema = tool_def.get("inputSchema", {})
            ns_entry["tools"][verb] = {
                "description": tool_def["description"],
                "params": schema.get("properties", {}),
                "dynamic": name in self._dynamic,
                "tier": self._tiers.get(name, DEFAULT_TIER),
                "calls": self._call_counts.get(name, 0),
            }
        return {"namespaces": namespaces}

    def resolve(self, partial: str) -> list[str]:
        partial = partial.lower()
        exact = [n for n in self._tools if n.lower() == partial]
        if exact:
            return exact
        if "." not in partial:
            prefix = f"{partial}."
            return sorted(n for n in self._tools if n.lower().startswith(prefix))
        return sorted(n for n in self._tools if n.lower().startswith(partial))

    def get_schema(self, name: str) -> dict:
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")
        return self._tools[name]

    def has(self, name: str) -> bool:
        return name in self._tools

    def unregister(self, name: str) -> bool:
        if name not in self._tools:
            return False
        del self._tools[name]
        del self._handlers[name]
        self._dynamic.discard(name)
        self._tiers.pop(name, None)
        self._call_counts.pop(name, None)
        self._last_used.pop(name, None)
        return True

    # ── Dynamic tool management ───────────────────────────────────────────────

    def evict_dynamic(self) -> str | None:
        """Evict the least-used dynamic tool if at capacity. Returns evicted name or None."""
        if len(self._dynamic) < MAX_DYNAMIC_TOOLS:
            return None
        # Find least-used dynamic tool (by call count, then last_used)
        candidates = sorted(
            self._dynamic,
            key=lambda n: (self._call_counts.get(n, 0), self._last_used.get(n, 0)),
        )
        if not candidates:
            return None
        victim = candidates[0]
        self.unregister(victim)
        return victim

    def promote_hot_tools(self, threshold: int = 10) -> list[str]:
        """Promote frequently-used domain/rare tools to core tier."""
        promoted = []
        for name in list(self._tools.keys()):
            tier = self._tiers.get(name, DEFAULT_TIER)
            if tier != "core" and self._call_counts.get(name, 0) >= threshold:
                self._tiers[name] = "core"
                promoted.append(name)
        return promoted

    def demote_cold_tools(self, threshold: int = 0, max_age_s: float = 86400) -> list[str]:
        """Demote unused core tools back to domain tier."""
        now = time.time()
        demoted = []
        for name in list(self._tools.keys()):
            if name in self._dynamic:
                continue  # never auto-demote dynamic tools
            tier = self._tiers.get(name, DEFAULT_TIER)
            if tier == "core":
                calls = self._call_counts.get(name, 0)
                last = self._last_used.get(name, 0)
                age = now - last if last else float("inf")
                if calls <= threshold and age > max_age_s:
                    self._tiers[name] = "domain"
                    demoted.append(name)
        return demoted

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def tool_count(self) -> int:
        return len(self._tools)

    @property
    def dynamic_count(self) -> int:
        return len(self._dynamic)

    @property
    def all_names(self) -> list[str]:
        return sorted(self._tools)

    @property
    def all_tools(self) -> list[dict]:
        """Flat list of all tool defs (for legacy callers expecting a list)."""
        return [self._tools[n] for n in sorted(self._tools)]

    def get_usage_stats(self) -> dict:
        """Return tool usage statistics for monitoring."""
        return {
            "total_tools": self.tool_count,
            "dynamic_tools": self.dynamic_count,
            "max_dynamic": MAX_DYNAMIC_TOOLS,
            "tier_counts": {
                t: sum(1 for v in self._tiers.values() if v == t)
                for t in VALID_TIERS
            },
            "top_tools": sorted(
                self._call_counts.items(), key=lambda x: x[1], reverse=True
            )[:10],
            "never_called": [
                n for n in self._tools if self._call_counts.get(n, 0) == 0
            ],
        }
