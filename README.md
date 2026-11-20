# ai-router (`ai-route`)

A unified CLI that classifies coding task difficulty with a lightweight OpenCode model and orchestrates multiple AI coding agents (**opencode**, **agy**, **claude**) using `git worktree` and `tmux`.

## Features

- **Automatic Task Routing (`run`)**: Evaluates task complexity and selects an appropriate `(tool, model)` pair, optionally creating an isolated `git worktree` and launching in a dedicated `tmux` window or split pane.
- **In-Place Model Handoff (`handoff`)**: Transfers current worktree diffs and context to another model in a horizontal split pane while recording handoff lineage.
- **Parallel Multi-Model Review (`review`)**: Runs multiple models concurrently over diffs or files in either `headless` (with optional synthesized summary) or `interactive` tiled tmux panes.
- **Parallel Multi-Model Solve, Compare & Pick (`multi` / `compare` / `pick`)**: Spawns multiple models on isolated worktrees from a common base commit, compares commits, diff stats, and test outcomes side by side, and merges the winning branch while cleaning up the worktrees and `ai/` branches.
- **Analytics & Worktree Lifecycle (`stats` / `list` / `clean`)**: Logs every routing, handoff, review, and multi-solve event to a locked JSONL file and provides summary analytics and worktree cleanup.

## Prerequisites

- Python `>= 3.11` (managed with [`uv`](https://docs.astral.sh/uv/))
- `git` and `tmux`
- Installed agent CLIs as needed:
  - `opencode` (used for automatic routing and `opencode` tasks)
  - `agy` (Antigravity CLI)
  - `claude` (Claude Code CLI)

## Installation

Both `ai-route` and `ai-router` are registered as CLI entrypoints.

### Install globally as a CLI tool (recommended)

```bash
# Editable install (picks up local edits automatically)
uv tool install --editable .

# Standard install
uv tool install .
```

### Run without global installation

```bash
uv run ai-route --help
# or
uv run python -m ai_router --help
```

## Quick Start & Commands

### 1. `run` — Route and dispatch a single task

```bash
# Auto-route and run in a new isolated git worktree
ai-route run -w "Fix edge cases in the CSV parser"

# Pass target/reference files and read the task from a file (or '-' for stdin)
ai-route run -w -f src/parser.py -F task.md

# Preview the routing decision and refined prompt without launching
ai-route run --dry-run -f src/parser.py "Add streaming support"

# Skip automatic routing and force a specific tool/model
ai-route run --tool agy --model gemini-4-neon-high -w "Refactor authentication middleware"
```

### 2. `handoff` — Escalate or transfer an active worktree to another model

```bash
# Run from inside the active worktree
ai-route handoff --tool claude --model opus "Investigate the failing concurrency test"
ai-route handoff --tool agy --model gemini-4-neon-high -f src/worker.py "Review deadlock fix"
```

### 3. `review` — Parallel multi-model code review

```bash
# Headless review of git diff HEAD using the 'balanced' preset + synthesized summary
ai-route review --synthesize

# Review staged changes with distinct reviewer perspectives across the 'strong' preset
ai-route review --staged --preset strong --roles --synthesize

# Review specific files with explicit models in interactive tiled tmux panes
ai-route review -f src/api.py --models "claude:sonnet,agy:gemini-3.8-flash-medium" --mode interactive
```

### 4. `multi`, `compare` & `pick` — Parallel solving in isolated worktrees

```bash
# Launch multiple models on separate worktrees from HEAD
ai-route multi --preset strong -f src/scheduler.py "Optimize task scheduling throughput"

# Compare commits, diffs, and test results across the latest multi group
ai-route compare --test-cmd "pytest -q"

# Compare a specific group ID
ai-route compare a1b2c3 --test-cmd "uv run python -m unittest"

# Interactively pick the winning branch from the latest multi group, merge it, and clean up
ai-route pick

# Pick member #2 from the latest group (or specify group_id and index explicitly)
ai-route pick 2
ai-route pick a1b2c3 2

# Preview merge & cleanup without modifying git, or use squash merge / force cleanup
ai-route pick a1b2c3 2 --dry-run
ai-route pick a1b2c3 2 --squash --force
ai-route pick a1b2c3 2 --keep-winner
```

### 5. `list`, `clean`, and `stats`

```bash
# List active git worktrees
ai-route list

# Remove a finished worktree and its ai/ branch
ai-route clean ../my-repo-worktrees/fix-parser-1121-001000 --delete-branch

# View 30-day usage statistics (or filter to the current repo)
ai-route stats --here --days 30
```

## Models & Presets

Configured in [`src/ai_router/config.py`](src/ai_router/config.py):

| Difficulty | Tool | Model | Intended Use |
| :--- | :--- | :--- | :--- |
| `low` | `opencode` | `opencode/deepseek-v4.1-flash` | Minor fixes, boilerplate, single-file tests, doc updates |
| `medium` | `agy` | `gemini-3.8-flash-medium` | Mid-sized implementation, multi-file exploration/refactoring |
| `medium` | `claude` | `sonnet` | Complex logic, precise edits, debugging |
| `high` | `agy` | `gemini-4-neon-high` | High-complexity design, large cross-module refactoring |
| `high` | `claude` | `opus` | Architecture design, deep bug analysis, broad breaking changes |

### Presets (`--preset`)

- `fast`: `opencode:opencode/deepseek-v4.1-flash`, `agy:gemini-3.8-flash-medium`
- `balanced` *(default)*: `agy:gemini-3.8-flash-medium`, `claude:sonnet`
- `strong`: `claude:opus`, `agy:gemini-4-neon-high`, `claude:sonnet`
- `all`: All configured candidates

> Tip: Run `agy models` to inspect available Antigravity model IDs on your machine.

## Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `AI_ROUTE_ROUTER_MODEL` | `opencode/deepseek-v4.1-flash` | Lightweight OpenCode model used for routing and default review synthesis |
| `OPENCODE_ATTACH_URL` | *(empty)* | Optional `--attach` server URL passed to `opencode run` |
| `AI_ROUTE_LOG_FILE` | `$XDG_DATA_HOME/ai-route/history.jsonl` (fallback `~/.local/share/ai-route/history.jsonl`) | Path to the JSONL event history file |
| `AI_ROUTE_AGY_PASTE_FALLBACK` | `false` | Set to `1` / `true` to use legacy tmux paste-buffer injection instead of `agy --prompt-interactive` |
| `AI_ROUTE_AGY_DELAY` | `2.0` | Delay in seconds before pasting into `agy` when `AI_ROUTE_AGY_PASTE_FALLBACK` is enabled |

## Project Structure

```text
ai-router/
├── pyproject.toml              # Package metadata & CLI entrypoints (ai-route, ai-router)
├── README.md
├── src/
│   └── ai_router/
│       ├── __init__.py         # Re-exports main()
│       ├── __main__.py         # Enables `python -m ai_router`
│       ├── cli.py              # argparse definitions & CLI entrypoint
│       ├── config.py           # Env vars, model candidates, presets, fallback config
│       ├── models.py           # Core dataclasses (RouteDecision, ResolvedFile, DispatchEntry)
│       ├── utils.py            # Subprocess, git metadata, slug/JSON/spec helpers
│       ├── files.py            # File resolution, router previews, task text loading
│       ├── router.py           # Task difficulty routing via OpenCode
│       ├── worktree.py         # git worktree creation & untracked env file copying
│       ├── storage.py          # File-locked JSONL log & per-worktree session metadata
│       ├── dispatch.py         # Agent shell command builders & tmux dispatchers
│       └── commands/
│           ├── run.py          # `run` and `handoff` handlers
│           ├── review.py       # `review` handler (headless + interactive + synthesis)
│           ├── multi.py        # `multi`, `compare`, and `pick` handlers
│           ├── worktree.py     # `list` and `clean` handlers
│           └── stats.py        # `stats` handler
├── tests/                      # Unit tests (stdlib unittest)
└── snippets/                   # Maintenance scratchpad for snippets drafted in external chats
    ├── README.md
    └── archive/                # Archived prototypes kept for reference
```

### Maintenance Workflow (`snippets/`)

When prototyping new features or ideas in separate AI chats, save the raw snippets in `snippets/` (see [`snippets/README.md`](snippets/README.md)). Review them against the modular layout in `src/ai_router/`, integrate clean implementations with tests, and move superseded drafts into `snippets/archive/`.

## Development & Testing

```bash
# Sync virtual environment
uv sync

# Run unit tests
uv run python -m unittest discover -s tests -v
```
