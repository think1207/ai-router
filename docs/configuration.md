# 設定リファレンス (`config.toml` & 環境変数)

`ai-router` は設定ファイルなしでも組み込みデフォルトでそのまま動作しますが、`~/.config/ai-route/config.toml` を用意することで、利用モデル・サブスクリプション・ルーター・日常の既定フラグを自由にカスタマイズできます。

---

## 1. 初回セットアップと環境診断

最も簡単な始め方は `init` と `doctor` を使う方法です。

```bash
# マシンにインストール済みの CLI を検出し、最適化された ~/.config/ai-route/config.toml を生成
ai-route init

# 上書きや別パスへの出力、事前プレビュー
ai-route init --dry-run
ai-route init --force
ai-route init --output ./custom-config.toml

# 現在の環境・設定・ルーター・クレジット状態を一括診断
ai-route doctor
# （`ai-route status --check` でも同じ出力）
```

---

## 2. 設定ファイルの探索順と優先順位

### 設定ファイルパス
1. CLI グローバル引数: `ai-route --config /path/to/config.toml ...`
2. 環境変数: `AI_ROUTE_CONFIG=/path/to/config.toml`
3. デフォルトパス: `$XDG_CONFIG_HOME/ai-route/config.toml`（未設定時は `~/.config/ai-route/config.toml`）

※ 1・2 で明示したファイルが存在しない場合は起動時エラーになります。3 のデフォルトパスが存在しない場合はエラーにならず組み込みデフォルトで動作します。

### 設定値の優先順位
```text
CLI 引数 (--effort, --worktree/--no-worktree, --router-timeout 等)
  > 環境変数 (AI_ROUTE_ROUTER_TOOL, AI_ROUTE_ROUTER_TIMEOUT 等)
    > ユーザー設定ファイル (config.toml)
      > 組み込みデフォルト
```

---

## 3. `config.toml` 全セクション詳細

```toml
# ===========================================================================
# 1. 利用可能ツールと環境自動検出
# ===========================================================================

# サポート対象とするツール一覧
supported_tools = ["opencode", "agy", "claude", "copilot", "codex"]

# true の場合、PATH 上に存在しない CLI をルーティング・フェイルオーバー候補から自動除外
auto_detect_tools = true

# true にすると組み込みデフォルト候補をすべて破棄し、下の [[candidates]] のみを使用する
reset_candidates = false


# ===========================================================================
# 2. 候補モデルの追加・上書き・削除
# ===========================================================================

# (tool, model) が既存候補と一致すれば上書き、新規なら追加される
# difficulty: "low" | "medium" | "high"
# effort (任意): ルーターが省略した場合の既定 effort ("low"|"medium"|"high"|"xhigh"|"max")
[[candidates]]
tool = "claude"
model = "sonnet"
difficulty = "medium"
effort = "medium"
description = "複雑なロジック実装、精密なコード修正、デバッグ"

[[candidates]]
tool = "copilot"
model = "gpt-5.6-sol"
difficulty = "medium"
effort = "medium"
description = "中規模な実装、コード修正、GitHub 連携開発"

[[candidates]]
tool = "codex"
model = "gpt-6.1-sol"
difficulty = "high"
effort = "high"
description = "高難度の設計・実装・自律的なデバッグ"

# 組み込み候補から不要なモデルを削除（"tool:model" 文字列または { tool, model } 形式）
remove_candidates = [
    "opencode:opencode-go/deepseek-v4.1-flash",
]


# ===========================================================================
# 3. サブスクリプション／アクセス許可リスト
# ===========================================================================

# 省略時は全有効候補が利用可能。
# 指定すると、ルーター候補・allowed_pairs・CLI choices (--tool / --preset) がこのリストに限定される。
allowed_models = [
    "claude:sonnet",
    "claude:opus",
    "agy:gemini-3.8-flash-medium",
    "agy:gemini-4-neon-high",
    "copilot:gpt-5.6-sol",
]


# ===========================================================================
# 4. プリセットの追加・上書き (`review --preset` / `multi --preset`)
# ===========================================================================

# "tool:model" 文字列配列 または ["tool", "model"] 配列のどちらも記述可能
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
# "all" を明示しない場合、有効な全候補から自動生成される


# ===========================================================================
# 5. 自動フォールバック先（ルーター失敗・タイムアウト時）
# ===========================================================================

[fallback]
tool = "claude"
model = "sonnet"
difficulty = "medium"
effort = "medium"
branch_slug = "ai-task"
reason = "Fallback due to router output parse error or disallowed model selection"


# ===========================================================================
# 6. ランタイム設定（環境変数・CLI 引数で上書き可能）
# ===========================================================================

[runtime]
# ルーターとして使用するサービス（空文字 = 自動選択: opencode 優先、未導入なら他の導入済みサービス）
router_tool = ""
# ルーターのクレジット切れ時に順次試す代替ルーターサービス
router_fallback_tools = ["opencode", "agy", "copilot", "claude"]
router_model = "opencode-go/deepseek-v4.1-flash"
opencode_attach_url = ""

# agy の ACP 既定モード ("auto" | "accept-edits" | "default" | "plan")
# ※ planner / review / escalate / router の読み取り専用ステージは常に "plan" を使用
agy_acp_mode = "auto"

# GitHub Copilot CLI のヘッドレス実行時に付与する権限フラグ
copilot_permission_flag = "--allow-all-tools"

# 各種タイムアウト（秒。<= 0 で無制限）
router_timeout = 60.0
review_timeout = 300.0
test_cmd_timeout = 300.0
agent_timeout = 1800.0

# クレジット切れ検知後のクールダウン期間（秒）
credit_cooldown_sec = 3600.0


# ===========================================================================
# 7. 日常 CLI フラグの既定値
# ===========================================================================

[defaults]
worktree = false          # true にすると毎回 `-w` を付けずに worktree 作成（`--no-worktree` で解除可）
mode = "interactive"      # "interactive" | "headless"
auto = false              # true にすると毎回 `--auto` 有効
split = false             # true にすると毎回 `--split` 有効
plan_first = true         # false にすると対話モードの方針確認指示を既定でオフ
preset = "balanced"       # `review` / `multi` の既定プリセット
effort = ""               # 手動 `--tool`/`--model` 指定時の既定 effort
acp_mode = "auto"         # `agy` ACP の既定モード
```

---

## 4. よくある設定パターン例

### パターン A: Claude と GitHub Copilot のみ契約している場合

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

### パターン B: 毎回 worktree + 同期自動実行 (`--auto`) を既定にしたい場合

```toml
[defaults]
worktree = true
auto = true
```

この設定を入れておけば、普段は `ai-route "テストを追加して"` だけで worktree 作成 → 自動ルーティング → 同期実行 → ブロック時エスカレーション → クレジット切れフェイルオーバーまで自動実行されます。カレントディレクトリで実行したいときだけ `ai-route --no-worktree "..."` を使います。

---

## 5. 環境変数一覧

| 環境変数 | デフォルト | 説明 |
| :--- | :--- | :--- |
| `AI_ROUTE_CONFIG` | `~/.config/ai-route/config.toml` | 設定ファイルパスを上書き |
| `AI_ROUTE_ROUTER_TOOL` | *(自動)* | ルーターに使用するサービス (`opencode` / `agy` / `copilot` / `claude` / `codex`) |
| `AI_ROUTE_ROUTER_MODEL` | `opencode-go/deepseek-v4.1-flash` | ルーターおよびレビュー統合の既定モデル |
| `AI_ROUTE_AUTO_DETECT_TOOLS` | `true` | `0`/`false` で PATH による未導入 CLI 自動除外を無効化 |
| `AI_ROUTE_AGY_ACP_MODE` | `auto` | `agy` の既定 ACP モード (`auto` / `accept-edits` / `default` / `plan`) |
| `AI_ROUTE_COPILOT_PERMISSION_FLAG` | `--allow-all-tools` | `copilot` ヘッドレス実行時の権限フラグ |
| `OPENCODE_ATTACH_URL` | *(空)* | `opencode run --attach` に渡すサーバー URL |
| `AI_ROUTE_LOG_FILE` | `~/.local/share/ai-route/history.jsonl` | JSONL 履歴ログファイルのパス |
| `AI_ROUTE_PROVIDER_STATE_FILE` | `<log dir>/provider_state.json` | クレジット切れクールダウン状態の保存先 |
| `AI_ROUTE_CREDIT_COOLDOWN_SEC` | `3600` | クレジット切れサービスのクールダウン秒数 |
| `AI_ROUTE_CREDIT_PATTERNS` | *(空)* | 追加のクレジット切れ検知正規表現（`;;` 区切り、大小文字無視） |
| `AI_ROUTE_BLOCKED_PATTERNS` | *(空)* | 追加の判断待ち検知正規表現（`;;` 区切り、大小文字無視） |
| `AI_ROUTE_ROUTER_TIMEOUT` | `60` | ルーター呼び出しタイムアウト秒（`<=0` 無制限） |
| `AI_ROUTE_REVIEW_TIMEOUT` | `300` | `review` の 1 モデルあたりタイムアウト秒 |
| `AI_ROUTE_TEST_CMD_TIMEOUT` | `300` | `compare` / `done` の `--test-cmd` タイムアウト秒 |
| `AI_ROUTE_AGENT_TIMEOUT` | `1800` | `--auto` / `--wait` / planner / escalate のタイムアウト秒 |
| `AI_ROUTE_PLANNER` | *(空)* | Two-stage の既定 planner (`tool:model`) |
| `AI_ROUTE_IMPLEMENTER` | *(空)* | Two-stage / `--then-implement` の既定 implementer (`tool:model` または `tool`) |

---

## 6. 起動時バリデーション

不正な `config.toml` は起動直後に `ConfigError` として検出され、トレースバックなしで分かりやすい `[error] ...` メッセージを出力して終了コード `1` で停止します。

主な検出項目：
- TOML 構文エラー・指定ファイルの不在
- `supported_tools` に含まれない `tool` の指定
- 空の `model` 名
- `difficulty` が `low` / `medium` / `high` 以外
- `effort` が `low` / `medium` / `high` / `xhigh` / `max` 以外
- 同一 `(tool, model)` の重複定義
- `[fallback]` や `[presets]` が利用可能候補外のモデルを参照している
- `agy_acp_mode` / `defaults.acp_mode` が不正な値
