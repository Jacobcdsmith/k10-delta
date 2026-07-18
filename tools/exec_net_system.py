"""Facade: register exec/system/cron/hermes + net + mqtt namespaces.

Implementation lives in exec_sys.py, net.py, and mqtt_ns.py.
"""

from __future__ import annotations

from . import exec_sys, mqtt_ns, net


def register(registry, ctx) -> None:
    exec_sys.register(registry, ctx)
    net.register(registry, ctx)
    mqtt_ns.register(registry, ctx)
