from __future__ import annotations

import json
import logging
from datetime import datetime

from pr_watcher.models import PR, Notification
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


async def fetch_pr(pr_number: int) -> PR:
    rc, out, err = await run_cmd(
        "gh", "pr", "view", str(pr_number),
        "--repo", "burstcash/glide",
        "--json", "number,title,author,headRefName,url,body,additions,deletions",
    )
    if rc != 0:
        raise RuntimeError(f"Failed to fetch PR #{pr_number}: {err}")

    try:
        node = json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse PR #{pr_number}: {e}")

    return PR(
        number=node["number"],
        title=node["title"],
        author=node["author"]["login"],
        branch=node["headRefName"],
        url=node["url"],
        body=node.get("body", ""),
        additions=node.get("additions", 0),
        deletions=node.get("deletions", 0),
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


# --- Notifications ---


def _parse_gh_time(s: str) -> datetime:
    """Parse a GitHub ISO8601 timestamp (UTC, trailing Z) to naive local time."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt.astimezone().replace(tzinfo=None)


def _notification_from_node(node: dict) -> Notification:
    subject = node.get("subject") or {}
    repo = node.get("repository") or {}
    return Notification(
        thread_id=str(node["id"]),
        title=subject.get("title", ""),
        repo=repo.get("full_name", ""),
        reason=node.get("reason", ""),
        subject_type=subject.get("type", ""),
        subject_api_url=subject.get("url"),
        repo_url=repo.get("html_url", ""),
        updated_at=_parse_gh_time(node["updated_at"]),
        unread=node.get("unread", True),
    )


async def poll_notifications() -> list[Notification]:
    rc, out, err = await run_cmd(
        "gh", "api", "--paginate", "repos/burstcash/glide/notifications?all=true",
    )
    if rc != 0:
        log.error("GitHub notifications poll failed: %s", err)
        raise RuntimeError(f"gh notifications failed: {err}")

    try:
        nodes = json.loads(out)
    except json.JSONDecodeError as e:
        log.error("Failed to parse notifications response: %s", e)
        raise RuntimeError(f"Failed to parse notifications: {e}")

    return [_notification_from_node(n) for n in nodes]


async def mark_notification_read(thread_id: str) -> bool:
    rc, _, err = await run_cmd(
        "gh", "api", "-X", "PATCH", f"notifications/threads/{thread_id}",
    )
    if rc != 0:
        log.error("Failed to mark notification %s read: %s", thread_id, err)
        return False
    return True


async def mark_notification_done(thread_id: str) -> bool:
    rc, _, err = await run_cmd(
        "gh", "api", "-X", "DELETE", f"notifications/threads/{thread_id}",
    )
    if rc != 0:
        log.error("Failed to mark notification %s done: %s", thread_id, err)
        return False
    return True


async def resolve_notification_url(n: Notification) -> str:
    """Resolve the web URL for a notification, falling back to the repo page."""
    if n.subject_api_url:
        rc, out, err = await run_cmd(
            "gh", "api", n.subject_api_url, "--jq", ".html_url",
        )
        if rc == 0:
            url = out.strip()
            if url and url != "null":
                return url
        log.warning("Failed to resolve html_url for notification %s: %s", n.thread_id, err)
    return n.repo_url
