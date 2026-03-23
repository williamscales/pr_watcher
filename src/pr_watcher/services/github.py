from __future__ import annotations

import json
import logging

from pr_watcher.models import PR
from pr_watcher.services import run_cmd

log = logging.getLogger(__name__)

_cached_username: str | None = None

async def _get_username() -> str:
    global _cached_username
    if _cached_username:
        return _cached_username
    rc, out, err = await run_cmd("gh", "api", "user", "--jq", ".login")
    if rc != 0:
        log.error("Failed to get GitHub username: %s", err)
        raise RuntimeError(f"gh api user failed: {err}")
    _cached_username = out.strip()
    return _cached_username


async def poll_review_requests() -> list[PR]:
    rc, out, err = await run_cmd(
        "gh", "pr", "list",
        "--repo", "burstcash/glide",
        "--search", "review-requested:@me",
        "--json", "number,title,author,headRefName,url,body,additions,deletions",
        "--limit", "50",
    )
    if rc != 0:
        log.error("GitHub poll failed: %s", err)
        return []

    try:
        nodes = json.loads(out)
    except json.JSONDecodeError as e:
        log.error("Failed to parse GitHub response: %s", e)
        return []

    return [
        PR(
            number=node["number"],
            title=node["title"],
            author=node["author"]["login"],
            branch=node["headRefName"],
            url=node["url"],
            body=node.get("body", ""),
            additions=node.get("additions", 0),
            deletions=node.get("deletions", 0),
        )
        for node in nodes
    ]


async def check_review_submitted(pr_number: int) -> bool:
    username = await _get_username()
    rc, out, err = await run_cmd(
        "gh", "api", f"repos/burstcash/glide/pulls/{pr_number}/reviews"
    )
    if rc != 0:
        log.error("Failed to check reviews for PR #%d: %s", pr_number, err)
        return False

    try:
        reviews = json.loads(out)
    except json.JSONDecodeError:
        return False

    submitted_states = {"APPROVED", "CHANGES_REQUESTED", "COMMENTED"}
    return any(
        r.get("user", {}).get("login") == username and r.get("state") in submitted_states
        for r in reviews
    )


async def check_pr_state(pr_number: int) -> str:
    rc, out, err = await run_cmd(
        "gh", "pr", "view", str(pr_number),
        "--repo", "burstcash/glide",
        "--json", "state", "--jq", ".state",
    )
    if rc != 0:
        log.error("Failed to check state for PR #%d: %s", pr_number, err)
        return "UNKNOWN"
    return out.strip()
