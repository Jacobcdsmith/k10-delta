"""text.* tool handlers — text processing utilities."""

from __future__ import annotations

import re
import json
from typing import Any

from .context import ctx_get
from .schema import _p, tool


def register(registry, ctx) -> None:
    """Register all text.* tools."""

    # ── text.upper ──
    def to_upper(args: dict) -> str:
        return json.dumps({"result": args.get("text", "").upper()})

    registry.register_from_def(
        tool(
            "text.upper",
            "Convert text to uppercase.",
            {"text": _p("string", "Input text")},
            tier="core",
        ),
        to_upper,
    )

    # ── text.lower ──
    def to_lower(args: dict) -> str:
        return json.dumps({"result": args.get("text", "").lower()})

    registry.register_from_def(
        tool(
            "text.lower",
            "Convert text to lowercase.",
            {"text": _p("string", "Input text")},
            tier="core",
        ),
        to_lower,
    )

    # ── text.trim ──
    def trim_text(args: dict) -> str:
        return json.dumps({"result": args.get("text", "").strip()})

    registry.register_from_def(
        tool(
            "text.trim",
            "Trim whitespace from text.",
            {"text": _p("string", "Input text")},
            tier="core",
        ),
        trim_text,
    )

    # ── text.reverse ──
    def reverse_text(args: dict) -> str:
        return json.dumps({"result": args.get("text", "")[::-1]})

    registry.register_from_def(
        tool(
            "text.reverse",
            "Reverse a string.",
            {"text": _p("string", "Input text")},
            tier="core",
        ),
        reverse_text,
    )

    # ── text.count ──
    def count_text(args: dict) -> str:
        text = args.get("text", "")
        return json.dumps({
            "chars": len(text),
            "words": len(text.split()),
            "lines": text.count("\n") + (1 if text else 0),
        })

    registry.register_from_def(
        tool(
            "text.count",
            "Count characters, words, and lines in text.",
            {"text": _p("string", "Input text")},
            tier="core",
        ),
        count_text,
    )

    # ── text.replace ──
    def replace_text(args: dict) -> str:
        text = args.get("text", "")
        old = args.get("old", "")
        new = args.get("new", "")
        count = args.get("count", -1)
        return json.dumps({"result": text.replace(old, new, count)})

    registry.register_from_def(
        tool(
            "text.replace",
            "Replace substring in text.",
            {
                "text": _p("string", "Input text"),
                "old": _p("string", "Substring to replace"),
                "new": _p("string", "Replacement string"),
                "count": _p("integer", "Max replacements (-1 for all)", required=False),
            },
            tier="core",
        ),
        replace_text,
    )

    # ── text.split ──
    def split_text(args: dict) -> str:
        text = args.get("text", "")
        sep = args.get("sep", " ")
        maxsplit = args.get("maxsplit", -1)
        parts = text.split(sep, maxsplit) if maxsplit >= 0 else text.split(sep)
        return json.dumps({"parts": parts})

    registry.register_from_def(
        tool(
            "text.split",
            "Split text by separator.",
            {
                "text": _p("string", "Input text"),
                "sep": _p("string", "Separator (default: space)", required=False),
                "maxsplit": _p("integer", "Max splits (-1 for unlimited)", required=False),
            },
            tier="core",
        ),
        split_text,
    )

    # ── text.join ──
    def join_text(args: dict) -> str:
        parts = args.get("parts", [])
        sep = args.get("sep", "")
        return json.dumps({"result": sep.join(str(p) for p in parts)})

    registry.register_from_def(
        tool(
            "text.join",
            "Join array of strings with separator.",
            {
                "parts": _p("array", "Array of strings to join"),
                "sep": _p("string", "Separator (default: empty)", required=False),
            },
            tier="core",
        ),
        join_text,
    )

    # ── text.slugify ──
    def slugify_text(args: dict) -> str:
        text = args.get("text", "")
        text = re.sub(r"[^\w\s-]", "", text.lower())
        text = re.sub(r"[\s_-]+", "-", text)
        text = text.strip("-")
        return json.dumps({"slug": text})

    registry.register_from_def(
        tool(
            "text.slugify",
            "Convert text to URL-friendly slug.",
            {"text": _p("string", "Input text")},
            tier="core",
        ),
        slugify_text,
    )

    # ── text.diff ──
    def diff_text(args: dict) -> str:
        import difflib
        a = args.get("a", "").splitlines()
        b = args.get("b", "").splitlines()
        diff = list(difflib.unified_diff(a, b, lineterm=""))
        return json.dumps({"diff": diff, "changed": len(diff) > 0})

    registry.register_from_def(
        tool(
            "text.diff",
            "Compute unified diff between two texts.",
            {
                "a": _p("string", "Original text"),
                "b": _p("string", "New text"),
            },
            tier="domain",
        ),
        diff_text,
    )

    # ── text.truncate ──
    def truncate_text(args: dict) -> str:
        text = args.get("text", "")
        length = args.get("length", 100)
        suffix = args.get("suffix", "…")
        if len(text) <= length:
            return json.dumps({"result": text, "truncated": False})
        return json.dumps({"result": text[:length - len(suffix)] + suffix, "truncated": True})

    registry.register_from_def(
        tool(
            "text.truncate",
            "Truncate text to max length with optional suffix.",
            {
                "text": _p("string", "Input text"),
                "length": _p("integer", "Max length (default 100)", required=False),
                "suffix": _p("string", "Truncation suffix (default: …)", required=False),
            },
            tier="core",
        ),
        truncate_text,
    )

    # ── text.extract_urls ──
    def extract_urls(args: dict) -> str:
        text = args.get("text", "")
        urls = re.findall(r'https?://[^\s<>"{}|\\^`\[\]]+', text)
        return json.dumps({"urls": urls})

    registry.register_from_def(
        tool(
            "text.extract_urls",
            "Extract URLs from text.",
            {"text": _p("string", "Input text")},
            tier="domain",
        ),
        extract_urls,
    )

    # ── text.extract_emails ──
    def extract_emails(args: dict) -> str:
        text = args.get("text", "")
        emails = re.findall(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', text)
        return json.dumps({"emails": emails})

    registry.register_from_def(
        tool(
            "text.extract_emails",
            "Extract email addresses from text.",
            {"text": _p("string", "Input text")},
            tier="domain",
        ),
        extract_emails,
    )
