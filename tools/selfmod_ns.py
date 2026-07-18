"""self.* tool handlers."""

from __future__ import annotations

import json
import re
from typing import Any

from .context import ctx_get
from .schema import _p, tool

_GAP_STOPWORDS = {
    "the", "a", "an", "is", "it", "in", "on", "at", "to", "of", "and", "or", "for",
    "with", "this", "that", "was", "are", "be", "been", "by", "as", "from",
    "have", "has", "had", "do", "did", "will", "can", "not", "but", "so", "if", "then",
}

_CAPABILITY_HINTS: dict[str, list[str]] = {
    "code": ["exec.python", "exec.shell"],
    "python": ["exec.python"],
    "shell": ["exec.shell"],
    "command": ["exec.shell"],
    "fetch": ["net.fetch"],
    "http": ["net.fetch"],
    "url": ["net.fetch"],
    "write": ["fs.write"],
    "file": ["fs.write", "fs.read", "fs.list"],
    "read": ["fs.read"],
    "list": ["fs.list"],
    "synthesise": ["emerge.synthesise"],
    "synthesize": ["emerge.synthesise"],
    "research": ["emerge.synthesise", "net.fetch"],
    "hermes": ["hermes.query"],
    "delegate": ["hermes.query"],
    "generate": ["hermes.query", "emerge.synthesise", "exec.python"],
    "implement": ["hermes.query", "exec.python", "self.patch"],
    "schedule": ["cron.schedule"],
    "cron": ["cron.schedule"],
    "goal": ["goal.pursue", "goal.list"],
}




def _goal_keywords(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z0-9]{3,}", text.lower())
    return [w for w in words if w not in _GAP_STOPWORDS]


def register(registry, ctx) -> None:
    soul = ctx_get(ctx, "soul")
    engine = ctx_get(ctx, "engine")
    dream = ctx_get(ctx, "dream")
    memetic = ctx_get(ctx, "memetic")
    prober = ctx_get(ctx, "prober")
    selfmod = ctx_get(ctx, "selfmod")
    goals = ctx_get(ctx, "goals")

    def read_source(args: dict) -> str:
        return selfmod.read_source(args["path"])

    def list_sources(args: dict) -> str:
        return json.dumps(selfmod.list_sources(), indent=2)

    def validate(args: dict) -> str:
        return json.dumps(selfmod.validate_python(args["code"]), indent=2)

    def backup(args: dict) -> str:
        path = selfmod.backup_source(args["path"])
        return json.dumps({"ok": True, "backup": path})

    def patch(args: dict) -> str:
        return json.dumps(
            selfmod.patch_source(args["path"], args["old_str"], args["new_str"]),
            indent=2,
        )

    def ast_replace_function(args: dict) -> str:
        return json.dumps(
            selfmod.ast_replace_function(args["path"], args["func_name"], args["new_code"]),
            indent=2,
        )

    def propose_tool(args: dict) -> str:
        schema = json.loads(args["schema"]) if isinstance(args["schema"], str) else args["schema"]
        return json.dumps(
            selfmod.propose_tool(
                args["name"], args["description"], schema, args["handler_code"],
                tier=args.get("tier"),
            ),
            indent=2,
        )

    def commit_tool(args: dict) -> str:
        return json.dumps(selfmod.commit_tool(args["name"]), indent=2)

    def list_staged(args: dict) -> str:
        return json.dumps(selfmod.list_staged(), indent=2)

    def get_tool_schema(args: dict) -> str:
        return json.dumps(selfmod.get_tool_schema(args["name"]), indent=2)

    def introspect(args: dict) -> str:
        engine_stats = {
            "reflection_cycles": engine._cycle,
            "dream_cycles": dream._dream_count,
            "memetic_cycles": memetic._cycle,
            "probe_cycles": prober._probe_cycle,
        }
        return json.dumps(
            selfmod.introspect_self(soul, engine_stats, registry=registry),
            indent=2,
        )

    def list_namespaces(args: dict) -> str:
        return json.dumps(registry.namespace_summary(), indent=2)

    def list_tools(args: dict) -> str:
        return json.dumps(
            registry.list_tools(
                namespace=args.get("namespace"),
                tier=args.get("tier"),
                tiers=args.get("tiers"),
                cursor=args.get("cursor"),
                limit=args.get("limit"),
            ),
            indent=2,
        )

    def tool_index(args: dict) -> str:
        return json.dumps(registry.index(), indent=2)

    def search_tools(args: dict) -> str:
        return json.dumps(
            registry.search(args["query"], limit=int(args.get("limit", 20))),
            indent=2,
        )

    def revoke_tool(args: dict) -> str:
        return json.dumps(
            selfmod.revoke_tool(args["name"], unregister_fn=registry.unregister),
            indent=2,
        )

    def usage_stats(args: dict) -> str:
        return json.dumps(registry.get_usage_stats(), indent=2)

    def promote_hot(args: dict) -> str:
        threshold = int(args.get("threshold", 10))
        promoted = registry.promote_hot_tools(threshold=threshold)
        return json.dumps({"promoted": promoted, "threshold": threshold}, indent=2)

    def demote_cold(args: dict) -> str:
        threshold = int(args.get("threshold", 0))
        max_age = float(args.get("max_age_s", 86400))
        demoted = registry.demote_cold_tools(threshold=threshold, max_age_s=max_age)
        return json.dumps({"demoted": demoted, "threshold": threshold, "max_age_s": max_age}, indent=2)

    def gap_analysis(args: dict) -> str:
        goal_id = args["goal_id"]
        try:
            goal = goals.get(goal_id)
        except KeyError:
            return json.dumps({"error": f"Goal not found: {goal_id}"})

        goal_text = goal["text"]
        keywords = _goal_keywords(goal_text)
        idx = registry.index()

        have: list[str] = []
        matched_kw: set[str] = set()

        for ns, ns_data in idx["namespaces"].items():
            ns_haystack = f"{ns} {ns_data.get('description', '')}".lower()
            for kw in keywords:
                if kw in ns_haystack and ns not in have:
                    matched_kw.add(kw)
                    have.append(ns)

            for verb, tool_data in ns_data.get("tools", {}).items():
                tool_name = f"{ns}.{verb}"
                params = " ".join(tool_data.get("params", {}).keys())
                tool_haystack = (
                    f"{tool_name} {tool_data.get('description', '')} {params}"
                ).lower()
                for kw in keywords:
                    if kw in tool_haystack and tool_name not in have:
                        matched_kw.add(kw)
                        have.append(tool_name)

        for kw in keywords:
            for tool_name in _CAPABILITY_HINTS.get(kw, []):
                if tool_name not in have:
                    have.append(tool_name)
                matched_kw.add(kw)

        need = [f"no tool coverage for '{kw}'" for kw in keywords if kw not in matched_kw]

        suggested_actions: list[dict] = []
        if any(kw in keywords for kw in ("generate", "implement", "complex", "deliver")):
            suggested_actions.append({
                "tool": "hermes.query",
                "args": {
                    "prompt": f"Deliver concrete output for goal: {goal_text}",
                    "timeout": 180,
                },
            })
        if any(kw in keywords for kw in ("code", "python", "implement", "generate", "example")):
            suggested_actions.append({
                "tool": "fs.write",
                "args": {
                    "path": f"goal_{goal_id}.md",
                    "content": f"# Goal {goal_id}\n\n{goal_text}\n",
                },
            })
        if any(kw in keywords for kw in ("research", "analyze", "analyse", "synthesise", "synthesize")):
            suggested_actions.append({
                "tool": "emerge.synthesise",
                "args": {"topic": goal_text},
            })
        if any(kw in keywords for kw in ("fetch", "http", "url", "download", "research")):
            suggested_actions.append({
                "tool": "net.search",
                "args": {"query": goal_text[:120], "count": 5},
            })
        if need:
            suggested_actions.append({
                "tool": "goal.pursue",
                "args": {"step": "hermes"},
            })
        if not suggested_actions:
            suggested_actions.append({
                "tool": "goal.pursue",
                "args": {"step": "auto"},
            })

        return json.dumps(
            {
                "goal_id": goal_id,
                "goal_text": goal_text,
                "have": have,
                "need": need,
                "suggested_actions": suggested_actions,
            },
            indent=2,
        )

    registry.register_from_def(
        tool(
            "self.read_source",
            "Read a source file from anywhere on the filesystem.",
            {"path": _p("string", "Path to the file")},
        ),
        read_source,
    )
    registry.register_from_def(tool("self.list_sources", "List all source files with sizes and line counts."), list_sources)
    registry.register_from_def(
        tool(
            "self.validate",
            "Syntax-check Python code without running it.",
            {"code": _p("string", "Python source")},
        ),
        validate,
    )
    registry.register_from_def(
        tool(
            "self.backup",
            "Snapshot a source file to soul_history/ before modification.",
            {"path": _p("string", "Filename")},
        ),
        backup,
    )
    registry.register_from_def(
        tool(
            "self.patch",
            "Replace a string in a source file (auto-backs up first).",
            {
                "path": _p("string", "Filename"),
                "old_str": _p("string", "Text to replace"),
                "new_str": _p("string", "Replacement text"),
            },
        ),
        patch,
    )
    registry.register_from_def(
        tool(
            "self.ast_replace_function",
            "Replace a function definition semantically using AST. Preserves formatting and comments outside the target function.",
            {
                "path": _p("string", "Relative path to the python file."),
                "func_name": _p("string", "Name of the function to replace."),
                "new_code": _p("string", "Full new code for the function including 'def'."),
            },
        ),
        ast_replace_function,
    )
    registry.register_from_def(
        tool(
            "self.propose_tool",
            "Stage a new tool for hot-loading.",
            {
                "name": _p("string", "Tool name (namespace.verb)"),
                "description": _p("string", "What the tool does"),
                "schema": _p("string", "JSON inputSchema object"),
                "handler_code": _p("string", "Python defining handler(arguments)->str"),
                "tier": _p("string", "Tool tier: core, domain, or rare (default: domain)", required=False),
            },
        ),
        propose_tool,
    )
    registry.register_from_def(
        tool(
            "self.commit_tool",
            "Hot-load a staged tool into the running server.",
            {"name": _p("string", "Tool name")},
        ),
        commit_tool,
    )
    registry.register_from_def(tool("self.list_staged", "List staged (uncommitted) tools."), list_staged)
    registry.register_from_def(
        tool(
            "self.get_tool_schema",
            "Get schema for any registered tool.",
            {"name": _p("string", "Tool name")},
        ),
        get_tool_schema,
    )
    registry.register_from_def(
        tool("self.introspect", "Full self-portrait: sources, tools, engines, state."),
        introspect,
    )
    registry.register_from_def(tool("self.list_namespaces", "Namespace summaries with tier breakdown and sample tools."), list_namespaces)
    registry.register_from_def(
        tool(
            "self.list_tools",
            "List tools with filtering by namespace, tier, and pagination.",
            {
                "namespace": _p("string", "Filter to namespace (e.g. memory)", required=False),
                "tier": _p("string", "Filter to tier: core, domain, rare", required=False),
                "tiers": _p("array", "List of tiers to include", required=False),
                "cursor": _p("integer", "Pagination offset (0-indexed)", required=False),
                "limit": _p("integer", "Max tools to return", required=False),
            },
        ),
        list_tools,
    )
    registry.register_from_def(tool("self.tool_index", "Full registry index with params."), tool_index)
    registry.register_from_def(
        tool(
            "self.search_tools",
            "Search registered tools by name or description.",
            {
                "query": _p("string", "Search query"),
                "limit": _p("integer", "Max results (default 20)", required=False),
            },
        ),
        search_tools,
    )
    registry.register_from_def(
        tool(
            "self.revoke_tool",
            "Unregister a hot-loaded dynamic tool.",
            {"name": _p("string", "Tool name")},
        ),
        revoke_tool,
    )
    registry.register_from_def(tool("self.usage_stats", "Tool usage statistics: top tools, tier counts, never-called."), usage_stats)
    registry.register_from_def(
        tool(
            "self.promote_hot",
            "Promote frequently-used tools to core tier.",
            {"threshold": _p("integer", "Min call count to promote (default 10)", required=False)},
        ),
        promote_hot,
    )
    registry.register_from_def(
        tool(
            "self.demote_cold",
            "Demote unused core tools back to domain tier.",
            {
                "threshold": _p("integer", "Max call count to demote (default 0)", required=False),
                "max_age_s": _p("number", "Max seconds since last use (default 86400)", required=False),
            },
        ),
        demote_cold,
    )
    registry.register_from_def(
        tool(
            "self.gap_analysis",
            "Compare a goal against registered tools to find capability gaps.",
            {"goal_id": _p("string", "Goal ID")},
        ),
        gap_analysis,
    )