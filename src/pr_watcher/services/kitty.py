from __future__ import annotations

import json
import logging
import os
from enum import Enum
from pathlib import Path

from pr_watcher.models import PR
from pr_watcher.services import run_cmd

log = logging.getLogger(__name__)

KITTEN = "/Applications/kitty.app/Contents/MacOS/kitten"
CLAUDE = os.path.expanduser("~/.local/bin/claude")
CLAUDE_FLAGS = "--dangerously-skip-permissions --model opus --effort high"
SHELL_NAMES = frozenset({"zsh", "bash", "sh", "fish", "dash", "ksh", "tcsh", "csh"})


class ContinueOutcome(Enum):
    LAUNCHED = "launched"  # new tab opened running claude --continue
    RESUMED = "resumed"  # typed into the PR's existing tab, which sat at a shell
    BUSY = "busy"  # tab is running something; focused it instead of typing over it
    ERROR = "error"


def _base_cmd() -> list[str]:
    """Base kitten command. Uses KITTY_LISTEN_ON for socket discovery."""
    socket = os.environ.get("KITTY_LISTEN_ON", os.environ.get("PR_WATCHER_KITTY_SOCKET", ""))
    if socket:
        return [KITTEN, "@", "--to", socket]
    return [KITTEN, "@"]


async def _launch_tab(pr: PR, shell_cmd: str, *, take_focus: bool) -> bool:
    focus_args: list[str] = []
    if not take_focus:
        focus_args.append("--dont-take-focus")
    rc, _, err = await run_cmd(
        *_base_cmd(), "launch",
        "--type", "tab",
        "--tab-title", pr.tab_title,
        "--cwd", pr.worktree_path_expanded,
        *focus_args,
        "--", "zsh", "-l", "-c", shell_cmd,
    )
    if rc != 0:
        log.error("Failed to launch kitty tab for PR #%d: %s", pr.number, err)
        return False
    return True


async def launch_tab(pr: PR) -> bool:
    shell_cmd = (
        f'{CLAUDE} {CLAUDE_FLAGS} --permission-mode plan "$(cat {pr.prompt_path})"; exec zsh'
    )
    return await _launch_tab(pr, shell_cmd, take_focus=False)


async def launch_continue_tab(pr: PR) -> bool:
    """Open a tab resuming the worktree's most recent Claude session."""
    return await _launch_tab(pr, f"{CLAUDE} {CLAUDE_FLAGS} --continue; exec zsh", take_focus=True)


def _at_shell(window: dict) -> bool:
    """True when nothing but the window's own shell is in the foreground.

    An interactive shell reports a bare cmdline; anything with arguments is a shell
    running a script, which is a foreground program like any other.
    """
    procs = window.get("foreground_processes", [])
    if not procs:
        return False
    for proc in procs:
        cmdline = proc.get("cmdline", [])
        if len(cmdline) != 1:
            return False
        if os.path.basename(cmdline[0]).lstrip("-") not in SHELL_NAMES:
            return False
    return True


async def continue_session(pr: PR) -> ContinueOutcome:
    """Resume the worktree's last Claude session, reusing the PR's tab if it has one."""
    tabs = await _query_tabs()
    if tabs is None:
        return ContinueOutcome.ERROR

    marker = f"PR #{pr.number}"
    tab = next((t for t in tabs if marker in t.get("title", "")), None)
    if tab is None:
        if await launch_continue_tab(pr):
            return ContinueOutcome.LAUNCHED
        return ContinueOutcome.ERROR

    wid = _window_id(tab)
    if wid is None:
        return ContinueOutcome.ERROR

    # Typing into a window that is running something would corrupt that program's input.
    idle = next((w for w in tab.get("windows", []) if _at_shell(w)), None)
    if is_claude_running(tab) or idle is None:
        await _focus_window(wid)
        return ContinueOutcome.BUSY

    # Leading ^C clears anything half-typed at the prompt, which would otherwise
    # be run as one mangled command with what we send.
    rc, _, err = await run_cmd(
        *_base_cmd(), "send-text", "--match", f"id:{idle['id']}",
        f"\x03{CLAUDE} {CLAUDE_FLAGS} --continue\r",
    )
    if rc != 0:
        log.error("Failed to send continue command for PR #%d: %s", pr.number, err)
        return ContinueOutcome.ERROR

    await _focus_window(idle["id"])
    return ContinueOutcome.RESUMED


def session_dir(pr: PR) -> Path:
    """Where Claude Code stores conversations for this worktree."""
    slug = pr.worktree_path_expanded.replace("/", "-").replace(".", "-")
    return Path(os.path.expanduser("~/.claude/projects")) / slug


def has_prior_session(pr: PR) -> bool:
    return any(session_dir(pr).glob("*.jsonl"))


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
    if not windows:
        return None
    return windows[0]["id"]


def _tab_match(window_id: int) -> str:
    """Match the tab holding this window.

    `id:` would be read as a tab id first and only fall back to window ids, so it
    can select an unrelated tab that happens to carry that number.
    """
    return f"window_id:{window_id}"


def is_claude_running(tab: dict) -> bool:
    for window in tab.get("windows", []):
        for proc in window.get("foreground_processes", []):
            cmdline = proc.get("cmdline", [])
            if any("claude" in arg for arg in cmdline):
                return True
    return False


async def _focus_window(window_id: int) -> bool:
    rc, _, err = await run_cmd(
        *_base_cmd(), "focus-window", "--match", f"id:{window_id}",
    )
    if rc != 0:
        log.error("Failed to focus kitty window %d: %s", window_id, err)
        return False
    return True


async def focus_tab(pr_number: int) -> bool:
    tab = await find_pr_tab(pr_number)
    if tab is None:
        log.error("No tab found for PR #%d", pr_number)
        return False
    wid = _window_id(tab)
    if wid is None:
        return False
    rc, _, err = await run_cmd(
        *_base_cmd(), "focus-tab", "--match", _tab_match(wid),
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
        *_base_cmd(), "close-tab", "--match", _tab_match(wid),
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
        *_base_cmd(), "set-tab-title", "--match", _tab_match(wid),
        new_title,
    )
    if rc != 0:
        log.error("Failed to update tab title for PR #%d: %s", pr_number, err)
        return False
    return True
