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
DEFAULT_HOST = os.environ.get("K10_DASHBOARD_HOST", "0.0.0.0")


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
            in_progress_goals = 0
            stalled_goals = 0
            if _ctx.goals:
                open_goals = len(_ctx.goals.list_all("open"))
                in_progress_goals = len(_ctx.goals.list_all("in_progress"))
                stalled_goals = len(_ctx.goals.list_all("stalled"))
            return _json_response(self, {
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
                "in_progress_goals": in_progress_goals,
                "stalled_goals": stalled_goals,
                "active_goals": open_goals + in_progress_goals,
                "will_enabled": bool(auto.get("enabled", True)),
                "last_intention": last_intention,
                "last_tick_cycle": auto.get("last_tick_cycle"),
                "will_counters": counters,
                "outward_act_ratio": (
                    round(outward / tool_acts, 3) if tool_acts else None
                ),
                "axiom_count": len(_ctx.soul.get("axioms") or []),
            })

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
            sock.bind((DEFAULT_HOST, port))
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
    if _server is not None and _active_port is not None:
        return f"http://{DEFAULT_HOST}:{_active_port}"

    chosen = find_dashboard_port(port)
    if chosen != port:
        log.warning("Dashboard port %d busy — using %d", port, chosen)

    _server = ThreadingHTTPServer((DEFAULT_HOST, chosen), DashboardHandler)
    _active_port = chosen
    _thread = threading.Thread(
        target=_server.serve_forever, daemon=True, name="dashboard")
    _thread.start()
    url = f"http://{DEFAULT_HOST}:{chosen}"
    log.info("Dashboard UI at %s", url)
    return url