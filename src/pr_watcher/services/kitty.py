from __future__ import annotations

import json
import logging
import os

from pr_watcher.models import PR
from pr_watcher.services import run_cmd

log = logging.getLogger(__name__)

KITTEN = "/Applications/kitty.app/Contents/MacOS/kitten"
CLAUDE = os.path.expanduser("~/.local/bin/claude")


def _base_cmd() -> list[str]:
    """Base kitten command. Uses KITTY_LISTEN_ON for socket discovery."""
    socket = os.environ.get("KITTY_LISTEN_ON", os.environ.get("PR_WATCHER_KITTY_SOCKET", ""))
    if socket:
        return [KITTEN, "@", "--to", socket]
    return [KITTEN, "@"]


async def launch_tab(pr: PR) -> bool:
    shell_cmd = f'{CLAUDE} --dangerously-skip-permissions --effort high --permission-mode plan "$(cat {pr.prompt_path})"; exec zsh'
    rc, _, err = await run_cmd(
        *_base_cmd(), "launch",
        "--type", "tab",
        "--tab-title", pr.tab_title,
        "--cwd", pr.worktree_path_expanded,
        "--dont-take-focus",
        "--", "zsh", "-l", "-c", shell_cmd,
    )
    if rc != 0:
        log.error("Failed to launch kitty tab for PR #%d: %s", pr.number, err)
        return False
    return True


async def _query_tabs() -> list[dict] | None:
    """Return kitty tabs, or None if the query failed (distinct from 'no tabs')."""
    rc, out, err = await run_cmd(*_base_cmd(), "ls")
    if rc != 0:
        log.error("Failed to list kitty tabs: %s", err)
        return None
    try:
        os_windows = json.loads(out)
    except json.JSONDecodeError:
        log.error("Failed to parse kitty ls output")
        return None

    tabs: list[dict] = []
    for os_win in os_windows:
        for tab in os_win.get("tabs", []):
            tabs.append({
                "id": tab.get("id"),
                "title": tab.get("title", ""),
                "windows": tab.get("windows", []),
            })
    return tabs


async def list_tabs() -> list[dict]:
    return (await _query_tabs()) or []


async def find_pr_tab(pr_number: int) -> dict | None:
    tabs = await list_tabs()
    marker = f"PR #{pr_number}"
    for tab in tabs:
        if marker in tab.get("title", ""):
            return tab
    return None


async def in_use(pr_number: int) -> bool:
    """Conservative liveness gate for auto-cleanup.

    True if a tab for this PR is open OR the kitty query failed. We only return
    False when we can positively confirm no tab exists, so a flaky query can never
    green-light destroying a worktree that still has a live session in it.
    """
    tabs = await _query_tabs()
    if tabs is None:
        return True  # couldn't determine — assume in use, never auto-destroy
    marker = f"PR #{pr_number}"
    return any(marker in t.get("title", "") for t in tabs)


async def live_session_label(pr_number: int) -> str | None:
    """Describe the session in this PR's tab; None only if we confirm none exists.

    On a failed query we return a label (not None) so callers still prompt before
    doing anything destructive.
    """
    tabs = await _query_tabs()
    if tabs is None:
        return "a session that could not be verified (kitty query failed)"
    marker = f"PR #{pr_number}"
    tab = next((t for t in tabs if marker in t.get("title", "")), None)
    if tab is None:
        return None
    if is_claude_running(tab):
        return "a running Claude Code session"
    return "an open terminal session"


def _window_id(tab: dict) -> int | None:
    windows = tab.get("windows", [])
    return windows[0]["id"] if windows else None


def is_claude_running(tab: dict) -> bool:
    for window in tab.get("windows", []):
        for proc in window.get("foreground_processes", []):
            cmdline = proc.get("cmdline", [])
            if any("claude" in arg for arg in cmdline):
                return True
    return False


async def focus_tab(pr_number: int) -> bool:
    tab = await find_pr_tab(pr_number)
    if tab is None:
        log.error("No tab found for PR #%d", pr_number)
        return False
    wid = _window_id(tab)
    if wid is None:
        return False
    rc, _, err = await run_cmd(
        *_base_cmd(), "focus-tab", "--match", f"id:{wid}",
    )
    if rc != 0:
        log.error("Failed to focus tab for PR #%d: %s", pr_number, err)
        return False
    return True


async def close_tab(pr_number: int) -> bool:
    tab = await find_pr_tab(pr_number)
    if tab is None:
        return True  # already gone
    wid = _window_id(tab)
    if wid is None:
        return False
    rc, _, err = await run_cmd(
        *_base_cmd(), "close-tab", "--match", f"id:{wid}",
    )
    if rc != 0:
        log.error("Failed to close tab for PR #%d: %s", pr_number, err)
        return False
    return True


async def update_tab_title(pr_number: int, new_title: str) -> bool:
    tab = await find_pr_tab(pr_number)
    if tab is None:
        return False
    wid = _window_id(tab)
    if wid is None:
        return False
    rc, _, err = await run_cmd(
        *_base_cmd(), "set-tab-title", "--match", f"id:{wid}",
        new_title,
    )
    if rc != 0:
        log.error("Failed to update tab title for PR #%d: %s", pr_number, err)
        return False
    return True
