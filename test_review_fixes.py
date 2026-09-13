import json
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path

import dashboard
from selfmod import SelfModEngine
from tools.fs import resolve_path
from tools.registry import ToolRegistry


class TestReviewFixes(unittest.TestCase):

    def test_fs_resolve_blocks_paths_outside_allowed_roots(self):
        with tempfile.TemporaryDirectory() as d:
            workspace = Path(d) / "workspace"
            workspace.mkdir()
            with self.assertRaises(PermissionError):
                resolve_path("/etc/passwd", workspace)

    def test_registry_requires_namespace_dot_verb_names(self):
        registry = ToolRegistry()
        with self.assertRaises(ValueError):
            registry.register("invalid", "desc", {"type": "object"}, lambda args: "{}")

    def test_commit_tool_does_not_execute_handler(self):
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / "marker.txt"
            registered = []
            engine = SelfModEngine(
                get_tools_fn=lambda: [{"name": "demo.echo", "description": "d", "inputSchema": {"type": "object"}}],
                get_handler_fn=lambda: None,
                register_tool_fn=lambda tool_def, handler: registered.append((tool_def, handler)),
            )
            proposed = engine.propose_tool(
                "demo.echo",
                "desc",
                {"type": "object", "properties": {}},
                "def handler(arguments):\n"
                f"    open({json.dumps(str(marker))}, 'w').write('ran')\n"
                "    return 'ok'\n",
            )
            self.assertTrue(proposed["ok"])
            committed = engine.commit_tool("demo.echo")
            self.assertTrue(committed["ok"])
            self.assertEqual(committed["status"], "live")
            self.assertFalse(marker.exists())
            self.assertEqual(len(registered), 1)

    def test_propose_tool_rejects_blocked_imports(self):
        engine = SelfModEngine(
            get_tools_fn=lambda: [],
            get_handler_fn=lambda: None,
            register_tool_fn=lambda tool_def, handler: None,
        )
        with self.assertRaises(ValueError):
            engine.propose_tool(
                "demo.safe",
                "desc",
                {"type": "object"},
                "import subprocess\n\ndef handler(arguments):\n    return 'ok'\n",
            )

    def test_dashboard_lite_payload_does_not_depend_on_full_status_builder(self):
        original_ctx = dashboard._ctx
        original_builder = dashboard._build_status_payload
        try:
            dashboard._ctx = dashboard.DashboardContext()
            dashboard._ctx.soul = {
                "boot_count": 3,
                "semantic": {
                    "trajectory": {"label": "stable"},
                    "tool_usage": {"error_tools": ["net.search"]},
                },
                "autonomy": {
                    "last_intention": {"text": "ping", "kind": "act"},
                },
            }
            dashboard._ctx.read_episodes = lambda n: [{"i": 1}, {"i": 2}]
            dashboard._ctx.goals = type(
                "GoalsStub",
                (),
                {"list_all": lambda self, status=None: [1] if status == "open" else []},
            )()
            dashboard._build_status_payload = lambda: (_ for _ in ()).throw(AssertionError("unused"))
            payload = dashboard._build_lite_status_payload()
            self.assertEqual(payload["boot_count"], 3)
            self.assertEqual(payload["episode_count"], 2)
            self.assertEqual(payload["active_goals"], 1)
            self.assertEqual(payload["trajectory"]["label"], "stable")
            self.assertEqual(payload["tool_usage"]["error_tool_count"], 1)
        finally:
            dashboard._ctx = original_ctx
            dashboard._build_status_payload = original_builder

    def test_dashboard_lite_endpoint_returns_lightweight_payload(self):
        original_ctx = dashboard._ctx
        original_host = os.environ.get("K10_DASHBOARD_HOST")
        dashboard.stop_dashboard()
        try:
            os.environ["K10_DASHBOARD_HOST"] = "127.0.0.1"
            dashboard._ctx = dashboard.DashboardContext()
            dashboard._ctx.soul = {
                "boot_count": 7,
                "semantic": {
                    "trajectory": {"label": "ready"},
                    "tool_usage": {"error_tools": ["sensor.poll", "net.search"]},
                },
            }
            dashboard._ctx.read_episodes = lambda n: [{"i": 1}]
            dashboard._ctx.goals = type(
                "GoalsStub",
                (),
                {
                    "list_all": lambda self, status=None: (
                        [1] if status == "open" else [1, 2] if status == "active" else []
                    )
                },
            )()
            url = dashboard.start_dashboard(8765)
            with urllib.request.urlopen(f"{url}/api/status/lite", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["boot_count"], 7)
            self.assertEqual(payload["episode_count"], 1)
            self.assertEqual(payload["active_goals"], 3)
            self.assertEqual(payload["trajectory"]["label"], "ready")
            self.assertEqual(payload["tool_usage"]["error_tool_count"], 2)
        finally:
            dashboard.stop_dashboard()
            if original_host is None:
                os.environ.pop("K10_DASHBOARD_HOST", None)
            else:
                os.environ["K10_DASHBOARD_HOST"] = original_host
            dashboard._ctx = original_ctx


if __name__ == "__main__":
    unittest.main()
