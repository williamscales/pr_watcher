from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pr_watcher.models import PR

STATE_DIR = Path("~/.pr-watcher").expanduser()
STATE_FILE = STATE_DIR / "state.json"
PROMPTS_DIR = STATE_DIR / "prompts"

def ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)


def load_state() -> dict[int, PR]:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text())
        return {int(k): PR.from_dict(v) for k, v in data.items()}
    except (json.JSONDecodeError, KeyError, ValueError):
        return {}


def save_state(prs: dict[int, PR]) -> None:
    ensure_dirs()
    data = {str(k): v.to_dict() for k, v in prs.items()}
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except BaseException:
        os.unlink(tmp)
        raise


def write_prompt(pr: PR) -> None:
    ensure_dirs()
    prompt = f"/pr-review {pr.number}\n"
    Path(pr.prompt_path).write_text(prompt)


def remove_prompt(pr: PR) -> None:
    path = Path(pr.prompt_path)
    if path.exists():
        path.unlink()
