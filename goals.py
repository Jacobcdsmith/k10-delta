"""Goal stack — pursuits that turn introspection into action."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("k10d.goals")

GOALS_PATH = Path("./goals.json")
MAX_GOALS = 100


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class GoalStore:
    def __init__(self, path: Path = GOALS_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._store: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._store = data if isinstance(data, dict) else {}
            except Exception as e:
                log.error("Failed to load goals from %s: %s", self.path, e)
                self._store = {}

    def _save(self) -> None:
        self.path.write_text(
            json.dumps(self._store, indent=2), encoding="utf-8")

    def add(self, text: str, source: str = "agent",
            priority: int = 5, kind: str = "open") -> dict:
        text = text.strip()
        if not text:
            raise ValueError("Goal text required")
        kind = (kind or "open").strip().lower()
        if kind not in ("open", "general", "research", "delivery"):
            kind = "open"
        with self._lock:
            for g in self._store.values():
                if g.get("status") in ("open", "in_progress") and g["text"].lower() == text.lower():
                    return dict(g)
            if len(self._store) >= MAX_GOALS:
                self._prune_done()
            gid = str(uuid.uuid4())[:8]
            goal = {
                "id": gid,
                "text": text,
                "source": source,
                "status": "open",
                "kind": kind,
                "priority": max(1, min(int(priority), 10)),
                "created": _now_iso(),
                "updated": _now_iso(),
                "evidence": [],
                "failures": 0,
            }
            self._store[gid] = goal
            self._save()
            return dict(goal)

    def list_all(self, status: str | None = None) -> list[dict]:
        with self._lock:
            items = list(self._store.values())
        if status:
            items = [g for g in items if g.get("status") == status]
        return sorted(
            items,
            key=lambda g: (-g.get("priority", 0), g.get("created", "")),
        )

    def get(self, gid: str) -> dict:
        with self._lock:
            if gid not in self._store:
                raise KeyError(gid)
            return dict(self._store[gid])

    def exists(self, gid: str) -> bool:
        with self._lock:
            return gid in self._store

    def update(self, gid: str, **fields) -> dict:
        with self._lock:
            if gid not in self._store:
                raise KeyError(gid)
            g = self._store[gid]
            for key in ("text", "status", "priority", "source", "kind"):
                if key in fields:
                    g[key] = fields[key]
            g["updated"] = _now_iso()
            self._save()
            return dict(g)

    def set_plan(self, gid: str, steps: list[dict]) -> dict:
        with self._lock:
            if gid not in self._store:
                raise KeyError(gid)
            g = self._store[gid]
            g["plan"] = steps
            g["updated"] = _now_iso()
            self._save()
            return dict(g)

    def get_plan(self, gid: str) -> list[dict]:
        with self._lock:
            if gid not in self._store:
                raise KeyError(gid)
            return list(self._store[gid].get("plan", []))

    def add_evidence(self, gid: str, text: str) -> dict:
        with self._lock:
            if gid not in self._store:
                raise KeyError(gid)
            g = self._store[gid]
            g.setdefault("evidence", []).append({"text": text, "ts": _now_iso()})
            g["evidence"] = g["evidence"][-20:]
            g["updated"] = _now_iso()
            self._save()
            return dict(g)

    def complete(self, gid: str, evidence: str = "") -> dict:
        with self._lock:
            if gid not in self._store:
                raise KeyError(gid)
            g = self._store[gid]
            g["status"] = "done"
            g["updated"] = _now_iso()
            if evidence:
                g.setdefault("evidence", []).append(
                    {"text": evidence, "ts": _now_iso()})
            self._save()
            return dict(g)

    def mark_stalled(self, gid: str, reason: str = "") -> dict:
        with self._lock:
            if gid not in self._store:
                raise KeyError(gid)
            g = self._store[gid]
            g["status"] = "stalled"
            g["updated"] = _now_iso()
            if reason:
                g.setdefault("evidence", []).append({"text": reason, "ts": _now_iso()})
            self._save()
            return dict(g)

    def record_failure(self, gid: str) -> dict:
        with self._lock:
            if gid not in self._store:
                raise KeyError(gid)
            g = self._store[gid]
            g["failures"] = int(g.get("failures", 0)) + 1
            g["updated"] = _now_iso()
            self._save()
            return dict(g)

    def clear_failures(self, gid: str) -> dict:
        with self._lock:
            if gid not in self._store:
                raise KeyError(gid)
            g = self._store[gid]
            g["failures"] = 0
            g["updated"] = _now_iso()
            self._save()
            return dict(g)

    def failure_count(self, gid: str) -> int:
        with self._lock:
            if gid not in self._store:
                return 0
            return int(self._store[gid].get("failures", 0))

    def stalled_count(self, gid: str) -> int:
        """Compat: prefer failure_count / failures field going forward."""
        with self._lock:
            if gid not in self._store:
                return 0
            g = self._store[gid]
            if "failures" in g:
                return int(g.get("failures", 0))
            ev = g.get("evidence", [])
            count = 0
            for e in reversed(ev):
                text = e.get("text", "")
                if "failed" in text.lower() or "error" in text.lower() or "stalled" in text.lower():
                    count += 1
                else:
                    break
            return count

    def list_stale(self, max_age_s: float = 7200) -> list[dict]:
        import time
        now = time.time()
        stale = []
        with self._lock:
            for g in self._store.values():
                if g.get("status") not in ("open", "in_progress"):
                    continue
                updated = g.get("updated", "")
                try:
                    ts = datetime.fromisoformat(str(updated).replace("Z", "+00:00")).timestamp()
                    if now - ts > max_age_s:
                        stale.append(dict(g))
                except Exception:
                    continue
        return stale

    def top_open(self) -> dict | None:
        in_prog = self.list_all("in_progress")
        if in_prog:
            return in_prog[0]
        open_goals = self.list_all("open")
        if open_goals:
            return open_goals[0]
        stalled = self.list_all("stalled")
        return stalled[0] if stalled else None

    def _prune_done(self) -> None:
        done = sorted(
            [g for g in self._store.values() if g.get("status") == "done"],
            key=lambda g: g.get("updated", ""),
        )
        for g in done[:max(1, len(self._store) - MAX_GOALS + 5)]:
            del self._store[g["id"]]
        self._save()