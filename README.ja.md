# DyroEngineeringFlow

[English](README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [Español](README.es.md)

**DyroEngineeringFlow · `dyro` CLI** は、複数リポジトリのチーム向けのローカルファーストなエンジニアリング自動化・デリバリー制御プラットフォームです。開発ライン、Git worktree、Agent 起動、タスクゲート、独立レビュー、マージ監査を、バージョン管理可能なワークスペース設定に統合します。

**タスクからデリバリーまで、エンジニアリングを自動的に進めます。**

Codex、Claude、特定の業務ドメインには依存しません。各チームは `dyro.toml` Profile でリポジトリ、レイアウト、Agent adapter、デリバリーポリシーを定義し、業務ルール、モデルコスト、リリース規約は Profile 側に保持します。

## 主な保証

- タスクは必ず一つの開発ラインに属し、機能版と Hotfix のワークスペースを混在させません。
- 各タスクは `task/<id>` ブランチの独立した `git worktree` で実行されます。
- ゲートはオーケストレーターが実行し、Agent の自己申告だけを成功の根拠にしません。
- レビューは実行 receipt とリポジトリごとの正確なタスク HEAD の両方に紐づき、ソースの変更で無効になります。
- タスクは独立レビュー後にのみ `done` になり、マージと push は既定で明示確認が必要です。
- 実行可能な設定は argv 配列です。コアは TOML 内の shell 文字列を実行しません。

## クイックスタート

日常的な CLI 利用では、隔離された `pipx` 環境で PyPI から `dyro` をインストールします。Python 3.11 以降が必要です。

```bash
python3 -m pip install --user --upgrade pipx
python3 -m pipx ensurepath
# ensurepath の後に新しいターミナルを開いてから実行します。
pipx install dyro
dyro --version
```

更新時は `pipx upgrade dyro` を実行します。チームが `pip` で Python パッケージを管理する場合は、次を使用できます。

```bash
python3 -m pip install --user --upgrade dyro
```

リポジトリをワークスペースに置いてから初期化します。

```bash

mkdir my-workspace && cd my-workspace
# 先に Git リポジトリをこのディレクトリ配下へ clone または移動します。
dyro init . --discover --name my-workspace
```

`--discover` はローカル Git リポジトリを走査し、ワークスペース相対パス、開発ライン内の配置、利用可能な場合は `origin` を自動登録します。TOML の編集は不要です。まだリポジトリがない場合は、対話式の代替を使います。

```bash
dyro init . --wizard --name my-workspace
```

後からリポジトリを追加する場合も、`dyro.toml` を開く必要はありません。

```bash
dyro repo add repositories/services/payments
dyro repo list
```

Profile に remote がある場合、不足している repository anchor を安全に補完できます。

```bash
dyro --dry-run bootstrap
dyro bootstrap --yes
dyro doctor
```

新しいメンバーの通常の入口は一つのコマンドです。ワークスペースを検査してから、開発ラインとローカル Agent を選択します。

```bash
dyro start
```

## デリバリーの流れ

```bash
dyro doctor
dyro line create release-2026-10 --base origin/main --yes
# 必要なリポジトリだけ、検証済みの base を上書きできます。
dyro line create release-2026-10 --base origin/main --repo-base web=v2026.10.0 --yes
dyro open release-2026-10 --agent codex
dyro task create API-101 --title "Implement API contract" --line release-2026-10 --repository api
dyro task next
dyro task next --run --yes
dyro task review API-101
dyro task merge API-101 --yes
dyro changeset create release-2026-10-ready --line release-2026-10
dyro changeset verify release-2026-10-ready
```

本番 Hotfix では、検証済みの本番ベースを必ず明示します。

```bash
dyro hotfix create incident-123 --base v2026.09.7 --repos api,web --yes
```

実行と承認を別の信頼されたシステムで行う Profile では、`policy.execution_mode = "external"` と `policy.require_external_signoff = true` を設定します。ローカル Dyro は計画のみを許可し、receipt と正確なタスク HEAD に紐づくレビュー後も明示的な signoff が必要です。

```bash
dyro task claim API-101 --by isolated-runner-1
dyro task evidence execution API-101 --receipt /runner/out/receipt.md --gates /runner/out/gates.json --heads /runner/out/task-heads.json
dyro task evidence review API-101 --file /review/out/review.md
dyro task signoff API-101 --by release-manager
```

書き込みを伴う操作はすべて計画モードを利用できます。

```bash
dyro --dry-run line create release-2026-10 --base origin/main
dyro --dry-run task run API-101
```

## コマンド一覧

| コマンド | 用途 |
| --- | --- |
| `init --discover` / `init --wizard` / `repo add/list` / `bootstrap` / `start` | TOML 編集なしの導入、anchor 管理、開発ラインと Agent の選択。 |
| `doctor` / `status` | 制御プレーンの検証と状態表示。 |
| `line create/list` / `hotfix create` | 機能開発ラインまたは明示的な本番ベースからの Hotfix を作成。 |
| `changeset create/list/verify` | 複数リポジトリのデリバリーを構成する、クリーンで正確な Git HEAD を固定・検証。 |
| `agent list` / `open` | adapter の確認、正しい開発ラインでの Agent 起動。 |
| `task create/list/board/status/next` | タスク定義、状態、次の実行可能な作業を管理。 |
| `task run/answer/gates/review/signoff/merge` | 実行、質問解決、ゲート、独立レビュー、署名、マージ。 |
| `task claim` / `task evidence execution/review` | 隔離 runner による一度だけの取得と、回執・ゲート・タスク HEAD・レビュー証拠の取り込み。 |
| `task loop/daemon/stats/decisions` | 制御されたバッチ、スケジューリング、台帳、決定ゲート。 |

## 言語と現在の範囲

README は英語、簡体字中国語、日本語、韓国語、スペイン語で提供します。コマンド、設定キー、ディレクトリ名、安全規則は各訳で同一です。現在、CLI のメッセージと詳細技術文書は主に中国語です。README の多言語化は実行時の言語切替を意味しません。

DyroEngineeringFlow はローカルのワークフローを完結させ、より厳格なチームをローカルの計画専用モードに保つポリシーを提供します。remote リポジトリ作成、SaaS 認証情報、外部 runner 実装は含みません。外部 runner は Profile 拡張として接続します。[MIT License](LICENSE) で提供し、[PyPI の `dyro`](https://pypi.org/project/dyro/) として公開済みです。
