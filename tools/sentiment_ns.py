"""sentiment.* — lexicon-based text polarity (no ML model)."""

from __future__ import annotations

import json
import re

from .schema import _p, tool

_POS = frozenset(
    "good great excellent happy love best wonderful fantastic awesome positive success".split())
_NEG = frozenset(
    "bad terrible awful sad hate worst horrible negative angry poor fail broken".split())


def register(registry, ctx) -> None:
    def sentiment_analyze(args: dict) -> str:
        text = (args.get("text") or "").lower()
        words = re.findall(r"\b\w+\b", text)
        pos = sum(1 for w in words if w in _POS)
        neg = sum(1 for w in words if w in _NEG)
        total = pos + neg
        if total == 0:
            score, label = 0.0, "neutral"
        else:
            score = round((pos - neg) / total, 3)
            label = "positive" if score > 0.2 else "negative" if score < -0.2 else "neutral"
        return json.dumps({
            "ok": True,
            "method": "lexicon",
            "score": score,
            "label": label,
            "positive_hits": pos,
            "negative_hits": neg,
            "word_count": len(words),
            "disclaimer": "Lexicon heuristic — not a trained sentiment model.",
        }, indent=2)

    registry.register_from_def(
        tool(
            "sentiment.analyze",
            "Lexicon-based sentiment (-1..1). Not a neural model.",
            {"text": _p("string", "Text to analyze")},
        ),
        sentiment_analyze,
    )