# -*- coding: utf-8 -*-
"""動作確認用のサンプルPDFを生成する（架空企業のサステナビリティレポート風・3ページ）。

    python examples/make_sample.py
    → examples/sample.pdf

実在の文書は含めない。ヘッダー・フッター・2段組・罫線表・見出しなど、
このワークベンチが扱うレイアウト要素を一通り含むように作ってある。
"""
from pathlib import Path

import pymupdf

OUT = Path(__file__).resolve().parent / "sample.pdf"
W, H = 595, 842  # A4縦
FONT = "japan"


def furniture(page, pageno: int):
    """全ページ共通のヘッダー・フッター（柱の除去とページ番号処理のデモ用）。"""
    page.insert_text((40, 24), "架空電機株式会社  サステナビリティレポート 2026",
                     fontname=FONT, fontsize=7, color=(0.5, 0.5, 0.5))
    page.insert_text((W - 60, 24), "環境  社会  ガバナンス", fontname=FONT, fontsize=7,
                     color=(0.5, 0.5, 0.5))
    page.insert_text((W / 2, H - 18), str(pageno), fontname=FONT, fontsize=8,
                     color=(0.5, 0.5, 0.5))


def para(page, x, y, width, lines, size=9.0, leading=13.5):
    for i, ln in enumerate(lines):
        page.insert_text((x, y + i * leading), ln, fontname=FONT, fontsize=size)


def main():
    doc = pymupdf.open()

    # --- p1: 表紙 ---
    page = doc.new_page(width=W, height=H)
    page.insert_text((60, 320), "サステナビリティレポート 2026", fontname=FONT, fontsize=26)
    page.insert_text((60, 360), "架空電機株式会社", fontname=FONT, fontsize=16)

    # --- p2: 本文（見出し＋2段組＋生成AIの言及） ---
    page = doc.new_page(width=W, height=H)
    furniture(page, 2)
    page.insert_text((40, 60), "DXによる価値創造", fontname=FONT, fontsize=16)

    para(page, 40, 100, 240, [
        "当社は、社会課題の解決に向けてデジタル技",
        "術の活用を進めています。とりわけ生成AIに",
        "ついては、業務効率化と新事業創出の両面か",
        "ら取り組みを強化しています。",
    ])
    para(page, 40, 170, 240, [
        "生成AI活用ガイドライン",  # 本文と同じサイズの見出し（分割操作のデモ用）
        "従業員が安心して生成AIを利用できるよう、",
        "2025年度に全社ガイドラインを制定しまし",
        "た。個人情報の取り扱いなど、リスクに応じ",
        "た利用ルールを定めています。",
    ])
    # 右段（段またぎ結合のデモ用に、左段の続きから始める）
    para(page, 320, 100, 240, [
        "また、大規模言語モデル（LLM）を用いた社",
        "内ナレッジ検索の実証実験を開始しました。",
        "問い合わせ対応の時間を約3割削減する効果",
        "を確認しています。",
    ])

    # --- p3: 罫線表（表の行復元のデモ用） ---
    page = doc.new_page(width=W, height=H)
    furniture(page, 3)
    page.insert_text((40, 60), "主要KPIと実績", fontname=FONT, fontsize=16)

    x0, x1, x2, x3 = 40, 200, 380, 555
    rows_y = [90, 120, 150, 180]
    for y in rows_y:
        page.draw_line((x0, y), (x3, y), color=(0.3, 0.3, 0.3), width=0.7)
    for x in (x0, x1, x2, x3):
        page.draw_line((x, rows_y[0]), (x, rows_y[-1]), color=(0.3, 0.3, 0.3), width=0.7)

    cells = [
        ("項目", "2026年度目標", "2025年度実績"),
        ("再生可能エネルギー比率", "60%", "48%"),
        ("生成AI研修の受講率", "全従業員の90%", "72%"),
    ]
    for (a, b, c), y in zip(cells, rows_y):
        page.insert_text((x0 + 6, y + 20), a, fontname=FONT, fontsize=8.5)
        page.insert_text((x1 + 6, y + 20), b, fontname=FONT, fontsize=8.5)
        page.insert_text((x2 + 6, y + 20), c, fontname=FONT, fontsize=8.5)

    doc.save(OUT, deflate=True)
    print(f"saved: {OUT}")


if __name__ == "__main__":
    main()
