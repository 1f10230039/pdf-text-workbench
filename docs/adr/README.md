# ADR 一覧

アーキテクチャ上の重要な決定を [MADR](https://adr.github.io/madr/) 形式で記録する。
1 決定 = 1 ファイル。決定を覆すときは新しい ADR を起こし、古いものは Superseded にする。

| # | タイトル | 状態 |
|---|---|---|
| [0001](0001-pymupdf.md) | PDF 抽出ライブラリに PyMuPDF を採用する | Accepted |
| [0002](0002-settings-json.md) | すべての手動操作を設定 JSON に記録して再現性を担保する | Accepted |
| [0003](0003-no-llm.md) | LLM による自動抽出を採用しない | Accepted |
| [0004](0004-sentence-page-unit.md) | 集計単位を「文 × ページ」とし、段落の復元を行わない | Accepted |
| [0005](0005-table-row-blocks.md) | 罫線表は「行」単位のブロックに再構成する | Accepted |
| [0006](0006-two-layers.md) | 抽出を 2 層（L1 全文層 / L2 抽出層）に分離する | Accepted |
| [0007](0007-pymupdf-lock.md) | PyMuPDF の呼び出しをグローバルロックで直列化する | Accepted |
| [0008](0008-records-out-of-cache-signature.md) | 人の判断の「記録」はキャッシュ署名に含めない | Accepted |
