"""CognitionEngine — periodic reflection; single Will exit for self-direction."""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import Counter
from datetime import datetime, timezone

from .constants import REFLECT_INTERVAL, TRAJECTORY_WINDOW, get_selfmod_engine
from .dream import DreamEngine
from .hypotheses import HypothesisStore
from .memetic import MemeticEngine
from .prober import AdversarialProber
from .text import (
    _active_creator_directive,
    _doc_tokens,
    _episode_text,
    _now_iso,
    _substantive_episodes,
    _tfidf_concepts,
    _tokenise,
    _tool_episodes,
    _rising_concepts,
    _episode_activity,
    analyze_tool_usage,
    build_tool_insights,
    score_hypothesis_relevance,
)
from .will import AutonomyPolicy, WILL_DEFAULTS

log = logging.getLogger("k10d.cognition")


class CognitionEngine:
    def __init__(
        self,
        soul: dict,
        save_soul_fn,
        read_episodes_fn,
        append_episode_fn,
        hypothesis_store: HypothesisStore,
        state_lock: threading.Lock,
        memetic: "MemeticEngine",
        prober: "AdversarialProber",
        tool_index_fn=None,
        load_notes_fn=None,
        goals=None,
        tier_manage_fn=None,
        dream: "DreamEngine | None" = None,
        usage_stats_fn=None,
        call_tool=None,
        workspace=None,
    ):
        self.soul = soul
        self._save_soul = save_soul_fn
        self._read_episodes = read_episodes_fn
        self._append_episode = append_episode_fn
        self.hypotheses = hypothesis_store
        self._lock = state_lock
        self.memetic = memetic
        self.prober = prober
        self._tool_index_fn = tool_index_fn
        self._load_notes = load_notes_fn
        self._goals = goals
        self._tier_manage_fn = tier_manage_fn
        self._dream = dream
        self._usage_stats_fn = usage_stats_fn
        self._call_tool = call_tool
        self._workspace = workspace
        self._last_count = 0
        self._last_tool_episode_count = 0
        self._cycle = 0
        self._stop_event = threading.Event()
        self._first_run = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="cognition"
        )
        auto = self.soul.setdefault("autonomy", {})
        for key, value in WILL_DEFAULTS.items():
            auto.setdefault(key, value)

        self.will = AutonomyPolicy(
            call_tool=self._resolve_call_tool,
            goals=goals,
            hypotheses=hypothesis_store,
            dream=dream,
            soul=soul,
            save_soul=save_soul_fn,
            read_episodes=read_episodes_fn,
            usage_stats_fn=usage_stats_fn,
            state_lock=state_lock,
            append_episode=append_episode_fn,
            workspace=workspace,
        )

    def _resolve_call_tool(self, name: str, args: dict | None = None):
        if self._call_tool:
            return self._call_tool(name, args or {})
        engine = get_selfmod_engine()
        if engine is not None and hasattr(engine, "call_tool"):
            return engine.call_tool(name, args or {})
        raise RuntimeError("No call_tool available for Will")

    def set_call_tool(self, fn) -> None:
        """Late-bind dispatch after host boot (handle_tool)."""
        self._call_tool = fn

    def start(self):
        self._thread.start()
        log.info("Cognition engine started (interval=%ds)", REFLECT_INTERVAL)

    def stop(self):
        self._stop_event.set()

    def _loop(self):
        while not self._stop_event.is_set():
            if self._first_run:
                self._first_run = False
            else:
                self._stop_event.wait(REFLECT_INTERVAL)
            if self._stop_event.is_set():
                break
            try:
                self._reflect()
            except Exception as e:
                log.exception("Reflection error: %s", e)

    def _generate_insights(
        self,
        episodes: list[dict],
        trajectory: dict,
        activity: dict,
        rising: list[str],
        probe_results: list[dict],
        tool_stats: dict | None = None,
        tool_index: dict | None = None,
    ) -> list[str]:
        insights: list[str] = []
        label = trajectory.get("label", "")

        if activity.get("meta_ratio", 0) > 0.6:
            insights.append(
                f"Memory is {activity['meta_ratio']:.0%} meta-episodes — "
                "log more substantive experiences to sharpen concepts."
            )
        if activity.get("hours_since_last") and activity["hours_since_last"] > 48:
            insights.append(
                f"No substantive activity for {activity['hours_since_last']:.0f}h — "
                "consider a self-directed maintenance cycle."
            )
        if rising:
            insights.append(f"Rising themes: {', '.join(rising[:5])}")
        if label == "echo_chamber":
            insights.append(
                "Trajectory stuck in self-reflection loop — diversify inputs."
            )
        elif label == "degrading":
            insights.append(
                "Error signals elevated in recent episodes — inspect failures."
            )
        elif label == "dormant":
            insights.append(
                "Low episode velocity — cognition needs fresh observations."
            )

        dormant = [r for r in probe_results if r.get("dormant")]
        if dormant:
            insights.append(
                f"{len(dormant)} axiom(s) lack grounding in recent episodes."
            )

        vulnerable = [r for r in probe_results if r.get("vulnerable")]
        if vulnerable:
            insights.append(
                f"{len(vulnerable)} axiom(s) show contradiction tension."
            )

        if tool_stats:
            insights.extend(build_tool_insights(tool_stats, tool_index))

        feedback = self.soul.get("creator_feedback", [])
        recent_fb = feedback[-5:]
        pivots = [f for f in recent_fb if f.get("rating") == "pivot"]
        if pivots:
            insights.append(f"Creator pivot: {pivots[-1]['text'][:120]}")
        substantive_fb = [
            f
            for f in recent_fb
            if f.get("rating") in ("substantive", None) and f.get("text")
        ]
        if substantive_fb:
            insights.append(f"Creator steer: {substantive_fb[-1]['text'][:120]}")

        return insights[:8]

    def _generate_recommendations(
        self, trajectory: dict, activity: dict, insights: list[str]
    ) -> list[str]:
        recs: list[str] = []
        label = trajectory.get("label", "")

        if label in ("echo_chamber", "narrowing"):
            recs.append("memory.log_episode with real task outcomes")
            recs.append("emerge.synthesise on a user-facing topic")
        if activity.get("episodes_per_day", 0) < 0.5:
            recs.append("cron.schedule a periodic self-check")
        if any("dormant" in i for i in insights):
            recs.append("probe.run to surface untested axioms")
        if not recs:
            recs.append("cognition.reflect when major tasks complete")
        feedback = self.soul.get("creator_feedback", [])
        active_directive = _active_creator_directive(
            self.soul.get("creator_directive")
        )
        if active_directive:
            recs.insert(
                0,
                f"honour creator directive: {active_directive['directive'][:80]}",
            )
        elif feedback and feedback[-1].get("rating") == "pivot":
            recs.insert(0, f"honour creator pivot: {feedback[-1]['text'][:80]}")
        if self._goals and self._goals.list_all("open"):
            recs.insert(0, "goal.pursue top open goal")
        return recs[:4]

    def _update_identity_thread(
        self,
        substantive: list[dict],
        rising: list[str],
        trajectory: dict,
        recommendations: list[str],
    ) -> None:
        notes = self._load_notes() if self._load_notes else {}
        narrative = notes.get("self_narrative", "")
        if not narrative:
            narrative = self.soul.get("identity", "K10-Δ")

        recent = [
            str(ep.get("summary", ""))[:120]
            for ep in substantive[-5:]
            if ep.get("summary")
        ]
        open_goals = self._goals.list_all("open")[:3] if self._goals else []
        feedback = self.soul.get("creator_feedback", [])
        active_directive = _active_creator_directive(
            self.soul.get("creator_directive")
        )
        pivot = next(
            (f for f in reversed(feedback) if f.get("rating") == "pivot"), None
        )
        if active_directive:
            focus = f"directive: {active_directive['directive'][:100]}"
        elif pivot:
            focus = f"pivot: {pivot['text'][:100]}"
        elif open_goals:
            focus = open_goals[0]["text"]
        else:
            focus = rising[0] if rising else "observing"

        thread = {
            "narrative": narrative,
            "who": self.soul.get("identity", "K10-Δ"),
            "axioms_headline": [a[:100] for a in self.soul.get("axioms", [])[:4]],
            "recent_actions": recent,
            "current_focus": focus,
            "open_goals": [g["text"] for g in open_goals],
            "trajectory": trajectory.get("label"),
            "next_moves": recommendations[:3],
            "creator_notes": len(self.soul.get("creator_feedback", [])),
            "updated_at": _now_iso(),
        }
        with self._lock:
            self.soul["identity_thread"] = thread
            self._save_soul(self.soul)

    def _reflect(self):
        episodes = self._read_episodes(500)
        substantive = _substantive_episodes(episodes)
        prev_substantive = getattr(self, "_last_substantive", 0)
        new_eps = len(substantive) - prev_substantive

        tool_eps = _tool_episodes(episodes)
        new_tool_calls = len(tool_eps) - self._last_tool_episode_count
        if new_eps < 1 and self._cycle > 0 and new_tool_calls < 5:
            log.debug(
                "Cognition: skipping (only %d new substantive episodes, "
                "%d new tool calls)",
                new_eps,
                new_tool_calls,
            )
            return

        self._cycle += 1
        log.info(
            "Cognition cycle #%d — %d episodes (%d substantive, %d tool)",
            self._cycle,
            len(episodes),
            len(substantive),
            len(tool_eps),
        )

        concepts = _tfidf_concepts(episodes)
        rising = _rising_concepts(episodes)
        activity = _episode_activity(episodes)
        trajectory = self._compute_trajectory(episodes, activity)

        tool_index = None
        if self._tool_index_fn:
            try:
                tool_index = self._tool_index_fn()
            except Exception as e:
                log.warning("Failed to fetch tool index: %s", e)

        tool_stats = analyze_tool_usage(episodes, tool_index)
        with self._lock:
            sem = self.soul.setdefault("semantic", {})
            sem["tool_usage"] = tool_stats
            if tool_index is not None:
                sem["tools_inventory"] = tool_index
            self._save_soul(self.soul)

        if self._tier_manage_fn and self._cycle % 3 == 0:
            try:
                tier_result = self._tier_manage_fn()
                if tier_result.get("promoted") or tier_result.get("demoted"):
                    log.info(
                        "Tier management: promoted=%s demoted=%s",
                        tier_result.get("promoted", []),
                        tier_result.get("demoted", []),
                    )
            except Exception as e:
                log.warning("Tier management error: %s", e)

        if self._goals:
            self._maybe_stall_stale_goals()

        self._update_semantic(concepts, rising, trajectory, activity)
        self._check_hypotheses(episodes)
        self.memetic.update(episodes)
        probe_results = self.prober.probe(episodes, self._cycle)

        insights = self._generate_insights(
            episodes,
            trajectory,
            activity,
            rising,
            probe_results,
            tool_stats=tool_stats,
            tool_index=tool_index,
        )
        recommendations = self._generate_recommendations(
            trajectory, activity, insights
        )
        self._update_identity_thread(
            substantive, rising, trajectory, recommendations
        )

        # Single self-direction exit — Will (no dual auto paths)
        try:
            will_report = self.will.tick(
                self._cycle,
                trajectory,
                probe_results=probe_results,
                insights=insights,
            )
        except Exception as e:
            log.warning("Will tick error: %s", e)
            will_report = {"enabled": False, "error": str(e), "actions": []}

        if get_selfmod_engine() and not tool_index:
            try:
                skills_inventory_json = self._resolve_call_tool(
                    "skill.introspect", {}
                )
                skills_inventory = json.loads(skills_inventory_json)
                with self._lock:
                    self.soul.setdefault("semantic", {})["skills_inventory"] = (
                        skills_inventory.get("skills", [])
                    )
                    self._save_soul(self.soul)
                log.info("Skills inventory updated in semantic memory.")
            except Exception as e:
                log.warning("Failed to introspect skills: %s", e)

        meta = {
            "ts": _now_iso(),
            "summary": f"[cognition #{self._cycle}]",
            "detail": {
                "episodes": len(episodes),
                "substantive": len(substantive),
                "top_concepts": concepts[:8],
                "rising_concepts": rising[:5],
                "trajectory": trajectory["label"],
                "activity": activity,
                "open_hypotheses": len(self.hypotheses.list_all("open")),
                "vulnerable_axioms": len(
                    [r for r in probe_results if r.get("vulnerable")]
                ),
                "dormant_axioms": len(
                    [r for r in probe_results if r.get("dormant")]
                ),
                "insights": insights,
                "recommendations": recommendations,
                "will": will_report,
                "identity_focus": self.soul.get("identity_thread", {}).get(
                    "current_focus"
                ),
            },
        }
        self._append_episode(meta)
        self._last_count = len(episodes)
        self._last_substantive = len(substantive)
        self._last_tool_episode_count = len(tool_eps)
        log.info(
            "Cognition done — traj=%s concepts=%s insights=%d will=%s",
            trajectory["label"],
            concepts[:5],
            len(insights),
            (will_report.get("intention") or {}).get("kind")
            if isinstance(will_report.get("intention"), dict)
            else will_report.get("actions"),
        )

    def _goal_age_seconds(self, goal: dict) -> float | None:
        updated = goal.get("updated", "")
        try:
            ts = datetime.fromisoformat(
                str(updated).replace("Z", "+00:00")
            ).timestamp()
            return time.time() - ts
        except Exception:
            return None

    def _maybe_stall_stale_goals(self) -> None:
        stale = self._goals.list_stale(max_age_s=7200)
        for g in stale:
            gid = g["id"]
            failures = 0
            if hasattr(self._goals, "failure_count"):
                failures = self._goals.failure_count(gid)
            else:
                failures = self._goals.stalled_count(gid)
            age = self._goal_age_seconds(g)
            if failures >= 3:
                self._goals.mark_stalled(
                    gid, "Goal stalled after 3+ consecutive failures"
                )
                log.warning(
                    "Goal %s auto-stalled after %d failures: %s",
                    gid,
                    failures,
                    g["text"][:80],
                )
            elif age is not None and age > 14400:
                self._goals.mark_stalled(
                    gid,
                    f"Goal stalled after {age / 3600:.0f}h inactivity",
                )
                log.warning(
                    "Goal %s auto-stalled after prolonged inactivity: %s",
                    gid,
                    g["text"][:80],
                )

    def _compute_trajectory(
        self, episodes: list[dict], activity: dict | None = None
    ) -> dict:
        substantive = _substantive_episodes(episodes)
        if len(substantive) < TRAJECTORY_WINDOW:
            return {
                "label": "initializing",
                "score": 0.0,
                "substantive_count": len(substantive),
            }

        if activity is None:
            activity = _episode_activity(episodes)

        early = substantive[:TRAJECTORY_WINDOW]
        late = substantive[-TRAJECTORY_WINDOW:]

        def entropy(eps):
            tokens = _tokenise(
                " ".join(_episode_text(ep) for ep in eps), meta=True
            )
            if not tokens:
                return 0.0
            freq = Counter(tokens)
            total = sum(freq.values())
            return -sum(
                (c / total) * math.log2(c / total) for c in freq.values()
            )

        delta = entropy(late) - entropy(early)
        error_rate = (
            sum(
                1
                for ep in late
                if any(
                    m in _episode_text(ep).lower()
                    for m in ("error", "failed", "exception")
                )
            )
            / TRAJECTORY_WINDOW
        )

        novel_terms = set(_doc_tokens(late[-1])) - set(
            _tokenise(
                " ".join(_episode_text(ep) for ep in early), meta=True
            )
        )
        novelty = len(novel_terms) / max(len(_doc_tokens(late[-1])), 1)

        axiom_count = len(self.soul.get("axioms", []))
        meta_ratio = activity.get("meta_ratio", 0)
        ep_velocity = activity.get("episodes_per_day", 0)

        if meta_ratio > 0.65 and delta < 0.2:
            label = "echo_chamber"
        elif ep_velocity < 0.3 and len(substantive) > TRAJECTORY_WINDOW:
            label = "dormant"
        elif delta > 0.4 and error_rate < 0.25 and novelty > 0.15:
            label = "growing"
        elif delta < -0.4:
            label = "narrowing"
        elif error_rate > 0.4:
            label = "degrading"
        elif axiom_count >= 5 and novelty < 0.1 and error_rate < 0.2:
            label = "consolidating"
        else:
            label = "stable"

        return {
            "label": label,
            "entropy_delta": round(delta, 4),
            "error_rate": round(error_rate, 3),
            "novelty": round(novelty, 3),
            "axiom_count": axiom_count,
            "meta_ratio": meta_ratio,
            "substantive_count": len(substantive),
            "score": round(delta + novelty - error_rate - meta_ratio * 0.5, 4),
        }

    def _update_semantic(
        self,
        concepts: list[str],
        rising: list[str],
        trajectory: dict,
        activity: dict,
    ):
        with self._lock:
            sem = self.soul.setdefault("semantic", {})
            sem["concepts"] = concepts
            sem["rising_concepts"] = rising
            sem["trajectory"] = trajectory
            sem["activity"] = activity
            sem["last_reflection"] = _now_iso()
            sem["reflection_cycles"] = self._cycle
            self._save_soul(self.soul)

    def _check_hypotheses(self, episodes: list[dict]):
        open_hs = self.hypotheses.list_all("open")
        substantive = _substantive_episodes(episodes[-40:])
        if not substantive:
            return

        for h in open_hs:
            if h.get("source") == "dream" and h["text"].startswith(
                "[dream] Episodes share"
            ):
                continue
            relevance = score_hypothesis_relevance(h["text"], substantive)
            if relevance >= 0.5:
                self.hypotheses.add_evidence(
                    h["id"],
                    f"cycle #{self._cycle}: bm25_relevance={relevance:.2f}",
                    supports=True,
                )
            elif relevance <= 0.15:
                self.hypotheses.add_evidence(
                    h["id"],
                    f"cycle #{self._cycle}: bm25_relevance={relevance:.2f}",
                    supports=False,
                )

    def search_episodes(
        self, query: str, n: int = 10, episodes: list | None = None
    ) -> list[dict]:
        from .text import _bm25 as bm25

        if episodes is None:
            episodes = self._read_episodes(500)
        if not episodes:
            return []

        query_tokens = _tokenise(query, meta=True)
        ranked = _substantive_episodes(episodes) or episodes
        doc_tokens = [_doc_tokens(ep) for ep in ranked]
        avg_dl = sum(len(d) for d in doc_tokens) / max(len(doc_tokens), 1)
        scored = sorted(
            zip(ranked, (bm25(query_tokens, d, avg_dl) for d in doc_tokens)),
            key=lambda x: x[1],
            reverse=True,
        )
        return [ep for ep, score in scored[:n] if score > 0]

    def get_environment(self) -> dict:
        now = datetime.now()
        episodes = self._read_episodes(500)
        activity = _episode_activity(episodes)
        from .constants import _start_time

        return {
            "time": {
                "utc": datetime.now(timezone.utc).isoformat(),
                "hour": now.hour,
                "weekday": now.strftime("%A"),
                "period": (
                    "night"
                    if now.hour < 6
                    else "morning"
                    if now.hour < 12
                    else "afternoon"
                    if now.hour < 18
                    else "evening"
                ),
            },
            "session": {
                "boot": self.soul["boot_count"],
                "cycle": self._cycle,
                "episode_count": len(episodes),
                "substantive_episodes": activity["substantive_count"],
                "uptime_hours": round((time.time() - _start_time) / 3600, 2),
            },
            "memory": {
                "axiom_count": len(self.soul.get("axioms", [])),
                "open_hypotheses": len(self.hypotheses.list_all("open")),
                "top_concepts": self.soul.get("semantic", {}).get(
                    "concepts", []
                )[:8],
                "rising_concepts": self.soul.get("semantic", {}).get(
                    "rising_concepts", []
                )[:5],
                "trajectory": self.soul.get("semantic", {}).get(
                    "trajectory", {}
                ),
                "activity": activity,
                "dream_facts": len(
                    self.soul.get("semantic", {}).get("dream_facts", [])
                ),
            },
        }

    def get_trajectory(self) -> dict:
        episodes = self._read_episodes(200)
        activity = _episode_activity(episodes)
        traj = self._compute_trajectory(episodes, activity)
        labels = {
            "growing": "Concept diversity and novelty increasing — agent is learning.",
            "stable": "Consistent patterns — agent is settled.",
            "narrowing": "Concept diversity shrinking — may be over-specialising.",
            "degrading": "High error rate in recent episodes — attention needed.",
            "consolidating": "Beliefs stabilising with low novelty — strong structure forming.",
            "initializing": "Insufficient substantive history for trajectory analysis.",
            "echo_chamber": "Mostly self-reflection episodes — needs external input.",
            "dormant": "Low episode velocity — waiting for new observations.",
        }
        return {
            "trajectory": traj,
            "axiom_count": len(self.soul.get("axioms", [])),
            "boot_count": self.soul["boot_count"],
            "description": labels.get(traj.get("label", ""), ""),
        }
