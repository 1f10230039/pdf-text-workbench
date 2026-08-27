# pdf-text-workbench

**PDF文書を、再現可能・監査可能な形でテキスト化するためのローカルWebワークベンチ。**

企業の報告書のような多段組・図表混じりのPDFからテキストを取り出すとき、
機械抽出だけでは読み順や文の境界が必ずどこかで壊れる。
このツールは、機械判定の結果を**原本と見比べながら人が直し、その判断をすべて記録する**ことで、
「同じPDFと同じ設定ファイルからは、誰がやっても同じテキストになる」状態を作る。
テキストマイニング（KH Coder 等）の前処理として設計している。

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-2.3-000000?logo=flask&logoColor=white)
![PyMuPDF](https://img.shields.io/badge/PyMuPDF-1.28-orange)
![License: MIT](https://img.shields.io/badge/License-MIT-green)

## 主な機能

- **レイアウト解析** — 多段組の読み順復元、ヘッダー/フッター/柱の除去、罫線表の「1行=1ブロック」再構成、
  フォントサイズによる種別付け（本文/見出し/注記）
- **確認モード** — キーワードを含む箇所を1件ずつキューで目視確認。検索語の出現位置が原本上でピンポイントに光る。
  修正（文を足す・繋ぐ・分ける・除外）はその場で完結
- **すべての操作を記録** — 手動の修正は理由コードつきで文書ごとの設定JSONに保存。
  除外した箇所も消さずに監査記録として出力（→ [ADR-0002](docs/adr/0002-settings-json.md)）
- **切り取りと除外** — ヘッダー/フッターの境界が本文を巻き込んでいないかを全文書で機械診断し、
  原本の上の**境界線をドラッグ**して文書ごとに調整。除外ページ（表紙・目次）も同じ画面で決める。
  未チェック/チェック済みの2タブと進捗表示で、数十冊を1冊ずつ消化していける
- **出力** — CSV（全件=監査記録）/ Excel（外部変数つき）/ KH Coder用テキスト
- **キャッシュとジョブ** — 数十冊規模でも実用速度（メモリ＋ディスクの2段キャッシュ・並列事前解析・進捗つき非同期処理）

## スクリーンショット

確認モード：左のカードが抽出単位（青枠=出力される範囲）、右の原本では検索語そのものがハイライトされる。

![確認モード](docs/images/audit-mode.jpg)

作業画面：罫線表は行単位のブロックに再構成される。上端の赤い破線=切り捨てられるヘッダー、青い破線=切り取り境界。

![作業画面（表の検出）](docs/images/editor-table.jpg)

## クイックスタート

```bash
git clone https://github.com/1f10230039/pdf-text-workbench.git
cd pdf-text-workbench
pip install -r requirements.txt

python examples/make_sample.py   # 動作確認用のサンプルPDFを生成
python ui/app.py                 # → http://127.0.0.1:5000 が開く
```

画面から `examples/sample.pdf` をアップロードすれば、2段組・見出し・罫線表を含む
サンプル文書で一通りの機能を試せる。詳細は [セットアップ手順](docs/runbooks/setup.md)。

## 仕組み（概要）

```mermaid
flowchart LR
    PDF[(PDF)] --> M[機械判定<br>座標・フォント・罫線]
    CONF[(設定JSON<br>手動修正の全記録)] --> M
    M --> L1[L1: 全文<br>文×ページ]
    L1 --> L2[L2: キーワードを含む箇所の<br>抽出単位]
    L1 --> OUT[(CSV / Excel /<br>KH Coder用テキスト)]
    L2 --> OUT
```

- 抽出ロジックは `core.py` に集約し、Web UIとバッチ（`pdf2txt.py`）が同じ関数を呼ぶ
- 出現率の集計（L1）と内容分析用の単位化（L2）を分離し、手作業が統計量を歪めない構造にしている
- LLMは使わない。決定性と説明可能性を優先した（→ [ADR-0003](docs/adr/0003-no-llm.md)）

詳細は [アーキテクチャ概要](docs/architecture.md) を参照。

## ディレクトリ構成

```
├── core.py            # 抽出エンジン（画面もI/Oも持たない純粋層）
├── cachekit.py        # キャッシュの署名・永続化（UIとバッチで共通）
├── pdf2txt.py         # 一括変換バッチ
├── warm_cache.py      # キャッシュの並列事前生成
├── ui/
│   ├── app.py         # Flask サーバー（REST API・ジョブ）
│   └── static/        # フロントエンド（vanilla JS）
├── examples/          # サンプルPDFのジェネレータ
├── tests/             # ユニットテスト
└── docs/
    ├── architecture.md
    ├── data-model.md
    ├── adr/           # アーキテクチャ決定記録
    └── runbooks/      # セットアップ・運用・デプロイ手順
```

## ドキュメント

| ドキュメント | 内容 |
|---|---|
| [アーキテクチャ概要](docs/architecture.md) | 全体構成・抽出パイプライン・キャッシュ・並行処理 |
| [データモデル](docs/data-model.md) | 設定JSONのスキーマ・出力ファイル・キャッシュ構造 |
| [ADR 一覧](docs/adr/README.md) | 主要な設計判断の記録（8件） |
| [セットアップ手順](docs/runbooks/setup.md) | 初回セットアップ・データディレクトリ |
| [運用手順](docs/runbooks/operations.md) | 推奨フロー・キャッシュ・トラブルシューティング |
| [デプロイ手順](docs/runbooks/deploy.md) | 公開デモ（Render）のデプロイとロールバック |

## 開発

```bash
pip install ruff pytest
ruff check .    # 静的解析
pytest -q       # ユニットテスト
```

CI（GitHub Actions）で lint・テスト・サンプルPDFのエンドツーエンド抽出を検証している。

## ライセンス

[MIT](LICENSE)
