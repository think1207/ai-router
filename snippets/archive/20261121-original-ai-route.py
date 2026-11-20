#!/usr/bin/env python3
"""
ai-route: OpenCode Go の軽量モデルでタスク難易度を判定し、
git worktree + tmux で各コーディングエージェント（opencode / agy / claude）へ
ディスパッチ・並列解決・並列レビュー・振り返りを行う統合CLI。

サブコマンド:
  run      : 単一タスクの難易度判定とディスパッチ（-w worktree / -f ファイル / -F タスクファイル対応）
  handoff  : 同一 worktree 内でペイン分割して別モデルへ差分ごと引き継ぎ
  review   : 複数モデルによる並列コードレビュー（headless統合サマリ / interactive分割ペイン）
  multi    : 複数モデルに独立 worktree で同一タスクを並列解決させる
  compare  : multi で並列実行した各モデルの差分・コミット・テスト結果を横並び比較
  stats    : JSONL ログから振り分け実績・handoff 遷移・複数モデル実行回数を集計表示
  list     : アクティブな git worktree 一覧
  clean    : 作業完了した worktree（および ai/ ブランチ）を削除
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import dataclasses
import datetime
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

# ==============================================================================
# 設定（環境に合わせてモデルID・環境変数を調整してください）
# ==============================================================================

ROUTER_MODEL = os.environ.get("AI_ROUTE_ROUTER_MODEL", "opencode/deepseek-v4.1-flash")
OPENCODE_ATTACH_URL = os.environ.get("OPENCODE_ATTACH_URL", "")
AGY_PASTE_DELAY_SEC = float(os.environ.get("AI_ROUTE_AGY_DELAY", "2.0"))

# 新規 worktree に自動コピーする未追跡ファイル（存在するものだけコピー）
WORKTREE_COPY_FILES = [".env", ".env.local"]

# ルーターに渡すファイルプレビューの上限（遅延・トークン超過防止）
ROUTER_FILE_MAX_LINES = 60
ROUTER_TOTAL_PREVIEW_CHARS = 12000

# JSONL ログ保存先
LOG_FILE = Path(
    os.environ.get(
        "AI_ROUTE_LOG_FILE",
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        / "ai-route"
        / "history.jsonl",
    )
)

# 許可された (tool, model) のホワイトリスト
CANDIDATES: list[dict[str, str]] = [
    {
        "tool": "opencode",
        "model": "opencode/deepseek-v4.1-flash",
        "difficulty": "low",
        "description": "軽微な修正、ボイラープレート生成、単一ファイルのテスト追加、ドキュメント修正",
    },
    {
        "tool": "agy",
        "model": "Gemini 3.7 Flash (Low)",
        "difficulty": "medium",
        "description": "中規模な実装、複数ファイルにまたがる調査・リファクタリング、解決方針の比較検討",
    },
    {
        "tool": "claude",
        "model": "sonnet",
        "difficulty": "medium",
        "description": "複雑なロジック実装、精密なコード修正、デバッグ",
    },
    {
        "tool": "claude",
        "model": "opus",
        "difficulty": "high",
        "description": "高難度のアーキテクチャ設計、難解なバグ解析、広範囲の破壊的変更",
    },
]

# review / multi 用のモデル組み合わせプリセット
MODEL_PRESETS: dict[str, list[tuple[str, str]]] = {
    "fast": [
        ("opencode", "opencode/deepseek-v4.1-flash"),
        ("agy", "Gemini 3.7 Flash (Low)"),
    ],
    "balanced": [
        ("agy", "Gemini 3.7 Flash (Low)"),
        ("claude", "sonnet"),
    ],
    "strong": [
        ("claude", "sonnet"),
        ("claude", "opus"),
        ("agy", "Gemini 3.7 Flash (Low)"),
    ],
    "all": [(c["tool"], c["model"]) for c in CANDIDATES],
}

# review --roles 指定時の観点割り当て
REVIEW_ROLES = [
    "バグ・エッジケース・競合状態・エラーハンドリングの厳密な検出",
    "アーキテクチャ設計・責務分離・保守性・パフォーマンスへの影響",
    "セキュリティ・境界値・テスト網羅性・可読性",
]

FALLBACK_DECISION: dict[str, str] = {
    "difficulty": "medium",
    "tool": "claude",
    "model": "sonnet",
    "branch_slug": "ai-task",
    "reason": "ルーター出力の解析失敗または不正モデルのためフォールバック",
}


# ==============================================================================
# データモデル
# ==============================================================================

@dataclasses.dataclass
class RouteDecision:
    difficulty: str
    tool: str
    model: str
    branch_slug: str
    reason: str
    refined_prompt: str
    router_latency_ms: int = 0
    is_fallback: bool = False


@dataclasses.dataclass
class ResolvedFile:
    display_path: str       # エージェントに渡すパス（リポジトリ内なら相対パス）
    abs_path: Path
    in_repo: bool
    is_tracked: bool
    line_count: int | None
    preview: str            # ルーター用の抜粋


# ==============================================================================
# 共通ユーティリティ
# ==============================================================================

def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        capture_output=capture,
    )


def get_git_root(cwd: Path | None = None) -> Path | None:
    res = run_cmd(["git", "rev-parse", "--show-toplevel"], cwd=cwd, check=False)
    if res.returncode != 0:
        return None
    return Path(res.stdout.strip())


def get_git_dir(cwd: Path) -> Path | None:
    res = run_cmd(["git", "rev-parse", "--git-dir"], cwd=cwd, check=False)
    if res.returncode != 0:
        return None
    raw = Path(res.stdout.strip())
    return raw if raw.is_absolute() else (cwd / raw).resolve()


def get_repo_and_branch(cwd: Path) -> tuple[str, str]:
    root = get_git_root(cwd)
    repo_name = root.name if root else cwd.name
    branch = run_cmd(["git", "branch", "--show-current"], cwd=cwd, check=False).stdout.strip()
    return repo_name, branch or "(detached)"


def get_git_context(repo_root: Path) -> str:
    branch = run_cmd(["git", "branch", "--show-current"], cwd=repo_root, check=False).stdout.strip()
    status = run_cmd(["git", "status", "--short"], cwd=repo_root, check=False).stdout.strip()
    status_lines = "\n".join(status.splitlines()[:25])
    return f"Git branch: {branch}\nChanged files:\n{status_lines or '(clean)'}"


def sanitize_slug(slug: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", slug.lower()).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned[:32] or "ai-task"


def extract_first_json_object(raw_text: str) -> dict[str, Any] | None:
    """コードフェンスや余計な前置きが混ざっていても最初の有効なJSONオブジェクトを抽出"""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", raw_text):
        start = match.start()
        try:
            obj, _ = decoder.raw_decode(raw_text[start:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def parse_model_specs(models_arg: str | None, preset_arg: str) -> list[tuple[str, str]]:
    """
    --models "claude:sonnet,agy:Gemini 3.7 Flash (Low)" または --preset から
    [(tool, model), ...] のリストを返す
    """
    if not models_arg:
        return list(MODEL_PRESETS[preset_arg])

    specs: list[tuple[str, str]] = []
    for raw in models_arg.split(","):
        item = raw.strip()
        if not item:
            continue
        if ":" not in item:
            print(f"[error] モデル指定は 'tool:model' 形式で書いてください: {item}", file=sys.stderr)
            sys.exit(1)
        tool, model = item.split(":", 1)
        tool = tool.strip()
        model = model.strip()
        if tool not in {"opencode", "agy", "claude"}:
            print(f"[error] 未対応のツールです: {tool}", file=sys.stderr)
            sys.exit(1)
        specs.append((tool, model))

    if not specs:
        print("[error] 有効なモデルが1つも指定されていません。", file=sys.stderr)
        sys.exit(1)
    if len(specs) < 2:
        print("[warn] 指定モデルが1つだけです。複数モデルでの比較を推奨します。", file=sys.stderr)
    return specs


# ==============================================================================
# ファイル解決・タスク入力
# ==============================================================================

def resolve_files(file_args: list[str], repo_root: Path | None) -> list[ResolvedFile]:
    """指定されたファイルパスを検証・正規化し、ルーター用の軽量プレビューを生成"""
    resolved: list[ResolvedFile] = []
    seen: set[Path] = set()
    budget = ROUTER_TOTAL_PREVIEW_CHARS

    for raw in file_args:
        p = Path(raw).expanduser().resolve()
        if not p.exists():
            print(f"[error] 指定ファイルが見つかりません: {raw}", file=sys.stderr)
            sys.exit(1)
        if p.is_dir():
            print(
                f"[warn] ディレクトリはスキップします（個別ファイルまたはglobで指定してください）: {raw}",
                file=sys.stderr,
            )
            continue
        if p in seen:
            continue
        seen.add(p)

        in_repo = False
        is_tracked = False
        display_path = str(p)
        if repo_root:
            try:
                rel = p.relative_to(repo_root.resolve())
                in_repo = True
                display_path = rel.as_posix()
                chk = run_cmd(["git", "ls-files", "--error-unmatch", display_path], cwd=repo_root, check=False)
                is_tracked = chk.returncode == 0
            except ValueError:
                in_repo = False

        line_count: int | None = None
        preview = ""
        try:
            text = p.read_text(encoding="utf-8")
            lines = text.splitlines()
            line_count = len(lines)
            if budget > 0:
                head = "\n".join(lines[:ROUTER_FILE_MAX_LINES])
                if len(lines) > ROUTER_FILE_MAX_LINES:
                    head += f"\n... (全 {line_count} 行中 {ROUTER_FILE_MAX_LINES} 行まで抜粋)"
                if len(head) > budget:
                    head = head[:budget] + "\n... (プレビュー上限により省略)"
                preview = head
                budget -= len(head)
        except UnicodeDecodeError:
            preview = "(binary file)"

        resolved.append(
            ResolvedFile(
                display_path=display_path,
                abs_path=p,
                in_repo=in_repo,
                is_tracked=is_tracked,
                line_count=line_count,
                preview=preview,
            )
        )
    return resolved


def format_files_for_router(files: list[ResolvedFile]) -> str:
    if not files:
        return "(指定ファイルなし)"
    blocks: list[str] = []
    for rf in files:
        loc = "repo内" if rf.in_repo else "repo外(参照のみ)"
        lc = f"{rf.line_count}行" if rf.line_count is not None else "binary"
        blocks.append(f"### {rf.display_path} [{loc}, {lc}]\n```\n{rf.preview}\n```")
    return "\n\n".join(blocks)


def format_files_for_agent(files: list[ResolvedFile], *, using_worktree: bool) -> str:
    if not files:
        return ""
    lines = ["## 対象・参照ファイル"]
    for rf in files:
        note = ""
        if using_worktree and rf.in_repo and not rf.is_tracked:
            note = f" （※未追跡ファイルのためworktree内に存在しません。原本参照先: `{rf.abs_path}`）"
        elif not rf.in_repo:
            note = " （※リポジトリ外の参照専用ファイル）"
        lines.append(f"- `{rf.display_path}`{note}")
    lines.append("\n上記ファイルを優先的に確認したうえで対応してください。")
    return "\n".join(lines)


def load_task_text(task_arg: str | None, task_file: str | None) -> str:
    """位置引数と --task-file / 標準入力('-') を統合してタスク文を組み立てる"""
    parts: list[str] = []
    if task_file:
        if task_file == "-":
            parts.append(sys.stdin.read().strip())
        else:
            tf = Path(task_file).expanduser()
            if not tf.is_file():
                print(f"[error] タスクファイルが見つかりません: {task_file}", file=sys.stderr)
                sys.exit(1)
            parts.append(tf.read_text(encoding="utf-8").strip())
    if task_arg:
        parts.append(task_arg.strip())

    combined = "\n\n".join(p for p in parts if p)
    if not combined:
        print("[error] タスク説明（引数）または --task-file を指定してください。", file=sys.stderr)
        sys.exit(1)
    return combined


# ==============================================================================
# ルーティング（OpenCode Go 軽量モデル）
# ==============================================================================

def build_router_prompt(task: str, repo_ctx: str, files_ctx: str, plan_first: bool) -> str:
    candidates_text = "\n".join(
        f'{i + 1}. tool: "{c["tool"]}", model: "{c["model"]}" (目安: {c["difficulty"]})\n'
        f'   - 用途: {c["description"]}'
        for i, c in enumerate(CANDIDATES)
    )

    plan_note = (
        "refined_promptの末尾には、"
        "「まず考えられる解決方針と実装順序の選択肢を提示し、ユーザーの合意を得てからファイル編集に着手してください。」"
        "という指示を必ず含めてください。"
        if plan_first
        else ""
    )

    return f"""あなたはソフトウェア開発タスクのルーターです。
以下のタスクの難易度と性質を判定し、最適な実行ツールとモデルを1つ選定してください。
必ず有効なJSONオブジェクトのみを出力し、Markdownのコードブロックや前置きは一切含めないでください。

## 選定候補（この中から厳密に選ぶこと）
{candidates_text}

## 出力JSONスキーマ
{{
  "difficulty": "low" | "medium" | "high",
  "tool": "opencode" | "agy" | "claude",
  "model": "<上記候補のmodel文字列を完全一致で記載>",
  "branch_slug": "<gitブランチ用の短い英語スラッグ例: fix-parser-edge-case>",
  "reason": "<日本語で簡潔な選定理由>",
  "refined_prompt": "<選定ツールに渡す整理済みの実装指示（日本語）>"
}}

{plan_note}

## リポジトリ状況
{repo_ctx}

## 指定ファイル概要
{files_ctx}

## ユーザーからのタスク
{task}
"""


def route_task(task: str, repo_ctx: str, files_ctx: str, plan_first: bool) -> RouteDecision:
    prompt = build_router_prompt(task, repo_ctx, files_ctx, plan_first)
    cmd = ["opencode", "run"]
    if OPENCODE_ATTACH_URL:
        cmd.extend(["--attach", OPENCODE_ATTACH_URL])
    cmd.extend(["-m", ROUTER_MODEL, prompt])

    t0 = time.monotonic()
    res = run_cmd(cmd, check=False)
    latency_ms = int((time.monotonic() - t0) * 1000)

    if res.returncode != 0:
        print(f"[warn] Router execution failed: {res.stderr.strip()}", file=sys.stderr)
        return RouteDecision(
            **FALLBACK_DECISION,
            refined_prompt=task,
            router_latency_ms=latency_ms,
            is_fallback=True,
        )

    parsed = extract_first_json_object(res.stdout)
    if not parsed:
        print("[warn] Failed to parse JSON from router output. Falling back.", file=sys.stderr)
        return RouteDecision(
            **FALLBACK_DECISION,
            refined_prompt=task,
            router_latency_ms=latency_ms,
            is_fallback=True,
        )

    allowed_pairs = {(c["tool"], c["model"]) for c in CANDIDATES}
    tool = str(parsed.get("tool", ""))
    model = str(parsed.get("model", ""))
    is_fallback = False

    if (tool, model) not in allowed_pairs:
        print(f"[warn] Disallowed (tool, model)=({tool}, {model}). Falling back.", file=sys.stderr)
        tool = FALLBACK_DECISION["tool"]
        model = FALLBACK_DECISION["model"]
        is_fallback = True

    difficulty = str(parsed.get("difficulty", "medium"))
    if difficulty not in {"low", "medium", "high"}:
        difficulty = "medium"

    refined = str(parsed.get("refined_prompt") or "").strip() or task
    if plan_first and difficulty in {"medium", "high"} and "合意" not in refined:
        refined += "\n\n※まずは考えられる解決アプローチと実装順序の案を提示し、合意を得てから編集に着手してください。"

    return RouteDecision(
        difficulty=difficulty,
        tool=tool,
        model=model,
        branch_slug=sanitize_slug(str(parsed.get("branch_slug", "ai-task"))),
        reason=str(parsed.get("reason", "")),
        refined_prompt=refined,
        router_latency_ms=latency_ms,
        is_fallback=is_fallback,
    )


# ==============================================================================
# git worktree 管理
# ==============================================================================

def create_worktree(repo_root: Path, slug: str, base_ref: str = "HEAD") -> tuple[Path, str]:
    status = run_cmd(["git", "status", "--porcelain"], cwd=repo_root, check=False).stdout.strip()
    if status:
        print(
            "[warn] メインリポジトリに未コミットの変更があります。"
            "新規worktreeは指定ベースコミットから作成されるため、未コミット分は含まれません。",
            file=sys.stderr,
        )

    ts = datetime.datetime.now().strftime("%m%d-%H%M%S")
    branch_name = f"ai/{slug}-{ts}"
    wt_base = repo_root.parent / f"{repo_root.name}-worktrees"
    wt_base.mkdir(parents=True, exist_ok=True)
    wt_path = wt_base / f"{slug}-{ts}"

    run_cmd(["git", "worktree", "add", "-b", branch_name, str(wt_path), base_ref], cwd=repo_root)

    for rel in WORKTREE_COPY_FILES:
        src = repo_root / rel
        dst = wt_path / rel
        if src.is_file() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    return wt_path, branch_name


def cmd_list(_: argparse.Namespace) -> None:
    repo_root = get_git_root()
    if not repo_root:
        print("Gitリポジトリ内で実行してください。", file=sys.stderr)
        sys.exit(1)
    res = run_cmd(["git", "worktree", "list"], cwd=repo_root)
    print(res.stdout, end="")


def cmd_clean(args: argparse.Namespace) -> None:
    repo_root = get_git_root()
    if not repo_root:
        print("Gitリポジトリ内で実行してください。", file=sys.stderr)
        sys.exit(1)

    target = Path(args.path).expanduser().resolve()
    branch_name = ""
    if target.exists():
        branch_res = run_cmd(["git", "branch", "--show-current"], cwd=target, check=False)
        if branch_res.returncode == 0:
            branch_name = branch_res.stdout.strip()

    rm_cmd = ["git", "worktree", "remove"]
    if args.force:
        rm_cmd.append("--force")
    rm_cmd.append(str(target))
    run_cmd(rm_cmd, cwd=repo_root, capture=False)
    print(f"Removed worktree: {target}")

    if args.delete_branch and branch_name and branch_name.startswith("ai/"):
        del_flag = "-D" if args.force else "-d"
        run_cmd(["git", "branch", del_flag, branch_name], cwd=repo_root, capture=False)
        print(f"Deleted branch: {branch_name}")


# ==============================================================================
# JSONL ログ記録 & セッション追跡
# ==============================================================================

def append_jsonl(record: dict[str, Any]) -> None:
    """排他ロック付きで JSONL に1行追記"""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with LOG_FILE.open("a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def session_meta_path(workdir: Path) -> Path | None:
    git_dir = get_git_dir(workdir)
    if not git_dir:
        return None
    return git_dir / "ai-route-session.json"


def save_session_meta(workdir: Path, meta: dict[str, Any]) -> None:
    path = session_meta_path(workdir)
    if not path:
        return
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def load_session_meta(workdir: Path) -> dict[str, Any] | None:
    path = session_meta_path(workdir)
    if not path or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


# ==============================================================================
# エージェント実行コマンド構築 & tmux ディスパッチ
# ==============================================================================

def write_prompt_file(prompt: str) -> Path:
    fd, path_str = tempfile.mkstemp(prefix="ai-route-prompt-", suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(prompt)
    return Path(path_str)


def build_agent_shell_cmd(tool: str, model: str, prompt_file: Path, mode: str) -> str:
    pf = shlex.quote(str(prompt_file))
    m = shlex.quote(model)

    if tool == "claude":
        if mode == "headless":
            return f'claude -p "$(cat {pf})" --model {m}; rm -f {pf}; exec "$SHELL"'
        return f'claude --model {m} "$(cat {pf})"; rm -f {pf}; exec "$SHELL"'

    if tool == "opencode":
        if mode == "headless":
            return f'opencode run -m {m} "$(cat {pf})"; rm -f {pf}; exec "$SHELL"'
        return f'opencode -m {m} --prompt "$(cat {pf})"; rm -f {pf}; exec "$SHELL"'

    if tool == "agy":
        if mode == "headless":
            return f'agy -p "$(cat {pf})" --model {m}; rm -f {pf}; exec "$SHELL"'
        return f'agy --model {m}; rm -f {pf}; exec "$SHELL"'

    raise ValueError(f"Unsupported tool: {tool}")


def run_agent_headless_once(tool: str, model: str, prompt: str, cwd: Path) -> tuple[int, str, int]:
    """ヘッドレスで1モデルを実行し (returncode, output, latency_ms) を返す"""
    if tool == "claude":
        cmd = ["claude", "-p", prompt, "--model", model]
    elif tool == "agy":
        cmd = ["agy", "-p", prompt, "--model", model]
    elif tool == "opencode":
        cmd = ["opencode", "run"]
        if OPENCODE_ATTACH_URL:
            cmd.extend(["--attach", OPENCODE_ATTACH_URL])
        cmd.extend(["-m", model, prompt])
    else:
        raise ValueError(f"Unsupported tool: {tool}")

    t0 = time.monotonic()
    res = run_cmd(cmd, cwd=cwd, check=False)
    latency_ms = int((time.monotonic() - t0) * 1000)
    output = (res.stdout or "").strip()
    if res.returncode != 0 and res.stderr:
        output = f"{output}\n[stderr]\n{res.stderr.strip()}".strip()
    return res.returncode, output, latency_ms


def dispatch_to_tmux(
    *,
    shell_cmd: str,
    workdir: Path,
    win_name: str,
    tool: str,
    mode: str,
    prompt_file: Path,
    split_current: bool = False,
    pane_title: str = "",
) -> None:
    if not os.environ.get("TMUX"):
        print("[info] tmux外のため現在のシェルで起動します。")
        os.chdir(workdir)
        os.execvp("bash", ["bash", "-lc", shell_cmd])

    if split_current:
        tmux_cmd = [
            "tmux", "split-window", "-h",
            "-P", "-F", "#{pane_id}",
            "-c", str(workdir),
            shell_cmd,
        ]
    else:
        tmux_cmd = [
            "tmux", "new-window",
            "-P", "-F", "#{pane_id}",
            "-n", win_name,
            "-c", str(workdir),
            shell_cmd,
        ]

    pane_id = run_cmd(tmux_cmd).stdout.strip()
    if pane_title:
        run_cmd(["tmux", "select-pane", "-t", pane_id, "-T", pane_title], check=False)

    if tool == "agy" and mode == "interactive":
        time.sleep(AGY_PASTE_DELAY_SEC)
        run_cmd(["tmux", "load-buffer", str(prompt_file)])
        run_cmd(["tmux", "paste-buffer", "-t", pane_id])
        run_cmd(["tmux", "send-keys", "-t", pane_id, "Enter"])

    target_desc = "split pane" if split_current else f"window '{win_name}'"
    print(f"Dispatched to tmux {target_desc} ({pane_id}) in {workdir}")


def dispatch_tiled_panes(*, win_name: str, entries: list[dict[str, Any]]) -> None:
    """1つの新規 tmux ウィンドウに複数ペインを作成し tiled レイアウトで並列起動"""
    if not os.environ.get("TMUX"):
        print("[error] 並列対話モードは tmux セッション内で実行してください。", file=sys.stderr)
        sys.exit(1)

    pane_ids: list[tuple[str, dict[str, Any]]] = []
    win_target = ""

    for idx, ent in enumerate(entries):
        if idx == 0:
            cmd = [
                "tmux", "new-window",
                "-P", "-F", "#{window_id} #{pane_id}",
                "-n", win_name,
                "-c", str(ent["workdir"]),
                ent["shell_cmd"],
            ]
            out = run_cmd(cmd).stdout.strip().split()
            win_target, pane_id = out[0], out[1]
        else:
            cmd = [
                "tmux", "split-window",
                "-t", win_target,
                "-P", "-F", "#{pane_id}",
                "-c", str(ent["workdir"]),
                ent["shell_cmd"],
            ]
            pane_id = run_cmd(cmd).stdout.strip()
            run_cmd(["tmux", "select-layout", "-t", win_target, "tiled"], check=False)

        run_cmd(["tmux", "select-pane", "-t", pane_id, "-T", ent["title"]], check=False)
        pane_ids.append((pane_id, ent))

    run_cmd(["tmux", "select-layout", "-t", win_target, "tiled"], check=False)

    agy_panes = [(pid, ent) for pid, ent in pane_ids if ent["tool"] == "agy" and ent["mode"] == "interactive"]
    if agy_panes:
        time.sleep(AGY_PASTE_DELAY_SEC)
        for pid, ent in agy_panes:
            run_cmd(["tmux", "load-buffer", str(ent["prompt_file"])])
            run_cmd(["tmux", "paste-buffer", "-t", pid])
            run_cmd(["tmux", "send-keys", "-t", pid, "Enter"])

    print(f"Dispatched {len(pane_ids)} panes in tmux window '{win_name}' ({win_target})")


# ==============================================================================
# サブコマンド: run / handoff
# ==============================================================================

def cmd_run(args: argparse.Namespace) -> None:
    repo_root = get_git_root()
    repo_ctx = get_git_context(repo_root) if repo_root else "(not a git repository)"
    task_text = load_task_text(args.task, args.task_file)
    resolved_files = resolve_files(args.files or [], repo_root)
    files_ctx = format_files_for_router(resolved_files)

    if args.tool and args.model:
        decision = RouteDecision(
            difficulty="manual",
            tool=args.tool,
            model=args.model,
            branch_slug=sanitize_slug(args.slug or "manual-task"),
            reason="ユーザーによる明示指定",
            refined_prompt=task_text,
        )
    else:
        decision = route_task(
            task_text,
            repo_ctx,
            files_ctx,
            plan_first=not args.no_plan_first,
        )

    agent_files_block = format_files_for_agent(resolved_files, using_worktree=bool(args.worktree))
    if agent_files_block:
        decision.refined_prompt = f"{agent_files_block}\n\n{decision.refined_prompt}"

    print("========================================")
    print(f" Difficulty : {decision.difficulty}")
    print(f" Selected   : {decision.tool} ({decision.model})")
    print(f" Reason     : {decision.reason}")
    if resolved_files:
        print(f" Files      : {', '.join(rf.display_path for rf in resolved_files)}")
    print("========================================")

    if args.dry_run:
        print("\n[Dry Run] Refined Prompt:\n" + decision.refined_prompt)
        return

    workdir = Path.cwd()
    if args.worktree:
        if not repo_root:
            print("[error] --worktree はGitリポジトリ内でのみ使用できます。", file=sys.stderr)
            sys.exit(1)
        workdir, branch = create_worktree(repo_root, decision.branch_slug, base_ref=args.base)
        print(f" Worktree   : {workdir} (branch: {branch})")

    session_id = uuid.uuid4().hex[:8]
    repo_name, branch_name = get_repo_and_branch(workdir)

    save_session_meta(
        workdir,
        {
            "session_id": session_id,
            "current_tool": decision.tool,
            "current_model": decision.model,
            "handoff_count": 0,
            "started_at": now_iso(),
        },
    )

    append_jsonl(
        {
            "ts": now_iso(),
            "event": "route",
            "session_id": session_id,
            "repo": repo_name,
            "branch": branch_name,
            "workdir": str(workdir),
            "tool": decision.tool,
            "model": decision.model,
            "difficulty": decision.difficulty,
            "mode": args.mode,
            "worktree": bool(args.worktree),
            "router_model": None if (args.tool and args.model) else ROUTER_MODEL,
            "router_latency_ms": decision.router_latency_ms,
            "is_fallback": decision.is_fallback,
            "files": [rf.display_path for rf in resolved_files],
            "task": task_text,
            "reason": decision.reason,
        }
    )

    prompt_file = write_prompt_file(decision.refined_prompt)
    shell_cmd = build_agent_shell_cmd(decision.tool, decision.model, prompt_file, args.mode)
    win_name = f"{decision.tool}-{decision.branch_slug[:14]}"

    dispatch_to_tmux(
        shell_cmd=shell_cmd,
        workdir=workdir,
        win_name=win_name,
        tool=decision.tool,
        mode=args.mode,
        prompt_file=prompt_file,
        split_current=args.split,
        pane_title=f"{decision.tool}:{decision.model[:18]}",
    )


def cmd_handoff(args: argparse.Namespace) -> None:
    """現在の worktree の diff を引き継ぎ、同一 tmux ウィンドウの分割ペインで別モデルを起動"""
    cwd = Path.cwd()
    repo_root = get_git_root(cwd)
    resolved_files = resolve_files(args.files or [], repo_root)
    agent_files_block = format_files_for_agent(resolved_files, using_worktree=False)

    diff_stat = run_cmd(["git", "diff", "--stat"], cwd=cwd, check=False).stdout.strip()
    status = run_cmd(["git", "status", "--short"], cwd=cwd, check=False).stdout.strip()

    handoff_prompt = (
        f"現在作業中のworktreeを引き継ぎます。\n\n"
        f"{agent_files_block + chr(10) + chr(10) if agent_files_block else ''}"
        f"## 現在の変更状況\n```\n{status or '(no changes)'}\n{diff_stat}\n```\n\n"
        f"## 引き継ぎ指示\n{args.note}\n\n"
        f"現在の差分を確認したうえで、方針の確認と続きの実装を進めてください。"
    )

    meta = load_session_meta(cwd) or {}
    session_id = str(meta.get("session_id") or uuid.uuid4().hex[:8])
    from_tool = str(meta.get("current_tool", "unknown"))
    from_model = str(meta.get("current_model", "unknown"))
    handoff_seq = int(meta.get("handoff_count", 0)) + 1
    repo_name, branch_name = get_repo_and_branch(cwd)

    save_session_meta(
        cwd,
        {
            "session_id": session_id,
            "current_tool": args.tool,
            "current_model": args.model,
            "handoff_count": handoff_seq,
            "started_at": meta.get("started_at", now_iso()),
        },
    )

    append_jsonl(
        {
            "ts": now_iso(),
            "event": "handoff",
            "session_id": session_id,
            "repo": repo_name,
            "branch": branch_name,
            "workdir": str(cwd),
            "from_tool": from_tool,
            "from_model": from_model,
            "tool": args.tool,
            "model": args.model,
            "handoff_seq": handoff_seq,
            "files": [rf.display_path for rf in resolved_files],
            "note": args.note,
        }
    )

    prompt_file = write_prompt_file(handoff_prompt)
    shell_cmd = build_agent_shell_cmd(args.tool, args.model, prompt_file, "interactive")
    dispatch_to_tmux(
        shell_cmd=shell_cmd,
        workdir=cwd,
        win_name=f"handoff-{args.tool}",
        tool=args.tool,
        mode="interactive",
        prompt_file=prompt_file,
        split_current=True,
        pane_title=f"ho:{args.tool}:{args.model[:16]}",
    )


# ==============================================================================
# サブコマンド: review（複数モデル並列レビュー）
# ==============================================================================

def collect_review_target(args: argparse.Namespace, cwd: Path, repo_root: Path | None) -> str:
    sections: list[str] = []

    if args.files:
        resolved = resolve_files(args.files, repo_root)
        for rf in resolved:
            try:
                content = rf.abs_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = "(binary file)"
            sections.append(f"### File: {rf.display_path}\n```\n{content}\n```")

    if not args.files or args.include_diff:
        if repo_root:
            if args.staged:
                diff_cmd = ["git", "diff", "--cached"]
                label = "git diff --cached"
            elif args.base:
                diff_cmd = ["git", "diff", f"{args.base}...HEAD"]
                label = f"git diff {args.base}...HEAD"
            else:
                diff_cmd = ["git", "diff", "HEAD"]
                label = "git diff HEAD"
            diff_out = run_cmd(diff_cmd, cwd=cwd, check=False).stdout.strip()
            if diff_out:
                sections.append(f"### {label}\n```diff\n{diff_out}\n```")

    if not sections:
        print(
            "[error] レビュー対象の差分もファイルも見つかりません（-f または --staged / --base を指定してください）。",
            file=sys.stderr,
        )
        sys.exit(1)

    return "\n\n".join(sections)


def cmd_review(args: argparse.Namespace) -> None:
    cwd = Path.cwd()
    repo_root = get_git_root(cwd)
    specs = parse_model_specs(args.models, args.preset)
    target_text = collect_review_target(args, cwd, repo_root)
    focus = args.focus or "バグ、設計上の懸念、エッジケース、パフォーマンス、保守性"

    group_id = uuid.uuid4().hex[:8]
    repo_name, branch_name = get_repo_and_branch(cwd)

    def build_review_prompt(idx: int) -> str:
        role_line = ""
        if args.roles:
            role_line = f"\n- **あなたの担当観点:** {REVIEW_ROLES[idx % len(REVIEW_ROLES)]}"
        return (
            "あなたは厳格なシニアソフトウェアエンジニアです。以下の変更内容／コードをレビューしてください。\n"
            "ファイルの直接編集は行わず、指摘と改善案のみを出力してください。\n\n"
            f"- **重点確認テーマ:** {focus}{role_line}\n"
            "- **出力形式:** 重要度別（Critical / Warning / Suggestion）に、該当ファイル・箇所・理由・修正案を簡潔に記載すること。\n\n"
            f"## レビュー対象\n{target_text}\n"
        )

    if args.mode == "interactive":
        entries: list[dict[str, Any]] = []
        for idx, (tool, model) in enumerate(specs):
            pf = write_prompt_file(build_review_prompt(idx))
            shell_cmd = build_agent_shell_cmd(tool, model, pf, "interactive")
            entries.append(
                {
                    "shell_cmd": shell_cmd,
                    "workdir": cwd,
                    "tool": tool,
                    "model": model,
                    "mode": "interactive",
                    "prompt_file": pf,
                    "title": f"rev:{tool}:{model[:16]}",
                }
            )

        append_jsonl(
            {
                "ts": now_iso(),
                "event": "review",
                "group_id": group_id,
                "repo": repo_name,
                "branch": branch_name,
                "mode": "interactive",
                "models": [f"{t}:{m}" for t, m in specs],
                "files": args.files or [],
                "focus": focus,
            }
        )
        dispatch_tiled_panes(win_name=f"rev-{group_id[:6]}", entries=entries)
        return

    print(f"=== Multi-Model Review (group={group_id}, models={len(specs)}) ===")
    results: list[dict[str, Any]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(specs)) as ex:
        fut_map = {
            ex.submit(run_agent_headless_once, tool, model, build_review_prompt(idx), cwd): (idx, tool, model)
            for idx, (tool, model) in enumerate(specs)
        }
        for fut in concurrent.futures.as_completed(fut_map):
            idx, tool, model = fut_map[fut]
            rc, out, lat = fut.result()
            results.append({"idx": idx, "tool": tool, "model": model, "rc": rc, "output": out, "latency_ms": lat})
            print(f"  [done] {tool}:{model} ({lat} ms, rc={rc})")

    results.sort(key=lambda r: r["idx"])

    report_parts = [f"# Multi-Model Code Review (group: {group_id})\n"]
    for r in results:
        report_parts.append(f"## {r['tool']} ({r['model']}) [{r['latency_ms']} ms]\n\n{r['output']}\n")

    if args.synthesize:
        if args.synthesizer and ":" in args.synthesizer:
            syn_tool, syn_model = args.synthesizer.split(":", 1)
            syn_tool, syn_model = syn_tool.strip(), syn_model.strip()
        else:
            syn_tool, syn_model = "opencode", ROUTER_MODEL

        syn_prompt = (
            "以下は同一コードに対して複数のAIモデルが独立して行ったコードレビュー結果です。\n"
            "これらを統合し、次の3セクションで簡潔な最終レビューサマリを日本語で作成してください：\n"
            "1. **複数モデルが一致して指摘した重要課題（優先対応）**\n"
            "2. **特定モデルのみが指摘した注目点（見落とし防止）**\n"
            "3. **モデル間で見解が割れた論点・設計トレードオフ**\n\n"
            + "\n---\n".join(report_parts[1:])
        )
        print(f"  [synthesizing with {syn_tool}:{syn_model}...]")
        _, syn_out, syn_lat = run_agent_headless_once(syn_tool, syn_model, syn_prompt, cwd)
        report_parts.insert(1, f"## 統合サマリ ({syn_tool}:{syn_model}, {syn_lat} ms)\n\n{syn_out}\n\n---\n")

    full_report = "\n".join(report_parts)

    out_path = Path(args.output) if args.output else (LOG_FILE.parent / "reviews" / f"review-{group_id}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(full_report, encoding="utf-8")

    append_jsonl(
        {
            "ts": now_iso(),
            "event": "review",
            "group_id": group_id,
            "repo": repo_name,
            "branch": branch_name,
            "mode": "headless",
            "models": [f"{t}:{m}" for t, m in specs],
            "synthesized": bool(args.synthesize),
            "report_path": str(out_path),
            "files": args.files or [],
            "focus": focus,
        }
    )

    print("\n" + full_report)
    print(f"\n[saved] {out_path}")


# ==============================================================================
# サブコマンド: multi（複数モデル並列実装・問題解決） & compare
# ==============================================================================

def cmd_multi(args: argparse.Namespace) -> None:
    if not os.environ.get("TMUX"):
        print("[error] multi は並列ペイン／ウィンドウ生成のため tmux セッション内で実行してください。", file=sys.stderr)
        sys.exit(1)

    repo_root = get_git_root()
    if not repo_root:
        print("[error] multi は Git リポジトリ内でのみ使用できます（worktree隔離が必須のため）。", file=sys.stderr)
        sys.exit(1)

    task_text = load_task_text(args.task, args.task_file)
    resolved_files = resolve_files(args.files or [], repo_root)
    specs = parse_model_specs(args.models, args.preset)
    group_id = uuid.uuid4().hex[:6]
    base_slug = sanitize_slug(args.slug or f"multi-{group_id}")

    agent_files_block = format_files_for_agent(resolved_files, using_worktree=True)
    base_prompt = f"{agent_files_block}\n\n{task_text}" if agent_files_block else task_text
    if not args.no_plan_first and args.mode == "interactive":
        base_prompt += "\n\n※まずは考えられる解決アプローチと実装順序の案を提示し、合意を得てから編集に着手してください。"

    print(f"=== Multi-Model Parallel Solve (group={group_id}, base={args.base}) ===")

    entries: list[dict[str, Any]] = []
    created_members: list[dict[str, Any]] = []

    for idx, (tool, model) in enumerate(specs, 1):
        model_tag = sanitize_slug(f"{tool}-{model}")[:16]
        slug = f"m{group_id}-{idx}-{model_tag}"
        wt_path, branch_name = create_worktree(repo_root, slug, base_ref=args.base)

        session_id = f"{group_id}-{idx}"
        save_session_meta(
            wt_path,
            {
                "session_id": session_id,
                "group_id": group_id,
                "current_tool": tool,
                "current_model": model,
                "handoff_count": 0,
                "started_at": now_iso(),
            },
        )

        pf = write_prompt_file(base_prompt)
        shell_cmd = build_agent_shell_cmd(tool, model, pf, args.mode)

        entries.append(
            {
                "shell_cmd": shell_cmd,
                "workdir": wt_path,
                "tool": tool,
                "model": model,
                "mode": args.mode,
                "prompt_file": pf,
                "title": f"{idx}:{tool}:{model[:14]}",
                "win_name": f"m{group_id}-{tool[:4]}{idx}",
            }
        )
        created_members.append(
            {
                "index": idx,
                "tool": tool,
                "model": model,
                "branch": branch_name,
                "workdir": str(wt_path),
            }
        )
        print(f"  [{idx}] {tool}:{model:<28} -> {wt_path} ({branch_name})")

    repo_name, _ = get_repo_and_branch(repo_root)
    append_jsonl(
        {
            "ts": now_iso(),
            "event": "multi",
            "group_id": group_id,
            "repo": repo_name,
            "base_ref": args.base,
            "mode": args.mode,
            "layout": args.layout,
            "members": created_members,
            "files": [rf.display_path for rf in resolved_files],
            "task": task_text,
        }
    )

    if args.layout == "tiled":
        dispatch_tiled_panes(win_name=f"multi-{base_slug[:12]}", entries=entries)
    else:
        for ent in entries:
            dispatch_to_tmux(
                shell_cmd=ent["shell_cmd"],
                workdir=ent["workdir"],
                win_name=ent["win_name"],
                tool=ent["tool"],
                mode=ent["mode"],
                prompt_file=ent["prompt_file"],
                split_current=False,
                pane_title=ent["title"],
            )

    print(f"\n比較コマンド: ai-route compare {group_id}")


def cmd_compare(args: argparse.Namespace) -> None:
    """multi で並列実行したグループの複数 worktree の差分・状態を横並び比較"""
    if not LOG_FILE.is_file():
        print("ログファイルがありません。", file=sys.stderr)
        sys.exit(1)

    target_event: dict[str, Any] | None = None
    with LOG_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") == "multi":
                if not args.group_id or rec.get("group_id") == args.group_id:
                    target_event = rec

    if not target_event:
        print(f"該当する multi グループが見つかりません: {args.group_id or '(latest)'}", file=sys.stderr)
        sys.exit(1)

    gid = target_event["group_id"]
    base_ref = target_event.get("base_ref", "HEAD")
    print(f"=== Multi-Model Comparison (group={gid}, base={base_ref}) ===")
    print(f"Task: {str(target_event.get('task', '')).splitlines()[0][:80]}\n")

    for m in target_event.get("members", []):
        wt = Path(m["workdir"])
        print("-" * 72)
        print(f"[{m['index']}] {m['tool']}:{m['model']}  (branch: {m['branch']})")
        if not wt.exists():
            print("  (worktree 削除済み)")
            continue

        status = run_cmd(["git", "status", "--short"], cwd=wt, check=False).stdout.strip()
        commits = run_cmd(["git", "log", "--oneline", f"{base_ref}..HEAD"], cwd=wt, check=False).stdout.strip()
        diff_stat = run_cmd(["git", "diff", "--stat", base_ref], cwd=wt, check=False).stdout.strip()

        print(f"  Path   : {wt}")
        print(f"  Commits: {len(commits.splitlines()) if commits else 0}")
        if commits:
            for c in commits.splitlines()[:5]:
                print(f"    {c}")
        if status:
            print("  Uncommitted:")
            for s in status.splitlines()[:10]:
                print(f"    {s}")
        if diff_stat:
            print("  Diff stat vs base:")
            for d in diff_stat.splitlines():
                print(f"    {d}")

        if args.test_cmd:
            print(f"  Running test: {args.test_cmd}")
            t_res = run_cmd(["bash", "-lc", args.test_cmd], cwd=wt, check=False)
            verdict = "PASS" if t_res.returncode == 0 else f"FAIL (rc={t_res.returncode})"
            print(f"  Test result : {verdict}")


# ==============================================================================
# サブコマンド: stats（振り返り集計）
# ==============================================================================

def cmd_stats(args: argparse.Namespace) -> None:
    if not LOG_FILE.is_file():
        print(f"ログファイルがまだありません: {LOG_FILE}")
        return

    cutoff: datetime.datetime | None = None
    if args.days > 0:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=args.days)

    repo_filter = args.repo
    if args.here:
        repo_filter, _ = get_repo_and_branch(Path.cwd())

    events: list[dict[str, Any]] = []
    with LOG_FILE.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                print(f"[warn] Skipping malformed JSON at line {lineno}", file=sys.stderr)
                continue

            if repo_filter and rec.get("repo") != repo_filter:
                continue
            if cutoff:
                try:
                    ev_dt = datetime.datetime.fromisoformat(rec["ts"]).astimezone(datetime.timezone.utc)
                    if ev_dt < cutoff:
                        continue
                except (KeyError, ValueError):
                    pass
            events.append(rec)

    routes = [e for e in events if e.get("event") == "route"]
    handoffs = [e for e in events if e.get("event") == "handoff"]
    reviews = [e for e in events if e.get("event") == "review"]
    multis = [e for e in events if e.get("event") == "multi"]

    if not routes and not handoffs and not reviews and not multis:
        print("指定条件に一致するログがありません。")
        return

    handoffs_by_session: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for h in handoffs:
        handoffs_by_session[str(h.get("session_id", ""))].append(h)

    model_counts: collections.Counter[str] = collections.Counter()
    model_handoff_sessions: collections.Counter[str] = collections.Counter()
    diff_counts: collections.Counter[str] = collections.Counter()
    latencies: list[int] = []
    fallback_count = 0

    for r in routes:
        key = f'{r.get("tool")} ({r.get("model")})'
        model_counts[key] += 1
        diff_counts[str(r.get("difficulty", "unknown"))] += 1
        if r.get("is_fallback"):
            fallback_count += 1
        if isinstance(r.get("router_latency_ms"), int) and r["router_latency_ms"] > 0:
            latencies.append(r["router_latency_ms"])
        if handoffs_by_session.get(str(r.get("session_id", ""))):
            model_handoff_sessions[key] += 1

    transitions: collections.Counter[str] = collections.Counter()
    for h in handoffs:
        src = f'{h.get("from_tool", "?")} ({h.get("from_model", "?")})'
        dst = f'{h.get("tool", "?")} ({h.get("model", "?")})'
        transitions[f"{src} -> {dst}"] += 1

    total_routes = len(routes)
    sessions_with_ho = sum(1 for r in routes if handoffs_by_session.get(str(r.get("session_id", ""))))
    ho_rate = (sessions_with_ho / total_routes * 100) if total_routes else 0.0
    avg_lat = (sum(latencies) / len(latencies)) if latencies else 0.0

    scope = f"過去{args.days}日間" if args.days > 0 else "全期間"
    if repo_filter:
        scope += f" / repo={repo_filter}"

    print(f"=== ai-route 統計 ({scope}) ===")
    print(f"総タスク数       : {total_routes}")
    print(f"総handoff回数    : {len(handoffs)} (handoff発生セッション: {sessions_with_ho}/{total_routes} = {ho_rate:.1f}%)")
    print(f"複数モデル実行   : review {len(reviews)} 回 / multi {len(multis)} 回")
    print(f"ルーター平均応答 : {avg_lat:.0f} ms / フォールバック: {fallback_count} 回")

    if total_routes > 0:
        print("\n--- 初回振り分けモデル別 ---")
        print(f"{'Model':<42} {'Count':>5} {'Share':>7} {'Handoff発生':>12}")
        for model_key, cnt in model_counts.most_common():
            share = cnt / total_routes * 100
            ho_s = model_handoff_sessions[model_key]
            ho_pct = (ho_s / cnt * 100) if cnt else 0.0
            print(f"{model_key:<42} {cnt:>5} {share:>6.1f}% {ho_s:>4} ({ho_pct:>4.0f}%)")

        print("\n--- 難易度別 ---")
        for d in ("low", "medium", "high", "manual"):
            if diff_counts[d]:
                print(f"  {d:<8}: {diff_counts[d]}")

    if transitions:
        print("\n--- Handoff 遷移パターン ---")
        for trans, cnt in transitions.most_common():
            print(f"  {cnt:>3}x  {trans}")

    if args.recent > 0 and routes:
        print(f"\n--- 直近 {min(args.recent, len(routes))} 件のセッション ---")
        for r in routes[-args.recent :][::-1]:
            sid = str(r.get("session_id", ""))
            ho_list = handoffs_by_session.get(sid, [])
            ho_tag = f"HOx{len(ho_list)}" if ho_list else "HOx0"
            ts_short = str(r.get("ts", ""))[:16].replace("T", " ")
            task_short = str(r.get("task", "")).replace("\n", " ")[:48]
            print(f"  [{ts_short}] {sid} | {r.get('tool')}:{r.get('model')} | {ho_tag} | {task_short}")


# ==============================================================================
# CLI エントリーポイント
# ==============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ai-route",
        description="AI Coding Agent Router with tmux, git worktree, handoff, multi-model review/solve, and JSONL stats",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # run
    p_run = sub.add_parser("run", help="タスクを判定してエージェントを起動")
    p_run.add_argument("task", nargs="?", default=None, help="実装内容や問題の説明（-F 指定時は省略・補足どちらも可）")
    p_run.add_argument("-f", "--file", dest="files", action="append", default=[], help="対象・参照ファイル（複数回指定可）")
    p_run.add_argument("-F", "--task-file", help="タスク説明を読み込むファイル（'-' で標準入力）")
    p_run.add_argument("-w", "--worktree", action="store_true", help="新規 git worktree を切って隔離実行")
    p_run.add_argument("--base", default="HEAD", help="worktreeのベースブランチ/コミット (default: HEAD)")
    p_run.add_argument("--slug", default="", help="手動指定時のブランチスラッグ")
    p_run.add_argument("--mode", choices=["interactive", "headless"], default="interactive", help="実行モード")
    p_run.add_argument("--split", action="store_true", help="新規ウィンドウではなく現在のペインを分割して起動")
    p_run.add_argument("--no-plan-first", action="store_true", help="方針合意先行の指示を付与しない")
    p_run.add_argument("--tool", choices=["opencode", "agy", "claude"], help="ルーターをスキップしてツールを固定")
    p_run.add_argument("--model", help="--tool と併用してモデルを固定")
    p_run.add_argument("--dry-run", action="store_true", help="判定結果と生成プロンプトのみ表示")
    p_run.set_defaults(func=cmd_run)

    # handoff
    p_ho = sub.add_parser("handoff", help="現在のworktreeで別モデルにペイン分割して引き継ぐ")
    p_ho.add_argument("note", help="引き継ぎたい課題や次ステップの指示")
    p_ho.add_argument("-f", "--file", dest="files", action="append", default=[], help="引き継ぎ時に参照させたいファイル（複数回指定可）")
    p_ho.add_argument("--tool", choices=["opencode", "agy", "claude"], default="claude")
    p_ho.add_argument("--model", default="opus")
    p_ho.set_defaults(func=cmd_handoff)

    # review
    p_rev = sub.add_parser("review", help="複数モデルで並列コードレビューを実行")
    p_rev.add_argument("-f", "--file", dest="files", action="append", default=[], help="レビュー対象ファイル（複数回指定可）")
    p_rev.add_argument("--staged", action="store_true", help="ステージ済み差分 (git diff --cached) をレビュー")
    p_rev.add_argument("--base", default="", help="指定ブランチからの差分 (base...HEAD) をレビュー")
    p_rev.add_argument("--include-diff", action="store_true", help="-f 指定時にも git diff を併せて含める")
    p_rev.add_argument("--focus", default="", help="特に見てほしい観点（例: メモリ安全性と競合状態）")
    p_rev.add_argument("--preset", choices=list(MODEL_PRESETS.keys()), default="balanced", help="モデル組み合わせプリセット")
    p_rev.add_argument("--models", default="", help="明示指定 例: 'claude:sonnet,agy:Gemini 3.7 Flash (Low)'")
    p_rev.add_argument("--roles", action="store_true", help="モデルごとに異なるレビュー観点を割り当てる")
    p_rev.add_argument("--mode", choices=["headless", "interactive"], default="headless", help="実行モード (default: headless)")
    p_rev.add_argument("--synthesize", action="store_true", help="複数モデルのレビュー結果を1つに統合サマリ化")
    p_rev.add_argument("--synthesizer", default="", help="統合役モデル 'tool:model' (省略時はルーター軽量モデル)")
    p_rev.add_argument("-o", "--output", default="", help="レビューレポートの保存先パス")
    p_rev.set_defaults(func=cmd_review)

    # multi
    p_mul = sub.add_parser("multi", help="複数モデルに独立 worktree で同一タスクを並列解決させる")
    p_mul.add_argument("task", nargs="?", default=None, help="実装内容や問題の説明")
    p_mul.add_argument("-f", "--file", dest="files", action="append", default=[], help="対象・参照ファイル（複数回指定可）")
    p_mul.add_argument("-F", "--task-file", help="タスク説明を読み込むファイル（'-' で標準入力）")
    p_mul.add_argument("--preset", choices=list(MODEL_PRESETS.keys()), default="balanced", help="モデル組み合わせプリセット")
    p_mul.add_argument("--models", default="", help="明示指定 例: 'claude:opus,agy:Gemini 3.7 Flash (Low)'")
    p_mul.add_argument("--base", default="HEAD", help="全 worktree の共通ベースコミット (default: HEAD)")
    p_mul.add_argument("--slug", default="", help="ウィンドウ名用の短いスラッグ")
    p_mul.add_argument("--mode", choices=["interactive", "headless"], default="interactive", help="実行モード (default: interactive)")
    p_mul.add_argument("--layout", choices=["tiled", "windows"], default="tiled", help="tmux配置 (default: tiled)")
    p_mul.add_argument("--no-plan-first", action="store_true", help="方針合意先行の指示を付与しない")
    p_mul.set_defaults(func=cmd_multi)

    # compare
    p_cmp = sub.add_parser("compare", help="multi で並列実行した各モデルの差分・テスト結果を比較")
    p_cmp.add_argument("group_id", nargs="?", default="", help="比較する group_id（省略時は最新の multi グループ）")
    p_cmp.add_argument("--test-cmd", default="", help="各 worktree で実行する検証コマンド（例: 'ctest --output-on-failure'）")
    p_cmp.set_defaults(func=cmd_compare)

    # list
    p_ls = sub.add_parser("list", help="アクティブなworktree一覧を表示")
    p_ls.set_defaults(func=cmd_list)

    # clean
    p_cl = sub.add_parser("clean", help="作業完了したworktreeを削除")
    p_cl.add_argument("path", help="削除対象のworktreeパス")
    p_cl.add_argument("-b", "--delete-branch", action="store_true", help="ai/ ブランチも同時に削除")
    p_cl.add_argument("-f", "--force", action="store_true", help="未コミット変更があっても強制削除")
    p_cl.set_defaults(func=cmd_clean)

    # stats
    p_st = sub.add_parser("stats", help="JSONLログからモデル振り分けとhandoff実績を集計表示")
    p_st.add_argument("--days", type=int, default=30, help="集計対象の日数 (0で全期間, default: 30)")
    p_st.add_argument("--repo", default="", help="特定リポジトリ名で絞り込み")
    p_st.add_argument("--here", action="store_true", help="現在のリポジトリのみに絞り込み")
    p_st.add_argument("--recent", type=int, default=10, help="直近セッションの表示件数 (default: 10)")
    p_st.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
