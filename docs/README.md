# ai-router (`ai-route`) ドキュメント

> **Languages:** [English](en/README.md) | **日本語**

`ai-router`（CLI コマンド: `ai-route` / `ai-router`）の詳細な使い方・設定リファレンス・各サービス連携仕様をまとめたドキュメントです。

## 目次

1. **[使い方ガイド (`docs/usage.md`)](usage.md)**
   - 日常の 3〜4 コマンドによる基本ワークフロー
   - タスク投入（`run` 省略、パイプ入力、`-f`/`-F`、`--effort`、`--auto`、`-p`/`--plan`）
   - セッション継続・復旧（`resume` / `status` / `escalate` / `handoff`）
   - worktree の一括仕上げ（`done` / `list` / `clean`）
   - マルチモデルレビュー・並列解決（`review` / `multi` / `compare` / `pick`）
   - 統計確認（`stats`）とシェル補完（`--completions`）
2. **[設定リファレンス (`docs/configuration.md`)](configuration.md)**
   - `ai-route init` / `ai-route doctor` による初期セットアップと診断
   - 設定の優先順位（CLI > 環境変数 > `config.toml` > 組み込みデフォルト）
   - `config.toml` の全セクション・全キー仕様とカスタマイズ実例
   - 環境変数一覧と起動時バリデーション
3. **[対応サービス・ACP・クレジット切れフェイルオーバー (`docs/services-and-failover.md`)](services-and-failover.md)**
   - 対応 5 サービス（`claude` / `agy` / `opencode` / `copilot` / `codex`）の呼び出し仕様と effort マッピング
   - `agy` の ACP モード（`agy acp --stdio`）の仕組みと利用規約上の注意点
   - 環境自動検出とマルチサービス・ルーターの仕組み
   - クレジット切れ検知・クールダウン管理・別サービスへの自動フェイルオーバー
   - よくあるトラブルと対処法

---

## 最短クイックリファレンス

```bash
# 0. 初回セットアップ & 環境確認
ai-route init
ai-route doctor
eval "$(ai-route --completions zsh)"   # bash / zsh / fish

# 1. タスク実行（`run` は省略可能）
ai-route "CSV パーサーのエッジケースを修正"
ai-route -w "キャッシュ層を追加"                         # 新規 git worktree で実行
ai-route --auto "パーサーの単体テストを追加"              # 同期ヘッドレス + 自動エスカレーション + クレジット切替
ai-route -p -w "認証ミドルウェアをリファクタリング"        # Two-stage (設計判断 -> 実装)
gh issue view 42 | ai-route -w                           # パイプ入力を自動読み込み

# 2. 止まった／クレジット切れのセッションを 1 コマンドで再開
ai-route resume                                          # 状況に応じて自動エスカレーション or 別サービス切替
ai-route resume --auto                                   # 同期モードで完走させる

# 3. worktree の作業をテスト・マージ（または PR 作成）・後片付けまで 1 コマンドで完了
ai-route done --test-cmd "pytest -q"                     # main へマージ & worktree/ブランチ削除
ai-route done --pr                                       # push & `gh pr create`

# 4. レビュー & 状態・クレジット確認
ai-route review --synthesize
ai-route status
ai-route status --reset-credits
```
