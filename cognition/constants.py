"""Shared constants and module-level runtime for cognition package."""
from __future__ import annotations

import re
import time
import logging

log = logging.getLogger("k10d.cognition")

REFLECT_INTERVAL   = 300
DREAM_IDLE_SECS    = 1800
MEME_DECAY_CYCLES  = 5
PROBE_INTERVAL     = 3
MAX_HYPOTHESES     = 50
CONCEPT_TOP_N      = 20
TRAJECTORY_WINDOW  = 10
DREAM_EVIDENCE_W   = 0.25
META_EPISODE_RE    = re.compile(r"^\[(cognition|dream)\s+#\d+\]", re.I)

STOPWORDS = {
    "the", "a", "an", "is", "it", "in", "on", "at", "to", "of", "and", "or", "for",
    "with", "this", "that", "was", "are", "be", "been", "by", "as", "from", "i",
    "my", "we", "you", "he", "she", "they", "have", "has", "had", "do", "did",
    "will", "can", "not", "but", "so", "if", "then", "ok", "true", "false",
    "null", "none", "error", "type", "string", "integer", "object", "list",
    "summary", "detail", "ts", "source", "compressed",
}

META_STOPWORDS = STOPWORDS | {
    "concepts", "episodes", "cognition", "top", "hypotheses", "axioms",
    "trajectory", "vulnerable", "open", "initializing", "narrowing", "growing",
    "stable", "insights", "hypos", "tested", "dream", "reflection", "cycles",
    "semantic", "potentiation", "decaying", "confidence", "synthesis",
    "substantive", "meta", "relevance", "match", "rate", "sample", "cycle",
    "received", "around", "evidence", "status", "confirmed", "falsified",
}

CONTRADICTION_MARKERS = (
    "failed", "failure", "contradict", "incorrect", "wrong", "deny", "denied",
    "unlike", "however", "although", "despite", "cannot", "unable", "blocked",
    "refused", "rejected", "broken", "missing", "empty", "never", "without",
)

STRUCTURAL_DETAIL_KEYS = frozenset({
    "episodes", "top_concepts", "trajectory", "open_hypotheses",
    "vulnerable_axioms", "hypos_tested", "concepts", "insights",
    "rising_concepts", "activity", "recommendations",
})

_selfmod_engine = None
_start_time = time.time()


def set_selfmod_engine(engine_instance):
    global _selfmod_engine
    _selfmod_engine = engine_instance


def get_selfmod_engine():
    return _selfmod_engine
