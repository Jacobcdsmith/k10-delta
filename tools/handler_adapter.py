"""Adapt legacy staged tool handlers to the handler(args: dict) -> str contract."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

_DISPATCH_NAMES = frozenset({"arguments", "args", "a", "payload", "params"})


def adapt_handler(fn: Callable[..., Any],
                  input_schema: dict | None = None) -> Callable[[dict], str]:
    """Wrap handlers that use positional/kwargs signatures instead of a single dict."""
    sig = inspect.signature(fn)
    params = [p for p in sig.parameters.values() if p.name != "self"]

    if not params:
        return lambda _args: str(fn())

    if len(params) == 1:
        name = params[0].name
        if name in _DISPATCH_NAMES:
            return lambda args: str(fn(args))
        props = (input_schema or {}).get("properties", {})
        if name in props or len(props) == 1:
            key = name if name in props else next(iter(props), name)

            def _single(args: dict) -> str:
                if isinstance(args, dict):
                    if key in args:
                        return str(fn(args[key]))
                    if name in args:
                        return str(fn(args[name]))
                    if len(args) == 1:
                        return str(fn(next(iter(args.values()))))
                return str(fn(args))

            return _single

    param_names = [p.name for p in params]
    defaults = {
        p.name: p.default
        for p in params
        if p.default is not inspect.Parameter.empty
    }

    def _kwargs(args: dict) -> str:
        payload = args if isinstance(args, dict) else {}
        bound = {name: payload[name] for name in param_names if name in payload}
        for name, value in defaults.items():
            bound.setdefault(name, value)
        missing = [name for name in param_names if name not in bound]
        if missing:
            raise TypeError(
                f"handler missing required args: {', '.join(missing)}")
        return str(fn(**bound))

    return _kwargs