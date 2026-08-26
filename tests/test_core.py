# -*- coding: utf-8 -*-
"""core.py の純粋関数のテスト。PDF を使わずに実行できるものだけを対象にする。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core  # noqa: E402


def st(**kw) -> core.Settings:
    return core.Settings.from_dict(kw)


# ---------- clean_text / to_units ----------

def test_to_units_appends_kuten_to_label():
    # 句点で終わらない単位（見出し・ラベル）には句点が補われる
    units = core.to_units("サステナビリティ経営", st())
    assert units == ["サステナビリティ経営。"]


def test_clean_text_keeps_kuten_count():
    # 変換は句点を新しく作らず・消さず・跨がない（「先に整形→後で分割」と
    # 「先に分割→各文を整形」が同じ結果になるための不変条件）
    text = "第一の文です。第二の文です。"
    assert core.clean_text(text).count("。") == text.count("。")


def test_to_units_splits_on_kuten():
    units = core.to_units("最初の文です。次の文です。", st())
    assert units == ["最初の文です。", "次の文です。"]


def test_to_units_min_len_filters_fragments():
    units = core.to_units("ａ。これは十分な長さの文です。", st(min_len=4))
    assert units == ["これは十分な長さの文です。"]


# ---------- keyword_regex ----------

def test_keyword_regex_matches_japanese_keyword():
    rx = core.keyword_regex(["生成AI"])
    assert rx.search("当社は生成AIの活用を進めています。")


def test_keyword_regex_ascii_word_boundary():
    # 英字語は単語境界を確認する（Fulfillment の中の "llm" に当てない）
    rx = core.keyword_regex(["LLM"])
    assert not rx.search("Fulfillment center")
    assert rx.search("国産のLLMを開発する。")


# ---------- Settings ----------

def test_settings_ignores_unknown_keys():
    # 古い設定JSONに未知のキーが残っていても読み込みは落ちない
    s = core.Settings.from_dict({"footer_margin": 10, "obsolete_key": 1})
    assert s.footer_margin == 10


def test_settings_roundtrip():
    s = st(footer_margin=5, table_off=[{"page": 3, "reason": "図解の枠"}])
    s2 = core.Settings.from_dict(s.to_dict())
    assert s2.to_dict() == s.to_dict()


# ---------- Excluder ----------

def test_excluder_matches_text_and_page():
    ex = core.Excluder([{"text": "ロゴ文字", "page": 2}])
    assert ex.hit("ロゴ文字", 2)
    assert not ex.hit("ロゴ文字", 3)
    assert not ex.hit("別の文字", 2)


# ---------- apply_splits ----------

def _line(text, x0, y0):
    return {"text": text, "x0": x0, "y0": y0, "x1": x0 + 100, "y1": y0 + 10, "size": 8.0}


def test_apply_splits_cuts_group_at_line():
    heading = _line("見出しの行", 10, 100)
    body = _line("本文の行です。", 10, 112)
    rule = {"page": 1, "text": "見出しの行本文の行です。", "line": 1, "reason": "見出しの癒着"}
    out, unused = core.apply_splits([[heading, body]], st(splits=[rule]), 1)
    assert [len(g) for g in out] == [1, 1]
    assert unused == []


def test_apply_splits_reports_unused_rules():
    # 当たらなかったルールは黙って無視せず返す（画面が警告を出すため）
    rule = {"page": 1, "text": "存在しないテキスト", "line": 1}
    lines = [[_line("実際の行です。", 10, 100), _line("続きの行です。", 10, 112)]]
    out, unused = core.apply_splits(lines, st(splits=[rule]), 1)
    assert len(out) == 1
    assert unused == [rule]


# ---------- 行レベルの後段（rowsだけで動くもの） ----------

ROWS = [
    {"ページ": 1, "ページ表示": "1", "セクション": "戦略", "種別": "本文", "pt": 8.0,
     "文": "生成AIの活用を進めます。", "ブロック": 1},
    {"ページ": 1, "ページ表示": "1", "セクション": "戦略", "種別": "本文", "pt": 8.0,
     "文": "続きの取り組みです。", "ブロック": 2},
    {"ページ": 2, "ページ表示": "2", "セクション": "環境", "種別": "本文", "pt": 8.0,
     "文": "気候変動への対応です。", "ブロック": 3},
]


def test_aggregate_pages_counts_per_page():
    pages = core.aggregate_pages(ROWS)
    assert [p["ページ"] for p in pages] == [1, 2]
    assert pages[0]["文数"] == 2
    assert pages[0]["本文"] == "生成AIの活用を進めます。続きの取り組みです。"


def test_kh_text_layout():
    text = core.kh_text(ROWS, "サンプル_2026")
    lines = text.strip().splitlines()
    assert lines[0] == "<h1>サンプル_2026</h1>"
    assert "<h2>p1</h2>" in lines and "<h2>p2</h2>" in lines


def test_context_windows_finds_hit_with_context():
    wins = core.context_windows(ROWS, n=1, keywords=["生成AI"])
    assert len(wins) == 1
    assert "生成AI" in wins[0]["ヒット文"]
