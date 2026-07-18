"""opencode.api.* — OpenCode API integration for chat completion and code generation."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .context import ctx_get
from .schema import _p, tool




def _get_opencode_path() -> str:
    """Resolve the OpenCode binary path."""
    # Try common locations
    candidates = [
        "opencode",
        os.path.expanduser("~/.opencode/bin/opencode"),
        os.path.expanduser("~/AppData/Local/opencode/bin/opencode.exe"),
        os.path.expanduser("~/AppData/Roaming/npm/opencode.cmd"),
        "C:/Users/Public/opencode/bin/opencode.exe",
    ]
    for c in candidates:
        try:
            result = subprocess.run([c, "--version"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return c
        except Exception:
            continue
    return "opencode"  # fallback to PATH


def _run_opencode(args: list[str], workdir: str | None = None, timeout: int = 120) -> str:
    """Run OpenCode CLI and return stdout as JSON string."""
    bin_path = _get_opencode_path()
    cmd = [bin_path] + args
    try:
        result = subprocess.run(
            cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "OPENCODE_NON_INTERACTIVE": "1"},
        )
        if result.returncode != 0:
            return json.dumps({"ok": False, "error": result.stderr.strip() or result.stdout.strip()})
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "error": f"OpenCode timed out after {timeout}s"})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})


def _run_opencode_stream(args: list[str], workdir: str | None = None) -> str:
    """Run OpenCode with streaming output for interactive sessions."""
    bin_path = _get_opencode_path()
    cmd = [bin_path] + args
    try:
        # Use Popen for streaming
        proc = subprocess.Popen(
            cmd,
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env={**os.environ, "OPENCODE_NON_INTERACTIVE": "1"},
        )
        stdout, stderr = proc.communicate(timeout=180)
        if proc.returncode != 0:
            return json.dumps({"ok": False, "error": stderr.strip() or stdout.strip()})
        return stdout.strip()
    except subprocess.TimeoutExpired:
        proc.kill()
        return json.dumps({"ok": False, "error": "OpenCode streaming timed out"})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})


def register(registry, ctx) -> None:
    """Register OpenCode API tools."""
    call_tool = ctx_get(ctx, "call_tool")

    def opencode_chat_complete(args: dict) -> str:
        """Chat completion via OpenCode (non-interactive run)."""
        prompt = args["prompt"]
        model = args.get("model")
        files = args.get("files", [])
        thinking = args.get("thinking", False)
        system = args.get("system")
        timeout = min(int(args.get("timeout", 120)), 300)
        workdir = args.get("workdir")

        # Build the prompt with optional system message
        full_prompt = prompt
        if system:
            full_prompt = f"System: {system}\n\nUser: {prompt}"

        # Create temp file for context files if provided
        file_args = []
        if files:
            for f in files:
                file_args.extend(["-f", f])

        args_list = ["run"]
        if model:
            args_list.extend(["--model", model])
        if thinking:
            args_list.append("--thinking")
        args_list.extend(file_args)
        args_list.append(full_prompt)

        return _run_opencode(args_list, workdir=workdir, timeout=timeout)

    def opencode_code_generate(args: dict) -> str:
        """Code generation via OpenCode (non-interactive run)."""
        prompt = args["prompt"]
        model = args.get("model")
        files = args.get("files", [])
        thinking = args.get("thinking", True)
        workdir = args.get("workdir")
        timeout = min(int(args.get("timeout", 180)), 600)

        file_args = []
        if files:
            for f in files:
                file_args.extend(["-f", f])

        args_list = ["run"]
        if model:
            args_list.extend(["--model", model])
        if thinking:
            args_list.append("--thinking")
        args_list.extend(file_args)
        args_list.append(prompt)

        return _run_opencode(args_list, workdir=workdir, timeout=timeout)

    def opencode_code_review(args: dict) -> str:
        """Code review via OpenCode."""
        prompt = args["prompt"]
        files = args.get("files", [])
        model = args.get("model")
        workdir = args.get("workdir")
        timeout = min(int(args.get("timeout", 180)), 600)

        file_args = []
        if files:
            for f in files:
                file_args.extend(["-f", f])

        args_list = ["run"]
        if model:
            args_list.extend(["--model", model])
        args_list.append("--thinking")
        args_list.extend(file_args)
        args_list.append(prompt)

        return _run_opencode(args_list, workdir=workdir, timeout=timeout)

    def opencode_list_models(args: dict) -> str:
        """List available OpenCode models."""
        result = _run_opencode(["models", "list"])
        return result

    def opencode_auth_status(args: dict) -> str:
        """Check OpenCode auth status."""
        result = _run_opencode(["auth", "list"])
        return result

    def opencode_session_start(args: dict) -> str:
        """Start an interactive OpenCode session (background)."""
        workdir = args.get("workdir")
        model = args.get("model")
        session_id = args.get("session_id")

        args_list = []
        if model:
            args_list.extend(["--model", model])

        bin_path = _get_opencode_path()
        cmd = [bin_path] + args_list

        try:
            import uuid
            if not session_id:
                session_id = str(uuid.uuid4())[:8]

            proc = subprocess.Popen(
                cmd,
                cwd=workdir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env={**os.environ, "OPENCODE_NON_INTERACTIVE": "1"},
            )

            # Store process reference for later interaction
            # Note: In a real implementation, you'd store this in a session manager
            return json.dumps({
                "ok": True,
                "session_id": session_id,
                "message": "Session started. Use opencode.session_send to send prompts.",
                "pid": proc.pid,
            })
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)})

    def opencode_session_send(args: dict) -> str:
        """Send a prompt to a background OpenCode session."""
        # This is a placeholder - real implementation needs session tracking
        session_id = args.get("session_id")
        prompt = args.get("prompt")
        return json.dumps({
            "ok": False,
            "error": "Session management not fully implemented. Use opencode.run for one-shot tasks."
        })

    def opencode_auth_login(args: dict) -> str:
        """Login to OpenCode provider."""
        provider = args.get("provider")
        args_list = ["auth", "login"]
        if provider:
            args_list.extend(["--provider", provider])
        return _run_opencode(args_list)

    # Register tools
    registry.register_from_def(
        tool(
            "opencode.chat_complete",
            "Non-interactive chat completion via OpenCode run command.",
            {
                "prompt": _p("string", "User prompt/message"),
                "model": _p("string", "Model override (e.g., openrouter/anthropic/claude-sonnet-4)", required=False),
                "files": _p("array", "Context files to attach (-f)", required=False),
                "thinking": _p("boolean", "Show model thinking (--thinking)", required=False),
                "system": _p("string", "System prompt prefix", required=False),
                "timeout": _p("integer", "Timeout seconds (max 300)", required=False),
                "workdir": _p("string", "Working directory", required=False),
            },
        ),
        opencode_chat_complete,
    )

    registry.register_from_def(
        tool(
            "opencode.code_generate",
            "Code generation via OpenCode run command with thinking enabled by default.",
            {
                "prompt": _p("string", "Code generation prompt"),
                "model": _p("string", "Model override", required=False),
                "files": _p("array", "Context files to attach", required=False),
                "thinking": _p("boolean", "Enable thinking mode (default true)", required=False),
                "timeout": _p("integer", "Timeout seconds (max 600)", required=False),
                "workdir": _p("string", "Working directory", required=False),
            },
        ),
        opencode_code_generate,
    )

    registry.register_from_def(
        tool(
            "opencode.code_review",
            "Code review via OpenCode with thinking enabled.",
            {
                "prompt": _p("string", "Review prompt/instructions"),
                "files": _p("array", "Files to review", required=False),
                "model": _p("string", "Model override", required=False),
                "timeout": _p("integer", "Timeout seconds (max 600)", required=False),
                "workdir": _p("string", "Working directory", required=False),
            },
        ),
        opencode_code_review,
    )

    registry.register_from_def(
        tool(
            "opencode.models_list",
            "List available OpenCode models/providers.",
            {},
        ),
        opencode_list_models,
    )

    registry.register_from_def(
        tool(
            "opencode.auth_status",
            "Check OpenCode authentication status.",
            {},
        ),
        opencode_auth_status,
    )

    registry.register_from_def(
        tool(
            "opencode.auth_login",
            "Login to OpenCode provider.",
            {
                "provider": _p("string", "Provider name (openrouter, anthropic, openai, etc.)", required=False),
            },
        ),
        opencode_auth_login,
    )