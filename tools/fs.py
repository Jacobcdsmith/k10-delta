"""fs.* tool handlers — workspace and k10_delta scoped."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from store import K10_ROOT, WORKSPACE

from .context import ctx_get
from .schema import _p, tool

_TEXT_ENCODING = "utf-8"




def normalize_rel_path(rel: str) -> str:
    """Strip redundant workspace/ prefix when WORKSPACE is already the root."""
    p = rel.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    if p == "workspace":
        return "."
    if p.startswith("workspace/"):
        p = p[len("workspace/"):]
    return p or "."


def _allowed_roots(workspace: Path) -> list[Path]:
    return [workspace.resolve(), K10_ROOT.resolve()]


def resolve_path(rel: str, workspace: Path) -> Path:
    rel = normalize_rel_path(rel)
    raw = Path(rel)
    if raw.is_absolute():
        p = raw.resolve()
    else:
        candidates = [(workspace / raw).resolve(), (K10_ROOT / raw).resolve()]
        p = next((c for c in candidates if c.exists()), candidates[0])

    roots = _allowed_roots(workspace)
    if not any(str(p).startswith(str(root)) for root in roots):
        raise PermissionError(
            f"fs.* access denied outside workspace/k10_delta: {rel}")
    return p


def _fmt_size(n: int | None) -> str | None:
    if n is None:
        return None
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}TB"


def _rel_path(item: Path) -> str:
    try:
        return str(item.resolve().relative_to(K10_ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return str(item).replace("\\", "/")


def _file_entry(item: Path) -> dict:
    st = item.stat()
    ext = item.suffix.lower()
    return {
        "name": item.name,
        "path": _rel_path(item),
        "type": "dir" if item.is_dir() else "file",
        "size": st.st_size if item.is_file() else None,
        "size_human": _fmt_size(st.st_size) if item.is_file() else None,
        "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        "extension": ext or None,
        "is_markdown": ext in (".md", ".markdown"),
    }


def register(registry, ctx) -> None:
    workspace = ctx_get(ctx, "WORKSPACE")

    def list_dir(args: dict) -> str:
        rel = args.get("path", ".")
        p = resolve_path(rel, workspace)
        if not p.exists():
            raise FileNotFoundError(rel)
        if not p.is_dir():
            raise NotADirectoryError(rel)
        entries = [_file_entry(item) for item in sorted(p.iterdir())]
        dirs = sum(1 for e in entries if e["type"] == "dir")
        files = len(entries) - dirs
        md_files = sum(1 for e in entries if e.get("is_markdown"))
        return json.dumps(
            {
                "path": _rel_path(p),
                "dirs": dirs,
                "files": files,
                "markdown_files": md_files,
                "entries": entries,
            },
            indent=2,
        )

    def read_file(args: dict) -> str:
        p = resolve_path(args["path"], workspace)
        if not p.exists():
            raise FileNotFoundError(args["path"])
        if not p.is_file():
            raise IsADirectoryError(args["path"])
        return p.read_text(encoding=_TEXT_ENCODING, errors="replace")

    def write_file(args: dict) -> str:
        p = resolve_path(args["path"], workspace)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(args["content"], encoding=_TEXT_ENCODING)
        return json.dumps({"ok": True, "path": _rel_path(p), "absolute": str(p)})

    def delete_file(args: dict) -> str:
        p = resolve_path(args["path"], workspace)
        if not p.exists():
            raise FileNotFoundError(args["path"])
        if p.is_dir():
            raise IsADirectoryError(args["path"])
        p.unlink()
        return json.dumps({"ok": True})

    def file_info(args: dict) -> str:
        p = resolve_path(args["path"], workspace)
        if not p.exists():
            raise FileNotFoundError(args["path"])
        entry = _file_entry(p)
        if p.is_file():
            entry["lines"] = len(
                p.read_text(encoding=_TEXT_ENCODING, errors="replace").splitlines())
        return json.dumps(entry, indent=2)

    def find_files(args: dict) -> str:
        pattern = args.get("pattern", "*")
        rel = args.get("path", ".")
        root = resolve_path(rel, workspace)
        if not root.exists():
            raise FileNotFoundError(rel)
        max_results = min(int(args.get("max", 50)), 200)
        hits: list[dict] = []
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                if Path(name).match(pattern):
                    fp = Path(dirpath) / name
                    hits.append(_file_entry(fp))
                    if len(hits) >= max_results:
                        break
            if len(hits) >= max_results:
                break
        return json.dumps(
            {"root": _rel_path(root),
             "pattern": pattern, "count": len(hits), "matches": hits},
            indent=2,
        )

    def _tree_node(p: Path, depth: int, max_depth: int) -> dict:
        node = _file_entry(p)
        if p.is_dir() and depth < max_depth:
            node["children"] = [
                _tree_node(c, depth + 1, max_depth)
                for c in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            ]
        return node

    def tree(args: dict) -> str:
        rel = args.get("path", ".")
        max_depth = min(int(args.get("depth", 3)), 8)
        p = resolve_path(rel, workspace)
        if not p.exists():
            raise FileNotFoundError(rel)
        return json.dumps(_tree_node(p, 0, max_depth), indent=2)

    def _rel_to_root(fp: Path) -> str:
        try:
            return str(fp.resolve().relative_to(K10_ROOT.resolve())).replace("\\", "/")
        except ValueError:
            return str(fp).replace("\\", "/")

    def overview(args: dict) -> str:
        roots = []
        k10 = K10_ROOT.resolve()
        for label, base in (("workspace", workspace.resolve()), ("k10_delta", k10)):
            if not base.exists():
                continue
            files, dirs, md, total_bytes = 0, 0, 0, 0
            recent: list[tuple[float, str]] = []
            for dirpath, dirnames, filenames in os.walk(base):
                dirs += len(dirnames)
                for name in filenames:
                    files += 1
                    fp = (Path(dirpath) / name).resolve()
                    try:
                        st = fp.stat()
                        total_bytes += st.st_size
                        recent.append((st.st_mtime, _rel_to_root(fp)))
                        if fp.suffix.lower() in (".md", ".markdown"):
                            md += 1
                    except OSError:
                        pass
            recent.sort(reverse=True)
            roots.append({
                "label": label,
                "path": _rel_to_root(base),
                "dirs": dirs,
                "files": files,
                "markdown_files": md,
                "total_size_human": _fmt_size(total_bytes),
                "recent_files": [p for _, p in recent[:8]],
            })
        return json.dumps({"roots": roots}, indent=2)

    registry.register_from_def(
        tool(
            "fs.list",
            "List directory with sizes, types, and markdown flags.",
            {"path": _p("string", "Relative path (default: .)", required=False)},
        ),
        list_dir,
    )
    registry.register_from_def(
        tool("fs.read", "Read a file under workspace or k10_delta.", {"path": _p("string", "Path")}),
        read_file,
    )
    registry.register_from_def(
        tool(
            "fs.write",
            "Write a file under workspace or k10_delta.",
            {"path": _p("string", "Path"), "content": _p("string", "Content")},
        ),
        write_file,
    )
    registry.register_from_def(
        tool("fs.delete", "Delete a file under workspace or k10_delta.", {"path": _p("string", "Path")}),
        delete_file,
    )
    registry.register_from_def(
        tool("fs.info", "File metadata: size, mtime, lines, markdown flag.", {"path": _p("string", "Path")}),
        file_info,
    )
    registry.register_from_def(
        tool(
            "fs.find",
            "Find files by glob pattern under a path.",
            {
                "path": _p("string", "Root path (default .)", required=False),
                "pattern": _p("string", "Glob e.g. *.md", required=False),
                "max": _p("integer", "Max results (default 50)", required=False),
            },
        ),
        find_files,
    )
    registry.register_from_def(
        tool(
            "fs.tree",
            "Recursive directory tree (JSON).",
            {
                "path": _p("string", "Root path (default .)", required=False),
                "depth": _p("integer", "Max depth (default 3)", required=False),
            },
        ),
        tree,
    )
    registry.register_from_def(
        tool("fs.overview", "High-level map of workspace and k10_delta with recent files.", {}),
        overview,
    )