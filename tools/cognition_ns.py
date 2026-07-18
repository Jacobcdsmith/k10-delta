"""cognition.*, context.*, emerge.*, dream.*, meme.*, and probe.* tool handlers."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from cognition import _tfidf_concepts, score_hypothesis_relevance, AUTONOMY_DEFAULTS

from .context import ctx_get
from .schema import _p, tool




def register(registry, ctx) -> None:
    soul = ctx_get(ctx, "soul")
    save_soul = ctx_get(ctx, "save_soul")
    read_episodes = ctx_get(ctx, "read_episodes")
    state_lock = ctx_get(ctx, "state_lock")
    hypotheses = ctx_get(ctx, "hypotheses")
    engine = ctx_get(ctx, "engine")
    dream = ctx_get(ctx, "dream")
    memetic = ctx_get(ctx, "memetic")
    prober = ctx_get(ctx, "prober")
    DREAM_LOG = ctx_get(ctx, "DREAM_LOG")
    _now_iso = ctx_get(ctx, "_now_iso")
    _start_time = ctx_get(ctx, "_start_time")
    call_tool = ctx_get(ctx, "call_tool")

    def cognition_reflect(args: dict) -> str:
        threading.Thread(target=engine._reflect, daemon=True).start()
        return json.dumps({"ok": True, "msg": "Reflection triggered (async)"})

    def cognition_search(args: dict) -> str:
        results = engine.search_episodes(args["query"], n=min(int(args.get("count", 10)), 50))
        return json.dumps(results, indent=2)

    def cognition_autonomy(args: dict) -> str:
        """Read/tune the autonomy manager. Human override point."""
        auto = soul.setdefault("autonomy", {})
        for key, value in AUTONOMY_DEFAULTS.items():
            auto.setdefault(key, value)
        changed: dict = {}
        if "enabled" in args:
            auto["enabled"] = bool(args["enabled"])
            changed["enabled"] = auto["enabled"]
        overrides = args.get("set")
        if isinstance(overrides, dict):
            for key, value in overrides.items():
                if key in AUTONOMY_DEFAULTS and key != "enabled" \
                        and isinstance(value, (int, float)):
                    auto[key] = value
                    changed[key] = value
        if changed:
            with state_lock:
                save_soul(soul)
        return json.dumps(
            {
                "ok": True,
                "changed": changed,
                "config": {k: auto.get(k) for k in AUTONOMY_DEFAULTS},
                "counters": auto.get("counters", {}),
                "last_review": auto.get("last_review"),
                "note": "enabled=false disables all autonomous behavior "
                        "(human override). Config persists in soul.json.",
            },
            indent=2,
        )

    def cognition_introspect(args: dict) -> str:
        episodes = read_episodes(200)
        return json.dumps(
            {
                "soul_summary": {
                    "identity": soul.get("identity"),
                    "boot_count": soul["boot_count"],
                    "axioms": soul.get("axioms", []),
                },
                "top_concepts": _tfidf_concepts(episodes, top_n=15),
                "trajectory": engine._compute_trajectory(episodes),
                "open_hypotheses": hypotheses.list_all("open"),
                "axiom_vulnerabilities": soul.get("semantic", {}).get("axiom_vulnerabilities", []),
                "dream_facts": soul.get("semantic", {}).get("dream_facts", []),
                "reflection_cycles": engine._cycle,
                "episode_count": len(episodes),
            },
            indent=2,
        )

    def context_environment(args: dict) -> str:
        return json.dumps(engine.get_environment(), indent=2)

    def context_trajectory(args: dict) -> str:
        return json.dumps(engine.get_trajectory(), indent=2)

    def context_session(args: dict) -> str:
        return json.dumps(
            {
                "boot_count": soul["boot_count"],
                "reflection_cycles": engine._cycle,
                "dream_cycles": dream._dream_count,
                "episode_count": len(read_episodes(9999)),
                "uptime_seconds": round(time.time() - _start_time),
                "last_boot": soul.get("last_boot"),
            }
        )

    def emerge_spawn_hypothesis(args: dict) -> str:
        return json.dumps(hypotheses.spawn(args["text"], args.get("source", "llm")), indent=2)

    def emerge_add_evidence(args: dict) -> str:
        return json.dumps(
            hypotheses.add_evidence(args["id"], args["evidence"], bool(args["supports"])),
            indent=2,
        )

    def emerge_get_hypothesis(args: dict) -> str:
        return json.dumps(hypotheses.get(args["id"]), indent=2)

    def emerge_list_hypotheses(args: dict) -> str:
        return json.dumps(hypotheses.list_all(args.get("status") or None), indent=2)

    def emerge_auto_test(args: dict) -> str:
        h = hypotheses.get(args["id"])
        relevance = score_hypothesis_relevance(h["text"], read_episodes(50))
        if relevance >= 0.55:
            verdict = "likely_supported"
        elif relevance >= 0.3:
            verdict = "weak_signal"
        else:
            verdict = "not_supported"
        out = {
            "ok": True,
            "relevance": round(relevance, 4),
            "verdict": verdict,
            "method": "bm25_episode_overlap",
            "disclaimer": "Heuristic overlap — not causal proof. Use emerge.add_evidence to commit.",
        }
        if args.get("commit"):
            updated = hypotheses.add_evidence(
                args["id"],
                f"auto_test: bm25_relevance={relevance:.2f}",
                supports=(verdict == "likely_supported"),
            )
            out["hypothesis"] = updated
        return json.dumps(out, indent=2)

    def emerge_socratic_dialogue(args: dict) -> str:
        topic = args["topic"]
        depth = max(1, min(int(args.get("depth", 3)), 5))
        prompt = (
            f"Socratic dialogue on: {topic}\n"
            f"Run {depth} rounds of thesis → antithesis → synthesis.\n"
            "End with one testable hypothesis and one concrete next action."
        )
        result = call_tool("hermes.query", {"prompt": prompt, "timeout": 120})
        try:
            parsed = json.loads(result)
            if parsed.get("success") is False:
                return json.dumps({
                    "ok": False,
                    "error": parsed.get("error", "hermes failed"),
                    "topic": topic,
                })
        except json.JSONDecodeError:
            pass
        call_tool(
            "memory.log_episode",
            {"summary": f"Socratic dialogue on {topic[:60]}", "detail": str(result)[:2000]},
        )
        return json.dumps({"ok": True, "topic": topic, "depth": depth, "dialogue": result}, indent=2)

    def emerge_synthesise(args: dict) -> str:
        results = engine.search_episodes(args["topic"], n=20)
        top = _tfidf_concepts(results, top_n=15)
        fact = {
            "topic": args["topic"],
            "synthesised_at": _now_iso(),
            "evidence_episodes": len(results),
            "key_concepts": top,
            "summary": (
                f"From {len(results)} episodes around '{args['topic']}': {', '.join(top[:8])}."
            ),
        }
        with state_lock:
            soul.setdefault("semantic", {}).setdefault("synthesised_facts", {})[args["topic"]] = fact
            save_soul(soul)
        return json.dumps(fact, indent=2)

    def dream_status(args: dict) -> str:
        return json.dumps(dream.status, indent=2)

    def dream_force(args: dict) -> str:
        threading.Thread(target=dream._dream, daemon=True).start()
        return json.dumps({"ok": True, "msg": "Dream triggered (async)"})

    def dream_read_log(args: dict) -> str:
        n = min(int(args.get("count", 10)), 100)
        if not DREAM_LOG.exists():
            return "[]"
        lines = DREAM_LOG.read_text(encoding="utf-8").splitlines()
        out = []
        skipped = 0
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except Exception:
                skipped += 1
        return json.dumps({"entries": out, "skipped_lines": skipped}, indent=2)

    def meme_report(args: dict) -> str:
        return json.dumps(memetic.report(), indent=2)

    def meme_decaying(args: dict) -> str:
        poten = soul.get("axiom_potentiation", {})
        return json.dumps([v for v in poten.values() if v.get("decaying")], indent=2)

    def probe_run(args: dict) -> str:
        results = prober.probe(read_episodes(200), engine._cycle)
        return json.dumps(results, indent=2)

    def probe_vulnerabilities(args: dict) -> str:
        return json.dumps(soul.get("semantic", {}).get("axiom_vulnerabilities", []), indent=2)

    registry.register_from_def(tool("cognition.reflect", "Force immediate reflection cycle."), cognition_reflect)
    registry.register_from_def(
        tool(
            "cognition.search",
            "BM25 search over episodes.",
            {
                "query": _p("string", "Query"),
                "count": _p("integer", "Results", required=False),
            },
        ),
        cognition_search,
    )
    registry.register_from_def(
        tool("cognition.introspect", "Full self-analysis snapshot."),
        cognition_introspect,
    )
    registry.register_from_def(
        tool(
            "cognition.autonomy",
            "Autonomy manager status and human override (kill switch, tuning).",
            {
                "enabled": _p("boolean", "Enable/disable autonomous behavior", required=False),
                "set": _p("object", "Config overrides (keys from AUTONOMY_DEFAULTS)", required=False),
            },
        ),
        cognition_autonomy,
    )
    registry.register_from_def(
        tool("context.environment", "Time, session, memory snapshot."),
        context_environment,
    )
    registry.register_from_def(
        tool("context.trajectory", "Trajectory analysis with interpretation."),
        context_trajectory,
    )
    registry.register_from_def(tool("context.session", "Session stats."), context_session)
    registry.register_from_def(
        tool(
            "emerge.spawn_hypothesis",
            "Create a hypothesis.",
            {
                "text": _p("string", "Text"),
                "source": _p("string", "Source (default: llm)", required=False),
            },
        ),
        emerge_spawn_hypothesis,
    )
    registry.register_from_def(
        tool(
            "emerge.add_evidence",
            "Add evidence to a hypothesis.",
            {
                "id": _p("string", "ID"),
                "evidence": _p("string", "Evidence"),
                "supports": _p("boolean", "Supports?"),
            },
        ),
        emerge_add_evidence,
    )
    registry.register_from_def(
        tool("emerge.get_hypothesis", "Get hypothesis by ID.", {"id": _p("string", "ID")}),
        emerge_get_hypothesis,
    )
    registry.register_from_def(
        tool(
            "emerge.list_hypotheses",
            "List hypotheses.",
            {"status": _p("string", "open|confirmed|falsified", required=False)},
        ),
        emerge_list_hypotheses,
    )
    registry.register_from_def(
        tool(
            "emerge.auto_test",
            "BM25 relevance check against recent episodes (heuristic).",
            {
                "id": _p("string", "ID"),
                "commit": _p("boolean", "Write evidence if true", required=False),
            },
        ),
        emerge_auto_test,
    )
    registry.register_from_def(
        tool(
            "emerge.socratic_dialogue",
            "Socratic exploration via Hermes (real LLM dialogue).",
            {
                "topic": _p("string", "Topic"),
                "depth": _p("integer", "Rounds (default 3)", required=False),
            },
        ),
        emerge_socratic_dialogue,
    )
    registry.register_from_def(
        tool(
            "emerge.synthesise",
            "TF-IDF concept extraction from episodes matching a topic.",
            {"topic": _p("string", "Topic")},
        ),
        emerge_synthesise,
    )
    registry.register_from_def(
        tool("dream.status", "Dream engine status and dream facts."),
        dream_status,
    )
    registry.register_from_def(tool("dream.force", "Force an immediate dream cycle."), dream_force)
    registry.register_from_def(
        tool(
            "dream.read_log",
            "Read dream log entries.",
            {"count": _p("integer", "Entries (default 10)", required=False)},
        ),
        dream_read_log,
    )
    registry.register_from_def(tool("meme.report", "Axiom potentiation report."), meme_report)
    registry.register_from_def(tool("meme.decaying", "List axioms currently decaying."), meme_decaying)
    registry.register_from_def(
        tool("probe.run", "Run adversarial probe against all axioms immediately."),
        probe_run,
    )
    registry.register_from_def(
        tool("probe.vulnerabilities", "List currently flagged vulnerable axioms."),
        probe_vulnerabilities,
    )