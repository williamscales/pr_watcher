from __future__ import annotations

import logging
import os
from pathlib import Path

from pr_watcher.models import PR
from pr_watcher.services import run_cmd

log = logging.getLogger(__name__)

BARE_REPO = os.path.expanduser("~/src/glide")


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


async def update_worktree(pr: PR) -> bool:
    if not Path(pr.worktree_path_expanded).is_dir():
        return await create_worktree(pr)

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


async def remove_worktree(pr: PR) -> bool:
    rc, _, err = await run_cmd(
        "git", "worktree", "remove", "--force",
        f"./review-pr-{pr.number}",
        cwd=BARE_REPO,
    )
    if rc != 0 and "not a working tree" not in err and "is not a valid" not in err:
        log.error("git worktree remove failed for PR #%d: %s", pr.number, err)
        return False

    await run_cmd("git", "worktree", "prune", cwd=BARE_REPO)
    return True


def worktree_exists(pr: PR) -> bool:
    return Path(pr.worktree_path_expanded).is_dir()
