"""
K10-Δ Self-Modification Engine

Allows the agent to read, understand, patch, and hot-extend its own source.

  read_source(path)          — read any file on the filesystem
  list_sources()             — list all source files + sizes + line counts
  validate_python(code)      — compile-check without executing
  backup_source(path)        — snapshot to soul_history/ before any write
  patch_source(path,old,new) — replace text in source file (with backup)
  propose_tool(spec)         — validate + stage a new tool definition
  commit_tool(name)          — hot-load a staged tool into the running process
  list_staged()              — list staged (uncommitted) tools
  get_tool_schema(name)      — return schema for any registered tool
  introspect_self()          — full self-portrait: sources, tools, engines, state
"""

import ast
import json
import os
import shutil
import threading
import logging
import libcst as cst
from datetime import datetime, timezone
from pathlib import Path

from tools.handler_adapter import adapt_handler

log = logging.getLogger("k10d.selfmod")

K10_DIR      = Path(__file__).parent.resolve()
HISTORY_DIR  = K10_DIR / "soul_history"
STAGED_PATH  = K10_DIR / "staged_tools.json"
HISTORY_DIR.mkdir(exist_ok=True)

_BLOCKED_HANDLER_MODULES = frozenset({
    "os", "subprocess", "shutil", "ctypes", "socket", "http",
    "pickle", "marshal", "code", "codeop",
})

_WRITE_EXTENSIONS = frozenset({".py", ".json", ".txt", ".md", ".jsonl"})
_READ_EXTENSIONS  = frozenset({".py", ".json", ".txt", ".md", ".jsonl"})

RESTRICTED_BUILTINS = {
    "print": print, "len": len, "str": str, "int": int, "float": float,
    "bool": bool, "list": list, "dict": dict, "set": set, "tuple": tuple,
    "range": range, "enumerate": enumerate, "zip": zip, "map": map,
    "filter": filter, "sorted": sorted, "reversed": reversed,
    "min": min, "max": max, "sum": sum, "any": any, "all": all,
    "abs": abs, "round": round, "isinstance": isinstance, "type": type,
    "hasattr": hasattr, "getattr": getattr,
    "Exception": Exception, "ValueError": ValueError, "KeyError": KeyError,
    "TypeError": TypeError, "RuntimeError": RuntimeError, "json": json,
    "__import__": __import__, "open": open,
}

_FORBIDDEN_STRING_REFS = frozenset({
    "__builtins__", "__loader__", "__spec__",
    "exec(", "eval(", "compile(",
})


# ── Helpers ────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _safe_source_path(rel: str) -> Path:
    resolved = Path(rel).resolve()
    try:
        if resolved.is_symlink():
            resolved = resolved.readlink().resolve()
    except (OSError, NotImplementedError):
        pass
    if not (str(resolved).startswith(str(K10_DIR) + os.sep)
            or resolved == K10_DIR or K10_DIR in resolved.parents):
        raise ValueError(f"Path outside K10_DIR: {rel}")
    return resolved


def _check_source_extension(path: Path, allowed: frozenset) -> None:
    suffix = path.suffix.lower()
    if suffix not in allowed:
        raise ValueError(
            f"File extension '{suffix}' not allowed for this operation "
            f"(allowed: {sorted(allowed)})")


def _is_sys_modules_access(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "sys"
            and node.attr == "modules")


def _scan_handler_safety(code: str) -> list[str]:
    """AST scan for dangerous imports and sys.modules manipulation in handler code."""
    tree = ast.parse(code)
    issues: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _BLOCKED_HANDLER_MODULES:
                    issues.append(f"blocked import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                if root in _BLOCKED_HANDLER_MODULES:
                    issues.append(f"blocked import: {node.module}")
        elif isinstance(node, ast.Subscript) and _is_sys_modules_access(node.value):
            issues.append("sys.modules manipulation")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and _is_sys_modules_access(target.value):
                    issues.append("sys.modules manipulation")
                elif _is_sys_modules_access(target):
                    issues.append("sys.modules manipulation")
    for forbidden in _FORBIDDEN_STRING_REFS:
        if forbidden in code:
            issues.append(f"forbidden reference: {forbidden}")
    return issues


# ── SelfModEngine ──────────────────────────────────────────────────────────────

class FunctionReplacementTransformer(cst.CSTTransformer):
    def __init__(self, target_name: str, new_node: cst.FunctionDef):
        self.target_name = target_name
        self.new_node = new_node
        self.found = False

    def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.FunctionDef:
        if original_node.name.value == self.target_name:
            self.found = True
            # Preserve leading lines (blank lines and comments immediately before the function)
            return self.new_node.with_changes(leading_lines=original_node.leading_lines)
        return updated_node

class SelfModEngine:
    def __init__(self, get_tools_fn, get_handler_fn, register_tool_fn, call_tool_fn=None):
        """
        get_tools_fn()           → list[dict]   (current TOOLS)
        get_handler_fn()         → callable     (current handle_tool)
        register_tool_fn(t, fn)  → None         (adds tool + handler at runtime)
        """
        self._get_tools    = get_tools_fn
        self._get_handler  = get_handler_fn
        self._register     = register_tool_fn
        self._call_tool    = call_tool_fn # New: for tools to call other tools

        self._lock         = threading.Lock()
        self._staged: dict[str, dict] = self._load_staged()
        self._revoke_hooks: list = []

    # ── Staged tools persistence ───────────────────────────────────────────────

    def _load_staged(self) -> dict:
        if STAGED_PATH.exists():
            try:
                return json.loads(STAGED_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save_staged(self):
        STAGED_PATH.write_text(
            json.dumps(self._staged, indent=2), encoding="utf-8")

    def _compile_handler(self, spec: dict):
        ns: dict = {}
        ns["__builtins__"] = RESTRICTED_BUILTINS
        if self._call_tool:
            ns["_call_tool"] = self._call_tool
        exec(compile(spec["handler_code"], f"<tool:{spec['name']}>", "exec"), ns)
        return adapt_handler(ns["handler"], spec.get("inputSchema"))

    # ── Source reading ─────────────────────────────────────────────────────────

    def read_source(self, rel: str) -> str:
        path = _safe_source_path(rel)
        _check_source_extension(path, _READ_EXTENSIONS)
        return path.read_text(encoding="utf-8", errors="replace")

    def list_sources(self) -> list[dict]:
        entries = []
        for p in sorted(K10_DIR.iterdir()):
            if p.suffix in (".py", ".json", ".jsonl", ".md", ".txt") and p.is_file():
                try:
                    _safe_source_path(str(p))
                except ValueError:
                    continue
                text  = p.read_text(encoding="utf-8", errors="replace")
                lines = text.count("\n")
                entries.append({
                    "file":  p.name,
                    "size":  p.stat().st_size,
                    "lines": lines,
                    "mtime": datetime.fromtimestamp(
                        p.stat().st_mtime, tz=timezone.utc).isoformat(),
                })
        return entries

    # ── Validation ─────────────────────────────────────────────────────────────

    def validate_python(self, code: str) -> dict:
        try:
            tree = ast.parse(code)
            return {
                "valid": True,
                "node_count": len(list(ast.walk(tree))),
                "top_level": [
                    type(n).__name__ for n in ast.iter_child_nodes(tree)
                ]
            }
        except SyntaxError as e:
            return {"valid": False, "error": str(e), "line": e.lineno}

    # ── Backup ─────────────────────────────────────────────────────────────────

    def backup_source(self, rel: str) -> str:
        src = _safe_source_path(rel)
        _check_source_extension(src, _READ_EXTENSIONS)
        if not src.exists():
            raise FileNotFoundError(rel)
        ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest    = HISTORY_DIR / f"{src.stem}_{ts}{src.suffix}"
        shutil.copy2(src, dest)
        log.info("Backed up %s → %s", src.name, dest.name)
        return str(dest.relative_to(K10_DIR))

    # ── Source patching ────────────────────────────────────────────────────────

    def patch_source(self, rel: str, old_str: str, new_str: str) -> dict:
        src  = _safe_source_path(rel)
        _check_source_extension(src, _WRITE_EXTENSIONS)
        text = src.read_text(encoding="utf-8")
        if old_str not in text:
            raise ValueError(f"old_str not found in {rel}")
        # Backup first
        backup = self.backup_source(rel)
        patched = text.replace(old_str, new_str, 1)
        # Validate if Python
        if src.suffix == ".py":
            v = self.validate_python(patched)
            if not v["valid"]:
                raise SyntaxError(f"Patch produces invalid Python: {v['error']}")
        src.write_text(patched, encoding="utf-8")
        log.info("Patched %s (backup: %s)", rel, backup)
        return {"ok": True, "file": rel, "backup": backup,
                "chars_changed": len(new_str) - len(old_str)}

    # ── Tool proposal & hot-loading ────────────────────────────────────────────

    def propose_tool(self, name: str, description: str,
                     schema: dict, handler_code: str, tier: str | None = None) -> dict:
        """
        Stage a new tool. handler_code must define a function named
        `handler(arguments: dict) -> str`.
        """
        if not name.replace(".", "_").replace("-", "_").isidentifier():
            raise ValueError(f"Invalid tool name: {name!r}")

        v = self.validate_python(handler_code)
        if not v["valid"]:
            raise SyntaxError(f"Handler code invalid: {v['error']}")

        # Check handler function exists
        tree  = ast.parse(handler_code)
        fnames = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        if "handler" not in fnames:
            raise ValueError("handler_code must define a function named 'handler'")

        # Check if the handler code uses the _call_tool function
        if "_call_tool" in handler_code and self._call_tool is None:
            raise ValueError("Handler code uses _call_tool but no call_tool_fn was provided to SelfModEngine")

        safety_issues = _scan_handler_safety(handler_code)
        if safety_issues:
            raise ValueError(
                "Handler code blocked by safety scan: " + "; ".join(safety_issues))

        with self._lock:
            spec = {
                "name":         name,
                "description":  description,
                "inputSchema":  schema,
                "handler_code": handler_code,
                "staged_at":    _now_iso(),
                "committed":    False,
            }
            if tier:
                spec["tier"] = tier
            self._staged[name] = spec
            self._save_staged()

        log.info("Tool staged: %s", name)
        return {"ok": True, "name": name, "status": "staged"}

    def commit_tool(self, name: str) -> dict:
        """Hot-load a staged tool into the running MCP server."""
        with self._lock:
            if name not in self._staged:
                raise KeyError(f"No staged tool: {name!r}")
            spec = self._staged[name]

        handler_fn = self._compile_handler(spec)
        tool_def = {
            "name":        spec["name"],
            "description": spec["description"],
            "inputSchema": spec["inputSchema"],
        }
        if "tier" in spec:
            tool_def["tier"] = spec["tier"]
        self._register(tool_def, handler_fn)

        with self._lock:
            self._staged[name]["committed"] = True
            self._save_staged()

        log.info("Tool committed (hot-loaded): %s", name)
        return {"ok": True, "name": name, "status": "live"}

    def list_staged(self) -> list[dict]:
        with self._lock:
            return [
                {k: v for k, v in s.items() if k != "handler_code"}
                for s in self._staged.values()
            ]

    def auto_load_staged(self, registry) -> dict:
        """Load committed staged tools into the registry, skipping already-registered names.
        On any error, rolls back newly-loaded tools to prevent a partially-loaded state."""
        loaded: list[str] = []
        skipped: list[str] = []
        errors: list[dict] = []
        rolled_back: list[str] = []

        with self._lock:
            staged = {k: dict(v) for k, v in self._staged.items()}

        snapshot = set(t["name"] for t in registry.all_tools)

        for name, spec in staged.items():
            if not spec.get("committed"):
                continue
            if registry.has(name):
                skipped.append(name)
                continue
            try:
                handler_fn = self._compile_handler(spec)
                tool_def = {
                    "name":        spec["name"],
                    "description": spec["description"],
                    "inputSchema": spec["inputSchema"],
                }
                if "tier" in spec:
                    tool_def["tier"] = spec["tier"]
                registry.register_from_def(tool_def, handler_fn, dynamic=True)
                loaded.append(name)
                log.info("Auto-loaded staged tool: %s", name)
            except Exception as e:
                errors.append({"name": name, "error": str(e)})
                log.warning("Failed to auto-load staged tool %s: %s", name, e)
                break

        if errors and loaded:
            for tool_name in loaded:
                try:
                    registry.unregister(tool_name)
                    rolled_back.append(tool_name)
                except Exception as unreg_err:
                    log.warning("Rollback unregister failed for %s: %s", tool_name, unreg_err)
            with self._lock:
                for tool_name in loaded:
                    if tool_name in self._staged:
                        self._staged[tool_name]["committed"] = False
                self._save_staged()
            log.warning("Rolled back %d tools after error in auto_load_staged: %s",
                        len(rolled_back), ", ".join(rolled_back))
            return {
                "loaded": [], "skipped": skipped, "errors": errors,
                "rolled_back": rolled_back,
            }

        return {"loaded": loaded, "skipped": skipped, "errors": errors, "rolled_back": []}

    def revoke_tool(self, name: str, unregister_fn=None) -> dict:
        """Revoke a hot-loaded tool. Registry unregister is delegated to callback/hooks."""
        if unregister_fn:
            unregister_fn(name)
        for hook in self._revoke_hooks:
            try:
                hook(name)
            except Exception as e:
                log.warning("revoke hook failed for %s: %s", name, e)

        with self._lock:
            if name in self._staged:
                self._staged[name]["committed"] = False
                self._save_staged()

        log.info("Tool revoked: %s", name)
        return {"ok": True, "name": name, "status": "revoked"}

    def add_revoke_hook(self, hook) -> None:
        self._revoke_hooks.append(hook)

    # ── Tool invocation ──────────────────────────────────────────────────────────

    def call_tool(self, name: str, args: dict) -> str:
        """Invoke a registered tool (used by cognition engine for skill introspection)."""
        if self._call_tool is None:
            raise ValueError("No call_tool_fn provided to SelfModEngine")
        return self._call_tool(name, args)

    # ── Schema introspection ───────────────────────────────────────────────────

    def get_tool_schema(self, name: str) -> dict:
        tools = self._get_tools()
        for t in tools:
            if t["name"] == name:
                return t
        raise KeyError(f"Tool not found: {name!r}")

    def introspect_self(self, soul: dict, engine_stats: dict,
                        registry=None) -> dict:
        tools   = self._get_tools()
        sources = self.list_sources()
        portrait = {
            "identity":    soul.get("identity", "K10-Δ"),
            "version":     "2.0",
            "boot_count":  soul.get("boot_count", 0),
            "tool_count":  len(tools),
            "tool_namespaces": sorted(set(t["name"].split(".")[0] for t in tools)),
            "source_files": sources,
            "staged_tools": len(self._staged),
            "engine_stats": engine_stats,
            "axiom_count":  len(soul.get("axioms", [])),
            "semantic_keys": list(soul.get("semantic", {}).keys()),
            "dream_facts":   len(soul.get("semantic", {}).get("dream_facts", [])),
            "introspected_at": _now_iso(),
        }
        if registry is not None:
            portrait["tool_index"] = registry.index()
        return portrait

    def ast_replace_function(self, rel: str, func_name: str, new_code: str) -> dict:
        """
        Semantically replace a function definition using LibCST.
        This preserves comments and formatting outside the target function.
        """
        src = _safe_source_path(rel)
        _check_source_extension(src, _WRITE_EXTENSIONS)
        if not src.exists():
            return {"success": False, "error": f"File not found: {rel}"}

        source_text = src.read_text(errors="replace")

        try:
            # Parse target file
            module = cst.parse_module(source_text)

            # Parse new function code
            new_func_module = cst.parse_module(new_code.strip())

            # Find the function definition in the new code
            new_func_node = None
            for node in new_func_module.body:
                if isinstance(node, cst.FunctionDef):
                    new_func_node = node
                    break

            if not new_func_node:
                return {"success": False, "error": "New code does not contain a valid function definition"}

            if new_func_node.name.value != func_name:
                return {"success": False, "error": f"Function name mismatch: new code defines '{new_func_node.name.value}' but target is '{func_name}'"}

            # Perform replacement
            transformer = FunctionReplacementTransformer(func_name, new_func_node)
            modified_module = module.visit(transformer)

            if not transformer.found:
                return {"success": False, "error": f"Function '{func_name}' not found in {rel}"}

            # Backup and write
            backup = self.backup_source(rel)
            src.write_text(modified_module.code)

            log.info("AST-replaced function '%s' in %s (backup: %s)", func_name, rel, backup)
            return {"success": True, "file": rel, "backup": backup}

        except Exception as e:
            log.error("AST replacement failed: %s", e)
            return {"success": False, "error": str(e)}


def auto_load_staged(registry, engine: SelfModEngine | None = None) -> dict:
    """Standalone helper — auto-load committed staged tools via a SelfModEngine instance."""
    if engine is None:
        raise ValueError("auto_load_staged requires a SelfModEngine instance")
    return engine.auto_load_staged(registry)
