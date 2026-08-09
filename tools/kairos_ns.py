"""kairos.* — bridge to the local Kairos knowledge/provenance workspace
(C:\\Users\\jacob\\kairos), a source-grounded, local-first store: ingested
content is parsed by real structure (Markdown headings, AST nodes, log
lines), linked via typed relations, and every result carries full
provenance (artifact id, path, locator, parser). No LLM, no network — pure
SQLite + FTS5. Kairos's own README describes it as "a local-first substrate
for a single persistent agent runtime" and explicitly excludes autonomous
execution and self-modification from its own scope, i.e. it is designed to
be driven by something exactly like this host, not to act on its own.

Workspace: this repo's own .kairos/ directory (K10_DIR/.kairos), separate
from Kairos's other example workspaces elsewhere on disk.
"""

from __future__ import annotations

import json
from pathlib import Path

from .schema import _p, tool

# AGENT_DIR is the delta/ package directory — KAIROS workspace lives at delta/.kairos/
AGENT_DIR = Path(__file__).resolve().parent.parent

_KAIROS_AVAILABLE = False
try:
    from kairos.domain.errors import KairosError, WorkspaceNotFoundError
    from kairos.services.activity import recent_events as _recent_events
    from kairos.services.artifacts import list_artifacts as _list_artifacts
    from kairos.services.config_query import get_config_symbol as _get_config_symbol
    from kairos.services.context import RuntimeContext
    from kairos.services.doctor import run_doctor as _run_doctor
    from kairos.services.ingest import ingest as _ingest
    from kairos.services.logs_query import query_logs as _query_logs
    from kairos.services.notes import add_note as _add_note
    from kairos.services.notes import list_notes as _list_notes
    from kairos.services.search import search as _search
    from kairos.services.show import show as _show
    from kairos.services.trace import trace as _trace
    from kairos.services.wells import add_member as _well_add_member
    from kairos.services.wells import create_well as _well_create
    from kairos.services.wells import list_all_wells as _well_list_all
    from kairos.services.wells import remove_member as _well_remove_member
    from kairos.services.wells import show_well as _well_show
    from kairos.services.workspace_init import init as _workspace_init

    _KAIROS_AVAILABLE = True
except Exception:  # pragma: no cover — honest fallback, mirrors tools/mqtt_ns.py
    KairosError = Exception  # type: ignore[assignment,misc]
    WorkspaceNotFoundError = Exception  # type: ignore[assignment,misc]


def _dump(model) -> str:
    if isinstance(model, list):
        return json.dumps([m.model_dump(mode="json") for m in model], indent=2, default=str)
    return json.dumps(model.model_dump(mode="json"), indent=2, default=str)


def register(registry, ctx) -> None:
    _cached_ctx: dict[str, object] = {}

    def _open_ctx():
        if "ctx" in _cached_ctx:
            return _cached_ctx["ctx"]
        rc = RuntimeContext.open(AGENT_DIR)
        _cached_ctx["ctx"] = rc
        return rc

    def _resolve_path(raw: str) -> Path:
        p = Path(raw)
        return p if p.is_absolute() else (AGENT_DIR / p)

    def _unavailable(reason: str) -> str:
        return json.dumps({"ok": False, "error": reason}, indent=2)

    def kairos_init(args: dict) -> str:
        if not _KAIROS_AVAILABLE:
            return _unavailable("Kairos is not importable on this host (C:\\Users\\jacob\\kairos not found or its dependencies are missing).")
        try:
            rc = _open_ctx()
            return json.dumps({"ok": True, "created": False, "root": str(rc.workspace.root)}, indent=2)
        except WorkspaceNotFoundError:
            pass
        try:
            rc = _workspace_init(K10_DIR, name=args.get("name") or "k10-delta")
            _cached_ctx["ctx"] = rc
            return json.dumps({"ok": True, "created": True, "root": str(rc.workspace.root)}, indent=2)
        except KairosError as e:
            return _unavailable(str(e))

    def _with_ctx(fn):
        """Run fn(rc) against the open workspace, with uniform error handling."""
        if not _KAIROS_AVAILABLE:
            return _unavailable("Kairos is not importable on this host.")
        try:
            rc = _open_ctx()
        except WorkspaceNotFoundError:
            return _unavailable("No Kairos workspace yet at K10_DIR/.kairos — call kairos.init first.")
        try:
            return fn(rc)
        except KairosError as e:
            return json.dumps({"ok": False, "error": str(e)}, indent=2)

    def kairos_ingest(args: dict) -> str:
        recursive = bool(args.get("recursive", False))
        path = _resolve_path(args["path"])
        return _with_ctx(lambda rc: _dump(_ingest(rc, path, recursive=recursive)))

    def kairos_search(args: dict) -> str:
        return _with_ctx(lambda rc: _dump(_search(
            rc, args["query"], kind=args.get("kind"), well=args.get("well"),
            limit=min(int(args.get("limit", 20)), 100),
        )))

    def kairos_trace(args: dict) -> str:
        return _with_ctx(lambda rc: _dump(_trace(
            rc, args["query"], depth=min(int(args.get("depth", 2)), 5), well=args.get("well"),
        )))

    def kairos_show(args: dict) -> str:
        return _with_ctx(lambda rc: _dump(_show(rc, args["artifact_id"], locator=args.get("locator"))))

    def kairos_artifacts(args: dict) -> str:
        return _with_ctx(lambda rc: _dump(_list_artifacts(
            rc, kind=args.get("kind"), limit=min(int(args.get("limit", 50)), 500),
        )))

    def kairos_note_add(args: dict) -> str:
        return _with_ctx(lambda rc: _dump(_add_note(rc, args["target_id"], args["text"])))

    def kairos_note_list(args: dict) -> str:
        return _with_ctx(lambda rc: _dump(_list_notes(rc, args["target_id"])))

    def kairos_well_create(args: dict) -> str:
        return _with_ctx(lambda rc: _dump(_well_create(rc, args["name"], args["purpose"])))

    def kairos_well_add(args: dict) -> str:
        return _with_ctx(lambda rc: _dump(_well_add_member(
            rc, args["well_name"], args["target_id"], note=args.get("note"),
        )))

    def kairos_well_show(args: dict) -> str:
        return _with_ctx(lambda rc: _dump(_well_show(rc, args["well_name"])))

    def kairos_well_list(args: dict) -> str:
        return _with_ctx(lambda rc: _dump(_well_list_all(rc)))

    def kairos_well_remove(args: dict) -> str:
        def _do(rc):
            _well_remove_member(rc, args["well_name"], args["member_id"])
            return json.dumps({"ok": True, "well_name": args["well_name"], "member_id": args["member_id"]}, indent=2)
        return _with_ctx(_do)

    def kairos_config(args: dict) -> str:
        return _with_ctx(lambda rc: _dump(_get_config_symbol(rc, args["symbol"])))

    def kairos_logs(args: dict) -> str:
        return _with_ctx(lambda rc: _dump(_query_logs(
            rc, args["query"], before=int(args.get("before", 0)), after=int(args.get("after", 0)),
            level=args.get("level"), limit=min(int(args.get("limit", 20)), 200),
        )))

    def kairos_activity(args: dict) -> str:
        return _with_ctx(lambda rc: _dump(_recent_events(rc, limit=min(int(args.get("limit", 20)), 200))))

    def kairos_doctor(args: dict) -> str:
        return _with_ctx(lambda rc: _dump(_run_doctor(rc)))

    registry.register_from_def(
        tool("kairos.init", "Create or open the Kairos workspace at this repo's .kairos/ directory.",
             {"name": _p("string", "Workspace name (default: k10-delta)", required=False)}),
        kairos_init,
    )
    registry.register_from_def(
        tool("kairos.ingest", "Ingest a file or directory into Kairos (parsed, hashed, provenance-tracked; source never modified).",
             {"path": _p("string", "File or directory path (absolute, or relative to repo root)"),
              "recursive": _p("boolean", "Ingest a directory tree (default false)", required=False)}),
        kairos_ingest,
    )
    registry.register_from_def(
        tool("kairos.search", "Full-text search (FTS5) over ingested content, with provenance per hit.",
             {"query": _p("string", "Search query"),
              "kind": _p("string", "Filter by artifact kind", required=False),
              "well": _p("string", "Scope to a named coherence well", required=False),
              "limit": _p("integer", "Max hits (default 20, max 100)", required=False)}),
        kairos_search,
    )
    registry.register_from_def(
        tool("kairos.trace", "Evidence-first relation/mention graph traversal from a term, artifact id, or span id.",
             {"query": _p("string", "Seed term, artifact id, or span id"),
              "depth": _p("integer", "Hop depth (default 2, max 5)", required=False),
              "well": _p("string", "Scope seeding to a named coherence well", required=False)}),
        kairos_trace,
    )
    registry.register_from_def(
        tool("kairos.show", "Full parsed structure (spans + provenance) of one ingested artifact.",
             {"artifact_id": _p("string", "Artifact id"),
              "locator": _p("string", "Restrict to one locator", required=False)}),
        kairos_show,
    )
    registry.register_from_def(
        tool("kairos.artifacts", "List ingested artifacts.",
             {"kind": _p("string", "Filter by kind", required=False),
              "limit": _p("integer", "Max results (default 50, max 500)", required=False)}),
        kairos_artifacts,
    )
    registry.register_from_def(
        tool("kairos.note_add", "Attach an owner-authored annotation to an artifact or span.",
             {"target_id": _p("string", "Artifact or span id"),
              "text": _p("string", "Note body")}),
        kairos_note_add,
    )
    registry.register_from_def(
        tool("kairos.note_list", "List notes attached to an artifact or span.",
             {"target_id": _p("string", "Artifact or span id")}),
        kairos_note_list,
    )
    registry.register_from_def(
        tool("kairos.well_create", "Create a named coherence well (curated working set).",
             {"name": _p("string", "Well name"), "purpose": _p("string", "What this well is for")}),
        kairos_well_create,
    )
    registry.register_from_def(
        tool("kairos.well_add", "Add an artifact or span to a coherence well.",
             {"well_name": _p("string", "Well name"), "target_id": _p("string", "Artifact or span id"),
              "note": _p("string", "Optional note on this membership", required=False)}),
        kairos_well_add,
    )
    registry.register_from_def(
        tool("kairos.well_show", "Show a coherence well and its members.",
             {"well_name": _p("string", "Well name")}),
        kairos_well_show,
    )
    registry.register_from_def(
        tool("kairos.well_list", "List all coherence wells."),
        kairos_well_list,
    )
    registry.register_from_def(
        tool("kairos.well_remove", "Remove a member from a coherence well.",
             {"well_name": _p("string", "Well name"), "member_id": _p("string", "Well-membership id (from kairos.well_add/well_show)")}),
        kairos_well_remove,
    )
    registry.register_from_def(
        tool("kairos.config", "Look up one ingested Kconfig symbol (prompt/type/default/depends_on/choices/children).",
             {"symbol": _p("string", "Kconfig symbol name")}),
        kairos_config,
    )
    registry.register_from_def(
        tool("kairos.logs", "Search ingested log lines, with optional before/after context and level filter.",
             {"query": _p("string", "Search query"),
              "before": _p("integer", "Lines of context before each hit (default 0)", required=False),
              "after": _p("integer", "Lines of context after each hit (default 0)", required=False),
              "level": _p("string", "Filter by log level", required=False),
              "limit": _p("integer", "Max anchor hits (default 20, max 200)", required=False)}),
        kairos_logs,
    )
    registry.register_from_def(
        tool("kairos.activity", "Recent Kairos workspace events (the append-only audit log).",
             {"limit": _p("integer", "Max events (default 20, max 200)", required=False)}),
        kairos_activity,
    )
    registry.register_from_def(
        tool("kairos.doctor", "Kairos workspace/environment health checks (FTS5, content integrity, index consistency)."),
        kairos_doctor,
    )
