"""Shell completion script generators for bash, zsh, and fish."""

from __future__ import annotations

from ai_router.config import VALID_EFFORTS, AppConfig, get_active_config

SUBCOMMANDS: tuple[str, ...] = (
    "run",
    "resume",
    "continue",
    "next",
    "review",
    "status",
    "done",
    "init",
    "doctor",
    "handoff",
    "escalate",
    "multi",
    "compare",
    "pick",
    "list",
    "clean",
    "stats",
)


def generate_completions(shell: str, config: AppConfig | None = None) -> str:
    """Return a shell completion script for `bash`, `zsh`, or `fish`."""
    cfg = config or get_active_config()
    cmds = " ".join(SUBCOMMANDS)
    tools = " ".join(cfg.available_tools)
    presets = " ".join(sorted(cfg.presets.keys()))
    efforts = " ".join(VALID_EFFORTS)
    common_flags = "-w --worktree --auto -p --plan --effort --tool --model --dry-run -f --file -F --task-file --config --help"

    if shell == "bash":
        return f"""# bash completion for ai-route / ai-router
_ai_route_complete() {{
    local cur prev
    COMPREPLY=()
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    prev="${{COMP_WORDS[COMP_CWORD-1]}}"

    case "$prev" in
        --tool|--mark-exhausted|--reset-credits)
            COMPREPLY=( $(compgen -W "{tools}" -- "$cur") )
            return 0 ;;
        --preset)
            COMPREPLY=( $(compgen -W "{presets}" -- "$cur") )
            return 0 ;;
        --effort)
            COMPREPLY=( $(compgen -W "{efforts}" -- "$cur") )
            return 0 ;;
        --mode|--impl-mode)
            COMPREPLY=( $(compgen -W "interactive headless" -- "$cur") )
            return 0 ;;
        --completions)
            COMPREPLY=( $(compgen -W "bash zsh fish" -- "$cur") )
            return 0 ;;
    esac

    if [[ $COMP_CWORD -eq 1 ]]; then
        COMPREPLY=( $(compgen -W "{cmds} {common_flags}" -- "$cur") )
        return 0
    fi
    COMPREPLY=( $(compgen -W "{common_flags}" -- "$cur") )
}}
complete -F _ai_route_complete ai-route ai-router
"""

    if shell == "zsh":
        return f"""#compdef ai-route ai-router
_ai_route() {{
    local -a subcommands tools presets efforts
    subcommands=({cmds})
    tools=({tools})
    presets=({presets})
    efforts=({efforts})

    case "$words[CURRENT-1]" in
        --tool|--mark-exhausted|--reset-credits)
            _describe 'tool' tools
            return ;;
        --preset)
            _describe 'preset' presets
            return ;;
        --effort)
            _describe 'effort' efforts
            return ;;
        --mode|--impl-mode)
            _values 'mode' interactive headless
            return ;;
        --completions)
            _values 'shell' bash zsh fish
            return ;;
    esac

    if (( CURRENT == 2 )); then
        _describe 'command' subcommands
        return
    fi
    _files
}}
compdef _ai_route ai-route ai-router
"""

    if shell == "fish":
        lines = [
            "# fish completion for ai-route / ai-router",
            f"complete -c ai-route -n '__fish_use_subcommand' -a '{cmds}'",
            f"complete -c ai-router -n '__fish_use_subcommand' -a '{cmds}'",
            f"complete -c ai-route -l tool -xa '{tools}'",
            f"complete -c ai-route -l preset -xa '{presets}'",
            f"complete -c ai-route -l effort -xa '{efforts}'",
            "complete -c ai-route -l mode -xa 'interactive headless'",
            "complete -c ai-route -l completions -xa 'bash zsh fish'",
            "complete -c ai-route -s w -l worktree -d 'Create isolated git worktree'",
            "complete -c ai-route -l auto -d 'Sync headless with auto-escalate & failover'",
            "complete -c ai-route -s p -l plan -d 'Two-stage planner -> implementer'",
        ]
        return "\n".join(lines) + "\n"

    raise ValueError(f"Unsupported shell: {shell} (choose from: bash, zsh, fish)")
