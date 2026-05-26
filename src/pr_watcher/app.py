from __future__ import annotations

import asyncio
import logging
import os
import re
import webbrowser
from datetime import datetime

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, Input, DataTable
from textual.containers import Vertical
from textual import work

from pr_watcher.models import PR, Status
from pr_watcher.services import github, kitty, worktree, notify
from pr_watcher.state import load_state, save_state, write_prompt, remove_prompt, ensure_dirs
from pr_watcher.widgets.detail_pane import DetailPane
from pr_watcher.widgets.pr_table import PRTable

log = logging.getLogger(__name__)

APP_CSS = """
Screen {
    layout: vertical;
}

#pr-table {
    height: 1fr;
    min-height: 8;
}

#detail-pane {
    height: auto;
    max-height: 8;
    border-top: solid $accent;
    padding: 1 2;
}

#url-input {
    dock: bottom;
    display: none;
}

#url-input.visible {
    display: block;
}
"""


class PRWatcherApp(App):
    CSS = APP_CSS
    TITLE = "PR Review Watcher"
    SUB_TITLE = "burstcash/glide"
    BINDINGS = [
        Binding("o", "spawn", "Spawn"),
        Binding("f", "focus_tab", "Focus"),
        Binding("enter", "focus_tab", "Focus", show=False),
        Binding("r", "respawn", "Re-spawn"),
        Binding("u", "update", "Update"),
        Binding("d", "dismiss", "Dismiss"),
        Binding("a", "add_pr", "Add PR"),
        Binding("b", "open_in_browser", "Browser"),
        Binding("R", "force_refresh", "Refresh", key_display="shift+r"),
        Binding("q", "quit", "Quit"),
    ]

    _URL_RE = re.compile(r"github\.com/burstcash/glide/pull/(\d+)")

    def __init__(self) -> None:
        super().__init__()
        self.prs: dict[int, PR] = {}
        self._spawning: set[int] = set()
        self._updating: set[int] = set()

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield PRTable(id="pr-table")
            yield DetailPane(id="detail-pane")
        yield Input(placeholder="Paste GitHub PR URL or number…", id="url-input")
        yield Footer()

    def on_mount(self) -> None:
        self.theme = "solarized-dark"
        ensure_dirs()
        self.prs = load_state()
        self._refresh_table()

        # Background polling
        self.set_interval(60, self._poll_github)
        self.set_interval(10, self._check_tab_health)
        self.set_interval(120, self._check_review_status)

        # Immediate first poll
        self._poll_github()

    def _get_selected_pr(self) -> PR | None:
        table = self.query_one("#pr-table", PRTable)
        if table.row_count == 0:
            return None
        try:
            cursor_row = table.cursor_row
            row_key = table.ordered_rows[cursor_row].key
            return self.prs.get(int(row_key.value))
        except (IndexError, ValueError, AttributeError):
            return None

    def _refresh_table(self) -> None:
        table = self.query_one("#pr-table", PRTable)
        table.update_prs(self.prs)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value:
            pr = self.prs.get(int(event.row_key.value))
            self.query_one("#detail-pane", DetailPane).show_pr(pr)

    # --- Actions ---

    def action_spawn(self) -> None:
        pr = self._get_selected_pr()
        if pr is None:
            self.notify("No PR selected", severity="warning")
            return
        if pr.status == Status.ACTIVE:
            self._do_focus_tab(pr)
            return
        self._spawn_pr(pr)

    def action_focus_tab(self) -> None:
        pr = self._get_selected_pr()
        if pr:
            self._do_focus_tab(pr)

    def action_respawn(self) -> None:
        pr = self._get_selected_pr()
        if pr is None:
            self.notify("No PR selected", severity="warning")
            return
        self._do_respawn(pr)

    def action_update(self) -> None:
        pr = self._get_selected_pr()
        if pr is None:
            self.notify("No PR selected", severity="warning")
            return
        self._do_update(pr)

    def action_dismiss(self) -> None:
        pr = self._get_selected_pr()
        if pr is None:
            self.notify("No PR selected", severity="warning")
            return
        self._do_dismiss(pr)

    def action_open_in_browser(self) -> None:
        pr = self._get_selected_pr()
        if pr is None:
            self.notify("No PR selected", severity="warning")
            return
        webbrowser.open(pr.url)

    def action_force_refresh(self) -> None:
        self._poll_github()

    def action_add_pr(self) -> None:
        inp = self.query_one("#url-input", Input)
        if inp.has_class("visible"):
            inp.remove_class("visible")
            self.query_one("#pr-table", PRTable).focus()
        else:
            inp.value = ""
            inp.add_class("visible")
            inp.focus()

    def _parse_pr_number(self, text: str) -> int | None:
        text = text.strip()
        if text.isdigit():
            return int(text)
        m = self._URL_RE.search(text)
        return int(m.group(1)) if m else None

    def on_input_submitted(self, event: Input.Submitted) -> None:
        inp = self.query_one("#url-input", Input)
        inp.remove_class("visible")
        self.query_one("#pr-table", PRTable).focus()

        number = self._parse_pr_number(event.value)
        if number is None:
            self.notify("Invalid URL — expected a burstcash/glide PR URL or number", severity="error")
            return
        if number in self.prs:
            self.notify(f"PR #{number} is already in the list", severity="warning")
            return
        self._fetch_and_spawn(number)

    @work(group="add_pr")
    async def _fetch_and_spawn(self, number: int) -> None:
        try:
            pr = await github.fetch_pr(number)
        except RuntimeError as e:
            self.notify(str(e), severity="error")
            return
        pr.last_updated = datetime.now()
        self.prs[pr.number] = pr
        save_state(self.prs)
        self._refresh_table()
        await self._spawn_pr_async(pr)

    # --- Workers ---

    @work(exclusive=True, group="github_poll")
    async def _poll_github(self) -> None:
        try:
            new_prs = await github.poll_review_requests()
        except RuntimeError as e:
            self.notify(f"GitHub error: {e}", severity="error")
            return

        new_numbers = {p.number for p in new_prs}
        now = datetime.now()

        # Detect new PRs
        for pr in new_prs:
            if pr.number not in self.prs:
                pr.last_updated = now
                self.prs[pr.number] = pr
                await self._spawn_pr_async(pr)
            else:
                self.prs[pr.number].last_updated = now

        # Detect PRs no longer in review requests
        for num, pr in list(self.prs.items()):
            if num not in new_numbers and pr.status not in (Status.DONE, Status.CLOSED):
                state = await github.check_pr_state(num)
                if state in ("MERGED", "CLOSED"):
                    await self._cleanup_pr(pr, state.lower())

        save_state(self.prs)
        self.call_from_thread(self._refresh_table) if not self.is_running else self._refresh_table()

    @work(exclusive=True, group="tab_health")
    async def _check_tab_health(self) -> None:
        tabs = await kitty.list_tabs()
        changed = False

        for pr in list(self.prs.values()):
            if pr.status not in (Status.ACTIVE, Status.IDLE):
                continue

            marker = f"PR #{pr.number}"
            tab = next((t for t in tabs if marker in t.get("title", "")), None)

            if tab is None:
                if pr.status == Status.ACTIVE:
                    pr.status = Status.IDLE
                    changed = True
            elif kitty.is_claude_running(tab):
                if pr.status != Status.ACTIVE:
                    pr.status = Status.ACTIVE
                    changed = True
            else:
                if pr.status != Status.IDLE:
                    pr.status = Status.IDLE
                    changed = True

        if changed:
            save_state(self.prs)
            self._refresh_table()

    @work(exclusive=True, group="review_status")
    async def _check_review_status(self) -> None:
        changed = False

        for pr in list(self.prs.values()):
            if pr.review_submitted:
                continue
            if pr.status not in (Status.ACTIVE, Status.IDLE):
                continue

            submitted = await github.check_review_submitted(pr.number)
            if submitted:
                pr.review_submitted = True
                pr.status = Status.REVIEW_SUBMITTED
                await kitty.update_tab_title(pr.number, pr.tab_title)
                changed = True

        if changed:
            save_state(self.prs)
            self._refresh_table()

    # --- Spawn / cleanup helpers ---

    @work(group="spawn")
    async def _spawn_pr(self, pr: PR) -> None:
        await self._spawn_pr_async(pr)

    async def _spawn_pr_async(self, pr: PR) -> None:
        if pr.number in self._spawning:
            return
        self._spawning.add(pr.number)

        try:
            pr.status = Status.SPAWNING
            save_state(self.prs)
            self._refresh_table()

            ok = await worktree.create_worktree(pr)
            if not ok:
                pr.status = Status.NEW
                self.notify(f"Failed to create worktree for PR #{pr.number}", severity="error")
                return

            write_prompt(pr)

            ok = await kitty.launch_tab(pr)
            if not ok:
                pr.status = Status.NEW
                self.notify(f"Failed to launch tab for PR #{pr.number}", severity="error")
                return

            pr.status = Status.ACTIVE
            pr.spawned_at = datetime.now()
            save_state(self.prs)
            self._refresh_table()

            await notify.notify("New PR for Review", f"PR #{pr.number}: {pr.title[:50]} by {pr.author}")
        finally:
            self._spawning.discard(pr.number)

    async def _cleanup_pr(self, pr: PR, reason: str) -> None:
        await kitty.close_tab(pr.number)
        await worktree.remove_worktree(pr)
        remove_prompt(pr)
        await notify.notify("PR Closed", f"PR #{pr.number} was {reason}")
        self.prs.pop(pr.number, None)

    @work(group="respawn")
    async def _do_respawn(self, pr: PR) -> None:
        await kitty.close_tab(pr.number)
        await worktree.remove_worktree(pr)
        pr.status = Status.NEW
        await self._spawn_pr_async(pr)

    @work(group="update")
    async def _do_update(self, pr: PR) -> None:
        if pr.number in self._updating:
            return
        self._updating.add(pr.number)
        try:
            state = await github.check_pr_state(pr.number)
            if state in ("MERGED", "CLOSED"):
                await self._cleanup_pr(pr, state.lower())
                save_state(self.prs)
                self._refresh_table()
                return

            await kitty.close_tab(pr.number)

            ok = await worktree.update_worktree(pr)
            if not ok:
                self.notify(f"Failed to update worktree for PR #{pr.number}", severity="error")
                return

            try:
                fresh = await github.fetch_pr(pr.number)
                pr.title = fresh.title
                pr.body = fresh.body
                pr.additions = fresh.additions
                pr.deletions = fresh.deletions
            except RuntimeError as e:
                log.warning("Failed to refetch PR #%d metadata: %s", pr.number, e)

            pr.review_submitted = False
            pr.last_updated = datetime.now()
            await self._spawn_pr_async(pr)
        finally:
            self._updating.discard(pr.number)

    @work(group="dismiss")
    async def _do_dismiss(self, pr: PR) -> None:
        await kitty.close_tab(pr.number)
        await worktree.remove_worktree(pr)
        remove_prompt(pr)
        self.prs.pop(pr.number, None)
        save_state(self.prs)
        self._refresh_table()

    @work(group="focus")
    async def _do_focus_tab(self, pr: PR) -> None:
        ok = await kitty.focus_tab(pr.number)
        if not ok:
            self.notify(f"No tab found for PR #{pr.number}", severity="warning")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        filename=os.path.expanduser("~/.pr-watcher/pr-watcher.log"),
    )
    app = PRWatcherApp()
    app.run()


if __name__ == "__main__":
    main()
