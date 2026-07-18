"""act.* tool handlers."""

from __future__ import annotations

import json
from typing import Any

from .context import ctx_get
from .schema import _p, tool




def register(registry, ctx) -> None:
    goals = ctx_get(ctx, "goals")
    call_tool = ctx_get(ctx, "call_tool")

    def act_deliver(args: dict) -> str:
        goal_id = args["goal_id"]
        path = args["path"]
        content = args["content"]
        summary = args["summary"]
        write_result = call_tool("fs.write", {"path": path, "content": content})
        goals.add_evidence(goal_id, f"deliver: {summary} -> {path}")
        call_tool(
            "memory.log_episode",
            {
                "summary": summary,
                "detail": json.dumps({
                    "goal_id": goal_id,
                    "path": path,
                    "action": "act.deliver",
                }),
            },
        )
        return json.dumps(
            {
                "ok": True,
                "goal_id": goal_id,
                "path": path,
                "write": json.loads(write_result),
                "summary": summary,
            },
            indent=2,
        )

    registry.register_from_def(
        tool(
            "act.deliver",
            "Write artifact, log goal evidence and episode.",
            {
                "goal_id": _p("string", "Goal ID"),
                "path": _p("string", "Artifact path"),
                "content": _p("string", "File content"),
                "summary": _p("string", "Delivery summary"),
            },
        ),
        act_deliver,
    )