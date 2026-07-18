"""Adversarial axiom prober."""
from __future__ import annotations

import logging
import threading

from .constants import CONTRADICTION_MARKERS, PROBE_INTERVAL
from .hypotheses import HypothesisStore
from .text import _episode_text, _substantive_episodes, _tokenise, score_hypothesis_relevance

log = logging.getLogger("k10d.cognition")

class AdversarialProber:
    """
    Stress-tests axioms by measuring support vs contradiction signals in
    substantive episodes. Flags vulnerable (contradicted) and dormant (untested) axioms.
    """
    VULN_THRESHOLD = 0.4
    DORMANT_THRESHOLD = 0.15

    def __init__(self, soul: dict, save_soul_fn, hypotheses: HypothesisStore,
                 state_lock: threading.Lock):
        self.soul         = soul
        self._save        = save_soul_fn
        self.hypotheses   = hypotheses
        self._lock        = state_lock
        self._probe_cycle = 0

    def _contradiction_score(self, axiom: str, episodes: list[dict]) -> float:
        tokens = _tokenise(axiom, meta=True)
        if not tokens:
            return 0.0

        hits = 0
        for ep in episodes:
            text = _episode_text(ep).lower()
            for marker in CONTRADICTION_MARKERS:
                if marker not in text:
                    continue
                # Contradiction near an axiom keyword is stronger signal
                for token in tokens:
                    idx = text.find(token)
                    if idx == -1:
                        continue
                    window = text[max(0, idx - 40): idx + len(token) + 40]
                    if marker in window:
                        hits += 1
                        break
        return hits / max(len(tokens), 1)

    def probe(self, episodes: list[dict], reflection_cycle: int) -> list[dict]:
        if reflection_cycle % PROBE_INTERVAL != 0:
            return []

        self._probe_cycle += 1
        axioms   = self.soul.get("axioms", [])
        corpus   = _substantive_episodes(episodes[-80:])
        if not corpus:
            corpus = episodes[-30:]
        results  = []
        vulnerable = []
        dormant    = []

        for i, axiom in enumerate(axioms):
            support = score_hypothesis_relevance(axiom, corpus)
            contra  = self._contradiction_score(axiom, corpus)
            # High contradiction relative to support → vulnerable
            tension = contra / (support + 0.1)

            entry = {
                "index": i, "axiom": axiom,
                "support_score": round(support, 4),
                "contradiction_score": round(contra, 4),
                "tension": round(tension, 4),
            }

            if support < self.DORMANT_THRESHOLD and contra < 0.1:
                entry["dormant"] = True
                dormant.append(entry)
            elif tension >= self.VULN_THRESHOLD or (contra >= 0.5 and support < 0.3):
                entry["vulnerable"] = True
                vulnerable.append(entry)
                existing = [h for h in self.hypotheses.list_all("open")
                            if f"axiom {i}" in h["text"].lower()]
                if not existing:
                    h = self.hypotheses.spawn(
                        f"axiom {i} may not hold: \"{axiom[:120]}\" "
                        f"(tension={tension:.2f}, support={support:.2f})",
                        source="adversarial_prober")
                    entry["hypothesis_id"] = h["id"]
                    log.warning("Adversarial: axiom %d vulnerable (tension=%.2f) → hypo %s",
                                i, tension, h["id"])
            results.append(entry)

        with self._lock:
            sem = self.soul.setdefault("semantic", {})
            sem["axiom_vulnerabilities"] = vulnerable
            sem["axiom_dormant"] = dormant
            self._save(self.soul)

        return results
