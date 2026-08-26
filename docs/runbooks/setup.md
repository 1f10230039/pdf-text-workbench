# 初回セットアップ手順

## 前提条件

| 必要なもの | バージョン |
|---|---|
| Python | 3.11 以上 |
| OS | Windows / macOS / Linux（開発・動作確認は主に Windows） |

## 手順

```bash
git clone https://github.com/1f10230039/pdf-text-workbench.git
cd pdf-text-workbench

# 仮想環境（任意だが推奨）
python -m venv .venv
# Windows: .venv\Scripts\activate ／ macOS・Linux: source .venv/bin/activate

pip install -r requirements.txt
```

## データディレクトリ

作業データ（PDF・設定・出力）はリポジトリの外に置く。場所は環境変数で指定する。

```bash
# 例（未設定の場合は既定のパスが使われる）
set WORKBENCH_DATA=C:\path\to\data      # Windows
export WORKBENCH_DATA=/path/to/data     # macOS・Linux
```

初回起動時に以下の構成が自動で作られる。

```
WORKBENCH_DATA/
├── pdf/        # 入力 PDF（企業名_年度.pdf の形式）
├── 設定/       # 文書ごとの設定 JSON・検索語.json
├── txt/        # KH Coder 用テキストの出力先
└── .cache/     # 解析キャッシュ（削除可）
```

## 起動

```bash
python ui/app.py
# → http://127.0.0.1:5000 が開く（--no-browser でブラウザ起動を抑止）
```

サーバーは `127.0.0.1` にのみバインドされる。認証を持たないため、
LAN 内の他端末へ公開する設定にはしないこと。

## サンプルで試す

手元に PDF が無くても、同梱のジェネレータで動作確認できる。

```bash
python examples/make_sample.py           # examples/sample.pdf を生成
```

生成された `sample.pdf` を UI からアップロードすると、
2 段組・見出し・罫線表・ヘッダー/フッターを含む 3 ページの文書で一通りの機能を試せる。

## 動作確認（開発者向け）

```bash
pip install ruff pytest
ruff check .        # 静的解析
pytest -q           # ユニットテスト
```
