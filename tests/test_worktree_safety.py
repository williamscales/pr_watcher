"""Regression tests: worktree removal/update must never silently discard work.

Everything runs against a throwaway repo built under a monkeypatched HOME, so the
tests cannot touch the real ~/src/glide.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from pr_watcher.models import PR
from pr_watcher.services import kitty, worktree

BRANCH = "feature-x"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        env={**os.environ, **GIT_ENV},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


@pytest.fixture
def glide(tmp_path, monkeypatch):
    """A cloned repo at $HOME/src/glide with origin having main + feature-x."""
    home = tmp_path / "home"
    (home / "src").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(origin))

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    (seed / "file.txt").write_text("hello\n")
    _git(seed, "add", "file.txt")
    _git(seed, "commit", "-m", "init")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "origin", "main")
    _git(seed, "checkout", "-b", BRANCH)
    (seed / "file.txt").write_text("hello\nfeature\n")
    _git(seed, "commit", "-am", "feature")
    _git(seed, "push", "origin", BRANCH)
    # Default origin to main so a plain clone doesn't check out feature-x.
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")

    glide_dir = home / "src" / "glide"
    _git(tmp_path, "clone", str(origin), str(glide_dir))
    monkeypatch.setattr(worktree, "BARE_REPO", str(glide_dir))
    return glide_dir


def _pr() -> PR:
    return PR(
        number=999, title="t", author="a", branch=BRANCH, url="u",
        body="", additions=0, deletions=0,
    )


def _wt() -> Path:
    return Path(os.path.expanduser("~/src/glide/review-pr-999"))


def test_created_worktree_is_clean(glide):
    async def run():
        pr = _pr()
        assert await worktree.create_worktree(pr) is True
        ws = await worktree.inspect_worktree(pr)
        assert ws.exists
        assert not ws.has_work
    asyncio.run(run())


def test_inspect_flags_untracked(glide):
    async def run():
        pr = _pr()
        await worktree.create_worktree(pr)
        (_wt() / "scratch.txt").write_text("session data\n")
        ws = await worktree.inspect_worktree(pr)
        assert ws.untracked and ws.has_work
    asyncio.run(run())


def test_inspect_flags_dirty(glide):
    async def run():
        pr = _pr()
        await worktree.create_worktree(pr)
        (_wt() / "file.txt").write_text("locally edited\n")
        ws = await worktree.inspect_worktree(pr)
        assert ws.dirty and ws.has_work
    asyncio.run(run())


def test_inspect_flags_unpushed_commit(glide):
    async def run():
        pr = _pr()
        await worktree.create_worktree(pr)
        wt = _wt()
        (wt / "file.txt").write_text("committed locally\n")
        _git(wt, "commit", "-am", "local work")
        ws = await worktree.inspect_worktree(pr)
        # Tree is clean, but the commit isn't on origin — still counts as work.
        assert not ws.dirty and not ws.untracked
        assert ws.unpushed and ws.has_work
    asyncio.run(run())


def test_remove_refuses_dirty_and_preserves_files(glide):
    async def run():
        pr = _pr()
        await worktree.create_worktree(pr)
        scratch = _wt() / "scratch.txt"
        scratch.write_text("precious\n")
        outcome = await worktree.remove_worktree(pr)  # force=False
        assert outcome is worktree.RemoveOutcome.REFUSED
        assert _wt().is_dir()
        assert scratch.read_text() == "precious\n"
    asyncio.run(run())


def test_remove_refuses_clean_but_unpushed(glide):
    # git worktree remove (no --force) would happily delete this; our layer won't.
    async def run():
        pr = _pr()
        await worktree.create_worktree(pr)
        wt = _wt()
        (wt / "file.txt").write_text("committed locally\n")
        _git(wt, "commit", "-am", "local work")
        outcome = await worktree.remove_worktree(pr)
        assert outcome is worktree.RemoveOutcome.REFUSED
        assert wt.is_dir()
    asyncio.run(run())


def test_remove_clean_succeeds(glide):
    async def run():
        pr = _pr()
        await worktree.create_worktree(pr)
        outcome = await worktree.remove_worktree(pr)
        assert outcome is worktree.RemoveOutcome.REMOVED
        assert not _wt().exists()
    asyncio.run(run())


def test_force_remove_discards_dirty(glide):
    async def run():
        pr = _pr()
        await worktree.create_worktree(pr)
        (_wt() / "scratch.txt").write_text("precious\n")
        outcome = await worktree.remove_worktree(pr, force=True)
        assert outcome is worktree.RemoveOutcome.REMOVED
        assert not _wt().exists()
    asyncio.run(run())


def test_update_refuses_over_dirty_and_preserves_edits(glide):
    async def run():
        pr = _pr()
        await worktree.create_worktree(pr)
        edited = _wt() / "file.txt"
        edited.write_text("MY UNCOMMITTED EDITS\n")
        ok = await worktree.update_worktree(pr)  # force=False
        assert ok is False
        assert edited.read_text() == "MY UNCOMMITTED EDITS\n"
    asyncio.run(run())


def test_in_use_fails_closed_when_query_errors(monkeypatch):
    async def boom():
        return None  # kitty query failed

    monkeypatch.setattr(kitty, "_query_tabs", boom)

    async def run():
        assert await kitty.in_use(123) is True
        assert await kitty.live_session_label(123) is not None
    asyncio.run(run())


def test_in_use_false_only_when_absence_confirmed(monkeypatch):
    async def empty():
        return []  # query succeeded, no tabs

    monkeypatch.setattr(kitty, "_query_tabs", empty)

    async def run():
        assert await kitty.in_use(123) is False
        assert await kitty.live_session_label(123) is None
    asyncio.run(run())
