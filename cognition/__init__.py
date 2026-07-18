"""K10-Δ Cognition package — engines, Will, and text analytics."""
from __future__ import annotations

from .constants import (
    REFLECT_INTERVAL,
    DREAM_IDLE_SECS,
    set_selfmod_engine,
    get_selfmod_engine,
    _start_time,
)
from .dream import DreamEngine
from .engine import CognitionEngine
from .hypotheses import HypothesisStore
from .memetic import MemeticEngine
from .prober import AdversarialProber
from .text import (
    _now_iso,
    _substantive_episodes,
    _is_meta_episode,
    _tfidf_concepts,
    score_hypothesis_relevance,
    synthesise_fact,
    analyze_tool_usage,
)
from .will import (
    AutonomyPolicy,
    Intention,
    WILL_DEFAULTS,
    AUTONOMY_DEFAULTS,
)

# Removed pools — empty tuples for any leftover imports
AUTONOMY_GOAL_POOL = ()
AUTONOMY_HYPOTHESIS_POOL = ()
AUTONOMY_TOOL_PRIORITY = ()

__all__ = [
    "CognitionEngine",
    "DreamEngine",
    "MemeticEngine",
    "AdversarialProber",
    "HypothesisStore",
    "AutonomyPolicy",
    "Intention",
    "WILL_DEFAULTS",
    "AUTONOMY_DEFAULTS",
    "set_selfmod_engine",
    "get_selfmod_engine",
    "_now_iso",
    "_start_time",
    "_substantive_episodes",
    "_is_meta_episode",
    "_tfidf_concepts",
    "score_hypothesis_relevance",
    "synthesise_fact",
    "analyze_tool_usage",
    "REFLECT_INTERVAL",
    "DREAM_IDLE_SECS",
    "AUTONOMY_GOAL_POOL",
    "AUTONOMY_HYPOTHESIS_POOL",
    "AUTONOMY_TOOL_PRIORITY",
]
