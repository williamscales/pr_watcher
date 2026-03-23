from __future__ import annotations

import logging

from pr_watcher.services import run_cmd

log = logging.getLogger(__name__)


async def notify(title: str, message: str) -> None:
    safe_title = title.replace('"', '\\"')
    safe_msg = message.replace('"', '\\"')
    await run_cmd(
        "osascript", "-e",
        f'display notification "{safe_msg}" with title "{safe_title}" sound name "Glass"',
    )
