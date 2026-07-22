"""utils.* tool handlers — general purpose utilities."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .context import ctx_get
from .schema import _p, tool


def register(registry, ctx) -> None:
    """Register all utils.* tools."""

    # ── utils.uuid ──
    def gen_uuid(args: dict) -> str:
        return json.dumps({"uuid": str(uuid.uuid4())})

    registry.register_from_def(
        tool(
            "utils.uuid",
            "Generate a random UUID v4.",
            {},
            tier="core",
        ),
        gen_uuid,
    )

    # ── utils.timestamp ──
    def get_timestamp(args: dict) -> str:
        now = datetime.now(timezone.utc)
        fmt = args.get("format", "iso")
        if fmt == "unix":
            return json.dumps({"timestamp": int(now.timestamp())})
        elif fmt == "unix_ms":
            return json.dumps({"timestamp": int(now.timestamp() * 1000)})
        elif fmt == "rfc3339":
            return json.dumps({"timestamp": now.isoformat(timespec="seconds")})
        else:
            return json.dumps({"timestamp": now.isoformat()})

    registry.register_from_def(
        tool(
            "utils.timestamp",
            "Get current timestamp in various formats.",
            {
                "format": _p("string", "Format: iso, unix, unix_ms, rfc3339", required=False),
            },
            tier="core",
        ),
        get_timestamp,
    )

    # ── utils.hash ──
    def compute_hash(args: dict) -> str:
        data = args.get("data", "").encode("utf-8")
        algo = args.get("algo", "sha256")
        h = hashlib.new(algo)
        h.update(data)
        return json.dumps({"hash": h.hexdigest(), "algorithm": algo})

    registry.register_from_def(
        tool(
            "utils.hash",
            "Compute hash of input data (sha256, md5, sha1, etc.).",
            {
                "data": _p("string", "Input data to hash"),
                "algo": _p("string", "Hash algorithm (sha256, md5, sha1, sha512)", required=False),
            },
            tier="core",
        ),
        compute_hash,
    )

    # ── utils.base64 ──
    def base64_encode(args: dict) -> str:
        data = args.get("data", "").encode("utf-8")
        return json.dumps({"encoded": base64.b64encode(data).decode("ascii")})

    def base64_decode(args: dict) -> str:
        data = args.get("data", "")
        return json.dumps({"decoded": base64.b64decode(data).decode("utf-8")})

    registry.register_from_def(
        tool(
            "utils.base64_encode",
            "Base64 encode a string.",
            {"data": _p("string", "Input string to encode")},
            tier="core",
        ),
        base64_encode,
    )

    registry.register_from_def(
        tool(
            "utils.base64_decode",
            "Base64 decode a string.",
            {"data": _p("string", "Base64 encoded string")},
            tier="core",
        ),
        base64_decode,
    )

    # ── utils.json ──
    def json_encode(args: dict) -> str:
        data = args.get("data")
        return json.dumps({"json": json.dumps(data, ensure_ascii=False)})

    def json_decode(args: dict) -> str:
        s = args.get("json", "{}")
        return json.dumps({"data": json.loads(s)})

    registry.register_from_def(
        tool(
            "utils.json_encode",
            "Encode any value to JSON string.",
            {"data": _p("object", "Value to encode as JSON")},
            tier="core",
        ),
        json_encode,
    )

    registry.register_from_def(
        tool(
            "utils.json_decode",
            "Decode JSON string to object.",
            {"json": _p("string", "JSON string to decode")},
            tier="core",
        ),
        json_decode,
    )

    # ── utils.random ──
    def random_value(args: dict) -> str:
        kind = args.get("type", "int")
        if kind == "int":
            lo = args.get("min", 0)
            hi = args.get("max", 100)
            return json.dumps({"value": random.randint(lo, hi)})
        elif kind == "float":
            lo = args.get("min", 0.0)
            hi = args.get("max", 1.0)
            return json.dumps({"value": random.uniform(lo, hi)})
        elif kind == "choice":
            choices = args.get("choices", ["a", "b", "c"])
            return json.dumps({"value": random.choice(choices)})
        elif kind == "shuffle":
            items = args.get("items", [])
            random.shuffle(items)
            return json.dumps({"shuffled": items})
        return json.dumps({"error": f"Unknown type: {kind}"})

    registry.register_from_def(
        tool(
            "utils.random",
            "Generate random values (int, float, choice, shuffle).",
            {
                "type": _p("string", "Type: int, float, choice, shuffle", required=False),
                "min": _p("number", "Minimum for int/float", required=False),
                "max": _p("number", "Maximum for int/float", required=False),
                "choices": _p("array", "Choices for choice type", required=False),
                "items": _p("array", "Items to shuffle", required=False),
            },
            tier="core",
        ),
        random_value,
    )

    # ── utils.sleep ──
    def sleep_tool(args: dict) -> str:
        ms = args.get("ms", 1000)
        time.sleep(ms / 1000.0)
        return json.dumps({"slept_ms": ms})

    registry.register_from_def(
        tool(
            "utils.sleep",
            "Sleep for specified milliseconds.",
            {"ms": _p("integer", "Milliseconds to sleep", required=False)},
            tier="core",
        ),
        sleep_tool,
    )

    # ── utils.env ──
    def get_env(args: dict) -> str:
        key = args.get("key")
        if key:
            return json.dumps({key: os.environ.get(key)})
        return json.dumps(dict(os.environ))

    registry.register_from_def(
        tool(
            "utils.env",
            "Get environment variable(s).",
            {"key": _p("string", "Specific env var to get (omit for all)", required=False)},
            tier="domain",
        ),
        get_env,
    )

    # ── utils.time_now ──
    def time_now(args: dict) -> str:
        """Alias for timestamp with timezone support."""
        tz = args.get("timezone", "UTC")
        now = datetime.now(timezone.utc)
        if tz.upper() == "LOCAL":
            now = datetime.now().astimezone()
        return json.dumps({
            "iso": now.isoformat(),
            "unix": int(now.timestamp()),
            "unix_ms": int(now.timestamp() * 1000),
            "timezone": str(now.tzinfo),
        })

    registry.register_from_def(
        tool(
            "utils.time_now",
            "Get current time with optional timezone.",
            {"timezone": _p("string", "Timezone: UTC or LOCAL", required=False)},
            tier="core",
        ),
        time_now,
    )

    # ── utils.counter ──
    _counters = {}

    def counter(args: dict) -> str:
        name = args.get("name", "default")
        op = args.get("op", "inc")
        if name not in _counters:
            _counters[name] = 0
        if op == "inc":
            _counters[name] += args.get("by", 1)
        elif op == "dec":
            _counters[name] -= args.get("by", 1)
        elif op == "set":
            _counters[name] = args.get("value", 0)
        elif op == "reset":
            _counters[name] = 0
        elif op == "get":
            pass
        return json.dumps({"counter": name, "value": _counters[name]})

    registry.register_from_def(
        tool(
            "utils.counter",
            "Simple named counters (inc, dec, set, get, reset).",
            {
                "name": _p("string", "Counter name", required=False),
                "op": _p("string", "Operation: inc, dec, set, get, reset", required=False),
                "by": _p("integer", "Amount for inc/dec", required=False),
                "value": _p("integer", "Value for set", required=False),
            },
            tier="domain",
        ),
        counter,
    )

    # ── utils.format ──
    def format_string(args: dict) -> str:
        template = args.get("template", "")
        values = args.get("values", {})
        try:
            result = template.format(**values)
            return json.dumps({"formatted": result})
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register_from_def(
        tool(
            "utils.format",
            "Format a template string with values.",
            {
                "template": _p("string", "Template string with {placeholders}"),
                "values": _p("object", "Values to substitute"),
            },
            tier="core",
        ),
        format_string,
    )

    # ── utils.regex ──
    import re

    def regex_match(args: dict) -> str:
        pattern = args.get("pattern", "")
        text = args.get("text", "")
        flags = args.get("flags", 0)
        try:
            match = re.search(pattern, text, flags)
            if match:
                return json.dumps({
                    "matched": True,
                    "groups": match.groups(),
                    "groupdict": match.groupdict(),
                    "span": match.span(),
                })
            return json.dumps({"matched": False})
        except Exception as e:
            return json.dumps({"error": str(e)})

    def regex_replace(args: dict) -> str:
        pattern = args.get("pattern", "")
        text = args.get("text", "")
        replacement = args.get("replacement", "")
        flags = args.get("flags", 0)
        try:
            result = re.sub(pattern, replacement, text, flags=flags)
            return json.dumps({"result": result})
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register_from_def(
        tool(
            "utils.regex_match",
            "Test regex pattern against text.",
            {
                "pattern": _p("string", "Regex pattern"),
                "text": _p("string", "Text to search"),
                "flags": _p("integer", "Regex flags (re.IGNORECASE=2, etc.)", required=False),
            },
            tier="core",
        ),
        regex_match,
    )

    registry.register_from_def(
        tool(
            "utils.regex_replace",
            "Replace regex matches in text.",
            {
                "pattern": _p("string", "Regex pattern"),
                "text": _p("string", "Text to process"),
                "replacement": _p("string", "Replacement string"),
                "flags": _p("integer", "Regex flags", required=False),
            },
            tier="core",
        ),
        regex_replace,
    )

