"""workflow.* — multi-step tool pipelines stored in notes."""

from __future__ import annotations

import json
from typing import Any

from .context import ctx_get
from .schema import _p, tool

_WF_KEY = "workflow.definitions"




def _load_workflows(load_notes) -> dict:
    notes = load_notes()
    raw = notes.get(_WF_KEY, "{}")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _save_workflows(save_notes, load_notes, workflows: dict) -> None:
    notes = load_notes()
    notes[_WF_KEY] = json.dumps(workflows)
    save_notes(notes)


def register(registry, ctx) -> None:
    load_notes = ctx_get(ctx, "load_notes")
    save_notes = ctx_get(ctx, "save_notes")
    call_tool = ctx_get(ctx, "call_tool")

    def workflow_define(args: dict) -> str:
        name = args["name"]
        steps = json.loads(args["steps"]) if isinstance(args["steps"], str) else args["steps"]
        if not isinstance(steps, list):
            return json.dumps({"ok": False, "error": "steps must be a JSON array"})
        workflows = _load_workflows(load_notes)
        workflows[name] = {"steps": steps, "created": ctx_get(ctx, "_now_iso")()}
        _save_workflows(save_notes, load_notes, workflows)
        return json.dumps({"ok": True, "name": name, "steps": len(steps)})

    def workflow_list(args: dict) -> str:
        workflows = _load_workflows(load_notes)
        return json.dumps(list(workflows.keys()), indent=2)

    def workflow_run(args: dict) -> str:
        name = args["name"]
        workflows = _load_workflows(load_notes)
        if name not in workflows:
            return json.dumps({"ok": False, "error": f"Workflow '{name}' not found"})
        wf = workflows[name]
        steps = wf["steps"] if isinstance(wf, dict) else wf
        results = []
        for i, step in enumerate(steps):
            tool_name = step.get("tool")
            if not tool_name:
                results.append({"step": i, "ok": False, "error": "missing tool"})
                break
            step_args = step.get("arguments") or step.get("args") or {}
            cond = step.get("condition")
            if cond:
                prev_ok = results and results[-1].get("ok")
                if cond == "prev_ok" and not prev_ok:
                    results.append({"step": i, "skipped": True, "condition": cond})
                    continue
            try:
                out = call_tool(tool_name, step_args)
                ok = True
                if out:
                    try:
                        parsed = json.loads(out)
                        if isinstance(parsed, dict) and parsed.get("ok") is False:
                            ok = False
                    except json.JSONDecodeError:
                        pass
                results.append({"step": i, "tool": tool_name, "ok": ok, "result": out[:2000]})
                if not ok:
                    break
            except Exception as e:
                results.append({"step": i, "tool": tool_name, "ok": False, "error": str(e)})
                break
        return json.dumps({"ok": True, "workflow": name, "results": results}, indent=2)

    def workflow_delete(args: dict) -> str:
        name = args["name"]
        workflows = _load_workflows(load_notes)
        if name not in workflows:
            return json.dumps({"ok": False, "error": f"Workflow '{name}' not found"})
        del workflows[name]
        _save_workflows(save_notes, load_notes, workflows)
        return json.dumps({"ok": True, "deleted": name})

    registry.register_from_def(
        tool("workflow.define", "Define a workflow (steps JSON array).", {
            "name": _p("string", "Workflow name"),
            "steps": _p("string", "JSON array of {tool, arguments} steps"),
        }),
        workflow_define,
    )
    registry.register_from_def(tool("workflow.list", "List workflow names.", {}), workflow_list)
    registry.register_from_def(
        tool("workflow.run", "Run a workflow step by step.", {"name": _p("string", "Workflow name")}),
        workflow_run,
    )
    registry.register_from_def(
        tool("workflow.delete", "Delete a workflow.", {"name": _p("string", "Workflow name")}),
        workflow_delete,
    )