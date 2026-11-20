# ai-router (`ai-route`)

A unified CLI that classifies coding task difficulty with a lightweight OpenCode model and orchestrates multiple AI coding agents (**opencode**, **agy**, **claude**) using `git worktree` and `tmux`.

## Features

- **Automatic Task Routing (`run`)**: Evaluates task complexity and selects an appropriate `(tool, model)` pair, optionally creating an isolated `git worktree` and launching in a dedicated `tmux` window or split pane.
- **In-Place Model Handoff (`handoff`)**: Transfers current worktree diffs and context to another model in a horizontal split pane while recording handoff lineage.
- **Parallel Multi-Model Review (`review`)**: Runs multiple models concurrently over diffs or files in either `headless` (with optional synthesized summary) or `interactive` tiled tmux panes.
- **Parallel Multi-Model Solve, Compare & Pick (`multi` / `compare` / `pick`)**: Spawns multiple models on isolated worktrees from a common base commit, compares commits, diff stats, and test outcomes side by side, and merges the winning branch while cleaning up the worktrees and `ai/` branches.
- **Blocked Detection & Escalation (`run --wait` / `status` / `escalate`)**: Detects agents that stopped waiting for a human decision (explicit marker, question-like output, or exit code), escalates the decision to a stronger model without letting it edit files, and resumes the implementer with the decision.
- **Decision / Implementation Split (`run --two-stage`, `review --then-implement`)**: A strong planner fixes decisions and steps as JSON; a cheaper implementer executes them.
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

# Run headless *synchronously* in the current shell and detect blocking.
# A blocked agent is escalated automatically (claude:opus, decision only) and resumed once.
ai-route run --mode headless --wait "Add pagination to /users"
ai-route run --mode headless --wait --no-auto-escalate --agent-timeout 900 "..."

# Two-stage: planner (decisions only, no edits) -> implementer chosen by planned difficulty
ai-route run --two-stage -w "Introduce a caching layer for the API client"
ai-route run --two-stage --plan-preset cheap --mode headless --wait "Add CSV export"
ai-route run --two-stage --planner claude:opus --implementer agy "..."   # bare tool = model by difficulty
```

The default (`tmux`) behavior is unchanged; in addition, every launched agent's output is
logged to `<log dir>/sessions/<session_id>.log` (headless: `tee`, interactive: `tmux pipe-pane`)
so `ai-route status` can detect blocking afterwards.

### 2. `handoff` — Escalate or transfer an active worktree to another model

```bash
# Run from inside the active worktree
ai-route handoff --tool claude --model opus "Investigate the failing concurrency test"
ai-route handoff --tool agy --model gemini-4-neon-high -f src/worker.py "Review deadlock fix"

# Build the handoff from saved session state (task, refined prompt, blocked question,
# output summary, escalation decision, diff). The note becomes optional.
ai-route handoff --from-session 1a2b3c4d --tool claude --model opus
ai-route handoff --from-session            # current worktree's session
```

### 2b. `status` & `escalate` — Detect blocked agents and get a decision

```bash
# Inspect the current worktree's session (or recent sessions / a session ID / a multi group)
ai-route status
ai-route status --from-session 1a2b3c4d --json
ai-route status --group a1b2c3

# Ask a stronger model to decide (default claude:opus; --tool alone picks its high model).
# The decision is stored in the session metadata (`decision`).
ai-route escalate --from-session 1a2b3c4d --note "Prefer backwards compatibility"

# ...and resume the session's implementer with the decision (tmux, or synchronously)
ai-route escalate --then-implement
ai-route escalate --tool agy --then-implement --mode headless --wait
```

Blocked detection (`detect_blocked`) checks, in order:
1. The explicit marker `[[AI_ROUTE_BLOCKED]]` followed by `質問:` / `選択肢:` / `推奨:` (synchronous headless prompts ask agents to print it).
2. The exit code: `124` → `timeout`, `127` → `launch_failed`, other non-zero → `blocked` only if the output ends with a question.
3. Japanese/English question patterns in the last lines of the output (extendable via `AI_ROUTE_BLOCKED_PATTERNS`).

Only `blocked` (not `timeout` / `launch_failed`) triggers escalation. `run --wait` exits with code 1 when the final status is not `completed`.

### 3. `review` — Parallel multi-model code review

```bash
# Headless review of git diff HEAD using the 'balanced' preset + synthesized summary
ai-route review --synthesize

# Review staged changes with distinct reviewer perspectives across the 'strong' preset
ai-route review --staged --preset strong --roles --synthesize

# Review specific files with explicit models in interactive tiled tmux panes
ai-route review -f src/api.py --models "claude:sonnet,agy:gemini-3.8-flash-medium" --mode interactive

# Use the synthesized review as the plan and launch an implementer (implies --synthesize)
ai-route review --then-implement                     # implementer chosen by planned difficulty
ai-route review --then-implement=claude:sonnet       # or 'tool:model' / bare 'tool'
ai-route review --then-implement --impl-mode headless --wait
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
| `low` | `opencode` | `opencode-go/deepseek-v4.1-flash` | Minor fixes, boilerplate, single-file tests, doc updates |
| `medium` | `agy` | `gemini-3.8-flash-medium` | Mid-sized implementation, multi-file exploration/refactoring |
| `medium` | `claude` | `sonnet` | Complex logic, precise edits, debugging |
| `high` | `agy` | `gemini-4-neon-high` | High-complexity design, large cross-module refactoring |
| `high` | `claude` | `opus` | Architecture design, deep bug analysis, broad breaking changes |

### Presets (`--preset`)

- `fast`: `opencode:opencode-go/deepseek-v4.1-flash`, `agy:gemini-3.8-flash-medium`
- `balanced` *(default)*: `agy:gemini-3.8-flash-medium`, `claude:sonnet`
- `strong`: `claude:opus`, `agy:gemini-4-neon-high`, `claude:sonnet`
- `all`: All configured candidates

> Tip: Run `agy models` to inspect available Antigravity model IDs on your machine.

### Two-stage flow & stage presets (`--plan-preset`)

```
planner (headless, decisions only, JSON)          implementer (follows --mode)
{decisions, constraints, open_questions,   --->   prompt: ## タスク / ## 決定事項 / ## 制約 /
 implementation_steps, implementer_difficulty}            ## 実装手順 / ## 未解決点
```

An invalid planner JSON falls back to a safe default plan (difficulty `medium`) and passes the planner's raw text as reference notes. `review --then-implement` uses the synthesizer as the planner.

| Preset | Planner | Implementer |
| :--- | :--- | :--- |
| `claude` | `claude:opus` | `claude:sonnet` |
| `cheap` | `claude:sonnet` | `opencode:opencode-go/deepseek-v4.1-flash` |
| `gemini` | `agy:gemini-4-neon-high` | `agy:gemini-3.8-flash-medium` |
| *(none)* | `claude:opus` | chosen from the candidates by `implementer_difficulty` |

Resolution order: CLI (`--planner` / `--implementer`) > `AI_ROUTE_PLANNER` / `AI_ROUTE_IMPLEMENTER` > preset > default. An implementer given as a bare tool name (e.g. `claude`) picks that tool's model for the planned difficulty.

## Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `AI_ROUTE_ROUTER_MODEL` | `opencode-go/deepseek-v4.1-flash` | Lightweight OpenCode model used for routing and default review synthesis |
| `OPENCODE_ATTACH_URL` | *(empty)* | Optional `--attach` server URL passed to `opencode run` |
| `AI_ROUTE_LOG_FILE` | `$XDG_DATA_HOME/ai-route/history.jsonl` (fallback `~/.local/share/ai-route/history.jsonl`) | Path to the JSONL event history file |
| `AI_ROUTE_AGY_PASTE_FALLBACK` | `false` | Set to `1` / `true` to use legacy tmux paste-buffer injection instead of `agy --prompt-interactive` |
| `AI_ROUTE_AGY_DELAY` | `2.0` | Delay in seconds before pasting into `agy` when `AI_ROUTE_AGY_PASTE_FALLBACK` is enabled |
| `AI_ROUTE_ROUTER_TIMEOUT` | `60` | Router call timeout in seconds; falls back to the default model on timeout (`<=0` disables). Overridden by `run --router-timeout` |
| `AI_ROUTE_REVIEW_TIMEOUT` | `300` | Per-model timeout for headless `review` and synthesis (`<=0` disables). Overridden by `review --timeout` |
| `AI_ROUTE_TEST_CMD_TIMEOUT` | `300` | Per-worktree timeout for `compare --test-cmd` (`<=0` disables). Overridden by `compare --timeout` |
| `AI_ROUTE_AGENT_TIMEOUT` | `1800` | Timeout for synchronous agent runs (`--wait`, planner, escalation) (`<=0` disables). Overridden by `--agent-timeout` |
| `AI_ROUTE_BLOCKED_PATTERNS` | *(empty)* | Extra regexes (separated by `;;`, case-insensitive) treated as "waiting for a decision" in output tails |
| `AI_ROUTE_PLANNER` | *(empty)* | Planner `tool:model` for `run --two-stage` (overrides presets) |
| `AI_ROUTE_IMPLEMENTER` | *(empty)* | Implementer `tool:model` or bare tool for `--two-stage` / `--then-implement` |

## History Events (`history.jsonl`)

Existing events (`route`, `handoff`, `review`, `multi`, `pick`) keep their fields; new fields are only added.

| Event | Recorded when | Key fields |
| :--- | :--- | :--- |
| `route` | A session starts | + `stage` (`single`/`implementer`), `parent_session`, `wait` |
| `agent_result` | A synchronous agent run finishes | `rc`, `latency_ms`, `status` (`completed`/`blocked`/`timeout`/`launch_failed`/`failed`), `stage` |
| `blocked` | A block is detected (`source`: `wait` or `status`) | `kind` (`marker`/`pattern`/`exit`), `reason`, `question`, `options`, `recommendation`, `excerpt` |
| `escalated` | A decision is requested | `trigger` (`manual`/`auto`), `from_tool`/`from_model`, `tool`/`model`, `plan_valid`, `status` |
| `two_stage` | A planner finishes (`source`: `run` or `review`) | `status` (`planned`/`planner_failed`), planner/implementer models, `plan_valid` |
| `handoff` | + `from_session`, `blocked_question` | |
| `review` | + `then_implement` | |

`ai-route stats` also reports blocked / escalated / two-stage counts.

Session metadata (`.git/ai-route-session.json` per worktree) keeps the original keys and adds `workdir`, `difficulty`, `refined_prompt`, `task`, `status`, `last_output_summary`, `blocked`, `decision`, `diff_stat`, `diff` (truncated), `log_path`, and `history`.

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
│       ├── models.py           # Core dataclasses (RouteDecision, BlockSignal, ResolvedFile, DispatchEntry)
│       ├── utils.py            # Subprocess, git metadata, slug/JSON/spec helpers
│       ├── files.py            # File resolution, router previews, task text loading
│       ├── router.py           # Task difficulty routing via OpenCode
│       ├── worktree.py         # git worktree creation & untracked env file copying
│       ├── storage.py          # File-locked JSONL log & per-worktree session metadata
│       ├── dispatch.py         # Agent shell command builders, tmux dispatchers, output logs
│       ├── blocking.py         # detect_blocked(): marker / exit code / question patterns
│       ├── sessions.py         # Rich session metadata, diff capture, session lookup
│       ├── twostage.py         # Planner/escalation prompts, plan JSON validation, implementer selection
│       ├── flow.py             # Sync run + detection + recording, escalation, implementer launch
│       └── commands/
│           ├── run.py          # `run` (incl. --wait / --two-stage) and `handoff` handlers
│           ├── session.py      # `status` and `escalate` handlers
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
