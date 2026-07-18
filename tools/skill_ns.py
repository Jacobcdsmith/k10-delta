"""skill.* — discover SKILL.md packages on disk."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from store import K10_ROOT, WORKSPACE

from .context import ctx_get
from .schema import tool

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)




def _parse_skill_md(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    name = path.parent.name
    description = ""
    m = _FM_RE.match(text)
    if m:
        for line in m.group(1).splitlines():
            if line.lower().startswith("name:"):
                name = line.split(":", 1)[1].strip().strip('"').strip("'")
            elif line.lower().startswith("description:"):
                description = line.split(":", 1)[1].strip().strip('"').strip("'")
    if not description:
        for line in text.splitlines():
            if line.strip() and not line.startswith("#"):
                description = line.strip()[:200]
                break
    return {
        "name": name,
        "description": description or "No description",
        "path": str(path.parent),
        "skill_md": str(path),
    }


def _skill_roots() -> list[Path]:
    home = Path.home()
    candidates = [
        K10_ROOT / "skills",
        WORKSPACE / "skills",
        home / ".agents" / "skills",
        home / ".claude" / "skills",
    ]
    seen: set[str] = set()
    roots: list[Path] = []
    for p in candidates:
        rp = p.resolve()
        if rp.exists() and str(rp) not in seen:
            seen.add(str(rp))
            roots.append(rp)
    return roots


def register(registry, ctx) -> None:
    def skill_introspect(args: dict) -> str:
        skills: list[dict] = []
        scanned: list[str] = []
        for root in _skill_roots():
            scanned.append(str(root))
            if not root.is_dir():
                continue
            for item in sorted(root.iterdir()):
                if not item.is_dir():
                    continue
                skill_md = item / "SKILL.md"
                if skill_md.exists():
                    meta = _parse_skill_md(skill_md)
                    if meta:
                        skills.append(meta)
        return json.dumps({
            "ok": True,
            "scanned_roots": scanned,
            "count": len(skills),
            "skills": skills,
        }, indent=2)

    registry.register_from_def(
        tool("skill.introspect", "List SKILL.md packages from known skill directories.", {}),
        skill_introspect,
    )