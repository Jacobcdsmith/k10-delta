from __future__ import annotations

from typing import Any


def _p(type_: str, desc: str, required: bool = True, **kw: Any) -> dict:
    return {"_req": required, "schema": {"type": type_, "description": desc, **kw}}


def tool(name: str, desc: str, props: dict | None = None, tier: str | None = None) -> dict:
    props = props or {}
    defn = {
        "name": name,
        "description": desc,
        "inputSchema": {
            "type": "object",
            "properties": {k: v["schema"] for k, v in props.items()},
            "required": [k for k, v in props.items() if v.get("_req", True)],
        },
    }
    if tier:
        defn["tier"] = tier
    return defn
