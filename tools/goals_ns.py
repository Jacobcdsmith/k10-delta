"""goal.* and memory.creator_feedback tool handlers."""

from __future__ import annotations

import json
from typing import Any

from goals_pursuit import GoalPursuitService

from .context import ctx_get
from .schema import _p, tool


def register(registry, ctx) -> None:
    soul = ctx_get(ctx, "soul")
    save_soul = ctx_get(ctx, "save_soul")
    state_lock = ctx_get(ctx, "state_lock")
    goals = ctx_get(ctx, "goals")
    call_tool = ctx_get(ctx, "call_tool")
    _now_iso = ctx_get(ctx, "_now_iso")

    pursuit = GoalPursuitService(goals, call_tool)

    def goal_add(args: dict) -> str:
        return json.dumps(
            goals.add(
                args["text"],
                source=args.get("source", "agent"),
                priority=int(args.get("priority", 5)),
                kind=args.get("kind", "open"),
            ),
            indent=2,
        )

    def goal_list(args: dict) -> str:
        return json.dumps(goals.list_all(args.get("status")), indent=2)

    def goal_get(args: dict) -> str:
        try:
            return json.dumps(goals.get(args["id"]), indent=2)
        except KeyError:
            return json.dumps({"ok": False, "error": f"Goal not found: {args['id']}"})

    def goal_update(args: dict) -> str:
        fields = {
            k: args[k]
            for k in ("text", "status", "priority", "source", "kind")
            if k in args
        }
        try:
            return json.dumps(goals.update(args["id"], **fields), indent=2)
        except KeyError:
            return json.dumps({"ok": False, "error": f"Goal not found: {args['id']}"})

    def goal_complete(args: dict) -> str:
        try:
            return json.dumps(
                goals.complete(args["id"], args.get("evidence", "")), indent=2)
        except KeyError:
            return json.dumps({"ok": False, "error": f"Goal not found: {args['id']}"})

    def goal_plan(args: dict) -> str:
        return pursuit.plan(args)

    def goal_evaluate(args: dict) -> str:
        return pursuit.evaluate(args)

    def goal_pursue(args: dict) -> str:
        return pursuit.pursue(args)

    def goal_stall(args: dict) -> str:
        gid = args["id"]
        reason = args.get("reason", "")
        if not goals.exists(gid):
            return json.dumps({"ok": False, "error": f"Goal not found: {gid}"}, indent=2)
        goals.mark_stalled(gid, reason)
        return json.dumps({"ok": True, "goal": goals.get(gid)}, indent=2)

    def goal_delete(args: dict) -> str:
        gid = args["id"]
        if not goals.exists(gid):
            return json.dumps({"ok": False, "error": f"Goal not found: {gid}"}, indent=2)
        deleted = goals.delete(gid)
        return json.dumps({"ok": True, "deleted": deleted}, indent=2)

    def creator_feedback(args: dict) -> str:
        entry = {
            "ts": _now_iso(),
            "text": args["text"].strip(),
            "rating": args.get("rating"),
            "episode_ref": args.get("episode_ref"),
            "author": "Jacob",
        }
        # Automatic secondary signal alongside the human-set `rating` --
        # sentiment.analyze was fully functional but never called by
        # anything in the pipeline. Never let a sentiment hiccup block
        # recording the feedback itself.
        try:
            sentiment_raw = call_tool("sentiment.analyze", {"text": entry["text"]})
            sentiment = json.loads(sentiment_raw)
            entry["sentiment"] = {"score": sentiment.get("score"), "label": sentiment.get("label")}
        except Exception:
            entry["sentiment"] = None
        with state_lock:
            soul.setdefault("creator_feedback", []).append(entry)
            soul["creator_feedback"] = soul["creator_feedback"][-50:]
            save_soul(soul)
        return json.dumps({"ok": True, "entry": entry}, indent=2)

    def creator_feedback_list(args: dict) -> str:
        n = min(int(args.get("count", 20)), 50)
        return json.dumps(soul.get("creator_feedback", [])[-n:], indent=2)

    registry.register_from_def(
        tool("goal.add", "Add a pursuit goal.", {
            "text": _p("string", "Goal description"),
            "source": _p("string", "Source (user|agent|migrated)", required=False),
            "priority": _p("integer", "Priority 1-10 (default 5)", required=False),
            "kind": _p(
                "string",
                "Goal kind: open|general|research|delivery (default open)",
                required=False,
            ),
        }),
        goal_add,
    )
    registry.register_from_def(
        tool("goal.list", "List goals.", {
            "status": _p("string", "open|in_progress|done|cancelled", required=False),
        }),
        goal_list,
    )
    registry.register_from_def(
        tool("goal.get", "Get goal by ID.", {"id": _p("string", "Goal ID")}),
        goal_get,
    )
    registry.register_from_def(
        tool("goal.update", "Update goal fields.", {
            "id": _p("string", "Goal ID"),
            "text": _p("string", "New text", required=False),
            "status": _p("string", "Status", required=False),
            "priority": _p("integer", "Priority", required=False),
            "kind": _p("string", "open|general|research|delivery", required=False),
        }),
        goal_update,
    )
    registry.register_from_def(
        tool("goal.complete", "Mark goal done.", {
            "id": _p("string", "Goal ID"),
            "evidence": _p("string", "Completion note", required=False),
        }),
        goal_complete,
    )
    registry.register_from_def(
        tool("goal.delete", "Permanently delete a goal.", {
            "id": _p("string", "Goal ID"),
        }),
        goal_delete,
    )
    registry.register_from_def(
        tool("goal.plan", "Decompose a goal into ordered pursuit steps.", {
            "id": _p("string", "Goal ID (default: top open)", required=False),
            "max_steps": _p("integer", "Max steps (default 5)", required=False),
        }),
        goal_plan,
    )
    registry.register_from_def(
        tool("goal.evaluate", "Score whether recent evidence advanced the goal.", {
            "id": _p("string", "Goal ID"),
            "step_result": _p("string", "Optional result from last step", required=False),
        }),
        goal_evaluate,
    )
    registry.register_from_def(
        tool("goal.pursue", "Take one step on the top open goal.", {
            "step": _p(
                "string",
                "auto|search|fetch|synthesise|hermes|deliver|plan|tool",
                required=False,
            ),
            "id": _p("string", "Optional goal ID (default: top open)", required=False),
            "tool": _p("string", "Tool name when step=tool", required=False),
            "args": _p("object", "Tool args when step=tool", required=False),
        }),
        goal_pursue,
    )
    registry.register_from_def(
        tool("memory.creator_feedback", "Jacob rates or steers an episode/outcome.", {
            "text": _p("string", "Feedback text"),
            "rating": _p("string", "substantive|noise|pivot", required=False),
            "episode_ref": _p("string", "Episode summary ref", required=False),
        }),
        creator_feedback,
    )
    registry.register_from_def(
        tool("memory.creator_feedback_list", "Recent creator feedback.", {
            "count": _p("integer", "Count (default 20)", required=False),
        }),
        creator_feedback_list,
    )
    registry.register_from_def(
        tool("goal.stall", "Mark a goal as stalled with an optional reason.",
             {"id": _p("string", "Goal ID"), "reason": _p("string", "Reason for stalling", required=False)}),
        goal_stall,
    )

    # ── State machine tools ───────────────────────────────────────────────────

    def goal_review(args: dict) -> str:
        """Manual sign-off: advance a goal from REVIEW to COMPLETED, CANCELLED, or back to ACTIVE."""
        gid = args["id"]
        if not goals.exists(gid):
            return json.dumps({"ok": False, "error": f"Goal not found: {gid}"})
        decision = args.get("decision", "").lower()
        note = args.get("note", "")
        if decision == "complete":
            result = goals.mark_completed(gid, note)
        elif decision == "cancel":
            result = goals.mark_cancelled(gid, note)
        elif decision == "continue":
            result = goals.mark_active(gid)
        elif decision == "supersede":
            by_id = args.get("superseded_by", "")
            if not by_id:
                return json.dumps({"ok": False, "error": "superseded_by required when decision=supersede"})
            result = goals.mark_superseded(gid, by_id)
        else:
            # Auto-check completion criteria
            if goals.evaluate_completion(gid):
                result = goals.mark_completed(gid, "auto-completed via criteria")
            else:
                return json.dumps({
                    "ok": False,
                    "status": "review",
                    "message": "Completion criteria not met. Use decision=complete|cancel|continue|supersede.",
                    "goal": goals.get(gid),
                })
        return json.dumps({"ok": True, "goal": result}, indent=2)

    def goal_cancel(args: dict) -> str:
        gid = args["id"]
        if not goals.exists(gid):
            return json.dumps({"ok": False, "error": f"Goal not found: {gid}"})
        result = goals.mark_cancelled(gid, args.get("reason", ""))
        return json.dumps({"ok": True, "goal": result}, indent=2)

    def goal_supersede(args: dict) -> str:
        gid = args["id"]
        by_id = args["superseded_by"]
        if not goals.exists(gid):
            return json.dumps({"ok": False, "error": f"Goal not found: {gid}"})
        result = goals.mark_superseded(gid, by_id)
        return json.dumps({"ok": True, "goal": result}, indent=2)

    def goal_archive(args: dict) -> str:
        max_age = float(args.get("max_age_days", 7))
        deleted = goals.auto_archive(max_age_days=max_age)
        revived = goals.sweep_revivals(cooldown_hours=float(args.get("revival_cooldown_hours", 1.0)))
        return json.dumps({"ok": True, "archived": deleted, "revived": revived}, indent=2)

    def goal_generate_plan(args: dict) -> str:
        gid = args["id"]
        if not goals.exists(gid):
            return json.dumps({"ok": False, "error": f"Goal not found: {gid}"})
        steps = goals.generate_plan(gid)
        return json.dumps({"ok": True, "plan": steps}, indent=2)

    registry.register_from_def(
        tool("goal.review", "Sign off on a goal in REVIEW state.",
             {
                 "id": _p("string", "Goal ID"),
                 "decision": _p("string", "complete|cancel|continue|supersede (omit to auto-check criteria)", required=False),
                 "note": _p("string", "Optional note or evidence", required=False),
                 "superseded_by": _p("string", "ID of superseding goal (when decision=supersede)", required=False),
             }),
        goal_review,
    )
    registry.register_from_def(
        tool("goal.cancel", "Explicitly cancel a goal.",
             {
                 "id": _p("string", "Goal ID"),
                 "reason": _p("string", "Cancellation reason", required=False),
             }),
        goal_cancel,
    )
    registry.register_from_def(
        tool("goal.supersede", "Mark a goal as superseded by a newer goal.",
             {
                 "id": _p("string", "Goal ID to supersede"),
                 "superseded_by": _p("string", "ID of the newer goal"),
             }),
        goal_supersede,
    )
    registry.register_from_def(
        tool("goal.archive", "Auto-archive old terminal goals and sweep stalled goals for revival.",
             {
                 "max_age_days": _p("number", "Archive terminal goals older than N days (default 7)", required=False),
                 "revival_cooldown_hours": _p("number", "Promote stalled goals to revival after N hours (default 1)", required=False),
             }),
        goal_archive,
    )
    registry.register_from_def(
        tool("goal.generate_plan", "Assign the default plan steps for a goal's kind.",
             {"id": _p("string", "Goal ID")}),
        goal_generate_plan,
    )
