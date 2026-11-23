# Configuration Reference (`config.toml` & Environment Variables)

> **Languages:** **English** | [日本語](../configuration.md)

`ai-router` works out of the box with zero configuration, while supporting `~/.config/ai-route/config.toml` to customize models, subscriptions, presets, routing services, and everyday CLI defaults.

---

## 1. Quick Setup & Diagnostics

```bash
# Detect installed CLIs on PATH and generate a tailored ~/.config/ai-route/config.toml
ai-route init

# Preview, overwrite, or write to a custom path
ai-route init --dry-run
ai-route init --force
ai-route init --output ./custom-config.toml

# Inspect installed CLIs, active config, router service, candidates, and credit cooldowns
ai-route doctor
# (equivalent to `ai-route status --check`)
```

---

## 2. Config File Discovery & Precedence

### Config File Path Resolution
1. CLI global flag: `ai-route --config /path/to/config.toml ...`
2. Environment variable: `AI_ROUTE_CONFIG=/path/to/config.toml`
3. Default path: `$XDG_CONFIG_HOME/ai-route/config.toml` (fallback: `~/.config/ai-route/config.toml`)

If path (1) or (2) is explicitly specified and missing, `ai-route` exits with a clear error. If the default path (3) does not exist, built-in defaults are used with full backward compatibility.

### Value Precedence
```text
CLI Arguments (--effort, --worktree/--no-worktree, --router-timeout, ...)
  > Environment Variables (AI_ROUTE_ROUTER_TOOL, AI_ROUTE_ROUTER_TIMEOUT, ...)
    > User Configuration File (config.toml)
      > Built-in Defaults
```

---

## 3. Complete `config.toml` Reference

```toml
# ===========================================================================
# 1. Supported Tools & Environment Auto-Detection
# ===========================================================================

supported_tools = ["opencode", "agy", "claude", "copilot", "codex"]

# When true, CLIs not found on PATH are automatically skipped during routing & failover
auto_detect_tools = true

# Set true to discard all built-in candidates and use only [[candidates]] defined below
reset_candidates = false


# ===========================================================================
# 2. Adding, Overriding, and Removing Candidate Models
# ===========================================================================

# Matching (tool, model) overrides the built-in entry; new pairs are appended.
# difficulty: "low" | "medium" | "high"
# effort (optional): default reasoning effort ("low"|"medium"|"high"|"xhigh"|"max")
[[candidates]]
tool = "claude"
model = "sonnet"
difficulty = "medium"
effort = "medium"
description = "Complex logic implementation, precise edits, debugging"

[[candidates]]
tool = "copilot"
model = "gpt-5.6-sol"
difficulty = "medium"
effort = "medium"
description = "Mid-sized implementation, code edits, GitHub workflows"

[[candidates]]
tool = "codex"
model = "gpt-6.1-sol"
difficulty = "high"
effort = "high"
description = "High-complexity design, large implementations, autonomous debugging"

# Remove built-in or existing candidates ("tool:model" strings or { tool, model } tables)
remove_candidates = [
    "opencode:opencode-go/deepseek-v4.1-flash",
]


# ===========================================================================
# 3. Subscription / Access Allowlist
# ===========================================================================

# If omitted, all active candidates are allowed.
# When specified, router candidates, allowed_pairs, and CLI choices (--tool / --preset)
# are restricted to this exact set.
allowed_models = [
    "claude:sonnet",
    "claude:opus",
    "agy:gemini-3.8-flash-medium",
    "agy:gemini-4-neon-high",
    "copilot:gpt-5.6-sol",
]


# ===========================================================================
# 4. Presets (`review --preset` / `multi --preset`)
# ===========================================================================

# Accepts either "tool:model" strings or ["tool", "model"] pairs
[presets]
fast = [
    "agy:gemini-3.8-flash-medium",
    "copilot:gpt-5.6-sol",
]
balanced = [
    ["agy", "gemini-3.8-flash-medium"],
    ["claude", "sonnet"],
]
my_team = [
    "claude:opus",
    "agy:gemini-4-neon-high",
    "copilot:gpt-5.6-sol",
]
# The "all" preset is auto-generated from all active candidates unless defined explicitly


# ===========================================================================
# 5. Fallback Decision (used on router parse error / timeout / unavailability)
# ===========================================================================

[fallback]
tool = "claude"
model = "sonnet"
difficulty = "medium"
effort = "medium"
branch_slug = "ai-task"
reason = "Fallback due to router output parse error or disallowed model selection"


# ===========================================================================
# 6. Runtime Settings (overridable via env vars or CLI flags)
# ===========================================================================

[runtime]
# Service used for routing (empty = auto: opencode if installed, else first installed service)
router_tool = ""
# Fallback router services tried in order if the primary router runs out of credits
router_fallback_tools = ["opencode", "agy", "copilot", "claude"]
router_model = "opencode-go/deepseek-v4.1-flash"
opencode_attach_url = ""

# Default ACP mode for `agy` ("auto" | "accept-edits" | "default" | "plan")
# Read-only stages (planner, review, escalate, router) always use "plan".
agy_acp_mode = "auto"

# Permission flag passed to GitHub Copilot CLI in headless mode
copilot_permission_flag = "--allow-all-tools"

# Timeouts in seconds (<= 0 disables timeout)
router_timeout = 60.0
review_timeout = 300.0
test_cmd_timeout = 300.0
agent_timeout = 1800.0

# Cooldown duration (seconds) after a service hits credit/quota exhaustion
credit_cooldown_sec = 3600.0


# ===========================================================================
# 7. Everyday CLI Flag Defaults
# ===========================================================================

[defaults]
worktree = false          # true = always create a worktree (override once with --no-worktree)
mode = "interactive"      # "interactive" | "headless"
auto = false              # true = enable --auto by default
split = false             # true = enable --split by default
plan_first = true         # false = disable plan-first prompt suffix by default
preset = "balanced"       # default --preset for `review` and `multi`
effort = ""               # default effort when manually specifying --tool/--model
acp_mode = "auto"         # default ACP mode for `agy`
```

---

## 4. Common Recipes

### Recipe A: Claude + GitHub Copilot Subscription Only

```toml
allowed_models = [
    "claude:sonnet",
    "claude:opus",
    "copilot:gpt-5.6-sol",
]

[runtime]
router_tool = "copilot"
router_fallback_tools = ["copilot", "claude"]

[fallback]
tool = "claude"
model = "sonnet"
```

### Recipe B: Always Isolate in a Worktree + Run Synchronously (`--auto`)

```toml
[defaults]
worktree = true
auto = true
```

Now `ai-route "Add unit tests"` automatically creates a worktree, estimates difficulty & effort, runs headlessly, escalates if blocked, and fails over if a service runs out of credits. Use `ai-route --no-worktree "..."` whenever you want to run in place.

---

## 5. Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `AI_ROUTE_CONFIG` | `~/.config/ai-route/config.toml` | Override config file path |
| `AI_ROUTE_ROUTER_TOOL` | *(auto)* | Force a specific service for routing (`opencode` / `agy` / `copilot` / `claude` / `codex`) |
| `AI_ROUTE_ROUTER_MODEL` | `opencode-go/deepseek-v4.1-flash` | Default router & review synthesis model |
| `AI_ROUTE_AUTO_DETECT_TOOLS` | `true` | Set `0`/`false` to disable PATH auto-filtering of uninstalled CLIs |
| `AI_ROUTE_AGY_ACP_MODE` | `auto` | Default ACP mode for `agy` (`auto` / `accept-edits` / `default` / `plan`) |
| `AI_ROUTE_COPILOT_PERMISSION_FLAG` | `--allow-all-tools` | Permission flag passed to headless `copilot` |
| `OPENCODE_ATTACH_URL` | *(empty)* | Optional `--attach` server URL passed to `opencode run` |
| `AI_ROUTE_LOG_FILE` | `~/.local/share/ai-route/history.jsonl` | Path to JSONL event log |
| `AI_ROUTE_PROVIDER_STATE_FILE` | `<log dir>/provider_state.json` | Path to persisted credit-cooldown state |
| `AI_ROUTE_CREDIT_COOLDOWN_SEC` | `3600` | Cooldown seconds before re-trying an exhausted service |
| `AI_ROUTE_CREDIT_PATTERNS` | *(empty)* | Extra regexes (`;;`-separated, case-insensitive) for credit exhaustion detection |
| `AI_ROUTE_BLOCKED_PATTERNS` | *(empty)* | Extra regexes (`;;`-separated, case-insensitive) for blocked-agent detection |
| `AI_ROUTE_ROUTER_TIMEOUT` | `60` | Router call timeout in seconds (`<=0` disables) |
| `AI_ROUTE_REVIEW_TIMEOUT` | `300` | Per-model timeout for headless `review` |
| `AI_ROUTE_TEST_CMD_TIMEOUT` | `300` | Per-worktree timeout for `compare` / `done --test-cmd` |
| `AI_ROUTE_AGENT_TIMEOUT` | `1800` | Timeout for `--auto` / `--wait` / planner / escalation |
| `AI_ROUTE_PLANNER` | *(empty)* | Planner `tool:model` for two-stage runs |
| `AI_ROUTE_IMPLEMENTER` | *(empty)* | Implementer `tool:model` or bare `tool` for two-stage / `--then-implement` |

---

## 6. Startup Validation

Invalid configuration is caught immediately on startup via `ConfigError` and reported as a single `[error] ...` line on stderr (exit code `1`, no traceback):
- Invalid TOML syntax or missing explicit config file
- Unsupported `tool` not present in `supported_tools`
- Empty `model` name
- Invalid `difficulty` (must be `low` / `medium` / `high`) or `effort` (`low` / `medium` / `high` / `xhigh` / `max`)
- Duplicate `(tool, model)` candidates
- `[fallback]` or `[presets]` referencing models outside the allowed candidate pool
- Invalid `agy_acp_mode` / `defaults.acp_mode`
