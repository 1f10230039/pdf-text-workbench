# -*- coding: utf-8 -*-
"""解析キャッシュの共通処理。画面（ui/app.py）とバッチ（pdf2txt.py・warm_cache.py）の全員がこれを使う。

なぜ1つのモジュールにするか:
    キャッシュの鍵（設定の署名）や「設定JSONが無い文書の始め方」（表紙・目次の自動候補）を
    複数の場所に別々に書くと、片方だけ直したときに署名が食い違って毎回解析し直しになる。
    だから正はここ1か所。

ディスクキャッシュ（DATA/.cache/）:
    {名前}.rows.json  … extract_doc の結果。{"mtime":…, "sig":…, "rows":[…]}
    {名前}.meta.json  … 一覧バッジ用。{"mtime":…, "sig":…, "単位数":…, "採用数":…, "確認数":…}
    {名前}.cands.json … 除外ページの自動候補（suggest_skips）と推定本文pt。全ページ走査を1回で済ます

注意: rows.json の先頭は必ず {"mtime": …, "sig": …, "rows": の順で書く。
      cache_state() がファイル全体を読まずに先頭だけで有効判定するため（1冊数MBある）。
"""
import json
import os
import time
from pathlib import Path

import pymupdf

import core

DATA = Path(os.environ.get("WORKBENCH_DATA", str(Path.home() / "卒研データ")))
PDF_DIR = DATA / "pdf"
CONF_DIR = DATA / "設定"
CACHE_DIR = DATA / ".cache"

# extract_doc の結果を変えないフィールドは署名から外す：
#   ・unit_*      … 抽出単位（L2）の手作業と確認印。入れると「1件確認するたびに全文解析し直し」
#   ・boundary_check・task_states … 人の判断の記録。入れると「問題なし」と記録した
#     だけで全冊が要再解析になる
#   ・skip_pages  … ページ除外は適用しない（分母は全ページ・全文）。記録としては
#     設定JSONに残るが、extract_doc は見ないので署名にも入れない
L2_ONLY_FIELDS = ("unit_merges", "unit_excludes", "unit_checks",
                  "boundary_check", "task_states", "skip_pages")


def set_data_dir(path):
    """公開デモ（PUBLIC_MODE）では置き場が一時ディレクトリになる。app.py が起動時に呼ぶ。"""
    global DATA, PDF_DIR, CONF_DIR, CACHE_DIR
    DATA = Path(path)
    PDF_DIR = DATA / "pdf"
    CONF_DIR = DATA / "設定"
    CACHE_DIR = DATA / ".cache"


def pdf_path(name: str) -> Path:
    return PDF_DIR / f"{name}.pdf"


def conf_path(name: str) -> Path:
    return CONF_DIR / f"{name}.json"


def rows_sig(st: core.Settings) -> str:
    """設定の署名。これと PDF の mtime が一致すれば rows キャッシュは有効。"""
    d = st.to_dict()
    for k in L2_ONLY_FIELDS:
        d.pop(k, None)
    return json.dumps(d, sort_keys=True, ensure_ascii=False)


def load_keywords() -> list[str]:
    """検索語（設定\\検索語.json。無ければ core.KEYWORDS）。

    検索語は全文書・全時点で共通（片方だけ変えると比較が壊れる）。
    """
    try:
        d = json.loads((CONF_DIR / "検索語.json").read_text(encoding="utf-8"))
        kws = [k.strip() for k in d.get("keywords", []) if k and k.strip()]
        if kws:
            return kws
    except Exception:
        pass
    return list(core.KEYWORDS)


# --- 除外ページの自動候補（全ページ走査なので必ずキャッシュを通す） ------------

_cands_memo: dict[str, tuple[float, dict]] = {}


def load_cands(name: str, get_doc=None) -> dict:
    """suggest_skips の全候補と推定本文pt。{"body0": float, "cands": [{"page","reason"},…]}

    get_doc: 計算が要るときだけ呼ばれる「開いたPDFを返す関数」。
    None なら自分で開いて閉じる（呼び出し側が渡した doc は閉じない）。
    """
    p = pdf_path(name)
    mtime = p.stat().st_mtime
    hit = _cands_memo.get(name)
    if hit and hit[0] == mtime:
        return hit[1]
    disk = CACHE_DIR / f"{name}.cands.json"
    if disk.exists():
        try:
            d = json.loads(disk.read_text(encoding="utf-8"))
            if d.get("mtime") == mtime:
                out = {"body0": d["body0"], "cands": d["cands"]}
                _cands_memo[name] = (mtime, out)
                return out
        except Exception:
            pass
    own = get_doc is None
    doc = pymupdf.open(p) if own else get_doc()
    try:
        body0 = core.detect_body_size(doc)
        cands = core.suggest_skips(doc, body0)
    finally:
        if own:
            doc.close()
    out = {"body0": body0, "cands": cands}
    _cands_memo[name] = (mtime, out)
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        disk.write_text(json.dumps({"mtime": mtime, "body0": body0, "cands": cands},
                                   ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return out


def load_settings(name: str, get_doc=None) -> tuple[core.Settings, bool]:
    """文書ごとの設定。無ければ既定値。(設定, 保存済みか) を返す。

    設定JSONが無い文書は既定値そのままで始める。ページ除外を適用しないので、
    表紙・目次の自動候補をあらかじめ入れておく必要がない（→ core.extract_doc）。
    """
    p = conf_path(name)
    if p.exists():
        return core.Settings.from_dict(json.loads(p.read_text(encoding="utf-8"))), True
    return core.Settings(), False


# --- キャッシュの有効判定と読み書き ------------------------------------------

def _rows_head(mtime: float, sig: str) -> bytes:
    """rows.json の先頭にあるはずのバイト列（→ モジュール先頭の注意書き）。"""
    head = json.dumps({"mtime": mtime, "sig": sig}, ensure_ascii=False)
    return (head[:-1] + ', "rows":').encode("utf-8")


def cache_state(name: str) -> str:
    """"ok"＝rows キャッシュが今の設定・PDFと一致 ／ "stale"＝あるが古い ／ "none"＝無い。

    注意: ここは一覧（61冊×毎回）から呼ばれるので、重い処理をしてはいけない
    （設定JSONの読み込みと署名の計算だけ。PDFは開かない）。
    """
    p = pdf_path(name)
    rows_p = CACHE_DIR / f"{name}.rows.json"
    if not p.exists() or not rows_p.exists():
        return "none"
    mtime = p.stat().st_mtime
    try:
        st, _ = load_settings(name)
        sig = rows_sig(st)
        meta_p = CACHE_DIR / f"{name}.meta.json"
        if meta_p.exists():
            m = json.loads(meta_p.read_text(encoding="utf-8"))
            if m.get("mtime") == mtime and m.get("sig") == sig:
                return "ok"
            if "sig" in m:
                return "stale"
        # 古い形式の meta（sig 無し）：rows.json の先頭だけ読んで確かめる
        head = _rows_head(mtime, sig)
        with rows_p.open("rb") as f:
            if f.read(len(head)) == head:
                return "ok"
        return "stale"
    except Exception:
        return "stale"


def read_rows(name: str, mtime: float, sig: str) -> list[dict] | None:
    """有効なら rows を返す。無効・壊れているなら None。"""
    disk = CACHE_DIR / f"{name}.rows.json"
    if not disk.exists():
        return None
    try:
        head = _rows_head(mtime, sig)
        with disk.open("rb") as f:
            if f.read(len(head)) != head:
                return None
        d = json.loads(disk.read_text(encoding="utf-8"))
        return d["rows"]
    except Exception:
        return None


def write_rows(name: str, mtime: float, sig: str, rows: list[dict]):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / f"{name}.rows.json").write_text(json.dumps(
            {"mtime": mtime, "sig": sig, "rows": rows}, ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass


def write_meta(name: str, mtime: float, sig: str, units: list[dict]):
    """一覧のバッジ（単位数・確認数）用。sig を持つので有効判定もこれ1枚で済む。"""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / f"{name}.meta.json").write_text(json.dumps({
            "mtime": mtime, "sig": sig,
            "単位数": len(units),
            "採用数": sum(1 for u in units if not u["採用"]),
            "確認数": sum(1 for u in units if u.get("確認")),
        }, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# --- 1冊ぶんの解析（warm） ---------------------------------------------------

def warm_doc(name: str, keywords: list[str] | None = None) -> dict:
    """1冊を解析してキャッシュ（rows / meta / cands）を作る。有効なら何もしない。

    ui/app.py のジョブがサブプロセス（warm_cache.py）越しに並列で呼ぶほか、
    pdf2txt.py も同じ結果を書く。どこから呼んでも同じキャッシュになることが大事。
    """
    t0 = time.time()
    p = pdf_path(name)
    mtime = p.stat().st_mtime
    doc = None

    def get_doc():
        nonlocal doc
        if doc is None:
            doc = pymupdf.open(p)
        return doc

    try:
        st, _ = load_settings(name, get_doc)
        sig = rows_sig(st)
        rows = read_rows(name, mtime, sig)
        rebuilt = rows is None
        if rebuilt:
            body = st.body_size if st.body_size else load_cands(name, get_doc)["body0"]
            rows = core.extract_doc(get_doc(), st, body)
            write_rows(name, mtime, sig, rows)
        units = core.extract_units(rows, keywords or load_keywords(),
                                   st.unit_merges, st.unit_excludes,
                                   checks=st.unit_checks)["units"]
        write_meta(name, mtime, sig, units)
        return {"name": name, "文数": len(rows), "単位数": len(units),
                "再解析": rebuilt, "秒": round(time.time() - t0, 1)}
    finally:
        if doc is not None:
            doc.close()
