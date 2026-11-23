# ai-router (`ai-route`) Documentation

> **Languages:** **English** | [日本語](../README.md)

Comprehensive usage guide, configuration reference, and multi-service / ACP / failover documentation for `ai-router` (CLI: `ai-route` / `ai-router`).

## Table of Contents

1. **[Usage Guide (`docs/en/usage.md`)](usage.md)**
   - Minimal 3–4 command daily workflow
   - Starting tasks (omitting `run`, piped stdin, `-f`/`-F`, `--effort`, `--auto`, `-p`/`--plan`)
   - Resuming & recovering sessions (`resume` / `status` / `escalate` / `handoff`)
   - One-command worktree finish (`done` / `list` / `clean`)
   - Multi-model review & parallel solving (`review` / `multi` / `compare` / `pick`)
   - Analytics (`stats`) and shell completions (`--completions`)
2. **[Configuration Reference (`docs/en/configuration.md`)](configuration.md)**
   - Automated setup & environment diagnostics (`ai-route init` / `ai-route doctor`)
   - Precedence order (CLI > Environment variables > `config.toml` > Built-in defaults)
   - Complete `config.toml` schema & practical recipes (`[[candidates]]`, `allowed_models`, `[presets]`, `[fallback]`, `[runtime]`, `[defaults]`)
   - Environment variables and startup validation
3. **[Services, ACP Mode & Credit Failover (`docs/en/services-and-failover.md`)](services-and-failover.md)**
   - CLI invocation specs & effort mapping for all 5 services (`claude`, `agy`, `opencode`, `copilot`, `codex`)
   - Mandatory `agy` ACP mode (`agy acp --stdio`) architecture & Terms of Service compliance notes
   - Environment auto-detection and multi-service routing
   - Credit/quota exhaustion detection, cooldown tracking, automatic failover, and troubleshooting

---

## Quick Reference

```bash
# 0. Initial setup & environment diagnostics
ai-route init
ai-route doctor
eval "$(ai-route --completions zsh)"   # bash / zsh / fish

# 1. Start a task (`run` can be omitted)
ai-route "Fix edge cases in the CSV parser"
ai-route -w "Add a caching layer"                        # isolated git worktree
ai-route --auto "Add unit tests for the parser"          # sync headless + auto-escalate + credit failover
ai-route -p -w "Refactor authentication middleware"      # two-stage (planner -> implementer)
gh issue view 42 | ai-route -w                           # auto-reads piped stdin

# 2. Continue a stopped / blocked / credit-exhausted session in one command
ai-route resume                                          # auto-escalates or fails over to another service
ai-route resume --auto                                   # run synchronously to completion

# 3. Finish a worktree task (test -> merge or PR -> cleanup) in one command
ai-route done --test-cmd "pytest -q"                     # merge into main & remove worktree/branch
ai-route done --pr                                       # push & open GitHub PR via `gh pr create`

# 4. Review, inspect status & manage credit cooldowns
ai-route review --synthesize
ai-route status
ai-route status --reset-credits
```
