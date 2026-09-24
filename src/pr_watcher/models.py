from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Status(Enum):
    NEW = "new"
    SPAWNING = "spawning"
    ACTIVE = "active"
    IDLE = "idle"
    REVIEW_SUBMITTED = "submitted"
    DONE = "done"
    CLOSED = "closed"


STATUS_ICONS: dict[Status, str] = {
    Status.NEW: "\u27d0",
    Status.SPAWNING: "\u27d0",
    Status.ACTIVE: "\u25cf",
    Status.IDLE: "\u25cb",
    Status.REVIEW_SUBMITTED: "\u2713",
    Status.DONE: "\u2713",
    Status.CLOSED: "\u2717",
}


@dataclass
class PR:
    number: int
    title: str
    author: str
    branch: str
    url: str
    body: str
    additions: int
    deletions: int
    status: Status = Status.NEW
    review_submitted: bool = False
    spawned_at: datetime | None = None
    last_updated: datetime | None = None

    @property
    def worktree_path(self) -> str:
        return f"~/src/glide/review-pr-{self.number}"

    @property
    def worktree_path_expanded(self) -> str:
        return os.path.expanduser(self.worktree_path)

    @property
    def prompt_path(self) -> str:
        return os.path.expanduser(f"~/.pr-watcher/prompts/pr-{self.number}.txt")

    @property
    def tab_title(self) -> str:
        icon = STATUS_ICONS.get(self.status, "?")
        short = self.title[:40] + ("\u2026" if len(self.title) > 40 else "")
        return f"{icon} #{self.number}: {short}"

    @property
    def state_key(self) -> str:
        return str(self.number)

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "title": self.title,
            "author": self.author,
            "branch": self.branch,
            "url": self.url,
            "body": self.body,
            "additions": self.additions,
            "deletions": self.deletions,
            "status": self.status.value,
            "review_submitted": self.review_submitted,
            "spawned_at": self.spawned_at.isoformat() if self.spawned_at else None,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> PR:
        return cls(
            number=d["number"],
            title=d["title"],
            author=d["author"],
            branch=d["branch"],
            url=d["url"],
            body=d["body"],
            additions=d["additions"],
            deletions=d["deletions"],
            status=Status(d["status"]),
            review_submitted=d.get("review_submitted", False),
            spawned_at=datetime.fromisoformat(d["spawned_at"]) if d.get("spawned_at") else None,
            last_updated=datetime.fromisoformat(d["last_updated"]) if d.get("last_updated") else None,
        )


@dataclass
class Notification:
    thread_id: str
    title: str
    repo: str
    reason: str
    subject_type: str
    subject_api_url: str | None
    repo_url: str
    updated_at: datetime
    unread: bool
    html_url: str | None = None

    def to_dict(self) -> dict:
        return {
            "thread_id": self.thread_id,
            "title": self.title,
            "repo": self.repo,
            "reason": self.reason,
            "subject_type": self.subject_type,
            "subject_api_url": self.subject_api_url,
            "repo_url": self.repo_url,
            "updated_at": self.updated_at.isoformat(),
            "unread": self.unread,
            "html_url": self.html_url,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Notification:
        return cls(
            thread_id=d["thread_id"],
            title=d["title"],
            repo=d["repo"],
            reason=d["reason"],
            subject_type=d["subject_type"],
            subject_api_url=d.get("subject_api_url"),
            repo_url=d["repo_url"],
            updated_at=datetime.fromisoformat(d["updated_at"]),
            unread=d["unread"],
            html_url=d.get("html_url"),
        )
