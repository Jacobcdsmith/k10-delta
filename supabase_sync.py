"""
DELTA — Supabase cloud sync (optional).

If SUPABASE_URL and SUPABASE_KEY are set, goals/episodes/soul are mirrored
to the cloud so they survive machine wipes and work across devices.

Tables (auto-created on first sync if you run init_schema()):
  delta_soul      — one row per agent identity
  delta_goals     — goal lifecycle rows, upserted by id
  delta_episodes  — append-only episode log

Set env vars to enable:
  SUPABASE_URL   — e.g. https://yccbcxsfycplfbkxpura.supabase.co
  SUPABASE_KEY   — your secret key (sb_secret_...)
  DELTA_AGENT_ID — optional name to namespace rows (default: "delta")
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger("delta.supabase")

_client = None
_enabled = False
_agent_id = "delta"
_lock = threading.Lock()
_last_episode_ts: str | None = None   # track what we've already pushed

SYNC_INTERVAL = int(os.environ.get("DELTA_SUPABASE_INTERVAL", "60"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Init ──────────────────────────────────────────────────────────────────────

def init() -> bool:
    """
    Try to connect to Supabase. Returns True if enabled.
    Safe to call even if the supabase package isn't installed.
    """
    global _client, _enabled, _agent_id

    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    _agent_id = os.environ.get("DELTA_AGENT_ID", "delta")

    if not url or not key:
        log.debug("Supabase sync disabled (SUPABASE_URL / SUPABASE_KEY not set)")
        return False

    try:
        from supabase import create_client  # type: ignore
        _client = create_client(url, key)
        _enabled = True
        log.info("Supabase sync enabled (agent_id=%s)", _agent_id)
        return True
    except ImportError:
        log.warning(
            "supabase package not installed — run: pip install supabase\n"
            "Cloud sync disabled."
        )
        return False
    except Exception as exc:
        log.warning("Supabase init failed: %s", exc)
        return False


def is_enabled() -> bool:
    return _enabled


# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
-- Run this once in your Supabase SQL editor to create the tables.

create table if not exists delta_soul (
  agent_id   text primary key,
  identity   text,
  boot_count int  default 0,
  data       jsonb,
  updated_at timestamptz default now()
);

create table if not exists delta_goals (
  id         text  not null,
  agent_id   text  not null,
  text       text,
  status     text,
  priority   int   default 0,
  data       jsonb,
  created_at timestamptz,
  updated_at timestamptz default now(),
  primary key (agent_id, id)
);

create table if not exists delta_episodes (
  id         uuid  default gen_random_uuid() primary key,
  agent_id   text  not null,
  ts         timestamptz,
  summary    text,
  data       jsonb,
  inserted_at timestamptz default now()
);

create index if not exists delta_episodes_ts on delta_episodes (agent_id, ts desc);
create index if not exists delta_goals_status on delta_goals (agent_id, status);
"""


def print_schema():
    """Print the SQL you need to run in the Supabase dashboard."""
    print(SCHEMA_SQL)


# ── Push helpers ──────────────────────────────────────────────────────────────

def push_soul(soul: dict) -> None:
    if not _enabled:
        return
    try:
        row = {
            "agent_id": _agent_id,
            "identity": soul.get("identity", "delta"),
            "boot_count": soul.get("boot_count", 0),
            "data": soul,
            "updated_at": _now(),
        }
        _client.table("delta_soul").upsert(row).execute()
        log.debug("Soul pushed to Supabase")
    except Exception as exc:
        log.warning("Supabase push_soul failed: %s", exc)


def push_goals(goals: list[dict]) -> None:
    if not _enabled or not goals:
        return
    try:
        rows = [
            {
                "id": g["id"],
                "agent_id": _agent_id,
                "text": g.get("text", ""),
                "status": g.get("status", "spawned"),
                "priority": g.get("priority", 0),
                "data": g,
                "created_at": g.get("created_at", _now()),
                "updated_at": _now(),
            }
            for g in goals
        ]
        _client.table("delta_goals").upsert(rows).execute()
        log.debug("Pushed %d goals to Supabase", len(rows))
    except Exception as exc:
        log.warning("Supabase push_goals failed: %s", exc)


def push_episodes(episodes: list[dict]) -> None:
    """Push new episodes (deduplicates by ts)."""
    global _last_episode_ts
    if not _enabled or not episodes:
        return
    try:
        with _lock:
            new_eps = (
                [e for e in episodes if e.get("ts", "") > _last_episode_ts]
                if _last_episode_ts
                else episodes
            )
            if not new_eps:
                return
            rows = [
                {
                    "agent_id": _agent_id,
                    "ts": e.get("ts"),
                    "summary": e.get("summary", ""),
                    "data": e,
                }
                for e in new_eps
            ]
            _client.table("delta_episodes").insert(rows).execute()
            _last_episode_ts = max(e.get("ts", "") for e in new_eps)
            log.debug("Pushed %d episodes to Supabase", len(rows))
    except Exception as exc:
        log.warning("Supabase push_episodes failed: %s", exc)


# ── Background sync thread ────────────────────────────────────────────────────

def start_sync_thread(get_soul, get_goals, get_episodes, stop_event: threading.Event):
    """
    Spawn a background thread that periodically pushes state to Supabase.

    Args:
        get_soul:     callable() → dict
        get_goals:    callable() → list[dict]
        get_episodes: callable(n) → list[dict]
        stop_event:   threading.Event that signals shutdown
    """
    if not _enabled:
        return

    def _loop():
        log.info("Supabase sync thread started (interval=%ds)", SYNC_INTERVAL)
        while not stop_event.wait(timeout=SYNC_INTERVAL):
            try:
                push_soul(get_soul())
                push_goals(get_goals())
                push_episodes(get_episodes(200))
            except Exception as exc:
                log.warning("Supabase sync cycle error: %s", exc)
        log.info("Supabase sync thread stopped")

    t = threading.Thread(target=_loop, name="supabase-sync", daemon=True)
    t.start()
    return t
