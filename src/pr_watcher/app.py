from __future__ import annotations

import asyncio
import logging
import os
import re
import webbrowser
from datetime import datetime

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, Input, DataTable, TabbedContent, TabPane
from textual.containers import Vertical
from textual import work

from pr_watcher.models import PR, Status, Notification
from pr_watcher.services import github, kitty, worktree, notify
from pr_watcher.state import (
    load_state,
    save_state,
    write_prompt,
    remove_prompt,
    ensure_dirs,
    load_notifications,
    save_notifications,
)
from pr_watcher.widgets.detail_pane import DetailPane
from pr_watcher.widgets.pr_table import PRTable
from pr_watcher.widgets.notif_table import NotifTable
from pr_watcher.widgets.notif_detail import NotifDetail
from pr_watcher.widgets.confirm import ConfirmScreen

log = logging.getLogger(__name__)

APP_CSS = """
Screen {
    layout: vertical;
}

TabbedContent {
    height: 1fr;
}

#pr-table, #notif-table {
    height: 1fr;
    min-height: 8;
}

#detail-pane, #notif-detail {
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
        Binding("1", "show_tab('prs-tab')", "PRs"),
        Binding("2", "show_tab('notifs-tab')", "Notifs"),
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

    _PR_TAB_ACTIONS = frozenset(
        {"spawn", "focus_tab", "respawn", "update", "dismiss", "open_in_browser", "add_pr"}
    )

    def __init__(self) -> None:
        super().__init__()
        self.prs: dict[int, PR] = {}
        self.notifs: dict[str, Notification] = {}
        self._spawning: set[int] = set()
        self._updating: set[int] = set()
        self._first_notif_poll = True

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="prs-tab"):
            with TabPane("PRs", id="prs-tab"):
                with Vertical():
                    yield PRTable(id="pr-table")
                    yield DetailPane(id="detail-pane")
            with TabPane("Notifications", id="notifs-tab"):
                with Vertical():
                    yield NotifTable(id="notif-table")
                    yield NotifDetail(id="notif-detail")
        yield Input(placeholder="Paste GitHub PR URL or number…", id="url-input")
        yield Footer()

    def on_mount(self) -> None:
        self.theme = "solarized-dark"
        ensure_dirs()
        self.prs = load_state()
        self.notifs = load_notifications()
        self._refresh_table()
        self._refresh_notif_table()

        # Background polling
        self.set_interval(60, self._poll_github)
        self.set_interval(10, self._check_tab_health)
        self.set_interval(120, self._check_review_status)
        self.set_interval(30, self._poll_notifications)

        # Immediate first poll
        self._poll_github()
        self._poll_notifications()

        self.query_one("#pr-table", PRTable).focus()

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

    def _refresh_notif_table(self) -> None:
        self.query_one("#notif-table", NotifTable).update_notifs(self.notifs)

    def _selected_notif(self) -> Notification | None:
        tid = self.query_one("#notif-table", NotifTable).selected_id()
        return self.notifs.get(tid) if tid else None

    def _refresh_notif_detail(self) -> None:
        self.query_one("#notif-detail", NotifDetail).show_notification(self._selected_notif())

    def _prs_active(self) -> bool:
        return self.query_one(TabbedContent).active == "prs-tab"

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if self.query_one(TabbedContent).active == "notifs-tab":
            self.query_one("#notif-table", NotifTable).focus()
        else:
            self.query_one("#pr-table", PRTable).focus()
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in self._PR_TAB_ACTIONS and not self._prs_active():
            return None
        return True

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not (event.row_key and event.row_key.value):
            return
        if event.data_table.id == "notif-table":
            n = self.notifs.get(event.row_key.value)
            self.query_one("#notif-detail", NotifDetail).show_notification(n)
        else:
            pr = self.prs.get(int(event.row_key.value))
            self.query_one("#detail-pane", DetailPane).show_pr(pr)

    # --- Actions ---

    def action_show_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

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
        self._poll_notifications()

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
                    await self._retire_pr(pr, state.lower())

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

    async def _retire_pr(self, pr: PR, reason: str) -> None:
        """A PR merged/closed on GitHub. Never destroy a live/dirty worktree for it."""
        ws = await worktree.inspect_worktree(pr)
        tab_open = await kitty.in_use(pr.number)

        # Only auto-remove when there is provably nothing to lose: no open tab,
        # a clean tree, and nothing unpushed. `in_use` fails closed, so any doubt
        # about a live session keeps the worktree.
        if not tab_open and not ws.has_work:
            await worktree.remove_worktree(pr)  # force=False; safe by construction
            remove_prompt(pr)
            self.prs.pop(pr.number, None)
            await notify.notify(
                "PR Closed", f"PR #{pr.number} was {reason} — clean worktree removed"
            )
            return

        # Something is open or unsaved: keep the tab and worktree exactly as they are.
        pr.status = Status.DONE if reason == "merged" else Status.CLOSED
        await kitty.update_tab_title(pr.number, pr.tab_title)
        await notify.notify(
            f"PR {reason.capitalize()}",
            f"PR #{pr.number} was {reason} — press d to dismiss when ready (work preserved)",
        )

    async def _confirm_destroy(
        self,
        pr: PR,
        ws: worktree.WorkState,
        session: str | None,
        *,
        action: str,
        consequence: str,
    ) -> bool:
        """Prompt before discarding work. Returns True only on explicit confirmation."""
        at_risk = ws.describe()
        if session is not None:
            at_risk.append(session)
        if not at_risk:
            return True  # nothing would be lost — no need to interrupt

        items = "\n".join(f"  • {item}" for item in at_risk)
        message = (
            f"[bold]{action} PR #{pr.number}?[/bold]\n\n"
            f"{consequence}\n\n"
            f"This permanently discards:\n{items}\n\n"
            f"[dim]y = discard    n / Esc = keep[/dim]"
        )
        return await self.push_screen_wait(ConfirmScreen(message))

    @work(group="respawn")
    async def _do_respawn(self, pr: PR) -> None:
        ws = await worktree.inspect_worktree(pr)
        session = await kitty.live_session_label(pr.number)
        confirmed = False
        if ws.has_work or session is not None:
            confirmed = await self._confirm_destroy(
                pr, ws, session,
                action="Re-spawn",
                consequence="The worktree will be rebuilt from origin and the session restarted.",
            )
            if not confirmed:
                return

        await kitty.close_tab(pr.number)
        outcome = await worktree.remove_worktree(pr, force=confirmed)
        if outcome is not worktree.RemoveOutcome.REMOVED:
            self.notify(
                f"Re-spawn aborted — worktree for PR #{pr.number} kept (see log)",
                severity="warning",
            )
            return
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
                await self._retire_pr(pr, state.lower())
                save_state(self.prs)
                self._refresh_table()
                return

            ws = await worktree.inspect_worktree(pr)
            session = await kitty.live_session_label(pr.number)
            confirmed = False
            if ws.has_work or session is not None:
                confirmed = await self._confirm_destroy(
                    pr, ws, session,
                    action="Update",
                    consequence="The worktree will be reset to origin and the session restarted.",
                )
                if not confirmed:
                    return

            await kitty.close_tab(pr.number)

            ok = await worktree.update_worktree(pr, force=confirmed)
            if not ok:
                self.notify(
                    f"Update aborted — worktree for PR #{pr.number} kept (see log)",
                    severity="warning",
                )
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
        ws = await worktree.inspect_worktree(pr)
        session = await kitty.live_session_label(pr.number)
        confirmed = False
        if ws.has_work or session is not None:
            confirmed = await self._confirm_destroy(
                pr, ws, session,
                action="Dismiss",
                consequence="The worktree and its tab will be removed.",
            )
            if not confirmed:
                return

        await kitty.close_tab(pr.number)
        outcome = await worktree.remove_worktree(pr, force=confirmed)
        if outcome is not worktree.RemoveOutcome.REMOVED:
            self.notify(
                f"Worktree for PR #{pr.number} was kept (see log)", severity="warning"
            )
            return
        remove_prompt(pr)
        self.prs.pop(pr.number, None)
        save_state(self.prs)
        self._refresh_table()

    @work(group="focus")
    async def _do_focus_tab(self, pr: PR) -> None:
        ok = await kitty.focus_tab(pr.number)
        if not ok:
            self.notify(f"No tab found for PR #{pr.number}", severity="warning")

    # --- Notifications ---

    @work(exclusive=True, group="notif_poll")
    async def _poll_notifications(self) -> None:
        try:
            fresh = await github.poll_notifications()
        except RuntimeError as e:
            self.notify(f"GitHub notifications error: {e}", severity="error")
            return

        fresh_by_id = {n.thread_id: n for n in fresh}
        new_ids: list[str] = []

        for tid, n in fresh_by_id.items():
            existing = self.notifs.get(tid)
            if existing is None:
                self.notifs[tid] = n
                new_ids.append(tid)
            elif n.updated_at > existing.updated_at:
                # New activity on the thread — adopt the server state afresh.
                self.notifs[tid] = n
                if n.unread:
                    new_ids.append(tid)
            else:
                # Unchanged — preserve the local unread override and cached URL.
                existing.title = n.title
                existing.reason = n.reason
                existing.subject_type = n.subject_type
                existing.subject_api_url = n.subject_api_url
                existing.repo_url = n.repo_url

        for tid in list(self.notifs):
            if tid not in fresh_by_id:
                del self.notifs[tid]

        save_notifications(self.notifs)
        self._refresh_notif_table()
        self._refresh_notif_detail()

        if new_ids and not self._first_notif_poll:
            await self._alert_new_notifications(new_ids)
        self._first_notif_poll = False

    async def _alert_new_notifications(self, new_ids: list[str]) -> None:
        if len(new_ids) == 1:
            n = self.notifs.get(new_ids[0])
            if n is not None:
                await notify.notify(
                    "New GitHub Notification",
                    f"{n.title[:60]} ({n.reason.replace('_', ' ')})",
                )
        else:
            await notify.notify("New GitHub Notifications", f"{len(new_ids)} new notifications")

    @work(group="notif_open")
    async def do_notif_open(self, thread_id: str) -> None:
        n = self.notifs.get(thread_id)
        if n is None:
            return
        if not n.html_url:
            n.html_url = await github.resolve_notification_url(n)
        webbrowser.open(n.html_url)
        if await github.mark_notification_read(thread_id):
            n.unread = False
        save_notifications(self.notifs)
        self._refresh_notif_table()
        self._refresh_notif_detail()

    def do_notif_unread(self, thread_id: str) -> None:
        n = self.notifs.get(thread_id)
        if n is None:
            return
        n.unread = True
        save_notifications(self.notifs)
        self._refresh_notif_table()
        self._refresh_notif_detail()

    @work(group="notif_done")
    async def do_notif_done(self, thread_id: str) -> None:
        n = self.notifs.get(thread_id)
        if n is None:
            return
        if await github.mark_notification_done(thread_id):
            self.notifs.pop(thread_id, None)
            save_notifications(self.notifs)
            self._refresh_notif_table()
            self._refresh_notif_detail()
        else:
            self.notify("Failed to mark notification done", severity="error")


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
