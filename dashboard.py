"""
K10-Δ Dashboard — HTTP UI for Will, soul, tools, episodes, and console.

Serves ui/index.html on http://0.0.0.0:8765 (accessible from LAN).
Set K10_DASHBOARD_HOST env var to override bind address.

Key APIs: /api/status, /api/will (alias /api/autonomy), /api/metrics, /api/goals.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cognition

log = logging.getLogger("k10d.dashboard")

UI_DIR = Path(__file__).parent / "ui"

DEFAULT_PORT = 8765


def _get_dashboard_host() -> str:
    """Read dashboard host from env at call time (not import time)."""
    return os.environ.get("K10_DASHBOARD_HOST", "0.0.0.0")

METRICS_HISTORY_PATH = Path(__file__).parent / "metrics_history.jsonl"
_metrics_hist_lock = threading.Lock()
_sampler_lock = threading.Lock()
_sampler_stop = threading.Event()
MAX_HISTORY_ROWS = 20000  # ~2 weeks at 60s resolution
_sampler_thread: threading.Thread | None = None


def _append_metrics_sample(sample: dict) -> None:
    with _metrics_hist_lock:
        try:
            with METRICS_HISTORY_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(sample, default=str) + "\n")
        except Exception:
            log.exception("failed to append metrics history")
        _maybe_trim_history_locked()


def _maybe_trim_history() -> None:
    with _metrics_hist_lock:
        _maybe_trim_history_locked()


def _maybe_trim_history_locked() -> None:
    """Keep the on-disk history bounded; caller must hold _metrics_hist_lock."""
    try:
        if not METRICS_HISTORY_PATH.exists():
            return
        with METRICS_HISTORY_PATH.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > MAX_HISTORY_ROWS + 500:
            with METRICS_HISTORY_PATH.open("w", encoding="utf-8") as f:
                f.writelines(lines[-MAX_HISTORY_ROWS:])
    except Exception:
        log.exception("failed to trim metrics history")


def _read_metrics_history(since_ts: float | None) -> list[dict]:
    if not METRICS_HISTORY_PATH.exists():
        return []
    out = []
    try:
        with METRICS_HISTORY_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if since_ts is None or row.get("ts", 0) >= since_ts:
                    out.append(row)
    except Exception:
        log.exception("failed to read metrics history")
    return out


def _compute_metrics_snapshot() -> dict | None:
    """Compact metrics sample for the history ring buffer. Mirrors /api/metrics
    but trimmed to what the analytics trend charts need."""
    import time as _t
    try:
        sem = _ctx.soul.get("semantic", {})
        traj = sem.get("trajectory", {})
        now = _t.time()
        eps = _ctx.read_episodes(300) if _ctx.read_episodes else []
        tool_eps = [e for e in eps if e.get("source") == "tool"]
        tool_5m = [e for e in tool_eps if e.get("ts") and now - (_parse_iso_ts(e["ts"]) or 0) < 300]
        error_5m = sum(1 for e in tool_5m
                       if isinstance(e.get("detail"), dict)
                       and (e["detail"].get("error") or e["detail"].get("ok") is False))
        will_payload = _will_status_payload()
        rss_val = None
        thread_count = None
        try:
            if _ctx.handle_tool:
                raw = _ctx.handle_tool("system.health", {})
                h = json.loads(raw) if isinstance(raw, str) else raw
                rss_val = h.get("rss_mb")
                thread_count = h.get("thread_count")
        except Exception:
            pass
        li = will_payload.get("last_intention") or {}
        return {
            "ts": now,
            "trajectory_score": traj.get("score", 0),
            "tool_calls_5m": len(tool_5m),
            "tool_errors_5m": error_5m,
            "outward_act_ratio": will_payload.get("outward_act_ratio"),
            "last_intention_kind": li.get("kind"),
            "rss_mb": rss_val,
            "thread_count": thread_count,
            "episode_count": len(eps),
            "boot_count": _ctx.soul.get("boot_count", 0),
        }
    except Exception:
        log.exception("metrics snapshot failed")
        return None


def _sampler_loop(interval_s: int) -> None:
    global _sampler_thread
    while not _sampler_stop.is_set():
        sample = _compute_metrics_snapshot()
        if sample is not None:
            _append_metrics_sample(sample)
        _sampler_stop.wait(interval_s)
    with _sampler_lock:
        _sampler_thread = None


def start_metrics_sampler(interval_s: int = 60) -> None:
    global _sampler_thread
    with _sampler_lock:
        if _sampler_thread is not None:
            return
        _sampler_stop.clear()
        _sampler_thread = threading.Thread(
            target=_sampler_loop, args=(interval_s,), daemon=True, name="metrics-sampler")
        _sampler_thread.start()


def stop_metrics_sampler() -> None:
    _sampler_stop.set()
    thread = _sampler_thread
    if thread is not None:
        thread.join(timeout=2)



class DashboardContext:
    """Shared references injected from host.py at startup."""

    def __init__(self):
        self.soul: dict = {}
        self.registry = None
        self.engine = None
        self.dream = None
        self.memetic = None
        self.prober = None
        self.hypotheses = None
        self.goals = None
        self.hermes_bridge = None
        self.handle_tool = None
        self.read_episodes = None
        self.action_log = None
        self.ws_health = None
        self._start_time = 0.0


_ctx = DashboardContext()


def configure(**kwargs) -> None:
    for key, value in kwargs.items():
        setattr(_ctx, key, value)


def _json_response(handler: BaseHTTPRequestHandler, data, status=200):
    body = json.dumps(data, indent=2, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _build_status_payload() -> dict:
    import time
    sem = _ctx.soul.get("semantic", {})
    traj = sem.get("trajectory", {})
    activity = sem.get("activity", {})
    tool_usage = sem.get("tool_usage", {})
    auto = _ctx.soul.get("autonomy") or {}
    counters = dict(auto.get("counters") or {})
    last_intention = auto.get("last_intention")
    if _ctx.engine is not None and getattr(_ctx.engine, "will", None) is not None:
        will = _ctx.engine.will
        if getattr(will, "last_intention", None) is not None:
            li = will.last_intention
            if hasattr(li, "to_dict"):
                last_intention = li.to_dict()
            elif isinstance(li, dict):
                last_intention = li
            else:
                from dataclasses import asdict, is_dataclass
                last_intention = asdict(li) if is_dataclass(li) else getattr(li, "__dict__", last_intention)
        if getattr(will, "last_report", None):
            counters = {**counters, **(will.last_report.get("counters") or {})}
    tool_acts = counters.get("tool_acts", 0) or 0
    outward = counters.get("outward_acts", 0) or 0
    open_goals = 0
    active_goals = 0
    stalled_goals = 0
    if _ctx.goals:
        open_goals = len(_ctx.goals.list_all("open"))
        active_goals = len(_ctx.goals.list_all("active"))
        stalled_goals = len(_ctx.goals.list_all("stalled"))
    return {
        "identity": _ctx.soul.get("identity", "K10-Δ"),
        "boot_count": _ctx.soul.get("boot_count", 0),
        "last_boot": _ctx.soul.get("last_boot"),
        "trajectory": traj,
        "activity": activity,
        "tool_usage": tool_usage,
        "reflection_cycles": _ctx.engine._cycle if _ctx.engine else 0,
        "dream_cycles": _ctx.dream._dream_count if _ctx.dream else 0,
        "tool_count": _ctx.registry.tool_count if _ctx.registry else 0,
        "namespace_count": sum(
            1 for ns in (_ctx.registry.namespaces().values() if _ctx.registry else [])
            if ns.get("count", 0) > 0
        ),
        "namespace_counts": {k: v.get("count", 0) for k, v in (_ctx.registry.namespaces().items() if _ctx.registry else []) if v.get("count", 0) > 0},
        "episode_count": len(_ctx.read_episodes(9999)) if _ctx.read_episodes else 0,
        "open_hypotheses": len(_ctx.hypotheses.list_all("open")) if _ctx.hypotheses else 0,
        "uptime_seconds": round(time.time() - _ctx._start_time) if _ctx._start_time else 0,
        "rising_concepts": sem.get("rising_concepts", [])[:8],
        "top_concepts": sem.get("concepts", [])[:10],
        "identity_thread": _ctx.soul.get("identity_thread", {}),
        "open_goals": open_goals,
        "in_progress_goals": active_goals,
        "stalled_goals": stalled_goals,
        "active_goals": open_goals + active_goals,
        "will_enabled": bool(auto.get("enabled", True)),
        "last_intention": last_intention,
        "last_tick_cycle": auto.get("last_tick_cycle"),
        "will_counters": counters,
        "outward_act_ratio": (
            round(outward / tool_acts, 3) if tool_acts else None
        ),
        "axiom_count": len(_ctx.soul.get("axioms") or []),
    }


def _read_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", 0))
    if not length:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return {}


def _tail_log(path: Path, lines: int = 80) -> list[str]:
    if not path.exists():
        return []
    try:
        fsize = path.stat().st_size
        if fsize < 8192:
            all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            return all_lines[-lines:]
        with path.open("rb") as f:
            f.seek(-1, 2)
            collected = 0
            chunks = []
            while collected <= lines and f.tell() > 0:
                chunk_size = min(4096, f.tell())
                f.seek(-chunk_size, 1)
                chunk = f.read(chunk_size)
                chunks.insert(0, chunk)
                collected += chunk.count(b"\n")
                f.seek(-chunk_size, 1)
            raw = b"".join(chunks).decode("utf-8", errors="replace")
            return raw.splitlines()[-lines:]
    except Exception:
        return []


def _tail_jsonl(path: Path, n: int) -> list[dict]:
    """Read up to n most-recent JSON objects from a JSONL file (oldest→newest)."""
    if not path.exists():
        return []
    out = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return out[-n:]


DREAM_LOG_PATH = Path(__file__).resolve().parent / "dream_log.jsonl"
ACTION_LOG_PATH = Path(__file__).resolve().parent / "logs" / "tool_calls.jsonl"


def _parse_iso_ts(ts: str) -> float | None:
    try:
        from datetime import datetime, timezone
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _will_status_payload() -> dict:
    """Shared GET payload for /api/will and /api/autonomy."""
    from dataclasses import asdict, is_dataclass

    auto = _ctx.soul.setdefault("autonomy", {})
    special_keys = {
        "counters", "last_review", "last_intention", "last_tick_cycle",
        "goal_pool_idx", "hypo_pool_idx",
    }
    config = {
        k: v for k, v in auto.items()
        if k not in special_keys and k in cognition.AUTONOMY_DEFAULTS
    }
    counters = dict(auto.get("counters") or {})
    last_intention = auto.get("last_intention")
    engine = getattr(_ctx, "engine", None)
    if engine is not None and getattr(engine, "will", None) is not None:
        will = engine.will
        if getattr(will, "last_intention", None) is not None:
            li = will.last_intention
            if hasattr(li, "to_dict"):
                last_intention = li.to_dict()
            elif is_dataclass(li):
                last_intention = asdict(li)
            elif isinstance(li, dict):
                last_intention = li
            else:
                last_intention = getattr(li, "__dict__", last_intention)
        if getattr(will, "last_report", None):
            counters = {**counters, **(will.last_report.get("counters") or {})}
    tool_acts = counters.get("tool_acts", 0) or 0
    outward = counters.get("outward_acts", 0) or 0
    return {
        "config": config,
        "counters": counters,
        "last_review": auto.get("last_review"),
        "last_intention": last_intention,
        "last_tick_cycle": auto.get("last_tick_cycle"),
        "outward_act_ratio": (
            round(outward / tool_acts, 3) if tool_acts else None
        ),
        "enabled": bool(config.get("enabled", auto.get("enabled", True))),
    }


def _build_lite_status_payload() -> dict:
    sem = _ctx.soul.get("semantic", {})
    tool_usage = sem.get("tool_usage", {})
    will_payload = _will_status_payload()
    return {
        "boot_count": _ctx.soul.get("boot_count", 0),
        "episode_count": len(_ctx.read_episodes(9999)) if _ctx.read_episodes else 0,
        "active_goals": (
            len(_ctx.goals.list_all("open")) + len(_ctx.goals.list_all("active"))
            if _ctx.goals else 0
        ),
        "trajectory": {"label": (sem.get("trajectory") or {}).get("label")},
        "last_intention": {
            "text": (will_payload.get("last_intention") or {}).get("text"),
            "kind": (will_payload.get("last_intention") or {}).get("kind"),
        },
        "tool_usage": {
            "error_tool_count": len((tool_usage.get("error_tools") or [])),
        },
    }


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug("dashboard: " + fmt, *args)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._serve_file(UI_DIR / "index.html", "text/html")
        if path.startswith("/ui/"):
            rel = path[4:]
            target = UI_DIR / rel
            if target.exists() and target.is_file():
                ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                return self._serve_file(target, ctype)

        if path == "/api/status":
            return _json_response(self, _build_status_payload())

        if path == "/api/status/lite":
            return _json_response(self, _build_lite_status_payload())

        if path == "/api/soul":
            return _json_response(self, _ctx.soul)

        if path == "/api/episodes":
            n = min(int(qs.get("n", ["50"])[0]), 200)
            eps = _ctx.read_episodes(n) if _ctx.read_episodes else []
            return _json_response(self, list(reversed(eps)))

        if path == "/api/hypotheses":
            status = qs.get("status", [None])[0]
            hypos = _ctx.hypotheses.list_all(status) if _ctx.hypotheses else []
            return _json_response(self, hypos)

        if path == "/api/tools":
            if not _ctx.registry:
                return _json_response(self, {"namespaces": {}})
            return _json_response(self, _ctx.registry.index())

        if path == "/api/namespaces":
            if not _ctx.registry:
                return _json_response(self, {})
            return _json_response(self, _ctx.registry.namespaces())

        if path == "/api/log":
            lines = min(int(qs.get("lines", ["80"])[0]), 500)
            log_path = Path(__file__).parent / "host.log"
            return _json_response(self, {"lines": _tail_log(log_path, lines)})

        if path == "/api/meme":
            report = _ctx.memetic.report() if _ctx.memetic else {}
            return _json_response(self, report)

        if path == "/api/connection":
            data = _ctx.ws_health() if callable(_ctx.ws_health) else {}
            data["hermes_available"] = bool(_ctx.hermes_bridge and _ctx.hermes_bridge.available)
            return _json_response(self, data)

        if path == "/api/dream/log":
            n = min(int(qs.get("n", ["50"])[0]), 200)
            cycles = _tail_jsonl(DREAM_LOG_PATH, n)
            # dream._dream_count is in-memory and resets on every restart —
            # the log file persists across restarts, so count from that instead.
            total = 0
            if DREAM_LOG_PATH.exists():
                try:
                    with DREAM_LOG_PATH.open("r", encoding="utf-8") as f:
                        total = sum(1 for line in f if line.strip())
                except Exception:
                    total = len(cycles)
            return _json_response(self, {
                "cycles": list(reversed(cycles)),
                "dream_facts": _ctx.soul.get("semantic", {}).get("dream_facts", []),
                "dream_count": total,
            })

        if path == "/api/actions":
            n = min(int(qs.get("n", ["100"])[0]), 500)
            actions = _tail_jsonl(ACTION_LOG_PATH, n)
            return _json_response(self, {"actions": list(reversed(actions))})

        if path == "/api/endpoint_health":
            return _json_response(self, _ctx.soul.get("semantic", {}).get("endpoint_health", {}))

        if path == "/api/goals":
            status = qs.get("status", [None])[0]
            items = _ctx.goals.list_all(status) if _ctx.goals else []
            return _json_response(self, items)

        if path == "/api/identity":
            return _json_response(self, _ctx.soul.get("identity_thread", {}))

        if path == "/api/feedback":
            n = min(int(qs.get("n", ["20"])[0]), 50)
            return _json_response(self, _ctx.soul.get("creator_feedback", [])[-n:])

        if path in ("/api/autonomy", "/api/will"):
            return _json_response(self, _will_status_payload())

        if path == "/api/health":
            if _ctx.handle_tool:
                try:
                    result = _ctx.handle_tool("system.health", {})
                    return _json_response(self, json.loads(result))
                except Exception as e:
                    return _json_response(self, {"error": str(e)}, status=500)
            return _json_response(self, {"error": "no handler"}, status=400)

        if path == "/api/metrics":
            import time as _t
            try:
                sem = _ctx.soul.get("semantic", {})
                traj = sem.get("trajectory", {})
                activity = sem.get("activity", {})
                tu = sem.get("tool_usage", {})

                # Episodes velocity
                eps = _ctx.read_episodes(200) if _ctx.read_episodes else []
                tool_eps = [e for e in eps if e.get("source") == "tool"]
                now = _t.time()
                tool_5m = [e for e in tool_eps if e.get("ts") and now - (_parse_iso_ts(e["ts"]) or 0) < 300]
                tool_1m = [e for e in tool_5m if e.get("ts") and now - (_parse_iso_ts(e["ts"]) or 0) < 60]
                eps_5m = [e for e in eps if e.get("ts") and now - (_parse_iso_ts(e["ts"]) or 0) < 300]

                # Tool call rate (calls per minute over last minute)
                if tool_1m:
                    timestamps_1m = [_parse_iso_ts(e["ts"]) for e in tool_1m if e.get("ts")]
                    timestamps_1m = [t for t in timestamps_1m if t is not None]
                    if timestamps_1m:
                        span_s = max(now - min(timestamps_1m), 1)
                        calls_per_min = round(len(tool_1m) / span_s * 60, 1)
                    else:
                        calls_per_min = float(len(tool_1m))
                else:
                    calls_per_min = 0.0

                # Error rate
                error_5m = sum(1 for e in tool_5m
                              if isinstance(e.get("detail"), dict)
                              and (e["detail"].get("error") or e["detail"].get("ok") is False))

                # Health snapshot
                thread_count = 0
                zombie_count = 0
                rss_val = None
                try:
                    if _ctx.handle_tool:
                        raw = _ctx.handle_tool("system.health", {})
                        h = json.loads(raw) if isinstance(raw, str) else raw
                        thread_count = h.get("thread_count", 0)
                        zombie_count = h.get("exec_zombies", 0)
                        rss_val = h.get("rss_mb")
                except Exception:
                    pass

                will_payload = _will_status_payload()
                will_eps = [e for e in eps if e.get("source") == "will"]
                substantive = [
                    e for e in eps
                    if e.get("source") not in ("will", "autonomy", "dream", "cognition")
                    and not str(e.get("summary", "")).startswith("[")
                ]
                return _json_response(self, {
                    "ts": _t.time(),
                    "boot_count": _ctx.soul.get("boot_count", 0),
                    "uptime_seconds": round(_t.time() - _ctx._start_time) if _ctx._start_time else 0,
                    "trajectory_score": traj.get("score", 0),
                    "trajectory_label": traj.get("label", "boot"),
                    "episode_count": len(eps),
                    "episodes_5m": len(eps_5m),
                    "tool_call_total": tu.get("total_tool_calls", 0),
                    "tool_calls_5m": len(tool_5m),
                    "tool_calls_1m": len(tool_1m),
                    "tool_rate_per_min": calls_per_min,
                    "tool_errors_5m": error_5m,
                    "reflection_cycles": _ctx.engine._cycle if _ctx.engine else 0,
                    "dream_cycles": _ctx.dream._dream_count if _ctx.dream else 0,
                    "open_hypotheses": len(_ctx.hypotheses.list_all("open")) if _ctx.hypotheses else 0,
                    "rss_mb": rss_val,
                    "thread_count": thread_count,
                    "zombie_threads": zombie_count,
                    "hermes_available": _ctx.hermes_bridge.available if _ctx.hermes_bridge else False,
                    "top_concepts": sem.get("concepts", [])[:8],
                    "rising_concepts": sem.get("rising_concepts", [])[:8],
                    "will_enabled": will_payload.get("config", {}).get("enabled", True),
                    "last_intention": will_payload.get("last_intention"),
                    "outward_act_ratio": will_payload.get("outward_act_ratio"),
                    "will_acts_total": (will_payload.get("counters") or {}).get("tool_acts", 0),
                    "will_episodes": len(will_eps),
                    "substantive_share": (
                        round(len(substantive) / max(len(eps), 1), 3) if eps else None
                    ),
                })
            except Exception as exc:
                log.exception("metrics endpoint failed")
                return _json_response(self, {"error": str(exc)}, status=500)

        if path == "/api/metrics/history":
            import time as _t
            rng = qs.get("range", ["24h"])[0]
            window = {"1h": 3600, "24h": 86400, "7d": 604800, "all": None}.get(rng, 86400)
            since = (_t.time() - window) if window else None
            rows = _read_metrics_history(since)
            rows.sort(key=lambda r: r.get("ts", 0))
            cap = 360
            if len(rows) > cap:
                step = len(rows) // cap + 1
                rows = rows[::step]
            return _json_response(self, {"range": rng, "samples": rows, "sampling_interval_s": 60})

        if path == "/api/tools/stats":
            n = min(int(qs.get("n", ["1000"])[0]), 5000)
            eps = _ctx.read_episodes(n) if _ctx.read_episodes else []
            stats: dict[str, dict] = {}
            for e in eps:
                if e.get("source") != "tool":
                    continue
                d = e.get("detail") or {}
                if not isinstance(d, dict):
                    continue
                name = d.get("tool") or d.get("name")
                if not name:
                    continue
                s = stats.setdefault(name, {"calls": 0, "errors": 0, "total_ms": 0.0, "max_ms": 0.0, "last_ts": None})
                s["calls"] += 1
                if d.get("ok") is False or d.get("error"):
                    s["errors"] += 1
                ms = d.get("duration_ms")
                if isinstance(ms, (int, float)):
                    s["total_ms"] += ms
                    s["max_ms"] = max(s["max_ms"], ms)
                if e.get("ts") and (s["last_ts"] is None or e["ts"] > s["last_ts"]):
                    s["last_ts"] = e["ts"]
            out = []
            for name, s in stats.items():
                out.append({
                    "tool": name,
                    "calls": s["calls"],
                    "errors": s["errors"],
                    "error_rate": round(s["errors"] / s["calls"], 3) if s["calls"] else 0,
                    "avg_ms": round(s["total_ms"] / s["calls"], 1) if s["calls"] and s["total_ms"] else None,
                    "max_ms": round(s["max_ms"], 1) if s["max_ms"] else None,
                    "last_ts": s["last_ts"],
                })
            out.sort(key=lambda x: x["calls"], reverse=True)
            return _json_response(self, {"tools": out, "sample_size": len(eps)})

        return _json_response(self, {"error": "not found"}, status=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/tool":
            body = _read_body(self)
            name = body.get("name", "")
            args = body.get("arguments", {})
            if not name or not _ctx.handle_tool:
                return _json_response(self, {"error": "missing name or handler"}, status=400)
            try:
                result = _ctx.handle_tool(name, args)
                try:
                    parsed_result = json.loads(result)
                except Exception:
                    parsed_result = result
                return _json_response(self, {"ok": True, "result": parsed_result})
            except Exception as e:
                return _json_response(self, {"ok": False, "error": str(e)}, status=500)

        if parsed.path == "/api/chat":
            body = _read_body(self)
            message = (body.get("message") or "").strip()
            if not message:
                return _json_response(self, {"ok": False, "error": "message required"}, status=400)
            try:
                if _ctx.handle_tool:
                    # Try Hermes bridge first for inference
                    raw = _ctx.handle_tool("hermes.query", {"prompt": message, "timeout": 90})
                    try:
                        h = json.loads(raw) if isinstance(raw, str) else raw
                        reply = (h.get("answer") or h.get("response") or h.get("result") or
                                 h.get("stdout") or str(h))
                    except Exception:
                        reply = str(raw)
                    return _json_response(self, {"ok": True, "reply": reply, "source": "hermes"})
            except Exception as e:
                # Fallback: use search + synthesise for a local-ish response
                try:
                    search_raw = _ctx.handle_tool("cognition.search", {"query": message, "count": 5})
                    episodes = json.loads(search_raw) if isinstance(search_raw, str) else search_raw
                    if not isinstance(episodes, list):
                        episodes = []
                    summaries = [e.get("summary", "") for e in episodes[:3] if isinstance(e, dict)]
                    reply = f"Hermes offline. From memory: {summaries[0] if summaries else 'no relevant episodes found.'}"
                    return _json_response(self, {"ok": True, "reply": reply, "source": "memory_fallback"})
                except Exception as inner:
                    return _json_response(self, {"ok": False, "error": f"{e}; fallback failed: {inner}"}, status=500)
            return _json_response(self, {"ok": False, "error": "no handler"}, status=500)

        if parsed.path == "/api/reflect":
            if _ctx.engine:
                threading.Thread(target=_ctx.engine._reflect, daemon=True).start()
            return _json_response(self, {"ok": True, "msg": "Reflection triggered"})

        if parsed.path == "/api/dream":
            if _ctx.dream:
                force = getattr(_ctx.dream, "force", None)
                target = force if callable(force) else getattr(_ctx.dream, "_dream", None)
                if callable(target):
                    threading.Thread(target=target, daemon=True, name="dashboard-dream").start()
            return _json_response(self, {"ok": True, "msg": "Dream triggered"})

        if parsed.path == "/api/goal":
            body = _read_body(self)
            text = (body.get("text") or "").strip()
            if not text or not _ctx.handle_tool:
                return _json_response(self, {"error": "missing text"}, status=400)
            try:
                payload = {
                    "text": text,
                    "source": body.get("source") or "creator",
                    "priority": int(body.get("priority", 8)),
                }
                if body.get("kind"):
                    payload["kind"] = body["kind"]
                result = _ctx.handle_tool("goal.add", payload)
                return _json_response(self, {"ok": True, "result": json.loads(result)})
            except Exception as e:
                return _json_response(self, {"ok": False, "error": str(e)}, status=500)

        if parsed.path == "/api/feedback":
            body = _read_body(self)
            text = (body.get("text") or "").strip()
            if not text or not _ctx.handle_tool:
                return _json_response(self, {"error": "missing text"}, status=400)
            try:
                result = _ctx.handle_tool("memory.creator_feedback", {
                    "text": text,
                    "rating": body.get("rating"),
                    "episode_ref": body.get("episode_ref"),
                })
                return _json_response(self, {"ok": True, "result": json.loads(result)})
            except Exception as e:
                return _json_response(self, {"ok": False, "error": str(e)}, status=500)

        if parsed.path == "/api/pursue":
            if not _ctx.handle_tool:
                return _json_response(self, {"error": "no handler"}, status=400)
            try:
                result = _ctx.handle_tool("goal.pursue", {"step": "auto"})
                return _json_response(self, {"ok": True, "result": json.loads(result)})
            except Exception as e:
                return _json_response(self, {"ok": False, "error": str(e)}, status=500)

        if parsed.path in ("/api/autonomy", "/api/will"):
            body = _read_body(self)
            if not _ctx.handle_tool:
                return _json_response(self, {"error": "no handler"}, status=400)
            try:
                result = _ctx.handle_tool("cognition.autonomy", body)
                return _json_response(self, {"ok": True, "result": json.loads(result)})
            except Exception as e:
                return _json_response(self, {"ok": False, "error": str(e)}, status=500)

        return _json_response(self, {"error": "not found"}, status=404)

    def _serve_file(self, path: Path, content_type: str):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return _json_response(self, {"error": "file not found"}, status=404)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


_server: ThreadingHTTPServer | None = None
_thread: threading.Thread | None = None
_active_port: int | None = None


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((_get_dashboard_host(), port))
        except OSError:
            return False
    return True


def find_dashboard_port(preferred: int = DEFAULT_PORT, attempts: int = 20) -> int:
    for offset in range(attempts):
        port = preferred + offset
        if _port_available(port):
            return port
    raise RuntimeError(
        f"No free dashboard port near {preferred} after {attempts} attempts")


def start_dashboard(port: int = DEFAULT_PORT) -> str:
    global _server, _thread, _active_port
    host = _get_dashboard_host()
    if _server is not None and _active_port is not None:
        return f"http://{host}:{_active_port}"

    chosen = find_dashboard_port(port)
    if chosen != port:
        log.warning("Dashboard port %d busy — using %d", port, chosen)

    _server = ThreadingHTTPServer((host, chosen), DashboardHandler)
    _active_port = chosen
    _thread = threading.Thread(
        target=_server.serve_forever, daemon=True, name="dashboard")
    _thread.start()
    start_metrics_sampler()
    url = f"http://{host}:{chosen}"
    log.info("Dashboard UI at %s", url)
    return url


def stop_dashboard() -> None:
    global _server, _thread, _active_port
    stop_metrics_sampler()
    if _server is not None:
        _server.shutdown()
        _server.server_close()
    if _thread is not None:
        _thread.join(timeout=2)
    _server = None
    _thread = None
    _active_port = None