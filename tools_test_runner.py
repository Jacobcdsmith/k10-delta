#!/usr/bin/env python3
"""Smoke-test every registered tool with safe minimal args."""

from __future__ import annotations

import json
import time
import traceback
from pathlib import Path

import host

SKIP = {
    "system.restart",
    "self.patch",
    "self.ast_replace_function",
    "self.propose_tool",
    "self.commit_tool",
    "self.revoke_tool",
    "hermes.query",  # 120s+ block
    "dream.force",
    "exec.shell",
    "mqtt.subscribe",  # blocks up to timeout
}

DEFAULTS: dict[str, dict] = {


    "memory.mutate_axiom": {"index": 0, "new_text": "mutated test"},
    "memory.log_episode": {"summary": "smoke test episode", "detail": "{}"},
    "memory.compress_episodes": {"count": 5, "summary": "smoke compress"},
    "memory.note_set": {"key": "_smoke_test", "value": "ok"},
    "memory.note_get": {"key": "_smoke_test"},
    "memory.note_delete": {"key": "_smoke_test"},
    "memory.semantic_set": {"key": "_smoke", "value": "1"},
    "memory.semantic_get": {"key": "_smoke"},
    "memory.set_field": {"key": "identity", "value": "\"K10-Δ\""},
    "memory.creator_feedback": {"text": "smoke test", "rating": "substantive"},
    "fs.write": {"path": "_smoke_test.txt", "content": "smoke"},
    "fs.read": {"path": "_smoke_test.txt"},
    "fs.list": {"path": "."},
    "fs.delete": {"path": "_smoke_test.txt"},
    "exec.python": {"code": "print('smoke')"},
    "net.fetch": {"url": "https://example.com"},
    "net.search": {"query": "esp32", "count": 2},
    "mqtt.publish": {"topic": "k10/smoke", "payload": "test", "broker": "test.mosquitto.org"},
    "cron.schedule": {"id": "_smoke_task", "interval": 86400, "code": "print('smoke')"},
    "cron.remove": {"id": "_smoke_task"},
    "goal.add": {"text": "_smoke goal delete me", "priority": 1},
    "goal.list": {},
    "goal.get": {"id": "8616065f"},
    "goal.plan": {"id": "8616065f", "max_steps": 3},
    "goal.evaluate": {"id": "8616065f"},
    "goal.pursue": {"step": "plan"},
    "goal.update": {"id": "8616065f", "priority": 10},
    "goal.complete": {"id": "_nonexistent"},
    "act.deliver": {
        "goal_id": "8616065f",
        "path": "_smoke_deliver.md",
        "content": "# smoke",
        "summary": "smoke deliver",
    },
    "k10.python_exec": {"code": "print('smoke')"},
    "self.get_tool_schema": {"name": "goal.list"},
    "self.read_source": {"path": "host.py"},
    "self.backup": {"path": "host.py"},
    "identity.update": {"focus": "smoke test focus"},
    "creator.steer": {"directive": "smoke test directive", "expires_hours": 1},
    "emerge.spawn_hypothesis": {"text": "_smoke hypothesis"},
    "emerge.list_hypotheses": {"status": "open"},
    "emerge.get_hypothesis": {"id": "dummy"},
    "emerge.add_evidence": {"id": "92f8ee33", "evidence": "smoke", "supports": True},
    "emerge.synthesise": {"topic": "smoke test"},
    "emerge.auto_test": {},
    "cognition.reflect": {},
    "cognition.search": {"query": "gateway", "count": 3},
    "self.gap_analysis": {"goal_id": "8616065f"},
    "self.validate": {"code": "x = 1"},
    "self.search_tools": {"query": "goal"},
    "workflow.define": {
        "name": "_smoke_wf",
        "steps": json.dumps([{"tool": "system.time", "arguments": {}}]),
    },
    "workflow.run": {"name": "_smoke_wf"},
    "workflow.delete": {"name": "_smoke_wf"},
    "hermes.status": {},
    "hermes.cancel": {},
}


def _minimal_args(tool_def: dict) -> dict:
    name = tool_def["name"]
    if name in DEFAULTS:
        return dict(DEFAULTS[name])
    schema = tool_def.get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = schema.get("required") or []
    args: dict = {}
    for key in required:
        p = props.get(key, {})
        t = p.get("type", "string")
        if t == "string":
            args[key] = "smoke"
        elif t == "integer":
            args[key] = 1
        elif t == "boolean":
            args[key] = False
        elif t == "object":
            args[key] = {}
        else:
            args[key] = "smoke"
    return args


def _classify(name: str, result: str, exc: Exception | None) -> dict:
    if exc is not None:
        return {"status": "error", "kind": type(exc).__name__, "detail": str(exc)[:300]}

    text = result or ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None

    if data is not None and isinstance(data, dict):
        if data.get("ok") is False:
            return {"status": "fail", "kind": "ok_false", "detail": text[:300]}
        if data.get("success") is False:
            return {"status": "fail", "kind": "success_false", "detail": text[:300]}
        if "error" in data and data.get("ok") is not True:
            err = str(data.get("error", ""))
            if err and "not found" not in err.lower():
                return {"status": "fail", "kind": "json_error", "detail": text[:300]}

    low = text.lower()
    if "traceback" in low or "exception" in low[:200]:
        return {"status": "fail", "kind": "traceback_in_output", "detail": text[:300]}
    if text.startswith("[ERROR]"):
        return {"status": "fail", "kind": "error_prefix", "detail": text[:300]}

    return {"status": "ok", "kind": "pass", "detail": text[:200]}


SETUP_FIRST = (
    "fs.write", "memory.note_set", "emerge.spawn_hypothesis",
    "workflow.define",
)


def main() -> None:
    state = host.boot()
    tools = sorted(
        state.registry.list_tools()["tools"],
        key=lambda t: (SETUP_FIRST.index(t["name"])
                       if t["name"] in SETUP_FIRST else 99, t["name"]),
    )
    results = []
    spawned_hypo: str | None = None

    for td in tools:
        name = td["name"]
        entry = {"tool": name, "args": {}, "ms": 0}

        if name in SKIP:
            entry.update({"status": "skipped", "kind": "unsafe_or_slow", "detail": ""})
            results.append(entry)
            continue

        args = _minimal_args(td)
        if name == "emerge.add_evidence" and spawned_hypo:
            args["id"] = spawned_hypo
        if name == "emerge.auto_test" and spawned_hypo:
            args["id"] = spawned_hypo
        if name == "emerge.get_hypothesis" and spawned_hypo:
            args["id"] = spawned_hypo
        entry["args"] = args
        start = time.time()
        try:
            out = host.handle_tool(name, args)
            entry["ms"] = round((time.time() - start) * 1000, 1)
            entry.update(_classify(name, out, None))
            entry["preview"] = (out or "")[:400]
            if name == "emerge.spawn_hypothesis":
                try:
                    spawned_hypo = json.loads(out).get("id")
                except Exception:
                    pass
        except Exception as e:
            entry["ms"] = round((time.time() - start) * 1000, 1)
            entry.update(_classify(name, "", e))
            entry["preview"] = traceback.format_exc()[-400:]

        results.append(entry)
        print(f"{entry['status']:8} {name} ({entry['ms']}ms)")

    out_path = Path("tools_test_report.json")
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    ok = sum(1 for r in results if r["status"] == "ok")
    fail = sum(1 for r in results if r["status"] in ("fail", "error"))
    skip = sum(1 for r in results if r["status"] == "skipped")
    print(f"\nTOTAL={len(results)} OK={ok} FAIL={fail} SKIP={skip}")
    print(f"Report: {out_path.resolve()}")


if __name__ == "__main__":
    main()