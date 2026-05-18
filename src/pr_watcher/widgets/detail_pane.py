from __future__ import annotations

from rich.markup import escape
from textual.widgets import Static

from pr_watcher.models import PR, STATUS_ICONS


class DetailPane(Static):

    def show_pr(self, pr: PR | None) -> None:
        if pr is None:
            self.update("No PR selected")
            return

        icon = STATUS_ICONS.get(pr.status, "?")
        body_preview = (pr.body or "(no description)")[:500]
        if len(pr.body or "") > 500:
            body_preview += "\u2026"

        self.update(
            f"[bold]#{pr.number}[/bold] {escape(pr.title)}\n"
            f"Author: {escape(pr.author)}  Branch: [cyan]{escape(pr.branch)}[/cyan]\n"
            f"{pr.url}\n"
            f"+{pr.additions} -{pr.deletions}  {icon} {escape(pr.status.value)}\n"
            f"\n{escape(body_preview)}"
        )
