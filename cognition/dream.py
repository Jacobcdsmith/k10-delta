"""Idle-triggered dream engine."""
from __future__ import annotations

import json
import logging
import random
import threading
from collections import Counter
from pathlib import Path

from .constants import DREAM_IDLE_SECS
from .hypotheses import HypothesisStore
from .text import (
    _doc_tokens, _now_iso, _parse_ts, _substantive_episodes, _tfidf_concepts,
    score_hypothesis_relevance,
)

log = logging.getLogger("k10d.cognition")

class DreamEngine:
    """
    Triggered by idleness (no MCP tool calls for DREAM_IDLE_SECS).
    Finds non-obvious bridges between distant episodes, stress-tests hypotheses,
    and synthesises latent facts from co-occurrence patterns.
    """
    def __init__(self, soul: dict, save_soul_fn, read_episodes_fn,
                 append_episode_fn, hypotheses: HypothesisStore,
                 state_lock: threading.Lock, dream_log_path: Path):
        self.soul            = soul
        self._save           = save_soul_fn
        self._read_episodes  = read_episodes_fn
        self._append_episode = append_episode_fn
        self.hypotheses      = hypotheses
        self._lock           = state_lock
        self.dream_log       = dream_log_path
        self._timer_lock     = threading.Lock()
        self._idle_timer: threading.Timer | None = None
        self._pinged_during_dream = False
        self._dreaming       = False
        self._dream_count    = 0
        self._arm()

    def ping(self):
        with self._timer_lock:
            if self._idle_timer:
                self._idle_timer.cancel()
            self._pinged_during_dream = True
            self._arm()

    def force(self):
        """Public entry: run a dream cycle now (used by Will)."""
        threading.Thread(
            target=self._dream, daemon=True, name="dream-force").start()

    def _arm(self):
        self._idle_timer = threading.Timer(DREAM_IDLE_SECS, self._dream)
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def _find_bridges(self, episodes: list[dict], n: int = 3) -> list[dict]:
        """Episodes that share a rare concept but otherwise look unrelated."""
        substantive = _substantive_episodes(episodes)
        if len(substantive) < 4:
            return []

        doc_freq: Counter[str] = Counter()
        ep_tokens: list[tuple[dict, set[str]]] = []
        for ep in substantive:
            tokens = set(_doc_tokens(ep))
            ep_tokens.append((ep, tokens))
            doc_freq.update(tokens)

        bridges = []
        for i in range(len(ep_tokens)):
            for j in range(i + 1, len(ep_tokens)):
                ep_a, tok_a = ep_tokens[i]
                ep_b, tok_b = ep_tokens[j]
                shared = tok_a & tok_b
                if not shared:
                    continue
                # Rare terms only (appear in ≤ 20% of episodes)
                rare = {t for t in shared if doc_freq[t] <= max(2, len(substantive) * 0.2)}
                if not rare:
                    continue
                union = tok_a | tok_b
                jaccard = len(shared) / max(len(union), 1)
                if jaccard > 0.55:
                    continue  # too obvious

                ts_a, ts_b = _parse_ts(ep_a), _parse_ts(ep_b)
                gap_hours = abs(ts_a - ts_b) / 3600 if ts_a and ts_b else 0
                bridges.append({
                    "concept": sorted(rare, key=lambda t: doc_freq[t])[0],
                    "all_shared": sorted(rare)[:4],
                    "jaccard": round(jaccard, 3),
                    "gap_hours": round(gap_hours, 1),
                    "ep_a": ep_a.get("summary", "")[:80],
                    "ep_b": ep_b.get("summary", "")[:80],
                })

        bridges.sort(key=lambda b: (-b["gap_hours"], b["jaccard"]))
        return bridges[:n]

    def _cooccurrence_insights(self, episodes: list[dict]) -> list[str]:
        """Terms that reliably co-occur but aren't top-level concepts on their own."""
        substantive = _substantive_episodes(episodes[-60:])
        pair_counts: Counter[tuple[str, str]] = Counter()
        term_counts: Counter[str] = Counter()

        for ep in substantive:
            tokens = sorted(set(_doc_tokens(ep)))
            term_counts.update(tokens)
            for i, a in enumerate(tokens):
                for b in tokens[i + 1:]:
                    pair_counts[(a, b)] += 1

        insights = []
        for (a, b), count in pair_counts.most_common(20):
            if count < 2:
                break
            if term_counts[a] > count * 2 and term_counts[b] > count * 2:
                insights.append(f"'{a}' and '{b}' co-occur {count}× but rarely alone")
        return insights[:4]

    def _dream(self):
        if self._dreaming:
            return
        self._dreaming    = True
        self._dream_count += 1
        log.info("Dream cycle #%d triggered after %ds idle",
                 self._dream_count, DREAM_IDLE_SECS)
        try:
            episodes = self._read_episodes(500)
            if len(episodes) < 4:
                return

            insights: list[str] = []
            bridges = self._find_bridges(episodes)
            for bridge in bridges:
                insight = (
                    f"Bridge via '{bridge['concept']}' "
                    f"(gap={bridge['gap_hours']:.0f}h, j={bridge['jaccard']}): "
                    f"\"{bridge['ep_a']}\" ↔ \"{bridge['ep_b']}\""
                )
                insights.append(insight)
                if bridge["gap_hours"] >= 24 or bridge["jaccard"] < 0.15:
                    self.hypotheses.spawn(
                        f"[dream] {bridge['concept']} links distant episodes: "
                        f"{bridge['ep_a'][:60]} ... {bridge['ep_b'][:60]}",
                        source="dream")

            insights.extend(self._cooccurrence_insights(episodes))

            open_hs = self.hypotheses.list_all("open")
            substantive = _substantive_episodes(episodes)
            for h in random.sample(open_hs, min(3, len(open_hs))):
                relevance = score_hypothesis_relevance(h["text"], substantive[-40:])
                self.hypotheses.add_evidence(
                    h["id"],
                    f"[dream #{self._dream_count}] substantive relevance={relevance:.2f}",
                    supports=relevance >= 0.45)

            top_concepts = _tfidf_concepts(episodes, top_n=10)
            if top_concepts:
                latent_fact = {
                    "type": "dream_synthesis",
                    "concepts": top_concepts[:6],
                    "bridges": [b["concept"] for b in bridges[:3]],
                    "confidence": 0.25,
                    "dream": self._dream_count,
                }
                with self._lock:
                    sem = self.soul.setdefault("semantic", {})
                    sem.setdefault("dream_facts", []).append(latent_fact)
                    sem["dream_facts"] = sem["dream_facts"][-20:]
                    self._save(self.soul)

            dream_ep = {
                "ts":      _now_iso(),
                "summary": f"[dream #{self._dream_count}]",
                "source":  "dream",
                "detail":  {
                    "insights":     insights,
                    "concepts":     top_concepts[:8],
                    "bridges":      len(bridges),
                    "hypos_tested": min(3, len(open_hs)),
                },
            }
            self._append_episode(dream_ep)
            with self.dream_log.open("a") as f:
                f.write(json.dumps(dream_ep) + "\n")

            log.info("Dream #%d complete — %d insights, %d bridges, %d hypos probed",
                     self._dream_count, len(insights), len(bridges),
                     min(3, len(open_hs)))
        finally:
            self._dreaming = False
            with self._timer_lock:
                if self._pinged_during_dream:
                    self._pinged_during_dream = False
                else:
                    self._arm()

    @property
    def status(self) -> dict:
        return {
            "dream_count": self._dream_count,
            "idle_threshold_seconds": DREAM_IDLE_SECS,
            "currently_dreaming": self._dreaming,
            "dream_facts": self.soul.get("semantic", {}).get("dream_facts", []),
        }
