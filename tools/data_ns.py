"""data.* tool handlers — data transformation and encoding."""

from __future__ import annotations

import base64
import csv
import io
import json
import re
from datetime import datetime, timezone
from typing import Any

from .context import ctx_get
from .schema import _p, tool


def register(registry, ctx) -> None:
    """Register all data.* tools."""

    # ── data.json_encode ──
    def json_encode(args: dict) -> str:
        value = args.get("value")
        indent = args.get("indent")
        return json.dumps({"result": json.dumps(value, indent=indent)})

    registry.register_from_def(
        tool(
            "data.json_encode",
            "Encode value as JSON string.",
            {
                "value": _p("object", "Value to encode"),
                "indent": _p("integer", "Indentation (default: none)", required=False),
            },
            tier="core",
        ),
        json_encode,
    )

    # ── data.json_decode ──
    def json_decode(args: dict) -> str:
        text = args.get("text", "")
        try:
            return json.dumps({"result": json.loads(text), "valid": True})
        except json.JSONDecodeError as e:
            return json.dumps({"error": str(e), "valid": False})

    registry.register_from_def(
        tool(
            "data.json_decode",
            "Parse JSON string to object.",
            {"text": _p("string", "JSON string to parse")},
            tier="core",
        ),
        json_decode,
    )

    # ── data.csv_encode ──
    def csv_encode(args: dict) -> str:
        rows = args.get("rows", [])
        if not rows:
            return json.dumps({"result": ""})
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerows(rows)
        return json.dumps({"result": output.getvalue()})

    registry.register_from_def(
        tool(
            "data.csv_encode",
            "Encode array of arrays as CSV.",
            {"rows": _p("array", "Array of row arrays")},
            tier="domain",
        ),
        csv_encode,
    )

    # ── data.csv_decode ──
    def csv_decode(args: dict) -> str:
        text = args.get("text", "")
        reader = csv.reader(io.StringIO(text))
        rows = [row for row in reader]
        return json.dumps({"rows": rows})

    registry.register_from_def(
        tool(
            "data.csv_decode",
            "Parse CSV text to array of arrays.",
            {"text": _p("string", "CSV text")},
            tier="domain",
        ),
        csv_decode,
    )

    # ── data.yaml_encode ──
    def yaml_encode(args: dict) -> str:
        try:
            import yaml
            value = args.get("value")
            return json.dumps({"result": yaml.dump(value, sort_keys=False)})
        except ImportError:
            return json.dumps({"error": "PyYAML not installed"})

    registry.register_from_def(
        tool(
            "data.yaml_encode",
            "Encode value as YAML string (requires PyYAML).",
            {"value": _p("object", "Value to encode")},
            tier="domain",
        ),
        yaml_encode,
    )

    # ── data.yaml_decode ──
    def yaml_decode(args: dict) -> str:
        try:
            import yaml
            text = args.get("text", "")
            return json.dumps({"result": yaml.safe_load(text), "valid": True})
        except ImportError:
            return json.dumps({"error": "PyYAML not installed"})
        except Exception as e:
            return json.dumps({"error": str(e), "valid": False})

    registry.register_from_def(
        tool(
            "data.yaml_decode",
            "Parse YAML string to object (requires PyYAML).",
            {"text": _p("string", "YAML text")},
            tier="domain",
        ),
        yaml_decode,
    )

    # ── data.toml_decode ──
    def toml_decode(args: dict) -> str:
        try:
            import tomllib
            text = args.get("text", "")
            return json.dumps({"result": tomllib.loads(text), "valid": True})
        except ImportError:
            return json.dumps({"error": "tomllib not available (Python 3.11+)"})
        except Exception as e:
            return json.dumps({"error": str(e), "valid": False})

    registry.register_from_def(
        tool(
            "data.toml_decode",
            "Parse TOML string to object (Python 3.11+).",
            {"text": _p("string", "TOML text")},
            tier="domain",
        ),
        toml_decode,
    )

    # ── data.url_encode ──
    def url_encode(args: dict) -> str:
        from urllib.parse import quote
        text = args.get("text", "")
        safe = args.get("safe", "")
        return json.dumps({"result": quote(text, safe=safe)})

    registry.register_from_def(
        tool(
            "data.url_encode",
            "URL-encode a string.",
            {
                "text": _p("string", "Text to encode"),
                "safe": _p("string", "Characters not to encode", required=False),
            },
            tier="core",
        ),
        url_encode,
    )

    # ── data.url_decode ──
    def url_decode(args: dict) -> str:
        from urllib.parse import unquote
        text = args.get("text", "")
        return json.dumps({"result": unquote(text)})

    registry.register_from_def(
        tool(
            "data.url_decode",
            "URL-decode a string.",
            {"text": _p("string", "Text to decode")},
            tier="core",
        ),
        url_decode,
    )

    # ── data.html_escape ──
    def html_escape(args: dict) -> str:
        import html
        text = args.get("text", "")
        return json.dumps({"result": html.escape(text)})

    registry.register_from_def(
        tool(
            "data.html_escape",
            "Escape HTML special characters.",
            {"text": _p("string", "Text to escape")},
            tier="core",
        ),
        html_escape,
    )

    # ── data.html_unescape ──
    def html_unescape(args: dict) -> str:
        import html
        text = args.get("text", "")
        return json.dumps({"result": html.unescape(text)})

    registry.register_from_def(
        tool(
            "data.html_unescape",
            "Unescape HTML entities.",
            {"text": _p("string", "Text to unescape")},
            tier="core",
        ),
        html_unescape,
    )

    # ── data.base64_encode ──
    def base64_encode(args: dict) -> str:
        data = args.get("data", "").encode("utf-8")
        return json.dumps({"result": base64.b64encode(data).decode("ascii")})

    registry.register_from_def(
        tool(
            "data.base64_encode",
            "Base64 encode a string.",
            {"data": _p("string", "Input string")},
            tier="core",
        ),
        base64_encode,
    )

    # ── data.base64_decode ──
    def base64_decode(args: dict) -> str:
        data = args.get("data", "")
        return json.dumps({"result": base64.b64decode(data).decode("utf-8")})

    registry.register_from_def(
        tool(
            "data.base64_decode",
            "Base64 decode a string.",
            {"data": _p("string", "Base64 string")},
            tier="core",
        ),
        base64_decode,
    )

    # ── data.hex_encode ──
    def hex_encode(args: dict) -> str:
        data = args.get("data", "").encode("utf-8")
        return json.dumps({"result": data.hex()})

    registry.register_from_def(
        tool(
            "data.hex_encode",
            "Encode string as hex.",
            {"data": _p("string", "Input string")},
            tier="core",
        ),
        hex_encode,
    )

    # ── data.hex_decode ──
    def hex_decode(args: dict) -> str:
        data = args.get("data", "")
        return json.dumps({"result": bytes.fromhex(data).decode("utf-8")})

    registry.register_from_def(
        tool(
            "data.hex_decode",
            "Decode hex string.",
            {"data": _p("string", "Hex string")},
            tier="core",
        ),
        hex_decode,
    )

    # ── data.iso8601 ──
    def iso8601(args: dict) -> str:
        now = datetime.now(timezone.utc)
        return json.dumps({"iso8601": now.isoformat()})

    registry.register_from_def(
        tool(
            "data.iso8601",
            "Get current time in ISO 8601 format.",
            {},
            tier="core",
        ),
        iso8601,
    )

    # ── data.unix_time ──
    def unix_time(args: dict) -> str:
        now = datetime.now(timezone.utc)
        return json.dumps({"unix": int(now.timestamp()), "unix_ms": int(now.timestamp() * 1000)})

    registry.register_from_def(
        tool(
            "data.unix_time",
            "Get current Unix timestamp (seconds and ms).",
            {},
            tier="core",
        ),
        unix_time,
    )

    # ── data.parse_date ──
    def parse_date(args: dict) -> str:
        from dateutil import parser as dateparser
        text = args.get("text", "")
        try:
            dt = dateparser.parse(text)
            return json.dumps({
                "iso8601": dt.isoformat(),
                "unix": int(dt.timestamp()),
                "unix_ms": int(dt.timestamp() * 1000),
            })
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register_from_def(
        tool(
            "data.parse_date",
            "Parse natural language date/time string (requires python-dateutil).",
            {"text": _p("string", "Date string to parse")},
            tier="domain",
        ),
        parse_date,
    )
