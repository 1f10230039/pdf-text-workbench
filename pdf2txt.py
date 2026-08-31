# -*- coding: utf-8 -*-
"""
サステナビリティレポート／統合報告書の PDF を、KH Coder に読ませられるテキストにする。
**全社まとめて一気に変換するバッチ。** 1社ずつ設定を詰めるのは ui/app.py（画面）のほう。

使い方:
    python pdf2txt.py

    <データディレクトリ>\\pdf\\ に「企業名_年度.pdf」の形で入れておく。
    出力は同じ場所：
      文単位.csv          … 1行1文（全社）。企業名・年度・群・ページ・種別・pt が列にある
      ページ単位.csv      … 1行1ページ（全社）。文を連結した本文
      KHCoder.txt         … KH Coder に読ませる本体。<h1>企業名_年度</h1> / <h2>pN</h2> / 1行1文
      外部変数_文書.csv   … KHCoder.txt の H1 と同じ順・同じ数（企業名・年度・群）
      外部変数_ページ.csv … KHCoder.txt の H2 と同じ順・同じ数（企業名・年度・群・ページ・セクション）
      txt\\企業名_年度.txt … 1社ぶんの KH Coder 用テキスト（KHCoder.txt はこれを並べたもの）
      変換ログ.csv        … 社ごとの件数・設定の有無

設定について:
    <データディレクトリ>\\設定\\企業名_年度.json があれば、それを使う。無ければ core.DEFAULTS で走る。
    ⚠️ **レイアウトは会社ごとに違うので、新しい会社は画面で一度確認すること。**
    設定JSONは卒論の再現性の材料そのものなので、消さずに残す。

集計単位（2026-08-22 夜に決めた）:
    **文とページの2層。段落は作らない。** KH Coder では集計単位を
    文 ／ H2（＝ページ）／ H1（＝文書）から選ぶ。→ core.py「集計単位について」

方針:
    **捨てるのは柱（全ページで反復する文言）だけ。**（2026-08-31 ページ除外と
    ヘッダー・フッターの座標カットの適用を廃止。分母＝総文数・総ページ数は
    全ページ・全文で数え、冊ごとの人の判断を前処理から排除する）
    それ以外は種別（本文／大／小／極小／表）を付けて全部残す。
    前処理で捨てたものは戻らないが、種別を付けておけば分析のときに選べる。

来歴と処理の中身:
    しくみ.md を参照。抽出ロジックは core.py（画面と共通）。
"""
import collections
import csv
import os
import re
import sys
import time
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cachekit
import core

DATA = Path(os.environ.get("WORKBENCH_DATA", str(Path.home() / "卒研データ")))
PDF_DIR = DATA / "pdf"
TXT_DIR = DATA / "txt"
CONF_DIR = DATA / "設定"

COLS = ["企業名", "年度", "群", "ページ", "ページ表示", "セクション", "種別", "pt", "文"]
# ページ単位のほう。文単位.csv とは 企業名＋年度＋ページ で対応が付く（→ core.aggregate_pages）
PAGE_COLS = ["企業名", "年度", "群", "ページ", "ページ表示", "セクション",
             "種別", "文数", "文字数", "本文"]
# KH Coder の「外部変数と見出し → 読み込み」用。⚠️ 行の順と数は KHCoder.txt の H1／H2 と必ず揃える
DOC_VAR_COLS = ["文書", "企業名", "年度", "群"]
PAGE_VAR_COLS = ["文書", "企業名", "年度", "群", "ページ", "ページ表示", "セクション", "種別", "文数"]


def parse_name(stem):
    """ファイル名「企業名_年度」から企業名と年度を取る。"""
    m = re.match(r"^(.+?)_(\d{4})$", stem)
    return (m.group(1), m.group(2)) if m else (stem, "")


def load_groups():
    """対象一覧.csv があれば 企業名_年度 → 群 の対応を読む（無ければ空欄）。"""
    path = DATA / "対象一覧.csv"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {f"{r['企業名']}_{r['年度']}": r.get("群", "")
                for r in csv.DictReader(f)}


def load_keywords():
    """検索語（画面と共通。設定\\検索語.json。無ければ core.KEYWORDS）。

    🔴 検索語は全文書・全時点で共通。画面（ui/app.py）が保存するファイルをここでも読むことで、
    画面とバッチが**必ず同じ語**で抽出する。実装は cachekit に1本化。
    """
    return cachekit.load_keywords()


def main():
    pdfs = sorted(PDF_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"PDFがない: {PDF_DIR}")
        print("「企業名_年度.pdf」の形で置いてください。")
        return 1

    TXT_DIR.mkdir(parents=True, exist_ok=True)
    known = load_groups()
    keywords = load_keywords()
    print(f"検索語: {', '.join(keywords)}")
    rows, pages, doc_vars, page_vars, kh_parts, log = [], [], [], [], [], []
    unit_rows = []          # 抽出単位（L2）。全社まとめて 抽出単位.csv / KHCoder_抽出単位.xlsx に

    for pdf in pdfs:
        company, year = parse_name(pdf.stem)
        group = known.get(f"{company}_{year}", "")
        t0 = time.time()

        doc = pymupdf.open(pdf)
        # 設定・自動候補・本文ptは cachekit（画面と同じ実装・同じキャッシュ）を通す
        body0 = cachekit.load_cands(pdf.stem, lambda: doc)["body0"]
        st, has_conf = cachekit.load_settings(pdf.stem, lambda: doc)
        body = st.body_size if st.body_size else body0
        units = core.extract_doc(doc, st, body)
        n_pages = len(doc)
        doc.close()

        # 1社1ファイルの KH Coder 用テキスト。全社まとめ（KHCoder.txt）はこれを並べたもの
        kh = core.kh_text(units, pdf.stem)
        (TXT_DIR / f"{pdf.stem}.txt").write_text(kh, encoding="utf-8")
        kh_parts.append(kh)

        for u in units:
            rows.append({"企業名": company, "年度": year, "群": group, **u})
        # ページ単位。**PDFを読み直さず、上の units から作る**（両CSVが食い違わないように）
        doc_pages = core.aggregate_pages(units)
        for s in doc_pages:
            pages.append({"企業名": company, "年度": year, "群": group, **s})
        # 外部変数（KH Coder の H1／H2 と同じ順・同じ数）
        doc_vars.append({"文書": pdf.stem, "企業名": company, "年度": year, "群": group})
        for s in doc_pages:
            page_vars.append({"文書": pdf.stem, "企業名": company, "年度": year, "群": group,
                              "ページ": s["ページ"], "ページ表示": s["ページ表示"],
                              "セクション": s["セクション"], "種別": s["種別"], "文数": s["文数"]})

        # 抽出単位（L2）：生成AI関連語のヒット箇所を類型規則で単位化（→ core.extract_units）。
        # 手作業（unit_merges / unit_excludes）は設定JSONから来る＝画面と同じ結果になる
        l2 = core.extract_units(units, keywords, st.unit_merges, st.unit_excludes,
                                checks=st.unit_checks)
        unit_rows += core.unit_export_rows(l2["units"], pdf.stem, company, year, group)

        # 画面（ui/app.py）と同じキャッシュを温めておく：一覧のバッジと確認モードの初回が一瞬になる。
        # 署名の作り方も書く場所も cachekit に1本化してある（ズレると毎回解析し直しになる）
        mtime = pdf.stat().st_mtime
        sig = cachekit.rows_sig(st)
        cachekit.write_rows(pdf.stem, mtime, sig, units)
        cachekit.write_meta(pdf.stem, mtime, sig, l2["units"])
        for op, lost in l2["未適用"].items():
            if lost:
                print(f"  [!] {pdf.stem}: 当たらなかった{core.OP_LABELS[op]}が{len(lost)}件"
                      f"（設定JSONの {op} を確認）")

        by_kind = collections.Counter(u["種別"] for u in units)
        chars = sum(len(u["文"]) for u in units)
        secs = time.time() - t0
        # 2026-08-31 ページ除外の廃止：分母は全ページ・全文（除外ページ数・手順の列も撤去）
        log.append({"ファイル": pdf.name, "企業名": company, "年度": year, "群": group,
                    "ページ数": n_pages,
                    "本文pt": body, "設定": "個別" if has_conf else "既定",
                    "文数": len(units),
                    "抽出単位数": len(l2["units"]),
                    "ページ単位数": len(doc_pages),
                    "ページあたり文数": round(len(units) / len(doc_pages), 1) if doc_pages else 0,
                    **{k: by_kind[k] for k in core.KINDS},
                    "セクション数": len({u["セクション"] for u in units if u["セクション"]}),
                    "文字数": chars, "秒": round(secs, 1)})
        # ⚠️ 絵文字は Windows のコンソール（cp932）で落ちるので、表示用の文字列には使わない
        mark = "" if has_conf else "  [!] 既定値で変換（画面で確認していない）"
        breakdown = " ".join(f"{k}{by_kind[k]:,}" for k in core.KINDS)
        print(f"{pdf.name}: {n_pages}頁 / 本文{body}pt → {len(units):,}文 "
              f"({breakdown}) / {len(doc_pages):,}ページ / {chars:,}字 "
              f"({secs:.1f}秒){mark}")

    def write_csv(path, cols, data):
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            # ⚠️ extrasaction="ignore"：core.extract_doc が付ける印（ページまたぎ など）は列にしない
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(data)

    # 全社まとめ
    write_csv(DATA / "文単位.csv", COLS, rows)
    write_csv(DATA / "ページ単位.csv", PAGE_COLS, pages)
    write_csv(DATA / "外部変数_文書.csv", DOC_VAR_COLS, doc_vars)
    write_csv(DATA / "外部変数_ページ.csv", PAGE_VAR_COLS, page_vars)
    (DATA / "KHCoder.txt").write_text("".join(kh_parts), encoding="utf-8")
    write_csv(DATA / "変換ログ.csv", list(log[0].keys()), log)

    # 抽出単位（L2）。CSVは全件（除外も監査記録として残す）、xlsx は採用のみ＝KH Coder に読ませる本体
    # ⚠️ openpyxl は使わない：ワークシート参照を絶対パスで書き、KH Coder のパーサが読めない
    #    （2026-08-25 に実際に落ちた）。xlsxwriter は Excel と同じ相対パスで書く
    write_csv(DATA / "抽出単位.csv", core.UNIT_COLS, unit_rows)
    adopted = [r for r in unit_rows if r["採用"] == "○"]
    import xlsxwriter
    try:
        wb = xlsxwriter.Workbook(str(DATA / "KHCoder_抽出単位.xlsx"))
        ws = wb.add_worksheet("抽出単位")
        for j, c in enumerate(core.UNIT_COLS):
            ws.write(0, j, c)
        for i, r in enumerate(adopted, 1):
            for j, c in enumerate(core.UNIT_COLS):
                ws.write(i, j, r.get(c, ""))
        wb.close()
    except Exception as e:
        # ⚠️ Excel や KH Coder で開いたままだと Windows がファイルをロックしていて書けない
        #    （2026-08-25 に実際に起きた）。他の出力は全部済んでいるので、止めずに知らせる
        print(f"\n[!] KHCoder_抽出単位.xlsx を書き出せませんでした: {e}")
        print("    Excel か KH Coder で開いたままになっていないか確認して、閉じてから再実行してください。")

    print(f"\n{len(pdfs)}件 / 合計 {len(rows):,}文 / {len(pages):,}ページ")
    print(f"→ {DATA / 'KHCoder.txt'}  （L1：全文。集計単位は 文／H2＝ページ／H1＝文書）")
    print(f"→ {DATA / '外部変数_文書.csv'} ／ {DATA / '外部変数_ページ.csv'}  （H1／H2 の外部変数）")
    print(f"→ {DATA / '文単位.csv'} ／ {DATA / 'ページ単位.csv'}")
    print(f"→ {DATA / 'KHCoder_抽出単位.xlsx'}  （L2：抽出単位 {len(adopted):,}件（採用のみ）。1行1単位・テキスト列は「テキスト」）")
    print(f"→ {DATA / '抽出単位.csv'}  （L2の監査記録：除外 {len(unit_rows) - len(adopted):,}件を含む全 {len(unit_rows):,}件）")
    print(f"→ {TXT_DIR}")
    if any(l["設定"] == "既定" for l in log):
        print("\n[!] 既定値で変換した会社があります。ui/app.py で確認してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
