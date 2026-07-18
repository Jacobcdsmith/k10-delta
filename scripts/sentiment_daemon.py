#!/usr/bin/env python3
"""
Sentiment daemon — Phase 1 from sentiment_daemon_roadmap.md

Scores user/assistant exchanges, persists mood to voice profile memory
(MOOD.json + MEMORY.md) for Honcho pickup on next Hermes flush.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional

log = logging.getLogger("sentiment")

VOICE_HOME = Path(
    __import__("os").environ.get(
        "VOICE_HERMES_HOME",
        Path.home() / "AppData" / "Local" / "hermes" / "profiles" / "voice",
    )
)
MOOD_PATH = VOICE_HOME / "memories" / "MOOD.json"
MEMORY_PATH = VOICE_HOME / "memories" / "MEMORY.md"
MAX_HISTORY = 50

# Keyword buckets (fast path — no model dep required)
_POSITIVE = re.compile(
    r"\b(love|amazing|great|yes|hell\s*yes|fuck\s*yes|hot|sexy|more|please|"
    r"perfect|beautiful|gorgeous|want|need\s+you|turned\s+on|excited|curious|"
    r"wow|nice|good|thanks|thank\s+you)\b",
    re.I,
)
_NEGATIVE = re.compile(
    r"\b(hate|annoyed|frustrated|angry|stop|wrong|broken|sucks|terrible|"
    r"confused|lost|ugh|no\b|don't|doesn't\s+work|fail|error|stupid)\b",
    re.I,
)
_FLIRTY = re.compile(
    r"\b(kiss|touch|tease|naked|nude|bed|moan|whisper|dirty|naughty|"
    r"spank|dominant|submissive|kink)\b",
    re.I,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def score_text(text: str) -> Dict[str, object]:
    """Return sentiment tag, float score, and optional suggested_action."""
    if not text or not text.strip():
        return {"tag": "neutral", "score": 0.0, "suggested_action": None}

    t = text.strip()
    pos = len(_POSITIVE.findall(t))
    neg = len(_NEGATIVE.findall(t))
    flirty = len(_FLIRTY.findall(t))

    raw = (pos - neg) * 0.15
    if flirty:
        raw += 0.25
    raw = max(-1.0, min(1.0, raw))

    if raw < -0.35:
        tag = "frustrated"
    elif raw < -0.1:
        tag = "confused"
    elif raw > 0.45:
        tag = "excited" if flirty else "curious"
    elif raw > 0.15:
        tag = "curious"
    else:
        tag = "neutral"

    action = None
    if tag == "frustrated":
        action = "slow_down_and_clarify"
    elif tag == "excited":
        action = "match_energy_escalate"
    elif tag == "curious":
        action = "expand_detail"

    return {"tag": tag, "score": round(raw, 3), "suggested_action": action}


class SentimentDaemon:
    """Background mood tracker for voice sessions."""

    def __init__(self, hermes_home: Optional[Path] = None):
        self.hermes_home = Path(hermes_home) if hermes_home else VOICE_HOME
        self.mood_path = self.hermes_home / "memories" / "MOOD.json"
        self.memory_path = self.hermes_home / "memories" / "MEMORY.md"
        self._lock = threading.Lock()
        self._buffer: Deque[Dict] = deque(maxlen=20)
        self._last_review = 0.0

    def _load_mood(self) -> dict:
        if self.mood_path.exists():
            try:
                return json.loads(self.mood_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"current": {"tag": "neutral", "score": 0.0}, "history": [], "session_mood": ""}

    def _save_mood(self, data: dict) -> None:
        self.mood_path.parent.mkdir(parents=True, exist_ok=True)
        self.mood_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _append_memory(self, entry: str) -> None:
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        block = f"{entry}\n§\n"
        if self.memory_path.exists():
            content = self.memory_path.read_text(encoding="utf-8")
            if not content.endswith("\n"):
                content += "\n"
            self.memory_path.write_text(content + block, encoding="utf-8")
        else:
            self.memory_path.write_text(block, encoding="utf-8")

    def record_exchange(
        self,
        user_text: str,
        assistant_text: str,
        *,
        trigger: str = "voice",
        session_id: str = "",
    ) -> Dict[str, object]:
        """Score an exchange and persist mood."""
        user_s = score_text(user_text)
        asst_s = score_text(assistant_text)
        combined_score = round((user_s["score"] + asst_s["score"]) / 2, 3)

        if combined_score < -0.35:
            tag = "frustrated"
        elif combined_score > 0.4:
            tag = "excited"
        elif combined_score > 0.12:
            tag = "curious"
        elif combined_score < -0.08:
            tag = "confused"
        else:
            tag = "neutral"

        record = {
            "timestamp": _now_iso(),
            "trigger": trigger,
            "session_id": session_id,
            "user_snippet": user_text[:120],
            "assistant_snippet": assistant_text[:120],
            "sentiment": tag,
            "score": combined_score,
            "user_score": user_s["score"],
            "assistant_score": asst_s["score"],
            "suggested_action": user_s.get("suggested_action") or asst_s.get("suggested_action"),
        }

        with self._lock:
            self._buffer.append(record)
            mood = self._load_mood()
            mood["current"] = {
                "tag": tag,
                "score": combined_score,
                "updated_at": record["timestamp"],
                "suggested_action": record["suggested_action"],
            }
            history: List = mood.setdefault("history", [])
            history.append(record)
            mood["history"] = history[-MAX_HISTORY:]
            self._save_mood(mood)

            mem_line = (
                f"Voice mood [{tag}] score={combined_score} trigger={trigger} "
                f"user={user_text[:80]!r} → action={record['suggested_action']}"
            )
            self._append_memory(mem_line)

        log.info("Mood: %s (%.2f) session=%s", tag, combined_score, session_id or "-")
        return record

    def get_current_mood(self) -> Dict[str, object]:
        with self._lock:
            return self._load_mood().get("current", {"tag": "neutral", "score": 0.0})

    def mood_prompt_injection(self) -> str:
        """Short mood context for LLM system prompt."""
        cur = self.get_current_mood()
        tag = cur.get("tag", "neutral")
        score = cur.get("score", 0.0)
        action = cur.get("suggested_action")
        parts = [f"Current user mood: {tag} (intensity {score:.2f})."]
        if action == "match_energy_escalate":
            parts.append("Match his energy — he sounds engaged or turned on.")
        elif action == "slow_down_and_clarify":
            parts.append("He's frustrated — slow down, clarify, don't pile on.")
        elif action == "expand_detail":
            parts.append("He's curious — give him more, but keep it spoken and short.")
        return " ".join(parts)


# Module-level singleton for bridge import
_daemon: Optional[SentimentDaemon] = None


def get_daemon(hermes_home: Optional[Path] = None) -> SentimentDaemon:
    global _daemon
    if _daemon is None:
        _daemon = SentimentDaemon(hermes_home)
    return _daemon