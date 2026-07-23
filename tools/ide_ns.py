"""
ide.* tool namespace — IDE-like capabilities for the agent.

Tools:
  ide.tree            File tree of a directory
  ide.search_symbols  Grep for function/class/variable definitions
  ide.find_refs       Find all uses of a symbol across files
  ide.lint            Run ruff or py_compile on a file
  ide.test            Run pytest with structured output
  ide.git_diff        Show git diff (staged, unstaged, or by file)
  ide.git_log         Commit history
  ide.git_blame       Blame a file
  ide.git_status      Working tree status
  ide.refactor_rename Rename a symbol across files (sed-based)
  ide.docstring       Extract docstring for a named symbol
  ide.profile         Time-profile execution of a Python expression
  ide.coverage        Run pytest with coverage report
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from schema import _p, tool


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 30) -> dict:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=cwd, timeout=timeout,
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout[:8000],
            "stderr": result.stderr[:2000],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Timeout after {timeout}s"}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}


def _resolve(path: str) -> Path:
    """Resolve relative to cwd or as absolute."""
    p = Path(path)
    return p if p.is_absolute() else Path.cwd() / p


def register(registry, ctx) -> None:

    # ── File tree ─────────────────────────────────────────────────────────────

    def ide_tree(args: dict) -> str:
        root = _resolve(args.get("path", "."))
        max_depth = int(args.get("max_depth", 3))
        include = args.get("include", "")   # glob pattern filter
        lines = []

        def _walk(p: Path, depth: int, prefix: str = "") -> None:
            if depth > max_depth:
                return
            try:
                entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name))
            except PermissionError:
                return
            for i, entry in enumerate(entries):
                if entry.name.startswith(".") and not args.get("hidden"):
                    continue
                if entry.name in ("__pycache__", ".git", "node_modules", ".venv", "venv"):
                    continue
                connector = "└── " if i == len(entries) - 1 else "├── "
                lines.append(f"{prefix}{connector}{entry.name}{'/' if entry.is_dir() else ''}")
                if entry.is_dir():
                    ext = "    " if i == len(entries) - 1 else "│   "
                    _walk(entry, depth + 1, prefix + ext)

        lines.append(str(root))
        _walk(root, 1)
        if len(lines) > 500:
            lines = lines[:500] + [f"... ({len(lines)-500} more lines truncated)"]
        return json.dumps({"ok": True, "tree": "\n".join(lines)})

    # ── Symbol search ─────────────────────────────────────────────────────────

    def ide_search_symbols(args: dict) -> str:
        query = args["query"]
        root = str(_resolve(args.get("path", ".")))
        ext = args.get("ext", "py")
        kind = args.get("kind", "")  # def|class|var|any
        results = []

        patterns = []
        if kind in ("def", "any", ""):
            patterns.append(rf"^\s*def\s+{re.escape(query)}\b")
        if kind in ("class", "any", ""):
            patterns.append(rf"^\s*class\s+{re.escape(query)}\b")
        if kind in ("var", "any", ""):
            patterns.append(rf"^\s*{re.escape(query)}\s*=")
        if not patterns:
            patterns = [re.escape(query)]

        combined = "|".join(f"({p})" for p in patterns)
        r = _run(["python", "-m", "rg", combined, root, f"--glob=*.{ext}", "--line-number",
                  "--with-filename", "--no-heading"], timeout=15)
        # Fall back to grep if rg not available
        if not r["ok"] and "error" in r:
            r = _run(["grep", "-rn", "--include", f"*.{ext}",
                      "-E", combined, root], timeout=15)

        return json.dumps({"ok": True, "matches": r.get("stdout", "")[:4000]})

    # ── Find references ───────────────────────────────────────────────────────

    def ide_find_refs(args: dict) -> str:
        symbol = args["symbol"]
        root = str(_resolve(args.get("path", ".")))
        ext = args.get("ext", "py")
        r = _run(["grep", "-rn", "--include", f"*.{ext}",
                  "-w", symbol, root], timeout=15)
        return json.dumps({
            "ok": True,
            "symbol": symbol,
            "matches": r.get("stdout", "")[:4000],
            "count": r.get("stdout", "").count("\n"),
        })

    # ── Linting ───────────────────────────────────────────────────────────────

    def ide_lint(args: dict) -> str:
        path = str(_resolve(args["path"]))
        # Try ruff first, fall back to py_compile
        r = _run([sys.executable, "-m", "ruff", "check", path, "--output-format=json"])
        if r["ok"] or r.get("returncode") == 1:  # ruff exits 1 on violations
            return json.dumps({"ok": True, "tool": "ruff", "output": r.get("stdout", "")[:4000]})
        # Fallback: syntax check only
        import py_compile
        try:
            py_compile.compile(path, doraise=True)
            return json.dumps({"ok": True, "tool": "py_compile", "output": "No syntax errors"})
        except py_compile.PyCompileError as e:
            return json.dumps({"ok": False, "tool": "py_compile", "error": str(e)[:500]})


    # ── Testing ───────────────────────────────────────────────────────────────

    def ide_test(args: dict) -> str:
        path = args.get("path", ".")
        extra = args.get("args", "")
        cmd = [sys.executable, "-m", "pytest", path, "-v", "--tb=short",
               "--no-header", "-q"]
        if extra:
            cmd += extra.split()
        r = _run(cmd, timeout=60)
        return json.dumps({
            "ok": r["returncode"] == 0,
            "returncode": r["returncode"],
            "output": (r.get("stdout", "") + r.get("stderr", ""))[:6000],
        })

    def ide_coverage(args: dict) -> str:
        path = args.get("path", ".")
        cmd = [sys.executable, "-m", "pytest", path, "--cov", ".", "--cov-report=term-missing",
               "--no-header", "-q"]
        r = _run(cmd, timeout=90)
        return json.dumps({
            "ok": r["returncode"] == 0,
            "output": (r.get("stdout", "") + r.get("stderr", ""))[:6000],
        })

    # ── Git ───────────────────────────────────────────────────────────────────

    def ide_git_diff(args: dict) -> str:
        path = str(_resolve(args.get("path", ".")))
        file_ = args.get("file", "")
        staged = args.get("staged", False)
        cmd = ["git", "-C", path, "diff"]
        if staged:
            cmd.append("--staged")
        if file_:
            cmd += ["--", file_]
        r = _run(cmd, timeout=10)
        return json.dumps({"ok": r["ok"], "diff": r.get("stdout", "")[:8000]})

    def ide_git_log(args: dict) -> str:
        path = str(_resolve(args.get("path", ".")))
        n = int(args.get("n", 20))
        file_ = args.get("file", "")
        cmd = ["git", "-C", path, "log", f"-{n}",
               "--pretty=format:%h %as %an: %s"]
        if file_:
            cmd += ["--", file_]
        r = _run(cmd, timeout=10)
        return json.dumps({"ok": r["ok"], "log": r.get("stdout", "")[:4000]})

    def ide_git_blame(args: dict) -> str:
        path_arg = args["file"]
        p = _resolve(path_arg)
        r = _run(["git", "blame", "--porcelain", str(p)], cwd=str(p.parent), timeout=10)
        # Parse porcelain into compact form
        lines = r.get("stdout", "").splitlines()
        summary = []
        current = {}
        for line in lines:
            if line.startswith("\t"):
                if current:
                    summary.append(f"{current.get('hash','?')[:8]} {current.get('author','?')}: {line[1:]}")
            elif " " in line:
                parts = line.split(" ", 1)
                if len(parts[0]) == 40:
                    current = {"hash": parts[0]}
                elif line.startswith("author "):
                    current["author"] = line[7:]
        return json.dumps({"ok": r["ok"], "blame": "\n".join(summary[:100])})

    def ide_git_status(args: dict) -> str:
        path = str(_resolve(args.get("path", ".")))
        r = _run(["git", "-C", path, "status", "--short"], timeout=10)
        return json.dumps({"ok": r["ok"], "status": r.get("stdout", "")[:2000]})

    # ── Refactoring ───────────────────────────────────────────────────────────

    def ide_refactor_rename(args: dict) -> str:
        old_name = args["old_name"]
        new_name = args["new_name"]
        root = str(_resolve(args.get("path", ".")))
        ext = args.get("ext", "py")
        dry_run = bool(args.get("dry_run", True))  # default safe

        # Use Python to do the rename (avoids sed/powershell compat issues)
        changed = []
        for fpath in Path(root).rglob(f"*.{ext}"):
            if any(p in str(fpath) for p in ("__pycache__", ".venv", ".git")):
                continue
            try:
                src = fpath.read_text(encoding="utf-8")
                # Word-boundary replacement
                new_src = re.sub(rf"\b{re.escape(old_name)}\b", new_name, src)
                if new_src != src:
                    changed.append(str(fpath))
                    if not dry_run:
                        fpath.write_text(new_src, encoding="utf-8")
            except Exception:
                continue

        return json.dumps({
            "ok": True,
            "dry_run": dry_run,
            "changed_files": changed,
            "count": len(changed),
            "note": "Set dry_run=false to apply changes" if dry_run else "Changes applied",
        })

    # ── Docstring ─────────────────────────────────────────────────────────────

    def ide_docstring(args: dict) -> str:
        file_path = str(_resolve(args["file"]))
        symbol = args["symbol"]
        try:
            src = Path(file_path).read_text(encoding="utf-8")
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name == symbol:
                        doc = ast.get_docstring(node) or ""
                        sig_lines = src.splitlines()[node.lineno - 1:node.lineno + 2]
                        return json.dumps({
                            "ok": True, "symbol": symbol,
                            "signature": "\n".join(sig_lines),
                            "docstring": doc,
                            "line": node.lineno,
                        })
            return json.dumps({"ok": False, "error": f"Symbol '{symbol}' not found"})
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)[:200]})

    # ── Profiling ─────────────────────────────────────────────────────────────

    def ide_profile(args: dict) -> str:
        expr = args["expression"]
        n = int(args.get("n", 100))
        try:
            import timeit
            elapsed = timeit.timeit(expr, number=n, globals={})
            return json.dumps({
                "ok": True,
                "expression": expr,
                "runs": n,
                "total_s": round(elapsed, 6),
                "per_run_us": round(elapsed / n * 1e6, 3),
            })
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)[:300]})


    # ── Registration ──────────────────────────────────────────────────────────

    registry.register_from_def(
        tool("ide.tree", "Print a file tree of a directory.",
             {"path": _p("string", "Directory path (default .)", required=False),
              "max_depth": _p("integer", "Max depth (default 3)", required=False),
              "hidden": _p("boolean", "Include hidden files", required=False)}),
        ide_tree,
    )
    registry.register_from_def(
        tool("ide.search_symbols", "Find function/class/variable definitions by name.",
             {"query": _p("string", "Symbol name (exact or partial)"),
              "path": _p("string", "Root dir (default .)", required=False),
              "kind": _p("string", "def|class|var|any (default any)", required=False),
              "ext": _p("string", "File extension (default py)", required=False)}),
        ide_search_symbols,
    )
    registry.register_from_def(
        tool("ide.find_refs", "Find all uses of a symbol across files.",
             {"symbol": _p("string", "Symbol name"),
              "path": _p("string", "Root dir (default .)", required=False),
              "ext": _p("string", "File extension (default py)", required=False)}),
        ide_find_refs,
    )
    registry.register_from_def(
        tool("ide.lint", "Lint a Python file with ruff (or py_compile fallback).",
             {"path": _p("string", "File path")}),
        ide_lint,
    )
    registry.register_from_def(
        tool("ide.test", "Run pytest on a path with structured output.",
             {"path": _p("string", "File or directory (default .)", required=False),
              "args": _p("string", "Extra pytest args as a string", required=False)}),
        ide_test,
    )
    registry.register_from_def(
        tool("ide.coverage", "Run pytest with coverage report.",
             {"path": _p("string", "File or directory (default .)", required=False)}),
        ide_coverage,
    )
    registry.register_from_def(
        tool("ide.git_diff", "Show git diff.",
             {"path": _p("string", "Repo dir (default .)", required=False),
              "file": _p("string", "Specific file", required=False),
              "staged": _p("boolean", "Show staged diff", required=False)}),
        ide_git_diff,
    )
    registry.register_from_def(
        tool("ide.git_log", "Show git commit history.",
             {"path": _p("string", "Repo dir (default .)", required=False),
              "n": _p("integer", "Number of commits (default 20)", required=False),
              "file": _p("string", "Filter by file", required=False)}),
        ide_git_log,
    )
    registry.register_from_def(
        tool("ide.git_blame", "Show git blame for a file.",
             {"file": _p("string", "File path")}),
        ide_git_blame,
    )
    registry.register_from_def(
        tool("ide.git_status", "Show working tree status.",
             {"path": _p("string", "Repo dir (default .)", required=False)}),
        ide_git_status,
    )
    registry.register_from_def(
        tool("ide.refactor_rename", "Rename a symbol across all files (word-boundary safe).",
             {"old_name": _p("string", "Current symbol name"),
              "new_name": _p("string", "New symbol name"),
              "path": _p("string", "Root dir (default .)", required=False),
              "ext": _p("string", "File extension (default py)", required=False),
              "dry_run": _p("boolean", "Preview only without writing (default true)", required=False)}),
        ide_refactor_rename,
    )
    registry.register_from_def(
        tool("ide.docstring", "Extract docstring and signature for a named symbol.",
             {"file": _p("string", "Python file path"),
              "symbol": _p("string", "Function or class name")}),
        ide_docstring,
    )
    registry.register_from_def(
        tool("ide.profile", "Time-profile a Python expression.",
             {"expression": _p("string", "Python expression to time"),
              "n": _p("integer", "Number of runs (default 100)", required=False)}),
        ide_profile,
    )
