"""Hypothesis store."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from .constants import MAX_HYPOTHESES
from .text import _evidence_weight, _now_iso

class HypothesisStore:
    def __init__(self, path: Path):
        self.path   = path
        self._lock  = threading.Lock()
        self._store: dict[str, dict] = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self._store = json.loads(
                    self.path.read_text(encoding="utf-8"))
            except Exception:
                self._store = {}

    def _save(self):
        self.path.write_text(
            json.dumps(self._store, indent=2), encoding="utf-8")

    def _recompute_confidence(self, h: dict) -> None:
        w_for = sum(_evidence_weight(e["text"]) for e in h["evidence_for"])
        w_against = sum(_evidence_weight(e["text"]) for e in h["evidence_against"])
        total = w_for + w_against
        h["confidence"] = w_for / total if total else 0.5
        n_weighted = w_for + w_against
        if h["confidence"] >= 0.85 and n_weighted >= 2.5:
            h["status"] = "confirmed"
        elif h["confidence"] <= 0.15 and n_weighted >= 2.5:
            h["status"] = "falsified"

    @staticmethod
    def _norm_key(text: str) -> str:
        return " ".join(text.lower().split())[:160]

    def dedupe_open(self) -> int:
        """Remove duplicate open hypotheses (common after dream bursts)."""
        seen: dict[str, str] = {}
        removed = 0
        with self._lock:
            for hid, h in list(self._store.items()):
                if h.get("status") != "open":
                    continue
                key = self._norm_key(h.get("text", ""))
                if not key:
                    continue
                if key in seen:
                    del self._store[hid]
                    removed += 1
                else:
                    seen[key] = hid
            if removed:
                self._save()
        return removed

    def spawn(self, text: str, source: str = "llm") -> dict:
        import uuid
        key = self._norm_key(text)
        with self._lock:
            if key:
                for h in self._store.values():
                    if h.get("status") == "open" and self._norm_key(h["text"]) == key:
                        return dict(h)
            if len(self._store) >= MAX_HYPOTHESES:
                self._prune()
            hid = str(uuid.uuid4())[:8]
            h = {"id": hid, "text": text, "source": source,
                 "status": "open", "evidence_for": [], "evidence_against": [],
                 "created": _now_iso(), "updated": _now_iso(), "confidence": 0.5}
            self._store[hid] = h
            self._save()
            return dict(h)

    def add_evidence(self, hid: str, evidence: str, supports: bool) -> dict:
        with self._lock:
            if hid not in self._store:
                raise KeyError(hid)
            h = self._store[hid]
            h["evidence_for" if supports else "evidence_against"].append(
                {"text": evidence, "ts": _now_iso()})
            self._recompute_confidence(h)
            h["updated"] = _now_iso()
            self._save()
            return dict(h)

    def get(self, hid: str) -> dict:
        with self._lock:
            if hid not in self._store:
                raise KeyError(hid)
            return dict(self._store[hid])

    def list_all(self, status: str | None = None) -> list[dict]:
        with self._lock:
            items = list(self._store.values())
        if status:
            items = [h for h in items if h["status"] == status]
        return sorted(items, key=lambda h: h["created"], reverse=True)

    def _prune(self):
        closed = sorted(
            [h for h in self._store.values() if h["status"] != "open"],
            key=lambda h: h["updated"])
        for h in closed[:max(1, len(self._store) - MAX_HYPOTHESES + 5)]:
            del self._store[h["id"]]
        self._save()
