"""Goal pursuit service — plan / evaluate / pursue logic."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from goals import GoalStore, TERMINAL_STATUSES

log = logging.getLogger("k10d.goals")

CallTool = Callable[[str, dict], Any]

# Keyword hints only when kind is "open" or missing
_CODE_GOAL_HINTS = (
    "code", "hardware", "usb", "mqtt", "esp32", "micropython", "implement",
    "generate", "firmware", "driver", "script", "example",
)
_RESEARCH_GOAL_HINTS = (
    "research", "study", "learn", "investigate", "explore",
    "analyze", "analyse", "find out", "survey", "review",
)
_STATUS_GOAL_HINTS = (
    "sensor", "device status", "temperature", "humidity", "uptime",
    "how are you doing", "check in", "self-check",
)

# Plan templates by kind. open/general prefer outward paths (no forced deliver).
_KIND_TEMPLATES: dict[str, list[dict[str, str]]] = {
    "delivery": [
        {"action": "synthesise", "tool": "emerge.synthesise"},
        {"action": "hermes", "tool": "hermes.query"},
        {"action": "deliver", "tool": "act.deliver"},
    ],
    "research": [
        {"action": "search", "tool": "net.search"},
        {"action": "fetch", "tool": "net.search"},
        {"action": "synthesise", "tool": "emerge.synthesise"},
    ],
    "general": [
        {"action": "search", "tool": "net.search"},
        {"action": "hermes", "tool": "hermes.query"},
        {"action": "synthesise", "tool": "emerge.synthesise"},
    ],
    "open": [
        {"action": "search", "tool": "net.search"},
        {"action": "hermes", "tool": "hermes.query"},
        {"action": "synthesise", "tool": "emerge.synthesise"},
    ],
    # Zero-arg, read-only status/introspection tools only -- kept separate
    # from "open"/"general" so a status-check goal doesn't burn a
    # net.search/hermes.query round trip on something local and instant.
    "status_check": [
        {"action": "device_status", "tool": "host.get_device_status"},
        {"action": "sensor_poll", "tool": "sensor.poll"},
    ],
}

# Alternate plans per kind, indexed by revival attempt. A revived goal
# re-plans with a DIFFERENT strategy (attempt = revival_count) instead of
# retrying the identical plan that already failed. attempt 0 mirrors
# _KIND_TEMPLATES; later attempts reorder so a different tool leads.
_VARIED_TEMPLATES: dict[str, list[list[dict[str, str]]]] = {
    "delivery": [
        [{"action": "synthesise", "tool": "emerge.synthesise"},
         {"action": "hermes", "tool": "hermes.query"},
         {"action": "deliver", "tool": "act.deliver"}],
        [{"action": "search", "tool": "net.search"},
         {"action": "hermes", "tool": "hermes.query"},
         {"action": "deliver", "tool": "act.deliver"}],
    ],
    "research": [
        [{"action": "search", "tool": "net.search"},
         {"action": "fetch", "tool": "net.search"},
         {"action": "synthesise", "tool": "emerge.synthesise"}],
        [{"action": "hermes", "tool": "hermes.query"},
         {"action": "search", "tool": "net.search"},
         {"action": "synthesise", "tool": "emerge.synthesise"}],
    ],
    "general": [
        [{"action": "search", "tool": "net.search"},
         {"action": "hermes", "tool": "hermes.query"},
         {"action": "synthesise", "tool": "emerge.synthesise"}],
        [{"action": "hermes", "tool": "hermes.query"},
         {"action": "search", "tool": "net.search"},
         {"action": "synthesise", "tool": "emerge.synthesise"}],
    ],
    "open": [
        [{"action": "search", "tool": "net.search"},
         {"action": "hermes", "tool": "hermes.query"},
         {"action": "synthesise", "tool": "emerge.synthesise"}],
        [{"action": "hermes", "tool": "hermes.query"},
         {"action": "search", "tool": "net.search"},
         {"action": "synthesise", "tool": "emerge.synthesise"}],
    ],
    "status_check": [
        [{"action": "device_status", "tool": "host.get_device_status"},
         {"action": "sensor_poll", "tool": "sensor.poll"}],
        [{"action": "sensor_poll", "tool": "sensor.poll"},
         {"action": "device_status", "tool": "host.get_device_status"}],
    ],
}

MAX_FAILURES = 3
MIN_DELIVERABLE_BYTES = 80  # catches empty/failed writes, not a "realness" bar
MODERATE_COMPLETION_MIN_STEPS = 2  # distinct advancing steps -> auto-complete
# NOTE: 2, not 3 -- the "research" kind's "fetch" step dispatches through
# _swarm_search and is recorded under the "search" label (see _dispatch),
# so a standard 3-step research plan produces only 2 distinct progress
# labels even when every step succeeds. general/open (search/hermes/
# synthesise) produce 3 distinct labels, comfortably clearing this bar.


def _verify_deliver_result(result: Any) -> bool:
    """A deliver step is only 'ok' if act.deliver actually wrote a
    non-trivial file — free-text evidence alone can claim anything."""
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
        if not isinstance(parsed, dict) or not parsed.get("ok"):
            return False
        write = parsed.get("write") or {}
        path = write.get("absolute") or write.get("path")
        if not path:
            return False
        p = Path(path)
        return p.exists() and p.is_file() and p.stat().st_size >= MIN_DELIVERABLE_BYTES
    except Exception:
        return False


def _is_hermes_in_flight(action: str, result: Any) -> bool:
    if action not in ("hermes", "poll_hermes"):
        return False
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except Exception:
        return False
    if not isinstance(parsed, dict):
        return False
    if parsed.get("in_flight") is True:
        return True
    if parsed.get("status") == "running":
        return True
    return False


def _result_ok(result: Any, action: str | None = None) -> bool:
    if action == "deliver" or (isinstance(action, str) and action.startswith("tool:act.deliver")):
        return _verify_deliver_result(result)
    if not isinstance(result, str):
        return True
    try:
        parsed = json.loads(result)
        if isinstance(parsed, dict):
            # Bridge tools (hermes.*) report outcome under "success", not
            # "ok" — without this a failed hermes step reads as success,
            # suppressing the circuit breaker and mis-scoring completion.
            if "ok" in parsed:
                return bool(parsed.get("ok"))
            if "success" in parsed:
                return bool(parsed.get("success"))
            return True
    except Exception:
        pass
    return True


class GoalPursuitService:
    def __init__(self, goals: GoalStore, call_tool: CallTool):
        self.goals = goals
        self.call_tool = call_tool

    # ── step dispatcher (single table — no triplicated arg building) ──

    def _compose_deliverable(self, text: str, gid: str, step_results: dict, evidence: list) -> str:
        """Compose real deliverable content from full step results, not a
        single 200-char-truncated evidence echo."""
        synth = step_results.get("synthesise", {}).get("text", "")
        hermes = step_results.get("hermes", {}).get("text", "")
        parts = [f"# Goal {gid}\n\n{text}\n"]
        if synth:
            parts.append(f"\n## Synthesis\n{synth}")
        if hermes:
            parts.append(f"\n## Delegated output (hermes)\n{hermes}")
        if not synth and not hermes:
            trail = "\n".join(f"- {e.get('text', '')}" for e in evidence[-10:])
            parts.append(f"\n## Evidence trail\n{trail or '(no prior evidence)'}")
        return "\n".join(parts)

    def _get_latest_hermes_query_id(self, goal: dict) -> dict:
        step_results = goal.get("step_results") or {}
        hermes_result = step_results.get("hermes", {})
        hermes_text = hermes_result.get("text", "")
        try:
            parsed = json.loads(hermes_text) if isinstance(hermes_text, str) else hermes_text
            if isinstance(parsed, dict) and parsed.get("query_id"):
                return {"query_id": parsed["query_id"]}
        except Exception:
            pass
        for ev in reversed(goal.get("evidence", [])):
            ev_text = str(ev.get("text", ""))
            if "hermes" in ev_text and "query_id" in ev_text:
                try:
                    import re
                    match = re.search(r"query_id[=:]\s*([a-zA-Z0-9_]+)", ev_text)
                    if match:
                        return {"query_id": match.group(1)}
                except Exception:
                    pass
        return {"query_id": ""}

    def _build_step(self, action: str, goal: dict) -> tuple[str, dict]:
        """Map a pursuit action to (tool_name, args)."""
        text = goal["text"]
        gid = goal["id"]
        latest = ""
        evidence = goal.get("evidence") or []
        if evidence:
            latest = str(evidence[-1].get("text", ""))
        step_results = goal.get("step_results") or {}

        builders: dict[str, tuple[str, dict]] = {
            "search": (
                "net.search",
                {"query": text[:120], "count": 5},
            ),
            "synthesise": (
                "emerge.synthesise",
                {"topic": text},
            ),
            # SYNCHRONOUS hermes.query, not query_async. query_async returns
            # {"in_flight": true} immediately, which _is_hermes_in_flight
            # treats as "don't mark the step done" — but the plan templates
            # have no poll step, so the multi-act loop re-fires query_async
            # every act, spawning runaway `hermes -z` subprocesses on a goal
            # that never advances. The blocking query returns a terminal
            # result, so the step is marked executed and the plan progresses.
            "hermes": (
                "hermes.query",
                {
                    "prompt": (
                        f"Deliver concrete output for this goal:\n{text}\n\n"
                        "Provide working code examples, file paths, and setup steps."
                    ),
                    "timeout": 180,
                },
            ),
            "poll_hermes": (
                "hermes.poll",
                self._get_latest_hermes_query_id(goal),
            ),
            "deliver": (
                "act.deliver",
                {
                    "goal_id": gid,
                    "path": f"goal_{gid}.md",
                    "content": self._compose_deliverable(text, gid, step_results, evidence),
                    "summary": f"Delivered artifact for goal {gid}",
                },
            ),
            "plan": (
                "memory.log_episode",
                {
                    "summary": f"Goal planning: {text[:80]}",
                    "detail": json.dumps({
                        "goal_id": gid,
                        "action": "plan",
                        "focus": text,
                    }),
                },
            ),
            # Zero-arg, read-only status/introspection tools for the
            # "status_check" kind.
            "device_status": ("host.get_device_status", {}),
            "sensor_poll": ("sensor.poll", {}),
        }
        if action not in builders:
            raise ValueError(f"Unknown pursuit action: {action}")
        return builders[action]

    def _swarm_search(self, goal: dict) -> str:
        """Parallel multi-angle net.search fan-out. Used to be a second,
        byte-for-byte-identical net.search call (the old 'fetch' step) --
        now genuinely covers more of the topic per goal instead of wasting
        a round-trip repeating the exact same query."""
        text = goal["text"]
        base = text[:100]
        queries = [text[:120], f"{base} explained", f"{base} examples"]
        results: list[dict] = []
        errors: list[str] = []
        seen_urls: set[str] = set()

        with ThreadPoolExecutor(max_workers=len(queries)) as pool:
            futures = {
                pool.submit(self.call_tool, "net.search", {"query": q, "count": 5}): q
                for q in queries
            }
            for fut in as_completed(futures):
                q = futures[fut]
                try:
                    raw = fut.result()
                    parsed = json.loads(raw) if isinstance(raw, str) else raw
                except Exception as e:
                    errors.append(f"{q}: {e}")
                    continue
                if isinstance(parsed, dict) and parsed.get("ok"):
                    for r in parsed.get("results", []):
                        url = r.get("url")
                        if url and url in seen_urls:
                            continue
                        if url:
                            seen_urls.add(url)
                        results.append(r)
                else:
                    err = parsed.get("error") if isinstance(parsed, dict) else "bad response"
                    errors.append(f"{q}: {err}")

        return json.dumps(
            {"ok": bool(results), "queries": queries, "results": results, "errors": errors},
            indent=2,
        )

    # ── closing the loop: goals must reach a terminal state ────────────────

    def auto_evaluate_completion(self, goal: dict) -> tuple[bool, str]:
        """Can this goal be auto-completed right now? Used both when a plan
        exhausts and by goal.review's no-decision auto-check, so the two
        entry points agree on what "done" means.

        Precedence: explicit completion_criteria always wins if present.
        Delivery goals require a verified artifact (act.deliver actually
        wrote a real file) -- never moderate-complete a delivery goal on
        evidence breadth alone. Everything else uses the moderate rule:
        N distinct steps each produced progress evidence.
        """
        gid = goal["id"]
        if goal.get("completion_criteria") and self.goals.evaluate_completion(gid):
            return True, "completion_criteria met"

        if goal.get("kind") == "delivery":
            step_results = goal.get("step_results") or {}
            deliver_text = step_results.get("deliver", {}).get("text")
            if deliver_text and _verify_deliver_result(deliver_text):
                return True, "deliverable verified (act.deliver wrote a real file)"
            return False, "delivery goal has no verified deliverable yet"

        evidence = goal.get("evidence") or []
        progress_steps = {
            str(e.get("text", "")).split(":", 1)[0].strip()
            for e in evidence
            if e.get("signal") == "progress"
        }
        progress_steps.discard("")
        if len(progress_steps) >= MODERATE_COMPLETION_MIN_STEPS:
            return True, (
                f"{len(progress_steps)} distinct steps advanced the goal: "
                f"{sorted(progress_steps)}"
            )
        return False, (
            f"only {len(progress_steps)} distinct advancing step(s) so far: "
            f"{sorted(progress_steps)}"
        )

    def _close_exhausted_plan(self, goal: dict) -> tuple[str, Any]:
        """Plan fully executed: evaluate for completion instead of falling
        into an unbounded kind-based shuffle that never lets the goal reach
        a terminal state. Auto-completes when evidence supports it;
        otherwise parks in REVIEW for a human decision via goal.review."""
        gid = goal["id"]
        self.goals.mark_review(gid)
        goal = self.goals.get(gid)
        completed, reason = self.auto_evaluate_completion(goal)
        if completed:
            self.goals.mark_completed(gid, f"auto-completed: {reason}")
            return "auto_complete", json.dumps(
                {"ok": True, "auto_completed": True, "reason": reason}
            )
        return "plan_exhausted", json.dumps({
            "ok": True,
            "auto_completed": False,
            "reason": reason,
            "status": "review",
            "msg": "Plan exhausted; parked in review awaiting goal.review or stronger evidence.",
        })

    def _dispatch(self, action: str, goal: dict) -> tuple[str, Any]:
        """Run a named pursuit step; fetch now fans out a parallel multi-
        angle search instead of duplicating 'search'."""
        if action == "fetch":
            return "search", self._swarm_search(goal)
        tool_name, tool_args = self._build_step(action, goal)
        result = self.call_tool(tool_name, tool_args)
        return action, result

    def _run_plan_step(self, goal: dict, step: dict) -> tuple[str, Any]:
        action = step.get("action") or "synthesise"
        tool_name = step.get("tool") or ""
        # Prefer action dispatcher; fall back to raw tool if unknown action
        known = {
            "search", "fetch", "synthesise", "hermes", "poll_hermes", "deliver", "plan", "pursue",
            "device_status", "sensor_poll", "kairos_phase", "kairos_stats",
        }
        if action == "pursue":
            result = self.call_tool(
                "memory.log_episode",
                {
                    "summary": f"Goal ready for continued pursuit: {goal['text'][:80]}",
                    "detail": json.dumps({"goal_id": goal["id"], "action": "pursue"}),
                },
            )
            return "pursue", result
        if action in known:
            return self._dispatch(action, goal)
        # Unknown action: call tool with empty args if present
        if tool_name:
            return action, self.call_tool(tool_name, {})
        return self._dispatch("synthesise", goal)

    def _mark_plan_step_done(self, gid: str, plan: list[dict], step: dict) -> None:
        updated = []
        for item in plan:
            entry = dict(item)
            if entry.get("index") == step.get("index"):
                entry["executed"] = True
            updated.append(entry)
        self.goals.set_plan(gid, updated)

    def _log_episode(self, goal: dict, step: str, result: Any, **extra: Any) -> None:
        detail: dict[str, Any] = {
            "goal_id": goal["id"],
            "step": step,
            "result": str(result)[:500],
        }
        detail.update(extra)
        self.call_tool(
            "memory.log_episode",
            {
                "summary": f"Goal pursuit: {goal['text'][:80]}",
                "detail": json.dumps(detail),
            },
        )

    # ── kind / plan generation ──

    def _resolve_kind(self, goal: dict) -> str:
        kind = (goal.get("kind") or "open").strip().lower()
        if kind in _KIND_TEMPLATES and kind not in ("open",):
            return kind
        # open / missing: keyword hints as fallback only
        text = str(goal.get("text", "")).lower()
        if any(h in text for h in _CODE_GOAL_HINTS):
            return "delivery"
        if any(h in text for h in _RESEARCH_GOAL_HINTS):
            return "research"
        if any(h in text for h in _STATUS_GOAL_HINTS):
            return "status_check"
        return kind if kind in _KIND_TEMPLATES else "open"

    def _generate_plan_steps(self, goal: dict, max_steps: int) -> list[dict]:
        kind = self._resolve_kind(goal)
        # Pick the plan variant for this revival attempt so a revived goal
        # tries a different strategy than the one that already failed.
        variants = _VARIED_TEMPLATES.get(kind) or [
            _KIND_TEMPLATES.get(kind, _KIND_TEMPLATES["open"])
        ]
        attempt = int(goal.get("revival_count", 0) or 0)
        templates = variants[attempt % len(variants)]
        return [
            {
                "index": i,
                "action": tmpl["action"],
                "tool": tmpl["tool"],
                "executed": False,
            }
            for i, tmpl in enumerate(templates[:max_steps])
        ]

    def _resolve_goal(self, args: dict) -> dict | None:
        if args.get("id"):
            try:
                return self.goals.get(args["id"])
            except KeyError:
                return None
        return self.goals.top_open()

    def _prior_steps(self, goal: dict) -> list[str]:
        steps: list[str] = []
        for ev in goal.get("evidence", []):
            head = str(ev.get("text", "")).split(":", 1)[0].strip()
            if head:
                steps.append(head)
        return steps

    def _pick_auto_step(self, goal: dict, prior: list[str]) -> tuple[str, Any]:
        """Deterministic next step from kind/plan/evidence — no randomness."""
        gid = goal["id"]
        kind = self._resolve_kind(goal)

        if not self.goals.get_plan(gid):
            plan_raw = self.plan({"id": gid, "max_steps": 5})
            try:
                if json.loads(plan_raw).get("ok"):
                    goal = self.goals.get(gid)
            except json.JSONDecodeError:
                pass

        plan = self.goals.get_plan(gid)
        if plan:
            nxt = next((s for s in plan if not s.get("executed")), None)
            if nxt:
                action, result = self._run_plan_step(goal, nxt)
                self._mark_plan_step_done(gid, plan, nxt)
                return action, result
            # Plan is non-empty and every step executed: the goal has
            # finished its template march. Close the loop (evaluate for
            # completion) instead of falling into the kind-based fallback
            # below, which would otherwise re-dispatch search/hermes/
            # synthesise forever and never let the goal reach a terminal
            # state.
            return self._close_exhausted_plan(goal)

        # Plan generation produced nothing (pathological -- templates are
        # static and always non-empty in practice). Last-resort single
        # dispatch by kind so the goal isn't stuck with zero evidence.
        if kind == "research":
            if "search" not in prior:
                return self._dispatch("search", goal)
            return self._dispatch("synthesise", goal)

        if kind == "delivery":
            if "synthesise" not in prior:
                return self._dispatch("synthesise", goal)
            if "hermes" not in prior:
                return self._dispatch("hermes", goal)
            return self._dispatch("deliver", goal)

        # open / general: outward (search, hermes, synthesise) — no forced deliver
        if "search" not in prior:
            return self._dispatch("search", goal)
        if "hermes" not in prior:
            return self._dispatch("hermes", goal)
        if "synthesise" not in prior:
            return self._dispatch("synthesise", goal)
        return self._dispatch("search", goal)

    # ── public API ──

    def plan(self, args: dict) -> str:
        goal = self._resolve_goal(args)
        if not goal:
            return json.dumps({"ok": False, "msg": "No matching goal"})
        max_steps = max(1, min(int(args.get("max_steps", 5)), 10))
        steps = self._generate_plan_steps(goal, max_steps)
        updated = self.goals.set_plan(goal["id"], steps)
        return json.dumps(
            {"ok": True, "goal_id": goal["id"], "plan": steps, "goal": updated},
            indent=2,
        )

    def evaluate(self, args: dict) -> str:
        gid = args["id"]
        try:
            goal = self.goals.get(gid)
        except KeyError:
            return json.dumps({"ok": False, "error": f"Goal not found: {gid}"})
        plan = self.goals.get_plan(gid)
        step_result = str(args.get("step_result", "")).strip()
        evidence = goal.get("evidence", [])
        last_ev = str(evidence[-1].get("text", "")) if evidence else ""

        if plan:
            executed = sum(1 for s in plan if s.get("executed"))
            completion_pct = int(100 * executed / len(plan))
            next_item = next((s for s in plan if not s.get("executed")), None)
            next_step = next_item["action"] if next_item else None
            if next_item:
                gap = f"Need to execute: {next_item['action']} via {next_item['tool']}"
            else:
                gap = "Plan complete — consider goal.complete"
        else:
            completion_pct = min(len(evidence) * 20, 100)
            next_step = "plan"
            gap = "No plan — run goal.plan first"

        advanced = bool(step_result) or (
            bool(last_ev) and not last_ev.lower().startswith(("plan:", "error:"))
        )
        if step_result:
            self.goals.add_evidence(gid, f"evaluate: {step_result[:200]}")

        return json.dumps(
            {
                "advanced": advanced,
                "gap": gap,
                "next_step": next_step,
                "completion_pct": completion_pct,
            },
            indent=2,
        )

    def _record_step_exception(self, gid: str, action: str, exc: Exception) -> str:
        """A pursuit step *raised* (vs. returning a graceful ok:false payload).

        Route it through the same circuit breaker as a graceful failure so the
        exception can't propagate out of pursue() — which would skip
        record_failure() entirely and leave the goal wedged at status="active",
        re-picked and re-raising every tick until the age-based stale sweep.
        """
        self.goals.record_failure(
            gid, reason=f"{action}: raised {type(exc).__name__}: {exc}")
        # record_failure already auto-stalls at the threshold; only stall here
        # if it didn't (avoids a duplicate stall + duplicate dead_end evidence).
        if (self.goals.failure_count(gid) >= MAX_FAILURES
                and self.goals.get(gid).get("status") != "stalled"):
            self.goals.mark_stalled(
                gid, "Goal stalled after 3+ consecutive pursue failures")
        log.warning("Goal %s step %r raised: %s", gid, action, exc)
        return json.dumps({
            "ok": False,
            "goal": self.goals.get(gid),
            "step": action,
            "error": str(exc)[:800],
        }, indent=2)

    def pursue(self, args: dict) -> str:
        """Take one concrete step on the highest-priority open goal (or args id)."""
        goal = self._resolve_goal(args) if args.get("id") else self.goals.top_open()
        if not goal:
            return json.dumps({"ok": False, "msg": "No open goals"})

        gid = goal["id"]

        if goal.get("status") == "stalled":
            return json.dumps({
                "ok": False,
                "msg": f"Goal {gid} is stalled — re-plan or cancel before pursuing",
            })

        if goal.get("status") == "review":
            return json.dumps({
                "ok": False,
                "msg": (
                    f"Goal {gid} is awaiting review — use goal.review "
                    "(decision=complete|cancel|continue|supersede) to resolve it."
                ),
                "goal": goal,
            })

        if goal.get("status") in TERMINAL_STATUSES:
            return json.dumps({
                "ok": False,
                "msg": f"Goal {gid} is {goal['status']} (terminal) — cannot pursue.",
                "goal": goal,
            })

        # Circuit breaker via GoalStore failures field
        if self.goals.failure_count(gid) >= MAX_FAILURES:
            self.goals.mark_stalled(
                gid, "Goal stalled after 3+ consecutive pursue failures")
            return json.dumps({
                "ok": False,
                "msg": f"Goal {gid} stalled after 3+ consecutive failures",
            })

        # Canonical ACTIVE transition. Do NOT write the legacy "in_progress"
        # alias here: GoalStore.update() stores it raw, and top_open() /
        # Will._live_goals() query the canonical "active" — a raw "in_progress"
        # matches neither, orphaning the goal (invisible to pursuit AND to the
        # failure→stall breaker, which is gated on status == "active").
        self.goals.mark_active(gid)
        goal = self.goals.get(gid)

        step = str(args.get("step", "auto") or "auto").strip().lower()
        prior = self._prior_steps(goal)
        plan = self.goals.get_plan(gid)
        plan_step_meta: dict | None = None

        # Escape hatch: arbitrary tool call
        if step == "tool":
            tool_name = args.get("tool") or args.get("name")
            tool_args = args.get("args") or {}
            if not tool_name:
                return json.dumps({
                    "ok": False,
                    "msg": "step=tool requires tool name and optional args dict",
                })
            if not isinstance(tool_args, dict):
                return json.dumps({"ok": False, "msg": "args must be a dict"})
            action = f"tool:{tool_name}"
            try:
                result = self.call_tool(str(tool_name), tool_args)
            except Exception as e:
                return self._record_step_exception(gid, action, e)
            step_ok = _result_ok(result, action)
            self.goals.add_evidence(
                gid, f"{action}: {str(result)[:200]}",
                signal="progress" if step_ok else "dead_end")
            self.goals.set_step_result(gid, action, result)
            if step_ok:
                self.goals.clear_failures(gid)
            else:
                self.goals.record_failure(gid)
                if self.goals.failure_count(gid) >= MAX_FAILURES:
                    self.goals.mark_stalled(
                        gid, "Goal stalled after 3+ consecutive pursue failures")
            self._log_episode(goal, action, result)
            return json.dumps(
                {
                    "ok": step_ok,
                    "goal": self.goals.get(gid),
                    "step": action,
                    "result": str(result)[:800],
                },
                indent=2,
            )

        # Explicit plan generation step
        if step == "plan":
            return self.plan({"id": gid, "max_steps": int(args.get("max_steps", 5))})

        # Auto with existing plan: next unexecuted step
        if step == "auto" and plan:
            next_step = next((s for s in plan if not s.get("executed")), None)
            if next_step:
                try:
                    action, result = self._run_plan_step(goal, next_step)
                except Exception as e:
                    return self._record_step_exception(
                        gid, next_step.get("action") or "auto", e)
                in_flight = _is_hermes_in_flight(action, result)
                if not in_flight:
                    self._mark_plan_step_done(gid, plan, next_step)
                plan_step_meta = next_step
                step_ok = in_flight or _result_ok(result, action)
                evidence_text = (
                    f"{action}: in_flight, poll via hermes.poll" if in_flight
                    else f"{action}: {str(result)[:200]}"
                )
                # Tag failed steps 'dead_end' so auto_evaluate_completion (which
                # counts distinct 'progress'-signal step labels) can't complete
                # a goal on steps that produced nothing.
                self.goals.add_evidence(
                    gid, evidence_text,
                    signal="progress" if step_ok else "dead_end")
                self.goals.set_step_result(gid, action, result)
                if in_flight:
                    pass  # neither success nor failure -- don't touch the circuit breaker
                elif step_ok:
                    self.goals.clear_failures(gid)
                else:
                    self.goals.record_failure(gid)
                    if self.goals.failure_count(gid) >= MAX_FAILURES:
                        self.goals.mark_stalled(
                            gid, "Goal stalled after 3+ consecutive pursue failures")
                self._log_episode(
                    goal, action, result, plan_index=next_step.get("index"))
                # Outer ok reflects the step outcome so the Will's multi-act
                # break-on-failure guard actually fires for a soft-failed step.
                return json.dumps(
                    {
                        "ok": step_ok,
                        "goal": self.goals.get(gid),
                        "step": action,
                        "plan_step": plan_step_meta,
                        "result": str(result)[:800],
                    },
                    indent=2,
                )

        known_steps = {"search", "fetch", "synthesise", "hermes", "poll_hermes", "deliver"}
        try:
            if step in known_steps:
                action, result = self._dispatch(step, goal)
            elif step == "auto":
                action, result = self._pick_auto_step(goal, prior)
            else:
                # Unknown step → synthesise (legacy default)
                action, result = self._dispatch("synthesise", goal)
        except Exception as e:
            return self._record_step_exception(gid, step, e)

        in_flight = _is_hermes_in_flight(action, result)
        step_ok = in_flight or _result_ok(result, action)
        evidence_text = (
            f"{action}: in_flight, poll via hermes.poll" if in_flight
            else f"{action}: {str(result)[:200]}"
        )
        self.goals.add_evidence(
            gid, evidence_text,
            signal="progress" if step_ok else "dead_end")
        self.goals.set_step_result(gid, action, result)

        if in_flight:
            pass  # neither success nor failure -- don't touch the circuit breaker
        elif step_ok:
            self.goals.clear_failures(gid)
        else:
            self.goals.record_failure(gid)
            if self.goals.failure_count(gid) >= MAX_FAILURES:
                self.goals.mark_stalled(
                    gid, "Goal stalled after 3+ consecutive pursue failures")

        self._log_episode(goal, action, result)
        return json.dumps(
            {
                "ok": step_ok,
                "goal": self.goals.get(gid),
                "step": action,
                "result": str(result)[:800],
            },
            indent=2,
        )
