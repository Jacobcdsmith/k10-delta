"""memory.* tool handlers."""

from __future__ import annotations

import json
import os
from typing import Any

from .context import ctx_get
from .schema import _p, tool




def register(registry, ctx) -> None:
    soul = ctx_get(ctx, "soul")
    save_soul = ctx_get(ctx, "save_soul")
    read_episodes = ctx_get(ctx, "read_episodes")
    append_episode = ctx_get(ctx, "append_episode")
    load_notes = ctx_get(ctx, "load_notes")
    save_notes = ctx_get(ctx, "save_notes")
    state_lock = ctx_get(ctx, "state_lock")
    EPISODES_PATH = ctx_get(ctx, "EPISODES_PATH")
    _now_iso = ctx_get(ctx, "_now_iso")

    def get_soul(args: dict) -> str:
        if args.get("full"):
            result = json.dumps(soul, indent=2)
        else:
            slim = {k: v for k, v in soul.items() if k != "semantic"}
            sem = soul.get("semantic", {})
            if sem:
                slim_sem = {}
                for k, v in sem.items():
                    if k == "dream_facts":
                        slim_sem[k] = v[-5:] if isinstance(v, list) else v
                    elif k == "tools_inventory":
                        slim_inv = {"namespaces": {}}
                        ns_data = v.get("namespaces", {}) if isinstance(v, dict) else {}
                        for ns_name, ns_info in ns_data.items():
                            if isinstance(ns_info, dict):
                                slim_inv["namespaces"][ns_name] = {
                                    "description": ns_info.get("description", ""),
                                    "tool_count": len(ns_info.get("tools", {})),
                                }
                        slim_sem[k] = slim_inv
                    elif k == "tool_usage":
                        tu = v if isinstance(v, dict) else {}
                        slim_sem[k] = {
                            "top_tools": tu.get("top_tools", [])[:10],
                            "namespace_heat": tu.get("namespace_heat", {}),
                            "error_tools": tu.get("error_tools", []),
                            "total_tool_calls": tu.get("total_tool_calls", 0),
                        }
                    elif k == "concepts":
                        slim_sem[k] = v[:20] if isinstance(v, list) else v
                    else:
                        slim_sem[k] = v
                slim["semantic"] = slim_sem
            result = json.dumps(slim, indent=2)
        if len(result) > 262144:
            result = result[:262144] + "\n...[truncated {} bytes]".format(len(result) - 262144)
        return result

    def set_field(args: dict) -> str:
        if args["key"] in ("boot_count", "axioms"):
            raise ValueError(f"Use dedicated tools for '{args['key']}'")
        try:
            soul[args["key"]] = json.loads(args["value"])
        except Exception:
            soul[args["key"]] = args["value"]
        with state_lock:
            save_soul(soul)
        return json.dumps({"ok": True})

    def get_episodes(args: dict) -> str:
        return json.dumps(read_episodes(min(int(args.get("count", 10)), 200)), indent=2)

    def log_episode(args: dict) -> str:
        try:
            detail = json.loads(args.get("detail", "null"))
        except Exception:
            detail = args.get("detail", "")
        append_episode({"ts": _now_iso(), "summary": args["summary"], "detail": detail})
        return json.dumps({"ok": True})

    def compress_episodes(args: dict) -> str:
        n = min(int(args["count"]), 5000)
        if n < 1:
            return json.dumps({"ok": False, "error": "count must be >= 1"})
        lines = (
            EPISODES_PATH.read_text(encoding="utf-8").splitlines()
            if EPISODES_PATH.exists() else []
        )
        if n >= len(lines):
            return json.dumps({"ok": False, "error": f"Cannot compress {n} episodes (only {len(lines)} total). Max 90%."})
        max_n = max(1, int(len(lines) * 0.9))
        if n > max_n:
            return json.dumps({"ok": False, "error": f"Cannot compress {n} episodes — max {max_n} (90% of {len(lines)})"})
        summary_ep = {
            "ts": _now_iso(),
            "summary": f"[compressed {n}] {args['summary']}",
            "compressed_count": n,
        }
        remaining = lines[n:]
        out_lines = [json.dumps(summary_ep, ensure_ascii=False)]
        if remaining:
            out_lines.extend(remaining)
        output = "\n".join(out_lines) + "\n"
        tmp_path = EPISODES_PATH.with_suffix(".jsonl.tmp")
        tmp_path.write_text(output, encoding="utf-8")
        if not tmp_path.exists() or tmp_path.stat().st_size == 0:
            tmp_path.unlink(missing_ok=True)
            return json.dumps({"ok": False, "error": "Write failed — output is empty"})
        os.replace(tmp_path, EPISODES_PATH)
        return json.dumps({"ok": True, "compressed": n, "remaining": len(remaining), "total_before": len(lines)})

    def append_axiom(args: dict) -> str:
        soul.setdefault("axioms", []).append(args["axiom"].strip())
        with state_lock:
            save_soul(soul)
        return json.dumps({"ok": True, "axioms": soul["axioms"]})

    def remove_axiom(args: dict) -> str:
        idx = int(args["index"])
        axioms = soul.get("axioms", [])
        if not (0 <= idx < len(axioms)):
            raise IndexError(f"Index {idx} out of range")
        removed = axioms.pop(idx)
        with state_lock:
            save_soul(soul)
        return json.dumps({"ok": True, "removed": removed})

    def mutate_axiom(args: dict) -> str:
        idx = int(args["index"])
        axioms = soul.get("axioms", [])
        if not (0 <= idx < len(axioms)):
            raise IndexError(f"Index {idx} out of range")
        old = axioms[idx]
        axioms[idx] = args["new_text"].strip()
        with state_lock:
            save_soul(soul)
        return json.dumps({"ok": True, "old": old, "new": axioms[idx]})

    def note_set(args: dict) -> str:
        notes = load_notes()
        notes[args["key"]] = args["value"]
        save_notes(notes)
        return json.dumps({"ok": True})

    def note_get(args: dict) -> str:
        notes = load_notes()
        if args["key"] not in notes:
            if args.get("missing") == "empty":
                return "{}"
            return json.dumps({"ok": False, "error": f"Note '{args['key']}' not found"})
        val = notes[args["key"]]
        if args.get("structured"):
            return json.dumps({"ok": True, "key": args["key"], "value": val})
        return val if isinstance(val, str) else json.dumps(val)

    def note_list(args: dict) -> str:
        return json.dumps(list(load_notes().keys()))

    def note_delete(args: dict) -> str:
        notes = load_notes()
        if args["key"] not in notes:
            raise KeyError(f"Note '{args['key']}' not found")
        del notes[args["key"]]
        save_notes(notes)
        return json.dumps({"ok": True})

    def semantic_set(args: dict) -> str:
        soul.setdefault("semantic", {})[args["key"]] = args["value"]
        with state_lock:
            save_soul(soul)
        return json.dumps({"ok": True})

    def semantic_get(args: dict) -> str:
        val = soul.get("semantic", {}).get(args["key"])
        if val is None:
            return json.dumps({"ok": False, "error": f"Key '{args['key']}' not found"})
        return str(val) if not isinstance(val, (dict, list)) else json.dumps(val, indent=2)

    def semantic_dump(args: dict) -> str:
        return json.dumps(soul.get("semantic", {}), indent=2)

    registry.register_from_def(tool("memory.get_soul", "Full soul state."), get_soul)
    registry.register_from_def(
        tool(
            "memory.set_field",
            "Set a top-level soul field.",
            {"key": _p("string", "Field"), "value": _p("string", "JSON value")},
        ),
        set_field,
    )
    registry.register_from_def(
        tool(
            "memory.get_episodes",
            "Return N most recent episodes.",
            {"count": _p("integer", "Count (default 10, max 200)", required=False)},
        ),
        get_episodes,
    )
    registry.register_from_def(
        tool(
            "memory.log_episode",
            "Append an episode.",
            {
                "summary": _p("string", "Summary"),
                "detail": _p("string", "Payload", required=False),
            },
        ),
        log_episode,
    )
    registry.register_from_def(
        tool(
            "memory.compress_episodes",
            "Compress oldest N episodes.",
            {"count": _p("integer", "Count"), "summary": _p("string", "Description")},
        ),
        compress_episodes,
    )
    registry.register_from_def(
        tool("memory.append_axiom", "Add an axiom.", {"axiom": _p("string", "Axiom text")}),
        append_axiom,
    )
    registry.register_from_def(
        tool("memory.remove_axiom", "Remove axiom by index.", {"index": _p("integer", "Index")}),
        remove_axiom,
    )
    registry.register_from_def(
        tool(
            "memory.mutate_axiom",
            "Replace an axiom in-place.",
            {"index": _p("integer", "Index"), "new_text": _p("string", "New text")},
        ),
        mutate_axiom,
    )
    registry.register_from_def(
        tool(
            "memory.note_set",
            "Write a note.",
            {"key": _p("string", "Key"), "value": _p("string", "Content")},
        ),
        note_set,
    )
    registry.register_from_def(
        tool("memory.note_get", "Read a note.", {"key": _p("string", "Key")}),
        note_get,
    )
    registry.register_from_def(tool("memory.note_list", "List note keys."), note_list)
    registry.register_from_def(
        tool("memory.note_delete", "Delete a note.", {"key": _p("string", "Key")}),
        note_delete,
    )
    registry.register_from_def(
        tool(
            "memory.semantic_set",
            "Set semantic memory key.",
            {"key": _p("string", "Key"), "value": _p("string", "Value")},
        ),
        semantic_set,
    )
    registry.register_from_def(
        tool("memory.semantic_get", "Get semantic memory key.", {"key": _p("string", "Key")}),
        semantic_get,
    )
    registry.register_from_def(tool("memory.semantic_dump", "Dump all semantic memory."), semantic_dump)