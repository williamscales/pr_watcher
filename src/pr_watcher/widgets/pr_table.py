from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual.binding import Binding
from textual.widgets import DataTable

from pr_watcher.models import PR, Status

STATUS_STYLES: dict[Status, str] = {
    Status.NEW: "cyan",
    Status.SPAWNING: "yellow",
    Status.ACTIVE: "green",
    Status.IDLE: "dim",
    Status.REVIEW_SUBMITTED: "magenta",
    Status.DONE: "green bold",
    Status.CLOSED: "red",
}


def _format_age(dt: datetime | None) -> str:
    if dt is None:
        return "-"
    delta = datetime.now() - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


class PRTable(DataTable):

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.add_column("#", width=6, key="number")
        self.add_column("Title", width=42, key="title")
        self.add_column("Author", width=16, key="author")
        self.add_column("Status", width=14, key="status")
        self.add_column("Age", width=8, key="age")

    def update_prs(self, prs: dict[int, PR]) -> None:
        # Remember selected PR number
        selected_pr: int | None = None
        if self.row_count > 0:
            try:
                cursor_row = self.cursor_row
                row_key = self.ordered_rows[cursor_row].key
                selected_pr = int(row_key.value)
            except (IndexError, ValueError, AttributeError):
                pass

        self.clear()

        sorted_prs = sorted(prs.values(), key=lambda p: p.number, reverse=True)
        for pr in sorted_prs:
            style = STATUS_STYLES.get(pr.status, "")
            status_text = Text(pr.status.value, style=style)
            short_title = pr.title[:40] + ("\u2026" if len(pr.title) > 40 else "")
            self.add_row(
                str(pr.number),
                short_title,
                pr.author,
                status_text,
                _format_age(pr.spawned_at or pr.last_updated),
                key=str(pr.number),
            )

        # Restore cursor position
        if selected_pr is not None:
            for idx, row in enumerate(self.ordered_rows):
                if row.key.value == str(selected_pr):
                    self.move_cursor(row=idx)
                    break
