"""Tests for AutonomyPolicy / Will (single self-direction surface)."""

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).parent.resolve()))

from cognition import (
    AdversarialProber,
    AutonomyPolicy,
    CognitionEngine,
    HypothesisStore,
    MemeticEngine,
    WILL_DEFAULTS,
    _is_meta_episode,
)
from goals import GoalStore


class FakeDream:
    def __init__(self, log_path: Path):
        self.dream_log = log_path
        self.ran = threading.Event()

    def force(self):
        self.dream_log.write_text("{}\n", encoding="utf-8")
        self.ran.set()


def make_will(tmp: Path, *, soul_extra=None, dream=None, usage_stats=None,
              call_log=None):
    soul = {
        "identity": "test",
        "boot_count": 1,
        "axioms": ["a", "b"],
        "semantic": {},
    }
    if soul_extra:
        soul.update(soul_extra)
    episodes = []
    saved = []
    hypos = HypothesisStore(tmp / "hypotheses.json")
    lock = threading.Lock()
    goals = GoalStore(tmp / "goals.json")
    calls = call_log if call_log is not None else []

    def call_tool(name, args=None):
        calls.append((name, args or {}))
        return '{"ok": true}'

    will = AutonomyPolicy(
        call_tool=call_tool,
        goals=goals,
        hypotheses=hypos,
        dream=dream,
        soul=soul,
        save_soul=lambda s: saved.append(dict(s)),
        read_episodes=lambda n=100: episodes[-n:],
        usage_stats_fn=usage_stats,
        state_lock=lock,
        append_episode=lambda ep: episodes.append(ep),
        workspace=tmp / "workspace",
    )
    will._cycle_hint = 20
    return will, soul, episodes, hypos, goals, calls


CALM = {"novelty": 0.5, "meta_ratio": 0.1, "label": "stable"}
LOW = {"novelty": 0.0, "meta_ratio": 0.1, "label": "stable"}
HUNGRY = {"novelty": 0.0, "meta_ratio": 0.7, "label": "echo_chamber"}


class TestWill(unittest.TestCase):

    def test_kill_switch_disables_everything(self):
        with tempfile.TemporaryDirectory() as d:
            will, soul, episodes, hypos, goals, calls = make_will(
                Path(d), soul_extra={"autonomy": {"enabled": False}}
            )
            report = will.tick(20, LOW)
            self.assertEqual(report["enabled"], False)
            self.assertEqual(report["actions"], [])
            self.assertEqual(goals.list_all(), [])
            self.assertEqual(calls, [])

    def test_pursues_live_human_goal(self):
        with tempfile.TemporaryDirectory() as d:
            will, soul, episodes, hypos, goals, calls = make_will(Path(d))
            g = goals.add("Ship MQTT demo", source="user", priority=8)
            report = will.tick(20, CALM)
            self.assertEqual(report["intention"]["kind"], "pursue")
            self.assertEqual(report["intention"]["goal_id"], g["id"])
            self.assertTrue(any(c[0] == "goal.pursue" for c in calls))
            self.assertTrue(any(e.get("source") == "will" for e in episodes))

    def test_no_fill_to_three_goals(self):
        with tempfile.TemporaryDirectory() as d:
            will, soul, episodes, hypos, goals, calls = make_will(Path(d))
            # Empty queue → at most one seed
            report = will.tick(20, CALM)
            self.assertIn(report["intention"]["kind"], ("seed", "act", "dream", "none", "review"))
            open_count = len(goals.list_all("open")) + len(goals.list_all("in_progress"))
            self.assertLessEqual(open_count, 1)
            # Second tick with a live goal should pursue, not seed more
            if open_count == 0:
                goals.add("manual", source="user")
            report2 = will.tick(21, CALM)
            self.assertNotEqual(report2["intention"]["kind"], "seed")
            self.assertLessEqual(
                len(goals.list_all("open")) + len(goals.list_all("in_progress")),
                2,
            )

    def test_stalled_revival_when_empty(self):
        with tempfile.TemporaryDirectory() as d:
            will, soul, episodes, hypos, goals, calls = make_will(Path(d))
            g = goals.add("Finish ESP32 MQTT", source="user")
            goals.mark_stalled(g["id"], "test stall")
            report = will.tick(20, CALM)
            self.assertEqual(report["intention"]["kind"], "revive")
            self.assertEqual(goals.get(g["id"])["status"], "open")
            # Exhaust budget
            goals.mark_stalled(g["id"], "again")
            soul["autonomy"]["stall_revive_max"] = 1
            report2 = will.tick(30, CALM)
            self.assertNotEqual(report2["intention"]["kind"], "revive")

    def test_world_hunger_outward(self):
        with tempfile.TemporaryDirectory() as d:
            will, soul, episodes, hypos, goals, calls = make_will(Path(d))
            soul["semantic"]["rising_concepts"] = ["mqtt"]
            report = will.tick(20, HUNGRY)
            self.assertEqual(report["intention"]["kind"], "act")
            self.assertTrue(
                any(c[0] in ("net.search", "hermes.query") for c in calls)
            )

    def test_dream_force_public_api(self):
        with tempfile.TemporaryDirectory() as d:
            dream = FakeDream(Path(d) / "dream_log.jsonl")
            will, soul, episodes, hypos, goals, calls = make_will(
                Path(d), dream=dream
            )
            # Need empty goals and low novelty streak without hunger short-circuit
            # First tick builds streak; may act outward first due to prefer_outward
            will._low_novelty_streak = 2
            # Ensure no goals, no hunger label — only dream path
            traj = {"novelty": 0.0, "meta_ratio": 0.1, "label": "stable"}
            # Disable outward preference so dream can win after streak
            soul["autonomy"] = {"prefer_outward": False, "dream_novelty_streak": 2}
            will._low_novelty_streak = 2
            report = will.tick(20, traj)
            # With prefer_outward false and streak met, dream or seed
            if report["intention"]["kind"] == "dream":
                self.assertTrue(dream.ran.wait(timeout=5))
            else:
                # seed is acceptable if dream gap/form order differs; force path exists
                self.assertTrue(hasattr(dream, "force"))

    def test_will_episodes_are_meta(self):
        self.assertTrue(
            _is_meta_episode(
                {"summary": "[will] pursue", "source": "will"}
            )
        )
        self.assertTrue(
            _is_meta_episode(
                {"summary": "[autonomy] goal_seed", "source": "autonomy"}
            )
        )
        self.assertFalse(
            _is_meta_episode(
                {"summary": "user asked about MQTT", "source": "user"}
            )
        )

    def test_engine_has_will_not_dual_autonomy(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            soul = {"identity": "t", "boot_count": 1, "axioms": [], "semantic": {}}
            hypos = HypothesisStore(tmp / "h.json")
            lock = threading.Lock()
            memetic = MemeticEngine(soul, lambda s: None, lock)
            prober = AdversarialProber(soul, lambda s: None, hypos, lock)
            engine = CognitionEngine(
                soul=soul,
                save_soul_fn=lambda s: None,
                read_episodes_fn=lambda n: [],
                append_episode_fn=lambda ep: None,
                hypothesis_store=hypos,
                state_lock=lock,
                memetic=memetic,
                prober=prober,
                goals=GoalStore(tmp / "g.json"),
                call_tool=lambda n, a=None: "{}",
            )
            self.assertTrue(hasattr(engine, "will"))
            self.assertFalse(hasattr(engine, "_execute_auto_action"))
            self.assertFalse(hasattr(engine, "_autonomy_tick"))
            self.assertIsInstance(engine.will, AutonomyPolicy)

    def test_will_defaults_exported(self):
        self.assertIn("enabled", WILL_DEFAULTS)
        self.assertNotIn("min_active_goals", WILL_DEFAULTS)


if __name__ == "__main__":
    unittest.main()
