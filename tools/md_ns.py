"""md.* — markdown parsing, structure, and authoring."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .fs import resolve_path, normalize_rel_path
from .context import ctx_get
from .schema import _p, tool

_ENCODING = "utf-8"
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_CODE_FENCE_RE = re.compile(r"```[\w]*\n(.*?)```", re.DOTALL)




def _read_md(path: Path) -> tuple[str, dict, str]:
    text = path.read_text(encoding=_ENCODING, errors="replace")
    fm: dict = {}
    body = text
    m = _FM_RE.match(text)
    if m:
        raw_fm = m.group(1)
        body = text[m.end():]
        for line in raw_fm.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip().strip('"').strip("'")
    return text, fm, body


def _headings(body: str) -> list[dict]:
    out = []
    for m in _HEADING_RE.finditer(body):
        level = len(m.group(1))
        out.append({"level": level, "text": m.group(2).strip(),
                    "slug": re.sub(r"[^a-z0-9]+", "-", m.group(2).lower()).strip("-")})
    return out


def _extract_section(body: str, heading: str) -> str | None:
    target = heading.lower().strip()
    lines = body.splitlines()
    start = None
    level = None
    for i, line in enumerate(lines):
        hm = re.match(r"^(#{1,6})\s+(.+)$", line)
        if hm and hm.group(2).strip().lower() == target:
            start = i
            level = len(hm.group(1))
            break
    if start is None:
        return None
    chunk = [lines[start]]
    for line in lines[start + 1:]:
        hm = re.match(r"^(#{1,6})\s+", line)
        if hm and len(hm.group(1)) <= (level or 6):
            break
        chunk.append(line)
    return "\n".join(chunk).strip()


def register(registry, ctx) -> None:
    workspace = ctx_get(ctx, "WORKSPACE")
    call_tool = ctx_get(ctx, "call_tool")

    def _path(args: dict) -> Path:
        return resolve_path(args["path"], workspace)

    def md_read(args: dict) -> str:
        p = _path(args)
        if not p.exists():
            return json.dumps({"ok": False, "error": f"Not found: {args['path']}"})
        if p.suffix.lower() not in (".md", ".markdown"):
            return json.dumps({"ok": False, "error": "Not a markdown file"})
        full, fm, body = _read_md(p)
        headings = _headings(body)
        links = [{"text": a, "url": b} for a, b in _LINK_RE.findall(body)]
        code_blocks = len(_CODE_FENCE_RE.findall(body))
        return json.dumps({
            "ok": True,
            "path": normalize_rel_path(args["path"]),
            "frontmatter": fm,
            "headings": headings,
            "links": links[:50],
            "code_blocks": code_blocks,
            "lines": len(full.splitlines()),
            "words": len(body.split()),
            "body_preview": body[:500],
        }, indent=2)

    def md_toc(args: dict) -> str:
        p = _path(args)
        if not p.exists():
            return json.dumps({"ok": False, "error": f"Not found: {args['path']}"})
        _, _, body = _read_md(p)
        toc = _headings(body)
        return json.dumps({"ok": True, "path": args["path"], "toc": toc}, indent=2)

    def md_extract(args: dict) -> str:
        p = _path(args)
        if not p.exists():
            return json.dumps({"ok": False, "error": f"Not found: {args['path']}"})
        _, _, body = _read_md(p)
        section = _extract_section(body, args["heading"])
        if section is None:
            return json.dumps({"ok": False, "error": f"Heading not found: {args['heading']}"})
        return json.dumps({"ok": True, "heading": args["heading"], "content": section}, indent=2)

    def md_stats(args: dict) -> str:
        p = _path(args)
        if not p.exists():
            return json.dumps({"ok": False, "error": f"Not found: {args['path']}"})
        full, fm, body = _read_md(p)
        headings = _headings(body)
        return json.dumps({
            "ok": True,
            "path": args["path"],
            "lines": len(full.splitlines()),
            "words": len(body.split()),
            "characters": len(body),
            "headings": len(headings),
            "h1": sum(1 for h in headings if h["level"] == 1),
            "links": len(_LINK_RE.findall(body)),
            "code_blocks": len(_CODE_FENCE_RE.findall(body)),
            "has_frontmatter": bool(fm),
            "frontmatter_keys": list(fm.keys()),
        }, indent=2)

    def md_links(args: dict) -> str:
        p = _path(args)
        if not p.exists():
            return json.dumps({"ok": False, "error": f"Not found: {args['path']}"})
        _, _, body = _read_md(p)
        links = [{"text": a, "url": b} for a, b in _LINK_RE.findall(body)]
        return json.dumps({"ok": True, "count": len(links), "links": links}, indent=2)

    def md_frontmatter(args: dict) -> str:
        p = _path(args)
        if not p.exists():
            return json.dumps({"ok": False, "error": f"Not found: {args['path']}"})
        full, fm, body = _read_md(p)
        if args.get("set"):
            new_fm = json.loads(args["set"]) if isinstance(args["set"], str) else args["set"]
            if not isinstance(new_fm, dict):
                return json.dumps({"ok": False, "error": "set must be JSON object"})
            lines = ["---"] + [f"{k}: {v}" for k, v in new_fm.items()] + ["---", ""]
            new_content = "\n".join(lines) + body
            call_tool("fs.write", {"path": args["path"], "content": new_content})
            return json.dumps({"ok": True, "frontmatter": new_fm})
        return json.dumps({"ok": True, "frontmatter": fm, "has_frontmatter": bool(fm)}, indent=2)

    def md_write(args: dict) -> str:
        content = args["content"]
        fm = args.get("frontmatter")
        if fm:
            if isinstance(fm, str):
                fm = json.loads(fm)
            header = "---\n" + "\n".join(f"{k}: {v}" for k, v in fm.items()) + "\n---\n\n"
            content = header + content.lstrip("\n")
        result = call_tool("fs.write", {"path": args["path"], "content": content})
        return json.dumps({
            "ok": True,
            "path": normalize_rel_path(args["path"]),
            "write": json.loads(result),
        }, indent=2)

    def md_append(args: dict) -> str:
        p = _path(args)
        existing = ""
        if p.exists():
            existing = p.read_text(encoding=_ENCODING)
            if existing and not existing.endswith("\n"):
                existing += "\n"
        section = args.get("heading", "")
        block = args["content"]
        if section:
            block = f"\n## {section}\n\n{block}\n"
        new_content = existing + block
        result = call_tool("fs.write", {"path": args["path"], "content": new_content})
        return json.dumps({"ok": True, "appended": section or "(body)", "write": json.loads(result)}, indent=2)

    def md_list(args: dict) -> str:
        rel = args.get("path", ".")
        pattern = args.get("pattern", "*.md")
        raw = call_tool("fs.find", {"path": rel, "pattern": pattern, "max": args.get("max", 100)})
        data = json.loads(raw)
        errors: list[str] = []
        for m in data.get("matches", []):
            try:
                mp = resolve_path(m["path"], workspace)
                if mp.exists():
                    _, fm, body = _read_md(mp)
                    m["title"] = fm.get("title") or next(
                        (h["text"] for h in _headings(body) if h["level"] == 1), m["name"])
                    m["words"] = len(body.split())
            except Exception as e:
                errors.append(f"{m.get('path')}: {e}")
        if errors:
            data["enrich_errors"] = errors
        return json.dumps(data, indent=2)

    registry.register_from_def(
        tool("md.read", "Parse markdown: frontmatter, headings, links, stats.", {"path": _p("string", "Markdown file path")}),
        md_read,
    )
    registry.register_from_def(
        tool("md.toc", "Table of contents from markdown headings.", {"path": _p("string", "Markdown file path")}),
        md_toc,
    )
    registry.register_from_def(
        tool("md.extract", "Extract section by heading text.", {
            "path": _p("string", "Markdown file path"),
            "heading": _p("string", "Heading text to extract"),
        }),
        md_extract,
    )
    registry.register_from_def(
        tool("md.stats", "Word/line/heading/link counts for a markdown file.", {"path": _p("string", "Path")}),
        md_stats,
    )
    registry.register_from_def(
        tool("md.links", "Extract all markdown links.", {"path": _p("string", "Path")}),
        md_links,
    )
    registry.register_from_def(
        tool("md.frontmatter", "Read or replace YAML frontmatter.", {
            "path": _p("string", "Path"),
            "set": _p("string", "JSON frontmatter object to write", required=False),
        }),
        md_frontmatter,
    )
    registry.register_from_def(
        tool("md.write", "Write markdown with optional frontmatter.", {
            "path": _p("string", "Path"),
            "content": _p("string", "Markdown body"),
            "frontmatter": _p("string", "JSON frontmatter (optional)", required=False),
        }),
        md_write,
    )
    registry.register_from_def(
        tool("md.append", "Append section or content to markdown file.", {
            "path": _p("string", "Path"),
            "content": _p("string", "Content to append"),
            "heading": _p("string", "Optional H2 heading for new section", required=False),
        }),
        md_append,
    )
    registry.register_from_def(
        tool("md.list", "List markdown files with titles and word counts.", {
            "path": _p("string", "Root path (default .)", required=False),
            "pattern": _p("string", "Glob (default *.md)", required=False),
            "max": _p("integer", "Max files", required=False),
        }),
        md_list,
    )