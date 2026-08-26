# -*- coding: utf-8 -*-
"""
PDF→テキスト化を、原本と見比べながら手で詰めるための画面。

起動:
    python ui/app.py
    → ブラウザで http://127.0.0.1:5000

やること:
    ① PDFを開く（アップロード or 卒研データ\\pdf\\ から選ぶ）
    ② ヘッダー／フッターの境界線をドラッグで決める
    ③ 本文ptを確認する（サイズ分布を見る）
    ④ 除外ページを選ぶ（表紙・目次・章扉など。理由は core.TASKS）
    ⑤ 結合のパラメータを詰める（COL_TOL / LINE_GAP）
    ⑥ 書き出す（文単位.csv ＋ 設定JSON）

⚠️ このファイルには抽出ロジックを書かない。全部 core.py を呼ぶ。
   画面で詰めた設定が、バッチ（pdf2txt.py）でそのまま再現されることを保証するため。
"""
import csv
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

import pymupdf
from flask import Flask, jsonify, request, send_file, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cachekit  # noqa: E402
import core  # noqa: E402

# --- 手元か、公開デモか -------------------------------------------------
#
# 公開デモ（PUBLIC_MODE=1）は、教授に「実際に触って再現してもらう」ためのもの。
# ⚠️ **この画面には認証が無い。** だから公開側は手元のデータに一切触らせない：
#     ・置き場をプロセスごとの一時ディレクトリにする（サーバー再起動で消える）
#     ・アップロードのサイズに上限を置く
#     ・開いたPDFを抱え込みすぎないようにする（無料枠はメモリが小さい）
# 訪問者は**自分でPDFをアップロードして試す**。他社のPDFを同梱して再配布しないための形。
PUBLIC = os.environ.get("PUBLIC_MODE") == "1"

if PUBLIC:
    DATA = Path(tempfile.mkdtemp(prefix="workbench-"))
else:
    DATA = Path(os.environ.get("WORKBENCH_DATA", str(Path.home() / "卒研データ")))

PDF_DIR = DATA / "pdf"
TXT_DIR = DATA / "txt"
CONF_DIR = DATA / "設定"
cachekit.set_data_dir(DATA)      # キャッシュまわりの正は cachekit（バッチと共通）

MAX_UPLOAD_MB = 40
DOC_CACHE_MAX = 2 if PUBLIC else 8

app = Flask(__name__, static_folder="static", static_url_path="/static")
if PUBLIC:
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# 7.5MBのPDFを毎リクエスト開き直すと重いので、開いたものを持っておく
_docs: "OrderedDict[str, tuple[float, pymupdf.Document]]" = OrderedDict()

# 🔴 PyMuPDF はスレッドセーフでない。Flask は並行リクエストをスレッドで捌くので、
#    同じ Document をページ画像のレンダリングと解析が**同時に**触ることがある
#    （確認モードは選択と同時に /page・/hits・/page.jpg を並行で投げる）。
#    実害の実例（2026-08-26）：サーバー起動後の最初の解析だけ行の y0 が約8pt ずれ
#    （フォント計測が化けたとみられる）、その座標が分割ルールの位置キーに保存されて
#    以後どのブロックにも当たらなくなった。
#    → PyMuPDF を触る区間はすべてこのロックの中で行う。再入可（RLock）なので、
#      get_doc を呼ぶ側が先にロックを取っていてもよい。1人で使う道具なので直列化で足りる
_pdf_lock = threading.RLock()


def get_doc(name: str) -> pymupdf.Document:
    path = PDF_DIR / f"{name}.pdf"
    if not path.exists():
        raise FileNotFoundError(name)
    with _pdf_lock:
        mtime = path.stat().st_mtime
        cached = _docs.get(name)
        if cached and cached[0] == mtime:
            _docs.move_to_end(name)
            return cached[1]
        if cached:
            cached[1].close()
        doc = pymupdf.open(path)
        _docs[name] = (mtime, doc)
        # ⚠️ 開いたPDFは数十MB使う。無料枠のメモリは小さいので、古いものから閉じる
        while len(_docs) > DOC_CACHE_MAX:
            _, old = _docs.popitem(last=False)
            old[1].close()
        return doc


def conf_path(name: str) -> Path:
    return CONF_DIR / f"{name}.json"


def file_info(p: Path) -> dict:
    s = p.stat()
    return {
        "name": p.name,
        "path": str(p),
        "更新": datetime.datetime.fromtimestamp(s.st_mtime).strftime("%Y-%m-%d %H:%M"),
        "mb": round(s.st_size / 1024 / 1024, 2),
    }


def backup(p: Path) -> str | None:
    """上書きする前に、今あるファイルを `履歴\\` へ退避する。

    設定を詰める作業は「変えて → 書き出して → KH Coder で見て → また変えて」の繰り返しになる。
    そのたびに前の設定が消えると、**戻れなくなる**。退避先はファイルの更新時刻で名前を付けるので、
    同じ内容を二度取ることはない。
    """
    if not p.exists():
        return None
    hist = p.parent / "履歴"
    hist.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y%m%d-%H%M%S")
    dest = hist / f"{p.stem}_{ts}{p.suffix}"
    if not dest.exists():
        shutil.copy2(p, dest)
    return str(dest)


BAD_CHARS = re.compile(r'[\\/:*?"<>|]')


def out_paths(name: str, label: str = "") -> tuple[Path, Path, Path]:
    """書き出し先。ラベルを付けると別名になる（設定を変えた版を並べて比べるため）。

    返すのは **文単位CSV / ページ単位CSV / KH Coder 用 txt** の3つ（→ core.py「集計単位について」）。

    ⚠️ ラベルは画面から来る文字列なので、区切り文字を必ず弾いてから使う
    （`../` のような形で置き場の外を指させないため）。
    """
    if BAD_CHARS.search(label):
        raise ValueError("目印に使えない文字が入っています")
    suffix = f"_{label}" if label else ""
    return (DATA / f"文単位_{name}{suffix}.csv",
            DATA / f"ページ単位_{name}{suffix}.csv",
            TXT_DIR / f"{name}{suffix}.txt")


def load_settings(name: str) -> core.Settings:
    """文書ごとの設定。無ければ既定値＋表紙・目次の自動候補から始める。

    🔴 「無い文書の始め方」はバッチ（pdf2txt.py）と必ず同じにする。ズレると単位の件数が
    食い違ううえ、キャッシュの署名も合わず毎回解析し直しになる（2026-08-25 に実際に起きた）。
    だから実装は cachekit に1本化してある。
    """
    # ⚠️ ロックの中で：キャッシュが無いとき cachekit が doc を触る（→ _pdf_lock）
    with _pdf_lock:
        return cachekit.load_settings(name, lambda: get_doc(name))[0]


def req_settings(name: str) -> core.Settings:
    """リクエストで渡ってきた設定を使う。無ければ保存済みのもの。"""
    body = request.get_json(silent=True) or {}
    if "settings" in body:
        return core.Settings.from_dict(body["settings"])
    return load_settings(name)


# 描画済みページ画像。スクロールで戻ってきたときに再レンダリングしないためのもの。
_img_cache: "OrderedDict[tuple, bytes]" = OrderedDict()
# ⚠️ サムネイル（zoom 0.2、1枚 10KB 前後）も同じキャッシュに入る。400ページの文書で
#    サムネイルを全部持っても溢れないように、ページ画像80枚ぶんから増やした
IMG_CACHE_MAX = 600


def body_size_of(doc, st: core.Settings, name: str | None = None) -> float:
    """本文ptの自動推定は全ページ走査で重いので、cachekit の候補キャッシュに相乗りする。"""
    if st.body_size:
        return st.body_size
    with _pdf_lock:
        if name is None:
            return core.detect_body_size(doc)
        return cachekit.load_cands(name, lambda: get_doc(name))["body0"]


# 文書全体の解析結果（extract_doc の行）。**設定が同じなら再計算しない。**
# メモリ（直近数冊）→ ディスク（.cache/、全冊。cachekit が正）→ 再計算 の3段。
_rows_cache: "OrderedDict[tuple, list[dict]]" = OrderedDict()
ROWS_CACHE_MAX = 6

CACHE_DIR = cachekit.CACHE_DIR


def doc_rows(name: str, st: core.Settings) -> list[dict]:
    """⚠️ PDFはキャッシュが全部外れたときだけ開く（get_doc は開くだけで数百msかかる）。"""
    mtime = (PDF_DIR / f"{name}.pdf").stat().st_mtime
    sig = cachekit.rows_sig(st)
    key = (name, mtime, sig)
    hit = _rows_cache.get(key)
    if hit is not None:
        _rows_cache.move_to_end(key)
        return hit
    rows = cachekit.read_rows(name, mtime, sig)
    if rows is None:
        with _pdf_lock:
            doc = get_doc(name)
            rows = core.extract_doc(doc, st, body_size_of(doc, st, name))
        cachekit.write_rows(name, mtime, sig, rows)
    _rows_cache[key] = rows
    while len(_rows_cache) > ROWS_CACHE_MAX:
        _rows_cache.popitem(last=False)
    return rows


# --- 検索語（グローバル。2026-08-25） ------------------------------------
# 🔴 検索語は**全文書・全時点で共通**（片方だけ変えると比較が壊れる）。だから文書ごとの
#    設定JSONではなく、1つのファイルに持つ。これも卒論の再現性の材料（表3.3の実体）。
#    pdf2txt.py（バッチ）も同じファイルを読む。無ければ core.KEYWORDS が既定
KW_PATH = CONF_DIR / "検索語.json"


def load_keywords() -> list[str]:
    return cachekit.load_keywords()


@app.get("/api/keywords")
def api_keywords_get():
    return jsonify({"keywords": load_keywords(),
                    "既定": core.KEYWORDS,
                    "保存済み": KW_PATH.exists()})


@app.post("/api/keywords")
def api_keywords_save():
    body = request.get_json(silent=True) or {}
    kws = [k.strip() for k in (body.get("keywords") or []) if k and k.strip()]
    if not kws:
        return jsonify({"error": "検索語が空です"}), 400
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    kept = backup(KW_PATH)
    KW_PATH.write_text(json.dumps(
        {"keywords": kws, "更新": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify({"saved": str(KW_PATH), "退避": kept, "keywords": kws})


# --- 画面 ---------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ブラウザやWindowsが決め打ちで取りに来る場所。static/ に置いてあるものを返す
@app.get("/favicon.ico")
def favicon():
    return send_from_directory(app.static_folder, "favicon.ico")


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(app.static_folder, "manifest.webmanifest")


@app.get("/api/env")
def api_env():
    """手元か公開デモか。画面の断り書きを出し分けるのに使う。"""
    return jsonify({
        "公開モード": PUBLIC,
        "PDF置き場": str(PDF_DIR),
        "上限MB": MAX_UPLOAD_MB if PUBLIC else None,
    })


# --- 文書の一覧・追加 ---------------------------------------------------

@app.get("/api/docs")
def api_docs():
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for p in sorted(PDF_DIR.glob("*.pdf")):
        row = {"name": p.stem,
               "mb": round(p.stat().st_size / 1024 / 1024, 1),
               "設定済み": conf_path(p.stem).exists()}
        # 切り取り（ヘッダー・フッター）の点検を済ませたか。確認モードへ入る前の誘導に使う
        row["点検"] = False
        if row["設定済み"]:
            try:
                cj = json.loads(conf_path(p.stem).read_text(encoding="utf-8"))
                row["点検"] = bool(cj.get("boundary_check"))
            except Exception:
                pass
        # 抽出単位の数（前回解析時のメタ。→ cachekit.write_meta）。
        # 一覧で「ヒットの無い文書は開かなくてよい」と分かるようにするため
        meta_p = CACHE_DIR / f"{p.stem}.meta.json"
        if meta_p.exists():
            try:
                m = json.loads(meta_p.read_text(encoding="utf-8"))
                row["単位数"] = m.get("単位数")
                row["採用数"] = m.get("採用数")
                row["確認数"] = m.get("確認数")
                # PDFか設定が変わっていたら「要再解析」（判定は先頭読みだけ＝軽い）
                row["古い"] = cachekit.cache_state(p.stem) != "ok"
            except Exception:
                pass
        out.append(row)
    return jsonify(out)


@app.post("/api/upload")
def api_upload():
    """アップロードされたPDFを 卒研データ\\pdf\\ に置く。

    ファイル名は「企業名_年度.pdf」に揃える。この名前がそのまま
    KH Coder の外部変数（企業名・年度）になるため。
    """
    f = request.files.get("file")
    company = (request.form.get("company") or "").strip()
    year = (request.form.get("year") or "").strip()
    if not f:
        return jsonify({"error": "ファイルがありません"}), 400
    if not company or not re.fullmatch(r"\d{4}", year):
        return jsonify({"error": "企業名と、4桁の年度を入れてください"}), 400
    if re.search(r'[\\/:*?"<>|]', company):
        return jsonify({"error": "企業名に使えない文字が入っています"}), 400

    PDF_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{company}_{year}"
    dest = PDF_DIR / f"{name}.pdf"
    if dest.exists():
        return jsonify({"error": f"{dest.name} は既にあります"}), 409
    f.save(dest)
    try:
        with _pdf_lock:
            pymupdf.open(dest).close()
    except Exception as e:                      # PDFとして開けないものは残さない
        dest.unlink(missing_ok=True)
        return jsonify({"error": f"PDFとして開けませんでした: {e}"}), 400
    return jsonify({"name": name})


# --- 文書の情報 ---------------------------------------------------------

_hist_cache: dict[str, tuple[float, list]] = {}      # name -> (mtime, サイズ分布)


@app.get("/api/doc/<name>/info")
def api_info(name):
    with _pdf_lock:            # サイズ分布・寸法列挙で doc を全ページ触る（→ _pdf_lock）
        return _info(name)


def _info(name):
    doc = get_doc(name)
    st = load_settings(name)
    mtime = (PDF_DIR / f"{name}.pdf").stat().st_mtime
    hit = _hist_cache.get(name)
    if hit and hit[0] == mtime:
        hist = hit[1]
    else:
        hist = core.size_histogram(doc)      # 全ページ走査。2回目からはキャッシュ
        _hist_cache[name] = (mtime, hist)
    total = sum(n for _, n in hist) or 1
    # 除外ページの自動候補と推定本文pt（→ cachekit.load_cands。ディスクにキャッシュ）。
    # 🔴 **設定JSONがまだ無い文書では、表紙・目次の候補をそのまま除外に入れた状態で始める**
    #    （2026-08-22）。この2つは「外すのが既定」の手順で、60冊を毎回手で外すのは作業の無駄。
    #    `auto: true` を付けて残すので、「機械の候補をそのまま採った」ことが記録に残る。
    #    章扉・編集方針・対照表・保証報告書は「見て判断する」手順なので、候補として出すだけ。
    #    （load_settings が同じ候補を除外に入れて返す）
    cd = cachekit.load_cands(name, lambda: doc)
    body, cands = cd["body0"], cd["cands"]
    return jsonify({
        "name": name,
        "候補": cands,
        "表の方式": core.TABLE_STRATEGIES,
        "ページ数": len(doc),
        # 画像を読む前に高さを確保するため、先に全ページの寸法とラベルを渡す
        # （連続スクロール表示で、スクロールバーの長さを最初から正しくするのに要る）
        "ページ": [{"w": round(p.rect.width, 1), "h": round(p.rect.height, 1),
                    "label": core.page_label(p, i + 1)}
                   for i, p in enumerate(doc)],
        "推定本文pt": body,                      # 自動推定値（キャッシュ済み）
        "しおり件数": len(doc.get_toc()),        # 0 なら章構造は大見出しから推定するしかない
        "サイズ分布": [{"pt": pt, "文字数": n, "割合": round(n / total * 100, 1)}
                       for pt, n in hist[:14]],
        "設定": st.to_dict(),
        "既定": core.DEFAULTS,
        # 手順の定義はサーバー側（core.py）が正。画面に同じリストを二重に持たせない
        "手順": core.TASKS,
        "手順の状態": core.TASK_STATES,
        "操作の理由": core.OP_REASONS,
        "設定済み": conf_path(name).exists(),
        "検索語": load_keywords(),               # 文脈窓・抽出の既定の検索語（→ 設定\検索語.json）
    })


@app.get("/api/doc/<name>/page/<int:pageno>.jpg")
def api_page_jpg(name, pageno):
    """ページ画像。pageno は1始まり（人間が数える番号に揃える）。

    ⚠️ **画像は設定に依存しない**（枠は画面側でHTMLとして重ねている）。
       だからブラウザにキャッシュさせてよい。以前 no-store を付けていたせいで、
       スクロールで戻るたびに再レンダリングが走っていた。
    JPEGにしているのは、この用途（原本を目で確かめる）では可逆である必要がないため。
    """
    doc = get_doc(name)
    zoom = round(float(request.args.get("zoom", 1.2)), 2)
    mtime = (PDF_DIR / f"{name}.pdf").stat().st_mtime
    key = (name, pageno, zoom, mtime)

    data = _img_cache.get(key)
    if data is None:
        with _pdf_lock:        # レンダリングと解析を同じ doc で同時に走らせない
            pix = doc[pageno - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        data = pix.tobytes("jpg", jpg_quality=72)
        _img_cache[key] = data
        while len(_img_cache) > IMG_CACHE_MAX:
            _img_cache.popitem(last=False)
    else:
        _img_cache.move_to_end(key)

    return app.response_class(data, mimetype="image/jpeg", headers={
        "Cache-Control": "public, max-age=86400",
        "ETag": f'"{pageno}-{zoom}-{int(mtime)}"',
    })


@app.post("/api/doc/<name>/page/<int:pageno>")
def api_page(name, pageno):
    """設定を渡すと、そのページの行・グループ・単位が座標付きで返る。

    画面はこの戻り値だけで描く。**画面用の別処理は書かない**（バッチとのズレを防ぐため）。
    """
    doc = get_doc(name)
    st = req_settings(name)
    with _pdf_lock:
        body = body_size_of(doc, st, name)
        page = doc[pageno - 1]
        res = core.analyze_page(page, st, body, pageno)
        res.update({
            "ページ": pageno,
            "ページ表示": core.page_label(page, pageno),
            "本文pt": body,
            "除外ページ": pageno in st.skip_set(),
        })
    return jsonify(res)


# --- 検索語の矩形（確認モード用。2026-08-26 追加） ----------------------
# **「ブロックの枠は広すぎて、どの語に当たったのか分からない」という指摘への答え。**
# ページ上の検索語の出現位置（語そのものの矩形）を返し、画面でピンポイントに光らせる。
#
# ⚠️ `page.search_for` は**部分一致**（大文字小文字は区別しない）。英字だけの語は
#    keyword_regex と同じ境界規則で確かめないと、「Fulfillment」の中の "llm" に当たる
#    （U社 2025 p45 で実際に誤検出した語）。矩形の左右を少し広げて周囲の文字ごと取り出し、
#    同じ正規表現で当たるかを見る。
# ⚠️ 設定に依存しない（語とPDFだけで決まる）ので、キャッシュの鍵も (文書, ページ, 語) だけ。
_hits_cache: "OrderedDict[tuple, list]" = OrderedDict()
HITS_CACHE_MAX = 400


def _word_rects(page, word: str) -> list[list[float]]:
    rects = page.search_for(word)
    if not re.fullmatch(r"[A-Za-z0-9 .\-]+", word):
        return [[r.x0, r.y0, r.x1, r.y1] for r in rects]
    rx = re.compile(r"(?<![A-Za-z])" + re.escape(word) + r"(?![A-Za-z])", re.I)
    out = []
    for r in rects:
        pad = r.height * 0.9          # だいたい1文字ぶん。隣に英字が続いていれば拾える
        around = page.get_textbox(pymupdf.Rect(r.x0 - pad, r.y0, r.x1 + pad, r.y1))
        if rx.search(around or word):
            out.append([r.x0, r.y0, r.x1, r.y1])
    return out


def _seg_sentence(raw: str, idx: int) -> str:
    """raw（整形前の生テキスト）の位置 idx を含む句点区切りの片を、文の形にして返す。

    `core.clean_text` は句点を作らず・消さず・またがないので、生テキストの句点位置で
    切ってから片を整形しても、`to_units` が作る文と同じものになる（→ clean_text の説明）。
    """
    start = raw.rfind("。", 0, idx) + 1
    end = raw.find("。", idx)
    return core.clean_text(raw[start: end if end >= 0 else len(raw)]).strip() + "。"


def _word_rx(word: str) -> "re.Pattern":
    e = re.escape(word)
    if re.fullmatch(r"[A-Za-z0-9 .\-]+", word):
        e = r"(?<![A-Za-z])" + e + r"(?![A-Za-z])"
    return re.compile(e, re.I)


def _hit_in_texts(h: dict, groups: list[dict], texts: set) -> bool:
    """この矩形の出現は、いま見ている単位の文（texts）のものか。

    同じブロックに**別の単位のヒット文**があると、ブロックの枠だけの判定では
    隣の単位の同じ語まで光ってしまう（D社 2025 p104：ブロック2789に単位15と16）。
    → 矩形の中心 → ブロック → 行 → 生テキスト内の文字位置 → 句点区切りで**文へ帰属**させる。
    """
    x0, y0, x1, y1 = h["rect"]
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    g = next((g for g in groups
              if g["bbox"][0] - 3 <= cx <= g["bbox"][2] + 3
              and g["bbox"][1] - 3 <= cy <= g["bbox"][3] + 3), None)
    if g is None:
        return False
    raw = g["raw"]
    has_unit = any(u["text"] in texts for u in g.get("units", []))
    # どの行の出現か。通常ブロックは raw ＝ 行textの連結なので、行頭の文字位置が正確に出る。
    # 表の行は raw が列の組み直しで作られるので raw.find で照合する（→ analyze_page）
    line, off, pos = None, -1, 0
    for ln in g["lines"]:
        if line is None and ln["y0"] - 1 <= cy <= ln["y1"] + 1 \
                and ln["x0"] - 3 <= cx <= ln["x1"] + 3:
            line = ln
            off = raw.find(ln["text"]) if g.get("table") is not None else pos
        pos += len(ln["text"])
    if line is None or off < 0:
        return has_unit                # 行が特定できないときは、ブロックの判定まで戻す
    rx = _word_rx(h["語"])
    occs = [m.start() for m in rx.finditer(line["text"])]
    if occs:
        # 同じ行に同じ語が複数あるときは、矩形のx位置から一番近い出現を選ぶ（和文はほぼ等幅）
        width = max(1e-6, line["x1"] - line["x0"])
        est = (cx - line["x0"]) / width * len(line["text"])
        i = min(occs, key=lambda o: abs(o + len(h["語"]) / 2 - est))
        sents = {_seg_sentence(raw, off + i)}
    else:
        # 行の中に語が見つからない＝折り返し等。行の両端が入る文で判定する
        sents = {_seg_sentence(raw, off),
                 _seg_sentence(raw, off + max(0, len(line["text"]) - 1))}
    if sents & texts:
        return True
    # ページをまたいで繋がれた文は、単位側が連結済みで一致しない。断片の前方・後方一致で拾う
    for sp in {s.rstrip("。") for s in sents}:
        if sp and any(t.rstrip("。").startswith(sp) or t.rstrip("。").endswith(sp)
                      for t in texts):
            return True
    return False


# --- 表検出キャッシュの温め（2026-08-26） -------------------------------
# L1編集（文の除外・結合など）の再解析は、表検出が時間の9割を占める（→ core._table_cache）。
# 行キャッシュがディスクにあるうちは表検出が一度も走っていないので、**最初の編集だけ**約6秒かかる。
# → 確認モードで文書のカードを開いた時点で、裏のスレッドで温めておく（読んでいる数秒の間に終わる）。
_primed: set[tuple] = set()
_prime_lock = threading.Lock()


@app.post("/api/doc/<name>/prime")
def api_prime(name):
    """この文書の表検出キャッシュを裏で温める。何度呼んでも1回しか走らない。

    ⚠️ PyMuPDF はスレッドセーフでないので、リクエストと同じ Document は触らず
       **専用に開き直す**。失敗しても何も壊れない（温まらず、最初の編集が遅いだけ）。
    """
    st = load_settings(name)
    tsig = json.dumps([st.table_strategy, st.tables, st.table_off],
                      ensure_ascii=False, sort_keys=True, default=str)
    key = (name, tsig)
    with _prime_lock:
        if key in _primed:
            return jsonify({"started": False})
        _primed.add(key)
    path = PDF_DIR / f"{name}.pdf"

    def run():
        try:
            # ⚠️ 専用の Document でも、MuPDF の内部状態（フォント計測など）は共有される。
            #    リクエストの解析と同時に走らせない（1ページごとにロックを譲る）
            with _pdf_lock:
                d = pymupdf.open(path)
            for i in range(d.page_count):
                with _pdf_lock:
                    core.find_page_tables(d[i], st, i + 1)
            with _pdf_lock:
                d.close()
        except Exception:
            with _prime_lock:
                _primed.discard(key)     # 温め損ねたら、次の機会にやり直せるように
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"started": True})


@app.post("/api/doc/<name>/page/<int:pageno>/hits")
def api_page_hits(name, pageno):
    """そのページにある検索語の矩形。確認モードが原本の上に語のハイライトを描くのに使う。

    `texts`（いま見ている単位の、このページの文）と `settings` を渡すと、
    **その文に属する出現だけ**に絞って返す（→ `_hit_in_texts`）。無ければ全出現。
    """
    body = request.get_json(silent=True) or {}
    words = sorted({w.strip() for w in (body.get("words") or []) if w and w.strip()})
    doc = get_doc(name)
    page = doc[pageno - 1]
    mtime = (PDF_DIR / f"{name}.pdf").stat().st_mtime
    key = (name, pageno, tuple(words), mtime)
    hits = _hits_cache.get(key)
    if hits is None:
        with _pdf_lock:
            hits = [{"語": w, "rect": r} for w in words for r in _word_rects(page, w)]
        _hits_cache[key] = hits
        while len(_hits_cache) > HITS_CACHE_MAX:
            _hits_cache.popitem(last=False)
    else:
        _hits_cache.move_to_end(key)

    texts = {t for t in (body.get("texts") or []) if t}
    if texts:
        st = req_settings(name)
        with _pdf_lock:
            res = core.analyze_page(page, st, body_size_of(doc, st, name), pageno)
        hits = [h for h in hits if _hit_in_texts(h, res["groups"], texts)]
    return jsonify({"width": page.rect.width, "height": page.rect.height,
                    "ヒット": hits})


# ページ一覧（全ページを解析するので 110ページで 6秒ほど）。同じ設定なら再計算しない。
# ⚠️ これが走っている間は他のページの解析も待たされるので、キャッシュが効くことは体感に直結する
_pages_cache: "OrderedDict[tuple, list]" = OrderedDict()


@app.post("/api/doc/<name>/pages")
def api_pages(name):
    """全ページの単位数と見出し。除外ページを選ぶ画面・記録パネルに使う。"""
    doc = get_doc(name)
    st = req_settings(name)
    mtime = (PDF_DIR / f"{name}.pdf").stat().st_mtime
    key = (name, mtime, json.dumps(st.to_dict(), sort_keys=True, ensure_ascii=False))
    hit = _pages_cache.get(key)
    if hit is None:
        with _pdf_lock:
            hit = core.page_summary(doc, st, body_size_of(doc, st, name))
        _pages_cache[key] = hit
        while len(_pages_cache) > ROWS_CACHE_MAX:
            _pages_cache.popitem(last=False)
    return jsonify(hit)


# --- 設定の保存 ---------------------------------------------------------

@app.post("/api/doc/<name>/settings")
def api_save_settings(name):
    """設定JSONを保存する。**確認は出さず、前の版を `設定\\履歴\\` に退避してから上書きする。**

    設定を詰めている間は何度も保存することになるので、そのたびに確認を出すと
    「毎回OKを押す」動作が身につき、**本当に確認したい書き出しのダイアログまで素通り**する。
    → 確認ではなく、いつでも戻せるようにするほうを選んだ。
    """
    st = req_settings(name)
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    kept = backup(conf_path(name))
    conf_path(name).write_text(
        json.dumps(st.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify({"saved": str(conf_path(name)), "退避": kept})


# --- 書き出し -----------------------------------------------------------

@app.post("/api/doc/<name>/export")
def api_export(name):
    """この1社ぶんを書き出す（確認用）。全社まとめは pdf2txt.py で作る。

    **既にあるファイルは黙って上書きしない。** 設定を変えて書き出し、KH Coder で見て、
    また変えて…を繰り返すので、前の版を潰したかどうかが分からないと比較にならない。
    → 既存があれば 409 で知らせ、画面で「上書き／別名で残す／やめる」を選ばせる。
    """
    body = request.get_json(silent=True) or {}
    label = (body.get("label") or "").strip()
    try:
        csv_p, page_p, txt_p = out_paths(name, label)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    exists = [p for p in (csv_p, page_p, txt_p) if p.exists()]
    if exists and not body.get("overwrite"):
        return jsonify({"error": "同じ名前のファイルが既にあります",
                        "既存": [file_info(p) for p in exists],
                        "候補": csv_p.name}), 409

    st = req_settings(name)
    rows = doc_rows(name, st)
    pages = core.aggregate_pages(rows)   # ⚠️ PDFを読み直さない。2つのCSVを必ず一致させる
    company, year = _parse_name(name)

    # KH Coder 用テキスト：<h1>文書</h1> ／ <h2>pN</h2> ／ 1行1文（→ core.kh_text）
    TXT_DIR.mkdir(parents=True, exist_ok=True)
    txt_p.write_text(core.kh_text(rows, name), encoding="utf-8")

    cols = ["企業名", "年度", "ページ", "ページ表示", "セクション", "種別", "pt", "文"]
    with csv_p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({"企業名": company, "年度": year, **r})

    # ページ単位（1行1ページ。KH Coder 用 txt の <h2> と同じ順・同じ数）
    page_cols = ["企業名", "年度", "ページ", "ページ表示", "セクション",
                 "種別", "文数", "文字数", "本文"]
    with page_p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=page_cols)
        w.writeheader()
        for s in pages:
            w.writerow({"企業名": company, "年度": year, **s})

    # 書き出したら設定も一緒に残す（設定なしのCSVは再現できないため）
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    kept = backup(conf_path(name))
    conf_path(name).write_text(
        json.dumps(st.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    kinds = {k: sum(1 for r in rows if r["種別"] == k) for k in core.KINDS}
    q = f"?label={quote(label)}" if label else ""
    base = f"/api/doc/{quote(name)}/download"
    return jsonify({"csv": str(csv_p), "page": str(page_p), "txt": str(txt_p),
                    "退避": kept,
                    "単位数": len(rows), "内訳": kinds,
                    "ページ単位数": len(pages),
                    "文字数": sum(len(r["文"]) for r in rows),
                    # 公開デモでは置き場が一時ディレクトリなので、パスを見せても意味がない。
                    # 手元でも「CSVをすぐ開きたい」ときに使えるので、常に返す
                    "落とす": {"csv": f"{base}/csv{q}", "page": f"{base}/page{q}",
                               "txt": f"{base}/txt{q}"}})


@app.get("/api/doc/<name>/download/<kind>")
def api_download(name, kind):
    """書き出したファイルをそのままダウンロードさせる。

    公開デモでは置き場がサーバー上の一時ディレクトリなので、**これが唯一の受け取り方**になる。
    """
    try:
        csv_p, page_p, txt_p = out_paths(name, (request.args.get("label") or "").strip())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    p = {"csv": csv_p, "page": page_p, "txt": txt_p}.get(kind)
    if p is None:
        return jsonify({"error": "csv / page / txt のどれかを指定してください"}), 400
    if not p.exists():
        return jsonify({"error": "まだ書き出されていません"}), 404
    return send_file(p, as_attachment=True, download_name=p.name)


# --- 文脈窓（2026-08-22 夜に追加） -------------------------------------------

def _ctx_args(body: dict) -> tuple[int, list[str]]:
    n = int(body.get("n", 2))
    n = max(0, min(n, 8))
    kws = body.get("keywords")
    if isinstance(kws, str):
        kws = [k for k in re.split(r"[,、\n]", kws) if k.strip()]
    return n, (kws or None)


@app.post("/api/doc/<name>/context")
def api_context(name):
    """「生成AI」を含む文の前後N文（文脈窓）。文書全体を解析するので初回は時間がかかる。"""
    st = req_settings(name)
    body = request.get_json(silent=True) or {}
    n, kws = _ctx_args(body)
    rows = doc_rows(name, st)
    wins = core.context_windows(rows, n, kws)
    per_page: dict[int, int] = {}
    for w in wins:
        for i in w["hits"]:
            p = int(rows[i]["ページ"])
            per_page[p] = per_page.get(p, 0) + 1
    return jsonify({"n": n, "検索語": kws or core.KEYWORDS, "文数": len(rows),
                    "ヒット数": sum(w["ヒット数"] for w in wins), "窓": wins,
                    "ページ別": per_page})


@app.post("/api/doc/<name>/context/export")
def api_context_export(name):
    """文脈窓を書き出す：文脈窓_{name}_N2.csv ／ KHCoder_文脈窓_{name}_N2.txt ／ 外部変数_文脈窓_{name}_N2.csv"""
    st = req_settings(name)
    body = request.get_json(silent=True) or {}
    n, kws = _ctx_args(body)
    rows = doc_rows(name, st)
    wins = core.context_windows(rows, n, kws)
    company, year = _parse_name(name)
    suffix = f"_N{n}"
    csv_p = DATA / f"文脈窓_{name}{suffix}.csv"
    txt_p = TXT_DIR / f"文脈窓_{name}{suffix}.txt"
    var_p = DATA / f"外部変数_文脈窓_{name}{suffix}.csv"
    TXT_DIR.mkdir(parents=True, exist_ok=True)
    cols = ["文書", "企業名", "年度", "窓ID", "ページ", "最終ページ", "ページまたぎ",
            "ヒット数", "ヒット語", "文数", "文字数", "ヒット文", "本文", "検索語"]
    kw_str = "／".join(kws or core.KEYWORDS)
    with csv_p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for x in wins:
            w.writerow({"文書": name, "企業名": company, "年度": year, **x,
                        "窓ID": f"{name}-{x['窓ID']}",
                        "ページまたぎ": "○" if x["ページまたぎ"] else "", "検索語": kw_str})
    txt_p.write_text(f"<h1>{name}</h1>\n" + "".join(x["本文"] + "\n" for x in wins), encoding="utf-8")
    with var_p.open("w", encoding="utf-8-sig", newline="") as f:
        vcols = ["文書", "企業名", "年度", "窓ID", "ページ", "最終ページ", "ページまたぎ", "ヒット数", "ヒット語", "文数"]
        w = csv.DictWriter(f, fieldnames=vcols, extrasaction="ignore")
        w.writeheader()
        for x in wins:
            w.writerow({"文書": name, "企業名": company, "年度": year, **x,
                        "窓ID": f"{name}-{x['窓ID']}",
                        "ページまたぎ": "○" if x["ページまたぎ"] else ""})
    base = f"/api/doc/{quote(name)}/context/download?n={n}"
    return jsonify({"csv": str(csv_p), "txt": str(txt_p), "vars": str(var_p),
                    "窓数": len(wins), "ヒット数": sum(x["ヒット数"] for x in wins),
                    "落とす": {"csv": f"{base}&kind=csv", "txt": f"{base}&kind=txt",
                               "vars": f"{base}&kind=vars"}})


@app.get("/api/doc/<name>/context/download")
def api_context_download(name):
    n = max(0, min(int(request.args.get("n", 2)), 8))
    kind = request.args.get("kind", "csv")
    suffix = f"_N{n}"
    p = {"csv": DATA / f"文脈窓_{name}{suffix}.csv",
         "txt": TXT_DIR / f"文脈窓_{name}{suffix}.txt",
         "vars": DATA / f"外部変数_文脈窓_{name}{suffix}.csv"}.get(kind)
    if p is None or not p.exists():
        return jsonify({"error": "まだ書き出されていません"}), 404
    return send_file(p, as_attachment=True, download_name=p.name)


# --- 抽出単位（L2。2026-08-25 追加） -----------------------------------------

def _doc_units(name: str, st: core.Settings, kws: list[str] | None):
    """1文書ぶんの抽出単位（キャッシュ経由）。一覧バッジ用のメタも更新する。"""
    rows = doc_rows(name, st)
    res = core.extract_units(rows, kws or load_keywords(),
                             st.unit_merges, st.unit_excludes,
                             checks=st.unit_checks)
    cachekit.write_meta(name, (PDF_DIR / f"{name}.pdf").stat().st_mtime,
                        cachekit.rows_sig(st), res["units"])
    return rows, res


@app.post("/api/doc/<name>/units")
def api_units(name):
    """生成AI関連語のヒット箇所を、類型規則で単位化して返す（→ core.extract_units）。

    手作業（unit_merges / unit_excludes）と確認印（unit_checks）は
    設定JSONの一部としてリクエストの settings に入って来る。
    """
    st = req_settings(name)
    body = request.get_json(silent=True) or {}
    _, kws = _ctx_args(body)
    rows, res = _doc_units(name, st, kws)
    units = res["units"]
    return jsonify({"検索語": kws or load_keywords(), "文数": len(rows),
                    "単位数": len(units),
                    "採用数": sum(1 for u in units if not u["採用"]),
                    "確認数": sum(1 for u in units if u.get("確認")),
                    "ページ別": _units_per_page(units),
                    "単位": units, "未適用": res["未適用"]})


def _units_per_page(units):
    per: dict[int, int] = {}
    for u in units:
        p = int(u["ページ"])
        per[p] = per.get(p, 0) + 1
    return per


@app.post("/api/doc/<name>/units/export")
def api_units_export(name):
    """抽出単位を書き出す：抽出単位_{name}.csv（全件・監査記録）＋ KHCoder_抽出単位_{name}.xlsx（採用のみ）。

    全社まとめ（抽出単位.csv ／ KHCoder_抽出単位.xlsx）は pdf2txt.py が作る。ここは1社ぶんの確認用。
    """
    st = req_settings(name)
    body = request.get_json(silent=True) or {}
    _, kws = _ctx_args(body)
    _, res = _doc_units(name, st, kws)
    company, year = _parse_name(name)
    recs = core.unit_export_rows(res["units"], name, company, year,
                                 _load_groups().get(name, ""))

    csv_p = DATA / f"抽出単位_{name}.csv"
    xlsx_p = DATA / f"KHCoder_抽出単位_{name}.xlsx"
    with csv_p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=core.UNIT_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(recs)
    try:
        _write_units_xlsx(xlsx_p, [r for r in recs if r["採用"] == "○"])
    except XlsxLocked as e:
        return jsonify({"error": str(e)}), 409

    # 書き出したら設定も残す（手作業の記録＝再現性の材料）
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    kept = backup(conf_path(name))
    conf_path(name).write_text(
        json.dumps(st.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    base = f"/api/doc/{quote(name)}/units/download"
    return jsonify({"csv": str(csv_p), "xlsx": str(xlsx_p), "退避": kept,
                    "単位数": len(recs),
                    "採用数": sum(1 for r in recs if r["採用"] == "○"),
                    "落とす": {"csv": f"{base}?kind=csv", "xlsx": f"{base}?kind=xlsx"}})


class XlsxLocked(Exception):
    pass


def _write_units_xlsx(path, recs):
    """KH Coder に読ませる Excel（1行1単位＝1セル1ケース。テキスト列は「テキスト」）。

    ⚠️ **openpyxl は使わない。** openpyxl 3.1系はワークシートへの参照を絶対パス
    （`/xl/worksheets/sheet1.xml`）で書き、KH Coder の xlsx パーサ（相対パス前提）が
    シートを見つけられずに落ちる（2026-08-25 に実際に落ちた。xlsx.pm line 481）。
    xlsxwriter は Excel と同じ相対パス＋sharedStrings で書くので読める。
    ⚠️ Excel／KH Coder で開いたままの xlsx には書けない（Windows のロック）→ XlsxLocked。
    """
    import xlsxwriter
    try:
        wb = xlsxwriter.Workbook(str(path))
        ws = wb.add_worksheet("抽出単位")
        for j, c in enumerate(core.UNIT_COLS):
            ws.write(0, j, c)
        for i, r in enumerate(recs, 1):
            for j, c in enumerate(core.UNIT_COLS):
                ws.write(i, j, r.get(c, ""))
        wb.close()
    except Exception as e:
        if "Permission denied" in str(e):
            raise XlsxLocked(
                f"{Path(path).name} を書き出せません。Excel か KH Coder で開いたままに"
                "なっていないか確認して、閉じてからもう一度書き出してください。") from e
        raise


@app.get("/api/doc/<name>/units/download")
def api_units_download(name):
    kind = request.args.get("kind", "csv")
    p = {"csv": DATA / f"抽出単位_{name}.csv",
         "xlsx": DATA / f"KHCoder_抽出単位_{name}.xlsx"}.get(kind)
    if p is None or not p.exists():
        return jsonify({"error": "まだ書き出されていません"}), 404
    return send_file(p, as_attachment=True, download_name=p.name)


# --- 全文書横断（確認モード。2026-08-25） ---------------------------------

def _load_groups() -> dict:
    """対象一覧.csv があれば 企業名_年度 → 群 の対応（pdf2txt.py と同じ）。"""
    path = DATA / "対象一覧.csv"
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8-sig", newline="") as f:
            return {f"{r['企業名']}_{r['年度']}": r.get("群", "")
                    for r in csv.DictReader(f)}
    except Exception:
        return {}


@app.get("/api/doc/<name>/conf")
def api_conf(name):
    """保存済みの設定JSONだけを返す（軽量。/info はサイズ分布まで数えるので確認モードには重い）。"""
    if not (PDF_DIR / f"{name}.pdf").exists():
        return jsonify({"error": "無い文書です"}), 404
    return jsonify({"name": name, "設定": load_settings(name).to_dict(),
                    "設定済み": conf_path(name).exists()})


# --- ジョブ（時間のかかる処理はここを通す。2026-08-25 夜） --------------------
#
# 「未解析の文書があると何十秒も無言で待たされる」を無くすための仕組み：
#   POST /api/job {kind, ...} → 別スレッドで処理を始める（実行中は1つだけ）
#   GET  /api/job             → 進捗（%・いま何をしているか）。終わったら結果ごと返す
# kind:
#   warm      … 解析キャッシュ作り（サブプロセスで並列。→ warm_cache.py）
#   units_all … 全文書（または指定の文書）の抽出単位を集める（確認モードの入口）
#   table     … Excel プレビュー用の行と列
#   export_all… 全冊書き出し（抽出単位.csv ／ KHCoder_抽出単位.xlsx ＋分割）
#
# ⚠️ ジョブの状態はプロセス内に持つ。公開デモも gunicorn -w 1（render.yaml）なので成立する。
#    ワーカーを増やすなら、この仕組みを外に出さないといけない

WARM_SCRIPT = Path(__file__).resolve().parent.parent / "warm_cache.py"
# 並列数：extract_doc はGILに縛られるのでプロセスで分ける。1冊 数百MB 使うことがあるので
# コア数いっぱいまでは上げない（16論理コア・空きRAM 5GB の手元実測で 4 が安全圏）
WARM_WORKERS = 1 if PUBLIC else 4
W_WARM = 25          # 進捗の重み：解析1冊 ≒ 読み込み25冊ぶんの時間

_job: dict | None = None
_job_lock = threading.Lock()


def _job_set(**kw):
    with _job_lock:
        if _job is not None:
            _job.update(kw)


def _job_snapshot() -> dict | None:
    with _job_lock:
        if _job is None:
            return None
        d = {k: v for k, v in _job.items() if k != "result"}
        if _job["state"] == "done":
            d["result"] = _job["result"]
        total = d.get("total") or 0
        d["percent"] = round(d.get("done", 0) / total * 100) if total else 0
        return d


def _warm_names(names: list[str], upd) -> dict[str, str]:
    """キャッシュの無い・古い文書をサブプロセスで並列解析する。戻り値は 名前→エラー。"""
    todo = [n for n in names if cachekit.cache_state(n) != "ok"]
    upd(add_total=len(todo) * W_WARM)
    if not todo:
        return {}
    errors: dict[str, str] = {}
    running: set[str] = set()
    done = 0
    lock = threading.Lock()

    def show():
        with lock:
            now = "、".join(sorted(running)) or "…"
        upd(step=f"① 全ページを解析（初回だけ。同時{min(WARM_WORKERS, len(todo))}冊）",
            detail=f"解析 {done}/{len(todo)}冊 ─ 処理中: {now}")

    def one(n):
        with lock:
            running.add(n)
        show()
        env = {**os.environ, "WORKBENCH_DATA": str(DATA), "PYTHONIOENCODING": "utf-8"}
        r = subprocess.run([sys.executable, str(WARM_SCRIPT), n],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env)
        with lock:
            running.discard(n)
        if r.returncode != 0:
            errors[n] = (r.stderr or "").strip().splitlines()[-1:] or ["不明なエラー"]
            errors[n] = errors[n][0]
        return n

    with ThreadPoolExecutor(max_workers=WARM_WORKERS) as ex:
        futs = [ex.submit(one, n) for n in todo]
        for f in as_completed(futs):
            f.result()
            done += 1
            upd(add_done=W_WARM)
            show()
    return errors


def _units_payload(name: str, kws) -> dict:
    """確認モードが使う1文書ぶん（設定＋単位）。"""
    st = load_settings(name)
    _, res = _doc_units(name, st, kws)
    units = res["units"]
    return {"name": name, "設定": st.to_dict(), "単位": units,
            "単位数": len(units),
            "採用数": sum(1 for u in units if not u["採用"]),
            "確認数": sum(1 for u in units if u.get("確認"))}


# 点検結果のキャッシュ。診断は全ページ走査（1冊 数秒）なので、設定が同じなら使い回す
_boundary_cache: dict[tuple, dict] = {}


def _boundary_payload(name: str, kws) -> dict:
    """「切り取りの点検」1文書ぶん：ヘッダー／フッターの診断＋済んでいれば判断の記録。"""
    st = load_settings(name)
    mtime = (PDF_DIR / f"{name}.pdf").stat().st_mtime
    key = (name, mtime, st.header_y, st.footer_margin,
           json.dumps(st.skip_pages, ensure_ascii=False, sort_keys=True, default=str),
           tuple(kws or []))
    hit = _boundary_cache.get(key)
    if hit is None:
        with _pdf_lock:
            hit = core.boundary_scan(get_doc(name), st, kws)
        if len(_boundary_cache) > 300:
            _boundary_cache.clear()
        _boundary_cache[key] = hit
    out = dict(hit)
    out["name"] = name
    out["判断"] = st.boundary_check or None
    return out


def _table_rows(names: list[str], kws, upd, errors: dict | None = None) -> list[dict]:
    groups = _load_groups()
    out = []
    for i, name in enumerate(names, 1):
        upd(step="② 行と列を組み立て", detail=f"{i}/{len(names)}冊：{name}", add_done=1)
        try:
            st = load_settings(name)
            _, res = _doc_units(name, st, kws)
        except Exception as e:
            # 1冊壊れていても全体を止めない（どの冊が読めなかったかは結果に残す）
            if errors is not None:
                errors[name] = f"{type(e).__name__}: {e}"
            continue
        company, year = _parse_name(name)
        out += core.unit_export_rows(res["units"], name, company, year,
                                     groups.get(name, ""))
    return out


def _export_all(group_by, kws, upd, errors: dict | None = None) -> dict:
    """全文書まとめて書き出す（pdf2txt.py と同じもの＋分割）。"""
    names = [p.stem for p in sorted(PDF_DIR.glob("*.pdf"))]
    all_rows = _table_rows(names, kws, upd, errors)

    upd(step="③ ファイルに書き出し", detail="抽出単位.csv（全件＝監査記録）")
    csv_p = DATA / "抽出単位.csv"
    with csv_p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=core.UNIT_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_rows)
    adopted = [r for r in all_rows if r["採用"] == "○"]
    files = [{"path": str(csv_p), "件数": len(all_rows), "中身": "全件（監査記録）"}]

    xlsx_p = DATA / "KHCoder_抽出単位.xlsx"
    upd(detail=xlsx_p.name)
    _write_units_xlsx(xlsx_p, adopted)
    files.append({"path": str(xlsx_p), "件数": len(adopted), "中身": "採用のみ"})

    if group_by:
        vals = sorted({r.get(group_by) or "" for r in adopted})
        for v in vals:
            label = str(v) if str(v).strip() else "未設定"
            if BAD_CHARS.search(label):
                label = BAD_CHARS.sub("_", label)
            gp = DATA / f"KHCoder_抽出単位_{group_by}_{label}.xlsx"
            upd(detail=gp.name)
            sub = [r for r in adopted if (r.get(group_by) or "") == v]
            _write_units_xlsx(gp, sub)
            files.append({"path": str(gp), "件数": len(sub),
                          "中身": f"{group_by}＝{label}"})
    upd(add_done=5)
    return {"files": files, "全件": len(all_rows), "採用": len(adopted)}


def _run_job(kind: str, params: dict):
    def upd(add_total=0, add_done=0, **kw):
        with _job_lock:
            if _job is None:
                return
            _job["total"] += add_total
            _job["done"] += add_done
            _job.update(kw)

    try:
        names = params.get("docs") or [p.stem for p in sorted(PDF_DIR.glob("*.pdf"))]
        names = [n for n in names if (PDF_DIR / f"{n}.pdf").exists()]
        kws = params.get("keywords")

        # どのジョブも、まずキャッシュを揃える（未解析があるときだけ時間がかかる）。
        # ⚠️ 切り取りの点検（boundary）だけは行キャッシュを使わないので温めない（軽く走らせる）
        errors = {} if kind == "boundary" else _warm_names(names, upd)
        result: dict = {}

        if kind == "warm":
            result = {"冊数": len(names)}
        elif kind == "boundary":
            upd(add_total=len(names))
            docs = []
            for i, n in enumerate(names, 1):
                upd(step="切り取りの点検", detail=f"{i}/{len(names)}冊：{n}", add_done=1)
                try:
                    docs.append(_boundary_payload(n, kws or load_keywords()))
                except Exception as e:
                    errors[n] = f"{type(e).__name__}: {e}"
            result = {"docs": docs}
        elif kind == "units_all":
            upd(add_total=len(names))
            docs = []
            for i, n in enumerate(names, 1):
                upd(step="② 抽出単位を読み込み", detail=f"{i}/{len(names)}冊：{n}",
                    add_done=1)
                try:
                    docs.append(_units_payload(n, kws))
                except Exception as e:
                    errors[n] = f"{type(e).__name__}: {e}"
            # 理由の定義（core.OP_REASONS）も同梱する。確認モードは文書を開かずに
            # 直行できるので、/conf を経由しないと理由リストが手に入らない
            result = {"docs": docs, "検索語": kws or load_keywords(),
                      "操作の理由": core.OP_REASONS}
        elif kind == "table":
            upd(add_total=len(names))
            rows_out = _table_rows(names, kws, upd, errors)
            result = {"cols": core.UNIT_COLS, "rows": rows_out,
                      "採用数": sum(1 for r in rows_out if r["採用"] == "○")}
        elif kind == "export_all":
            upd(add_total=len(names) + 5)
            result = _export_all(params.get("group_by") or None, kws, upd, errors)
        else:
            raise ValueError(f"知らないジョブ: {kind}")

        if errors:
            result["エラー"] = errors
        _job_set(state="done", result=result, detail="", step="完了")
    except XlsxLocked as e:
        _job_set(state="error", error=str(e))
    except Exception as e:
        _job_set(state="error", error=f"{type(e).__name__}: {e}")


@app.post("/api/job")
def api_job_start():
    global _job
    body = request.get_json(silent=True) or {}
    kind = body.get("kind")
    if kind not in ("warm", "units_all", "table", "export_all", "boundary"):
        return jsonify({"error": f"kind が不正です: {kind}"}), 400
    if kind == "export_all":
        gb = body.get("group_by")
        if gb and gb not in core.UNIT_COLS:
            return jsonify({"error": f"group_by に使えない列: {gb}"}), 400
    with _job_lock:
        if _job is not None and _job["state"] == "running":
            return jsonify({"error": "別の処理が実行中です。終わるのを待ってください",
                            "kind": _job["kind"]}), 409
        _job = {"kind": kind, "state": "running", "total": 0, "done": 0,
                "step": "準備中", "detail": "", "error": None, "result": None,
                "開始": time.time()}
    threading.Thread(target=_run_job, args=(kind, body), daemon=True).start()
    return jsonify({"started": kind})


@app.get("/api/job")
def api_job_status():
    d = _job_snapshot()
    if d is None:
        return jsonify({"state": "none"})
    return jsonify(d)


def _parse_name(stem: str) -> tuple[str, str]:
    m = re.match(r"^(.+?)_(\d{4})$", stem)
    return (m.group(1), m.group(2)) if m else (stem, "")


PORT = 5000


def already_running(port: int) -> bool:
    """⚠️ Windows では、使用中のポートにもう1つサーバーを立ててしまえる（SO_REUSEADDR）。

    二重起動すると**古いほうがリクエストを受け続ける**ので、コードを直したのに
    画面の挙動が変わらない、という嵌まり方をする（実際に嵌まった）。だから先に確かめる。
    """
    import socket
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def to_log_if_no_console() -> Path | None:
    """🔴 出力先が無いときは、ログファイルに逃がす。

    `pythonw.exe`（コンソールを持たない python）で起動すると、`sys.stdout` が
    使えない状態になる。そこへ `print()` すると**例外が出てプロセスごと落ちる。**
    実際これで `起動.vbs` が「押しても何も起きない」状態になっていた（2026-08-10）。
    しかも**コンソールが無いのでエラーも見えない**、という最悪の組み合わせだった。

    ⚠️ `sys.stdout is None` だけの判定では足りない。壊れたハンドルが入っていて、
       書き込んだ瞬間に落ちる場合もある。→ **実際に書いてみて確かめる。**
    """
    def usable(stream) -> bool:
        try:
            if stream is None:
                return False
            stream.write("")
            stream.flush()
            return True
        except Exception:
            return False

    if usable(sys.stdout) and usable(sys.stderr):
        return None
    log = Path(__file__).resolve().parent / "起動ログ.txt"
    f = open(log, "a", encoding="utf-8", buffering=1)
    f.write(f"\n===== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} 起動 =====\n")
    sys.stdout = sys.stderr = f
    return log


if __name__ == "__main__":
    log_path = to_log_if_no_console()      # ⚠️ 最初の print より前に呼ぶこと
    url = f"http://127.0.0.1:{PORT}/"
    quiet = "--no-browser" in sys.argv

    # 既に立っていれば、そちらを開くだけ。二重に起動しないので、
    # ショートカットを何度押しても安全（スタートアップに入れても事故らない）
    if already_running(PORT):
        print(f"[i] 既に起動しています → {url}")
        if not quiet:
            import webbrowser
            webbrowser.open(url)
        sys.exit(0)

    PDF_DIR.mkdir(parents=True, exist_ok=True)
    print(f"PDF置き場 : {PDF_DIR}")
    print(f"設定置き場: {CONF_DIR}")
    if log_path:
        print(f"ログ      : {log_path}")
    print(f"→ {url}")
    if not quiet:
        import threading
        import webbrowser
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    # ⚠️ host は 127.0.0.1 のまま（明示しておく）。0.0.0.0 にすると同じWi-Fiの
    #    他の端末から丸見えになる。この画面には認証が無いし、企業のPDFを扱う
    app.run(host="127.0.0.1", port=PORT, debug=False)
