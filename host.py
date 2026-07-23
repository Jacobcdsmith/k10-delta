#!/usr/bin/env python3
"""
K10-Δ Host v4.0 — MCP server with ToolRegistry, cognition, dream, and self-modification.

Run:
  pip install websocket-client
  python Gateway/k10_delta/host.py
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
import types
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import hashlib

import websocket
from dotenv import load_dotenv

from cognition import (
    CognitionEngine, DreamEngine, MemeticEngine, AdversarialProber,
    HypothesisStore, _now_iso, _start_time, set_selfmod_engine,
    _substantive_episodes,
)
from selfmod import SelfModEngine
from chrono import ChronoEngine
from kairos import KairosEngine
from hermes_bridge import HermesBridge
from store import (
    SOUL_PATH, EPISODES_PATH, NOTES_PATH, HYPO_PATH, DREAM_LOG, WORKSPACE,
    state_lock, load_soul, save_soul, append_episode, read_episodes,
    load_notes, save_notes,
)
from tools.registry import ToolRegistry, build_namespace_proxies
from tools import memory as memory_tools
from tools import fs as fs_tools
from tools import exec_net_system
from tools import cognition_ns
from tools import selfmod_ns
from tools import goals_ns
from tools import act_ns
from tools import identity_ns
from tools import md_ns
from tools import skill_ns
from tools import device_ns
from tools import workflow_ns
from tools import sentiment_ns
from tools import utils_ns
from tools import text_ns
from tools import data_ns
from tools import k10_ns
from tools import kairos_ns
from tools import ide_ns
from goals import GoalStore, GOALS_PATH
import supabase_sync
from dashboard import configure as configure_dashboard, start_dashboard

# ── Config ─────────────────────────────────────────────────────────────────────

load_dotenv()

K10_DIR = Path(__file__).resolve().parent
SCHEDULE_PATH = K10_DIR / "schedule.json"
LOG_PATH = K10_DIR / "host.log"
RECONNECT_WAIT = int(os.environ.get("K10_RECONNECT_WAIT", "5"))
DASHBOARD_PORT = int(os.environ.get("K10_DASHBOARD_PORT", "8765"))

MCP_ENDPOINT = os.environ.get("K10_MCP_ENDPOINT", "")
if MCP_ENDPOINT:
    if not MCP_ENDPOINT.startswith("wss://"):
        MCP_ENDPOINT = f"wss://api.xiaozhi.me/mcp/?token={MCP_ENDPOINT}"
else:
    MCP_TOKEN = os.environ.get("K10_MCP_TOKEN", "")
    if not MCP_TOKEN:
        raise SystemExit(
            "Set K10_MCP_ENDPOINT (full wss:// URL) or K10_MCP_TOKEN "
            "(just the token) in the environment. See .env.example."
        )
    MCP_ENDPOINT = f"wss://api.xiaozhi.me/mcp/?token={MCP_TOKEN}"

log = logging.getLogger("k10d")

shutdown_requested = threading.Event()


def _handle_shutdown_signal(signum, frame):
    log.info("Received signal %d — requesting shutdown", signum)
    shutdown_requested.set()


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
    )
    if not any(isinstance(h, logging.FileHandler) for h in log.handlers):
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logging.getLogger().addHandler(fh)


@dataclass
class HostState:
    soul: dict
    registry: ToolRegistry
    hypotheses: HypothesisStore
    goals: GoalStore
    engine: CognitionEngine
    dream: DreamEngine
    memetic: MemeticEngine
    prober: AdversarialProber
    selfmod: SelfModEngine
    chrono: ChronoEngine
    kairos: KairosEngine
    hermes_bridge: HermesBridge
    ctx: types.SimpleNamespace
    dashboard_url: str


_state: HostState | None = None


def _register_dynamic_tool(registry: ToolRegistry, tool_def: dict, handler_fn):
    evicted = registry.evict_dynamic()
    if evicted:
        log.info("Evicted dynamic tool (at capacity): %s", evicted)
    registry.register_from_def(tool_def, handler_fn, dynamic=True)
    log.info("Hot-loaded tool: %s", tool_def["name"])


def _log_tool_call(name: str, args: dict, ok: bool, duration_ms: float,
                   error: str | None = None):
    detail = {
        "tool": name,
        "namespace": name.split(".", 1)[0],
        "duration_ms": round(duration_ms, 1),
        "ok": ok,
    }
    if error:
        detail["error"] = error[:200]
    append_episode({
        "ts": _now_iso(),
        "summary": f"tool:{name}",
        "source": "tool",
        "detail": detail,
    })


_dedup_cache: dict[str, tuple[float, str]] = {}
_DEDUP_MAX = 64
_DEDUP_READ_TTL = 30
_DEDUP_WRITE_TTL = 10

_WRITE_VERBS = frozenset({
    "set", "write", "delete", "append", "mutate", "compress", "add", "remove",
    "schedule", "restart", "create", "update", "commit", "revoke", "patch",
    "deliver", "propose", "steer",
})

def _is_write_tool(name: str) -> bool:
    verb = name.split(".", 1)[-1] if "." in name else name
    return verb in _WRITE_VERBS

def _dedup_key(name: str, args: dict) -> str:
    raw = json.dumps({"n": name, "a": args}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

def handle_tool(name: str, a: dict | None = None) -> str:
    if _state is None:
        raise RuntimeError("Host not booted")
    registry = _state.registry

    args = a or {}
    if name != "hermes.query":
        dedup_key = _dedup_key(name, args)
        ttl = _DEDUP_READ_TTL if not _is_write_tool(name) else _DEDUP_WRITE_TTL
        if dedup_key in _dedup_cache:
            ts, result = _dedup_cache[dedup_key]
            if time.time() - ts < ttl:
                log.debug("Dedup hit: %s", name)
                try:
                    parsed = json.loads(result)
                    parsed["_dedup"] = True
                    return json.dumps(parsed)
                except Exception:
                    return result

    start = time.time()
    try:
        result = registry.dispatch(name, args)
        if name != "hermes.query":
            if len(_dedup_cache) >= _DEDUP_MAX:
                oldest = min(_dedup_cache.keys(), key=lambda k: _dedup_cache[k][0])
                del _dedup_cache[oldest]
            _dedup_cache[_dedup_key(name, args)] = (time.time(), result)
        _log_tool_call(name, args, ok=True, duration_ms=(time.time() - start) * 1000)
        return result
    except Exception as e:
        _log_tool_call(name, args, ok=False,
                       duration_ms=(time.time() - start) * 1000, error=str(e))
        raise


def boot() -> HostState:
    """Initialize engines, registry, dashboard, and scheduled tasks."""
    global _state
    if _state is not None:
        return _state

    setup_logging()
    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    os.chdir(K10_DIR)
    WORKSPACE.mkdir(exist_ok=True)

    soul = load_soul()
    soul["boot_count"] = soul.get("boot_count", 0) + 1
    soul["last_boot"] = _now_iso()
    save_soul(soul)
    log.info("Boot #%d", soul["boot_count"])

    registry = ToolRegistry()
    hypotheses = HypothesisStore(HYPO_PATH)
    goals = GoalStore(GOALS_PATH)
    removed = hypotheses.dedupe_open()
    if removed:
        log.info("Deduped %d duplicate open hypotheses", removed)

    notes = load_notes()
    if pending := notes.pop("pending_request", None):
        g = goals.add(pending, source="migrated", priority=10)
        save_notes(notes)
        log.info("Migrated pending_request to goal %s", g["id"])

    memetic = MemeticEngine(soul, save_soul, state_lock)
    prober = AdversarialProber(soul, save_soul, hypotheses, state_lock)

    dream = DreamEngine(
        soul=soul, save_soul_fn=save_soul,
        read_episodes_fn=read_episodes, append_episode_fn=append_episode,
        hypotheses=hypotheses, state_lock=state_lock,
        dream_log_path=DREAM_LOG,
    )

    engine = CognitionEngine(
        soul=soul, save_soul_fn=save_soul,
        read_episodes_fn=read_episodes, append_episode_fn=append_episode,
        hypothesis_store=hypotheses, state_lock=state_lock,
        memetic=memetic, prober=prober,
        tool_index_fn=registry.index,
        load_notes_fn=load_notes,
        goals=goals,
        tier_manage_fn=lambda: {
            "promoted": registry.promote_hot_tools(threshold=10),
            "demoted": registry.demote_cold_tools(threshold=0, max_age_s=86400),
        },
        dream=dream,
        usage_stats_fn=registry.get_usage_stats,
        call_tool=handle_tool,
        workspace=WORKSPACE,
    )
    engine.start()

    kairos = KairosEngine(read_episodes, home=Path.home() / "kairos")
    chrono_globals = {
        "soul": soul, "save_soul": save_soul,
        "load_notes": load_notes, "save_notes": save_notes,
        "append_episode": append_episode, "read_episodes": read_episodes,
        "WORKSPACE": WORKSPACE, "hypotheses": hypotheses, "goals": goals,
        "engine": engine, "dream": dream, "memetic": memetic,
        "prober": prober, "kairos": kairos,
        "json": json, "Path": Path,
        "datetime": datetime, "timezone": timezone, "time": time,
    }
    chrono = ChronoEngine(SCHEDULE_PATH, soul, state_lock, globals_dict=chrono_globals)
    hermes_bridge = HermesBridge()

    selfmod = SelfModEngine(
        get_tools_fn=lambda: registry.all_tools,
        get_handler_fn=handle_tool,
        register_tool_fn=lambda d, h: _register_dynamic_tool(registry, d, h),
        call_tool_fn=handle_tool,
    )
    set_selfmod_engine(selfmod)
    selfmod.add_revoke_hook(registry.unregister)

    ctx = types.SimpleNamespace(
        soul=soul,
        save_soul=save_soul,
        read_episodes=read_episodes,
        append_episode=append_episode,
        load_notes=load_notes,
        save_notes=save_notes,
        state_lock=state_lock,
        hypotheses=hypotheses,
        goals=goals,
        call_tool=handle_tool,
        engine=engine,
        dream=dream,
        memetic=memetic,
        prober=prober,
        selfmod=selfmod,
        chrono=chrono,
        kairos=kairos,
        hermes_bridge=hermes_bridge,
        registry=registry,
        WORKSPACE=WORKSPACE,
        EPISODES_PATH=EPISODES_PATH,
        DREAM_LOG=DREAM_LOG,
        _now_iso=_now_iso,
        _start_time=_start_time,
    )

    memory_tools.register(registry, ctx)
    fs_tools.register(registry, ctx)
    exec_net_system.register(registry, ctx)
    cognition_ns.register(registry, ctx)
    selfmod_ns.register(registry, ctx)
    goals_ns.register(registry, ctx)
    act_ns.register(registry, ctx)
    identity_ns.register(registry, ctx)
    md_ns.register(registry, ctx)
    skill_ns.register(registry, ctx)
    device_ns.register(registry, ctx)
    workflow_ns.register(registry, ctx)
    sentiment_ns.register(registry, ctx)
    k10_ns.register(registry, ctx)
    kairos_ns.register(registry, ctx)
    ide_ns.register(registry, ctx)
    utils_ns.register(registry, ctx)
    text_ns.register(registry, ctx)
    data_ns.register(registry, ctx)

    engine._update_identity_thread(
        _substantive_episodes(read_episodes(100)),
        soul.get("semantic", {}).get("rising_concepts", []),
        soul.get("semantic", {}).get("trajectory", {"label": "boot"}),
        ["goal.pursue top open goal"] if goals.list_all("open") else ["observe and log"],
    )

    load_result = selfmod.auto_load_staged(registry)
    if load_result["loaded"]:
        log.info("Auto-loaded %d staged tools: %s",
                 len(load_result["loaded"]), ", ".join(load_result["loaded"]))
    if load_result["errors"]:
        for err in load_result["errors"]:
            log.warning("Staged tool load error %s: %s", err["name"], err["error"])

    chrono_globals.update(build_namespace_proxies(registry, handle_tool))

    active_ns = sum(
        1 for ns in registry.namespaces().values() if ns["count"] > 0)
    log.info("Registry ready — %d tools across %d namespaces",
             registry.tool_count, active_ns)

    # Host must be booted before chrono/cognition threads call handle_tool.
    _state = HostState(
        soul=soul,
        registry=registry,
        hypotheses=hypotheses,
        goals=goals,
        engine=engine,
        dream=dream,
        memetic=memetic,
        prober=prober,
        selfmod=selfmod,
        chrono=chrono,
        kairos=kairos,
        hermes_bridge=hermes_bridge,
        ctx=ctx,
        dashboard_url="",
    )

    configure_dashboard(
        soul=soul,
        registry=registry,
        engine=engine,
        dream=dream,
        memetic=memetic,
        prober=prober,
        hypotheses=hypotheses,
        goals=goals,
        handle_tool=handle_tool,
        read_episodes=read_episodes,
        _start_time=_start_time,
    )
    dashboard_url = start_dashboard(DASHBOARD_PORT)
    _state.dashboard_url = dashboard_url
    log.info("Dashboard UI → %s", dashboard_url)

    chrono.reset_last_run()
    chrono.start()

    # Optional Supabase cloud sync
    if supabase_sync.init():
        supabase_sync.start_sync_thread(
            get_soul=lambda: _state.soul,
            get_goals=lambda: list(_state.goals._goals.values()),
            get_episodes=lambda n: read_episodes(n),
            stop_event=shutdown_requested,
        )

    return _state


def _do_shutdown(state: HostState):
    log.info("Shutting down engines...")
    state.dream.ping()
    state.chrono.stop()
    state.engine.stop()
    save_soul(state.soul)
    log.info("Shutdown complete.")

# ── MCP server ─────────────────────────────────────────────────────────────────

def _reply(ws, msg_id, result=None, error=None):
    frame = {"jsonrpc": "2.0", "id": msg_id}
    frame["error" if error else "result"] = (
        {"code": -32000, "message": str(error)} if error else result)
    ws.send(json.dumps(frame))


def _handle(ws, raw: str, state: HostState):
    if shutdown_requested.is_set():
        return
    try:
        msg = json.loads(raw)
    except Exception:
        return

    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}
    if method is None:
        return

    if method == "initialize":
        log.info("← initialize")
        _reply(ws, msg_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {},
                "toolFiltering": True,
            },
            "serverInfo": {"name": "k10-delta-host", "version": "6.0"},
        })
    elif method == "tools/list":
        # Supports: namespace, tier, tiers, cursor, limit
        result = state.registry.list_tools(
            namespace=params.get("namespace"),
            tier=params.get("tier"),
            tiers=params.get("tiers"),
            cursor=params.get("cursor"),
            limit=params.get("limit"),
        )
        log.info("← tools/list (namespace=%s tier=%s → %d/%d tools)",
                 params.get("namespace", "*"), params.get("tier", "*"),
                 len(result["tools"]), result["total"])
        _reply(ws, msg_id, result)
    elif method == "tools/namespaces":
        summary = state.registry.namespace_summary()
        log.info("← tools/namespaces (%d namespaces)", len(summary))
        _reply(ws, msg_id, {"namespaces": summary})
    elif method == "tools/search":
        query = params.get("query", "")
        limit = params.get("limit", 20)
        results = state.registry.search(query, limit=limit)
        log.info("← tools/search %r (%d results)", query, len(results))
        _reply(ws, msg_id, {"tools": results})
    elif method == "tools/call":
        tname = params.get("name", "")
        args = params.get("arguments") or {}
        log.info("← %s", tname)
        state.dream.ping()
        try:
            text = handle_tool(tname, args)
            _reply(ws, msg_id, {"content": [{"type": "text", "text": text}]})
            log.info("→ ok  %s", tname)
        except Exception as e:
            log.warning("→ err %s: %s", tname, e)
            _reply(ws, msg_id, error=str(e))
    elif method == "ping":
        _reply(ws, msg_id, {})
    elif method.startswith("notifications/"):
        pass
    else:
        _reply(ws, msg_id, error=f"Method not implemented: {method}")


def _run_ws(state: HostState):
    ws = websocket.WebSocket()
    ws.connect(MCP_ENDPOINT, timeout=10)
    ws.settimeout(300)
    log.info("Connected — %d tools ready", state.registry.tool_count)
    try:
        while not shutdown_requested.is_set():
            try:
                raw = ws.recv()
                if raw:
                    _handle(ws, raw, state)
            except websocket.WebSocketTimeoutException:
                if shutdown_requested.is_set():
                    break
                ws.ping()
    finally:
        ws.close()


def run():
    state = boot()
    log.info("=== K10-Δ Host v6.0 — boot #%d ===", state.soul["boot_count"])
    while not shutdown_requested.is_set():
        try:
            _run_ws(state)
        except Exception as e:
            if shutdown_requested.is_set():
                break
            log.warning("Disconnected: %s — retry in %ds", e, RECONNECT_WAIT)
            time.sleep(RECONNECT_WAIT)
    _do_shutdown(state)


if __name__ == "__main__":
    run()