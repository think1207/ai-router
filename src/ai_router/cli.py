"""CLI argument parser and entrypoint for ai-router."""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from typing import NoReturn, Sequence

from ai_router.commands.multi import cmd_compare, cmd_multi, cmd_pick
from ai_router.commands.review import cmd_review
from ai_router.commands.run import cmd_handoff, cmd_run
from ai_router.commands.session import cmd_escalate, cmd_status
from ai_router.commands.stats import cmd_stats
from ai_router.commands.worktree import cmd_clean, cmd_list
from ai_router.config import MODEL_PRESETS, STAGE_PRESETS, SUPPORTED_TOOLS
from ai_router.errors import AiRouteError


def _get_version() -> str:
    try:
        return version("ai-router")
    except PackageNotFoundError:
        return "0.1.0-dev"


def build_parser() -> argparse.ArgumentParser:
    """Build and configure the top-level CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="ai-route",
        description=(
            "AI coding agent router and orchestrator with tmux, git worktree, "
            "handoff, multi-model review/solve, and JSONL analytics."
        ),
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {_get_version()}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # run
    p_run = sub.add_parser("run", help="Classify task difficulty and dispatch an agent")
    p_run.add_argument(
        "task",
        nargs="?",
        default=None,
        help="Task or problem description (optional or supplemental when -F is used)",
    )
    p_run.add_argument(
        "-f",
        "--file",
        dest="files",
        action="append",
        default=[],
        help="Target or reference file (repeatable)",
    )
    p_run.add_argument(
        "-F",
        "--task-file",
        help="Read task description from a file ('-' for stdin)",
    )
    p_run.add_argument(
        "-w",
        "--worktree",
        action="store_true",
        help="Create a new isolated git worktree for execution",
    )
    p_run.add_argument(
        "--base",
        default="HEAD",
        help="Base branch or commit for the worktree (default: HEAD)",
    )
    p_run.add_argument("--slug", default="", help="Custom branch slug when manually specifying tool/model")
    p_run.add_argument(
        "--mode",
        choices=["interactive", "headless"],
        default="interactive",
        help="Execution mode (default: interactive)",
    )
    p_run.add_argument(
        "--split",
        action="store_true",
        help="Launch in a split pane of the current tmux window instead of a new window",
    )
    p_run.add_argument(
        "--no-plan-first",
        action="store_true",
        help="Do not append the plan-agreement-first instruction",
    )
    p_run.add_argument(
        "--tool",
        choices=list(SUPPORTED_TOOLS),
        help="Skip router and force a specific tool (must be combined with --model)",
    )
    p_run.add_argument(
        "--model",
        help="Force a specific model (must be combined with --tool)",
    )
    p_run.add_argument(
        "--dry-run",
        action="store_true",
        help="Print routing decision and refined prompt without launching",
    )
    p_run.add_argument(
        "--router-timeout",
        type=float,
        default=None,
        metavar="SEC",
        help=(
            "Timeout for the router model call; falls back to the default model on timeout "
            "(default: $AI_ROUTE_ROUTER_TIMEOUT or 60; <=0 disables)"
        ),
    )
    p_run.add_argument(
        "--wait",
        action="store_true",
        help="With --mode headless, run synchronously and detect blocked (waiting-for-decision) output",
    )
    p_run.add_argument(
        "--auto-escalate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="With --wait, escalate a blocked agent to a stronger model and resume once (default: on)",
    )
    p_run.add_argument(
        "--agent-timeout",
        type=float,
        default=None,
        metavar="SEC",
        help=(
            "Timeout for synchronous agent runs (--wait, planner, escalation) "
            "(default: $AI_ROUTE_AGENT_TIMEOUT or 1800; <=0 disables)"
        ),
    )
    p_run.add_argument(
        "--two-stage",
        action="store_true",
        help="Plan with a strong model (decisions only, no edits), then implement with another; skips the router",
    )
    p_run.add_argument(
        "--plan-preset",
        choices=list(STAGE_PRESETS.keys()),
        default=None,
        help="Planner/implementer preset for --two-stage",
    )
    p_run.add_argument(
        "--planner",
        default=None,
        metavar="TOOL:MODEL",
        help="Planner for --two-stage (default: $AI_ROUTE_PLANNER, preset, or claude:opus)",
    )
    p_run.add_argument(
        "--implementer",
        default=None,
        metavar="TOOL[:MODEL]",
        help=(
            "Implementer for --two-stage; a bare tool picks the model by planned difficulty "
            "(default: $AI_ROUTE_IMPLEMENTER, preset, or by difficulty)"
        ),
    )
    p_run.set_defaults(func=cmd_run)

    # handoff
    p_ho = sub.add_parser(
        "handoff",
        help="Hand off the current worktree diff to another model in a split pane",
    )
    p_ho.add_argument(
        "note",
        nargs="?",
        default=None,
        help="Handoff notes or next-step instructions (optional with --from-session)",
    )
    p_ho.add_argument(
        "--from-session",
        nargs="?",
        const=".",
        default=None,
        metavar="SESSION_ID",
        help=(
            "Build the handoff from saved session state (task, refined prompt, blocked question, "
            "output summary); without an ID, uses the current worktree's session"
        ),
    )
    p_ho.add_argument(
        "-f",
        "--file",
        dest="files",
        action="append",
        default=[],
        help="Files to highlight during handoff (repeatable)",
    )
    p_ho.add_argument(
        "--tool",
        choices=list(SUPPORTED_TOOLS),
        default="claude",
        help="Target agent tool (default: claude)",
    )
    p_ho.add_argument(
        "--model",
        default="opus",
        help="Target model identifier (default: opus)",
    )
    p_ho.set_defaults(func=cmd_handoff)

    # review
    p_rev = sub.add_parser("review", help="Run parallel multi-model code review")
    p_rev.add_argument(
        "-f",
        "--file",
        dest="files",
        action="append",
        default=[],
        help="Target files to review (repeatable)",
    )
    p_rev.add_argument(
        "--staged",
        action="store_true",
        help="Review staged changes (git diff --cached)",
    )
    p_rev.add_argument(
        "--base",
        default="",
        help="Review diff against a base branch/ref (base...HEAD)",
    )
    p_rev.add_argument(
        "--include-diff",
        action="store_true",
        help="Also include git diff when -f files are specified",
    )
    p_rev.add_argument(
        "--focus",
        default="",
        help="Specific review focus area (e.g. 'memory safety and race conditions')",
    )
    p_rev.add_argument(
        "--preset",
        choices=list(MODEL_PRESETS.keys()),
        default="balanced",
        help="Predefined model combination preset (default: balanced)",
    )
    p_rev.add_argument(
        "--models",
        default="",
        help="Comma-separated 'tool:model' specs (e.g. 'claude:sonnet,agy:gemini-3.8-flash-medium')",
    )
    p_rev.add_argument(
        "--roles",
        action="store_true",
        help="Assign distinct review perspectives to each model",
    )
    p_rev.add_argument(
        "--mode",
        choices=["headless", "interactive"],
        default="headless",
        help="Execution mode (default: headless)",
    )
    p_rev.add_argument(
        "--synthesize",
        action="store_true",
        help="Synthesize individual model reviews into a single summary",
    )
    p_rev.add_argument(
        "--synthesizer",
        default="",
        help="Synthesizer model in 'tool:model' format (defaults to router model)",
    )
    p_rev.add_argument("-o", "--output", default="", help="Output path for the review report")
    p_rev.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SEC",
        help=(
            "Per-model timeout for headless reviews and synthesis "
            "(default: $AI_ROUTE_REVIEW_TIMEOUT or 300; <=0 disables)"
        ),
    )
    p_rev.add_argument(
        "--then-implement",
        nargs="?",
        const="",
        default=None,
        metavar="TOOL[:MODEL]",
        help=(
            "After a headless review, use the synthesized result as the plan and launch an "
            "implementer (implies --synthesize; default implementer chosen by planned difficulty)"
        ),
    )
    p_rev.add_argument(
        "--impl-mode",
        choices=["interactive", "headless"],
        default="interactive",
        help="Execution mode of the --then-implement implementer (default: interactive)",
    )
    p_rev.add_argument(
        "--wait",
        action="store_true",
        help="With --impl-mode headless, run the implementer synchronously and detect blocking",
    )
    p_rev.add_argument(
        "--auto-escalate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="With --wait, escalate a blocked agent to a stronger model and resume once (default: on)",
    )
    p_rev.add_argument(
        "--agent-timeout",
        type=float,
        default=None,
        metavar="SEC",
        help=(
            "Timeout for synchronous agent runs (--wait, planner, escalation) "
            "(default: $AI_ROUTE_AGENT_TIMEOUT or 1800; <=0 disables)"
        ),
    )
    p_rev.set_defaults(func=cmd_review)

    # escalate
    p_esc = sub.add_parser(
        "escalate",
        help="Ask a stronger model to decide on a blocked session (no edits), optionally resume",
    )
    p_esc.add_argument(
        "--from-session",
        default=None,
        metavar="SESSION_ID",
        help="Session to escalate (default: current worktree's session)",
    )
    p_esc.add_argument(
        "--tool",
        choices=list(SUPPORTED_TOOLS),
        help="Decision-maker tool (alone: its high-difficulty model; default: claude:opus)",
    )
    p_esc.add_argument("--model", help="Decision-maker model (requires --tool)")
    p_esc.add_argument("--note", default="", help="Extra guidance for the decision-maker")
    p_esc.add_argument(
        "--then-implement",
        action="store_true",
        help="Resume the session's implementer with the decision",
    )
    p_esc.add_argument(
        "--mode",
        choices=["interactive", "headless"],
        default="interactive",
        help="Execution mode of the resumed implementer (default: interactive)",
    )
    p_esc.add_argument(
        "--wait",
        action="store_true",
        help="With --mode headless, run the resumed implementer synchronously",
    )
    p_esc.add_argument(
        "--auto-escalate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="With --wait, escalate a blocked agent to a stronger model and resume once (default: on)",
    )
    p_esc.add_argument(
        "--agent-timeout",
        type=float,
        default=None,
        metavar="SEC",
        help=(
            "Timeout for synchronous agent runs (--wait, planner, escalation) "
            "(default: $AI_ROUTE_AGENT_TIMEOUT or 1800; <=0 disables)"
        ),
    )
    p_esc.set_defaults(func=cmd_escalate)

    # status
    p_status = sub.add_parser(
        "status",
        help="Show session status and detect blocked agents from captured output logs",
    )
    p_status.add_argument(
        "--from-session",
        default=None,
        metavar="SESSION_ID",
        help="Session to inspect (default: current worktree, else recent sessions)",
    )
    p_status.add_argument("--group", default="", help="Inspect all members of a `multi` group")
    p_status.add_argument(
        "--recent",
        type=int,
        default=10,
        help="Number of recent sessions shown when no session is selected (default: 10)",
    )
    p_status.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    p_status.set_defaults(func=cmd_status)

    # multi
    p_mul = sub.add_parser(
        "multi",
        help="Solve the same task in parallel across multiple models in isolated worktrees",
    )
    p_mul.add_argument("task", nargs="?", default=None, help="Task or problem description")
    p_mul.add_argument(
        "-f",
        "--file",
        dest="files",
        action="append",
        default=[],
        help="Target or reference file (repeatable)",
    )
    p_mul.add_argument(
        "-F",
        "--task-file",
        help="Read task description from a file ('-' for stdin)",
    )
    p_mul.add_argument(
        "--preset",
        choices=list(MODEL_PRESETS.keys()),
        default="balanced",
        help="Predefined model combination preset (default: balanced)",
    )
    p_mul.add_argument(
        "--models",
        default="",
        help="Comma-separated 'tool:model' specs (e.g. 'claude:opus,agy:gemini-4-neon-high')",
    )
    p_mul.add_argument(
        "--base",
        default="HEAD",
        help="Shared base commit for all worktrees (default: HEAD)",
    )
    p_mul.add_argument("--slug", default="", help="Short slug used for tmux window naming")
    p_mul.add_argument(
        "--mode",
        choices=["interactive", "headless"],
        default="interactive",
        help="Execution mode (default: interactive)",
    )
    p_mul.add_argument(
        "--layout",
        choices=["tiled", "windows"],
        default="tiled",
        help="tmux pane/window layout (default: tiled)",
    )
    p_mul.add_argument(
        "--no-plan-first",
        action="store_true",
        help="Do not append the plan-agreement-first instruction",
    )
    p_mul.set_defaults(func=cmd_multi)

    # compare
    p_cmp = sub.add_parser(
        "compare",
        help="Compare diffs, commits, and test results from a `multi` run",
    )
    p_cmp.add_argument(
        "group_id",
        nargs="?",
        default="",
        help="Group ID to compare (defaults to the latest `multi` run)",
    )
    p_cmp.add_argument(
        "--test-cmd",
        default="",
        help="Verification command to run in each worktree (e.g. 'pytest -q')",
    )
    p_cmp.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SEC",
        help=(
            "Per-worktree timeout for --test-cmd "
            "(default: $AI_ROUTE_TEST_CMD_TIMEOUT or 300; <=0 disables)"
        ),
    )
    p_cmp.set_defaults(func=cmd_compare)

    # pick
    p_pick = sub.add_parser(
        "pick",
        help="Merge a chosen branch from a `multi` run and clean up worktrees and `ai/` branches",
    )
    p_pick.add_argument(
        "target",
        nargs="?",
        default="",
        help="Group ID (defaults to latest `multi` run), or 1-based member index for the latest group",
    )
    p_pick.add_argument(
        "choice",
        nargs="?",
        type=int,
        default=None,
        help="1-based member index from `compare` output (prompts interactively if omitted)",
    )
    p_pick.add_argument(
        "-c",
        "--choice",
        dest="choice_opt",
        metavar="INDEX",
        type=int,
        default=None,
        help="Explicit 1-based member index to merge",
    )
    merge_group = p_pick.add_mutually_exclusive_group()
    merge_group.add_argument(
        "--squash",
        action="store_true",
        help="Squash-merge the winning branch instead of creating a merge commit",
    )
    merge_group.add_argument(
        "--no-ff",
        action="store_true",
        help="Always create a merge commit (git merge --no-ff)",
    )
    p_pick.add_argument(
        "--keep-winner",
        action="store_true",
        help="Keep the winning worktree and its `ai/` branch after merging",
    )
    p_pick.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Force-remove worktrees with uncommitted changes and allow picking a dirty winner",
    )
    p_pick.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview merge and cleanup actions without modifying git state",
    )
    p_pick.set_defaults(func=cmd_pick)

    # list
    p_ls = sub.add_parser("list", help="List active git worktrees")
    p_ls.set_defaults(func=cmd_list)

    # clean
    p_cl = sub.add_parser("clean", help="Remove a completed git worktree")
    p_cl.add_argument("path", help="Path to the worktree to remove")
    p_cl.add_argument(
        "-b",
        "--delete-branch",
        action="store_true",
        help="Also delete the associated `ai/` branch",
    )
    p_cl.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Force removal even if uncommitted changes exist",
    )
    p_cl.set_defaults(func=cmd_clean)

    # stats
    p_st = sub.add_parser(
        "stats",
        help="Display routing, handoff, and multi-model usage statistics from JSONL logs",
    )
    p_st.add_argument(
        "--days",
        type=int,
        default=30,
        help="Number of recent days to include (0 for all time, default: 30)",
    )
    p_st.add_argument("--repo", default="", help="Filter statistics by repository name")
    p_st.add_argument(
        "--here",
        action="store_true",
        help="Filter statistics to the current repository",
    )
    p_st.add_argument(
        "--recent",
        type=int,
        default=10,
        help="Number of recent sessions to display (default: 10)",
    )
    p_st.set_defaults(func=cmd_stats)

    return parser


def _format_cmd(cmd: object) -> str:
    if isinstance(cmd, (list, tuple)):
        return shlex.join(str(c) for c in cmd)
    return str(cmd)


def _fail(message: str) -> NoReturn:
    print(f"[error] {message}", file=sys.stderr)
    sys.exit(1)


def main(argv: Sequence[str] | None = None) -> None:
    """Parse CLI arguments and dispatch to the selected subcommand.

    All anticipated failures are reported as a single ``[error] ...`` line on stderr
    with exit code 1 instead of a Python traceback.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except AiRouteError as e:
        _fail(str(e))
    except subprocess.CalledProcessError as e:
        detail = e.stderr if isinstance(e.stderr, str) else ""
        msg = f"Command failed (rc={e.returncode}): {_format_cmd(e.cmd)}"
        _fail(f"{msg}\n{detail.strip()}" if detail.strip() else msg)
    except subprocess.TimeoutExpired as e:
        _fail(f"Command timed out after {e.timeout:g}s: {_format_cmd(e.cmd)}")
    except FileNotFoundError as e:
        _fail(f"File or command not found: {e.filename or e}")
    except OSError as e:
        _fail(f"OS error: {e}")
    except KeyboardInterrupt:
        print(file=sys.stderr)
        _fail("Interrupted by user.")
