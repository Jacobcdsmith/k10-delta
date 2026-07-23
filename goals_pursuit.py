"""Goal pursuit service — plan / evaluate / pursue logic."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from goals import GoalStore

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
}

MAX_FAILURES = 3
MIN_DELIVERABLE_BYTES = 80  # catches empty/failed writes, not a "realness" bar


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
    """True if a hermes step returned early because the query is still
    running (see hermes_bridge.HermesBridge.query's poll_timeout) -- this
    is neither success nor failure, just "still working"."""
    if action != "hermes":
        return False
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except Exception:
        return False
    return isinstance(parsed, dict) and parsed.get("in_flight") is True


def _result_ok(result: Any, action: str | None = None) -> bool:
    if action == "deliver" or (isinstance(action, str) and action.startswith("tool:act.deliver")):
        return _verify_deliver_result(result)
    if not isinstance(result, str):
        return True
    try:
        parsed = json.loads(result)
        if isinstance(parsed, dict):
            return bool(parsed.get("ok", True))
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
            "search", "fetch", "synthesise", "hermes", "deliver", "plan", "pursue",
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
        return kind if kind in _KIND_TEMPLATES else "open"

    def _generate_plan_steps(self, goal: dict, max_steps: int) -> list[dict]:
        kind = self._resolve_kind(goal)
        templates = _KIND_TEMPLATES.get(kind, _KIND_TEMPLATES["open"])
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

        # Fallbacks when plan is exhausted or empty
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

        # Circuit breaker via GoalStore failures field
        if self.goals.failure_count(gid) >= MAX_FAILURES:
            self.goals.mark_stalled(
                gid, "Goal stalled after 3+ consecutive pursue failures")
            return json.dumps({
                "ok": False,
                "msg": f"Goal {gid} stalled after 3+ consecutive failures",
            })

        self.goals.update(gid, status="in_progress")
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
            result = self.call_tool(str(tool_name), tool_args)
            action = f"tool:{tool_name}"
            self.goals.add_evidence(gid, f"{action}: {str(result)[:200]}")
            self.goals.set_step_result(gid, action, result)
            if _result_ok(result, action):
                self.goals.clear_failures(gid)
            else:
                self.goals.record_failure(gid)
                if self.goals.failure_count(gid) >= MAX_FAILURES:
                    self.goals.mark_stalled(
                        gid, "Goal stalled after 3+ consecutive pursue failures")
            self._log_episode(goal, action, result)
            return json.dumps(
                {
                    "ok": True,
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
                action, result = self._run_plan_step(goal, next_step)
                in_flight = _is_hermes_in_flight(action, result)
                if not in_flight:
                    self._mark_plan_step_done(gid, plan, next_step)
                plan_step_meta = next_step
                evidence_text = (
                    f"{action}: in_flight, poll via hermes.poll" if in_flight
                    else f"{action}: {str(result)[:200]}"
                )
                self.goals.add_evidence(gid, evidence_text)
                self.goals.set_step_result(gid, action, result)
                if in_flight:
                    pass  # neither success nor failure -- don't touch the circuit breaker
                elif _result_ok(result, action):
                    self.goals.clear_failures(gid)
                else:
                    self.goals.record_failure(gid)
                    if self.goals.failure_count(gid) >= MAX_FAILURES:
                        self.goals.mark_stalled(
                            gid, "Goal stalled after 3+ consecutive pursue failures")
                self._log_episode(
                    goal, action, result, plan_index=next_step.get("index"))
                return json.dumps(
                    {
                        "ok": True,
                        "goal": self.goals.get(gid),
                        "step": action,
                        "plan_step": plan_step_meta,
                        "result": str(result)[:800],
                    },
                    indent=2,
                )

        known_steps = {"search", "fetch", "synthesise", "hermes", "deliver"}
        if step in known_steps:
            action, result = self._dispatch(step, goal)
        elif step == "auto":
            action, result = self._pick_auto_step(goal, prior)
        else:
            # Unknown step → synthesise (legacy default)
            action, result = self._dispatch("synthesise", goal)

        in_flight = _is_hermes_in_flight(action, result)
        evidence_text = (
            f"{action}: in_flight, poll via hermes.poll" if in_flight
            else f"{action}: {str(result)[:200]}"
        )
        self.goals.add_evidence(gid, evidence_text)
        self.goals.set_step_result(gid, action, result)

        if in_flight:
            pass  # neither success nor failure -- don't touch the circuit breaker
        elif _result_ok(result, action):
            self.goals.clear_failures(gid)
        else:
            self.goals.record_failure(gid)
            if self.goals.failure_count(gid) >= MAX_FAILURES:
                self.goals.mark_stalled(
                    gid, "Goal stalled after 3+ consecutive pursue failures")

        self._log_episode(goal, action, result)
        return json.dumps(
            {
                "ok": True,
                "goal": self.goals.get(gid),
                "step": action,
                "result": str(result)[:800],
            },
            indent=2,
        )
