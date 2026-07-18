"""exec.*, system.*, cron.*, hermes.* tool handlers."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .context import ctx_get
from .registry import build_namespace_proxies
from .schema import _p, tool

log = logging.getLogger("k10d")

_exec_tracker: list[tuple] = []


def _cleanup_zombie_execs():
    alive = []
    for t, started, preview in _exec_tracker:
        if t.is_alive():
            alive.append((t, started, preview))
    _exec_tracker.clear()
    _exec_tracker.extend(alive[-50:])


def register(registry, ctx) -> None:
    soul = ctx_get(ctx, "soul")
    save_soul = ctx_get(ctx, "save_soul")
    load_notes = ctx_get(ctx, "load_notes")
    save_notes = ctx_get(ctx, "save_notes")
    append_episode = ctx_get(ctx, "append_episode")
    read_episodes = ctx_get(ctx, "read_episodes")
    WORKSPACE = ctx_get(ctx, "WORKSPACE")
    hypotheses = ctx_get(ctx, "hypotheses")
    engine = ctx_get(ctx, "engine")
    dream = ctx_get(ctx, "dream")
    memetic = ctx_get(ctx, "memetic")
    prober = ctx_get(ctx, "prober")
    selfmod = ctx_get(ctx, "selfmod")
    chrono = ctx_get(ctx, "chrono")
    hermes_bridge = ctx_get(ctx, "hermes_bridge")
    reg = ctx_get(ctx, "registry")
    _start_time = ctx_get(ctx, "_start_time")
    goals = ctx_get(ctx, "goals")

    def exec_python(args: dict) -> str:
        code = textwrap.dedent(args["code"])
        timeout = min(int(args.get("timeout", 30)), 120)
        stdout_buf, stderr_buf = io.StringIO(), io.StringIO()

        import copy

        _stop = threading.Event()
        soul_snapshot = copy.deepcopy(soul)

        def _save_soul_stub(s):
            return None

        class _ReadOnlyProxy:
            def __init__(self, obj):
                self._obj = obj

            def __getattr__(self, name):
                val = getattr(self._obj, name)
                if callable(val):

                    def _stub(*a, **kw):
                        raise RuntimeError(
                            "exec.python cannot call engine methods directly — use tool calls"
                        )

                    return _stub
                if isinstance(val, (dict, list)):
                    return copy.deepcopy(val)
                return val

            def __setattr__(self, name, value):
                if name == "_obj":
                    object.__setattr__(self, name, value)
                else:
                    raise RuntimeError(
                        "exec.python cannot mutate engine objects directly — use tool calls"
                    )

        g = {
            "soul": soul_snapshot,
            "save_soul": _save_soul_stub,
            "load_notes": load_notes,
            "save_notes": save_notes,
            "append_episode": append_episode,
            "read_episodes": read_episodes,
            "WORKSPACE": WORKSPACE,
            "hypotheses": hypotheses,
            "engine": _ReadOnlyProxy(engine),
            "dream": _ReadOnlyProxy(dream),
            "memetic": _ReadOnlyProxy(memetic),
            "prober": _ReadOnlyProxy(prober),
            "selfmod": _ReadOnlyProxy(selfmod),
            "json": json,
            "Path": Path,
            "datetime": datetime,
            "timezone": timezone,
            "time": time,
            "_k10_cancel": _stop,
        }
        g.update(build_namespace_proxies(reg, reg.dispatch))

        def _run() -> None:
            with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(
                stderr_buf
            ):
                if _stop.is_set():
                    return
                exec(compile(code, "<k10d-exec>", "exec"), g)

        t = threading.Thread(
            target=_run, daemon=True, name=f"k10d-exec-{int(time.time())}"
        )
        t.start()
        t.join(timeout=timeout)

        if t.is_alive():
            _stop.set()
            _cleanup_zombie_execs()
            _exec_tracker.append((t, time.time(), code[:80].replace("\n", " ")))
            return json.dumps(
                {
                    "ok": False,
                    "error": f"timed out after {timeout}s (thread continues detatched)",
                },
                indent=2,
            )

        _cleanup_zombie_execs()
        if _stop.is_set():
            return json.dumps(
                {
                    "stdout": stdout_buf.getvalue(),
                    "stderr": "execution cancelled by timeout",
                    "ok": False,
                },
                indent=2,
            )
        return json.dumps(
            {
                "stdout": stdout_buf.getvalue(),
                "stderr": stderr_buf.getvalue(),
                "ok": True,
            },
            indent=2,
        )

    def exec_shell(args: dict) -> str:
        timeout = min(int(args.get("timeout", 10)), 60)
        try:
            proc = subprocess.run(
                args["command"],
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return json.dumps(
                {
                    "stdout": proc.stdout,
                    "stderr": proc.stderr,
                    "returncode": proc.returncode,
                },
                indent=2,
            )
        except subprocess.TimeoutExpired:
            return json.dumps({"error": f"timed out after {timeout}s"})

    def system_info(args: dict) -> str:
        info = {
            "os": platform.platform(),
            "python": sys.version.split()[0],
            "uptime_seconds": round(time.time() - _start_time),
            "boot_count": soul["boot_count"],
            "hostname": platform.node(),
        }
        try:
            import psutil

            p = psutil.Process()
            info["rss_mb"] = round(p.memory_info().rss / 1024 / 1024, 1)
            info["cpu_percent"] = psutil.cpu_percent(interval=0.1)
        except ImportError:
            pass
        return json.dumps(info, indent=2)

    def system_time(args: dict) -> str:
        return json.dumps(
            {
                "utc": datetime.now(timezone.utc).isoformat(),
                "local": datetime.now().isoformat(),
                "timestamp": time.time(),
            }
        )

    def system_restart(args: dict) -> str:
        if not args.get("confirm") is True:
            return json.dumps(
                {
                    "ok": False,
                    "error": 'system.restart requires {"confirm": true} to proceed',
                }
            )
        log.info("Restarting process via os.execv...")

        def _exec():
            time.sleep(1)
            python = sys.executable
            os.execv(python, [python] + sys.argv)

        threading.Thread(target=_exec, daemon=True).start()
        return json.dumps({"ok": True, "message": "Restarting..."})

    def cron_schedule(args: dict) -> str:
        return json.dumps(
            chrono.schedule(args["id"], int(args["interval"]), args["code"])
        )

    def cron_list(args: dict) -> str:
        return json.dumps(chrono.list_tasks(), indent=2)

    def cron_remove(args: dict) -> str:
        return json.dumps(chrono.remove_task(args["id"]))

    def hermes_query(args: dict) -> str:
        timeout = min(int(args.get("timeout", 120)), 300)
        result = hermes_bridge.query(args["prompt"], timeout=timeout)
        return json.dumps(result, indent=2)

    def hermes_status(args: dict) -> str:
        return json.dumps(hermes_bridge.status(), indent=2)

    def hermes_cancel(args: dict) -> str:
        return json.dumps(hermes_bridge.cancel(), indent=2)

    def system_health(args: dict) -> str:
        _cleanup_zombie_execs()
        now = time.time()
        uptime = round(now - _start_time)
        threads = threading.enumerate()
        zombie_count = sum(1 for t, _, _ in _exec_tracker if t.is_alive())

        rss_mb = None
        try:
            import psutil

            rss_mb = round(psutil.Process().memory_info().rss / 1024 / 1024, 1)
        except ImportError:
            pass

        recent_eps = read_episodes(500)
        tool_eps = [e for e in recent_eps if e.get("source") == "tool"]
        errors_24h = 0
        for e in tool_eps:
            ts = e.get("ts")
            if ts:
                try:
                    et = datetime.fromisoformat(
                        str(ts).replace("Z", "+00:00")
                    ).timestamp()
                    if now - et < 86400:
                        detail = e.get("detail")
                        if isinstance(detail, dict) and (
                            detail.get("error") or detail.get("ok") is False
                        ):
                            errors_24h += 1
                except Exception:
                    pass

        warnings = []
        if zombie_count > 0:
            warnings.append(f"{zombie_count} zombie exec thread(s)")

        stalled_count = 0
        if goals:
            for g in goals.list_all():
                if g.get("status") == "stalled":
                    stalled_count += 1
                elif g.get("status") == "in_progress" and g.get("updated"):
                    try:
                        last = datetime.fromisoformat(
                            str(g["updated"]).replace("Z", "+00:00")
                        ).timestamp()
                        if now - last > 7200:
                            stalled_count += 1
                    except Exception:
                        pass
            if stalled_count > 0:
                warnings.append(f"{stalled_count} stalled goal(s)")

        if rss_mb and rss_mb > 500:
            warnings.append(f"High memory usage: {rss_mb}MB")

        disk_free_mb = None
        try:
            disk = shutil.disk_usage(str(WORKSPACE))
            disk_free_mb = round(disk.free / 1024 / 1024)
        except Exception:
            pass

        return json.dumps(
            {
                "uptime_seconds": uptime,
                "thread_count": len(threads),
                "exec_zombies": zombie_count,
                "rss_mb": rss_mb,
                "episode_count": len(recent_eps),
                "open_goals": len(goals.list_all("open")) if goals else 0,
                "stalled_goals": stalled_count,
                "tool_errors_24h": errors_24h,
                "hermes_available": hermes_bridge.available,
                "disk_free_mb": disk_free_mb,
                "warnings": warnings,
            },
            indent=2,
        )

    registry.register_from_def(
        tool(
            "exec.python",
            "Run Python on host.",
            {
                "code": _p("string", "Code"),
                "timeout": _p("integer", "Seconds (default 10)", required=False),
            },
        ),
        exec_python,
    )
    registry.register_from_def(
        tool(
            "exec.shell",
            "Run shell command.",
            {
                "command": _p("string", "Command"),
                "timeout": _p("integer", "Seconds (default 10)", required=False),
            },
        ),
        exec_shell,
    )
    registry.register_from_def(
        tool("system.info", "Host OS, Python, uptime."), system_info
    )
    registry.register_from_def(tool("system.time", "Current time."), system_time)
    registry.register_from_def(
        tool(
            "system.restart",
            "Restart the host process (requires confirm:true).",
            {"confirm": _p("boolean", "Must be true to proceed")},
        ),
        system_restart,
    )
    registry.register_from_def(
        tool(
            "cron.schedule",
            "Schedule a repeating task.",
            {
                "id": _p("string", "Unique task ID"),
                "interval": _p("integer", "Interval in seconds"),
                "code": _p("string", "Python code to run"),
            },
        ),
        cron_schedule,
    )
    registry.register_from_def(
        tool("cron.list", "List all scheduled tasks."), cron_list
    )
    registry.register_from_def(
        tool(
            "cron.remove",
            "Remove a scheduled task.",
            {"id": _p("string", "Task ID")},
        ),
        cron_remove,
    )
    registry.register_from_def(
        tool(
            "hermes.query",
            "Delegate a complex task or question to the local Hermes Agent.",
            {
                "prompt": _p("string", "The task or question for Hermes"),
                "timeout": _p(
                    "integer", "Timeout in seconds (default 120)", required=False
                ),
            },
        ),
        hermes_query,
    )
    registry.register_from_def(
        tool("hermes.status", "Hermes availability and in-flight query status."),
        hermes_status,
    )
    registry.register_from_def(
        tool("hermes.cancel", "Cancel the active Hermes query subprocess if any."),
        hermes_cancel,
    )
    registry.register_from_def(
        tool(
            "system.health",
            "Agent health snapshot: threads, memory, stalled goals, errors.",
        ),
        system_health,
    )
