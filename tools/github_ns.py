"""github.* — GitHub REST API access (repos, files, issues, pull requests).

Auth is read once from the GITHUB_TOKEN environment variable — never accepted
as a tool argument, so a token can't end up logged into episodes.jsonl or the
dashboard. Uses only the stdlib (urllib), matching net.py's zero-dependency
style; no PyGithub / requests dependency required.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .schema import _p, tool

_API_BASE = "https://api.github.com"
_USER_AGENT = "k10-delta/4.0"


def _token() -> str:
    return os.environ.get("GITHUB_TOKEN", "").strip()


def _request(method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
    token = _token()
    if not token:
        return {"ok": False, "error": "GITHUB_TOKEN not set — see .env.example"}

    url = f"{_API_BASE}{path}"
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "User-Agent": _USER_AGENT,
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            payload = json.loads(raw) if raw else {}
            return {"ok": True, "status": resp.status, "data": payload}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw)
            message = payload.get("message", str(exc))
        except Exception:
            message = raw.decode("utf-8", errors="replace") or str(exc)
        return {"ok": False, "status": exc.code, "error": message}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def register(registry, ctx) -> None:
    def github_whoami(args: dict) -> str:
        result = _request("GET", "/user")
        if not result["ok"]:
            return json.dumps(result, indent=2)
        user = result["data"]
        return json.dumps(
            {"ok": True, "login": user.get("login"), "name": user.get("name")},
            indent=2,
        )

    def github_list_repos(args: dict) -> str:
        owner = (args.get("owner") or "").strip()
        per_page = max(1, min(int(args.get("per_page", 30)), 100))
        if owner:
            result = _request("GET", f"/users/{owner}/repos", params={"per_page": per_page})
        else:
            result = _request("GET", "/user/repos", params={"per_page": per_page, "sort": "updated"})
        if not result["ok"]:
            return json.dumps(result, indent=2)
        repos = [
            {"name": r["name"], "full_name": r["full_name"], "private": r["private"], "url": r["html_url"]}
            for r in result["data"]
        ]
        return json.dumps({"ok": True, "repos": repos}, indent=2)

    def github_repo_info(args: dict) -> str:
        owner, repo = args["owner"], args["repo"]
        result = _request("GET", f"/repos/{owner}/{repo}")
        if not result["ok"]:
            return json.dumps(result, indent=2)
        r = result["data"]
        return json.dumps(
            {
                "ok": True,
                "full_name": r.get("full_name"),
                "description": r.get("description"),
                "default_branch": r.get("default_branch"),
                "private": r.get("private"),
                "stargazers_count": r.get("stargazers_count"),
                "open_issues_count": r.get("open_issues_count"),
                "url": r.get("html_url"),
            },
            indent=2,
        )

    def github_list_files(args: dict) -> str:
        owner, repo = args["owner"], args["repo"]
        path = (args.get("path") or "").strip().lstrip("/")
        ref = args.get("ref")
        result = _request("GET", f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref})
        if not result["ok"]:
            return json.dumps(result, indent=2)
        data = result["data"]
        if isinstance(data, dict):
            data = [data]
        entries = [{"name": e["name"], "path": e["path"], "type": e["type"], "size": e.get("size")} for e in data]
        return json.dumps({"ok": True, "entries": entries}, indent=2)

    def github_read_file(args: dict) -> str:
        owner, repo, path = args["owner"], args["repo"], args["path"].lstrip("/")
        ref = args.get("ref")
        result = _request("GET", f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref})
        if not result["ok"]:
            return json.dumps(result, indent=2)
        data = result["data"]
        if isinstance(data, list) or data.get("type") != "file":
            return json.dumps({"ok": False, "error": f"{path} is not a file"}, indent=2)
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        return json.dumps({"ok": True, "path": path, "sha": data["sha"], "content": content}, indent=2)

    def github_write_file(args: dict) -> str:
        owner, repo, path = args["owner"], args["repo"], args["path"].lstrip("/")
        message = args["message"]
        content = args["content"]
        branch = args.get("branch")

        sha = args.get("sha")
        if not sha:
            existing = _request("GET", f"/repos/{owner}/{repo}/contents/{path}", params={"ref": branch})
            if existing["ok"] and isinstance(existing["data"], dict):
                sha = existing["data"].get("sha")

        body = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        if branch:
            body["branch"] = branch
        if sha:
            body["sha"] = sha

        result = _request("PUT", f"/repos/{owner}/{repo}/contents/{path}", body=body)
        if not result["ok"]:
            return json.dumps(result, indent=2)
        commit = result["data"].get("commit", {})
        return json.dumps(
            {"ok": True, "path": path, "commit_sha": commit.get("sha"), "url": commit.get("html_url")},
            indent=2,
        )

    def github_list_issues(args: dict) -> str:
        owner, repo = args["owner"], args["repo"]
        state = args.get("state", "open")
        result = _request("GET", f"/repos/{owner}/{repo}/issues", params={"state": state, "per_page": 30})
        if not result["ok"]:
            return json.dumps(result, indent=2)
        issues = [
            {"number": i["number"], "title": i["title"], "state": i["state"], "url": i["html_url"]}
            for i in result["data"]
            if "pull_request" not in i
        ]
        return json.dumps({"ok": True, "issues": issues}, indent=2)

    def github_create_issue(args: dict) -> str:
        owner, repo = args["owner"], args["repo"]
        body: dict[str, Any] = {"title": args["title"]}
        if args.get("body"):
            body["body"] = args["body"]
        labels = args.get("labels")
        if labels:
            body["labels"] = [l.strip() for l in labels.split(",") if l.strip()]
        result = _request("POST", f"/repos/{owner}/{repo}/issues", body=body)
        if not result["ok"]:
            return json.dumps(result, indent=2)
        i = result["data"]
        return json.dumps({"ok": True, "number": i["number"], "url": i["html_url"]}, indent=2)

    def github_create_pr(args: dict) -> str:
        owner, repo = args["owner"], args["repo"]
        body = {
            "title": args["title"],
            "head": args["head"],
            "base": args["base"],
        }
        if args.get("body"):
            body["body"] = args["body"]
        result = _request("POST", f"/repos/{owner}/{repo}/pulls", body=body)
        if not result["ok"]:
            return json.dumps(result, indent=2)
        pr = result["data"]
        return json.dumps({"ok": True, "number": pr["number"], "url": pr["html_url"]}, indent=2)

    def github_search_code(args: dict) -> str:
        query = args["query"]
        result = _request("GET", "/search/code", params={"q": query, "per_page": 20})
        if not result["ok"]:
            return json.dumps(result, indent=2)
        items = [
            {"path": it["path"], "repository": it["repository"]["full_name"], "url": it["html_url"]}
            for it in result["data"].get("items", [])
        ]
        return json.dumps({"ok": True, "results": items}, indent=2)

    registry.register_from_def(
        tool("github.whoami", "Return the login of the authenticated GitHub token.", {}),
        github_whoami,
    )
    registry.register_from_def(
        tool(
            "github.list_repos",
            "List repositories for the authenticated user, or a given owner.",
            {
                "owner": _p("string", "Username/org to list public repos for (default: authenticated user)", required=False),
                "per_page": _p("integer", "Max results, 1-100 (default 30)", required=False),
            },
        ),
        github_list_repos,
    )
    registry.register_from_def(
        tool(
            "github.repo_info",
            "Get metadata for a repository.",
            {"owner": _p("string", "Repo owner"), "repo": _p("string", "Repo name")},
        ),
        github_repo_info,
    )
    registry.register_from_def(
        tool(
            "github.list_files",
            "List files/dirs at a path in a repo.",
            {
                "owner": _p("string", "Repo owner"),
                "repo": _p("string", "Repo name"),
                "path": _p("string", "Directory path (default: repo root)", required=False),
                "ref": _p("string", "Branch/tag/commit SHA (default: default branch)", required=False),
            },
        ),
        github_list_files,
    )
    registry.register_from_def(
        tool(
            "github.read_file",
            "Read a file's contents from a repo.",
            {
                "owner": _p("string", "Repo owner"),
                "repo": _p("string", "Repo name"),
                "path": _p("string", "File path"),
                "ref": _p("string", "Branch/tag/commit SHA (default: default branch)", required=False),
            },
        ),
        github_read_file,
    )
    registry.register_from_def(
        tool(
            "github.write_file",
            "Create or update a file in a repo (single-file commit).",
            {
                "owner": _p("string", "Repo owner"),
                "repo": _p("string", "Repo name"),
                "path": _p("string", "File path"),
                "content": _p("string", "New file content (utf-8 text)"),
                "message": _p("string", "Commit message"),
                "branch": _p("string", "Target branch (default: repo's default branch)", required=False),
                "sha": _p("string", "Blob SHA of the file being replaced (auto-resolved if omitted)", required=False),
            },
        ),
        github_write_file,
    )
    registry.register_from_def(
        tool(
            "github.list_issues",
            "List issues in a repo.",
            {
                "owner": _p("string", "Repo owner"),
                "repo": _p("string", "Repo name"),
                "state": _p("string", "open | closed | all (default open)", required=False),
            },
        ),
        github_list_issues,
    )
    registry.register_from_def(
        tool(
            "github.create_issue",
            "Open a new issue in a repo.",
            {
                "owner": _p("string", "Repo owner"),
                "repo": _p("string", "Repo name"),
                "title": _p("string", "Issue title"),
                "body": _p("string", "Issue body", required=False),
                "labels": _p("string", "Comma-separated label names", required=False),
            },
        ),
        github_create_issue,
    )
    registry.register_from_def(
        tool(
            "github.create_pr",
            "Open a pull request.",
            {
                "owner": _p("string", "Repo owner"),
                "repo": _p("string", "Repo name"),
                "title": _p("string", "PR title"),
                "head": _p("string", "Branch containing changes (e.g. 'feature-x' or 'user:feature-x')"),
                "base": _p("string", "Branch to merge into (e.g. 'main')"),
                "body": _p("string", "PR description", required=False),
            },
        ),
        github_create_pr,
    )
    registry.register_from_def(
        tool(
            "github.search_code",
            "Search code across GitHub (GitHub code search syntax).",
            {"query": _p("string", "Search query, e.g. 'libcst repo:owner/name'")},
        ),
        github_search_code,
    )
