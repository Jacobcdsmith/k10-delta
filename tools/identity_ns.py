"""identity.* and creator.* tool handlers."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .context import ctx_get
from .schema import _p, tool




def _directive_active(directive: dict | None) -> dict | None:
    """Return directive if not expired, else None."""
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
        return None
    return directive


def register(registry, ctx) -> None:
    soul = ctx_get(ctx, "soul")
    save_soul = ctx_get(ctx, "save_soul")
    load_notes = ctx_get(ctx, "load_notes")
    save_notes = ctx_get(ctx, "save_notes")
    state_lock = ctx_get(ctx, "state_lock")
    _now_iso = ctx_get(ctx, "_now_iso")

    def identity_read(args: dict) -> str:
        notes = load_notes()
        return json.dumps(
            {
                "identity_thread": soul.get("identity_thread", {}),
                "self_narrative": notes.get("self_narrative", ""),
            },
            indent=2,
        )

    def identity_update(args: dict) -> str:
        narrative = args.get("narrative")
        focus = args.get("focus")
        if narrative is None and focus is None:
            raise ValueError("Provide at least one of narrative or focus")

        with state_lock:
            notes = load_notes()
            if narrative is not None:
                notes["self_narrative"] = str(narrative).strip()
                save_notes(notes)
            thread = soul.setdefault("identity_thread", {})
            if narrative is not None:
                thread["narrative"] = notes["self_narrative"]
            if focus is not None:
                thread["current_focus"] = str(focus).strip()
            thread["updated_at"] = _now_iso()
            soul["identity_thread"] = thread
            save_soul(soul)

        return json.dumps(
            {
                "ok": True,
                "identity_thread": thread,
                "self_narrative": notes.get("self_narrative", ""),
            },
            indent=2,
        )

    def creator_steer(args: dict) -> str:
        directive = str(args["directive"]).strip()
        priority = int(args.get("priority", 8))
        expires_hours = int(args.get("expires_hours", 24))
        now = datetime.now(timezone.utc)
        set_at = _now_iso()
        expires_at = (now + timedelta(hours=expires_hours)).isoformat()

        entry = {
            "directive": directive,
            "priority": priority,
            "set_at": set_at,
            "expires_at": expires_at,
        }
        feedback_entry = {
            "ts": set_at,
            "text": directive,
            "rating": "pivot",
            "author": "Jacob",
        }
        with state_lock:
            soul["creator_directive"] = entry
            soul.setdefault("creator_feedback", []).append(feedback_entry)
            soul["creator_feedback"] = soul["creator_feedback"][-50:]
            save_soul(soul)

        return json.dumps({"ok": True, "directive": entry}, indent=2)

    def creator_directive(args: dict) -> str:
        active = _directive_active(soul.get("creator_directive"))
        return json.dumps({"active": active}, indent=2)

    registry.register_from_def(
        tool("identity.read", "Read identity thread and self-narrative.", {}),
        identity_read,
    )
    registry.register_from_def(
        tool("identity.update", "Update self-narrative and/or current focus.", {
            "narrative": _p("string", "Self-narrative text", required=False),
            "focus": _p("string", "Current focus label", required=False),
        }),
        identity_update,
    )
    registry.register_from_def(
        tool("creator.steer", "Set a time-bounded creator directive (highest steering priority).", {
            "directive": _p("string", "Steering directive"),
            "priority": _p("integer", "Priority 1-10 (default 8)", required=False),
            "expires_hours": _p("integer", "Hours until expiry (default 24)", required=False),
        }),
        creator_steer,
    )
    registry.register_from_def(
        tool("creator.directive", "Read active creator directive (null if expired).", {}),
        creator_directive,
    )