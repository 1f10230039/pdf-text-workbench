# -*- coding: utf-8 -*-
"""
PDF → 単位（1行1文）への抽出ロジック本体。

**このファイルには画面もファイル入出力も入れない。** 純粋に「PDFと設定を渡すと単位が返る」だけ。
理由は、UI（ui/app.py）とバッチ（pdf2txt.py）が**完全に同じ処理**を通ることを保証するため。
UIで詰めた設定が、そのままバッチで再現されないと意味がない。

処理の中身の解説は しくみ.md を参照。ここはその実装。

2026-08-08 に pdf2txt.py から切り出し。同時に足したもの：
  ・ページ番号／ページラベル（印刷上の番号。get_label() が返す）
  ・セクション（直近の大見出しを引き継ぐ）
  ・設定を Settings にまとめ、文書ごとに JSON で保存できる形にした

🔴 2026-08-31 前処理を大幅に簡素化：ページ除外（skip_pages）と座標カット
   （header_y / footer_margin）の適用を廃止。捨てるのは反復行（repeated_lines）だけ。
   ヒットに混ざった柱・番号は L2 の unit_excludes（理由コード付き）で単位側から外す。
   → 記録/2026-08-31.md
"""
from __future__ import annotations

import collections
import copy
import json
import re
from dataclasses import asdict, dataclass, field, fields

import pymupdf

# --- 既定値 -------------------------------------------------------------
# しくみ.md「4. パラメータ一覧」の値。A社 SR 2025 で目視して決めたもの。
# ⚠️ 会社ごとにレイアウトが違うので、これは「出発点」であって正解ではない。
DEFAULTS = {
    # 🔴 2026-08-31 `header_y`・`footer_margin`（座標でヘッダー・フッターを切る）を廃止。
    #    ページ番号と同じ高さに本文が回り込む冊（R社）があり、値では分けられず
    #    冊ごとの判断が必須になるため。切らずに全部残し、ヒットに柱・番号が混ざったときは
    #    L2 の unit_excludes（理由：ヘッダー／フッター）で単位側から外す。
    #    実測（61冊・診断キャッシュ）：廃止で新たに入る 15,901 行に検索語入りは 0 件
    # 全ページのこの割合以上で「同じ位置に同じ文言」が現れる行は柱（ランニングヘッド・ナビ・
    # 社名入りフッター）とみなして捨てる（2026-08-22 追加。0で無効。→ repeated_lines）。
    # これが唯一の「捨てる」規則（冊ごとの判断ゼロ・全冊一律）
    "repeat_ratio": 0.3,
    "size_tol": 0.6,        # 本文フォントサイズからの許容差(pt)。この範囲を「本文」とする
    "tiny_ratio": 0.5,      # 本文ptに対するこの比より小さい文字は「極小」とする（0で無効）
    "col_tol": 12.0,        # 同じ列とみなす x座標の差(pt)
    "line_gap": 2.2,        # 同じ段落とみなす y座標の差（フォントサイズの何倍まで許すか）
    "join_gap": 1.2,        # 同じ行の続きとみなす横の隙間（フォントサイズの何倍まで）。
                            # 🔴 2026-08-22 に 0.3→1.2。「原則1」＋「人権擁護の支持と尊重」のように
                            #    1文字ぶん空けて横に並ぶラベルを1行にするため。実測では段の間は3倍以上空く
                            #    （P社2025：3.0〜3.5倍、D社2025：3.5〜4倍）ので 1.2 なら段は繋がらない
    "min_len": 2,           # これ未満の単位は捨てる（1文字は語にならない）
    "body_size": None,      # 本文pt。None なら文書から自動推定する
    "section_min_pt": None, # これ以上の大見出しをセクション名として拾う。None なら 本文pt+3
    "section_max_len": 40,  # これより長い見出しはセクション名にしない（本文の誤検出よけ）
    # --- 表（2026-08-22 追加。→ find_page_tables / table_row_groups） ---
    # 罫線で区切られた表を検出し、**行ごとに1ブロック**に組み直す。
    #   "lines_strict" … 罫線（線分）だけを使う（既定。塗りの矩形は見ない）
    #   "lines"        … 罫線＋塗りつぶしの矩形も使う
    #   "text"         … 文字の位置だけで推定する（罫線が無い表向け。誤検出が多い）
    #   "none"         … 表を検出しない
    "table_strategy": "lines_strict",
    "tables": [],           # 手で指定した表の範囲。[{"page": ページ, "rect": [x0,y0,x1,y1], "strategy": 上のどれか, "reason": ""}]
    "table_off": [],        # 表の自動検出をやめるページ。[{"page": ページ, "reason": ""}]
    # 除外するページ。[{"page": 1始まりの番号, "reason": TASKS のキー or ""}]
    # ⚠️ 2026-08-12 に「番号だけのリスト」から変えた。番号だけだと、後から見て
    #    p45 が章扉だったのか判断ミスだったのか分からず、付録として使えなかったため。
    #    古い形（[1,2,3]）は Settings.from_dict が自動で移行する（reason は空欄）。
    "skip_pages": [],
    "task_states": [],      # 除外以外の結論。[{"key": TASKSのキー, "state": "該当なし"|"残した", "memo": ""}]
    # 手で除外した単位。[{"text": 本文, "page": ページ or null, "pt": 文字サイズ or null}]
    # ⚠️ pt が無いと、同じページに同じ文言が2つあるとき（見出しと参照リンクなど）
    #    片方だけ消せない。pt を持たない古いルールは pt を問わず一致する（→ Excluder）
    "excluded": [],
    "joins": [],            # 手で繋いだブロック。[{"page": ページ, "a": 前の生text, "b": 後の生text}]
    "order": "reading",     # ブロックの並び順。"reading"=読み順（列→上から） / "pdf"=描画順
    "manual_order": [],     # 手で並べ替えたページ。[{"page": ページ, "keys": [生text, ...]}]
    "kinds": [],            # 手で直した種別。[{"page": ページ, "text": 生text, "kind": KINDS のどれか}]
    # 句点で終わらず、最終行が列の右端まで届いているブロックを、次のブロックに自動で繋ぐ
    # （段またぎ・ページまたぎの文。→ auto_join_groups / extract_doc）
    "auto_join": True,
    # --- 抽出単位（L2。2026-08-25 追加。→「抽出単位について」/ extract_units） ---
    # 生成AI関連語のヒット箇所を単位化するときの手作業。どちらも全件が設定JSONに残る
    "unit_merges": [],      # 単位に足した文。[{"page": ヒット文のページ, "hit": ヒット文, "add": [文,...], "reason": ""}]
    "unit_excludes": [],    # 抽出から外したヒット。[{"page": ページ, "text": ヒット文, "reason": ""}]
    # 確認モード（旧・監査モード）で「確認した」印。抽出結果は変えない（進み具合と、1サイトあたりの確認時間の実測）
    "unit_checks": [],      # [{"page": ページ, "hit": ヒット文, "秒": 確認にかかった秒, "日": "YYYY-MM-DD"}]
}

# 「表」は 2026-08-22 に追加。罫線で区切られた表の**1行**を1ブロックにしたもの。
# フォントptではなく「表の中にあった」ことで決まる種別（→ table_row_groups）
KINDS = ("本文", "大", "小", "極小", "表")
TABLE_STRATEGIES = ("lines_strict", "lines", "text", "none")


# --- 前処理の手順（2026-08-12 追加） -------------------------------------
# **新しいレポートを開いたときに、必ず一通り目を通す項目。**
#
# なぜチェックリストにしたか
# ---------------------------------------------------------------------------
# `skip_pages` に番号を並べるだけだと「何をしたか」は残るが、**「やるべきことをやったか」が
# 残らない**。2社目以降で同じ手順を踏んだと言えなければ、社間の比較そのものが成立しない。
# → 手順を先に決め、**各項目について「どう結論したか」を必ず記録する**形にした。
#   卒論には「全社に同一の手順を適用し、各社の判断を付録に載せた」と書ける。
#
# ⚠️ **`must` は「必ず外す」ではなく「外すのが既定」という意味。**
#    全項目を「やれ」にすると、README が確認ダイアログについて書いているのと同じことが起きる
#    （中身を見ずに埋める動作が身につく）。だから**判断が割れる項目は `must=False`** にして、
#    残す判断も同じ重みで記録できるようにしてある。
TASKS = [
    {"key": "表紙", "label": "表紙・裏表紙", "must": True,
     "note": "レポート名とビジュアルだけのページ。企業の記述ではない"},
    {"key": "目次", "label": "目次", "must": True,
     "note": "見出しとページ番号の羅列。⚠️ 残すと『参照ページ』のような語が上位に来る"},
    {"key": "章扉", "label": "章扉（各章の扉ページ）", "must": False,
     "note": "章名だけのページ。⚠️ リード文やKPIサマリが載っている会社は、そこだけ残すか検討する"},
    {"key": "編集方針", "label": "編集方針・報告範囲", "must": False,
     "note": "「報告にあたって」「レポートについて」等も同じ。"
             "🔴 参照した制度名（TCFD/TNFD/GRI 等）が書かれていることが多く、"
             "開示制度を扱う分析では**それ自体が資料になる**。外す前に中身を見ること"},
    {"key": "対照表", "label": "対照表・データ集", "must": False,
     "note": "GRI/SASB内容索引、ESGデータ集。規格番号と数値の表。"
             "⚠️ ページ数が多く、外すと総単位数が大きく動く。件数を必ず記録する"},
    {"key": "保証報告書", "label": "第三者保証報告書", "must": False,
     "note": "保証機関が書いた文書。企業自身の記述ではないので、通常は外す"},
]
TASK_KEYS = tuple(t["key"] for t in TASKS)
TASK_STATES = ("該当なし", "残した")     # 「除外した」は skip_pages から導くので持たない


# --- 手を入れた理由（2026-08-13 追加） -----------------------------------
# **1件ずつの操作にも「なぜそうしたか」を持たせる。**
#
# 件数だけでは「多いから前処理が乱暴」なのか「文書の作りが特殊なだけ」なのかが分からない。
# 理由の内訳が出れば、**その文書のレイアウトの性質**として説明できる。
#   例）結合30件のうち28件が「段またぎ」→ 2段組みの文書だからで、恣意的な操作ではない
#
# ⚠️ **理由は必須にしない。** 未設定でも動く。強制すると、埋めるために適当な理由を
#    選ぶようになって、記録の意味がむしろ落ちる。
# ⚠️ 選択肢はレイアウト由来の事実に寄せてある（「読みやすいから」のような主観を並べない）。
#    結合の分類が group_lines の3条件（same_size / same_col / next_line）に対応しているのは、
#    **「機械がなぜ切ったか」を書くのが一番説明しやすい**ため（→ しくみ.md §3-④）。
OP_REASONS = {
    # 結合：group_lines がブロックを切ってしまった理由に対応する
    "joins": [
        {"key": "段またぎ", "label": "段をまたいで続く本文",
         "note": "左段の末尾から右段の先頭へ続いているもの。x座標が離れるので機械では繋げない"},
        {"key": "列ずれ", "label": "同じ段だが左端が揃っていない",
         "note": "字下げ・箇条書き・ぶら下げなどで x0 がずれ、別の列と判定されたもの"},
        {"key": "サイズ違い", "label": "1つの見出し／文だが文字の大きさが違う",
         "note": "タイトルの一部だけ大きい、単位や注記だけ小さい、など"},
        {"key": "行間", "label": "1つの本文だが行の間隔が広い",
         "note": "余裕を持たせた組みで LINE_GAP を超えたもの"},
        {"key": "割り込み", "label": "間に図・アイコン・注記が入って分断された",
         "note": "文としては続いているが、途中に別の要素が挟まっているもの"},
    ],
    # 分割：group_lines が別々のものを1ブロックにしてしまった理由に対応する（結合の逆）
    "splits": [
        {"key": "見出しの癒着", "label": "本文と同じptの見出しが、本文と同じブロックに入っていた",
         "note": "見出しは句点で終わらないので、切り離さないと下の文と1つの文になる"},
        {"key": "別の文の癒着", "label": "別々の文・ラベルが1つのブロックに入っていた",
         "note": "図解のカードなどで、行間が詰まっていて1ブロックにまとまったもの"},
        # 2026-09-01 表の行の分割（apply_table_splits）に伴い追加（D社 2024 p11・2025 p10）
        {"key": "セル群の癒着", "label": "表の1行に、独立した箇条書きのセル群が複数連結されていた",
         "note": "列見出し（KPI／実績など）を異にする箇条書きセル群が1行に連結されたKPI表など"
                 "（ラベルが行内で再掲される場合を含む）。セル群の境目で分け、"
                 "ヒットを含むセル群を単位にする（単一セル内の箇条書きは行全体のまま＝08-26の決定）"},
    ],
    # 種別：フォントptからの自動判定が外れる典型
    "kinds": [
        {"key": "色太さの見出し", "label": "本文と同じptだが、色や太さで見出しにしている",
         "note": "ptが同じなので機械では見出しと分からない"},
        {"key": "図解の説明文", "label": "本文より小さいが、図の中の説明文",
         "note": "『小』に落ちるが、記述としては本文と同じ扱いにしたいもの"},
        {"key": "強調だけ", "label": "大きいが見出しではない（引用・強調）",
         "note": "『大』になってセクション見出しに誤検出されるのを防ぐ"},
        {"key": "極小の誤判定", "label": "極端に小さいが、縮小コピーではなく本物の記述",
         "note": "TINY_RATIO に引っかかったが、他ページの重複ではないもの"},
    ],
    # 除外した単位：🔴 ここは「そもそも文書の記述でないもの」に限る（→ README の層の話）
    "excluded": [
        {"key": "参照表記", "label": "参照・リンク表記（「詳しくはP.○○」など）",
         "note": "本文への案内であって、記述そのものではない"},
        {"key": "ロゴ商標", "label": "ロゴ・商標・意匠の文字"},
        {"key": "ページ表記", "label": "ページ番号・柱（反復除去で落ちなかったもの）",
         "note": "座標カットは 2026-08-31 に廃止。短い番号は min_len でも落ちる"},
        {"key": "図表の断片", "label": "図表の目盛り・単位・記号だけの断片",
         "note": "『t-CO2e』『MWh』のような、語として意味を成さないもの"},
        {"key": "二重描画", "label": "二重描画の取りこぼし",
         "note": "自動（drop_duplicate_blocks）で落ちなかった重複"},
    ],
    # 並べ替え：reading_order（列→上から）で復元できないレイアウト
    "manual_order": [
        {"key": "回り込み", "label": "図の回り込みで読み順が崩れた"},
        {"key": "見出しが後ろ", "label": "見出しが本文より後に描画されていた",
         "note": "⚠️ セクション列に効く。直さないと本文が前ページのセクションに付く"},
        {"key": "図の中の順序", "label": "図中の要素の順序が、実際に読む順と違う"},
    ],
    # 表の範囲を手で指定した：自動検出（罫線）で拾えなかった表
    "tables": [
        {"key": "罫線なし", "label": "罫線が無い表（余白や塗りだけで区切っている）",
         "note": "罫線で検出できないので、文字の位置から列を推定する"},
        {"key": "検出漏れ", "label": "罫線はあるが自動では検出されなかった",
         "note": "線が細い・点線・画像として描かれている、など"},
    ],
    # 表の検出をやめたページ：罫線はあるが表ではないもの
    "table_off": [
        {"key": "図解の枠", "label": "表ではなく図解の枠線だった（組織図・フロー図など）"},
        {"key": "レイアウトの枠", "label": "ページ全体や段組みを囲む枠線だった"},
    ],
    # 抽出単位（L2）から外したヒット：🔴 内容の記述でないものに限る（→ extract_units）
    "unit_excludes": [
        {"key": "商標注記", "label": "商標・登録商標の注記",
         "note": "「※1 〈製品名〉は…の登録商標」のような、語の権利表示であって内容の記述でないもの"},
        {"key": "出典注記", "label": "出典・参照先の注記",
         "note": "「詳細はこちら」「＊1 2024年4月に設立」のような、本文への案内・脚注"},
        {"key": "誤ヒット", "label": "検索語の誤ヒット",
         "note": "別の語の一部に当たったもの（例：Fulfillment の中の llm）。リスト側で直せるなら直す"},
        {"key": "断片", "label": "語として意味を成さない断片",
         "note": "図解のラベルがバラけたもの（「エージェントAI生成AI生成AI。」など）"},
        {"key": "柱の取り残し", "label": "柱・ナビの取り残し",
         "note": "自動の柱除去で落ちなかった、毎ページ繰り返す行"},
        # 2026-08-31 ページ除外の廃止に伴い追加：構成要素のページに出たヒットは
        # ページごとではなく**その単位だけ**を理由付きで外す
        {"key": "目次", "label": "目次の項目",
         "note": "目次ページの見出し＋ページ番号の羅列。本文の章題の再掲であって内容の記述でない"},
        {"key": "表紙", "label": "表紙・裏表紙の文言",
         "note": "タイトル・社名など、文書の外装であって内容の記述でない"},
        {"key": "章扉", "label": "章扉の章題・ナビ",
         "note": "章の扉ページの章題や一覧。⚠️ リード文・KPIが載っている場合は内容なので残す"},
        {"key": "対照表", "label": "対照表・索引の項目",
         "note": "GRI対照表・内容索引などの、本文への参照行。内容の記述でない"},
        # 2026-08-31 座標カット（header_y / footer_margin）の廃止に伴い追加：
        # ヘッダー・フッター帯の行は切らずに残すので、ヒットに混ざったらここで外す
        {"key": "ヘッダー", "label": "ヘッダーのため除外（章名ナビ・柱）",
         "note": "ページ上部の章名ナビ・ランニングヘッド。反復除去で落ちなかったもの"},
        {"key": "フッター", "label": "フッターのため除外（ページ番号・欄外）",
         "note": "ページ下部の番号・欄外表記。反復除去で落ちなかったもの"},
        # 2026-09-01 追加（D社 2024 p114 のリンク一覧「〈生成AI製品名〉: 製品・ソリューション」）
        {"key": "リンク", "label": "リンクのため除外（参照先のラベル・ボタン文言）",
         "note": "ウェブページ等への誘導リンクの文言。⚠️ 文の形をした記述に"
                 "リンクが張られているだけのものは内容なので残す"},
    ],
    # 抽出単位（L2）に足した文
    "unit_merges": [
        {"key": "ラベル一体", "label": "同じ図解・カードで一体の意味をなすラベル",
         "note": "見出しラベルとその説明など、離すと意味が取れないもの"},
        {"key": "文の続き", "label": "機械で繋がらなかった文の続き",
         "note": "自動結合の条件（右端・句点）に外れて別の文になったもの"},
        {"key": "表の続き", "label": "同じ表で一体の行",
         "note": "行の復元で割れた、本来1行のもの"},
    ],
}
OP_LABELS = {"joins": "結合したブロック", "splits": "分けたブロック", "kinds": "直した種別",
             "excluded": "除外した単位", "manual_order": "並べ替えたページ",
             "tables": "手で指定した表", "table_off": "表の検出をやめたページ",
             "unit_excludes": "抽出から外したヒット", "unit_merges": "抽出単位に足した文"}


def reason_counts(st: Settings) -> dict:
    """操作ごとの理由の内訳。**卒論に載せるのはこの表**（件数だけでは説明にならない）。"""
    out = {}
    for op in OP_REASONS:
        c = collections.Counter((r.get("reason") or "未設定")
                                for r in (getattr(st, op) or []))
        if c:
            out[op] = dict(c)
    return out

# --- 集計単位について（2026-08-13 追加。🔴 2026-08-22 夜に「文 × ページ」へ変更） ----------
# **集計単位は「文」と「ページ」の2層。段落は作らない。**
#
# 経緯
# ---------------------------------------------------------------------------
#   8/13  1ページ＝1集計単位を既定にし、図解カードのページは手で ✂ を入れて分ける方式
#   8/22  60冊ではその手作業が回らないので、規則（字下げ・短い行・太字…）で段落を切る方式へ
#   8/22夜 **段落の規則は社ごとに破綻し、直すたびに別の社で副作用が出た**（U社で直すと
#         B社・P社で割れる）。「段落」の定義そのものが人の感覚で、機械的に全社へ当てはまらない。
#         → 段落の規則・段落モード・手動 ✂ を全部外し、**文とページだけ**にした。
#
# なぜ文とページか
# ---------------------------------------------------------------------------
#   文   … 句点で切れる。全社同じ定義で、KH Coder の標準の単位。
#          「直接的な意味の関連をもつ語同士に注目したい場合には，集計単位を文にする」
#          （中村・周・樋口「計量テキスト分析および KH Coder を用いた論文の執筆・査読チェックポイント」§2.1.1）
#   ページ… PDF に必ずある。河村ほか（2021、統合報告書のESG関連ページ推定）が
#          「統合報告書はページごとに内容がある程度まとまっている」「テキスト化で文の形が
#          崩れやすいのでページ単位で抽出する」としている。手元の17冊では本文は1ページあたり
#          中央値 340〜1,480 字＝日本語で2〜5段落ぶん。「節」くらいの粒度で、でかすぎはしない。
#   → 狭い端（文）と広い端（ページ）の両方で見て、両方で出る結果を主張にする。
#     中間の「段落」を無理に作らない。論文では「集計単位は文とページ」と一文で言える。
#
# 出力（→ extract_doc / aggregate_pages / kh_text）
# ---------------------------------------------------------------------------
#   文単位.csv     … 1行1文（ページ・種別・pt 付き）
#   ページ単位.csv … 1行1ページ（文を連結した本文）
#   KH Coder 用 txt … <h1>文書</h1> ／ <h2>pN</h2> ／ 1行1文。
#                    KH Coder では集計単位を 文 ／ H2（ページ）／ H1（文書）から選べる。
#                    ⚠️ H1〜H5 の見出しの文字列は KH Coder 3.Alpha.7 以降、語の集計から外れる
#                    （旧掲示板 No.3231 の樋口氏の回答）ので「A社_2025」「p12」は語に混ざらない。
#   外部変数_文書.csv ／ 外部変数_ページ.csv … txt の H1／H2 と同じ順・同じ数の行（社・年・群・ページ）
#
# ブロック（④）は「文を壊さないための下処理」として残る（横割れの結合・ページまたぎ・柱・表の行）。
# ブロックの切れ目は**文の切れ目を強制する**だけで、集計単位ではない。
#

# --- ブロックを1つだけ名指しするための鍵（2026-08-13） -------------------
# **「同じページの、同じ文言の、どれか1つ」をどう指すか。**
#
# 経緯（3段階で強くしていった）
# ---------------------------------------------------------------------------
#   ① 文言だけ          → 別のページの同じ文言まで巻き込む
#   ② ＋ページ          → 同じページの見出しと参照リンク（16pt / 7pt）を分けられない
#   ③ ＋pt              → **ptまで同じものが残った**
#   ④ ＋座標 ←いまここ
#
# ③で足りなかった実例（A社 p11。KPIバッジが3枚のカードに1つずつ）：
#
#     6.1pt  KPI   x0= 55.5    ← Planet positive のカード
#     6.1pt  KPI   x0=311.5    ← Prosperity positive のカード
#     6.1pt  KPI   x0=567.6    ← People positive のカード
#
# **1つ消すと3つとも消えた。** 全文書で「同じページ・同じ文言・同じpt」は**115箇所**
# （最大12個重複）あり、例外ではなく普通に起きる。
#
# 🔴 **「座標で持つと壊れやすい」という当初の判断について。**
#    もともと座標を避けたのは「パラメータを変えると単位の切れ方が変わって指し先を見失う」
#    という理由だった。だが改めて考えると、**ブロックの切れ方が変われば文言も変わる**ので、
#    そのときは座標があろうと無かろうと、どのみち一致しない。
#    → **座標を足しても新しい壊れ方は増えない。** 同じ文言の別の実体を区別できるだけ増える。
#
# ⚠️ ただし**当たらなくなったルールは黙って無視される**（＝直したはずが直っていない）。
#    これは元からある性質だが、条件が増えるぶん起きやすくなる。
#    → `analyze_page` が当たらなかったルールを返し、画面で警告を出すようにした。
#
# ⚠️ **座標を持たない古いルールは、これまでどおり位置を問わず一致する。**
#    設定JSONを書き換えずに済ませるため。画面から作るルールには常に座標が入る。

AT_TOL = 1.0        # 座標の許容差(pt)。丸め誤差を吸収するだけで、隣のブロックには届かない値


def _xy(v) -> tuple[float, float] | None:
    """ルールの位置キー `[x, y]` を正規化する。None なら「位置を問わない」。"""
    if not v:
        return None
    try:
        return (round(float(v[0]), 1), round(float(v[1]), 1))
    except (TypeError, ValueError, IndexError):
        return None                      # 壊れた値で落とさない（古い設定JSONを開けなくしない）


def _at_hit(at, bbox) -> bool:
    """ルールの位置キーが、このブロックの左上と一致するか。"""
    if at is None or bbox is None:
        return True
    return abs(at[0] - bbox[0]) <= AT_TOL and abs(at[1] - bbox[1]) <= AT_TOL


# 除外ルール（excluded）について
# ---------------------------------------------------------------------------
# 画面から作るのは常に「そのページの・そのptの・その位置の・その文だけ」を落とすルール。
#
# ここで消すのは、**リンク表記・ロゴ・ページ表記のような、明らかに文書の記述でないもの**。
# 「この語は分析に入れたくない」という判断は KH Coder 側の「使用しない語」でやる。層が違う。
# （JSONを手で編集して page: null にすれば全ページ一括にもできるが、画面には出していない）
#
# ⚠️ これは「前処理での恣意的な取捨選択」そのもの。設定JSONに全部残るようにしてあるので、
#    卒論には除外した文言のリストと件数をそのまま載せる。増えすぎたら方法のほうを疑う。
#    （#23 は「使用しない語は1〜数語に留め、客観的に説明できるものだけにせよ」と警告している）


class Excluder:
    """除外ルールの判定。

    照合は **文言＋ページ＋pt＋位置**（→ 上の「ブロックを1つだけ名指しするための鍵」）。
    文言ごとに引けるようにしておく（単位ごとに全ルールを舐めると、
    数千単位×数十ルールで効いてくるため）。

    ⚠️ pt / 位置 を持たない古いルールは、それぞれ**その条件を問わずに**一致する。
    """

    def __init__(self, rules: list[dict]):
        # 文言 → [(ページ or None, pt or None, 位置 or None, 元のルール), ...]
        self.by_text: dict[str, list[tuple]] = {}
        for r in rules or []:
            text = (r.get("text") or "").strip()
            if not text:
                continue
            pt = r.get("pt")
            self.by_text.setdefault(text, []).append((
                None if r.get("page") is None else int(r["page"]),
                None if pt is None else float(pt),
                _xy(r.get("at")), r))
        self.used: set[int] = set()      # 当たったルール（id）。当たらなかった分を知らせるため

    def hit(self, text: str, page: int, size: float | None = None,
            bbox: list | None = None) -> bool:
        for rp, rpt, at, rule in self.by_text.get(text.strip(), ()):
            if rp is not None and rp != page:
                continue
            # 0.05pt はサイズを round(1桁) で持っていることに対する許容。実質は完全一致
            if rpt is not None and size is not None and abs(rpt - size) > 0.05:
                continue
            if not _at_hit(at, bbox):
                continue
            self.used.add(id(rule))
            return True
        return False

    def unused_on(self, page: int) -> list[dict]:
        """このページを名指ししているのに、1つも当たらなかったルール。

        🔴 **黙って効かないのを防ぐためだけの仕組み。** 条件（ページ・pt・位置）が増えるほど、
           パラメータを変えたときに外れやすくなる。外れたことに気づけないと、
           「除外したはずのものが出力に残っている」まま先へ進んでしまう。
        """
        out = []
        for rules in self.by_text.values():
            for rp, _, _, rule in rules:
                if rp == page and id(rule) not in self.used:
                    out.append(rule)
        return out


class KindOverride:
    """**種別（本文／大／小／極小）の手動指定。**

    種別はフォントptから機械的に決めている（`kind_of`）。だいたい合うが、外れる例がある：

    - 見出しなのに本文と同じptで、色や太さだけで区別しているデザイン → `本文` になる
    - 図解カードの中の説明文が本文より少し小さい → `小` に落ちる（本文として扱いたい）
    - 引用や強調の1文が本文より少し大きい → `大` になり、セクション見出し扱いされかける

    **ptを動かして直そうとしてはいけない。** `SIZE_TOL` は文書全体に効くので、
    1ブロックを直すために全体の判定を崩すことになる。→ **そのブロックだけを名指しで直す。**

    キーは生テキスト＋ページ＋位置（`Excluder` と同じ鍵。理由も同じ）。設定JSONに全部残る。
    ⚠️ 位置を持たない古いルールは、位置を問わず一致する。
    """

    def __init__(self, rules: list[dict]):
        # 文言 → [(ページ or None, 位置 or None, 種別), ...]
        self.by_text: dict[str, list[tuple]] = {}
        for r in rules or []:
            text, kind = r.get("text") or "", r.get("kind")
            if not text.strip() or kind not in KINDS:
                continue
            self.by_text.setdefault(text, []).append((
                None if r.get("page") is None else int(r["page"]),
                _xy(r.get("at")), kind))

    def get(self, text: str, page: int, bbox: list | None = None) -> str | None:
        # ⚠️ ページ指定のあるルールを先に見る。全ページ指定より個別指定が優先
        for want_page in (page, None):
            for rp, at, kind in self.by_text.get(text, ()):
                if rp != want_page or not _at_hit(at, bbox):
                    continue
                return kind
        return None


@dataclass
class Settings:
    """1文書ぶんの抽出設定。そのまま JSON にして保存する（＝卒論の再現性の材料）。

    🔴 2026-08-31 `header_y`・`footer_margin` を廃止（座標カットをやめた。→ DEFAULTS の注記）。
       古い設定JSONに残っている値は from_dict が読み飛ばす。
    """
    repeat_ratio: float = DEFAULTS["repeat_ratio"]
    size_tol: float = DEFAULTS["size_tol"]
    tiny_ratio: float = DEFAULTS["tiny_ratio"]
    col_tol: float = DEFAULTS["col_tol"]
    line_gap: float = DEFAULTS["line_gap"]
    join_gap: float = DEFAULTS["join_gap"]
    min_len: int = DEFAULTS["min_len"]
    body_size: float | None = DEFAULTS["body_size"]
    section_min_pt: float | None = DEFAULTS["section_min_pt"]
    section_max_len: int = DEFAULTS["section_max_len"]
    table_strategy: str = DEFAULTS["table_strategy"]
    tables: list[dict] = field(default_factory=list)
    table_off: list[dict] = field(default_factory=list)
    skip_pages: list[dict] = field(default_factory=list)
    task_states: list[dict] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    joins: list[dict] = field(default_factory=list)
    splits: list[dict] = field(default_factory=list)
    order: str = DEFAULTS["order"]
    manual_order: list[dict] = field(default_factory=list)
    kinds: list[dict] = field(default_factory=list)
    auto_join: bool = DEFAULTS["auto_join"]
    unit_merges: list[dict] = field(default_factory=list)
    unit_excludes: list[dict] = field(default_factory=list)
    unit_checks: list[dict] = field(default_factory=list)
    # 切り取りの点検（ヘッダー／フッター境界の確認）の記録。{"日", "判断", "メモ"}。
    # 空なら「未点検」。→ boundary_scan と ui の「切り取りの点検」
    boundary_check: dict = field(default_factory=dict)

    def excluder(self) -> "Excluder":
        return Excluder(self.excluded)

    def kind_override(self) -> "KindOverride":
        return KindOverride(self.kinds)

    def skip_map(self) -> dict[int, str]:
        """除外ページ番号 → 理由（TASKS のキー。未設定なら空文字）。"""
        out = {}
        for r in self.skip_pages or []:
            out[int(r["page"])] = r.get("reason") or ""
        return out

    def skip_set(self) -> set[int]:
        return {int(r["page"]) for r in self.skip_pages or []}

    def table_off_set(self) -> set[int]:
        """表の自動検出をやめるページ番号。"""
        return {int(r["page"]) for r in self.table_off or []
                if r.get("page") is not None}

    def manual_tables_on(self, page: int) -> list[dict]:
        """そのページで手で指定した表の範囲。"""
        return [r for r in self.tables or []
                if r.get("page") == page and r.get("rect")]

    @classmethod
    def from_dict(cls, d: dict | None) -> "Settings":
        """未知のキーは黙って捨てる。設定JSONに古い項目が残っていても落ちないように。"""
        d = d or {}
        known = {f.name for f in fields(cls)}
        st = cls(**{k: v for k, v in d.items() if k in known})
        st.skip_pages = _migrate_skip_pages(st.skip_pages)
        if st.table_strategy not in TABLE_STRATEGIES:
            st.table_strategy = DEFAULTS["table_strategy"]
        st.auto_join = bool(st.auto_join)
        return st

    def to_dict(self) -> dict:
        return asdict(self)


def _migrate_skip_pages(v) -> list[dict]:
    """`skip_pages` の古い形（番号だけのリスト）を新しい形に移す。

    2026-08-12 より前の設定JSONは `[1, 2, 3]`。**黙って捨てずに理由を空欄で引き継ぐ。**
    空欄のまま残るので、画面の手順一覧に「理由が未設定 N件」として出て、後から埋められる。
    """
    out = []
    for r in v or []:
        if isinstance(r, dict):
            if r.get("page") is None:
                continue
            e = {"page": int(r["page"]), "reason": r.get("reason") or ""}
            if r.get("auto"):            # 機械の候補をそのまま採用した印（→ suggest_skips）
                e["auto"] = True
            out.append(e)
        else:                                   # 旧形式：ただの番号
            out.append({"page": int(r), "reason": ""})
    out.sort(key=lambda r: r["page"])
    return out


def task_status(st: Settings) -> list[dict]:
    """手順ごとの「どう結論したか」。画面・バッチ・卒論の付録が同じものを見るために共通化する。

    状態は3つ。**「未確認」と「該当なし」を必ず区別する** ―― 区別できないと
    「まだ見ていない」のか「見た上で無かった」のかが分からず、記録として使えない。
    """
    smap = st.skip_map()
    notes = {n.get("key"): n for n in st.task_states or []}
    out = []
    for t in TASKS:
        pages = sorted(p for p, reason in smap.items() if reason == t["key"])
        n = notes.get(t["key"]) or {}
        if pages:
            state, memo = "除外した", n.get("memo") or ""
        elif n.get("state") in TASK_STATES:
            state, memo = n["state"], n.get("memo") or ""
        else:
            state, memo = "未確認", ""
        out.append({**t, "state": state, "pages": pages, "memo": memo})
    return out


def unfinished(st: Settings) -> dict:
    """書き出す前に知らせるべきこと。空の dict なら手順は片付いている。

    🔴 2026-08-31 ページ除外の廃止に伴い、手順チェックリスト（TASKS）は引退した。
    分母は全ページ・全文なので「外すべきページを外したか」という問い自体が無くなり、
    催促するとむしろ廃止済みの作業へ誘導してしまう。task_status / TASKS は
    **過去の設定JSONを付録として読むため**に残してある。
    """
    return {}


# --- 本文サイズの推定 ---------------------------------------------------

def size_histogram(doc) -> list[tuple[float, int]]:
    """フォントサイズごとの総文字数。多い順。

    本文ptの推定根拠であり、UI では「どのサイズが何文字あるか」を見せるのに使う。
    しくみ.md §4 の表を、会社ごとに自動で作れるようにしたもの。
    """
    c = collections.Counter()
    for page in doc:
        for blk in page.get_text("dict")["blocks"]:
            for line in blk.get("lines", []):
                for sp in line["spans"]:
                    c[round(sp["size"], 1)] += len(sp["text"].strip())
    return c.most_common()


def detect_body_size(doc) -> float:
    """文書全体で最も文字数の多いフォントサイズ＝本文サイズ、とみなす。

    会社ごとに本文のptが違う（9.0だったり10.5だったり）ので決め打ちにしない。
    ⚠️ 図表の多いレポートではキャプションのほうが多くなる可能性がある（しくみ.md §8）。
       だから UI で上書きできるようにしてある。
    """
    hist = size_histogram(doc)
    return hist[0][0] if hist else 0.0


# --- 1ページぶんの処理 --------------------------------------------------

# --- 柱（全ページで同じ位置に同じ文言）を捨てる（2026-08-22 追加） ------------------
# **「全ページの3割以上で、同じ x（±3pt）に同じ文言」が現れる行**を柱とみなして捨てる。
# 例：P社 2024 のページ右端の章名ナビ（1ページ36単位×190ページ）、タイトル入りフッター。
# 文書ごとに1回数えるだけの機械的な規則。章名の見出しは章の中でしか繰り返さない（3割未満）。
# 割合は `repeat_ratio`（既定 0.3。0 で無効）。少なくとも5ページに現れることも条件にする。
# 🔴 2026-08-31 座標カットの廃止後は、これが**唯一の「捨てる」規則**（冊ごとの判断ゼロ）。
#    反復しない番号・章名がヒットに混ざったときは L2 の unit_excludes（ヘッダー／フッター）で外す
_repeat_cache: dict[tuple, frozenset] = {}


def _line_key(text: str, x0: float, y0: float) -> tuple:
    # ⚠️ y は鍵に入れない（2026-08-22 夕方に外した）。P社 2025 の右端ナビは、その章の小項目の数で
    #    上下にずれるので、同じ y には来ない。同じ x（列）に同じ文言が3割以上のページで出れば柱とみなす
    return (text.strip(), round(x0 / 3))


def repeated_lines(doc, st: Settings) -> frozenset:
    """柱とみなす行の鍵 `(文言, x0/3, y0/3)` の集合。文書ごとに1回だけ数える。"""
    if not st.repeat_ratio:
        return frozenset()
    key = (doc.name, doc.page_count, st.repeat_ratio)
    hit = _repeat_cache.get(key)
    if hit is not None:
        return hit
    count: collections.Counter = collections.Counter()
    for page in doc:
        seen = set()
        for blk in page.get_text("dict")["blocks"]:
            for ln in blk.get("lines", []):
                text = "".join(sp["text"] for sp in ln["spans"])
                if not text.strip():
                    continue
                k = _line_key(text, ln["bbox"][0], ln["bbox"][1])
                if k not in seen:
                    seen.add(k)
                    count[k] += 1
    need = max(5, int(doc.page_count * st.repeat_ratio + 0.999))
    out = frozenset(k for k, n in count.items() if n >= need)
    if len(_repeat_cache) > 16:
        _repeat_cache.clear()
    _repeat_cache[key] = out
    return out


def collect_lines(page, st: Settings, repeats: frozenset | None = None) -> list[dict]:
    """1ページ分の行を座標・サイズ付きで集める。

    柱（反復行）も**捨てずに dropped 印を付けて返す**。
    UI で「今どこが切り落とされているか」を見せるため。バッチ側で dropped を除く。
    `repeats` は `repeated_lines(doc, st)` の結果（None なら柱の判定をしない）。
    🔴 2026-08-31 座標カット（header_y / footer_margin）を廃止：捨てるのは反復だけ。
    """
    out = []
    for blk in page.get_text("dict")["blocks"]:      # sortは付けない（段組みが崩れる）
        for ln in blk.get("lines", []):
            x0, y0, x1, y1 = ln["bbox"]
            text = "".join(sp["text"] for sp in ln["spans"])
            if not text.strip():
                continue
            dropped = "repeat" if (repeats and _line_key(text, x0, y0) in repeats) else None
            out.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1,
                        "size": _line_size(ln["spans"]),
                        "text": text, "dropped": dropped,
                        # 太字か（→ group_lines の _block_break）。span の flags bit4 が太字。
                        # フォント名に Bold が入っているだけで flags が立たないPDFもあるので両方見る
                        "bold": _is_bold(ln["spans"])})
    return out


def _line_size(spans) -> float:
    """行の代表サイズ。

    **日本語の文字を含む span のうち、最も文字数の多いもの**のサイズ。日本語が無ければ最多の span。
    ⚠️ 2026-08-22 まで「最も文字数の多い span」だったが、**英数字だけ別のフォント・別のサイズで
       組む**デザイン（P社 2025：日本語 7.8pt／英数字 8.4pt）で、英数字の多い行だけ 8.4pt になり、
       段落の途中で「大」に化けて段落が割れていた。数字や英単語は本文の一部なので、
       行の大きさは日本語の文字で決める。
    """
    jp, other = [], []
    for sp in spans:
        t = sp["text"].strip()
        if not t:
            continue
        n_jp = sum(1 for c in t if _is_japanese(c))
        (jp if n_jp else other).append((n_jp if n_jp else len(t), sp["size"]))
    pool = jp or other
    if not pool:
        return round(spans[0]["size"], 1) if spans else 0.0
    return round(max(pool, key=lambda p: p[0])[1], 1)


def _is_japanese(c: str) -> bool:
    o = ord(c)
    return (0x3040 <= o <= 0x30ff or 0x3400 <= o <= 0x9fff     # かな・カナ・漢字
            or 0xf900 <= o <= 0xfaff or 0xff66 <= o <= 0xff9f)  # 互換漢字・半角カナ


def _is_bold(spans) -> bool:
    """行の文字の大半が太字か。記号だけの span は数に入れない。"""
    n = b = 0
    for sp in spans:
        k = len(sp["text"].strip())
        if not k:
            continue
        n += k
        if (sp.get("flags", 0) & 16) or "bold" in (sp.get("font") or "").lower():
            b += k
    return n > 0 and b / n >= 0.8


def _overlap_ratio(a: dict, b: dict) -> float:
    """2つの矩形の重なりを、小さいほうの面積に対する比で返す。"""
    ox = min(a["bbox"][2], b["bbox"][2]) - max(a["bbox"][0], b["bbox"][0])
    oy = min(a["bbox"][3], b["bbox"][3]) - max(a["bbox"][1], b["bbox"][1])
    if ox <= 0 or oy <= 0:
        return 0.0
    sa = (a["bbox"][2] - a["bbox"][0]) * (a["bbox"][3] - a["bbox"][1])
    sb = (b["bbox"][2] - b["bbox"][0]) * (b["bbox"][3] - b["bbox"][1])
    return ox * oy / min(sa, sb) if min(sa, sb) > 0 else 0.0


def drop_duplicate_blocks(groups: list[dict]) -> list[dict]:
    """**同じ文字を2回描いている**ブロックの片方を捨てる。

    縁取りや影を出すために、同じ文言を少しずらして重ねて描くデザインがある。
    実データ（p10）：

        11.7pt  テクノロジーの力で事業の環境負荷を低 / 減し、社会に実装することで、地球環境の / 再生をリードする
        11.0pt  テクノロジーの力で事業の環境負荷を  / 低減し、社会に実装することで、地球環境の / 再生をリードする

    **人の目には1つにしか見えないのに、抽出すると2回出てくる。**
    そのままだと頻出語が二重に数えられる。

    ⚠️ **行単位で消してはいけない。** 上の例のように**改行位置が1文字ずれる**ので、
       行単位だと一致する行だけが消えて、**残ったブロックが虫食いになる**（実際そうなった）。
       ブロックにまとめてから、**空白を無視して比較**する。

    判定は「**空白を除いたテキストが一致** かつ **矩形が重なる**（小さいほうの面積の半分以上）」。
    位置が離れていれば別物なので残す（表の中で同じ語が何度も出るのは正常）。
    残すのは**大きいほう**（主たる描画とみなす）。
    """
    kept: list[dict] = []
    seen: dict[str, list[int]] = {}          # 正規化テキスト -> kept のインデックス
    for g in groups:
        key = re.sub(r"\s+", "", g["raw"])
        if not key:
            kept.append(g)
            continue
        hit = None
        for j in seen.get(key, []):
            if _overlap_ratio(kept[j], g) >= 0.5:
                hit = j
                break
        if hit is None:
            seen.setdefault(key, []).append(len(kept))
            kept.append(g)
        elif g["size"] > kept[hit]["size"]:
            kept[hit] = g
    return kept


def merge_row_fragments(lines: list[dict], st: Settings) -> list[dict]:
    """**1つの視覚的な行が横に割れているのを繋ぎ直す。**

    PyMuPDF の "line" は、同じ行でも描画命令が途切れると分かれる。
    実例（p4）：`「Respect every voice.」` と `「Think big. Be ` は同じ y座標なのに別の行だった。
    これを繋がないまま次の工程へ渡すと、**文の途中に句点が補われて文が割れる。**

    ⚠️ **横に繋ぐのは危険な操作**でもある。2段組みでは左段と右段が同じ y にあるため、
       雑に繋ぐと pdfminer で起きた「左右が混ざる」問題を自分で再現することになる。
       だから、**隣り合う断片の横の隙間**で判定する。

    実測（A社 SR 2025・全110ページ／隙間はフォントサイズ比）:

        -6.5 〜 -1.5倍   38件  すべて**同じテキストの重複描画**（縁取り効果）。繋いではいけない
        -1.0 〜  0.0倍  131件  繋ぐべき断片（`（CMP）`+`＊2の構築を`、`⽣成`+`AI`）
                                 括弧のカーニングで、隙間はマイナス（重なる）になる
        +0.5倍〜        多数    表のセル。**+2.5倍は別の段**（繋ぐと文が壊れる）

    → 上限を **+0.3倍** に置けば、繋ぐべきものと段の境界（+2.5倍）を安全に分けられる。
       重複描画は「直前と同じテキストなら繋がない」で除ける。
    """
    if not lines:
        return lines
    # ① 同じフォントサイズごとに分け ② y座標で「行バンド」に切り ③ バンド内を左から順に見る
    #
    # ⚠️ **必ずバンド内を x0 の昇順で見ること。**
    #    最初の実装は「サイズ→y→x」で一括ソートしていたが、y が 1pt ずれるだけで
    #    別のバンドに落ちて x の順序が逆転し、**右段の行の直後に左段の行が来た**。
    #    その結果 gap が -399pt という巨大なマイナスになり、「隙間が小さい」と誤判定して
    #    `技術開発によるイノベーションの創出` + `サステナビリティ担当執行役員メッセージ`
    #    のように**別々の段を繋いでしまった**（2026-08-09 に修正）。
    by_size: dict[float, list[int]] = {}
    for i, ln in enumerate(lines):
        by_size.setdefault(round(ln["size"], 1), []).append(i)

    heads: dict[int, dict] = {}          # 元のインデックス -> 連結後の行
    for size, idxs in by_size.items():
        idxs.sort(key=lambda i: lines[i]["y0"])
        # 行バンド：先頭行を基準に、y のずれが size*0.3 以内のものを同じ行とみなす
        # （基準を先頭に固定するのは、少しずつずれた行が数珠つなぎになるのを防ぐため）
        bands: list[list[int]] = []
        base = None
        for i in idxs:
            if base is not None and lines[i]["y0"] - lines[base]["y0"] <= size * 0.3:
                bands[-1].append(i)
            else:
                bands.append([i])
                base = i

        for band in bands:
            band.sort(key=lambda i: lines[i]["x0"])     # ★ここが要
            cur = prev_text = None
            for i in band:
                ln = lines[i]
                if cur is not None:
                    gap = ln["x0"] - cur["x1"]
                    dup = ln["text"].strip() == prev_text        # 重複描画よけ
                    # 下限も設ける。大きく重なるものは同じ行の続きではなく別の要素
                    if (not dup and -cur["size"] * 1.2 <= gap <= cur["size"] * st.join_gap):
                        cur["text"] += ln["text"]
                        cur["x1"] = max(cur["x1"], ln["x1"])
                        cur["y1"] = max(cur["y1"], ln["y1"])
                        prev_text = ln["text"].strip()
                        continue
                cur = dict(ln)
                prev_text = ln["text"].strip()
                heads[i] = cur
    # 元の順序（＝PyMuPDFのブロック順＝段の順序）に戻して返す
    return [heads[i] for i in sorted(heads)]


def group_lines(lines: list[dict], st: Settings) -> list[list[dict]]:
    """同じ列で縦に連続する行をまとめる。

    ブロック単位でまとめられない理由は しくみ.md §3-③。
    （本文は1行ごとに別ブロックへ割れ、逆に別々のKPIが同じブロックに入る）
    """
    groups, cur = [], []
    for ln in lines:
        if cur:
            prev = cur[-1]
            # ⚠️ 0.3 から size_tol（既定0.6）に広げた（2026-08-22）。英数字だけの行（別フォントで
            #    0.6pt 大きい）が段落の途中で別ブロックになるのを防ぐ。見出しは太字・字下げ・
            #    空きで別に切れるので、ここを広げても見出しが本文にくっつくことはまず無い
            same_size = abs(ln["size"] - prev["size"]) <= st.size_tol + 0.05
            same_col = abs(ln["x0"] - prev["x0"]) <= st.col_tol
            next_line = 0 <= ln["y0"] - prev["y0"] <= prev["size"] * st.line_gap
            if (same_size and same_col and next_line
                    and not _block_break(cur, ln)):
                cur.append(ln)
                continue
            groups.append(cur)
        cur = [ln]
    if cur:
        groups.append(cur)
    return groups


# 箇条書き・番号付きの項目の頭。「●」「・」「※1」「1．」「(1)」「①」など
_LIST_HEAD = re.compile(r"^(?:[●○■□◆◇▲△▼▽・※]|[①-⑳]|[（(]\d{1,2}[)）]|\d{1,2}[．.、]\s*\S|[a-zA-Z][.)]\s)")


def _block_break(cur: list[dict], ln: dict) -> bool:
    """同じ列・同じ大きさ・行送りの範囲内でも、ここでブロックを切る条件。

    ブロックの切れ目は**文の切れ目**になる（→ to_units が句点を補う）。だから、ここで見るのは
    「この行は前の行の文の続きではない」と**機械的に**言える2つだけ：
      ① 箇条書き・番号の頭で始まる行（新しい項目）
      ② 太字だけの行と、そうでない行の境目（本文と同じ pt で太さだけで見出しにしたデザイン）
    🔴 2026-08-22 夜に、字下げ・前の行が短い・列の右端 などの「段落」の規則を外した。
       レイアウトから段落を当てる規則は社ごとに破綻し、直すたびに別の社で副作用が出たため
       （→「集計単位について」）。集計単位は文とページで、段落は使わない。
    """
    if _LIST_HEAD.match(ln["text"].lstrip()):
        return True
    if bool(ln.get("bold")) != bool(cur[-1].get("bold")):
        return True
    return False


def kind_of(size: float, body: float, st: Settings) -> str:
    """本文サイズとの比較で種別を決める。捨てずに分けるためのラベル。

    ⚠️ **「極小」を最初に見る。** 放っておくと「小」に紛れて見えなくなるため。

       **名前が「極小」であって「サムネイル」でないのは、判定しているのがptだけだから。**
       見つけたきっかけは、図解ページに他ページのカードを縮小して並べたサムネイルが
       あり、そこから出た16単位すべてが他ページと同じ文言だった（＝同じ記述を二重に
       数えていた）こと。だが実際に引っかかるのはそれだけではない：

           A社 SR 2025（本文9.3pt → しきい値4.65pt未満）で19単位
               p7  2.6〜3.5pt  サムネイル16件
               p40 3.0pt       図中のロゴ `OIL OIL`
               p66/67 3.6〜3.9pt  注記記号 `＊1`

       ロゴや注記記号を「縮小（＝他ページの縮小版）」と呼ぶのは事実に反する。
       **測っているものをそのまま名前にする**（→ しくみ.md §3-⑦）。

       消さずに種別を付けるのは、他の種別と同じ理由。CSVには残るので、
       分析のときに絞るかどうかを KH Coder 側で選べる。
       ⚠️ 閾値は会社ごとに違うはず。A社では全体の0.5%だったが、
          サムネイルを多用する会社ではもっと多くなる。画面で件数を見て決めること。
    """
    if st.tiny_ratio and size < body * st.tiny_ratio:
        return "極小"
    if abs(size - body) <= st.size_tol:
        return "本文"
    return "大" if size > body else "小"


def to_units(text: str, st: Settings) -> list[str]:
    """生のテキストを、句点で区切った単位のリストにする。

    日本語は行末の改行を消すだけで繋がる（英語のようなスペース判断が要らない）。
    句点で終わらない単位（見出し・ラベル）には句点を補う。理由は しくみ.md §3-⑨。

    ⚠️ **入力は「句点を補う前」の生テキスト。** 手でブロックを繋ぐとき（`joins`）も、
       補われた句点を消して回るのではなく、**生テキストの段階で繋いでからここへ通す。**
       そうすれば `…「Prosperity` ＋ `positive（経済）」…` が自然に1文になる。
    """
    return [p.strip() + "。" for p in clean_text(text).split("。")
            if len(p.strip()) >= st.min_len]


def clean_text(text: str) -> str:
    """`to_units` の整形部分（句点で切る前段。2026-08-26 に切り出し）。

    🔴 どの置換も**句点「。」を作らず・消さず・またがない**。だから
    「整形してから切る」＝「切ってから各片を整形する」が成り立つ。
    この性質を、原本上の語ハイライトの文への帰属（生テキストの句点位置 → 文）が
    使っている（→ ui/app.py の `/hits`）。整形規則を足すときはこの性質を壊さないこと。
    """
    t = text.replace("　", " ")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"[■●▲◆•]+", "", t)
    # 私用領域（U+E000〜F8FF）の文字＝Wingdings 等の記号フォントで描いた箇条書きの点・矢印。
    # 文字としての意味は無く、残すと KH Coder で謎の1文字語になる（B社 2022 p18 の U+F09F）
    t = re.sub(r"[\ue000-\uf8ff]+", "", t)
    # 制御文字（目次のリーダー線や箇条書きを制御コードで描いているPDFがある）と、
    # 同じ記号の3回以上の連続（「…………」「........」のリーダー）を落とす
    t = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", t)
    t = re.sub(r"([^\w\s、。])\1{2,}", "", t)
    return t


# 列の切れ目とみなす x方向の隙間（ページ幅に対する比）。841pt なら約50pt。
# 段組みの段間（実測で270pt以上）よりずっと小さく、列内の字下げ（十数pt）より大きい値。
COL_GAP_RATIO = 0.06


def reading_order(groups: list[dict], page_width: float) -> list[dict]:
    """**人が読む順に並べ替える。**

    PyMuPDF が返すブロックの順序は**描画順**であって、読む順ではない。
    実データ（p4 CEOメッセージ）では、写真の上に重ねた見出しが最後に描かれるため：

        gid0 グローバル共通の価値観「Our Way」   (y=165)   ← 本文が先
        …
        gid6 CEOメッセージ                      (y= 50)   ← ページ最上部の見出しが最後

    ⚠️ **これは表示の問題では済まない。** セクションは「直近の大見出しを引き継ぐ」方式
    （§3-⑩）なので、見出しが本文より後ろにあると、**本文が前ページのセクションに
    割り当てられてしまう。** 実際 CEOメッセージの本文は `Contents` に入っていた。

    並べ替え方：**x0 の隙間で「列」に切り、列を左から順に、列の中は上から順に。**
    単純な y ソートでは2段組みの左右が交互になるので使えない（§2 と同じ罠）。
    """
    if len(groups) < 2:
        return groups
    xs = sorted(g["bbox"][0] for g in groups)
    gap_min = page_width * COL_GAP_RATIO
    bounds = [(a + b) / 2 for a, b in zip(xs, xs[1:]) if b - a >= gap_min]

    def col(g):
        return sum(1 for b in bounds if g["bbox"][0] > b)

    return sorted(groups, key=lambda g: (col(g), g["bbox"][1], g["bbox"][0]))


def apply_manual_order(groups: list[dict], st: Settings, pageno: int) -> list[dict]:
    """**手で並べ替えたページの順序を復元する。**

    `reading_order` は「列に切って左上から」という機械的な規則でしかないので、
    実データではまだ崩れる（回り込みのある図解ページ、段の幅が途中で変わるページなど）。
    レイアウトの意図は座標だけからは復元できない。→ **人が並べ替えて、それを残す。**

    ルールは `{"page": ページ, "keys": [生text, 生text, ...]}`。
    **並べ替えは `apply_joins` より前に効かせる**（結合は「隣り合うブロック」を繋ぐ操作なので、
    先に順序を確定させないと隣が変わる）。だから `keys` に入るのは**結合前の生text**。

    ⚠️ 保存した後でパラメータ（JOIN_GAP など）を触ると、ブロックの切れ方が変わって
       `keys` に無いブロックが現れる。そういうブロックは**直前のブロックの後ろに置く**ので、
       並びが全部飛ぶことはない（元の位置の近くに留まる）。

    🔴 **同じ文言のブロックが複数あるページでの割り当て**（2026-08-13 修正）。
       以前は `{生text: 順位}` の辞書を作っていたので、同じ文言があると**最後の1つで
       上書きされ、全部が同じ順位になっていた。** 実例（A社 p11）：`KPI` が3つ。
       `keys` が `[…, KPI, B, KPI, …]` のとき、1つ目の `KPI` まで B の後ろへ飛んでいた。

       → **`keys` を先頭から見て、同じ文言のブロックへ出てくる順に割り当てる。**
       ここは座標を足さずに直せる。**文言が同じなら、どちらがどちらに割り当たっても
       出力は同じ**（区別できないものを区別する必要がない）ので、順番に配れば足りる。
    """
    keys = None
    for r in (st.manual_order or []):
        if r.get("page") == pageno:
            keys = r.get("keys") or []
    if not keys:
        return groups

    todo: dict[str, list[int]] = {}        # 生text → まだ割り当てていないブロックの添字
    for i, g in enumerate(groups):
        todo.setdefault(g["raw"], []).append(i)
    rank: dict[int, int] = {}              # ブロックの添字 → 指定された順位
    for i, k in enumerate(keys):
        q = todo.get(k)
        if q:
            rank[q.pop(0)] = i

    ranked, last, run = [], -1.0, 0
    for i, g in enumerate(groups):
        p = rank.get(i)
        if p is None:
            run += 1                       # 指定に無いブロック＝直前のすぐ後ろに置く
        else:
            last, run = float(p), 0
        ranked.append(((last, run), g))
    ranked.sort(key=lambda t: t[0])        # 安定ソート。同順位は元の順序のまま
    return [g for _, g in ranked]


def apply_joins(groups: list[dict], st: Settings, pageno: int) -> list[dict]:
    """**手で指定したブロック同士を繋ぐ。**

    段組みのページでは、左段の末尾から右段の先頭へ本文が続く。
    しかし PDF の中では完全に別のブロックで、**座標からは「続き」だと判定できない**
    （右段の先頭が、左段の続きなのか新しい話題なのかは、意味を読まないと分からない）。
    → **ロジックで解こうとせず、人が指定する。** その指定は設定JSONに残るので再現できる。

    ⚠️ **繋ぐのは「句点を補う前」の生テキスト同士。** だから

        …今後は「Planet positive（環境）」「Prosperity   ＋   positive（経済）」「People…

    が連結されてから句点で切り直され、**1つの文として正しく出てくる。**
    「末尾の句点を消して繋ぐ」という後始末が要らない。

    ルールは `{"page": ページ, "a": 前の生text, "b": 後の生text}`。
    判定は生テキストの完全一致（除外ルールと同じ理由 → `Excluder` の説明）。

    ⚠️ **`a` も `b` も「結合前」の生text であること。** 既に結合されたブロックの
    連結後テキストを渡すと、どのブロックにも当たらず**黙って無視される**。
    （画面から `⬆ 上と結合` を押しても何も起きない、という形で実際に露見した）

    戻り値は `(結合後のグループ, 当たらなかったルール)`。
    **当たらなかったルールを捨てずに返すのは、黙って効かないのを防ぐため。**
    パラメータを変えるとブロックの切れ方が変わり、保存済みのルールが外れることがある。
    """
    # ⚠️ `part_boxes` は各パーツの**結合前の左上座標**。種別・集計単位の区切りは
    #    パーツ単位で名指しするので（→「ブロックを1つだけ名指しするための鍵」）、
    #    結合で潰れてしまう前の座標をここで取っておく必要がある。
    rules = {(r.get("a", ""), r.get("b", "")) for r in (st.joins or [])
             if r.get("page") == pageno}
    if not rules:
        for g in groups:
            g["parts"] = [g["raw"]]
            g["part_boxes"] = [g["bbox"][:2]]
        return groups, []

    used: set[tuple[str, str]] = set()
    out: list[dict] = []
    prev_raw = None                 # 直前に処理した「元の」ブロックの生text
    for g in groups:
        if out and (prev_raw, g["raw"]) in rules:
            used.add((prev_raw, g["raw"]))
            p = out[-1]
            p["raw"] += g["raw"]
            p["lines"] += g["lines"]
            p["parts"].append(g["raw"])
            p["part_boxes"].append(g["bbox"][:2])
            prev_raw = g["raw"]     # 連鎖して繋げるように、元のtextで覚える
            continue
        g["parts"] = [g["raw"]]
        g["part_boxes"] = [g["bbox"][:2]]
        out.append(g)
        prev_raw = g["raw"]
    return out, [{"a": a, "b": b} for a, b in rules - used]


def apply_splits(line_groups: list[list[dict]], st: Settings,
                 pageno: int) -> tuple[list[list[dict]], list[dict]]:
    """**手で指定したブロックを、指定した行の手前で2つに分ける（`apply_joins` の逆）。**

    きっかけ（D社 2023 p62）：本文と同じptで、色や太さだけで見出しにしたデザインでは、
    見出しの行が下の本文と**同じブロック**に入ることがある。見出しは句点で終わらないので、
    句点で切り直すと**見出し＋本文が1つの文**になってしまう。同じ見た目の見出しでも、
    独立したブロックなら単独の「文」になる——つまり扱いが**PDFの内部構造の偶然**で
    変わってしまう。座標や文字からは「ここに見出しがある」と判定できないので、
    → **ロジックで解こうとせず、人が名指しで分ける。** 指定は設定JSONに残るので再現できる。

    ルールは `{"page": ページ, "text": 分ける前の生text, "at": [x, y], "line": N}`。
    N行目の手前で切る（1始まり。N=1 は「先頭の手前」なので不正）。
    照合は生テキストの完全一致＋左上の位置（除外・種別と同じ鍵の考え方）。

    ⚠️ **結合（joins）より前**、ブロックが group_lines から出てきた直後の行のまとまりに
    適用する。だから `text` は**結合前**の生text（joins の a/b と同じ流儀）。
    分けた**両方の断片**に続けて照合するので、1ブロックを3つ以上に分けることもできる
    （2本目のルールは、1本目で分けた断片の生textを名指しする）。
    🔴 前半にも照合を続けるのは 2026-09-01 から。従来は後ろ半分だけだったため、
    「後ろの境目 → 前の境目」の順で操作すると、前半を名指しした2本目のルールが
    どこにも当たらず**黙って効かなかった**（D社 2025 p105 で実害。UIは見えている
    ブロックの境目を選ばせるので、エンジン側が操作の順序に依存してはいけない）。

    戻り値は `(分けた後の行グループ, 当たらなかったルール)`。
    当たらなかったルールを捨てない理由は `apply_joins` と同じ（黙って効かないのが一番まずい）。
    """
    rules = [r for r in (st.splits or []) if r.get("page") == pageno]
    if not rules:
        return line_groups, []
    used: set[int] = set()
    out: list[list[dict]] = []
    for g in line_groups:
        # ルールが当たらなくなるまで全断片を見直す。1回の分割で1本のルールを消費するので
        # 必ず止まる。断片は元の並び順のまま（前半を i の位置で置き換える）
        pieces: list[list[dict]] = [g]
        i = 0
        while i < len(pieces):
            rest = pieces[i]
            raw = "".join(ln["text"] for ln in rest)
            pre = [min(ln["x0"] for ln in rest), min(ln["y0"] for ln in rest)]
            hit = None
            for j, r in enumerate(rules):
                if j in used or (r.get("text") or "") != raw:
                    continue
                if not _at_hit(_xy(r.get("at")), pre):
                    continue
                try:
                    k = int(r.get("line") or 0)
                except (TypeError, ValueError):
                    k = 0
                if 1 <= k < len(rest):
                    hit = (j, k)
                    break
            if hit is None:
                i += 1                       # この断片はもう分かれない。次へ
                continue
            used.add(hit[0])
            pieces[i:i + 1] = [rest[:hit[1]], rest[hit[1]:]]   # 前半も含めて i から見直す
        out.extend(pieces)
    return out, [rules[j] for j in range(len(rules)) if j not in used]


# --- 表（2026-08-22 追加） -----------------------------------------------
# **罫線で区切られた表を、行ごとに1ブロックに組み直す。**
#
# きっかけ（B社 2022 p68。健康経営の指標表）
# ---------------------------------------------------------------------------
# 表のセルは1つずつ別のブロックになるので、③④の規則（同じ列・行送り）で繋ぐと
# **列ごと**に繋がる。「定期健康診断受診率」と「99.9%」は別の列にあるので、
# 1ページが142ブロックに割れ、指標名と数値が二度と出会わなかった。
# 人が読むときの単位は**行**（指標名＋年度ごとの値）なので、そちらに合わせる。
#
# どうやって表を見つけるか
# ---------------------------------------------------------------------------
# PyMuPDF の `page.find_tables()` を使う。既定の `lines_strict` は、PDFの中の**罫線（線分）**
# だけを手がかりにセルを組み立てる（pdfplumber 由来の規則的な処理。学習モデルではない）。
#   ・塗りつぶしの矩形は見ない（`lines` にすると見る。B社 p68 では列が16本に割れた）
#   ・罫線の無い表は見つからない → 人が範囲を指定する（`tables`。`strategy: "text"`）
#   ・図解の枠線も表として拾う → 人がそのページの検出を切る（`table_off`）
# 全社で同じ規則を適用し、手で足し引きした分は設定JSONに残る。
#
# 行への組み直し
# ---------------------------------------------------------------------------
# 検出された表の**横罫線の y 座標**で行バンドを、**セルの左端の x 座標**で列バンドを作り、
# 表の範囲内の各行（テキスト行）を、その中心座標が入るバンドに割り当てる。
#   ・同じ行バンドのものを、列の順（左→右）に、同じ列の中は上から並べて1ブロックにする
#   ・列と列の間は半角スペース、同じ列の中で折り返した行は詰めて繋ぐ（日本語）
# セル単位で割り当てないのは、**1列ぶんのラベルがセル1つにまとまっているPDF**があるため
# （D社 2023 p33：「Scope 1／Scope 2／Scope 3」が1セル）。行バンドで割ると正しく別行に入る。
# 代償として、複数行に折り返した結合セル（「生活習慣病・／がん対策」）は2行に割れる。
# ⚠️ 表の中にある表（外枠）は捨てる。D社 2023 p33 はページ全体を囲む枠が 2×2 の表に見えた。
#
# 出力は種別 `表`。**ptでは決まらない種別**なので、他の4つとは別の軸だが、
# 「分析時に KH Coder で絞れる」という使い方は同じ。

# 表とみなす最低限の大きさ。1×N / N×1 は枠付きの本文や箇条書きなので表にしない
TABLE_MIN_ROWS = 2
TABLE_MIN_COLS = 2


def _rect_contains(a, b, tol: float = 1.0) -> bool:
    """矩形 a が矩形 b を含むか。"""
    return (a[0] - tol <= b[0] and a[1] - tol <= b[1]
            and a[2] + tol >= b[2] and a[3] + tol >= b[3])


# 表検出の結果キャッシュ（2026-08-26 追加）。
# 🔴 **find_tables が処理時間の9割超**（D社 2023 実測：extract_doc 6.5秒のうち約6秒）。
# 検出結果は「そのページ＋表関連の設定（strategy・table_off・手動の範囲）」だけで決まる
# **純関数**なので、その組でキャッシュする。確認モードの L1 編集（文の除外・結合など）は
# 表の設定を変えない＝再解析で表検出を全ページぶんスキップでき、体感が数秒→1秒弱になる。
# ⚠️ 返す前に deepcopy する（呼び出し側やJSON化で書き換えられてもキャッシュを汚さない）
_table_cache: dict[tuple, list] = {}


def find_page_tables(page, st: Settings, pageno: int) -> list[dict]:
    """そのページの表。`[{bbox, ybounds, xbounds, strategy, manual}]`。

    自動検出（`table_strategy`）＋手で指定した範囲（`tables`）。
    `table_off` に入っているページでは自動検出をしない（手で指定した範囲だけ見る）。
    """
    cache_key = (page.parent.name, page.parent.page_count, pageno,
                 st.table_strategy, pageno in st.table_off_set(),
                 json.dumps(st.manual_tables_on(pageno), ensure_ascii=False, sort_keys=True))
    hit = _table_cache.get(cache_key)
    if hit is not None:
        return copy.deepcopy(hit)

    found: list[dict] = []

    def collect(strategy: str, clip=None, manual=False):
        if strategy == "none":
            return
        try:
            tf = page.find_tables(strategy=strategy, clip=clip)
        except Exception:
            return                                  # 壊れたPDFで落とさない
        for t in tf.tables:
            if t.row_count < TABLE_MIN_ROWS or t.col_count < TABLE_MIN_COLS:
                continue
            ys = sorted({round(v, 1) for r in t.rows for v in (r.bbox[1], r.bbox[3])})
            xs = sorted({round(c[0], 1) for c in t.cells if c} | {round(t.bbox[2], 1)})
            if len(ys) < 3 or len(xs) < 3:          # バンドが作れない
                continue
            found.append({"bbox": [round(v, 1) for v in t.bbox],
                          "ybounds": ys, "xbounds": xs,
                          "cells": [[round(v, 1) for v in c] for c in t.cells if c],
                          "strategy": strategy, "manual": manual})

    if pageno not in st.table_off_set():
        collect(st.table_strategy)
        # 罫線（線分）で1つも見つからなければ、塗りつぶしの矩形も手がかりにして試す。
        # 塗りだけで行や列を区切る表（U社 2025 p97〜99 のシナリオ表）を拾うため。
        # ⚠️ 罫線表のあるページでは使わない（B社 p68 では塗りが邪魔をして列が16本に割れた）
        if not found and st.table_strategy == "lines_strict":
            collect("lines")
    for r in st.manual_tables_on(pageno):
        try:
            clip = pymupdf.Rect(*[float(v) for v in r["rect"]])
        except (TypeError, ValueError):
            continue
        collect(r.get("strategy") or "text", clip=clip, manual=True)

    # 入れ子の外側（ページや段落を囲む枠）は捨てる。中の表だけ残す
    keep = []
    for i, t in enumerate(found):
        outer = any(j != i and _rect_contains(t["bbox"], u["bbox"])
                    and not _rect_contains(u["bbox"], t["bbox"])
                    for j, u in enumerate(found))
        if not outer:
            keep.append(t)
    if len(_table_cache) > 30000:      # 61冊×100ページでも余る上限。溢れたら作り直し
        _table_cache.clear()
    _table_cache[cache_key] = copy.deepcopy(keep)
    return keep


def _band(bounds: list[float], v: float) -> int:
    """v が bounds のどのバンドに入るか（0始まり）。外側は端のバンドに寄せる。"""
    n = len(bounds) - 1
    for i in range(n):
        if v < bounds[i + 1]:
            return i
    return n - 1


def table_row_groups(tables: list[dict], lines: list[dict],
                     pad: float = 2.0) -> tuple[list[dict], list[dict]]:
    """表の中の行を、**表の1行＝1ブロック**に組み直す。

    戻り値は `(表のブロック, 表に入らなかった行)`。表のブロックは `group_lines` の出力と
    同じ形（行の dict のリスト）に、`table`（ページ内の表番号）と `row`（行番号）を足して返す。
    ブロックの生textは `raw` に入れる（列の間は半角スペース。→ 上の説明）。

    **結合セル（複数の行バンドにまたがるセル）の扱い**
    ---------------------------------------------------------------------------
    セルの中の行を、**行送りの近さ**（1.8×pt 以内）で「ひとつながり（run）」にまとめてから決める。
      ・run が1つだけ → 縦中央寄せの見出し（「重点施策」の列）や折り返した文言（「疾病による休業の状／況」）
        → **1つの文言**として、セルの先頭の行バンドに入れる（結合セルの見出しは先頭行に属するとみなす）
      ・run が複数   → 1セルの中に行ごとのラベルが並んでいる（D社 2023 p33「Scope 1／2／3」、
        「電気／ガス／燃料」が1セル）→ **run ごとに、その位置の行バンドへ**
    セルに入らない行（罫線の外にはみ出した文字）は、中心の y 座標の行バンドに入れる。
    """
    if not tables:
        return [], lines
    rest: list[dict] = []
    # (表番号, 行バンド) → [(列バンド, 行)]
    buckets: dict[tuple[int, int], list[tuple[int, dict]]] = {}
    # (表番号, セル番号) → [行]
    in_cell: dict[tuple[int, int], list[dict]] = {}
    for ln in lines:
        cx, cy = (ln["x0"] + ln["x1"]) / 2, (ln["y0"] + ln["y1"]) / 2
        hit = None
        for ti, t in enumerate(tables):
            b = t["bbox"]
            if b[0] - pad <= cx <= b[2] + pad and b[1] - pad <= cy <= b[3] + pad:
                hit = ti
                break
        if hit is None:
            rest.append(ln)
            continue
        t = tables[hit]
        ci = next((k for k, c in enumerate(t["cells"])
                   if c[0] - 0.5 <= cx <= c[2] + 0.5 and c[1] - 0.5 <= cy <= c[3] + 0.5), None)
        if ci is None:
            buckets.setdefault((hit, _band(t["ybounds"], cy)), []).append(
                (_band(t["xbounds"], cx), ln))
        else:
            in_cell.setdefault((hit, ci), []).append(ln)

    for (ti, ci), ls in in_cell.items():
        t = tables[ti]
        c = t["cells"][ci]
        ls.sort(key=lambda l: (l["y0"], l["x0"]))
        col = _band(t["xbounds"], (c[0] + c[2]) / 2)
        first_band = _band(t["ybounds"], c[1] + 0.5)
        last_band = _band(t["ybounds"], c[3] - 0.5)
        if last_band <= first_band:                      # 1行バンドに収まるセル
            for l in ls:
                buckets.setdefault((ti, first_band), []).append((col, l))
            continue
        # 複数の行バンドにまたがるセル：行送りの近い行を「ひとつながり（run）」にまとめる
        runs: list[list[dict]] = [[ls[0]]]
        for prev, l in zip(ls, ls[1:]):
            if l["y0"] - prev["y0"] <= max(prev["size"], l["size"]) * 1.8:
                runs[-1].append(l)
            else:
                runs.append([l])
        if len(runs) == 1:
            # ひとつながりだけ＝結合セルの見出し（縦中央寄せ／折り返し）→ 先頭の行バンドへ
            for l in ls:
                buckets.setdefault((ti, first_band), []).append((col, l))
        else:
            # 離れた run が複数＝1セルの中に行ごとのラベルが並んでいる → それぞれの行バンドへ
            for run in runs:
                l0 = run[0]
                band = _band(t["ybounds"], (l0["y0"] + l0["y1"]) / 2)
                for l in run:
                    buckets.setdefault((ti, band), []).append((col, l))

    out: list[dict] = []
    for (ti, ri) in sorted(buckets):
        # 同じ列の中は上から。⚠️ 上付き文字（「m3」の 3）は y0 が少し上にあるので、
        #    中心の y を行の高さで丸めて「見た目が同じ行」を揃え、その中を x の順にする
        items = sorted(buckets[(ti, ri)], key=lambda p: (
            p[0], round((p[1]["y0"] + p[1]["y1"]) / 2 / max(p[1]["size"], 1.0) / 0.8), p[1]["x0"]))
        out.append({"lines": [ln for _, ln in items],
                    # 各行の列バンド。分割（apply_table_splits）で raw を組み直すのに使う
                    "cols": [ci for ci, _ in items],
                    "raw": _row_raw(items),
                    "table": ti, "row": ri})
    return out, rest


def _row_raw(items: list[tuple[int, dict]]) -> str:
    """表の行の生text（同じ列の中は詰めて繋ぎ、列の間は半角スペース。→ 上の「行への組み直し」）。"""
    cols: list[list[str]] = []
    last_col = None
    for ci, ln in items:
        if ci != last_col:
            cols.append([])
            last_col = ci
        cols[-1].append(ln["text"].strip())
    return " ".join("".join(c) for c in cols if any(c))


def apply_table_splits(table_rows: list[dict], st: Settings,
                       pageno: int) -> tuple[list[dict], list[dict]]:
    """**表の行ブロックを、行（セル断片）の境目で2つに分ける**（`apply_splits` の表版。2026-09-01）。

    きっかけ（D社 2024 p11）：KPI表で、1つの行の中に「ラベル＋箇条書き」のセル群が
    目標側と実績側の2つ連結され、行の鍵になるラベルが行内で再掲されていた。
    行が実質、並列したサブ表の連結になっているケースは、セル群の境目で分けて
    「ヒットを含むセル群＝抽出単位」とする（→ 記録/2026-09-01.md。
    単一セル内の箇条書きは 08-26 の決定どおり行全体のまま）。

    ルールの形は splits と同じ `{"page", "text", "line", "reason"}`（設定JSONも同じ場所）。
    ⚠️ 照合の `text` は**行の raw**。通常ブロックと違い、表の raw は列の組み直しで
    作られるので行textの連結とは一致しない。だから分けた後の raw も同じ組み直し
    （_row_raw）で作り直す。`line` は行ブロック内の並び（列→上から）の
    N 行目の手前で切る（1始まり）。分けた**両方の断片**に続けて照合するので、
    2本目のルールで3つ以上に分けることもできる（apply_splits と同じ流儀。
    🔴 前半にも照合を続ける＝操作の順序に依存しない。2026-09-01）。

    戻り値は `(分けた後の行ブロック, 使われたルール)`。
    **未適用の報告は analyze_page 側で apply_splits の結果と突き合わせる**
    （同じ splits のルールを、通常ブロックと表の行の両方の経路で探すため）。
    """
    rules = [r for r in (st.splits or []) if r.get("page") == pageno]
    if not rules or not table_rows:
        return table_rows, []
    used: list[dict] = []
    out: list[dict] = []
    for tr in table_rows:
        pieces: list[dict] = [tr]
        i = 0
        while i < len(pieces):
            rest = pieces[i]
            hit = None
            for r in rules:
                if any(r is u for u in used) or (r.get("text") or "") != rest["raw"]:
                    continue
                try:
                    k = int(r.get("line") or 0)
                except (TypeError, ValueError):
                    k = 0
                if 1 <= k < len(rest["lines"]):
                    hit = (r, k)
                    break
            if hit is None:
                i += 1
                continue
            r, k = hit
            used.append(r)
            head = {**rest, "lines": rest["lines"][:k], "cols": rest["cols"][:k],
                    "raw": _row_raw(list(zip(rest["cols"][:k], rest["lines"][:k])))}
            tail = {**rest, "lines": rest["lines"][k:], "cols": rest["cols"][k:],
                    "raw": _row_raw(list(zip(rest["cols"][k:], rest["lines"][k:])))}
            pieces[i:i + 1] = [head, tail]      # 前半も含めて i から見直す
        out.extend(pieces)
    return out, used


# --- 続いている文を自動で繋ぐ（2026-08-22 追加） ---------------------------------
# **段の末尾・ページの末尾で切れた文を、次のブロックの頭と繋ぐ。**
#
# きっかけ
# ---------------------------------------------------------------------------
# 2段組みでは左段の末尾の文が右段の頭へ続く。ページの末尾の文が次ページの頭へ続くことも多い。
# PDFの中では完全に別のブロックなので ④ の規則では繋がらず、句点が補われて**1つの文が2つの
# 「文」になる**。これまでは手で `⬆ 上と結合` していたが、60冊では回らない。
#
# 「続いている」と判定する条件（座標と文字だけを見る。意味は読まない）
# ---------------------------------------------------------------------------
#   ① 前のブロックが句点・閉じ括弧などで終わっていない（`：` でも終わっていない）
#   ② 前のブロックが2行以上あり、最終行がそのブロックの右端まで届いている（1.5文字以内）
#      ＝ 書き手がそこで文を終えたのではなく、段・ページの境界で切れた
#      ⚠️ 1行しか無いブロック（見出し・ラベル・箇条書き）は右端が分からないので繋がない
#   ③ 前のブロックが文章である（句点を含む。2行だけなら必須、3行以上なら不問）
#      ＝ 2行のラベル（「新規開発製品に対する／環境配慮設計適用率」）を繋がないため
#   ④ 次のブロックの1行目が、そのブロックの左端より引っ込んでいない（字下げ＝新しい段落）
#   ⑤ 次のブロックも文章である（句点を含む）か、「、」「」」「）」のように文の途中でしか
#      現れない文字で始まる
#   ⑥ 種別が同じ（本文どうし・小どうし）で、文字サイズも同じ。表の行は対象外
# 見出し（大）や、箇条書きの1行（句点無し・1行）はこの条件に当たらないので繋がらない。
#
# 繋いだブロックには `auto_joined`（繋いだ数）が付き、画面で「⛓ 自動」と出る。
# 手で指定した結合（joins）と同じ扱いで、生textの段階で繋いでから句点で切り直す。
# ⚠️ 規則なので外れることはある。外したいときは `auto_join` を切る（文書全体）。

SENTENCE_END = "。．.!?！？」』）)]」〕】”\"'"


def _continues(prev: dict, g: dict) -> bool:
    """ブロック prev の文が、ブロック g の頭へ続いているとみなせるか。"""
    if prev.get("table") is not None or g.get("table") is not None:
        return False
    if prev["kind"] not in ("本文", "小") or g["kind"] != prev["kind"]:
        return False
    if abs(prev["size"] - g["size"]) >= 0.3:
        return False
    text = prev["raw"].rstrip()
    nxt = g["raw"].lstrip()
    if not text or not nxt:
        return False
    if text[-1] in SENTENCE_END or text.endswith(("：", ":")):
        return False
    size = prev["size"] or 1.0
    pl = prev["lines"]
    if len(pl) < 2:
        return False                                    # 1行だけ＝右端が分からない（見出し・ラベル・箇条書き）
    right = max(l["x1"] for l in pl)
    if pl[-1]["x1"] < right - size * 1.5:               # 最終行が短い＝そこで終わっている
        return False
    # 「文章のブロック」である証拠：句点を含む（2行のラベル「新規開発製品に対する／環境配慮設計適用率」を
    # 繋がないため。ラベルは2行とも同じくらいの幅なので、右端の条件だけでは通ってしまう）
    if not any(c in text for c in "。．！？") and len(pl) < 3:
        return False
    gl = g["lines"]
    if len(gl) >= 2 and gl[0]["x0"] - min(l["x0"] for l in gl) > size * 0.5:
        return False                                    # 次の頭が字下げ＝新しい段落
    # 次も文章であること：句点を含むか、文の途中でしか現れない文字で始まる。
    # 箇条書きの頭（●■・①など）で始まるブロックは新しい項目なので繋がない
    if nxt[0] in "●■◆▲・※①②③④⑤⑥⑦⑧⑨⑩" or nxt.startswith(("（1）", "(1)", "1.", "1．")):
        return False
    if not any(c in nxt for c in "。．！？") and nxt[0] not in "、」』）)]〕】":
        return False
    return True


def auto_join_groups(groups: list[dict], st: Settings) -> list[dict]:
    """続いている文を持つ隣り合うブロックを繋ぐ（`apply_joins` の後に呼ぶ）。"""
    if not st.auto_join or len(groups) < 2:
        return groups
    out: list[dict] = []
    for g in groups:
        # 繋ぐ相手＝直前のブロック。ただし直前が図解のラベル（小・極小）なら、その手前の
        # 本文まで遡る（段の末尾の本文 → 図 → 次の段の頭の本文、という並びが多いため）。
        # 遡る途中に 本文・大・表 があればそこで止める（別の文章をまたいでは繋がない）
        target = None
        if out and g["kind"] == "本文":
            for p in reversed(out):
                if p["kind"] == "本文":
                    target = p
                    break
                if p["kind"] not in ("小", "極小"):
                    break
        elif out:
            target = out[-1]
        if target is not None and _continues(target, g):
            target["raw"] += g["raw"]
            target["lines"] += g["lines"]
            target["parts"] += g["parts"]
            target["part_boxes"] += g["part_boxes"]
            target["auto_joined"] = target.get("auto_joined", 0) + 1
            continue
        out.append(g)
    return out


def analyze_page(page, st: Settings, body: float, pageno: int,
                 ex: "Excluder | None" = None,
                 ko: "KindOverride | None" = None) -> dict:
    """1ページを解析して、行・グループ・単位を座標付きで返す。

    UI はこの戻り値だけで画面を描ける（＝画面用に別の処理を書かない）。
    手で除外した単位は**消さずに `excluded` 印を付けて返す**。画面で戻せるようにするため。
    """
    ex = ex if ex is not None else st.excluder()
    ko = ko if ko is not None else st.kind_override()
    lines = collect_lines(page, st, repeated_lines(page.parent, st))
    # 横に割れた行を繋ぎ直してから、縦に連続する行をまとめる。
    # 先に横を直しておかないと、文の途中に句点が補われて文が割れる
    live = merge_row_fragments([ln for ln in lines if ln["dropped"] is None], st)
    section_min = st.section_min_pt if st.section_min_pt is not None else body + 3.0

    # 表の中の行は、④の段落化ではなく「表の1行＝1ブロック」に組み直す（→ 上の「表」の説明）。
    # 表に入らなかった行だけを ④ に渡す
    tables = find_page_tables(page, st, pageno)
    table_rows, live = table_row_groups(tables, live)
    # 表の行の分割は、行の組み直しの直後に適用する（セル群の癒着 → apply_table_splits）
    table_rows, used_tbl_splits = apply_table_splits(table_rows, st, pageno)

    # 手で指定したブロックの分割は、group_lines の直後（＝結合より前）に適用する。
    # ルールの鍵が「結合前の生text」であることを保つため（→ apply_splits）
    line_groups, unused_splits = apply_splits(list(group_lines(live, st)), st, pageno)
    # 同じ splits のルールを通常ブロックと表の行の両方の経路で探すので、
    # 「未適用」は**どちらにも当たらなかったもの**だけにする
    unused_splits = [r for r in unused_splits
                     if not any(r is u for u in used_tbl_splits)]

    groups = []
    for g in line_groups:
        # ブロックのサイズ＝行の代表サイズの最頻値（英数字だけの行が1行目でも本文扱いになるように）
        size = collections.Counter(ln["size"] for ln in g).most_common(1)[0][0]
        # ⚠️ strip しない。段をまたぐ結合で `「Prosperity ` の末尾の空白が要るため
        raw = "".join(ln["text"] for ln in g)
        auto = kind_of(size, body, st)
        # ⚠️ 種別の指定は**結合前のブロック**を名指しする（キーは後の `parts[0]`）。
        #    だから照合に使う座標も、ここで作っている結合前のブロックの左上でなければならない
        pre = [min(ln["x0"] for ln in g), min(ln["y0"] for ln in g)]
        forced = ko.get(raw, pageno, pre)     # 手で直した種別があればそちらを使う
        groups.append({
            "kind": forced or auto,
            "auto_kind": auto,                # 自動判定。UIで「自動に戻す」を出すのに使う
            "forced_kind": forced,
            "size": size,
            # text も持たせる（2026-08-26）。語ハイライトの「どの行の出現か」の照合に使う。
            # 通常ブロックでは raw ＝ この並びの text の連結（＝行頭までの文字数が正確に出る）
            "lines": [{k: ln[k] for k in ("x0", "y0", "x1", "y1", "text")} for ln in g],
            "raw": raw,
        })
    for tr in table_rows:
        sizes = collections.Counter(ln["size"] for ln in tr["lines"])
        size = sizes.most_common(1)[0][0]
        tb = tables[tr["table"]]["bbox"]
        # 位置の鍵は bbox の左上と揃える（下の span_x で左端は表の左端になる）
        pre = [tb[0], min(ln["y0"] for ln in tr["lines"])]
        forced = ko.get(tr["raw"], pageno, pre)
        groups.append({
            "kind": forced or "表",
            "auto_kind": "表",
            "forced_kind": forced,
            "size": size,
            # ⚠️ 表の raw は列の組み直しで作られるので、行の text の連結とは一致しない
            #    （語ハイライトの照合は raw.find で行う。→ ui/app.py の `/hits`）
            "lines": [{k: ln[k] for k in ("x0", "y0", "x1", "y1", "text")} for ln in tr["lines"]],
            "raw": tr["raw"],
            "table": tr["table"], "row": tr["row"],
            # 🔴 並べ替えの鍵は**表の左端**にする。行ごとに最初のセルが空だと左端がばらつき、
            #    列→上から の並べ替えで同じ表の行が別の列に振り分けられて順序が崩れる
            "span_x": (tb[0], tb[2]),
        })

    def _bbox(g):
        b = [min(l["x0"] for l in g["lines"]), min(l["y0"] for l in g["lines"]),
             max(l["x1"] for l in g["lines"]), max(l["y1"] for l in g["lines"])]
        if g.get("span_x"):
            b[0], b[2] = g["span_x"]
        return b

    # ⚠️ 二重描画の除去 → 自動の並べ替え → 手の並べ替え → 結合、の順。
    #    結合は「隣り合うブロック」を繋ぐ操作なので、先に順序を確定させないと隣が変わる
    for g in groups:
        g["bbox"] = _bbox(g)
    groups = drop_duplicate_blocks(groups)
    if st.order != "pdf":
        groups = reading_order(groups, page.rect.width)
    groups = apply_manual_order(groups, st, pageno)
    groups, unused_joins = apply_joins(groups, st, pageno)
    groups = auto_join_groups(groups, st)

    for gid, g in enumerate(groups):
        text = g["raw"].strip()
        # 🔴 **bbox を先に更新する。** 結合したブロックの bbox は「1つ目のパーツのもの」の
        #    ままなので、ここで全パーツを含む矩形に直してから照合に使う。
        #    直さずに ex.hit へ渡すと、**画面が見せている枠と、除外の照合に使う枠がズレる**
        #    （結合したブロックの除外を戻せなくなる）。
        g["bbox"] = _bbox(g)
        # ⚠️ pt と位置も渡す。同じページに同じ文言・同じptのブロックが複数あるため
        #    （→「ブロックを1つだけ名指しするための鍵」）
        units = [{"text": t, "excluded": ex.hit(t, pageno, g["size"], g["bbox"])}
                 for t in to_units(g["raw"], st)]
        g.update({
            "gid": gid,
            "units": units,
            # 全単位が除外されたグループ＝ページ画像でも赤枠にして「捨てた」と見せる。
            # ⚠️ 一部だけ除外された場合は、どの行が該当するかまでは特定できない
            #    （1グループの行が句点で複数の単位に割れるため）。別の印を付けて区別する。
            "all_excluded": bool(units) and all(u["excluded"] for u in units),
            "some_excluded": any(u["excluded"] for u in units),
            # セクション見出しの候補か（実際に採用するかは通し処理側が決める）
            # ⚠️ 手で `大` にしたものは pt の条件を免除する。ptで判定を外したブロックを
            #    直しているのに、同じptの条件でもう一度落とすのでは直したことにならない
            "is_section": (g["kind"] == "大"
                           and (g["forced_kind"] == "大" or g["size"] >= section_min)
                           and 0 < len(text) <= st.section_max_len),
        })
    return {"width": page.rect.width, "height": page.rect.height,
            "lines": lines, "groups": groups,
            # 検出した表（画面で枠を描くため）。行ブロック側の table 番号と対応する
            "表": tables,
            "表検出オフ": pageno in st.table_off_set(),
            # 当たらなかったルール。画面で警告を出すために返す。
            # 🔴 **黙って効かないのが一番まずい。** パラメータを変えるとブロックの切れ方が
            #    変わり、保存済みのルールが外れる。外れたことに気づけないまま先へ進むと、
            #    「除外したはずのものが出力に残っている」状態で分析することになる。
            "未適用の結合": unused_joins,
            "未適用の分割": unused_splits,
            "未適用の除外": ex.unused_on(pageno)}


# --- 文書まるごと -------------------------------------------------------

def page_label(page, pageno: int) -> str:
    """印刷上のページ番号（PDFのページラベル）。無ければ通し番号。

    ⚠️ D社 2025 は `<FEFF0043>1` のように**生の16進文字列**で返ってくる（UTF-16BE の PDF 文字列を
       PyMuPDF が復号していない）。見た目が壊れるので、ここで復号し、制御文字と BOM を落とす。
    """
    lab = page.get_label() or ""
    m = re.match(r"^<([0-9A-Fa-f]+)>(.*)$", lab)
    if m:
        try:
            lab = bytes.fromhex(m.group(1)).decode("utf-16-be") + m.group(2)
        except Exception:
            pass
    lab = re.sub(r"[\x00-\x1f\x7f\ufeff]", "", lab).strip()
    return lab or str(pageno)


def extract_doc(doc, st: Settings, body: float | None = None) -> list[dict]:
    """文書全体を単位のリストにする。バッチ（pdf2txt.py）が呼ぶ本線。

    セクションは「直近に現れた大見出しを引き継ぐ」方式。
    ⚠️ PDFのしおり（get_toc）が入っていれば正確に取れるが、
       A社 SR 2025 は 0件だったので、この推定に頼るしかない。
       → セクション列は「参考値」であって、正確な章構造ではないことを卒論では明示する。
    """
    if body is None:
        body = st.body_size if st.body_size else detect_body_size(doc)
    # 🔴 2026-08-31 ページ除外（skip_pages）の適用を廃止：分母（総文数・総ページ数）は
    #    **全ページ・全文**で数える＝人の判断を分母から排除する。表紙・目次などにヒットが
    #    出た場合は L2 の unit_excludes（理由コード付き）で単位側から外す
    ex, ko = st.excluder(), st.kind_override()

    rows, section = [], ""
    bid = 0                              # 文書内のブロック通し番号（→ 抽出単位。表の行の復元に使う）
    # ページをまたいで続く文（→「続いている文を自動で繋ぐ」）。
    # 前ページの最後の本文ブロックと、その最後の文が入った rows の添字
    carry: tuple[dict, int] | None = None
    for i, page in enumerate(doc):
        pageno = i + 1                       # 人間が数える番号（1始まり）
        label = page_label(page, pageno)          # 印刷上のページ番号
        res = analyze_page(page, st, body, pageno, ex, ko)
        groups = res["groups"]

        # --- ページまたぎ：前ページ末尾の文の続きが、このページの頭にあるか
        skip_first_unit_of = None
        if carry is not None and st.auto_join:
            head = _head_text_group(groups)
            if head is not None and _continues(carry[0], head):
                first = head["units"][0]
                if not first["excluded"]:
                    prev_row = rows[carry[1]]
                    # 前ページの最後の文は句点を補われているので、それを外して続きを足す
                    prev_row["文"] = prev_row["文"].rstrip("。") + first["text"]
                    prev_row["ページまたぎ"] = True
                    skip_first_unit_of = head
        carry = None

        last_text_g = None
        for g in groups:
            bid += 1                     # ブロック番号（表の1行＝1ブロック。→ extract_units）
            # ⚠️ セクション見出しは、除外されていても引き継ぎには使う。
            #    見出しを消した結果、後続の本文までセクション不明になるのを避けるため。
            if g["is_section"]:
                section = g["raw"].strip()
            for k, u in enumerate(g["units"]):
                if u["excluded"] or (g is skip_first_unit_of and k == 0):
                    continue
                rows.append({
                    "ページ": pageno, "ページ表示": label, "セクション": section,
                    "種別": g["kind"], "pt": g["size"], "文": u["text"],
                    "ブロック": bid,     # ⚠️ CSVの列にはしない（fieldnames に無いので落ちる）
                })
                # 持ち越し候補＝このページで最後の、2行以上ある本文ブロック。
                # ⚠️ 1行の本文（ページ右端のナビ項目・1行の見出し）は候補にしない。
                #    柱の除去で大半は消えるが、残ったときに段落の続きを食わないため
                if g["kind"] == "本文" and len(g["lines"]) >= 2:
                    last_text_g = (g, len(rows) - 1)
        # このページの最後の本文ブロック（の最後の文）を、次ページへの持ち越し候補にする
        if last_text_g is not None and last_text_g[0]["units"] \
                and not last_text_g[0]["units"][-1]["excluded"] \
                and rows[last_text_g[1]]["文"] == last_text_g[0]["units"][-1]["text"]:
            carry = last_text_g
    return rows


def _head_text_group(groups: list[dict]) -> dict | None:
    """ページの頭の本文ブロック。柱（ページ上部の小さい1行）は読み飛ばす。"""
    for g in groups[:4]:
        if not g["units"]:
            continue
        if g["kind"] in ("小", "極小") and len(g["lines"]) == 1 and len(g["raw"]) <= 60:
            continue                         # 柱・「目次に戻る」のような1行（柱の除去で残った分）
        return g if g["kind"] == "本文" else None
    return None


def aggregate_pages(rows: list[dict]) -> list[dict]:
    """文単位の行をページごとにまとめる（`ページ単位.csv`）。

    **`extract_doc` の出力から作る**（PDFを読み直さない）。こうしておけば
    `文単位.csv` と `ページ単位.csv` が食い違うことが原理的に起きない。

    `本文` は文をそのまま連結したもの。文はすべて句点で終わっているので区切り文字は要らない
    （→ `to_units`）。`種別` は、そのページで一番多い文の種別（本文／大／小／極小／表）。
    `セクション` は、そのページの最初の文が持っていたセクション。

    ⚠️ 文が1つも出なかったページ（除外ページ・図だけのページ）は行にならない。
       ページ数とページ単位の数が合わないのはそのため。**KH Coder 用 txt の `<h2>` と同じ順・同じ数**。
    """
    out: dict[int, dict] = {}
    for r in rows:
        s = out.get(r["ページ"])
        if s is None:
            s = out[r["ページ"]] = {
                "ページ": r["ページ"], "ページ表示": r["ページ表示"],
                "セクション": r["セクション"],
                "種別": "", "_kinds": collections.Counter(),
                "文数": 0, "文字数": 0, "本文": "",
            }
        s["_kinds"][r["種別"]] += 1
        s["文数"] += 1
        s["文字数"] += len(r["文"])
        s["本文"] += r["文"]
    for s in out.values():
        s["種別"] = s.pop("_kinds").most_common(1)[0][0]
    return list(out.values())


def kh_text(rows: list[dict], title: str) -> str:
    """KH Coder に読ませるテキスト（1文書ぶん）。

        <h1>A社_2025</h1>
        <h2>p3</h2>
        創業以来、当社は……。      ← 1行1文
        ……

    KH Coder の集計単位は 文 ／ H2（＝ページ）／ H1（＝文書）から選ぶ。
    ⚠️ 見出し（h1/h2）の文字列は KH Coder 3.Alpha.7 以降、語の集計に入らない（旧掲示板 No.3231）。
    ⚠️ `<h2>` はそのページに文があるときだけ出す。`aggregate_pages` の行と同じ順・同じ数になる。
    ⚠️ ページをまたいで繋いだ文は、始まったページの行に入っている（→ extract_doc）。
    """
    out = [f"<h1>{title}</h1>"]
    last = None
    for r in rows:
        if r["ページ"] != last:
            out.append(f"<h2>p{r['ページ']}</h2>")
            last = r["ページ"]
        out.append(r["文"])
    return "\n".join(out) + "\n"


# --- 文脈窓（2026-08-22 夜に追加。補助） -----------------------------------
# 「生成AI」を含む文の**前後N文**を1つの窓にする。文（狭い）とページ（広い）の間を
# **固定幅**で見るためのもの。段落のような「意味のまとまり」は作らない。恣意性は N の1つだけ。
#
# 検索語。⚠️ ここを変えたら卒論にも書く（何を「生成AIの言及」とみなしたか）。画面からも変えられる
KEYWORDS = ["生成AI", "生成 AI", "生成系AI", "ジェネレーティブAI", "ジェネレーティブ AI",
            "Generative AI", "GenAI", "Gen AI", "大規模言語モデル", "LLM", "ChatGPT"]


def keyword_regex(keywords: list[str] | None = None) -> "re.Pattern":
    kws = [k.strip() for k in (keywords or KEYWORDS) if k and k.strip()]
    if not kws:
        kws = list(KEYWORDS)
    # ⚠️ 英字だけの語（LLM・GenAI）は**単語として**当てる。そうしないと「Fulfillment」の中の
    #    "llm" に当たる（U社 2025 p45 で実際に誤検出した）。日本語を含む語はそのまま部分一致
    parts = []
    for k in kws:
        e = re.escape(k)
        if re.fullmatch(r"[A-Za-z0-9 .\-]+", k):
            e = r"(?<![A-Za-z])" + e + r"(?![A-Za-z])"
        parts.append(e)
    return re.compile("|".join(parts), re.I)


def context_windows(rows: list[dict], n: int = 2,
                    keywords: list[str] | None = None) -> list[dict]:
    """1文書ぶんの文（`extract_doc` の出力順）から、検索語を含む文の前後 n 文の窓を作る。

    決めごと：
      ・窓は同じ文書の中だけ。ページはまたぐ（`ページまたぎ` に印）
      ・隣り合うヒットで窓が重なる・接するときは1つにまとめる（同じ文を2回数えないため）
      ・表・極小の文も窓に入る（本線と同じく、まず入れる）
    返す各窓：文頭からの添字 `lo`/`hi`、ヒットした文の添字 `hits`、本文、ページ範囲、ヒット語。
    """
    rx = keyword_regex(keywords)
    hit_idx = [i for i, r in enumerate(rows) if rx.search(r["文"])]
    spans: list[list] = []
    for i in hit_idx:
        lo, hi = max(0, i - n), min(len(rows) - 1, i + n)
        if spans and lo <= spans[-1][1] + 1:
            spans[-1][1] = max(spans[-1][1], hi)
            spans[-1][2].append(i)
        else:
            spans.append([lo, hi, [i]])
    out = []
    for k, (lo, hi, his) in enumerate(spans, 1):
        body = rows[lo:hi + 1]
        text = "".join(r["文"] for r in body)
        pages = sorted({int(r["ページ"]) for r in body})
        words = sorted({m.group(0) for i in his for m in rx.finditer(rows[i]["文"])})
        out.append({
            "窓ID": k, "lo": lo, "hi": hi, "hits": his,
            "ページ": pages[0], "最終ページ": pages[-1],
            "ページまたぎ": len(pages) > 1,
            "ヒット数": len(his), "ヒット語": "／".join(words),
            "文数": len(body), "文字数": len(text),
            "ヒット文": "｜".join(rows[i]["文"] for i in his), "本文": text,
            # 画面用：各文とヒットかどうか
            "文": [{"文": r["文"], "ページ": r["ページ"], "種別": r["種別"],
                    "hit": (lo + j) in his} for j, r in enumerate(body)],
        })
    return out



# --- 抽出単位（L2。2026-08-25 追加） ------------------------------------------
# **生成AI関連語のヒット箇所だけを、構造に応じた単位に組む。**
#
# 位置づけ（→ 検討_2026-08-25_抽出単位方式.md）
# ---------------------------------------------------------------------------
#   L1 機械層   … 全文の 文×ページ（extract_doc）。出現率の分子・分母と章の分布はここで数える
#   L2 抽出層   … ヒット箇所を下の類型規則で単位化。共起・関連語・KWICはここで見る
#   L2' 対照層  … 固定幅の文脈窓（context_windows）。L2 と結果が一致するかの感度分析
#
# 類型規則（閉じたリスト。文書・時点で共通）
# ---------------------------------------------------------------------------
#   本文の文       → その1文（規則「文」）
#   表の行         → ヒットした行ブロック全体（規則「表の行」。ブロック番号で復元する）
#   図解のラベル等（小・極小）→ その1文（規則「ラベル」）。一体のラベルは手で結合できる
#   見出し（大）   → その1行（規則「見出し」）。結合しない
# 手作業は2種類だけ：**足す**（unit_merges）と**外す**（unit_excludes・理由コード必須級）。
# どちらも全件が設定JSONに残り、再生すれば同一の出力になる。
# ⚠️ **RQ1（出現率）は L2 で数えない。** 手作業で件数が動くと率が手続きに依存するため、
#    率は L1 のヒット文数・ヒットページ数で数える。L2 は内容を見る層。

UNIT_RULE_BY_KIND = {"表": "表の行", "大": "見出し", "小": "ラベル", "極小": "ラベル"}


def _find_near_row(rows: list[dict], text: str, anchor: int, span: int = 40) -> int | None:
    """anchor の近くで、文が text に一致する行を探す（近い順）。手作業の結合の照合に使う。

    文言＋近さで照合するのは Excluder と同じ理由（同じ文言が文書内に複数ありうる）。
    パラメータが変わって文言が変われば当たらない＝黙って別の文を繋がない。
    """
    best = None
    for j in range(max(0, anchor - span), min(len(rows), anchor + span + 1)):
        if rows[j]["文"] == text:
            if best is None or abs(j - anchor) < abs(best - anchor):
                best = j
    return best


def extract_units(rows: list[dict], keywords: list[str] | None = None,
                  merges: list[dict] | None = None,
                  excludes: list[dict] | None = None,
                  ctx: int = 2,
                  checks: list[dict] | None = None) -> dict:
    """1文書ぶんの文（extract_doc の出力順）から、抽出単位（L2）を作る。

    返り値 `{"units": [...], "未適用": {...}}`。
    各単位：単位ID・ページ・最終ページ・セクション・種別・規則・手作業・除外理由・
            ヒット数・ヒット語・文数・文字数・テキスト・文（画面用の内訳）・前/後（近傍の文）。
    **除外された単位も捨てずに返す**（監査記録として書き出しにも載せる）。
    """
    rx = keyword_regex(keywords)
    by_block: dict = {}
    for i, r in enumerate(rows):
        by_block.setdefault(r.get("ブロック"), []).append(i)

    hit_set = {i for i, r in enumerate(rows) if rx.search(r["文"])}
    mg_rules = list(merges or [])
    ex_rules = list(excludes or [])
    used_mg: set[int] = set()
    used_ex: set[int] = set()

    units: list[dict] = []
    consumed: set[int] = set()
    for i in sorted(hit_set):
        if i in consumed:
            continue
        r = rows[i]
        rule = UNIT_RULE_BY_KIND.get(r["種別"], "文")
        if rule == "表の行" and r.get("ブロック") is not None:
            idxs = list(by_block.get(r["ブロック"], [i]))
        else:
            idxs = [i]
        consumed.update(idxs)
        u = {"anchor": i, "idxs": idxs, "規則": rule, "結合": None, "除外理由": None}

        # 手作業①：この単位に足された文（照合はヒット文＋ページ）
        # ⚠️ **既に別の単位に属している文は足さない**（2026-09-01。D社 2025 p63〔誌面〕で、
        #    足した文が検索語の追加（AIエージェント）で自分もヒットになり、
        #    「自分の単位」と「足された先の単位」の両方に載って黙って二重カウントされた。
        #    consumed を見ることで、同じ文はどちらか片方＝先に確定した単位だけに属する）
        for k, m in enumerate(mg_rules):
            if int(m.get("page") or 0) == int(r["ページ"]) and (m.get("hit") or "") == r["文"]:
                used_mg.add(k)
                u["結合"] = m.get("reason") or ""
                for t in (m.get("add") or []):
                    j = _find_near_row(rows, t, i)
                    if j is not None and j not in consumed:
                        u["idxs"].append(j)
                        consumed.add(j)
        # 手作業②：抽出から外す
        for k, e in enumerate(ex_rules):
            if int(e.get("page") or 0) == int(r["ページ"]) and (e.get("text") or "") == r["文"]:
                used_ex.add(k)
                u["除外理由"] = e.get("reason") or ""
        units.append(u)

    # 確認モードの「確認済み」印（アンカー＝ヒット文＋ページで照合。抽出結果は変えない）
    check_map = {(int(c.get("page") or 0), c.get("hit") or ""): c
                 for c in (checks or [])}

    out = []
    for no, u in enumerate(units, 1):
        idxs = sorted(set(u["idxs"]))
        body = [rows[j] for j in idxs]
        text = "".join(x["文"] for x in body)
        hits = [j for j in idxs if j in hit_set]
        words = sorted({m.group(0) for j in hits for m in rx.finditer(rows[j]["文"])})
        pages = sorted({int(x["ページ"]) for x in body})
        a = rows[u["anchor"]]
        chk = check_map.get((int(a["ページ"]), a["文"]))
        lo, hi = idxs[0], idxs[-1]
        # pt・ブロックも画面へ渡す。確認モードから L1 の手作業（文の除外＝excluded、
        # ブロックの結合＝joins）を作るときの照合キーになる（→ Excluder / apply_joins）
        near = lambda j: {"i": j, "文": rows[j]["文"], "ページ": rows[j]["ページ"],
                          "種別": rows[j]["種別"], "pt": rows[j].get("pt"),
                          "ブロック": rows[j].get("ブロック")}
        out.append({
            "単位ID": no, "anchor": u["anchor"],
            "ページ": pages[0], "最終ページ": pages[-1],
            "ページ表示": a["ページ表示"], "セクション": a["セクション"],
            "種別": a["種別"], "規則": u["規則"],
            "手作業": ("結合" if u["結合"] is not None else ""),
            "結合理由": u["結合"] or "",
            "採用": "" if u["除外理由"] is None else "除外",
            "除外理由": u["除外理由"] or "",
            "ヒット数": len(hits), "ヒット語": "／".join(words),
            "文数": len(body), "文字数": len(text),
            "ヒット文": "｜".join(rows[j]["文"] for j in hits),
            "テキスト": text,
            "確認": bool(chk), "確認秒": (chk or {}).get("秒"),
            # 画面用：単位の内訳と、足せる近傍の文（前後 ctx 文。単位に入っていないもの）
            "文": [{**near(j), "hit": j in hit_set} for j in idxs],
            "前": [near(j) for j in range(max(0, lo - ctx), lo)],
            "後": [near(j) for j in range(hi + 1, min(len(rows), hi + 1 + ctx))],
        })
    return {"units": out,
            "未適用": {"unit_merges": [mg_rules[k] for k in range(len(mg_rules)) if k not in used_mg],
                       "unit_excludes": [ex_rules[k] for k in range(len(ex_rules)) if k not in used_ex]}}


# 抽出単位CSVの列（全社共通。KH Coder 用の xlsx もこの並び。テキストは最後の列）
UNIT_COLS = ["文書", "企業名", "年度", "群", "単位ID", "ページ", "最終ページ", "ページ表示",
             "セクション", "種別", "規則", "手作業", "結合理由", "採用", "除外理由",
             "ヒット数", "ヒット語", "文数", "文字数", "ヒット文", "テキスト"]


def unit_export_rows(units: list[dict], doc_id: str, company: str, year: str,
                     group: str = "") -> list[dict]:
    """抽出単位を書き出し用の行にする（UNIT_COLS の列だけ。除外も監査記録として含む）。"""
    out = []
    for u in units:
        row = {"文書": doc_id, "企業名": company, "年度": year, "群": group,
               **{k: u[k] for k in UNIT_COLS if k in u}}
        row["単位ID"] = f"{doc_id}-{u['単位ID']}"
        row["採用"] = u["採用"] or "○"
        out.append(row)
    return out


# --- 除外ページの自動候補（2026-08-22 追加） ----------------------------------
# **表紙・目次・章扉・編集方針・対照表・保証報告書を、機械的な手がかりで「候補」に挙げる。**
#
# 60冊を1ページずつ見て回るのは現実的でないので、人の作業を「選ぶ」から「確かめる」に変える。
# ⚠️ 候補は**提案**であって決定ではない。どれも設定JSON（skip_pages）に入るのは人が採用したとき。
#    採用したものには `"auto": true` が付く＝「機械の候補をそのまま採った」と後から分かる。
#
# 手がかりは、ページの**位置**・**文字数**・**大きな文字の文言**だけ（意味は読まない）。
#   表紙       … 1ページ目。最終ページは文字が少なければ裏表紙
#   目次       … 文書の頭のほうで、「目次／CONTENTS／Index」が**そのページで一番大きな文字**か、
#                行の4割以上が数字で終わる（見出し＋ページ番号の羅列）。
#                ⚠️ 文言だけで判定すると、全ページに「Index」ナビがある D社 2025 で p2〜p17 が目次になる
#   章扉       … 文字が少なく（250字未満・12行以下）、本文の1.8倍以上の大きな文字がある
#   編集方針   … 見出しに「編集方針」「報告対象」「本レポートについて」等
#   対照表     … 見出しに「対照表」「内容索引」「ESGデータ」「データ集」等
#   保証報告書 … 見出しに「第三者保証」「独立した第三者」「保証報告書」等
SUGGEST_RULES = [
    ("編集方針", re.compile(r"編集方針|報告(方針|対象|範囲|期間)|(本|この)(レポート|報告書|サイト)について"
                            r"|レポートの(発行|概要|読み方)|Editorial Policy|About (this|the) Report", re.I)),
    ("対照表", re.compile(r"対照表|内容索引|Content Index|SASB|ESGデータ|データ(集|編|一覧)"
                          r"|非財務データ|KPI一覧|Data Section", re.I)),
    ("保証報告書", re.compile(r"第三者保証|独立(した)?第三者|保証報告書|第三者検証|第三者意見"
                              r"|Independent Assurance|Assurance (Statement|Report)", re.I)),
    ("目次", re.compile(r"^(目\s*次|もくじ|CONTENTS?|INDEX)$", re.I)),
]


# 🔴 boundary_scan（境界の診断と自動提案）は 2026-08-31 に座標カットの廃止とともに削除した。
#    v3 の実装と検証は 記録/2026-08-31.md、経緯は git 履歴に残っている。


def suggest_skips(doc, body: float, st: Settings | None = None) -> list[dict]:
    """除外ページの候補。`[{"page", "reason", "why"}]`。`why` は人が確かめるための短い根拠。

    抽出の設定（ヘッダー除去など）には依存させない＝開いた瞬間に出せる軽い処理にする。
    """
    n = len(doc)
    out: list[dict] = []
    seen: set[int] = set()

    def add(pageno, reason, why):
        if pageno in seen:
            return
        seen.add(pageno)
        out.append({"page": pageno, "reason": reason, "why": why})

    for i, page in enumerate(doc):
        pageno = i + 1
        heads, nchar, nline, numtail, biggest = [], 0, 0, 0, 0.0
        top_heads = []                       # そのページで一番大きな文字の行（目次の判定に使う）
        for blk in page.get_text("dict")["blocks"]:
            for ln in blk.get("lines", []):
                text = "".join(sp["text"] for sp in ln["spans"]).strip()
                if not text:
                    continue
                nline += 1
                nchar += len(text)
                size = max(sp["size"] for sp in ln["spans"])
                if size >= body + 3:
                    heads.append(text)
                if size > biggest + 0.5:
                    biggest, top_heads = size, [text]
                elif abs(size - biggest) <= 0.5:
                    top_heads.append(text)
                if re.search(r"\d{1,3}\s*$", text):      # 「…… 12」のような行
                    numtail += 1
        if pageno == 1:
            add(pageno, "表紙", "1ページ目")
            continue
        if pageno == n and nchar < 300:
            add(pageno, "表紙", f"最終ページで文字が少ない（{nchar}字）")
            continue
        joined = "／".join(heads)
        if pageno <= max(10, n * 0.15):
            # ⚠️ 「目次／CONTENTS／Index」の文言は、**そのページで一番大きな文字**のときだけ目次とみなす。
            #    D社 2025 は全ページの右上に 11pt の「Index」ナビがあり、p2〜p17 が全部「目次」に
            #    なっていた（本物の目次は p2 の 22pt「目次」だけ）
            if any(SUGGEST_RULES[3][1].match(h) for h in top_heads):
                add(pageno, "目次", "「目次／CONTENTS」がページで一番大きな見出し")
                continue
            if nline >= 10 and numtail / nline >= 0.4:
                add(pageno, "目次", f"行の{numtail * 100 // nline}%が数字で終わる")
                continue
        for key, rx in SUGGEST_RULES[:3]:
            if rx.search(joined):
                m = rx.search(joined)
                add(pageno, key, f"見出しに「{m.group(0)}」")
                break
        else:
            if nchar < 250 and nline <= 12 and any(
                    max(sp["size"] for sp in ln["spans"]) >= body * 1.8
                    for blk in page.get_text("dict")["blocks"]
                    for ln in blk.get("lines", []) if ln["spans"]):
                add(pageno, "章扉", f"文字が少なく（{nchar}字）大きな見出しだけ")
    return out


def page_summary(doc, st: Settings, body: float) -> list[dict]:
    """ページごとの単位数と代表見出し。UI のページ一覧（除外ページ選び）に使う。"""
    ex, ko = st.excluder(), st.kind_override()
    cands = {s["page"]: s for s in suggest_skips(doc, body, st)}
    out = []
    for i, page in enumerate(doc):
        pageno = i + 1
        res = analyze_page(page, st, body, pageno, ex, ko)
        kinds = collections.Counter()
        head = ""
        for g in res["groups"]:
            for u in g["units"]:
                if not u["excluded"]:
                    kinds[g["kind"]] += 1
            if not head and g["is_section"]:
                head = g["raw"].strip()
        out.append({
            "ページ": pageno,
            "ページ表示": page_label(page, pageno),
            "見出し": head,
            **{k: kinds[k] for k in KINDS},
            "計": sum(kinds.values()),
            "表数": len(res["表"]),
            # 除外の自動候補（→ suggest_skips）。理由キーと根拠。採用するかは人が決める
            "候補": cands.get(pageno, {}).get("reason", ""),
            "候補の根拠": cands.get(pageno, {}).get("why", ""),
        })
    return out
