# pr-watcher

A terminal UI for `burstcash/glide` with two tabs (switch with `1` / `2`):

- **PRs** — polls GitHub for PRs awaiting your review and automatically spawns [Claude Code](https://claude.com/claude-code) review sessions in [kitty](https://sw.kovidgoyal.net/kitty/) tabs.
- **Notifications** — your GitHub notifications inbox for the repo, for quick triage.

## How it works

### PRs

1. Polls `burstcash/glide` for PRs where your review is requested
2. For each new PR, creates a git worktree and opens a kitty tab running Claude Code with the `/pr-review` skill
3. Tracks PR lifecycle — spawning, active review, idle, review submitted, closed/merged
4. Automatically cleans up worktrees and tabs when PRs are closed or merged
5. Sends macOS notifications for new and closed PRs

### Notifications

1. Polls your `burstcash/glide` GitHub notifications every 30 seconds
2. Lists them with unread (●, bold) and read (○, dim) state, newest first
3. Sends a macOS notification when new ones arrive
4. `Enter` opens the target in your browser and marks it read on GitHub; `x` marks it done (removes it from your inbox); `u` flags it unread

> GitHub's API has no "mark as unread" endpoint, so `u` is local to this app — it won't change the unread state on github.com.

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

Global: `1` / `2` switch tabs, `j` / `k` move the cursor, `Shift+R` force-refreshes both tabs, `q` quits.

**PRs tab**

| Key | Action |
|-----|--------|
| `o` | Spawn a Claude Code review session for the selected PR (focuses its tab if already active) |
| `c` | Continue — resume the worktree's most recent Claude session (`claude --continue`), in the PR's existing tab if it has one sitting at a shell, otherwise in a new tab |
| `f` / `Enter` | Focus the kitty tab for the selected PR |
| `r` | Re-spawn — close and restart the review session |
| `u` | Update — sync the worktree to the latest PR head and re-spawn |
| `a` | Add a PR by URL or number |
| `b` | Open the selected PR in your browser |
| `d` | Dismiss — remove the PR from the list and clean up |

**Notifications tab**

| Key | Action |
|-----|--------|
| `Enter` | Open the notification's target in your browser and mark it read on GitHub |
| `u` | Mark unread (local to this app) |
| `x` | Mark done on GitHub — removes it from your inbox |

## State

Runtime state is stored in `~/.pr-watcher/`:

- `state.json` — tracked PRs and their statuses
- `notifications.json` — cached notifications with local read/unread overrides
- `prompts/` — generated prompt files passed to Claude Code
- `pr-watcher.log` — application log
