"""Memetic / axiom potentiation engine."""
from __future__ import annotations

import logging
import threading

from .constants import MEME_DECAY_CYCLES
from .text import _bm25, _doc_tokens, _substantive_episodes, _tokenise

log = logging.getLogger("k10d.cognition")

class MemeticEngine:
    """
    Tracks axiom grounding in substantive episodes via BM25 relevance.
    Reinforces applied axioms; flags dormant (never cited) and decaying ones.
    """
    def __init__(self, soul: dict, save_soul_fn, state_lock: threading.Lock):
        self.soul       = soul
        self._save      = save_soul_fn
        self._lock      = state_lock
        self._cycle     = 0
        self.RELEVANCE_THRESHOLD = 0.35

    def _axiom_relevance(self, axiom: str, episodes: list[dict]) -> dict:
        query = _tokenise(axiom, meta=True)
        if not query:
            return {"hit_count": 0, "max_score": 0.0, "mean_score": 0.0}

        docs = [_doc_tokens(ep) for ep in episodes]
        avg_dl = sum(len(d) for d in docs) / max(len(docs), 1)
        scores = [_bm25(query, doc, avg_dl) for doc in docs]
        hits = sum(1 for s in scores if s >= self.RELEVANCE_THRESHOLD)
        return {
            "hit_count": hits,
            "max_score": round(max(scores) if scores else 0.0, 4),
            "mean_score": round(sum(scores) / max(len(scores), 1), 4),
        }

    def update(self, episodes: list[dict]):
        self._cycle += 1
        axioms = self.soul.get("axioms", [])
        if not axioms:
            return

        corpus = _substantive_episodes(episodes[-100:])
        if not corpus:
            corpus = episodes[-30:]
        poten  = self.soul.setdefault("axiom_potentiation", {})

        for i, axiom in enumerate(axioms):
            key = str(i)
            rel = self._axiom_relevance(axiom, corpus)
            entry = poten.setdefault(key, {
                "axiom": axiom, "total_refs": 0, "last_ref_cycle": 0,
                "confidence": 1.0, "decaying": False, "dormant": False,
                "max_relevance": 0.0,
            })
            entry["axiom"] = axiom
            entry["max_relevance"] = rel["max_score"]

            if rel["hit_count"] > 0:
                entry["total_refs"]    += rel["hit_count"]
                entry["last_ref_cycle"] = self._cycle
                entry["confidence"]     = min(1.0, entry["confidence"] + 0.04 * rel["hit_count"])
                entry["decaying"]       = False
                entry["dormant"]        = False
            else:
                entry["dormant"] = entry["total_refs"] == 0
                cycles_silent = self._cycle - entry.get("last_ref_cycle", 0)
                if cycles_silent >= MEME_DECAY_CYCLES:
                    entry["confidence"] = max(0.1, entry["confidence"] - 0.05)
                    entry["decaying"]   = True

        valid_keys = {str(i) for i in range(len(axioms))}
        for k in list(poten.keys()):
            if k not in valid_keys:
                del poten[k]

        with self._lock:
            self._save(self.soul)

        decaying = [v for v in poten.values() if v.get("decaying")]
        dormant  = [v for v in poten.values() if v.get("dormant")]
        if decaying:
            log.info("Memetic: %d axioms decaying, %d dormant", len(decaying), len(dormant))

    def report(self) -> dict:
        poten  = self.soul.get("axiom_potentiation", {})
        axioms = self.soul.get("axioms", [])
        return {
            "cycle": self._cycle,
            "axioms": [
                {"index": int(k), "axiom": axioms[int(k)] if int(k) < len(axioms) else "?",
                 **{kk: vv for kk, vv in v.items() if kk != "axiom"}}
                for k, v in sorted(poten.items(), key=lambda x: int(x[0]))
            ]
        }
