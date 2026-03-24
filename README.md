# pr-watcher

A terminal UI that polls GitHub for PRs awaiting your review and automatically spawns [Claude Code](https://claude.com/claude-code) review sessions in [kitty](https://sw.kovidgoyal.net/kitty/) tabs.

## How it works

1. Polls `burstcash/glide` for PRs where your review is requested
2. For each new PR, creates a git worktree and opens a kitty tab running Claude Code with the `/pr-review` skill
3. Tracks PR lifecycle — spawning, active review, idle, review submitted, closed/merged
4. Automatically cleans up worktrees and tabs when PRs are closed or merged
5. Sends macOS notifications for new and closed PRs

## Requirements

- Python 3.12+
- [kitty](https://sw.kovidgoyal.net/kitty/) terminal with remote control enabled
- [GitHub CLI](https://cli.github.com/) (`gh`) authenticated
- [Claude Code](https://claude.com/claude-code) CLI installed at `~/.local/bin/claude`
- A bare clone of `burstcash/glide` at `~/src/glide`

## Install

```
uv pip install -e .
```

## Usage

Run from within a kitty terminal:

```
pr-watcher
```

### Keybindings

| Key | Action |
|-----|--------|
| `o` | Spawn a Claude Code review session for the selected PR |
| `f` / `Enter` | Focus the kitty tab for the selected PR |
| `r` | Re-spawn — close and restart the review session |
| `d` | Dismiss — remove the PR from the list and clean up |
| `Shift+R` | Force refresh from GitHub |
| `q` | Quit |

## State

Runtime state is stored in `~/.pr-watcher/`:

- `state.json` — tracked PRs and their statuses
- `prompts/` — generated prompt files passed to Claude Code
- `pr-watcher.log` — application log
