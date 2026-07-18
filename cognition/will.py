"""Will / AutonomyPolicy — form one intention per tick, execute, audit.

Phase B/D of the K10-Δ agency plan: single primary act per reflection cycle,
continuity via identity_thread / rising_concepts / dream_facts / workspace.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("k10d.cognition")

# Trajectory labels that signal inward collapse / need for outward contact.
_HUNGER_LABELS = frozenset({"echo_chamber", "dormant", "narrowing"})
_HUMANISH_SOURCES = frozenset({
    "human", "user", "migrated", "agent", "creator", "directive",
})
_REVIVAL_PREFIXES = ("will: revival", "autonomy: revival")
_DEFAULT_SEED = "Observe environment and log one substantive finding"
_DEFAULT_SEARCH = "agent trajectory diversity external world"


@dataclass
class Intention:
    kind: str  # pursue | dream | act | spawn_hypo | revive | seed | none | review
    text: str
    tool: str | None = None
    args: dict | None = None
    goal_id: str | None = None
    source: str = "will"


WILL_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "max_acts_per_tick": 1,
    "dream_novelty_threshold": 0.1,
    "dream_novelty_streak": 2,
    "dream_min_gap_s": 1800,
    "stall_revive_max": 2,
    "seed_when_empty": True,
    "prefer_outward": True,
    "outward_namespaces": ("fs", "net", "exec", "hermes", "device", "act", "mqtt"),
    "review_interval_cycles": 10,
    "hypo_cooldown_cycles": 15,
    "max_open_will_hypotheses": 2,
}

# Compat alias for tests / older call sites.
AUTONOMY_DEFAULTS = WILL_DEFAULTS


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _active_creator_directive(directive: dict | None) -> dict | None:
    """Return creator_directive if not expired, else None."""
    if not directive:
        return None
    expires_at = directive.get("expires_at")
    if not expires_at:
        return directive
    try:
        exp = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > exp:
            return None
    except ValueError:
        pass
    return directive


def _preview(value: Any, n: int = 300) -> str:
    try:
        return str(value)[:n]
    except Exception:
        return ""


def _revival_count(goal: dict) -> int:
    count = 0
    for e in goal.get("evidence", []) or []:
        text = str(e.get("text", ""))
        if text.startswith(_REVIVAL_PREFIXES):
            count += 1
    return count


class AutonomyPolicy:
    """Bounded will: one primary intention per tick, audited to episodes."""

    def __init__(
        self,
        *,
        call_tool: Callable[..., Any],
        goals: Any,
        hypotheses: Any,
        dream: Any,
        soul: dict,
        save_soul: Callable[[dict], Any],
        read_episodes: Callable[..., Any],
        usage_stats_fn: Callable[[], dict] | None,
        state_lock: Any,
        append_episode: Callable[[dict], Any],
        workspace: Path | str | None = None,
    ):
        self.call_tool = call_tool
        self.goals = goals
        self.hypotheses = hypotheses
        self.dream = dream
        self.soul = soul
        self.save_soul = save_soul
        self.read_episodes = read_episodes
        self.usage_stats_fn = usage_stats_fn
        self.state_lock = state_lock
        self.append_episode = append_episode
        self.workspace = Path(workspace) if workspace else None

        self._low_novelty_streak = 0
        self._last_hypo_cycle = -10**9
        self.last_intention: Intention | None = None
        self.last_report: dict = {}

    # ── config / continuity ──────────────────────────────────────────────────

    def _config(self) -> dict:
        auto = self.soul.setdefault("autonomy", {})
        for key, value in WILL_DEFAULTS.items():
            auto.setdefault(key, value)
        # Migrate away from fill-to-N goal seeding.
        auto.pop("min_active_goals", None)
        auto.setdefault("counters", {})
        return auto

    def _continuity(self) -> dict:
        identity = self.soul.get("identity_thread") or {}
        semantic = self.soul.get("semantic") or {}
        rising = list(semantic.get("rising_concepts") or [])
        dream_facts = list(semantic.get("dream_facts") or [])[-3:]
        recent_files: list[str] = []
        if self.workspace is not None:
            try:
                root = Path(self.workspace)
                if root.is_dir():
                    files = [
                        p for p in root.iterdir()
                        if p.is_file() and not p.name.startswith(".")
                    ]
                    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                    recent_files = [p.name for p in files[:8]]
            except Exception:
                recent_files = []
        return {
            "identity": identity,
            "rising": rising,
            "dream_facts": dream_facts,
            "recent_files": recent_files,
            "focus": (identity.get("current_focus") or "").strip(),
        }

    def _live_goals(self) -> list[dict]:
        if not self.goals:
            return []
        return (
            list(self.goals.list_all("open"))
            + list(self.goals.list_all("in_progress"))
        )

    def _preferred_live_goal(self) -> dict | None:
        live = self._live_goals()
        if not live:
            return None
        non_auto = [
            g for g in live
            if g.get("source") != "autonomy" and g.get("source") != "will"
        ]
        # Prefer human/user/migrated/agent (and any non-autonomy) first.
        humanish = [
            g for g in non_auto
            if str(g.get("source", "")).lower() in _HUMANISH_SOURCES
        ]
        pool = humanish or non_auto or live
        return sorted(
            pool,
            key=lambda g: (-int(g.get("priority", 0)), g.get("created", "")),
        )[0]

    def _update_novelty_streak(self, trajectory: dict, auto: dict) -> None:
        novelty = trajectory.get("novelty")
        threshold = float(auto.get("dream_novelty_threshold", 0.1))
        if novelty is None or float(novelty) >= threshold:
            self._low_novelty_streak = 0
        else:
            self._low_novelty_streak += 1

    # ── intention formation (first match wins) ───────────────────────────────

    def _form_intention(
        self,
        cycle: int,
        trajectory: dict,
        auto: dict,
        cont: dict,
        probe_results: list | None,
        insights: list | None,
    ) -> Intention:
        # 2. Active creator_directive OR recent pivot feedback
        directive = _active_creator_directive(self.soul.get("creator_directive"))
        pivot = None
        feedback = self.soul.get("creator_feedback") or []
        for f in reversed(feedback[-5:]):
            if f.get("rating") == "pivot" and f.get("text"):
                pivot = f
                break

        if directive or pivot:
            steer_text = (
                (directive or {}).get("directive")
                or (directive or {}).get("text")
                or (pivot or {}).get("text")
                or ""
            )
            steer_text = str(steer_text).strip()
            top = self._preferred_live_goal()
            if top is None and self.goals:
                top = self.goals.top_open() if hasattr(self.goals, "top_open") else None
            if top:
                return Intention(
                    kind="pursue",
                    text=f"Honour creator steer via goal: {top.get('text', '')[:120]}",
                    tool="goal.pursue",
                    args={"step": "auto"},
                    goal_id=top.get("id"),
                )
            if steer_text:
                # Prefer outward search / hermes on the directive text.
                if auto.get("prefer_outward", True):
                    return Intention(
                        kind="act",
                        text=f"Creator steer outward: {steer_text[:120]}",
                        tool="net.search",
                        args={"query": steer_text[:120], "count": 5},
                    )
                return Intention(
                    kind="act",
                    text=f"Creator steer hermes: {steer_text[:120]}",
                    tool="hermes.query",
                    args={"prompt": steer_text[:500], "timeout": 120},
                )

        # 3. Live goals → pursue (prefer non-autonomy sources)
        live_goal = self._preferred_live_goal()
        if live_goal:
            return Intention(
                kind="pursue",
                text=f"Pursue: {live_goal.get('text', '')[:120]}",
                tool="goal.pursue",
                args={"step": "auto"},
                goal_id=live_goal.get("id"),
            )

        # 4. No live goals → revive ONE stalled under budget
        if self.goals:
            revive_max = int(auto.get("stall_revive_max", 2))
            for g in self.goals.list_all("stalled"):
                revivals = _revival_count(g)
                if revivals >= revive_max:
                    continue
                return Intention(
                    kind="revive",
                    text=f"Revive stalled: {g.get('text', '')[:120]}",
                    goal_id=g.get("id"),
                    args={"revival": revivals + 1},
                )

        # 5. World hunger → outward tool
        label = str(trajectory.get("label") or "")
        streak_need = int(auto.get("dream_novelty_streak", 2))
        novelty_hungry = self._low_novelty_streak >= max(1, streak_need)
        if label in _HUNGER_LABELS or (
            auto.get("prefer_outward", True) and novelty_hungry
        ):
            rising = cont.get("rising") or []
            focus = cont.get("focus") or ""
            query = (
                (rising[0] if rising else None)
                or (focus if focus and not focus.startswith("directive:") else None)
                or _DEFAULT_SEARCH
            )
            query = str(query)[:120]
            # Alternate hermes when focus looks deliberative.
            if focus and any(
                k in focus.lower() for k in ("why", "how", "meaning", "identity")
            ):
                return Intention(
                    kind="act",
                    text=f"World hunger hermes ({label or 'low_novelty'}): {query}",
                    tool="hermes.query",
                    args={"prompt": f"Explore and report on: {query}", "timeout": 120},
                )
            return Intention(
                kind="act",
                text=f"World hunger search ({label or 'low_novelty'}): {query}",
                tool="net.search",
                args={"query": query, "count": 5},
            )

        # 6. Dream on sustained low novelty + gap
        if (
            self._low_novelty_streak >= streak_need
            and self.dream is not None
            and self._dream_gap_ok(auto)
        ):
            novelty = trajectory.get("novelty")
            return Intention(
                kind="dream",
                text=(
                    f"Force dream (novelty={novelty}, "
                    f"streak={self._low_novelty_streak})"
                ),
            )

        # 7. Rare hypo from rising concept / dream_fact (not slogan pool)
        hypo = self._maybe_hypo_intention(cycle, auto, cont)
        if hypo is not None:
            return hypo

        # 8. Idle seed — only when zero open+in_progress; never fill-to-N
        if (
            auto.get("seed_when_empty", True)
            and self.goals is not None
            and not self._live_goals()
        ):
            text = self._seed_text(cont)
            return Intention(
                kind="seed",
                text=text,
                args={"priority": 6, "source": "will"},
            )

        # 9. Review handled separately in tick (may run alongside / alone)
        return Intention(kind="none", text="no intention")

    def _dream_gap_ok(self, auto: dict) -> bool:
        gap = float(auto.get("dream_min_gap_s", 1800))
        dream = self.dream
        if dream is None:
            return False
        log_path = getattr(dream, "dream_log", None)
        try:
            if log_path is not None and Path(log_path).exists():
                last = Path(log_path).stat().st_mtime
            else:
                last = 0.0
        except OSError:
            last = 0.0
        return (time.time() - last) >= gap

    def _maybe_hypo_intention(
        self, cycle: int, auto: dict, cont: dict
    ) -> Intention | None:
        if self.hypotheses is None:
            return None
        cooldown = int(auto.get("hypo_cooldown_cycles", 15))
        if cycle - self._last_hypo_cycle < cooldown:
            return None
        open_hs = self.hypotheses.list_all("open")
        n_will = sum(
            1 for h in open_hs
            if h.get("source") in ("will", "autonomy")
        )
        if n_will >= int(auto.get("max_open_will_hypotheses", 2)):
            return None

        rising = cont.get("rising") or []
        facts = cont.get("dream_facts") or []
        text: str | None = None
        if rising:
            concept = str(rising[0]).strip()
            if concept:
                text = (
                    f"Sustained engagement with rising concept '{concept}' "
                    f"increases trajectory novelty"
                )
        if text is None and facts:
            fact = facts[-1]
            if isinstance(fact, dict):
                fact = fact.get("text") or fact.get("fact") or str(fact)
            fact = str(fact).strip()
            if fact:
                text = f"Dream fact holds under scrutiny: {fact[:160]}"
        if not text:
            return None

        # Dedupe against open set when possible.
        norm = " ".join(text.lower().split())[:160]
        for h in open_hs:
            existing = " ".join(str(h.get("text", "")).lower().split())[:160]
            if existing == norm:
                return None

        return Intention(kind="spawn_hypo", text=text)

    def _seed_text(self, cont: dict) -> str:
        focus = cont.get("focus") or ""
        if focus and not focus.startswith(("directive:", "pivot:")):
            return focus[:200]
        if focus.startswith(("directive:", "pivot:")):
            # Still usable — strip prefix for a clean goal.
            body = focus.split(":", 1)[-1].strip()
            if body:
                return body[:200]
        for name in cont.get("recent_files") or []:
            lower = name.lower()
            if lower in (
                "readme.md", "notes.md", "protocols.md", "roadmap.md",
                ".gitkeep",
            ):
                continue
            if name.startswith("_") or name.startswith("."):
                continue
            return f"Finish or advance unfinished workspace item: {name}"[:200]
        return _DEFAULT_SEED

    # ── execution ────────────────────────────────────────────────────────────

    def _execute(self, intention: Intention, cycle: int, auto: dict) -> dict:
        kind = intention.kind
        result: Any = None
        ok = True
        error: str | None = None
        action: dict[str, Any] = {
            "action": kind,
            "text": intention.text[:120],
        }

        try:
            if kind == "pursue" or (kind == "act" and intention.tool):
                tool = intention.tool
                args = intention.args or {}
                if not tool:
                    raise ValueError("act/pursue requires tool")
                result = self.call_tool(tool, args)
                action["tool"] = tool
                action["args"] = args
                if intention.goal_id:
                    action["goal_id"] = intention.goal_id

            elif kind == "dream":
                force = getattr(self.dream, "force", None)
                if not callable(force):
                    ok = False
                    error = "dream.force unavailable"
                else:
                    force()
                    self._low_novelty_streak = 0
                    action["action"] = "dream_force"

            elif kind == "revive":
                gid = intention.goal_id
                if not gid or not self.goals:
                    raise ValueError("revive requires goal_id and goals store")
                revival_n = int((intention.args or {}).get("revival", 1))
                self.goals.update(gid, status="open")
                self.goals.add_evidence(
                    gid,
                    f"will: revival #{revival_n} (no active goals)",
                )
                action["id"] = gid
                action["action"] = "goal_revive"
                action["revival"] = revival_n
                result = {"id": gid, "status": "open", "revival": revival_n}

            elif kind == "spawn_hypo":
                h = self.hypotheses.spawn(intention.text, source="will")
                self._last_hypo_cycle = cycle
                action["action"] = "hypothesis_seed"
                action["id"] = h.get("id")
                action["text"] = intention.text[:120]
                result = h

            elif kind == "seed":
                goal = self.goals.add(
                    intention.text,
                    source="will",
                    priority=int((intention.args or {}).get("priority", 6)),
                )
                action["action"] = "goal_seed"
                action["id"] = goal.get("id")
                action["text"] = intention.text[:120]
                intention.goal_id = goal.get("id")
                result = goal

            elif kind in ("none", "review"):
                action["action"] = kind
                result = None

            else:
                ok = False
                error = f"unknown intention kind: {kind}"

        except Exception as e:
            ok = False
            error = str(e)
            log.warning("Will execute %s failed: %s", kind, e)

        action["ok"] = ok
        if error:
            action["error"] = error
        preview = _preview(result)
        action["result_preview"] = preview

        # Counters
        counters = auto.setdefault("counters", {})
        if ok and kind not in ("none", "review"):
            key = {
                "pursue": "pursuits",
                "act": "outward_acts",
                "dream": "dreams_forced",
                "revive": "goals_revived",
                "spawn_hypo": "hypos_seeded",
                "seed": "goals_seeded",
            }.get(kind)
            if key:
                counters[key] = counters.get(key, 0) + 1

        return {
            "action": action,
            "ok": ok,
            "error": error,
            "result_preview": preview,
            "result": result,
        }

    def _audit(
        self,
        intention: Intention,
        exec_report: dict | None,
        cycle: int,
    ) -> None:
        kind = intention.kind
        if kind in ("none",):
            return
        detail = {
            "intention": asdict(intention),
            "result_preview": (exec_report or {}).get("result_preview"),
            "cycle": cycle,
            "ok": (exec_report or {}).get("ok", True),
        }
        if exec_report and exec_report.get("error"):
            detail["error"] = exec_report["error"]
        try:
            self.append_episode({
                "ts": _now_iso(),
                "summary": f"[will] {kind}",
                "source": "will",
                "detail": detail,
            })
        except Exception as e:
            log.warning("Will audit episode failed: %s", e)

    def _maybe_review(
        self,
        cycle: int,
        trajectory: dict,
        auto: dict,
        *,
        had_act: bool,
    ) -> dict | None:
        interval = int(auto.get("review_interval_cycles", 10))
        if interval < 1 or cycle % interval != 0:
            return None
        never_called: list[str] = []
        if self.usage_stats_fn:
            try:
                never_called = list(
                    (self.usage_stats_fn() or {}).get("never_called") or []
                )
            except Exception:
                pass
        live = self._live_goals()
        stalled = (
            len(self.goals.list_all("stalled")) if self.goals else 0
        )
        open_hypos = (
            len(self.hypotheses.list_all("open")) if self.hypotheses else 0
        )
        review = {
            "cycle": cycle,
            "ts": _now_iso(),
            "active_goals": len(live),
            "stalled_goals": stalled,
            "open_hypotheses": open_hypos,
            "novelty": trajectory.get("novelty"),
            "meta_ratio": trajectory.get("meta_ratio"),
            "label": trajectory.get("label"),
            "never_called_tools": len(never_called),
            "counters": dict(auto.get("counters") or {}),
            "alongside_act": had_act,
        }
        auto["last_review"] = review
        log.info("Will review: %s", review)
        try:
            self.append_episode({
                "ts": _now_iso(),
                "summary": f"[will] review cycle={cycle}",
                "source": "will",
                "detail": review,
            })
        except Exception as e:
            log.warning("Will review episode failed: %s", e)
        return review

    # ── public tick ──────────────────────────────────────────────────────────

    def tick(
        self,
        cycle: int,
        trajectory: dict,
        probe_results: list | None = None,
        insights: list | None = None,
    ) -> dict:
        """Form at most one primary intention, execute, audit. Returns report."""
        auto = self._config()
        if not auto.get("enabled", True):
            none = Intention(kind="none", text="autonomy disabled")
            report = {
                "enabled": False,
                "actions": [],
                "intention": asdict(none),
                "counters": dict(auto.get("counters") or {}),
            }
            self.last_intention = none
            self.last_report = report
            auto["last_intention"] = asdict(none)
            auto["last_tick_cycle"] = cycle
            return report

        trajectory = trajectory or {}
        self._update_novelty_streak(trajectory, auto)
        cont = self._continuity()

        intention = self._form_intention(
            cycle, trajectory, auto, cont, probe_results, insights
        )

        actions: list[dict] = []
        exec_report: dict | None = None
        had_act = intention.kind not in ("none", "review")

        if had_act:
            exec_report = self._execute(intention, cycle, auto)
            actions.append(exec_report["action"])
            self._audit(intention, exec_report, cycle)
            log.info("Will: %s — %s", intention.kind, intention.text[:80])

        review = self._maybe_review(
            cycle, trajectory, auto, had_act=had_act
        )

        # If nothing else fired and review is due, review is the only output
        # (already appended above). Surface it as intention kind=review when
        # no primary act ran.
        if not had_act and review is not None:
            intention = Intention(
                kind="review",
                text=f"Periodic autonomy review cycle={cycle}",
            )

        auto["last_intention"] = asdict(intention)
        auto["last_tick_cycle"] = cycle

        try:
            with self.state_lock:
                self.save_soul(self.soul)
        except Exception as e:
            log.warning("Will save_soul failed: %s", e)

        report: dict[str, Any] = {
            "enabled": True,
            "actions": actions,
            "intention": asdict(intention),
            "review": review,
            "counters": dict(auto.get("counters") or {}),
        }
        if exec_report is not None:
            report["result_preview"] = exec_report.get("result_preview")
            report["ok"] = exec_report.get("ok")
        self.last_intention = intention
        self.last_report = report
        return report

