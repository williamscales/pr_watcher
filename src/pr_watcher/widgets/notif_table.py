from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.widgets import DataTable

from pr_watcher.models import Notification
from pr_watcher.widgets.pr_table import format_age


class NotifTable(DataTable):

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("enter", "open", "Open"),
        Binding("u", "mark_unread", "Unread"),
        Binding("x", "mark_done", "Done"),
    ]

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.add_column("", width=2, key="dot")
        self.add_column("Reason", width=18, key="reason")
        self.add_column("Title", width=52, key="title")
        self.add_column("Age", width=6, key="age")

    def selected_id(self) -> str | None:
        if self.row_count == 0:
            return None
        try:
            return self.ordered_rows[self.cursor_row].key.value
        except (IndexError, AttributeError):
            return None

    def update_notifs(self, notifs: dict[str, Notification]) -> None:
        selected = self.selected_id()
        self.clear()

        ordered = sorted(notifs.values(), key=lambda n: n.updated_at, reverse=True)
        for n in ordered:
            style = "bold" if n.unread else "dim"
            short_title = n.title[:50] + ("…" if len(n.title) > 50 else "")
            self.add_row(
                Text("●" if n.unread else "○", style=style),
                Text(n.reason.replace("_", " "), style=style),
                Text(short_title, style=style),
                Text(format_age(n.updated_at), style=style),
                key=n.thread_id,
            )

        if selected is not None:
            for idx, row in enumerate(self.ordered_rows):
                if row.key.value == selected:
                    self.move_cursor(row=idx)
                    break

    def action_open(self) -> None:
        tid = self.selected_id()
        if tid:
            self.app.do_notif_open(tid)

    def action_mark_unread(self) -> None:
        tid = self.selected_id()
        if tid:
            self.app.do_notif_unread(tid)

    def action_mark_done(self) -> None:
        tid = self.selected_id()
        if tid:
            self.app.do_notif_done(tid)
