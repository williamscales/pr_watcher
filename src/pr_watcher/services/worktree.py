from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pr_watcher.models import PR
from pr_watcher.services import run_cmd

log = logging.getLogger(__name__)

BARE_REPO = os.path.expanduser("~/src/glide")


@dataclass(frozen=True)
class WorkState:
    """What unrecoverable work, if any, lives in a worktree."""

    exists: bool
    dirty: bool  # tracked files modified or staged
    untracked: bool  # untracked files present
    unpushed: bool  # local commits not on upstream, or no upstream to compare against

    @property
    def has_work(self) -> bool:
        return self.dirty or self.untracked or self.unpushed

    def describe(self) -> list[str]:
        items: list[str] = []
        if self.dirty:
            items.append("uncommitted changes to tracked files")
        if self.untracked:
            items.append("untracked files")
        if self.unpushed:
            items.append("commits not pushed to origin")
        return items


class RemoveOutcome(Enum):
    REMOVED = "removed"  # worktree is gone
    REFUSED = "refused"  # worktree still holds work and force was not set
    ERROR = "error"  # git failed for some other reason


async def _has_unpushed(path: str) -> bool:
    """True if HEAD has commits the tracked upstream doesn't — or if we can't tell."""
    rc, out, _ = await run_cmd(
        "git", "rev-list", "--count", "@{u}..HEAD", cwd=path
    )
    if rc != 0:
        # No upstream, detached HEAD, etc.: we can't prove the branch is pushed,
        # so treat it as unsafe rather than risk discarding local commits.
        return True
    return out.strip() not in ("", "0")


async def inspect_worktree(pr: PR) -> WorkState:
    """Report the work at risk in a worktree. Errs toward 'has work' on any doubt."""
    path = pr.worktree_path_expanded
    if not Path(path).is_dir():
        return WorkState(exists=False, dirty=False, untracked=False, unpushed=False)

    rc, out, err = await run_cmd("git", "status", "--porcelain", cwd=path)
    if rc != 0:
        # Can't determine cleanliness — assume the worst so nothing gets deleted.
        log.error("git status failed in %s: %s", path, err)
        return WorkState(exists=True, dirty=True, untracked=True, unpushed=True)

    dirty = False
    untracked = False
    for line in out.splitlines():
        if not line:
            continue
        if line.startswith("??"):
            untracked = True
        else:
            dirty = True

    unpushed = await _has_unpushed(path)
    return WorkState(exists=True, dirty=dirty, untracked=untracked, unpushed=unpushed)


async def create_worktree(pr: PR) -> bool:
    if Path(pr.worktree_path_expanded).is_dir():
        log.info("Worktree already exists for PR #%d", pr.number)
        return True

    # drop stale worktree registry entries — the dir may be gone but git still thinks it's there
    await run_cmd("git", "worktree", "prune", cwd=BARE_REPO)

    rc, _, err = await run_cmd(
        "git", "fetch", "origin", pr.branch, cwd=BARE_REPO
    )
    if rc != 0:
        log.error("git fetch failed for PR #%d: %s", pr.number, err)
        return False

    rc, _, err = await run_cmd(
        "git", "worktree", "add",
        "--track", "-B", pr.branch,
        f"./review-pr-{pr.number}",
        f"origin/{pr.branch}",
        cwd=BARE_REPO,
    )
    if rc != 0:
        # only treat as success if the worktree dir actually exists now —
        # "already checked out" can mean "branch is checked out elsewhere", not "we already have it"
        if Path(pr.worktree_path_expanded).is_dir():
            log.info("Worktree already exists for PR #%d", pr.number)
            return True
        log.error("git worktree add failed for PR #%d: %s", pr.number, err)
        return False

    # -B resets the branch but doesn't always (re)configure upstream — set it explicitly so `git pull` works
    await run_cmd(
        "git", "branch", "--set-upstream-to", f"origin/{pr.branch}", pr.branch,
        cwd=BARE_REPO,
    )

    return True


async def update_worktree(pr: PR, *, force: bool = False) -> bool:
    if not Path(pr.worktree_path_expanded).is_dir():
        return await create_worktree(pr)

    # Never blow away local work with reset --hard unless explicitly forced.
    if not force:
        ws = await inspect_worktree(pr)
        if ws.has_work:
            log.warning(
                "Refusing to update worktree for PR #%d: would discard %s",
                pr.number,
                ", ".join(ws.describe()),
            )
            return False

    rc, _, err = await run_cmd(
        "git", "fetch", "origin", pr.branch, cwd=BARE_REPO
    )
    if rc != 0:
        log.error("git fetch failed for PR #%d: %s", pr.number, err)
        return False

    # reset --hard, not pull --ff-only, so force-pushes don't break us
    rc, _, err = await run_cmd(
        "git", "reset", "--hard", f"origin/{pr.branch}",
        cwd=pr.worktree_path_expanded,
    )
    if rc != 0:
        log.error("git reset failed for PR #%d: %s", pr.number, err)
        return False

    return True


async def remove_worktree(pr: PR, *, force: bool = False) -> RemoveOutcome:
    if not Path(pr.worktree_path_expanded).is_dir():
        # Directory already gone; just drop any stale registry entry.
        await run_cmd("git", "worktree", "prune", cwd=BARE_REPO)
        return RemoveOutcome.REMOVED

    # Barrier that no caller can bypass: never discard work unless force is set.
    # `git worktree remove` (below) refuses dirty/untracked trees on its own, but
    # it does NOT protect unpushed commits — this check does.
    if not force:
        ws = await inspect_worktree(pr)
        if ws.has_work:
            log.warning(
                "Refusing to remove worktree for PR #%d: holds %s",
                pr.number,
                ", ".join(ws.describe()),
            )
            return RemoveOutcome.REFUSED

    args = ["git", "worktree", "remove"]
    if force:
        args.append("--force")
    args.append(f"./review-pr-{pr.number}")
    rc, _, err = await run_cmd(*args, cwd=BARE_REPO)

    if rc != 0:
        if "not a working tree" in err or "is not a valid" in err:
            # git already considers it gone.
            await run_cmd("git", "worktree", "prune", cwd=BARE_REPO)
            return RemoveOutcome.REMOVED
        # Without --force git refuses a dirty tree — treat as refused, never as done.
        if not force and ("modified or untracked" in err or "use --force" in err or "use 'remove -f'" in err):
            log.warning("git refused to remove dirty worktree for PR #%d: %s", pr.number, err)
            return RemoveOutcome.REFUSED
        log.error("git worktree remove failed for PR #%d: %s", pr.number, err)
        return RemoveOutcome.ERROR

    await run_cmd("git", "worktree", "prune", cwd=BARE_REPO)
    return RemoveOutcome.REMOVED


def worktree_exists(pr: PR) -> bool:
    return Path(pr.worktree_path_expanded).is_dir()
