"""device + sensor tools — honest host/device reporting."""

from __future__ import annotations

import json
import platform
from typing import Any

from .context import ctx_get
from .schema import tool




def _read_k10_sensors() -> dict:
    out: dict = {}
    try:
        from unihiker_k10 import temp_humi  # type: ignore
        th = temp_humi.read()
        out["environment"] = {
            "temperature": th.get("temperature"),
            "humidity": th.get("humidity"),
            "source": "unihiker_k10.temp_humi",
        }
    except Exception:
        pass
    try:
        from unihiker_k10 import light  # type: ignore
        out["light"] = {"lux": light.read().get("lux"), "source": "unihiker_k10.light"}
    except Exception:
        pass
    try:
        from unihiker_k10 import accel  # type: ignore
        a = accel.read()
        out["acceleration"] = {
            "x": a.get("x"), "y": a.get("y"), "z": a.get("z"),
            "source": "unihiker_k10.accel",
        }
    except Exception:
        pass
    return out


def register(registry, ctx) -> None:
    append_episode = ctx_get(ctx, "append_episode")
    _now_iso = ctx_get(ctx, "_now_iso")

    def get_device_status(args: dict) -> str:
        sensors = _read_k10_sensors()
        host_mode = not bool(sensors)
        status: dict = {
            "host_mode": host_mode,
            "platform": platform.system(),
            "node": platform.node(),
        }
        try:
            import psutil
            status["ram"] = {
                "free_mb": round(psutil.virtual_memory().available / 1024 / 1024, 1),
                "source": "psutil",
            }
        except Exception:
            status["ram"] = {"free_mb": None, "source": "unavailable"}

        if sensors:
            status.update(sensors)
            status["device"] = "k10"
        else:
            status["note"] = (
                "Gateway host — no K10 hardware drivers. "
                "Sensor fields omitted (not fabricated)."
            )
        return json.dumps(status, indent=2)

    def sensor_poll(args: dict) -> str:
        sensors = _read_k10_sensors()
        if not sensors:
            return json.dumps({
                "ok": False,
                "error": "No K10 sensor drivers on this host",
                "host_mode": True,
            })
        env = sensors.get("environment", {})
        light = sensors.get("light", {})
        accel = sensors.get("acceleration", {})
        return json.dumps({
            "ok": True,
            "temperature": env.get("temperature"),
            "humidity": env.get("humidity"),
            "light_lux": light.get("lux"),
            "accel": accel,
        }, indent=2)

    def sensorium_poll_and_log(args: dict) -> str:
        raw = sensor_poll({})
        data = json.loads(raw)
        if not data.get("ok"):
            append_episode({
                "ts": _now_iso(),
                "summary": "Sensorium poll (host — no hardware)",
                "detail": data,
            })
            return json.dumps({"ok": False, "logged": True, **data}, indent=2)
        append_episode({
            "ts": _now_iso(),
            "summary": "Sensorium poll",
            "detail": data,
        })
        return json.dumps({"ok": True, "logged": True, "sensors": data}, indent=2)

    registry.register_from_def(
        tool(
            "self.get_device_status",
            "Host or K10 device status. Omits fields that cannot be read (no fake defaults).",
            {},
        ),
        get_device_status,
    )
    registry.register_from_def(
        tool("sensor.poll", "Poll K10 sensors. Fails clearly on gateway host.", {}),
        sensor_poll,
    )
    registry.register_from_def(
        tool("sensorium.poll_and_log", "Poll sensors and log episode. Honest on host.", {}),
        sensorium_poll_and_log,
    )