"""Text, episode, and tool-usage analytics helpers."""
from __future__ import annotations

import math
import re
import time
from collections import Counter
from datetime import datetime, timezone

from .constants import (
    CONCEPT_TOP_N, DREAM_EVIDENCE_W, META_EPISODE_RE, META_STOPWORDS,
    STOPWORDS, STRUCTURAL_DETAIL_KEYS,
)

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tokenise(text: str, *, meta: bool = False) -> list[str]:
    words = re.findall(r"[a-zA-Z]{3,}", text.lower())
    skip = META_STOPWORDS if meta else STOPWORDS
    return [w for w in words if w not in skip]


def _episode_text(ep: dict) -> str:
    """Extract human-meaningful text from an episode, skipping structural keys."""
    parts: list[str] = []
    summary = ep.get("summary")
    if summary:
        parts.append(str(summary))
    detail = ep.get("detail")
    if isinstance(detail, str):
        parts.append(detail)
    elif isinstance(detail, dict):
        for key, value in detail.items():
            if key in STRUCTURAL_DETAIL_KEYS:
                continue
            if isinstance(value, (list, tuple)):
                parts.extend(str(v) for v in value)
            else:
                parts.append(str(value))
    return " ".join(parts)


def _is_meta_episode(ep: dict) -> bool:
    summary = str(ep.get("summary", ""))
    if META_EPISODE_RE.match(summary):
        return True
    return ep.get("source") in ("dream", "autonomy", "will")


def _substantive_episodes(episodes: list[dict]) -> list[dict]:
    return [ep for ep in episodes if not _is_meta_episode(ep)]


def _parse_ts(ep: dict) -> float | None:
    raw = ep.get("ts")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _bm25(query: list[str], doc: list[str], avg_dl: float,
          k1: float = 1.5, b: float = 0.75) -> float:
    if not query or not doc:
        return 0.0
    dl = len(doc)
    tf = Counter(doc)
    score = 0.0
    for t in query:
        f = tf.get(t, 0)
        num = f * (k1 + 1)
        den = f + k1 * (1 - b + b * dl / max(avg_dl, 1))
        score += num / max(den, 1e-9)
    return score


def _doc_tokens(ep: dict) -> list[str]:
    return _tokenise(_episode_text(ep), meta=True)


def _tfidf_concepts(episodes: list[dict], top_n: int = CONCEPT_TOP_N) -> list[str]:
    """Rank concepts by TF-IDF over substantive episodes (down-weights boilerplate)."""
    corpus = _substantive_episodes(episodes)
    if not corpus:
        corpus = episodes[-min(50, len(episodes)):]
    docs = [_doc_tokens(ep) for ep in corpus]
    if not docs:
        return []

    n_docs = len(docs)
    df: Counter[str] = Counter()
    tf_total: Counter[str] = Counter()
    for doc in docs:
        seen = set(doc)
        df.update(seen)
        tf_total.update(doc)

    scores: dict[str, float] = {}
    for term, freq in tf_total.items():
        idf = math.log((n_docs + 1) / (df[term] + 1)) + 1.0
        scores[term] = freq * idf

    ranked = sorted(scores, key=scores.get, reverse=True)
    return ranked[:top_n]


def _rising_concepts(episodes: list[dict], window: int = 15) -> list[str]:
    """Terms gaining frequency in the recent window vs the prior window."""
    substantive = _substantive_episodes(episodes)
    if len(substantive) < window * 2:
        return []

    early = Counter(_tokenise(
        " ".join(_episode_text(ep) for ep in substantive[-window * 2:-window]),
        meta=True))
    late = Counter(_tokenise(
        " ".join(_episode_text(ep) for ep in substantive[-window:]),
        meta=True))

    rising: list[tuple[str, float]] = []
    for term, late_count in late.items():
        early_count = early.get(term, 0)
        if late_count >= 2 and late_count > early_count:
            rising.append((term, (late_count - early_count) / max(early_count, 1)))
    rising.sort(key=lambda x: x[1], reverse=True)
    return [t for t, _ in rising[:8]]


def _episode_activity(episodes: list[dict]) -> dict:
    """Practical activity metrics from episode timestamps."""
    substantive = _substantive_episodes(episodes)
    timestamps = [t for ep in substantive if (t := _parse_ts(ep)) is not None]
    if len(timestamps) < 2:
        return {"substantive_count": len(substantive), "episodes_per_day": 0.0,
                "hours_since_last": None, "meta_ratio": round(
                    1 - len(substantive) / max(len(episodes), 1), 3)}

    timestamps.sort()
    span_days = max((timestamps[-1] - timestamps[0]) / 86400, 1 / 24)
    hours_since = (time.time() - timestamps[-1]) / 3600
    return {
        "substantive_count": len(substantive),
        "episodes_per_day": round(len(substantive) / span_days, 2),
        "hours_since_last": round(hours_since, 1),
        "meta_ratio": round(1 - len(substantive) / max(len(episodes), 1), 3),
    }


def score_hypothesis_relevance(hypothesis_text: str,
                               episodes: list[dict]) -> float:
    """BM25 relevance of a hypothesis against substantive episodes (0–1 scale)."""
    query = _tokenise(hypothesis_text, meta=True)
    if not query:
        return 0.0

    corpus = _substantive_episodes(episodes)
    if not corpus:
        corpus = episodes[-20:]
    docs = [_doc_tokens(ep) for ep in corpus]
    avg_dl = sum(len(d) for d in docs) / max(len(docs), 1)
    raw = max(_bm25(query, doc, avg_dl) for doc in docs) if docs else 0.0
    # Normalize: typical strong hit is ~3–8 depending on query length
    return min(1.0, raw / max(len(query) * 1.2, 1))


def _evidence_weight(evidence_text: str) -> float:
    if evidence_text.startswith("[dream"):
        return DREAM_EVIDENCE_W
    return 1.0


def _active_creator_directive(directive: dict | None) -> dict | None:
    """Return creator_directive if not expired, else None."""
    if not directive:
        return None
    expires_at = directive.get("expires_at")
    if not expires_at:
        return directive
    try:
        exp = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > exp:
            return None
    except ValueError:
        pass
    return directive


# ── Tool-use analytics ─────────────────────────────────────────────────────────

def _tool_episodes(episodes: list[dict]) -> list[dict]:
    return [ep for ep in episodes if ep.get("source") == "tool"]


def _extract_tool_name(ep: dict) -> str | None:
    detail = ep.get("detail")
    if isinstance(detail, dict):
        for key in ("tool", "name", "tool_name"):
            if detail.get(key):
                return str(detail[key])
    summary = str(ep.get("summary", "")).strip()
    if summary.startswith("[tool]"):
        return summary[6:].strip().split()[0] or None
    if summary.startswith("tool:"):
        return summary[5:].strip().split()[0] or None
    if "." in summary and " " not in summary:
        return summary
    return summary or None


def _tool_episode_error(ep: dict) -> bool:
    detail = ep.get("detail")
    if isinstance(detail, dict):
        if detail.get("error") or detail.get("ok") is False:
            return True
    text = _episode_text(ep).lower()
    return any(m in text for m in ("error", "failed", "exception", "traceback"))


def analyze_tool_usage(episodes: list[dict],
                       tool_index: dict | None = None) -> dict:
    """Aggregate tool-call episode statistics for reflection and introspection."""
    tool_eps = _tool_episodes(episodes)
    tool_counts: Counter[str] = Counter()
    ns_counts: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()

    for ep in tool_eps:
        name = _extract_tool_name(ep)
        if not name:
            continue
        tool_counts[name] += 1
        if "." in name:
            ns_counts[name.split(".", 1)[0]] += 1
        if _tool_episode_error(ep):
            error_counts[name] += 1

    stats: dict = {
        "top_tools": [{"tool": t, "count": c} for t, c in tool_counts.most_common(10)],
        "namespace_heat": dict(ns_counts.most_common()),
        "error_tools": [
            {"tool": t, "errors": c}
            for t, c in error_counts.most_common() if c > 0
        ],
        "total_tool_calls": len(tool_eps),
    }

    if tool_index:
        used_ns = set(ns_counts)
        all_ns = set(tool_index.get("namespaces", {}))
        stats["unused_namespaces"] = sorted(all_ns - used_ns)

    return stats


def build_tool_insights(tool_stats: dict,
                        tool_index: dict | None = None) -> list[str]:
    insights: list[str] = []
    total = tool_stats.get("total_tool_calls", 0)
    if total == 0:
        insights.append(
            "No tool-call episodes recorded — enable tool logging for usage analytics.")
        return insights

    top = tool_stats.get("top_tools", [])
    if top:
        insights.append(
            f"Most-used tool: {top[0]['tool']} ({top[0]['count']} calls)")

    heat = tool_stats.get("namespace_heat", {})
    if heat:
        hottest = max(heat, key=heat.get)
        insights.append(f"Hottest namespace: {hottest} ({heat[hottest]} calls)")

    errors = tool_stats.get("error_tools", [])
    if errors:
        insights.append(
            f"{len(errors)} tool(s) produced errors — top: {errors[0]['tool']}")

    unused = tool_stats.get("unused_namespaces", [])
    if unused and tool_index:
        preview = ", ".join(unused[:5])
        suffix = f" (+{len(unused) - 5} more)" if len(unused) > 5 else ""
        insights.append(f"Unused namespaces ({len(unused)}): {preview}{suffix}")

    return insights[:5]


def synthesise_fact(topic: str, episodes: list[dict],
                    search_fn) -> dict:
    """Synthesise a semantic fact from search results using TF-IDF concept ranking."""
    results = search_fn(topic, episodes)
    if not results:
        return {
            "topic": topic,
            "synthesised_at": _now_iso(),
            "evidence_episodes": 0,
            "key_concepts": [],
            "summary": f"No episodes found around '{topic}'.",
        }

    concepts = _tfidf_concepts(results, top_n=15)
    summary = (
        f"From {len(results)} episodes around '{topic}': "
        f"{', '.join(concepts[:8])}."
    )
    return {
        "topic": topic,
        "synthesised_at": _now_iso(),
        "evidence_episodes": len(results),
        "key_concepts": concepts,
        "summary": summary,
    }

