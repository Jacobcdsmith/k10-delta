"""Facade: register exec/system/cron/hermes + net + mqtt + github namespaces.

Implementation lives in exec_sys.py, net.py, mqtt_ns.py, and github_ns.py.
"""

from __future__ import annotations

from . import exec_sys, github_ns, mqtt_ns, net


def register(registry, ctx) -> None:
    exec_sys.register(registry, ctx)
    net.register(registry, ctx)
    mqtt_ns.register(registry, ctx)
    github_ns.register(registry, ctx)
