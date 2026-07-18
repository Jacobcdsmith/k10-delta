"""net.* — HTTP fetch and web search."""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .context import ctx_get
from .schema import _p, tool

_USER_AGENT = "k10-delta/4.0"


def _http_get(url: str, timeout: float = 15) -> tuple[int | None, str, str | None]:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read(131072).decode("utf-8", errors="replace")
            return response.status, body, None
    except urllib.error.HTTPError as exc:
        body = exc.read(65536).decode("utf-8", errors="replace")
        return exc.code, body, str(exc)
    except Exception as exc:
        return None, "", str(exc)


def _ddg_resolve_url(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and "uddg" in parsed.query:
        params = urllib.parse.parse_qs(parsed.query)
        uddg = params.get("uddg", [None])[0]
        if uddg:
            return urllib.parse.unquote(uddg)
    return href


def _strip_html(text: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(cleaned)).strip()


def _parse_ddg_lite(page_html: str, count: int) -> list[dict[str, str]]:
    titles = re.findall(
        r"<a rel=\"nofollow\" href=\"([^\"]+)\"[^>]*class='result-link'>([^<]+)</a>",
        page_html,
    )
    snippets = re.findall(
        r"class='result-snippet'>\s*(.*?)\s*</td>",
        page_html,
        flags=re.DOTALL,
    )
    results: list[dict[str, str]] = []
    for idx, (href, title) in enumerate(titles[:count]):
        snippet = _strip_html(snippets[idx]) if idx < len(snippets) else ""
        results.append(
            {
                "title": html.unescape(title).strip(),
                "url": _ddg_resolve_url(href),
                "snippet": snippet,
            }
        )
    return results


def _parse_ddg_instant(payload: dict, count: int) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []

    def add_result(title: str, url: str, snippet: str) -> None:
        if len(results) >= count or not url:
            return
        results.append(
            {"title": title.strip(), "url": url.strip(), "snippet": snippet.strip()}
        )

    abstract = payload.get("Abstract", "")
    abstract_url = payload.get("AbstractURL", "")
    if abstract and abstract_url:
        add_result(payload.get("Heading") or "Instant answer", abstract_url, abstract)

    def walk_topics(topics: list) -> None:
        for item in topics:
            if len(results) >= count:
                return
            if not isinstance(item, dict):
                continue
            if "Topics" in item:
                walk_topics(item.get("Topics") or [])
                continue
            text = item.get("Text", "")
            url = item.get("FirstURL", "")
            if not url:
                continue
            title, _, snippet = text.partition(" - ")
            add_result(title or text, url, snippet or text)

    walk_topics(payload.get("RelatedTopics") or [])
    return results


def register(registry, ctx) -> None:
    def net_fetch(args: dict) -> str:
        headers = {}
        raw_headers = args.get("headers")
        if raw_headers:
            try:
                headers = json.loads(raw_headers)
            except Exception:
                return json.dumps({"ok": False, "error": "invalid headers JSON"})
        req = urllib.request.Request(
            args["url"],
            headers={"User-Agent": _USER_AGENT, **headers},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.dumps(
                    {
                        "status": r.status,
                        "body": r.read(65536).decode("utf-8", errors="replace"),
                    }
                )
        except urllib.error.HTTPError as e:
            return json.dumps({"status": e.code, "error": str(e)})
        except Exception as e:
            return json.dumps({"error": str(e)})

    def net_search(args: dict) -> str:
        query = (args.get("query") or "").strip()
        if not query:
            return json.dumps(
                {"ok": False, "results": [], "error": "query is required"}
            )
        count = max(1, min(int(args.get("count", 5)), 20))
        results: list[dict[str, str]] = []
        errors: list[str] = []

        encoded = urllib.parse.quote_plus(query)
        lite_url = f"https://lite.duckduckgo.com/lite/?q={encoded}"
        status, body, err = _http_get(lite_url)
        if err:
            errors.append(f"lite: {err}")
        elif status and 200 <= status < 300:
            results = _parse_ddg_lite(body, count)

        if not results:
            api_url = (
                "https://api.duckduckgo.com/?"
                + urllib.parse.urlencode(
                    {
                        "q": query,
                        "format": "json",
                        "no_redirect": "1",
                        "no_html": "1",
                        "skip_disambig": "1",
                    }
                )
            )
            status, body, err = _http_get(api_url)
            if err:
                errors.append(f"api: {err}")
            elif status and 200 <= status < 300:
                try:
                    results = _parse_ddg_instant(json.loads(body), count)
                except json.JSONDecodeError as exc:
                    errors.append(f"api: invalid JSON ({exc})")

        if results:
            return json.dumps({"ok": True, "results": results}, indent=2)
        return json.dumps(
            {
                "ok": False,
                "results": [],
                "error": "; ".join(errors) if errors else "no results found",
            },
            indent=2,
        )

    registry.register_from_def(
        tool(
            "net.fetch",
            "HTTP GET a URL.",
            {
                "url": _p("string", "URL"),
                "headers": _p("string", "JSON headers", required=False),
            },
        ),
        net_fetch,
    )
    registry.register_from_def(
        tool(
            "net.search",
            "Web search via DuckDuckGo (no API key).",
            {
                "query": _p("string", "Search query"),
                "count": _p("integer", "Max results (default 5)", required=False),
            },
        ),
        net_search,
    )
