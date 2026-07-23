"""
Goal Lifecycle State Machine
============================

States:
  SPAWNED   → created, not yet acted upon
  ACTIVE    → being pursued step by step
  REVIEW    → plan exhausted; awaiting completion check
  COMPLETED → successfully resolved (terminal)
  CANCELLED → explicitly abandoned (terminal)
  SUPERSEDED→ replaced by a newer goal (terminal)
  STALLED   → blocked after 3 consecutive failures
  REVIVAL   → one retry with a new plan

Transitions:
  SPAWNED  → ACTIVE     (auto, on first pursue)
  ACTIVE   → REVIEW     (plan exhausted or all steps OK)
  ACTIVE   → STALLED    (3 consecutive failures)
  REVIEW   → COMPLETED  (criteria met or user confirms)
  REVIEW   → CANCELLED  (no longer relevant)
  REVIEW   → SUPERSEDED (replaced by newer goal)
  REVIEW   → ACTIVE     (needs more work)
  STALLED  → REVIVAL    (cooldown elapsed)
  REVIVAL  → ACTIVE     (new plan makes progress)
  REVIVAL  → CANCELLED  (failed again)
  any      → CANCELLED  (explicit cancel)
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("delta.goals")

GOALS_PATH = Path("./goals.json")
MAX_GOALS = 100
MAX_CONSECUTIVE_FAILURES = 3
MAX_REVIVALS = 1
ARCHIVE_TTL_DAYS = 7

TERMINAL_STATUSES = frozenset({"completed", "cancelled", "superseded", "done"})

# Legacy alias mapping for backward compat
_LEGACY_STATUS_MAP = {"open": "spawned", "in_progress": "active", "done": "completed"}

# Default plan steps per goal kind
_DEFAULT_PLANS: dict[str, list[str]] = {
    "research":  ["search", "hermes", "synthesise", "review"],
    "delivery":  ["tool", "deliver", "review"],
    "general":   ["search", "hermes", "review"],
    "open":      ["hermes", "review"],
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_days(iso_ts: str) -> float:
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts).total_seconds() / 86400
    except Exception:
        return 0.0


class GoalStore:
    def __init__(self, path: Path = GOALS_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._store: dict[str, dict] = {}
        self._load()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                raw = data if isinstance(data, dict) else {}
                # Migrate legacy status values
                for g in raw.values():
                    g["status"] = _LEGACY_STATUS_MAP.get(g.get("status", ""), g.get("status", "spawned"))
                self._store = raw
            except Exception as e:
                log.error("Failed to load goals: %s", e)
                self._store = {}

    def _save(self) -> None:
        self.path.write_text(json.dumps(self._store, indent=2), encoding="utf-8")

    # ── Factory ───────────────────────────────────────────────────────────────

    def add(
        self,
        text: str,
        source: str = "agent",
        priority: int = 5,
        kind: str = "open",
        completion_criteria: dict | None = None,
        status: str = "spawned",
    ) -> dict:
        """Create a goal. Returns existing goal if text is an exact match."""
        text = text.strip()
        if not text:
            raise ValueError("Goal text required")
        kind = (kind or "open").strip().lower()
        if kind not in _DEFAULT_PLANS:
            kind = "open"
        with self._lock:
            for g in self._store.values():
                if g["text"].lower() == text.lower():
                    return dict(g)
            if len(self._store) >= MAX_GOALS:
                self._prune_terminal()
            gid = str(uuid.uuid4())[:8]
            goal: dict = {
                "id": gid,
                "text": text,
                "source": source,
                "status": status,
                "kind": kind,
                "priority": max(1, min(int(priority), 10)),
                "created": _now_iso(),
                "updated": _now_iso(),
                "evidence": [],
                "failures": 0,
                "consecutive_failures": 0,
                "revival_count": 0,
                "plan": [],
            }
            if completion_criteria:
                goal["completion_criteria"] = completion_criteria
            self._store[gid] = goal
            self._save()
            return dict(goal)


    # ── State transitions ─────────────────────────────────────────────────────

    def mark_active(self, gid: str) -> dict:
        """SPAWNED or REVIVAL → ACTIVE."""
        with self._lock:
            g = self._require(gid)
            g["status"] = "active"
            g["updated"] = _now_iso()
            self._save()
            return dict(g)

    def mark_review(self, gid: str) -> dict:
        """ACTIVE → REVIEW. Triggered when plan is exhausted."""
        with self._lock:
            g = self._require(gid)
            g["status"] = "review"
            g["updated"] = _now_iso()
            self._save()
            return dict(g)

    def mark_completed(self, gid: str, evidence: str = "") -> dict:
        """REVIEW → COMPLETED."""
        with self._lock:
            g = self._require(gid)
            g["status"] = "completed"
            g["updated"] = _now_iso()
            if evidence:
                self._append_evidence(g, evidence, signal="progress")
            self._save()
            return dict(g)

    def mark_cancelled(self, gid: str, reason: str = "") -> dict:
        """Any non-terminal → CANCELLED."""
        with self._lock:
            g = self._require(gid)
            if g["status"] in TERMINAL_STATUSES:
                return dict(g)
            g["status"] = "cancelled"
            g["updated"] = _now_iso()
            if reason:
                self._append_evidence(g, reason, signal="dead_end")
            self._save()
            return dict(g)

    def mark_superseded(self, gid: str, by_id: str) -> dict:
        """REVIEW → SUPERSEDED by another goal."""
        with self._lock:
            g = self._require(gid)
            g["status"] = "superseded"
            g["superseded_by"] = by_id
            g["updated"] = _now_iso()
            self._save()
            return dict(g)

    def mark_stalled(self, gid: str, reason: str = "") -> dict:
        """ACTIVE → STALLED."""
        with self._lock:
            g = self._require(gid)
            g["status"] = "stalled"
            g["stalled_at"] = _now_iso()
            g["updated"] = _now_iso()
            if reason:
                self._append_evidence(g, reason, signal="dead_end")
            self._save()
            return dict(g)

    def mark_revival(self, gid: str) -> dict:
        """STALLED → REVIVAL. Generates a fresh plan."""
        with self._lock:
            g = self._require(gid)
            g["status"] = "revival"
            g["revival_count"] = int(g.get("revival_count", 0)) + 1
            g["consecutive_failures"] = 0
            g["plan"] = self._default_plan(g.get("kind", "open"))
            g["updated"] = _now_iso()
            self._save()
            return dict(g)

    # ── Failure tracking ──────────────────────────────────────────────────────

    def record_failure(self, gid: str, reason: str = "") -> dict:
        """Increment consecutive_failures. Auto-stalls at MAX_CONSECUTIVE_FAILURES."""
        with self._lock:
            g = self._require(gid)
            g["failures"] = int(g.get("failures", 0)) + 1
            cf = int(g.get("consecutive_failures", 0)) + 1
            g["consecutive_failures"] = cf
            g["updated"] = _now_iso()
            if reason:
                self._append_evidence(g, reason, signal="dead_end")
            if cf >= MAX_CONSECUTIVE_FAILURES and g["status"] == "active":
                g["status"] = "stalled"
                g["stalled_at"] = _now_iso()
                log.info("Goal %s auto-stalled after %d consecutive failures", gid, cf)
            self._save()
            return dict(g)

    def clear_failures(self, gid: str) -> dict:
        with self._lock:
            g = self._require(gid)
            g["failures"] = 0
            g["consecutive_failures"] = 0
            g["updated"] = _now_iso()
            self._save()
            return dict(g)

    def failure_count(self, gid: str) -> int:
        with self._lock:
            return int(self._store.get(gid, {}).get("failures", 0))


    # ── Completion evaluation ─────────────────────────────────────────────────

    def evaluate_completion(self, gid: str) -> bool:
        """
        Check completion_criteria. Returns True if the goal is done.
        Defaults to False (human review required) if no criteria set.
        """
        with self._lock:
            g = self._store.get(gid, {})
        criteria = g.get("completion_criteria")
        if not criteria:
            return False  # default: requires human sign-off via goal.review

        ctype = criteria.get("type", "")

        if ctype == "evidence_count":
            ev = [e for e in g.get("evidence", []) if e.get("signal") != "dead_end"]
            min_ev = int(criteria.get("min_evidence", 3))
            if criteria.get("unique_sources"):
                sources = {e.get("source", "") for e in ev if e.get("source")}
                return len(sources) >= min_ev
            return len(ev) >= min_ev

        if ctype == "tool_output_match":
            import re
            pattern = criteria.get("pattern", "")
            for e in reversed(g.get("evidence", [])):
                if re.search(pattern, e.get("text", ""), re.IGNORECASE):
                    return True
            return False

        # "user_confirmation" — always requires manual goal.review call
        return False

    # ── Plan management ───────────────────────────────────────────────────────

    def generate_plan(self, gid: str) -> list[dict]:
        """Assign the default plan for the goal's kind and return it."""
        with self._lock:
            g = self._require(gid)
            steps = self._default_plan(g.get("kind", "open"))
            g["plan"] = steps
            g["updated"] = _now_iso()
            self._save()
            return steps

    def set_plan(self, gid: str, steps: list[dict]) -> dict:
        with self._lock:
            g = self._require(gid)
            g["plan"] = steps
            g["updated"] = _now_iso()
            self._save()
            return dict(g)

    def get_plan(self, gid: str) -> list[dict]:
        with self._lock:
            return list(self._require(gid).get("plan", []))

    @staticmethod
    def _default_plan(kind: str) -> list[dict]:
        steps = _DEFAULT_PLANS.get(kind, _DEFAULT_PLANS["open"])
        return [{"action": s, "done": False} for s in steps]

    # ── Evidence ──────────────────────────────────────────────────────────────

    def add_evidence(self, gid: str, text: str,
                     signal: str = "progress", source: str = "") -> dict:
        """signal: 'progress' | 'dead_end' | 'repeat'"""
        with self._lock:
            g = self._require(gid)
            self._append_evidence(g, text, signal=signal, source=source)
            g["updated"] = _now_iso()
            self._save()
            return dict(g)

    @staticmethod
    def _append_evidence(g: dict, text: str,
                         signal: str = "progress", source: str = "") -> None:
        entry: dict = {"text": text, "ts": _now_iso(), "signal": signal}
        if source:
            entry["source"] = source
        g.setdefault("evidence", []).append(entry)
        g["evidence"] = g["evidence"][-20:]

    # ── Queries ───────────────────────────────────────────────────────────────

    def list_all(self, status: str | None = None) -> list[dict]:
        with self._lock:
            items = list(self._store.values())
        if status:
            # Resolve legacy aliases in filter too
            status = _LEGACY_STATUS_MAP.get(status, status)
            items = [g for g in items if g.get("status") == status]
        return sorted(items, key=lambda g: (-g.get("priority", 0), g.get("created", "")))

    def get(self, gid: str) -> dict:
        with self._lock:
            return dict(self._require(gid))

    def exists(self, gid: str) -> bool:
        with self._lock:
            return gid in self._store

    def update(self, gid: str, **fields) -> dict:
        with self._lock:
            g = self._require(gid)
            for key in ("text", "status", "priority", "source", "kind", "completion_criteria"):
                if key in fields:
                    g[key] = fields[key]
            g["updated"] = _now_iso()
            self._save()
            return dict(g)

    def top_open(self) -> dict | None:
        """Best goal to pursue next, respecting state machine priority."""
        for status in ("active", "spawned", "revival", "review", "stalled"):
            candidates = self.list_all(status)
            if candidates:
                return candidates[0]
        return None


    # ── Auto-archive & revival sweep ──────────────────────────────────────────

    def auto_archive(self, max_age_days: float = ARCHIVE_TTL_DAYS) -> list[str]:
        """Delete terminal goals older than max_age_days. Returns deleted IDs."""
        deleted = []
        with self._lock:
            for gid, g in list(self._store.items()):
                if g.get("status") in TERMINAL_STATUSES:
                    if _age_days(g.get("updated", g.get("created", ""))) > max_age_days:
                        del self._store[gid]
                        deleted.append(gid)
            if deleted:
                self._save()
        return deleted

    def sweep_revivals(self, cooldown_hours: float = 1.0) -> list[str]:
        """Promote stalled goals to REVIVAL after cooldown. Returns promoted IDs."""
        import time
        promoted = []
        with self._lock:
            for gid, g in self._store.items():
                if g.get("status") != "stalled":
                    continue
                if int(g.get("revival_count", 0)) >= MAX_REVIVALS:
                    g["status"] = "cancelled"
                    g["updated"] = _now_iso()
                    promoted.append(gid)
                    continue
                stalled_at = g.get("stalled_at", g.get("updated", ""))
                age_h = _age_days(stalled_at) * 24
                if age_h >= cooldown_hours:
                    g["status"] = "revival"
                    g["revival_count"] = int(g.get("revival_count", 0)) + 1
                    g["consecutive_failures"] = 0
                    g["plan"] = self._default_plan(g.get("kind", "open"))
                    g["updated"] = _now_iso()
                    promoted.append(gid)
            if promoted:
                self._save()
        return promoted

    # ── Step results ──────────────────────────────────────────────────────────

    def set_step_result(self, gid: str, action: str, result) -> dict:
        with self._lock:
            g = self._require(gid)
            text = result if isinstance(result, str) else json.dumps(result, default=str)
            if len(text) > 8000:
                text = text[:8000] + "...[truncated]"
            g.setdefault("step_results", {})[action] = {"text": text, "ts": _now_iso()}
            g["updated"] = _now_iso()
            self._save()
            return dict(g)

    # ── Compat shims ──────────────────────────────────────────────────────────

    def complete(self, gid: str, evidence: str = "") -> dict:
        """Compat: prefer mark_completed(). Sets status → completed."""
        return self.mark_completed(gid, evidence)

    def stalled_count(self, gid: str) -> int:
        """Compat: returns consecutive_failures."""
        with self._lock:
            return int(self._store.get(gid, {}).get("consecutive_failures", 0))

    def record_failure_compat(self, gid: str) -> dict:
        """Compat alias."""
        return self.record_failure(gid)

    def list_stale(self, max_age_s: float = 7200) -> list[dict]:
        import time
        now = time.time()
        stale = []
        with self._lock:
            for g in self._store.values():
                if g.get("status") in TERMINAL_STATUSES:
                    continue
                updated = g.get("updated", "")
                try:
                    ts = datetime.fromisoformat(
                        str(updated).replace("Z", "+00:00")).timestamp()
                    if now - ts > max_age_s:
                        stale.append(dict(g))
                except Exception:
                    continue
        return stale

    def delete(self, gid: str) -> dict:
        with self._lock:
            deleted = dict(self._require(gid))
            del self._store[gid]
            self._save()
            return deleted

    # ── Internals ─────────────────────────────────────────────────────────────

    def _require(self, gid: str) -> dict:
        if gid not in self._store:
            raise KeyError(gid)
        return self._store[gid]

    def _prune_terminal(self) -> None:
        terminal = sorted(
            [g for g in self._store.values() if g.get("status") in TERMINAL_STATUSES],
            key=lambda g: g.get("updated", ""),
        )
        for g in terminal[:max(1, len(self._store) - MAX_GOALS + 5)]:
            del self._store[g["id"]]
        self._save()
