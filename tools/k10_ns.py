"""k10.* — gateway-side K10 operations."""

from __future__ import annotations

import json
from typing import Any

from .context import ctx_get
from .schema import _p, tool




def register(registry, ctx) -> None:
    call_tool = ctx_get(ctx, "call_tool")

    def k10_python_exec(args: dict) -> str:
        code = args["code"]
        timeout = min(int(args.get("timeout", 10)), 60)
        try:
            from unihiker_k10 import temp_humi  # noqa: F401 — device check
            on_device = True
        except ImportError:
            on_device = False

        if not on_device:
            return call_tool("exec.python", {"code": code, "timeout": timeout})

        import io
        import sys
        old = sys.stdout
        buf = io.StringIO()
        sys.stdout = buf
        try:
            exec(code, {"__builtins__": __builtins__}, {})
            out = buf.getvalue()
            return json.dumps({
                "ok": True,
                "device": True,
                "stdout": out or "[no output]",
            })
        except Exception as e:
            return json.dumps({"ok": False, "device": True, "error": str(e)})
        finally:
            sys.stdout = old

    registry.register_from_def(
        tool(
            "k10.python_exec",
            "Execute Python on K10 device, or exec.python on gateway host.",
            {
                "code": _p("string", "Python code"),
                "timeout": _p("integer", "Timeout seconds (default 10)", required=False),
            },
        ),
        k10_python_exec,
    )