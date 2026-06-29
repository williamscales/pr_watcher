from __future__ import annotations

from rich.markup import escape
from textual.widgets import Static

from pr_watcher.models import Notification


class NotifDetail(Static):

    def show_notification(self, n: Notification | None) -> None:
        if n is None:
            self.update("No notification selected")
            return

        state = "unread" if n.unread else "read"
        reason = n.reason.replace("_", " ")
        url = n.html_url or n.subject_api_url or n.repo_url
        self.update(
            f"[bold]{escape(n.title)}[/bold]\n"
            f"{escape(n.repo)}  ·  {escape(reason)}  ·  {escape(n.subject_type)}  ·  {state}\n"
            f"{escape(url)}"
        )
