"use strict";
// 画面の状態。設定は常にここが正で、サーバーには毎回まるごと投げる。
// （サーバー側に状態を持たせない＝画面で見えている設定と、書き出される設定が必ず一致する）
const S = {
  name: null, info: null, st: null,
  page: 1, pageMax: 1,
  boxes: {},           // ページ番号 → ページの箱（DOM）
  cache: {},           // ページ番号 → 解析結果（読み込み済みのページだけ）
  pages: null,         // ページ一覧（見出し・種別ごとの単位数）。手順一覧も見る
  sel: null,           // 「理由が未設定」で選んでいるページ（まとめて理由を付ける用）
  selLast: null,       // 直前に触れたページ。Shift+クリックの範囲選択の起点
  taskSig: null,       // 記録パネルを最後に描いたときの中身。無駄な描き直しを避ける
  saved: null,         // 最後に保存した設定のJSON文字列。未保存かどうかの判定に使う
};

const $ = (id) => document.getElementById(id);
/** 線画アイコン（index.html のスプライトを参照）。必ず文字と組で使う → デザイン方針.md §2-3 */
const ICON = (n) => `<svg class="ic" aria-hidden="true"><use href="#i-${n}"/></svg>`;
const api = async (url, body) => {
  const r = await fetch(url, body ? {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  } : {});
  if (!r.ok) {
    // ⚠️ 本文も一緒に投げる。書き出しの「既に同じファイルがあります」(409) は、
    //    どのファイルがいつ書かれたかを画面で見せる必要がある
    const j = await r.json().catch(() => ({}));
    const err = new Error(j.error || r.statusText);
    err.status = r.status;
    err.data = j;
    throw err;
  }
  return r.json();
};

// 設定を入れる欄。id は p_<キー名> で統一してある
const PARAMS = ["header_y", "footer_margin", "size_tol", "tiny_ratio", "join_gap",
                "col_tol", "line_gap", "min_len", "body_size", "section_min_pt",
                "repeat_ratio"];
const BOOL_PARAMS = ["auto_join"];                        // チェックボックス
const SEL_PARAMS = ["table_strategy"];                    // プルダウン

// 種別。core.py の KINDS と同じ並び。⚠️ 片方だけ増やすと件数表示が合わなくなる
const KINDS = ["本文", "大", "小", "極小", "表"];

// ⚠️ 通信の失敗を握り潰さない。以前は api() が投げると unhandled rejection のままで、
//    サーバーを落とした状態で操作しても「何も起きない」画面になっていた。
window.addEventListener("unhandledrejection", (e) => {
  toast("サーバーとのやり取りに失敗しました：" +
        ((e.reason && e.reason.message) || e.reason) +
        "\n（app.py が動いているか確認してください）", "err");
});

// ---------- ① PDFを開く ----------

// この画面の主役は「文書を選んで確認モードに入る」こと。クリック＝選択にして、
// 個別に開く（設定・表・並べ替えを直す）ほうは行末の小さな「開く」に寄せてある。
// 選択はブラウザに残す（作業を中断しても選び直さなくていい）
const SEL = new Set((() => {
  try { return JSON.parse(localStorage.getItem("selDocs") || "[]"); } catch (e) { return []; }
})());

function updateSelUI() {
  try { localStorage.setItem("selDocs", JSON.stringify([...SEL])); } catch (e) { /* 保存できなくても動く */ }
  $("selCount").textContent = SEL.size ? `${SEL.size}冊を選択中` : "未選択（＝全冊が対象）";
  $("auditBtnLabel").textContent = SEL.size ? `ヒットを確認（${SEL.size}冊）` : "ヒットを確認（全冊）";
  $("selNone").disabled = !SEL.size;
}

let DOCS = [];                       // /api/docs の結果。点検ゲートとバッジが見る

async function loadDocs() {
  const docs = await api("/api/docs");
  DOCS = docs;
  updateBdUI();
  for (const n of [...SEL]) if (!docs.some((d) => d.name === n)) SEL.delete(n);
  const ul = $("docList");
  ul.innerHTML = "";
  if (!docs.length) {
    ul.innerHTML = '<li class="hint">まだPDFがありません。右で追加してください。</li>';
    updateSelUI();
    return;
  }
  const last = localStorage.getItem("lastDoc");
  for (const d of docs) {
    const li = document.createElement("li");
    // 抽出単位のバッジ：**ヒットの無い文書は開かなくてよい**と一覧で分かるようにする。
    // ⚠️ 列はグリッドで揃える（→ style.css .doclist li）。バッジを名前の後ろに流すと
    //    行ごとに位置がバラけて読めない、という指摘があった（2026-08-25）
    const n = d["単位数"];
    const hits = n == null ? '<span class="tag dim">未解析</span>'
      : n === 0 ? '<span class="tag zero">ヒットなし</span>'
      : `<span class="tag hit">${d["採用数"]}単位${d["確認数"] ? `・確認${d["確認数"]}` : ""}</span>`;
    li.innerHTML =
      `<span class="selbox">${ICON("check")}</span>` +
      `<span class="dname">${ICON("doc")}<span>${d.name}</span>` +
        (d.name === last ? '<span class="tag last">前回開いた</span>' : "") + "</span>" +
      `<span class="c-hit">${hits}</span>` +
      `<span class="c-tags">` +
        (d["古い"] ? '<span class="tag warn" title="PDFか設定が変わっています。次の解析で作り直されます">要再解析</span>' : "") +
        (d["設定済み"] ? '<span class="tag">設定あり</span>' : "") + "</span>" +
      `<span class="mb">${d.mb} MB</span>` +
      `<button class="ghost mini openbtn" title="この文書だけ開いて直す（設定・表の指定・並べ替え）">開く</button>`;
    li.title = "クリックで選択／解除";
    li.classList.toggle("sel", SEL.has(d.name));
    li.onclick = () => {
      if (SEL.has(d.name)) SEL.delete(d.name); else SEL.add(d.name);
      li.classList.toggle("sel", SEL.has(d.name));
      updateSelUI();
    };
    li.querySelector(".openbtn").onclick = (e) => {
      e.stopPropagation();                             // 開くボタンは選択を変えない
      if (ul.classList.contains("busy")) return;       // 二度押し防止
      li.classList.add("opening");
      ul.classList.add("busy");
      openDoc(d.name).finally(() => { li.classList.remove("opening"); ul.classList.remove("busy"); });
    };
    li.dataset.name = d.name.toLowerCase();
    ul.appendChild(li);
  }
  filterDocs();
  updateSelUI();
}
function filterDocs() {
  const q = ($("docFilter").value || "").trim().toLowerCase();
  for (const li of $("docList").children) li.classList.toggle("hide", !!q && !(li.dataset.name || "").includes(q));
}
$("docFilter").oninput = filterDocs;
$("selAll").onclick = () => {          // 「すべて」＝絞り込みで見えている行だけ（直感に合わせる）
  for (const li of $("docList").children) {
    if (li.classList.contains("hide") || !li.dataset.name) continue;
    SEL.add(li.querySelector(".dname span").textContent);
    li.classList.add("sel");
  }
  updateSelUI();
};
$("selNone").onclick = () => {
  SEL.clear();
  for (const li of $("docList").children) li.classList.remove("sel");
  updateSelUI();
};

$("upBtn").onclick = async () => {
  const f = $("upFile").files[0];
  const msg = $("upMsg");
  msg.className = "msg";
  if (!f) { msg.textContent = "PDFを選んでください"; msg.className = "msg err"; return; }
  const fd = new FormData();
  fd.append("file", f);
  fd.append("company", $("upCompany").value);
  fd.append("year", $("upYear").value);
  msg.textContent = "アップロード中…";
  const r = await fetch("/api/upload", { method: "POST", body: fd });
  const j = await r.json();
  if (!r.ok) { msg.textContent = j.error; msg.className = "msg err"; return; }
  msg.textContent = `${j.name}.pdf を追加しました`;
  msg.className = "msg ok";
  await loadDocs();
  openDoc(j.name);
};

async function openDoc(name) {
  showLoading(`${name} を開いています…`, "全ページの文字サイズと、表紙・目次などの候補を調べています");
  try {
    await _openDoc(name);
  } finally {
    hideLoading();
  }
}

async function _openDoc(name) {
  S.name = name;
  S.ctx = null;
  S.extract = null;                  // 前の文書の抽出一覧を持ち越さない
  S.info = await api(`/api/doc/${encodeURIComponent(name)}/info`);
  localStorage.setItem("lastDoc", name);
  $("ctxKw").value = (S.info["検索語"] || []).join(", ");
  $("ctxBody").innerHTML = CTX_EMPTY;
  $("ctxCount").textContent = "";
  $("ctxExport").disabled = true;
  $("unitBody").innerHTML = UNIT_EMPTY;
  $("unitCount").textContent = "";
  $("unitExport").disabled = true;
  $("unitTable").disabled = true;
  setLoading("画面を組み立てています…");
  S.st = S.info["設定"];
  // 設定JSONがまだ無い文書は「未保存」から始める（保存を促すため）
  S.saved = S.info["設定済み"] ? JSON.stringify(S.st) : null;
  S.pageMax = S.info["ページ数"];
  S.page = 0;                        // 0 にしておく（後の setCurrent(1) を必ず通すため）
  S.pick = null;                     // 前の文書で囲んだ選択を持ち越さない
  $("pane-open").hidden = true;
  $("work").hidden = false;
  $("tools").hidden = false;
  $("backBtn").hidden = false;
  $("pageMax").textContent = S.pageMax;
  $("pageNo").max = S.pageMax;
  $("docInfo").innerHTML = `<b>${name}</b> · ${S.pageMax}ページ`;
  $("docInfo").title = `推定本文 ${S.info["推定本文pt"]}pt ／ しおり ${S.info["しおり件数"]}件` +
    (S.info["しおり件数"] === 0 ? "（章構造は大見出しから推定）" : "");
  $("appTitle").hidden = true;
  fillParams();
  fillReasonPicker();
  drawHist();
  $("pageList").innerHTML = "";
  document.documentElement.style.setProperty("--pagew", $("zoom").value + "px");
  buildPages();
  buildThumbs();
  setCurrent(1);
  S.boxes[1].classList.add("cur");
  syncSaveState();

  // 設定JSONがまだ無い文書は、表紙・目次の候補が除外に入った状態で返ってくる（→ app.py）。
  // 黙って入れると「なぜ除外されているのか」が分からないので、最初に1回だけ知らせる
  const auto = (S.st.skip_pages || []).filter((r) => r.auto);
  const cand = (S.info["候補"] || []).filter((c) => !skipSet().has(c.page));
  if (auto.length || cand.length) {
    toast((auto.length
            ? `表紙と目次（p.${auto.map((r) => r.page).join(", ")}）を除外にしました。\n`
            : "")
        + (cand.length
            ? `ほかに、外してもよさそうなページが ${cand.length}ページあります（章扉・編集方針・対照表・保証報告書）。`
              + "\nこれらはまだ除外していません。左のサムネイルの「？」印か「記録」で原本を見て、外すか決めてください。"
            : ""), "ok");
  }
}

// ---------- 未保存の管理 ----------
// 除外・結合・種別・並べ替えは S.st に溜まるだけで、保存するまでJSONに残らない。
// **初見の人が一番失いやすいのはここ**なので、状態を常に見せ、離脱時に警告する。

const isDirty = () => !!S.name && JSON.stringify(S.st) !== S.saved;

function syncSaveState() {
  const d = isDirty();
  $("saveTop").innerHTML = ICON("save") + (d ? "設定を保存" : "保存済み");
  $("saveTop").classList.toggle("dirty", d);
  $("saveTop").disabled = !d;
}

window.addEventListener("beforeunload", (e) => {
  if (!isDirty()) return;
  e.preventDefault();
  e.returnValue = "";       // 文言はブラウザ側が決める（指定しても表示されない）
});

async function saveSettings() {
  const j = await api(`/api/doc/${encodeURIComponent(S.name)}/settings`, { settings: S.st });
  S.saved = JSON.stringify(S.st);
  syncSaveState();
  // 上書きの確認は出さない代わりに、前の版がどこへ退避されたかを必ず見せる
  toast(`設定を保存しました\n${j.saved}` +
        (j["退避"] ? `\n前の設定は 履歴\\ に残してあります:\n${j["退避"]}` : ""), "ok");
}
$("saveTop").onclick = saveSettings;
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "s" && S.name) {
    e.preventDefault();
    if (isDirty()) saveSettings();
  }
});

$("backBtn").onclick = () => {
  if (isDirty() && !confirm("保存していない設定があります。このまま選び直すと失われます。続けますか？")) return;
  S.name = null;                 // ⚠️ 消し忘れると beforeunload が鳴り続ける
  S.saved = null;
  openTasks(false);
  openCtx(false);
  S.ctx = null;
  S.pages = null;
  S.sel = null;
  S.selLast = null;
  S.selRec = {};
  $("work").hidden = true;
  $("tools").hidden = true;
  $("backBtn").hidden = true;
  $("toast").hidden = true;
  openCfg(false);
  $("thumbList").innerHTML = "";
  if (ioThumb) { ioThumb.disconnect(); ioThumb = null; }
  $("pane-open").hidden = false;
  $("docInfo").textContent = "";
  $("appTitle").hidden = false;
  loadDocs();
};

// ---------- 手動修正の一覧 ----------
// ⚠️ ここは**卒論の付録そのもの**。「自動でやりました」では通らないので、
//    どこを人が直したのかを全件、いつでも出せる形にしておく。
// （以前はバッジを押すと右パネルの除外リストへ飛ぶだけで、除外が0件だと**何も起きなかった**）

// 1件のときに「まとめて」と言わない。日本語として不自然なだけでなく、
// **1件しか選べていないことに気づかない**もとになる
const bulkLabel = (n) => (n === 1 ? "理由を付ける…" : `${n}件にまとめて理由を付ける…`);
const undoLabel = (n, one) => (n === 1 ? one : `まとめて${one}`);

// 選択状態は操作の種類ごとに持つ。⚠️ 添字ではなくルールの実体で持つ
// （理由を付けると並びや件数が変わるので、添字だと指し先がずれる）
S.selRec = {};

/** 選ぶ・まとめて理由を付ける・まとめて取り消す、の共通の一覧。
 *
 * 除外・結合・種別・並べ替えの4つは、**どれも「なぜそうしたか」を残す**という点で同じもの。
 * 作りを1つにまとめてあるのは、片方だけ機能が増えて食い違うのを防ぐため。
 *
 * @param o {op, title, note, rules, label(r), page(r)}
 *          rules は S.st の配列そのもの（書き換えがそのまま設定に効く）
 */
function recordSection(o) {
  const rules = o.rules || [];
  const choices = ((S.info && S.info["操作の理由"]) || {})[o.op] || [];
  const sel = S.selRec[o.op] = new Set([...(S.selRec[o.op] || [])].filter(
    (r) => rules.includes(r)));                  // 消えたルールは選択から外す

  const sec = document.createElement("section");
  sec.className = "mansec";
  // 理由の内訳を見出しに出す。**件数だけでは「乱暴なのか文書の性質なのか」が分からない**
  const by = {};
  for (const r of rules) by[r.reason || "未設定"] = (by[r.reason || "未設定"] || 0) + 1;
  const brk = Object.entries(by).map(([k, v]) => `${k} ${v}`).join(" ／ ");
  sec.appendChild(el(`<h4>${o.title}<span class="hint">${rules.length}件</span></h4>
    <p class="note">${o.note}${rules.length ? `<br><b>内訳：</b>${esc(brk)}` : ""}</p>`));
  if (!rules.length) {
    sec.appendChild(el('<p class="hint none">なし</p>'));
    return sec;
  }

  const bar = document.createElement("div");
  bar.className = "bulkbar";
  const allOn = sel.size === rules.length;
  const all = document.createElement("button");
  all.className = "ghost mini";
  all.textContent = allOn ? "選択を解除" : `すべて選ぶ（${rules.length}）`;
  all.onclick = () => {
    S.selRec[o.op] = allOn ? new Set() : new Set(rules);
    showManual();
  };
  bar.appendChild(all);
  bar.appendChild(el(`<span class="hint">${sel.size ? `${sel.size}件を選択中` : "未選択"}</span>`));

  if (sel.size) {
    const undo = document.createElement("button");
    undo.className = "x back";
    undo.textContent = undoLabel(sel.size, "戻す");
    undo.onclick = () => {
      for (const r of [...sel]) rules.splice(rules.indexOf(r), 1);
      S.selRec[o.op] = new Set();
      refresh();                      // 描き直しは refresh() の中でやる
    };
    bar.appendChild(undo);
  }

  const bulk = document.createElement("select");
  bulk.className = "treason";
  bulk.disabled = !sel.size;
  bulk.appendChild(el(`<option value=''>${
    sel.size ? bulkLabel(sel.size) : "選ぶと理由を付けられます"}</option>`));
  for (const c of choices) {
    const op = document.createElement("option");
    op.value = c.key; op.textContent = c.label;
    if (c.note) op.title = c.note;
    bulk.appendChild(op);
  }
  bulk.appendChild(el("<option value='—'>理由を未設定に戻す</option>"));
  bulk.onchange = () => {
    if (!bulk.value) return;
    for (const r of sel) r.reason = bulk.value === "—" ? "" : bulk.value;
    S.selRec[o.op] = new Set();
    syncSaveState(); showManual();     // ⚠️ 理由は抽出結果を変えないので refresh は要らない
  };
  bar.appendChild(bulk);
  sec.appendChild(bar);

  let lastIdx = null;
  rules.forEach((r, i) => {
    const page = o.page(r);
    const row = document.createElement("div");
    row.className = "orow" + (page === S.page ? " cur" : "") + (sel.has(r) ? " on" : "");

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = sel.has(r);
    cb.title = "Shift+クリックで、前に選んだ行からここまでをまとめて選ぶ";
    cb.onclick = (e) => {
      const last = S.selRec[o.op + ":last"];
      if (e.shiftKey && last != null) {
        for (const x of rules.slice(Math.min(last, i), Math.max(last, i) + 1)) sel.add(x);
      } else if (cb.checked) sel.add(r);
      else sel.delete(r);
      S.selRec[o.op + ":last"] = i;
      showManual();
    };
    row.appendChild(cb);

    const p = document.createElement("button");
    p.className = "pg";
    p.textContent = page === null || page === undefined ? "全ページ" : `p.${page}`;
    p.title = "このページへ移動する";
    if (page) p.onclick = () => go(page);
    row.appendChild(p);

    const label = o.label(r);
    row.appendChild(el(`<span class="ut" title="${esc(label)}">${esc(label)}</span>`));
    // 理由は行にも出す。⚠️ 出さないと、どれが未設定なのか選ぶ前に分からない
    row.appendChild(el(r.reason
      ? `<span class="rtag" title="${esc(reasonNote(o.op, r.reason))}">${esc(r.reason)}</span>`
      : '<span class="rtag none">未設定</span>'));
    sec.appendChild(row);
    lastIdx = i;
  });
  return sec;
}

/** 理由キー → 説明。プルダウンで選んだ後も、何を意味するか引けるようにする。 */
function reasonNote(op, key) {
  const c = (((S.info && S.info["操作の理由"]) || {})[op] || []).find((x) => x.key === key);
  return c ? `${c.label}${c.note ? "\n" + c.note : ""}` : key;
}

/** 前処理の記録パネルの開閉。
 *
 * ⚠️ **モーダルにしない。** 最初モーダルで作ったが、原本を見ながら理由を選ぶという
 *    本来の作業ができなかった。列を足して横に並べる（覆わない）。
 */
function openTasks(on) {
  const show = on === undefined ? $("taskPane").hidden : on;
  if (show) showTab("record");
  else if (S.sbTab === "record") showTab("pages");
}

function showManual() {
  if ($("taskPane").hidden) return openTasks(true);
  const cut = (s, n) => (s.trim().length > n ? s.trim().slice(0, n) + "…" : s.trim());
  const st = S.st;
  for (const k of ["excluded", "joins", "splits", "kinds", "manual_order"]) st[k] = st[k] || [];

  const wrap = document.createElement("div");
  const total = st.excluded.length + st.joins.length + st.splits.length +
                st.kinds.length + st.manual_order.length +
                (st.tables || []).length + (st.table_off || []).length +
                (st.unit_excludes || []).length + (st.unit_merges || []).length;

  wrap.appendChild(taskPane());

  wrap.appendChild(el(`<h3 class="manh">この文書で手を入れた箇所</h3>
    <p class="note">自動判定を手で直した箇所が<b>すべて</b>ここに出ます（合計 <b>${total}件</b>）。
    そのまま設定JSONに残り、卒論の付録になります。<br>
    <b>件数だけでは説明になりません。</b>「結合30件のうち28件が段またぎ」まで言えて初めて、
    レイアウト由来であって恣意的な操作ではないと示せます。理由を付けてください。<br>
    増えすぎたら、1件ずつ直すより<b>設定のほうを見直してください</b>。</p>`));

  wrap.appendChild(recordSection({
    op: "excluded", title: "除外した文", rules: st.excluded,
    note: "リンク表記・ロゴ・ページ表記など、明らかに文書の記述でないもの。"
        + "「この語を分析に入れたくない」は KH Coder 側でやること",
    page: (r) => r.page,
    // ⚠️ pt まで同じルールが他にもあるときは、座標を出さないと見分けられない
    label: (r) => (r.pt ? `${r.pt}pt ` : "")
      + (dupRules(st.excluded, r) && r.at ? `x${Math.round(r.at[0])} ` : "")
      + cut(r.text, 54),
  }));
  wrap.appendChild(recordSection({
    op: "joins", title: "結合したブロック", rules: st.joins,
    note: "座標からは「続き」だと判定できないので手作業になる箇所。"
        + "理由は<b>機械がなぜ切ったか</b>で選びます",
    page: (r) => r.page, label: (r) => `${cut(r.a, 22)} ＋ ${cut(r.b, 22)}`,
  }));
  wrap.appendChild(recordSection({
    op: "splits", title: "分けたブロック", rules: st.splits,
    note: "本文と同じptの見出しが本文と同じブロックに入った（癒着した）ものなど。"
        + "指定した行の境目で2つに分けます",
    page: (r) => r.page, label: (r) => `${cut(r.text, 40)} ｜ ${r.line}行目の後`,
  }));
  wrap.appendChild(recordSection({
    op: "kinds", title: "直した種別", rules: st.kinds,
    note: "フォントptからの自動判定が外れたもの",
    page: (r) => r.page, label: (r) => `［${r.kind}］ ${cut(r.text, 46)}`,
  }));
  wrap.appendChild(recordSection({
    op: "manual_order", title: "並べ替えたページ", rules: st.manual_order,
    note: "自動の読み順（列→上から）で崩れたページ",
    page: (r) => r.page, label: (r) => `${r.keys.length}ブロックの並び順`,
  }));

  // 表。自動検出（罫線）を人が足し引きした箇所
  for (const k of ["tables", "table_off"]) st[k] = st[k] || [];
  wrap.appendChild(el(`<h3 class="manh">表の判定を直した箇所</h3>
    <p class="note">罫線で区切られた表は自動で「表の1行＝1ブロック」になります（種別 <b class="k-表">表</b>）。
    自動で拾えなかった表を手で指定した箇所と、図解の枠線を表と誤認したので検出をやめたページが、ここに出ます。</p>`));
  wrap.appendChild(recordSection({
    op: "tables", title: "手で指定した表", rules: st.tables,
    note: "原本の上で囲んで「表にする」を押した範囲。文字の位置から列を推定しています",
    page: (r) => r.page,
    label: (r) => `範囲 x${Math.round(r.rect[0])}–${Math.round(r.rect[2])} ／ y${Math.round(r.rect[1])}–${Math.round(r.rect[3])}（${r.strategy || "text"}）`,
  }));
  wrap.appendChild(recordSection({
    op: "table_off", title: "表の検出をやめたページ", rules: st.table_off,
    note: "罫線はあるが表ではなかったページ。手で指定した表は残る",
    page: (r) => r.page, label: () => "このページの自動検出をやめる",
  }));

  // 抽出単位（L2）の手作業。→「抽出」タブで付けたもの
  for (const k of ["unit_excludes", "unit_merges"]) st[k] = st[k] || [];
  wrap.appendChild(el(`<h3 class="manh">抽出単位（L2）の手作業</h3>
    <p class="note">「抽出」タブで、生成AI関連語のヒット箇所を単位化するときに手を入れた箇所。
    件数と理由の内訳が、そのまま卒論3.5節の監査記録になります。</p>`));
  wrap.appendChild(recordSection({
    op: "unit_excludes", title: "抽出から外したヒット", rules: st.unit_excludes,
    note: "商標注記・誤ヒットなど、内容の記述でないもの。<b>迷ったら外さない</b>（外した件数と理由は全部開示する）",
    page: (r) => r.page, label: (r) => cut(r.text, 54),
  }));
  wrap.appendChild(recordSection({
    op: "unit_merges", title: "抽出単位に足した文", rules: st.unit_merges,
    note: "規則の既定（1文／表の行）では意味が取れないときだけ、隣の文を足したもの",
    page: (r) => r.page, label: (r) => `${cut(r.hit, 30)} ＋${(r.add || []).length}文`,
  }));

  // ⚠️ 一括で戻すのは「パラメータを詰め直したら、手で直した箇所が全部ズレた」ときのため。
  //    個別に戻すボタンを何十回も押させないための逃げ道であって、ふだん使うものではない。
  if (total) {
    const b = document.createElement("button");
    b.className = "danger ghost mini resetall";
    b.textContent = `手を入れた箇所を全部戻す（${total}件）`;
    b.onclick = resetManual;
    wrap.appendChild(b);
  }

  const body = $("taskBody");
  const keep = body.scrollTop;            // 理由を選ぶたび一番上に飛ばされないように
  body.innerHTML = "";
  body.appendChild(wrap);
  body.scrollTop = keep;

  const todo = taskStatus().filter((r) => r.state === "未確認").length +
               (S.st.skip_pages || []).filter((r) => !r.reason).length;
  $("taskCount").textContent = todo ? `残り ${todo}` : "すべて確認済み";
  S.taskSig = taskSig();       // ⚠️ どの経路から描いても控える（描き忘れの判定に使う）

  // 理由を選ぶときに「そのページが何なのか」が要る。読み込み済みなら即返る
  if (!S.pages) loadPageList().then(() => { if (!$("taskPane").hidden) showManual(); });
}
$("manBadge").onclick = () => openTasks();

// ---------- 手順（2026-08-12 追加） ----------
// **新しいレポートを開いたら、必ず一通り目を通す項目。** 定義は core.TASKS（サーバー側が正）。
//
// なぜ要るか：`skip_pages` に番号を並べるだけでは「何をしたか」しか残らず、
// **「やるべきことをやったか」が残らない。** 社間で同じ手順を踏んだと言えないと、
// そもそも比較が成立しない。→ 各項目について、必ず「どう結論したか」を記録する。
//
// ⚠️ **状態は3つあり、「未確認」と「該当なし」を必ず区別する。**
//    区別できないと「まだ見ていない」のか「見た上で無かった」のかが分からない。

const TASK_CLS = { "除外した": "done", "該当なし": "na", "残した": "kept", "未確認": "todo" };

// core.TASKS の note は Python 側でも読む文字列なので **強調** のまま書いてある。
// ⚠️ そのまま入れるとアスタリスクが見えてしまうので、太字だけHTMLに直す
const bold = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");

/** 手順ごとの状態。core.task_status() と同じ導き方をする（片方だけ変えないこと）。 */
function taskStatus() {
  const tasks = (S.info && S.info["手順"]) || [];
  const notes = S.st.task_states || [];
  return tasks.map((t) => {
    const pages = (S.st.skip_pages || [])
      .filter((r) => r.reason === t.key).map((r) => r.page).sort((a, b) => a - b);
    const n = notes.find((x) => x.key === t.key) || {};
    const state = pages.length ? "除外した" : (n.state || "未確認");
    return { ...t, state, pages, memo: n.memo || "" };
  });
}

function setTaskState(key, state, memo) {
  const rest = (S.st.task_states || []).filter((x) => x.key !== key);
  if (state) rest.push({ key, state, memo: memo || "" });
  S.st.task_states = rest;
}

/** 手順1件ぶんの行。 */
function taskRow(t) {
  const row = document.createElement("div");
  row.className = "taskrow " + TASK_CLS[t.state];
  row.appendChild(el(`<span class="tstate">${t.state}</span>
    <div class="tmain"><b>${esc(t.label)}</b>
      <p class="note">${bold(t.note)}</p></div>`));

  const act = document.createElement("div");
  act.className = "tact";

  if (t.state === "除外した") {
    // どのページを外したのかは、番号を押してその場で確認できるようにする
    const autoSet = new Set((S.st.skip_pages || []).filter((r) => r.auto).map((r) => r.page));
    for (const p of t.pages) {
      const b = document.createElement("button");
      b.className = "pg";
      b.textContent = `p.${p}` + (autoSet.has(p) ? "*" : "");
      b.title = (headingOf(p) || "このページへ移動する") +
                (autoSet.has(p) ? "\n＊自動候補をそのまま採用したページ" : "");
      b.onclick = () => go(p);
      act.appendChild(b);
    }
    const na = t.pages.filter((p) => autoSet.has(p)).length;
    act.appendChild(el(`<span class="hint">${t.pages.length}ページ${na ? `（＊自動 ${na}）` : ""}</span>`));
  } else if (t.state === "未確認") {
    // ⚠️ 手順は「そのページを開いて、原本を見ながら」片付けるもの。導線もそう書く
    act.appendChild(el(`<span class="hint">該当ページを開いて
      <b>「このページを除外」→ 隣の理由で「${esc(t.label)}」</b>を選ぶ</span>`));
    for (const s of (S.info["手順の状態"] || [])) {
      const b = document.createElement("button");
      b.className = "ghost mini";
      b.textContent = s;
      b.title = s === "該当なし"
        ? "この文書には無かった（＝見た上で該当が無い）"
        : "あるが、外さずに残すと判断した";
      b.onclick = () => { setTaskState(t.key, s, ""); refresh(); };
      act.appendChild(b);
    }
  } else {
    // 該当なし／残した ＝ 人の判断。**なぜそうしたかを書けるようにする**（付録に載る）
    const memo = document.createElement("input");
    memo.className = "tmemo";
    memo.placeholder = t.state === "残した"
      ? "残した理由（例：参照した制度名が書かれているため）"
      : "補足（任意）";
    memo.value = t.memo;
    memo.oninput = () => { setTaskState(t.key, t.state, memo.value); syncSaveState(); };
    act.appendChild(memo);
    const b = document.createElement("button");
    b.className = "x back";
    b.textContent = "戻す";
    b.onclick = () => { setTaskState(t.key, null); refresh(); };
    act.appendChild(b);
  }
  row.appendChild(act);
  return row;
}

function taskPane() {
  const rows = taskStatus();
  const wrap = document.createElement("div");
  const done = rows.filter((r) => r.state !== "未確認").length;

  wrap.appendChild(el(`<h3 class="manh">手順
      <span class="hint">${done} / ${rows.length} 確認済み</span></h3>
    <p class="note">新しいレポートを開いたら、ここを上から片付けます。
      <b>「外した」だけでなく「該当なし」「残した」も記録に残ります。</b><br>
      ⚠️ <b>「未確認」と「該当なし」は違います。</b>前者は見ていない、後者は見た上で無かった。
      この区別が無いと、手順を踏んだ証拠になりません。</p>`));

  for (const label of ["外すのが既定", "見て判断する"]) {
    const must = label === "外すのが既定";
    const sec = document.createElement("section");
    sec.className = "mansec";
    sec.appendChild(el(`<h4>${label}</h4>`));
    for (const t of rows.filter((r) => !!r.must === must)) sec.appendChild(taskRow(t));
    wrap.appendChild(sec);
  }

  // 除外ページの自動候補（→ core.suggest_skips）。**未採用のものだけ**出す。
  // 採用＝そのページを理由付きで除外に入れる（auto 印つき）。原本を見てから押す前提なので、
  // 行のページ番号で飛べるようにしてある
  wrap.appendChild(candidatePane());

  // 理由の付いていない除外ページ。ここを空にすることが「手順を片付けた」の中身
  const orphan = (S.st.skip_pages || []).filter((r) => !r.reason);
  const sec = document.createElement("section");
  sec.className = "mansec";
  sec.appendChild(el(`<h4>理由が未設定の除外ページ<span class="hint">${orphan.length}件</span></h4>
    <p class="note">番号だけでは、後から見て<b>章扉だったのか判断ミスだったのか分かりません</b>。
      理由を選ぶと、上の手順に反映されます。<br>
      <b>Shift+クリックで範囲選択。</b>巻末のデータ集のように十数ページ続くものは、
      先頭を選んで末尾を Shift+クリック → まとめて理由を付けられます。</p>`));
  if (!orphan.length) {
    sec.appendChild(el('<p class="hint none">なし</p>'));
    wrap.appendChild(sec);
    return wrap;
  }

  const pages = orphan.map((r) => r.page);
  // 理由が付いて一覧から消えたページは、選択からも外す
  S.sel = new Set([...(S.sel || [])].filter((p) => pages.includes(p)));
  const n = S.sel.size;

  // まとめて付けるバー。スクロールしても見えるように上に貼り付けておく
  // （12件も並ぶと、下まで送ったときに操作先が画面外へ消えるため）
  const bar = document.createElement("div");
  bar.className = "bulkbar";
  const allOn = n === pages.length;
  const all = document.createElement("button");
  all.className = "ghost mini";
  all.textContent = allOn ? "選択を解除" : `すべて選ぶ（${pages.length}）`;
  all.onclick = () => { S.sel = allOn ? new Set() : new Set(pages); showManual(); };
  bar.appendChild(all);
  bar.appendChild(el(`<span class="hint">${n ? `${n}件を選択中` : "未選択"}</span>`));

  if (n) {
    const undo = document.createElement("button");
    undo.className = "x back";
    undo.textContent = "除外をやめる";
    undo.onclick = () => {
      const target = [...S.sel];
      S.sel = new Set();            // ⚠️ 先に空にする。unskipPages の中で描き直されるため
      unskipPages(target);
    };
    bar.appendChild(undo);
  }

  const bulk = document.createElement("select");
  bulk.className = "treason";
  bulk.disabled = !n;
  bulk.appendChild(el(`<option value=''>${
    n ? bulkLabel(n) : "ページを選ぶと理由を付けられます"}</option>`));
  for (const t of (S.info["手順"] || [])) {
    const o = document.createElement("option");
    o.value = t.key; o.textContent = t.label;
    bulk.appendChild(o);
  }
  bulk.onchange = () => {
    if (!bulk.value) return;
    const target = [...S.sel];
    S.sel = new Set();           // ⚠️ 先に空にする（setSkipReasons の中で描き直されるため）
    setSkipReasons(target, bulk.value);       // 再解析もここで1回だけ
  };
  bar.appendChild(bulk);
  sec.appendChild(bar);

  for (const r of orphan) {
    const row = document.createElement("div");
    row.className = "orow" + (r.page === S.page ? " cur" : "") +
                    (S.sel.has(r.page) ? " on" : "");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = S.sel.has(r.page);
    cb.title = "Shift+クリックで、前に選んだページからここまでをまとめて選ぶ";
    cb.onclick = (e) => {
      if (e.shiftKey && S.selLast != null) {
        const a = pages.indexOf(S.selLast), b = pages.indexOf(r.page);
        if (a >= 0 && b >= 0) {
          for (const p of pages.slice(Math.min(a, b), Math.max(a, b) + 1)) S.sel.add(p);
        }
      } else if (cb.checked) S.sel.add(r.page);
      else S.sel.delete(r.page);
      S.selLast = r.page;
      showManual();
    };
    row.appendChild(cb);

    const p = document.createElement("button");
    p.className = "pg";
    p.textContent = `p.${r.page}`;
    p.title = "このページへ移動する";
    p.onclick = () => go(r.page);
    row.appendChild(p);
    const h = headingOf(r.page);
    row.appendChild(el(`<span class="ut" title="${esc(h)}">${
      h ? esc(h) : '<i class="hint">(見出しなし)</i>'}</span>`));
    sec.appendChild(row);
  }
  wrap.appendChild(sec);
  return wrap;
}

/** 除外ページの自動候補のうち、まだ採用していないもの。 */
function candidatePane() {
  const sec = document.createElement("section");
  sec.className = "mansec";
  const skip = skipSet();
  const all = (S.info && S.info["候補"]) || [];
  const todo = all.filter((c) => !skip.has(c.page));
  const adopted = all.filter((c) => skip.has(c.page)).length;
  sec.appendChild(el(`<h4>自動候補（未採用）<span class="hint">${todo.length}件${adopted ? `／採用済み ${adopted}` : ""}</span></h4>
    <p class="note">ページの位置・文字数・大きな文字の文言だけから機械的に挙げた候補です。
      <b>原本を見てから採用してください</b>（ページ番号で飛べます）。採用したものには「自動」の印が残ります。</p>`));
  if (!todo.length) {
    sec.appendChild(el('<p class="hint none">なし</p>'));
    return sec;
  }
  const bar = document.createElement("div");
  bar.className = "bulkbar";
  const allBtn = document.createElement("button");
  allBtn.className = "ghost mini";
  allBtn.textContent = `全部採用（${todo.length}）`;
  allBtn.title = "候補を全部、理由付きで除外に入れる。後から1件ずつ戻せます";
  allBtn.onclick = () => adoptCandidates(todo);
  bar.appendChild(allBtn);
  bar.appendChild(el(`<span class="hint">${[...new Set(todo.map((c) => c.reason))].map(
    (k) => `${k} ${todo.filter((c) => c.reason === k).length}`).join(" ／ ")}</span>`));
  sec.appendChild(bar);
  for (const c of todo) {
    const row = document.createElement("div");
    row.className = "orow candrow" + (c.page === S.page ? " cur" : "");
    const p = document.createElement("button");
    p.className = "pg";
    p.textContent = `p.${c.page}`;
    p.title = "このページへ移動する";
    p.onclick = () => go(c.page);
    row.appendChild(p);
    row.appendChild(el(`<span class="rtag cand">${esc(c.reason)}</span>`));
    row.appendChild(el(`<span class="why" title="${esc(c.why)}">${esc(c.why)}</span>`));
    const b = document.createElement("button");
    b.className = "ghost mini";
    b.textContent = "採用";
    b.style.marginLeft = "auto";
    b.onclick = () => toggleSkip(c.page, true, c.reason, true);
    row.appendChild(b);
    sec.appendChild(row);
  }
  return sec;
}

/** 手で直した箇所（除外・結合・種別・並べ替え）をまとめて自動判定に戻す。
 *
 * ⚠️ パラメータと除外ページには触らない。⚙ の「既定値に戻す」とは別物。
 *    こちらは「人の判断」だけを消し、あちらは「設定そのもの」を初期化する。
 */
function resetManual() {
  const st = S.st;
  const counts = [
    ["除外した文", (st.excluded || []).length],
    ["結合したブロック", (st.joins || []).length],
    ["分けたブロック", (st.splits || []).length],
    ["直した種別", (st.kinds || []).length],
    ["並べ替えたページ", (st.manual_order || []).length],
    ["手で指定した表", (st.tables || []).length],
    ["表の検出をやめたページ", (st.table_off || []).length],
    ["抽出から外したヒット", (st.unit_excludes || []).length],
    ["抽出単位に足した文", (st.unit_merges || []).length],
  ];
  const total = counts.reduce((a, [, n]) => a + n, 0);

  const body = el(`<p class="note">
      手で直した箇所を<b>すべて自動判定に戻します</b>（合計 <b>${total}件</b>）。<br>
      ${counts.map(([k, n]) => `${k} <b>${n}件</b>`).join(" ／ ")}</p>
    <p class="note">設定値（HEADER_Y・本文pt・TINY_RATIO など）・除外ページ・手順の記録は<b>そのまま残ります</b>。
      そちらも戻したいときは、設定の「既定値に戻す」を使ってください。</p>
    <p class="note">保存する前なら、ブラウザを再読み込みすれば元に戻ります。
      保存した後でも、直前の設定は <code>設定\\履歴\\</code> に残っています。</p>`);

  modal("手で直した箇所をすべて戻しますか？", body, [
    {
      label: `すべて戻す（${total}件）`, kind: "danger",
      run: () => {
        st.excluded = []; st.joins = []; st.splits = []; st.kinds = []; st.manual_order = [];
        st.tables = []; st.table_off = [];
        st.unit_excludes = []; st.unit_merges = [];
        refresh();
        toast(`手で直した ${total}件を自動判定に戻しました。`
          + "（まだ保存していなければ、再読み込みで元に戻せます）", "ok");
      },
    },
    { label: "やめる", kind: "ghost" },
  ]);
}

// ---------- 設定パネル ----------

function fillParams() {
  for (const k of PARAMS) {
    const el = $("p_" + k);
    el.value = S.st[k] === null || S.st[k] === undefined ? "" : S.st[k];
  }
  for (const k of BOOL_PARAMS) $("p_" + k).checked = !!S.st[k];
  for (const k of SEL_PARAMS) $("p_" + k).value = S.st[k] || "";
  $("order").value = S.st.order || "reading";
  updateSkipUI();
}

function readParams() {
  for (const k of PARAMS) {
    const v = $("p_" + k).value.trim();
    // body_size と section_min_pt は空欄＝自動（null）
    S.st[k] = v === "" ? null : Number(v);
  }
  for (const k of BOOL_PARAMS) S.st[k] = $("p_" + k).checked;
  for (const k of SEL_PARAMS) S.st[k] = $("p_" + k).value;
}

let timer = null;
function onParamChange() {
  readParams();
  clearTimeout(timer);
  timer = setTimeout(refresh, 180);
}
for (const k of PARAMS) $("p_" + k).addEventListener("input", onParamChange);
for (const k of [...BOOL_PARAMS, ...SEL_PARAMS]) $("p_" + k).addEventListener("change", onParamChange);

$("autoBody").onclick = () => { $("p_body_size").value = ""; onParamChange(); };

$("resetBtn").onclick = () => {
  if (!confirm("設定をすべて既定値に戻します。手で直した箇所や除外ページも消えます。続けますか？")) return;
  S.st = JSON.parse(JSON.stringify(S.info["既定"]));
  fillParams();
  refresh();
};

// ---------- 設定ドロワー ----------
// 設定は最初に1回詰めるだけのものなので、切り替え式にせず1枚に全部並べてある。
// 常時ヘッダーに出しておくのは、作業中に何度も使うものだけ（書き出し・除外件数）。

function openCfg(on) {
  const show = on === undefined ? $("drawer").hidden : on;
  if (show) showTab("settings");
  else if (S.sbTab === "settings") showTab("pages");
}
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("modal").hidden) closeModal();      // 手前にあるものから閉じる
});

// ---------- ダイアログ ----------
// 確認と一覧の共通の入れ物。confirm() だと中身（どのファイルがいつ書かれたか）を見せられない。

/** @param buttons [{label, kind, run}] — run が undefined なら閉じるだけ */
function modal(title, body, buttons) {
  $("modalTitle").textContent = title;
  $("modalBody").innerHTML = "";
  $("modalBody").append(body);
  $("modalFoot").innerHTML = "";
  for (const b of buttons) {
    const el = document.createElement("button");
    el.textContent = b.label;
    if (b.kind) el.className = b.kind;
    if (b.id) el.id = b.id;
    el.onclick = () => { closeModal(); if (b.run) b.run(); };
    $("modalFoot").appendChild(el);
  }
  $("modal").hidden = false;
}
const closeModal = () => { $("modal").hidden = true; };
$("modal").onclick = (e) => { if (e.target.id === "modal") closeModal(); };

/** タグ付きテンプレートで組む小さなヘルパ（HTML片から要素を作る） */
function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content;
}

/** 書き出し結果などの通知。閉じるまで残す（ファイルパスを読みたいので勝手に消さない）。
 *  @param links {ラベル: URL} — 付けるとダウンロードのボタンが並ぶ */
function toast(msg, kind, links) {
  $("toastMsg").textContent = msg;
  $("toast").className = kind || "";
  $("toastLinks").innerHTML = "";
  for (const [label, url] of Object.entries(links || {})) {
    const a = document.createElement("a");
    a.href = url;
    a.className = "dl";
    a.textContent = label;
    a.setAttribute("download", "");
    $("toastLinks").appendChild(a);
  }
  $("toast").hidden = false;
}
$("toastX").onclick = () => { $("toast").hidden = true; };

// ---------- 手元か、公開デモか ----------
// 公開デモは置き場がサーバー上の一時ディレクトリなので、
// 「ファイルのパス」を見せても意味がない。ダウンロードで受け取ってもらう。

const ENV = { 公開モード: false };

async function loadEnv() {
  Object.assign(ENV, await api("/api/env"));
  if (!ENV["公開モード"]) return;
  $("demoNote").hidden = false;
  $("demoNote").innerHTML =
    "<b>これは公開デモです。</b>お手元のPDFをアップロードして、そのまま試せます" +
    `（${ENV["上限MB"]}MBまで）。` +
    "<br>アップロードしたファイルと設定は<b>サーバーに残りません</b>" +
    "（一時領域に置き、再起動で消えます）。結果は<b>ダウンロードで受け取ってください</b>。" +
    "<br><span class='hint'>無料枠のため、15分アクセスが無いと停止します。" +
    "その場合、最初の表示に1分ほどかかります。</span>";
}

// ---------- サイズ分布 ----------

function drawHist() {
  const rows = S.info["サイズ分布"];
  const max = Math.max(...rows.map((r) => r["文字数"]));
  const body = S.st.body_size || S.info["推定本文pt"];
  $("hist").innerHTML = rows.map((r) => {
    const isBody = Math.abs(r.pt - body) <= (S.st.size_tol ?? 0.6);
    return `<tr class="${isBody ? "body-size" : ""}">
      <td>${r.pt}pt</td>
      <td style="width:60%"><span class="bar" style="width:${(r["文字数"] / max * 100).toFixed(0)}%"></span></td>
      <td>${r["文字数"].toLocaleString()}字</td></tr>`;
  }).join("");
}

// ---------- ページ描画（連続スクロール） ----------
//
// 110ページ分の画像を一度に持つと重い（1ページ約240KB）。なので
//   ・最初に全ページ分の「箱」だけを正しい縦横比で並べる（スクロールの長さが最初から合う）
//   ・見えている範囲の前後だけ画像を読み、離れたら捨てる
// という作りにしてある。読み込んだ解析結果は S.cache に持ち、右パネルはそこから描く。

/** 全ページ分の空の箱を並べる。中身はまだ入れない。 */
function buildPages() {
  const wrap = $("pages");
  wrap.innerHTML = "";
  S.cache = {};
  S.boxes = {};
  for (let n = 1; n <= S.pageMax; n++) {
    const p = S.info["ページ"][n - 1];
    const el = document.createElement("div");
    el.className = "pagebox";
    el.dataset.page = n;
    el.style.aspectRatio = `${p.w} / ${p.h}`;
    el.innerHTML = `<div class="pnum">p.${n}<small>${p.label}</small></div>` +
                   `<div class="ov"></div><div class="pload"></div>` +
                   `<div class="guide gh"><span>HEADER_Y</span></div>` +
                   `<div class="guide gf"><span>FOOTER_MARGIN</span></div>`;
    bindGuides(el);
    wrap.appendChild(el);
    S.boxes[n] = el;
  }
  markSkipped();
  observe();
}

let ioLoad = null, ioCurrent = null;

function observe() {
  const stage = $("stage");
  if (ioLoad) ioLoad.disconnect();
  if (ioCurrent) ioCurrent.disconnect();

  // 画面の前後400pxぶんだけ読み込む／出たら捨てる
  // ⚠️ ここを広げるほど「先読みされていて快適」だが、一度に走るリクエストが増えて逆に遅くなる
  ioLoad = new IntersectionObserver((ents) => {
    for (const e of ents) {
      const n = Number(e.target.dataset.page);
      if (e.isIntersecting) loadPage(n);
      else unloadPage(n);
    }
  }, { root: stage, rootMargin: "400px 0px" });

  // 画面の中央にかかっているページ＝「今のページ」
  ioCurrent = new IntersectionObserver((ents) => {
    for (const e of ents) {
      if (e.isIntersecting) setCurrent(Number(e.target.dataset.page));
    }
  }, { root: stage, rootMargin: "-49% 0px -49% 0px" });

  for (const el of Object.values(S.boxes)) { ioLoad.observe(el); ioCurrent.observe(el); }
}

/** 画像の解像度。表示幅にほぼ合わせる。
 *  0.25刻みに丸めているのは、幅を少し動かしただけで全ページを描き直さないため
 *  （サーバー側のキャッシュも、この値をキーにしている）。 */
function imgZoom(n) {
  const w = S.info["ページ"][n - 1].w;
  const z = ($("zoom").value / w) * 1.15;
  return Math.min(2, Math.max(0.75, Math.round(z * 4) / 4));
}

/** 走っている解析リクエストの数。0 でなければ「解析中…」を出す。
 *  設定を触ってから結果が変わるまで無反応に見える、という状態をなくすため。 */
let pending = 0;
function busy(d) {
  pending = Math.max(0, pending + d);
  $("busy").hidden = pending === 0;
}

async function loadPage(n) {
  const el = S.boxes[n];
  if (!el || el.dataset.loaded === "1") return;
  el.dataset.loaded = "1";

  const img = document.createElement("img");
  img.alt = `p.${n}`;
  img.loading = "eager";
  img.decoding = "async";
  img.src = `/api/doc/${encodeURIComponent(S.name)}/page/${n}.jpg?zoom=${imgZoom(n)}`;
  el.classList.add("loading");
  img.onload = () => { el.classList.add("ready"); el.classList.remove("loading"); };
  img.onerror = () => el.classList.remove("loading");
  el.prepend(img);

  busy(1);
  let d;
  try {
    d = await api(`/api/doc/${encodeURIComponent(S.name)}/page/${n}`, { settings: S.st });
  } catch (e) {
    el.dataset.loaded = "0";                  // 失敗したページは、もう一度読めるようにしておく
    throw e;                                  // 通知は unhandledrejection がまとめて出す
  } finally {
    busy(-1);
  }
  if (el.dataset.loaded !== "1") return;      // 待っている間にスクロールで外れた
  S.cache[n] = d;
  drawOverlay(n);
  drawGuides(el);
  if (n === S.page) drawUnits(d);             // 今のページなら右パネルも更新
}

function unloadPage(n) {
  const el = S.boxes[n];
  if (!el || el.dataset.loaded !== "1") return;
  el.dataset.loaded = "0";
  const img = el.querySelector("img");
  if (img) img.remove();                      // src ごと捨てる（メモリを戻す）
  el.classList.remove("ready", "loading");
  el.querySelector(".ov").innerHTML = "";
  delete S.cache[n];
}

/** 設定を変えたとき。読み込み済みのページだけ描き直す（全ページは触らない）。 */
function refresh() {
  markExtractStale();
  if (!S.name) return;
  syncSaveState();
  drawHist();
  markSkipped();
  redrawTasks();
  markCtxStale();
  for (const n of Object.keys(S.boxes).map(Number)) {
    if (S.boxes[n].dataset.loaded === "1") {
      S.boxes[n].dataset.loaded = "0";
      const img = S.boxes[n].querySelector("img");
      if (img) img.remove();
      loadPage(n);
    }
  }
}

/** 設定が変わったら、開いている記録パネルも描き直す。
 *
 * ⚠️ **ここを refresh() に入れるのが肝。** 除外・結合・種別・並べ替えはどれも refresh() を
 *    通るので、1か所で全部の反映を賄える。個々の操作から showManual() を呼ぶ形にしていた
 *    ときは、**呼び忘れた操作だけ画面に出ない**（開き直すまで気づけない）状態になっていた。
 *
 * ⚠️ 理由のメモを入力している最中だけは描き直さない。作り直すと入力欄のフォーカスが飛ぶ。
 */
function redrawTasks() {
  if ($("taskPane").hidden) return;
  const a = document.activeElement;
  if (a && a.classList && a.classList.contains("tmemo")) return;
  // ⚠️ パラメータをスライダーで動かすと refresh() が連続で走る。記録の中身が変わって
  //    いないときまで作り直すと、行数の多い文書でカクつくので、変化したときだけ描く
  if (taskSig() === S.taskSig) return;
  showManual();
}

/** 記録パネルの表示内容を決めているものだけを並べた指紋。 */
const taskSig = () => JSON.stringify([
  S.st.skip_pages, S.st.task_states, S.st.excluded,
  S.st.joins, S.st.kinds, S.st.manual_order,
  S.st.tables, S.st.table_off, S.page,
]);

function setCurrent(n) {
  if (S.page === n) return;
  S.page = n;
  $("pageNo").value = n;
  $("pageLabel").textContent = `印刷上のページ番号: ${S.info["ページ"][n - 1].label}`;
  $("skipThis").checked = skipSet().has(n);
  syncReasonPicker();
  for (const el of Object.values(S.boxes)) el.classList.remove("cur");
  S.boxes[n].classList.add("cur");
  markPageList();
  markThumbs();
  scrollThumbIntoView(n);
  // ページが変わったら右パネルは必ず先頭から。前のページで下まで見ていると、
  // 中途半端な位置から始まって「どこを見ているのか」が分からなくなる
  $("unitList").scrollTop = 0;
  if (S.cache[n]) drawUnits(S.cache[n]);
  else showSkeleton();
}

/** 解析待ちのページの右パネル。空のままだと「壊れた？」に見える */
function showSkeleton() {
  const wrap = $("unitList");
  wrap.classList.remove("fadein");
  wrap.innerHTML = `<div class="skel-note">このページを解析しています…</div>` +
    [3, 2, 4].map((k) => `<div class="skel">${"<div class='ln'></div>".repeat(k)}<div class="ln s"></div></div>`).join("");
  $("unitStat").textContent = "";
}

const pct = (v, total) => (v / total * 100).toFixed(3) + "%";

function drawOverlay(n) {
  const d = S.cache[n];
  const el = S.boxes[n];
  if (!d || !el) return;
  const ov = el.querySelector(".ov");
  ov.innerHTML = "";
  ov.classList.toggle("hidebox", !$("showBoxes").checked);
  ov.classList.toggle("hidetbl", !$("showTables").checked);

  // 検出した表の範囲（破線）。行ブロックの枠より下に敷く
  (d["表"] || []).forEach((t, i) => {
    const e = document.createElement("div");
    e.className = "tbox" + (t.manual ? " manual" : "");
    e.style.left = pct(t.bbox[0] - 2, d.width);
    e.style.top = pct(t.bbox[1] - 2, d.height);
    e.style.width = pct(t.bbox[2] - t.bbox[0] + 4, d.width);
    e.style.height = pct(t.bbox[3] - t.bbox[1] + 4, d.height);
    e.title = `表${i + 1}（${t.manual ? "手で指定" : "自動検出"}・${t.strategy}）`;
    // ⚠️ この関数の中では `el` がページの箱（DOM）を指しているので、ヘルパの el() は使えない
    const no = document.createElement("span");
    no.className = "tno";
    no.textContent = `表${i + 1}`;
    e.appendChild(no);
    ov.appendChild(e);
  });

  // 座標で捨てた行（ヘッダー・フッター）
  if ($("dimDropped").checked) {
    for (const ln of d.lines) {
      if (!ln.dropped) continue;
      ov.appendChild(box(ln, d, "k-drop", `${ln.dropped}: ${ln.text}`));
    }
  }
  // 残った行。所属グループごとに色を付ける
  for (const g of d.groups) {
    // 手で除外したものは、座標で捨てた行と同じ赤枠にする（＝画面上の意味を揃える）
    // 一部だけ除外された場合は、どの行かを特定できないので別の印（part）にする
    const cls = g.all_excluded ? "k-drop"
              : "k-" + g.kind + (g.some_excluded ? " part" : "");
    const note = g.all_excluded ? "（除外）" : g.some_excluded ? "（一部除外）" : "";
    for (const ln of g.lines) {
      const b = box(ln, d, cls, `${g.kind} ${g.size}pt${note}`);
      b.dataset.gid = g.gid;
      b.dataset.pg = n;
      b.onmouseenter = () => hl(g.gid, true);
      b.onmouseleave = () => hl(g.gid, false);
      b.onclick = () => {
        if (n !== S.page) return;
        const t = document.querySelector(`.grp[data-gid="${g.gid}"]`);
        if (t) t.scrollIntoView({ block: "center", behavior: "smooth" });
      };
      ov.appendChild(b);
    }
  }
  drawPick(ov, d, n);
}

/** 囲んで選んだブロックの印と、操作バー。**枠を描いた後に呼ぶ**（印を上書きされないため）。 */
function drawPick(ov, d, n) {
  if (!S.pick || S.pick.page !== n) return;
  const gs = d.groups.filter((g) => g.units.length && S.pick.keys.has(g.parts[0]));
  // ⚠️ パラメータを変えるとブロックの切れ方が変わり、選択が指し先を失う。黙って残さない
  if (gs.length < 2) { S.pick = null; return; }

  let bb = gs[0].bbox.slice();
  for (const g of gs) {
    ov.querySelectorAll(`.box[data-gid="${g.gid}"]`).forEach((b) => b.classList.add("picked"));
    bb = [Math.min(bb[0], g.bbox[0]), Math.min(bb[1], g.bbox[1]),
          Math.max(bb[2], g.bbox[2]), Math.max(bb[3], g.bbox[3])];
  }

  const bar = document.createElement("div");
  bar.className = "pickbar";
  bar.style.left = pct(bb[0], d.width);
  // 下に置くと紙面からはみ出すときだけ上に出す
  const below = bb[3] < d.height - 46;
  bar.style.top = pct(below ? bb[3] + 4 : bb[1] - 30, d.height);
  bar.appendChild(el(`<span class="pn">${gs.length}ブロック</span>`));

  for (const [label, title, run] of [
    [ICON("gather") + "まとめて並べる", "選んだブロックを読み順に並べて、1か所に集めます", gatherPicked],
    [ICON("table") + "表にする", "この範囲を表として扱い、文字の位置から列を推定して行ごとに組み直します（罫線の無い表）", tablePicked],
    [ICON("x") + "やめる", "選択をやめる", clearPick],
  ]) {
    const b = document.createElement("button");
    b.innerHTML = label;
    b.title = title;
    b.onclick = (e) => { e.stopPropagation(); run(); };
    bar.appendChild(b);
  }
  ov.appendChild(bar);
}

function box(ln, d, cls, title) {
  const el = document.createElement("div");
  el.className = "box " + cls;
  el.style.left = pct(ln.x0, d.width);
  el.style.top = pct(ln.y0, d.height);
  el.style.width = pct(ln.x1 - ln.x0, d.width);
  el.style.height = pct(ln.y1 - ln.y0, d.height);
  el.title = title;
  return el;
}

function hl(gid, on) {
  document.querySelectorAll(`.ov .box[data-gid="${gid}"][data-pg="${S.page}"]`)
    .forEach((e) => e.classList.toggle("hl", on));
  const g = document.querySelector(`.grp[data-gid="${gid}"]`);
  if (g) g.style.borderColor = on ? "var(--accent)" : "";
}

function markSkipped() {
  const skip = skipSet();
  for (const [n, el] of Object.entries(S.boxes || {}))
    el.classList.toggle("skip", skip.has(Number(n)));
  markThumbs();
}

// ---------- サムネイル一覧（2026-08-22 追加） ----------
// 長い文書（最大400ページ超）で「今どのあたりか」を掴み、一発で飛ぶためのもの。
// ページ画像と同じ endpoint を小さい zoom で呼ぶ（サーバー側のキャッシュは zoom 別）。
// 見えている範囲だけ読み込む（ページ本体と同じ作り）。畳めて、畳んだ状態は覚えておく。

let ioThumb = null;
const THUMB_ZOOM = 0.2;

function buildThumbs() {
  const wrap = $("thumbList");
  wrap.innerHTML = "";
  if (ioThumb) ioThumb.disconnect();
  for (let n = 1; n <= S.pageMax; n++) {
    const p = S.info["ページ"][n - 1];
    const t = document.createElement("div");
    t.className = "thumb";
    t.dataset.page = n;
    t.style.setProperty("--ar", `${p.w} / ${p.h}`);
    t.innerHTML = `<div class="ti"></div><span class="tn">${n}</span><span class="hits"></span>`;
    t.title = `p.${n}（印刷上 ${p.label}）`;
    t.onclick = () => go(n);
    wrap.appendChild(t);
  }
  ioThumb = new IntersectionObserver((ents) => {
    for (const e of ents) {
      if (!e.isIntersecting) continue;
      const t = e.target;
      if (t.dataset.loaded === "1") continue;
      t.dataset.loaded = "1";
      const img = document.createElement("img");
      // ⚠️ loading="lazy" は付けない。DOMに入れる前の img は画面に無いので永遠に読まれない
      //    （見えている範囲だけ読む制御は、上の IntersectionObserver がやっている）
      img.decoding = "async";
      img.alt = `p.${t.dataset.page}`;
      img.src = `/api/doc/${encodeURIComponent(S.name)}/page/${t.dataset.page}.jpg?zoom=${THUMB_ZOOM}`;
      const ph = t.querySelector(".ti");
      img.onload = () => ph && ph.replaceWith(img);
      ioThumb.unobserve(t);
    }
  }, { root: $("thumbs"), rootMargin: "600px 0px" });
  for (const t of wrap.children) ioThumb.observe(t);
  markThumbs();
  // 前回のタブを復元（無ければ抽出。この道具の主目的は抽出になった。2026-08-25）
  let tab = "units";
  try { tab = localStorage.getItem("sbTab") || "units"; } catch (e) { /* noop */ }
  if (!SB_TABS[tab]) tab = "units";
  showTab(tab);
  let open = true;
  try { open = localStorage.getItem("sbOpen") !== "0"; } catch (e) { /* noop */ }
  collapseSidebar(!open);
}

/** 除外・候補・表あり・現在ページ の印を付け直す。 */
function markThumbs() {
  const skip = skipSet();
  const cand = {};
  for (const c of ((S.info && S.info["候補"]) || [])) cand[c.page] = c.reason;
  const tbl = new Set((S.pages || []).filter((r) => r["表数"] > 0).map((r) => r["ページ"]));
  for (const t of $("thumbList").children) {
    const n = Number(t.dataset.page);
    t.classList.toggle("skip", skip.has(n));
    t.classList.toggle("cand", !!cand[n]);
    if (cand[n]) t.dataset.cand = cand[n];
    t.classList.toggle("tbl", tbl.has(n));
    t.classList.toggle("cur", n === S.page);
    const h = S.ctx && S.ctx["ページ別"] ? S.ctx["ページ別"][n] : 0;
    const hs = t.querySelector(".hits");
    if (hs) hs.textContent = h ? `${h}` : "";
  }
}

function showThumbs(on) {
  if (on) showTab("pages");
}

function scrollThumbIntoView(n) {
  const t = $("thumbList").children[n - 1];
  if (!t || $("thumbs").hidden) return;
  const box = $("thumbs");
  const top = t.offsetTop - box.clientHeight / 2 + t.clientHeight / 2;
  box.scrollTo({ top, behavior: "smooth" });
}

function drawUnits(d) {
  const wrap = $("unitList");
  const keep = wrap.scrollTop;      // 設定を変えた直後に一番上へ飛ばされないように
  let n = 0, ex = 0, kinds = Object.fromEntries(KINDS.map((k) => [k, 0]));
  wrap.innerHTML = "";
  wrap.classList.add("fadein");

  // ⚠️ 除外ページでも単位は普通に計算されて返ってくる（画面で中身を確認できるように）。
  //    それを黙って並べると「除外したのに効いていない」ように見えるので、必ず断る。
  const skipped = !!d["除外ページ"];
  wrap.classList.toggle("skipped", skipped);
  if (skipped) {
    const b = document.createElement("div");
    b.className = "skip-note";
    b.innerHTML = "<b>このページは除外しています。</b>下の文は<b>書き出されません</b>。" +
      "<br>戻すには、上のツールバーの「このページを除外」のチェックを外してください。";
    wrap.appendChild(b);
  }

  let shown = 0;                    // 表示した（単位を持つ）ブロックの数。先頭判定に使う
  for (const g of d.groups) {
    if (!g.units.length) continue;
    for (const u of g.units) {
      if (u.excluded) { ex++; continue; }
      n++; kinds[g.kind]++;
    }

    shown++;

    const div = document.createElement("div");
    div.className = "grp" + (g.all_excluded ? " gone" : "") +
                    (g.parts.length > 1 ? " joined" : "");
    div.dataset.gid = g.gid;
    div._g = g;                              // 並べ替えのとき parts（生text）を取り出す

    const head = document.createElement("div");
    head.className = "grp-head";
    head.innerHTML =
      `<span>${g.size}pt</span>` +
      (g.table !== undefined
        ? `<span class="tbl" title="表${g.table + 1} の ${g.row + 1} 行目">${ICON("table")}表${g.table + 1}</span>` : "") +
      (g.is_section ? `<span class="sec">${ICON("pin")}セクション見出し</span>` : "") +
      (g.parts.length > 1 ? `<span class="jn" title="${g.auto_joined
        ? "続いている文として自動で繋ぎました（句点で終わらず、最終行が右端まで届いていたため）" : "手で繋ぎました"}">${ICON("link")}${
        g.auto_joined ? "自動" : ""}${g.parts.length}結合</span>` : "") +
      `<span class="cnt">${g.units.length}文</span>`;

    // 段をまたいで続く本文を、人が指定して繋ぐ。座標からは判定できないので手作業になる
    if (g.parts.length > 1 && !g.auto_joined) {
      const b = document.createElement("button");
      b.className = "j undo";
      b.textContent = "結合を解除";
      b.onclick = () => unjoin(g.parts);
      head.appendChild(b);
    }
    const prev = prevShown(d.groups, g.gid);
    if (prev) {
      const b = document.createElement("button");
      b.className = "j";
      b.innerHTML = ICON("up") + "上と結合";
      b.title = "ひとつ上のブロックの続きとして繋ぎます（句点を挟まず1文になります）";
      // ⚠️ キーは必ず parts の端を使う。結合ルールはサーバー側で**結合前の生text**に対して
      //    照合されるので、g.raw（連結済み）を渡すとどのブロックにも当たらず、
      //    押しても何も起きない。既に結合済みのブロック同士を繋ぐときに露見した
      b.onclick = () => join(prev.parts[prev.parts.length - 1], g.parts[0]);
      head.appendChild(b);
    }

    if (g.units.length > 1 && !g.all_excluded) {
      const b = document.createElement("button");
      b.className = "x all";                  // グループごとまとめて除外
      b.textContent = "まとめて除外";
      b.title = "このブロックの文をすべて、このページから除外します";
      b.onclick = () => exclude(
        g.units.filter((u) => !u.excluded).map((u) => u.text), S.page, g.size, g.bbox);
      head.appendChild(b);
    }
    head.prepend(kindPicker(g));
    head.prepend(grip(div));
    div.appendChild(head);

    for (const u of g.units) div.appendChild(unitRow(u, g));
    div.onmouseenter = () => hl(g.gid, true);
    div.onmouseleave = () => hl(g.gid, false);
    wrap.appendChild(div);
  }

  // ⚠️ 当たらなかった除外ルールも黙って無視される。
  //    照合は 文言＋ページ＋pt＋位置 なので、パラメータを変えて切れ方が変わると外れる。
  //    **気づけないと「除外したはずのものが出力に残ったまま」分析へ進んでしまう。**
  const badEx = d["未適用の除外"] || [];
  if (badEx.length) {
    const w = document.createElement("div");
    w.className = "skip-note warn";
    w.innerHTML = `<b>このページの除外 ${badEx.length}件が、どの文にも当たっていません。</b>` +
      "<br>設定を変えてブロックの切れ方が変わったのかもしれません。" +
      "もう一度 <b>×</b> で除外し直すか、下の一覧から外してください。";
    for (const r of badEx) {
      const row = document.createElement("div");
      row.className = "badjoin";
      row.innerHTML = `<span class="ut">${(r.pt ? r.pt + "pt " : "")}` +
                      `${esc((r.text || "").trim().slice(0, 40))}</span>`;
      const b = document.createElement("button");
      b.className = "x back";
      b.textContent = "ルールを外す";
      // ⚠️ サーバーから返ってきた r は複製なので、識別ではなく値で比べる。
      //    位置は「両方無い／両方同じ」の厳密一致（atEq は片方が無いと真になるので使えない）
      b.onclick = () => {
        const key = JSON.stringify([r.page, r.text, r.pt ?? null, r.at ?? null]);
        S.st.excluded = (S.st.excluded || []).filter(
          (x) => JSON.stringify([x.page, x.text, x.pt ?? null, x.at ?? null]) !== key);
        refresh();
      };
      row.appendChild(b);
      w.appendChild(row);
    }
    wrap.prepend(w);
  }

  // ⚠️ 当たらなかった結合ルールは黙って無視されるので、必ず知らせる。
  //    パラメータを変えるとブロックの切れ方が変わり、保存済みのルールが外れることがある
  const bad = d["未適用の結合"] || [];
  if (bad.length) {
    const w = document.createElement("div");
    w.className = "skip-note warn";
    w.innerHTML = `<b>このページの結合 ${bad.length}件が、どのブロックにも当たっていません。</b>` +
      "<br>設定を変えてブロックの切れ方が変わったのかもしれません。" +
      "結合し直すか、下の一覧から外してください。";
    for (const r of bad) {
      const row = document.createElement("div");
      row.className = "badjoin";
      row.innerHTML = `<span class="ut">${esc(r.a.trim().slice(0, 24))} ＋ ` +
                      `${esc(r.b.trim().slice(0, 24))}</span>`;
      const b = document.createElement("button");
      b.className = "x back";
      b.textContent = "ルールを外す";
      b.onclick = () => {
        S.st.joins = (S.st.joins || []).filter(
          (x) => !(x.page === S.page && x.a === r.a && x.b === r.b));
        refresh();
      };
      row.appendChild(b);
      w.appendChild(row);
    }
    wrap.prepend(w);
  }

  // ⚠️ 当たらなかった分割ルールも同じ（黙って効かないのが一番まずい）
  const badSp = d["未適用の分割"] || [];
  if (badSp.length) {
    const w = document.createElement("div");
    w.className = "skip-note warn";
    w.innerHTML = `<b>このページの分割 ${badSp.length}件が、どのブロックにも当たっていません。</b>` +
      "<br>設定を変えてブロックの切れ方が変わったのかもしれません。" +
      "分け直すか、下の一覧から外してください。";
    for (const r of badSp) {
      const row = document.createElement("div");
      row.className = "badjoin";
      row.innerHTML = `<span class="ut">${esc((r.text || "").trim().slice(0, 40))}` +
                      ` ｜ ${r.line}行目の後</span>`;
      const b = document.createElement("button");
      b.className = "x back";
      b.textContent = "ルールを外す";
      // 値で比べる（サーバーから返ってきた r は複製。→ 除外の警告と同じ理由）
      b.onclick = () => {
        const key = JSON.stringify([r.page, r.text, r.line, r.at ?? null]);
        S.st.splits = (S.st.splits || []).filter(
          (x) => JSON.stringify([x.page, x.text, x.line, x.at ?? null]) !== key);
        refresh();
      };
      row.appendChild(b);
      w.appendChild(row);
    }
    wrap.prepend(w);
  }

  if (!wrap.querySelector(".grp")) {
    const e = document.createElement("p");
    e.className = "note empty";
    e.textContent = d.lines.length
      ? "このページの行は、すべてヘッダー／フッターとして切られています。"
      : "このページからは文字が取れませんでした（画像だけのページかもしれません）。";
    wrap.appendChild(e);
  }

  const dropped = d.lines.filter((l) => l.dropped);
  if (dropped.length) {
    const div = document.createElement("div");
    div.className = "drop-list";
    div.innerHTML = `<b>座標で捨てた行 ${dropped.length}件</b>（ヘッダー・フッター）` +
      dropped.map((l) => `<div class="d">${l.dropped} ／ ${esc(l.text)}</div>`).join("");
    wrap.appendChild(div);
  }

  // 種別は0件のものを並べても読みにくいだけなので、出たものだけ出す
  const bd = KINDS.filter((k) => kinds[k]).map((k) => `${k}${kinds[k]}`).join(" ");
  $("unitStat").textContent = (skipped ? "（除外ページ）" : "") +
    `${n}文（${bd}）` +
    (ex ? ` ／ 除外${ex}` : "") +
    ` ／ 本文${d["本文pt"]}pt`;
  $("ordReset").hidden = !hasManualOrder(S.page);
  drawTableCtl(d);
  wrap.scrollTop = keep;            // ページ切り替えのときは setCurrent が先に 0 にしている
  drawExcluded();
}

// ---------- ブロックの並べ替え ----------
// reading_order（列→上から）は機械的な規則でしかないので、回り込みのある図解ページなどでは
// まだ崩れる。レイアウトの意図は座標だけからは復元できない → 人が並べ替えて、それを残す。
//
// ⚠️ 並べ替えはサーバー側で**結合（joins）より前**に効く。結合は「隣り合うブロック」を
//    繋ぐ操作なので、順序を決めてから結合を指定するのが正しい順番。

let dragEl = null, orderBefore = null;

const hasManualOrder = (page) =>
  (S.st.manual_order || []).some((r) => r.page === page);

// 掴んだまま端に寄せるとリストが送られる。
// ⚠️ **`dragover` だけでは足りない。** あのイベントはポインタが動いたときにしか来ないので、
//    端で止めて待っていても何も起きない（＝1画面分より遠くへは運べない）。
//    → ドラッグ中は requestAnimationFrame で回し続け、最後のポインタ位置を見て送る。
const EDGE = 70;        // 上下の端からこの範囲(px)に入ったら送り始める
const SPEED = 18;       // 1フレームあたりの最大送り量(px)
let dragY = 0, scrollRaf = null;

function autoScroll() {
  if (!dragEl) { scrollRaf = null; return; }
  const wrap = $("unitList");
  const b = wrap.getBoundingClientRect();
  // 端に近いほど速く。いきなり最高速だと行き過ぎるので、距離に比例させる
  let v = 0;
  if (dragY < b.top + EDGE) v = -SPEED * Math.min(1, (b.top + EDGE - dragY) / EDGE);
  else if (dragY > b.bottom - EDGE) v = SPEED * Math.min(1, (dragY - (b.bottom - EDGE)) / EDGE);
  if (v) {
    const was = wrap.scrollTop;
    wrap.scrollTop += v;
    // 実際に動いたときだけ入れ直す。端で止まっているのに毎フレームDOMを触らない
    if (wrap.scrollTop !== was) placeDragged(dragY);
  }
  scrollRaf = requestAnimationFrame(autoScroll);
}

/** 現在の表示順を、結合前の生text列として取り出す（＝そのまま設定に保存できる形） */
function domOrder() {
  return [...$("unitList").querySelectorAll(".grp")].flatMap((el) => el._g.parts);
}

/** 掴む所。ブロック全体を draggable にすると本文が選択できなくなるので、取っ手だけにする。 */
function grip(div) {
  const g = document.createElement("span");
  g.className = "grip";
  g.textContent = "⠿";
  g.title = "掴んで上下に動かすと、このブロックの並び順を変えられます";
  g.draggable = true;
  g.ondragstart = (e) => {
    dragEl = div;
    orderBefore = JSON.stringify(domOrder());
    div.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", "");   // Firefox はこれが無いと開始しない
    e.dataTransfer.setDragImage(div, 24, 16);
    dragY = e.clientY;
    if (!scrollRaf) scrollRaf = requestAnimationFrame(autoScroll);
  };
  // drop でなく dragend で確定する。ドラッグ中に DOM を動かしているので、
  // 枠の外で離されても・Escで中止されても、ここを必ず通る
  g.ondragend = () => {
    div.classList.remove("dragging");
    dragEl = null;
    if (scrollRaf) { cancelAnimationFrame(scrollRaf); scrollRaf = null; }
    if (JSON.stringify(domOrder()) !== orderBefore) commitOrder();
  };
  return g;
}

/** 掴んでいるブロックを、どの要素の前に入れるか。中心より上にある一番近いものを選ぶ。 */
function dropTarget(wrap, y) {
  let best = null, bestOff = -Infinity;
  for (const el of wrap.querySelectorAll(".grp:not(.dragging)")) {
    const b = el.getBoundingClientRect();
    const off = y - b.top - b.height / 2;
    if (off < 0 && off > bestOff) { bestOff = off; best = el; }
  }
  return best;
}

/** 掴んでいるブロックを、いまのポインタ位置に合う場所へ入れ直す。 */
function placeDragged(y) {
  const wrap = $("unitList");
  // ⚠️ 末尾には「座標で捨てた行」のリストが居るので、appendChild ではその後ろに回ってしまう
  const target = dropTarget(wrap, y) || wrap.querySelector(".drop-list");
  if (target) wrap.insertBefore(dragEl, target);
  else wrap.appendChild(dragEl);
}

$("unitList").addEventListener("dragover", (e) => {
  if (!dragEl) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = "move";
  dragY = e.clientY;
  placeDragged(e.clientY);
});
// リストの外（ページ画像の上など）へ出ても位置は追い続ける。
// ⚠️ 追わないと、外へ出た瞬間に自動スクロールが止まって「端まで運べない」感じになる
document.addEventListener("dragover", (e) => { if (dragEl) dragY = e.clientY; });
$("unitList").addEventListener("drop", (e) => e.preventDefault());

/** 並び順を設定に書く。⚠️ 消す前に理由を拾う（同じページを何度も並べ替えるため）。 */
function saveOrder(page, keys, refreshAfter = true) {
  const was = (S.st.manual_order || []).find((r) => r.page === page);
  S.st.manual_order = (S.st.manual_order || []).filter((r) => r.page !== page);
  S.st.manual_order.push({ page, keys, reason: (was && was.reason) || "" });
  if (refreshAfter) refresh();                 // サーバーに通して、保存した順序で描き直す
}

const commitOrder = () => saveOrder(S.page, domOrder());

$("ordReset").onclick = () => {
  S.st.manual_order = (S.st.manual_order || []).filter((r) => r.page !== S.page);
  refresh();
};

// ---------- ページ上でドラッグして範囲選択（2026-08-13 追加） ----------
// 図解ページは並べ替える数が多く、右パネルで1つずつ動かすのが現実的でない
// （p6 の図では 20ブロック超）。→ **原本の上でまとめて囲んで、一気に並べる。**
//
// ⚠️ 囲んで「消す」機能にはしていない。除外は1件ずつ理由を付ける操作なので、
//    まとめてやれると雑になる（→ README「ここで消すもの／消さないもの」）。

S.pick = null;         // {page, keys:Set<結合前の生text>} 選んでいるブロック

/** 選択範囲（PDF座標）と重なる、単位を持つブロック。 */
const pickGroups = (d, r) => d.groups.filter((g) =>
  g.units.length &&
  g.bbox[0] < r[2] && g.bbox[2] > r[0] && g.bbox[1] < r[3] && g.bbox[3] > r[1]);

/** 画面上の矩形（clientX/Y）を PDF 座標に直す。 */
const toPdf = (box, d, l, t, w, h) => [
  (l - box.left) / box.width * d.width, (t - box.top) / box.height * d.height,
  (l - box.left + w) / box.width * d.width, (t - box.top + h) / box.height * d.height,
];

/** 選んだブロックを読み順に並べる。
 *
 * ⚠️ **core.reading_order と同じ規則を JS でもう一度書いている。** ふだんなら避けるが、
 *    ここで出すのは「並べ替えの結果」＝ `manual_order` に焼き付く**データ**であって、
 *    サーバーが後から計算し直すものではない。だから二重実装によるズレは起きない。
 *    （x方向の隙間で列に切り、列を左から、列の中は上から。COL_GAP_RATIO = 0.06）
 */
function readingSort(gs, pageWidth) {
  if (gs.length < 2) return gs.slice();
  const xs = gs.map((g) => g.bbox[0]).sort((a, b) => a - b);
  const bounds = [];
  for (let i = 1; i < xs.length; i++) {
    if (xs[i] - xs[i - 1] >= pageWidth * 0.06) bounds.push((xs[i] + xs[i - 1]) / 2);
  }
  const col = (g) => bounds.filter((b) => g.bbox[0] > b).length;
  return gs.slice().sort((a, b) =>
    col(a) - col(b) || a.bbox[1] - b.bbox[1] || a.bbox[0] - b.bbox[0]);
}

/** 選んだブロックを1か所に集める。**最初の1つが居た位置**へ、読み順で詰める。
 *  @return {all, out, chosen} 並べ替え後の列／選択が無ければ null
 */
function gatherOrder() {
  const d = S.pick && S.cache[S.pick.page];
  if (!d) return null;
  const all = d.groups.filter((g) => g.units.length);
  const on = (g) => S.pick.keys.has(g.parts[0]);
  const chosen = readingSort(all.filter(on), d.width);
  if (chosen.length < 2) return null;
  const rest = all.filter((g) => !on(g));
  // 「最初の1つより前にある、選ばれていないブロックの数」＝差し込む位置
  const at = all.slice(0, all.findIndex(on)).filter((g) => !on(g)).length;
  return { all, chosen, out: [...rest.slice(0, at), ...chosen, ...rest.slice(at)] };
}

function gatherPicked() {
  const r = gatherOrder();
  if (!r) return;
  const page = S.pick.page;
  S.pick = null;
  saveOrder(page, r.out.flatMap((g) => g.parts));
  toast(`${r.chosen.length}ブロックを読み順にまとめました。`
      + "（細かい順序は右パネルで掴んで直せます）", "ok");
}

function clearPick() {
  const n = S.pick && S.pick.page;
  S.pick = null;
  if (n) drawOverlay(n);
}

// ---------- 表（2026-08-22 追加） ----------
// 罫線で区切られた表は自動で「行ごと1ブロック」になる（→ core.find_page_tables）。
// ここにあるのは、その自動判定を人が足し引きする操作。どちらも設定JSONに残る。
//   ・▦ 表にする         … 囲んだ範囲を表として扱う（罫線の無い表。文字の位置で列を推定）
//   ・このページの検出をやめる … 図解の枠線を表と誤認したページで、自動検出だけを切る

/** 囲んだ範囲を「表」として登録する。範囲＝選んだブロック全体を含む矩形。 */
function tablePicked() {
  const d = S.pick && S.cache[S.pick.page];
  if (!d) return;
  const gs = d.groups.filter((g) => g.units.length && S.pick.keys.has(g.parts[0]));
  if (!gs.length) return;
  let bb = gs[0].bbox.slice();
  for (const g of gs) {
    bb = [Math.min(bb[0], g.bbox[0]), Math.min(bb[1], g.bbox[1]),
          Math.max(bb[2], g.bbox[2]), Math.max(bb[3], g.bbox[3])];
  }
  const page = S.pick.page;
  S.pick = null;
  S.st.tables = S.st.tables || [];
  S.st.tables.push({ page, rect: bb.map((v) => Math.round(v * 10) / 10),
                     strategy: "text", reason: "罫線なし" });
  S.st.tables.sort((a, b) => a.page - b.page);
  refresh();
  toast("囲んだ範囲を表として扱います（文字の位置から列を推定）。\n"
      + "行に分かれない／変に分かれるときは、記録パネルから指定を戻してください。", "ok");
}

const isTableOff = (page) => (S.st.table_off || []).some((r) => r.page === page);

function toggleTableOff(page, on) {
  S.st.table_off = (S.st.table_off || []).filter((r) => r.page !== page);
  if (on) {
    S.st.table_off.push({ page, reason: "" });
    S.st.table_off.sort((a, b) => a.page - b.page);
  }
  refresh();
}

/** 単位パネルの頭に出す「▦ 表N件 ／ 検出をやめる・戻す」。表があるページ、または切ったページだけ出す。 */
function drawTableCtl(d) {
  const box = $("tblCtl");
  box.innerHTML = "";
  const n = (d["表"] || []).length;
  const off = !!d["表検出オフ"];
  if (!n && !off) return;
  box.appendChild(el(`<span class="n" title="このページで表として組み直した範囲の数">${ICON("table")}表${n}件${off ? "（自動検出オフ）" : ""}</span>`));
  const b = document.createElement("button");
  b.className = "ghost mini";
  b.textContent = off ? "自動検出に戻す" : "このページの検出をやめる";
  b.title = off
    ? "このページの表の自動検出をもう一度有効にします"
    : "図解の枠線などを表と誤認しているとき。手で指定した表（表にする）は残ります";
  b.onclick = () => toggleTableOff(S.page, !off);
  box.appendChild(b);
}

// ページ画像の上でドラッグ。⚠️ ガイド線（.gh/.gf）は .ov の外にあるので競合しない
$("pages").addEventListener("mousedown", (e) => {
  if (e.button !== 0 || !e.target.closest) return;
  // ⚠️ 操作バーは .ov の中にある。除かないと、ボタンを押しただけで選択がやり直しになる
  if (e.target.closest(".pickbar")) return;
  const ov = e.target.closest(".ov");
  if (!ov) return;
  const n = Number(ov.parentElement.dataset.page);
  const d = S.cache[n];
  if (!d) return;
  e.preventDefault();                       // 画像やテキストのネイティブなドラッグを止める

  const x0 = e.clientX, y0 = e.clientY;
  const band = document.createElement("div");
  band.className = "band";
  let moved = false, hit = [];

  const move = (ev) => {
    // ⚠️ 数px の揺れでバンドを出さない。出すと、枠をクリックしただけで選択が始まる
    if (!moved && Math.abs(ev.clientX - x0) + Math.abs(ev.clientY - y0) < 5) return;
    if (!moved) { moved = true; ov.appendChild(band); }
    const box = ov.getBoundingClientRect();
    const l = Math.min(x0, ev.clientX), t = Math.min(y0, ev.clientY);
    const w = Math.abs(ev.clientX - x0), h = Math.abs(ev.clientY - y0);
    band.style.left = pct(l - box.left, box.width);
    band.style.top = pct(t - box.top, box.height);
    band.style.width = pct(w, box.width);
    band.style.height = pct(h, box.height);
    hit = pickGroups(d, toPdf(box, d, l, t, w, h));
    band.textContent = hit.length ? `${hit.length}ブロック` : "";
    // 何が入るのかを離す前に見せる。**離してから違ったと分かるのでは選び直しが増える**
    const gids = new Set(hit.map((g) => g.gid));
    ov.querySelectorAll(".box").forEach(
      (b) => b.classList.toggle("picked", gids.has(Number(b.dataset.gid))));
  };

  const up = () => {
    document.removeEventListener("mousemove", move);
    document.removeEventListener("mouseup", up);
    band.remove();
    if (!moved) return;
    S.pick = hit.length >= 2
      ? { page: n, keys: new Set(hit.map((g) => g.parts[0])) } : null;
    // ⚠️ 直後の click を1回だけ潰す。潰さないと、枠のクリック（その単位へ飛ぶ）が走る
    document.addEventListener("click", (c) => { c.stopPropagation(); c.preventDefault(); },
                              { capture: true, once: true });
    drawOverlay(n);
    if (!S.pick && hit.length) toast("ブロックを2つ以上囲んでください。", "err");
  };
  document.addEventListener("mousemove", move);
  document.addEventListener("mouseup", up);
});

// ---------- 種別（本文／大／小／極小）を手で直す ----------
// 種別はフォントptから機械的に決めているが、見出しが本文と同じptだったり、
// 図解の説明文が本文より少し小さかったりして外れる。
// ⚠️ SIZE_TOL を動かして直そうとしないこと。文書全体に効くので、1ブロックのために
//    全体の判定を崩すことになる。→ そのブロックだけを名指しで直す。

function kindPicker(g) {
  const frag = document.createDocumentFragment();
  const sel = document.createElement("select");
  sel.className = "kind k-" + g.kind + (g.forced_kind ? " forced" : "");
  sel.title = g.forced_kind
    ? `手で「${g.forced_kind}」にしています（自動判定は「${g.auto_kind}」）`
    : "種別はフォントptからの自動判定。外れているときはここで直せます";
  for (const k of KINDS) {
    const o = document.createElement("option");
    o.value = k;
    o.textContent = k;
    o.selected = k === g.kind;
    sel.appendChild(o);
  }
  sel.onchange = () => setKind(g, sel.value);
  frag.appendChild(sel);

  if (g.forced_kind) {
    const b = document.createElement("button");
    b.className = "j undo kauto";
    b.innerHTML = ICON("undo");
    b.title = `自動判定（${g.auto_kind}）に戻す`;
    b.onclick = () => setKind(g, g.auto_kind);
    frag.appendChild(b);
  }
  return frag;
}

function setKind(g, kind) {
  const key = g.parts[0];        // 結合したブロックでも、種別を決めているのは先頭
  // ⚠️ 位置も**先頭パーツのもの**を使う。サーバー側の照合が結合前のブロックに対して
  //    行われるため（→ core.py「ブロックを1つだけ名指しするための鍵」）
  const at = g.part_boxes[0];
  const same = (r) => r.page === S.page && r.text === key && atEq(r.at, at);
  // ⚠️ 種別を選び直すたびにルールを作り直すので、**付けてあった理由を引き継ぐ**
  //    （引き継がないと、種別を直すたびに理由が消えて記録が虫食いになる）
  const was = (S.st.kinds || []).find(same);
  S.st.kinds = (S.st.kinds || []).filter((r) => !same(r));
  // 自動判定と同じものを選んだら、指定を消すだけ（＝余計なルールを残さない）
  if (kind !== g.auto_kind) {
    S.st.kinds.push({ page: S.page, text: key, at, kind,
                      reason: (was && was.reason) || "" });
  }
  refresh();
}

/** 単位1つぶんの行。
 *  ⚠️ ブロックごと渡すのは、除外ルールが**文言＋ページ＋pt＋位置**で照合されるため。
 *     同じページに同じ文言・同じptのブロックが複数ある（A社 p11 の `KPI` ×3）。 */
function unitRow(u, g) {
  const row = document.createElement("div");
  row.className = "u" + (u.excluded ? " ex" : "");
  const t = document.createElement("span");
  t.className = "ut";
  t.textContent = u.text;
  row.appendChild(t);

  const b = document.createElement("button");
  if (u.excluded) {
    b.className = "x back";
    b.textContent = "戻す";
    b.onclick = () => unexclude(u.text, S.page, g.size, g.bbox);
  } else {
    b.className = "x";
    b.textContent = "×";
    b.title = "この文を除外します";
    b.onclick = () => exclude([u.text], S.page, g.size, g.bbox);
  }
  row.appendChild(b);
  return row;
}

// ---------- 除外の操作 ----------

/** 位置キーの一致。⚠️ 片方が無いルール（古い設定JSON）は「位置を問わない」＝一致とみなす。
 *  許容差は core.AT_TOL と同じ 1.0pt に揃えてある。 */
function atEq(a, b) {
  if (!a || !b) return true;
  return Math.abs(a[0] - b[0]) <= 1 && Math.abs(a[1] - b[1]) <= 1;
}

/** 位置キーは左上だけを持つ（幅・高さは結合で変わるが、始点は変わらない）。 */
const atOf = (bbox) => (bbox ? [Math.round(bbox[0] * 10) / 10,
                                Math.round(bbox[1] * 10) / 10] : null);

function exclude(texts, page, pt, bbox) {
  S.st.excluded = S.st.excluded || [];
  const at = atOf(bbox);
  for (const text of texts) {
    // 既存ルールで既に消えている単位は増やさない。
    // ⚠️ pt / 位置 を持たない古いルールはその条件を問わず効くので、当たっていれば重複扱い
    const dup = S.st.excluded.some((r) => r.text === text &&
      (r.page === null || r.page === page) &&
      (r.pt === null || r.pt === undefined || r.pt === pt) &&
      atEq(r.at, at));
    if (!dup) {
      S.st.excluded.push({ text, page, pt: pt === undefined ? null : pt, at, reason: "" });
    }
  }
  refresh();
}

// ---------- ブロックの結合 ----------
// PDFでは左段の末尾と右段の先頭が完全に別のブロックで、座標からは「続き」だと判定できない。
// （新しい話題なのか続きなのかは、意味を読まないと分からない）→ 人が指定する。

/** 単位を持つ、ひとつ上のブロック（見出しだけの空ブロックは飛ばさない＝見た目通り） */
function prevShown(groups, gid) {
  for (let i = gid - 1; i >= 0; i--) {
    if (groups[i].units.length) return groups[i];
  }
  return null;
}

function join(a, b) {
  S.st.joins = S.st.joins || [];
  if (!S.st.joins.some((r) => r.page === S.page && r.a === a && r.b === b)) {
    S.st.joins.push({ page: S.page, a, b, reason: "" });
  }
  refresh();
}

/** 結合を解除する。そのブロックを作っているルールを全部外す */
function unjoin(parts) {
  const set = new Set(parts);
  S.st.joins = (S.st.joins || []).filter(
    (r) => !(r.page === S.page && set.has(r.a) && set.has(r.b)));
  refresh();
}

/** 除外を取り消す。**その単位に実際に効いているルールだけ**を外す。
 *  ⚠️ pt / 位置 を持たない古いルールはその条件を問わず効いているので、一緒に外す
 *     （残すと「戻したのに消えたまま」になる）。 */
function unexclude(text, page, pt, bbox) {
  const at = atOf(bbox);
  S.st.excluded = (S.st.excluded || []).filter((r) => !(
    r.text === text &&
    (page === undefined || r.page === page) &&
    (pt === undefined || r.pt === null || r.pt === undefined || r.pt === pt) &&
    atEq(r.at, at)));
  refresh();
}

/** 除外したもの一覧。消しっぱなしにせず、常に見えて戻せる状態にしておく。 */
function drawExcluded() {
  const list = S.st.excluded || [];
  const box = $("exList");
  $("exCount").textContent = list.length ? `${list.length}件` : "なし";

  // 手動修正の件数。以前はバッジが3つ並んでいて、見た目が同じなのに
  // クリックできるもの（除外）とできないものが混在していた
  const n = list.length + (S.st.joins || []).length +
            (S.st.kinds || []).length + (S.st.manual_order || []).length +
            (S.st.tables || []).length + (S.st.table_off || []).length +
            (S.st.unit_excludes || []).length + (S.st.unit_merges || []).length;
  // ⚠️ 手順の未確認は件数より先に出す。**やり忘れは、やり過ぎより気づきにくい**
  const todo = S.info ? taskStatus().filter((r) => r.state === "未確認").length : 0;
  const cand = S.info ? (S.info["候補"] || []).filter((c) => !skipSet().has(c.page)).length : 0;
  // サイドバーの「記録」タブに、まだ見ていないもの（手順＋候補）の数を出す
  $("manCnt").textContent = (todo + cand) ? String(todo + cand) : "";
  $("manBadge").title = "手順の進み具合と、手で直した箇所" +
    (todo ? `\n手順 残り${todo}` : "") + (cand ? `\n外す候補 ${cand}` : "") + (n ? `\n手直し ${n}件` : "");
  $("manBadge").classList.toggle("todo", todo > 0);
  $("exWrap").hidden = !list.length;
  box.innerHTML = "";
  for (const r of list) {
    const row = document.createElement("div");
    row.className = "exrow";
    // ⚠️ pt を出さないと、同じ文言のルールが2つ並んだとき見分けられない。
    //    ptまで同じものがある場合（A社 p11 の `KPI` ×3）は座標も出す
    row.innerHTML = `<span class="scope ${r.page === null ? "g" : ""}">` +
      `${r.page === null ? "全ページ" : "p." + r.page}</span>` +
      (r.pt ? `<span class="scope pt">${r.pt}pt</span>` : "") +
      (dupRules(list, r) && r.at
        ? `<span class="scope at">x${Math.round(r.at[0])}</span>` : "") +
      `<span class="ut">${esc(r.text)}</span>`;
    const b = document.createElement("button");
    b.className = "x back";
    b.textContent = "戻す";
    // ⚠️ **このルールだけ**を外す。条件で絞ると、位置違いの同じ文言まで巻き添えになる
    b.onclick = () => {
      S.st.excluded = (S.st.excluded || []).filter((x) => x !== r);
      refresh();
    };
    row.appendChild(b);
    box.appendChild(row);
  }
}

/** 同じ文言・同じページ・同じptのルールが他にもあるか（＝位置まで出さないと見分けられない） */
const dupRules = (list, r) => list.some((x) =>
  x !== r && x.text === r.text && x.page === r.page && x.pt === r.pt);

const esc = (s) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

// ---------- ヘッダー／フッターの線をドラッグ ----------

/** ヘッダー／フッターの線は全ページ共通の設定なので、全ページに描く。 */
function drawGuides(el) {
  const targets = el ? [el] : Object.values(S.boxes || {});
  for (const b of targets) {
    const h = S.info["ページ"][Number(b.dataset.page) - 1].h;
    b.querySelector(".gh").style.top = pct(S.st.header_y ?? 0, h);
    b.querySelector(".gf").style.top = pct(h - (S.st.footer_margin ?? 0), h);
  }
}

/** どのページの線をつまんでも同じ設定が動く。 */
function bindGuides(el) {
  for (const [sel, key] of [[".gh", "header_y"], [".gf", "footer_margin"]]) {
    el.querySelector(sel).addEventListener("mousedown", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const line = el.querySelector(sel);
      line.classList.add("drag");
      const rect = el.getBoundingClientRect();
      const h = S.info["ページ"][Number(el.dataset.page) - 1].h;
      const move = (ev) => {
        const y = (ev.clientY - rect.top) / rect.height * h;
        const v = key === "header_y" ? y : h - y;
        S.st[key] = Math.max(0, Math.round(v * 10) / 10);
        $("p_" + key).value = S.st[key];
        drawGuides();
      };
      const up = () => {
        line.classList.remove("drag");
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        refresh();                       // 離したときだけ再解析する
      };
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });
  }
}

// ---------- ページ操作 ----------

/** そのページまでスクロールする。表示は連続なので「移動」＝スクロール。 */
function go(n, flash = true) {
  n = Math.min(Math.max(1, n), S.pageMax);
  const el = S.boxes[n];
  if (!el) return;
  $("stage").scrollTo({ top: el.offsetTop - 12, behavior: "smooth" });
  setCurrent(n);
  if (flash) {
    el.classList.remove("flash");
    void el.offsetWidth;                     // アニメーションを最初から
    el.classList.add("flash");
  }
}
$("prev").onclick = () => go(S.page - 1);
$("next").onclick = () => go(S.page + 1);
$("pageNo").onchange = () => go(Number($("pageNo").value));
$("zoom").oninput = () => {
  document.documentElement.style.setProperty("--pagew", $("zoom").value + "px");
};
$("zoom").onchange = () => refresh();        // 幅が決まってから画像を取り直す
// ⚠️ 並び順を変えると「隣り合うブロック」が変わるので、結合（joins）の効き方も変わる
$("order").onchange = () => { S.st.order = $("order").value; refresh(); };
$("showBoxes").onchange = () => Object.keys(S.cache).forEach((n) => drawOverlay(Number(n)));
$("dimDropped").onchange = () => Object.keys(S.cache).forEach((n) => drawOverlay(Number(n)));
$("showTables").onchange = () => Object.keys(S.cache).forEach((n) => drawOverlay(Number(n)));

// 「表示 ▾」のポップオーバー。外を押すと閉じる
$("viewBtn").onclick = (e) => {
  e.stopPropagation();
  $("viewMenu").hidden = !$("viewMenu").hidden;
  $("viewBtn").classList.toggle("on", !$("viewMenu").hidden);
};
document.addEventListener("click", (e) => {
  if ($("viewMenu").hidden || e.target.closest(".popwrap")) return;
  $("viewMenu").hidden = true;
  $("viewBtn").classList.remove("on");
});

// 「？ 使い方」。常時出していた説明をダイアログに移した（単位リストの見える件数を増やすため）
$("helpBtn").onclick = () => modal("この画面でできること", el(`<div class="helpbox">
  <h4>ページを見て回る</h4>
  <p><b>一覧</b>（<kbd>T</kbd>）で左にサムネイル。除外したページは赤、外す候補は「？」、表のあるページは下線。
    <kbd>←</kbd> <kbd>→</kbd> でもページを移動できます。</p>
  <h4>ページの判断（ツールバー）</h4>
  <p><b>このページを除外</b> にチェック → 隣で<b>理由</b>を選びます。理由は設定JSONに残り、卒論の付録になります。</p>
  <h4>ブロック（右の一覧）</h4>
  <p><b>⠿</b> を掴んで並べ替え（端に寄せると自動で送ります）／ プルダウンで<b>種別</b>を直す ／
    <b>×</b> でその文を除外 ／ <b>上と結合</b> で段をまたぐ本文を繋ぐ</p>
  <h4>表</h4>
  <p>罫線で区切られた表は自動で<b>1行＝1ブロック</b>（種別 <b class="k-表">表</b>）になります。原本の上では破線の枠。<br>
    図解の枠線を表と誤認しているページ → 右の一覧の上の <b>このページの検出をやめる</b>。<br>
    罫線の無い表 → 原本の上で<b>ドラッグして囲み</b> → <b>表にする</b>（文字の位置から列を推定）。</p>
  <h4>原本の上で囲む</h4>
  <p>ページ画像を<b>ドラッグして囲む</b>と、重なったブロックが選ばれます。
    <b>まとめて並べる</b>＝読み順に並べて1か所に集める ／ <b>表にする</b>。囲んで除外はできません（除外は1件ずつ理由を付ける操作です）。</p>
  <h4>数える単位</h4>
  <p><b>文</b>と<b>ページ</b>の2つです（段落は作りません）。書き出すと <code>文単位_○○.csv</code>・<code>ページ単位_○○.csv</code>・
    KH Coder 用 txt（<code>&lt;h1&gt;文書&lt;/h1&gt;</code> <code>&lt;h2&gt;pN&lt;/h2&gt;</code> ＋ 1行1文）が出ます。
    ブロックの切れ目は文の切れ目になるだけです。</p>
  <h4>文脈窓（ヘッダー、<kbd>F</kbd>）</h4>
  <p>「生成AI」などの検索語を含む文と、その<b>前後N文</b>を一覧します。窓を押すとそのページへ飛び、
    該当ブロックが光ります。<b>書き出す</b>で <code>文脈窓_○○_N2.csv</code>・KH Coder 用 txt（1行1窓）・外部変数CSV が出ます。</p>
  <h4>キー</h4>
  <p><kbd>←</kbd> <kbd>→</kbd> ページ移動 ／ <kbd>T</kbd> サムネイル ／ <kbd>F</kbd> 文脈窓 ／ <kbd>Ctrl</kbd>+<kbd>S</kbd> 保存 ／ <kbd>Esc</kbd> 選択・パネルを閉じる</p>
  <h4>記録（ヘッダー）</h4>
  <p>手順の進み具合・外す候補・手で直した箇所のすべて。そのまま設定JSONに残り、卒論の付録になります。</p>
</div>`), [{ label: "閉じる", kind: "ghost" }]);

document.addEventListener("keydown", (e) => {
  if (["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) return;
  if (e.key === "ArrowLeft") go(S.page - 1);
  if (e.key === "ArrowRight") go(S.page + 1);
  if ((e.key === "t" || e.key === "T") && S.name && $("modal").hidden) showTab("pages", true);
  if (e.key === "Escape" && $("modal").hidden) {
    if (S.pick) clearPick();                                   // 手前にあるものから
    else if (S.sbTab !== "pages") showTab("pages");
  }
  if ((e.key === "f" || e.key === "F") && S.name && $("modal").hidden) showTab("ctx", true);
  if ((e.key === "u" || e.key === "U") && S.name && $("modal").hidden) showTab("units", true);
});

// ---------- 除外ページ ----------
// ⚠️ 2026-08-12 に `skip_pages` を「番号のリスト」から `[{page, reason}]` に変えた。
//    番号だけだと、後から見て p45 が章扉だったのか判断ミスだったのか分からない。
//    理由は core.TASKS のキー。空欄のままでも動くが、手順一覧に「未設定」として出る。

const skipSet = () => new Set((S.st.skip_pages || []).map((r) => r.page));
const skipReason = (p) =>
  ((S.st.skip_pages || []).find((r) => r.page === p) || {}).reason || "";

/** 除外を変えた後の後始末。⚠️ refresh() は重いので、まとめて操作するときは最後に1回だけ。 */
function afterSkipChange() {
  (S.st.skip_pages || []).sort((a, b) => a.page - b.page);
  syncReasonPicker();
  updateSkipUI();
  markPageList();
  refresh();          // 右パネルの「除外ページです」の断りを、その場で出し入れするため
}

/** 除外の切り替え。理由は引き継ぐ（外す→戻す→また外す で消えると腹立たしいので）。
 *  `auto` は「機械の候補をそのまま採用した」印（→ core.suggest_skips）。人が理由を選び直したら消える。 */
function toggleSkip(page, on, reason, auto) {
  const cur = S.st.skip_pages || [];
  const was = cur.find((r) => r.page === page);
  const rest = cur.filter((r) => r.page !== page);
  if (on) {
    const r = { page, reason: reason !== undefined ? reason : ((was && was.reason) || "") };
    if (auto || (reason === undefined && was && was.auto)) r.auto = true;
    rest.push(r);
  }
  S.st.skip_pages = rest;
  afterSkipChange();
}

/** 自動候補をまとめて採用する（記録パネルの「全部採用」）。 */
function adoptCandidates(list) {
  const cur = (S.st.skip_pages || []).filter((r) => !list.some((c) => c.page === r.page));
  for (const c of list) cur.push({ page: c.page, reason: c.reason, auto: true });
  S.st.skip_pages = cur;
  afterSkipChange();
}

/** 複数ページにまとめて理由を付ける。巻末のデータ集のように十数ページ続くことがあるので、
 *  1ページずつ選ばせない（そこが面倒だと、理由を付けること自体が後回しになる）。 */
function setSkipReasons(pages, reason) {
  const set = new Set(pages);
  S.st.skip_pages = (S.st.skip_pages || [])
    .map((r) => (set.has(r.page) ? { page: r.page, reason } : r));
  afterSkipChange();
}

/** 複数ページの除外をまとめてやめる。 */
function unskipPages(pages) {
  const set = new Set(pages);
  S.st.skip_pages = (S.st.skip_pages || []).filter((r) => !set.has(r.page));
  afterSkipChange();
}

$("skipThis").onchange = (e) => toggleSkip(S.page, e.target.checked);

// ツールバーの理由プルダウン。**除外チェックのすぐ隣**に置いてある。
// ⚠️ 理由を別の画面で後からまとめて付ける形にすると、そのページを見ていないときに
//    思い出しながら選ぶことになる。原本が目の前にある「今」選ぶのが一番正確。
function fillReasonPicker() {
  const sel = $("skipReason");
  sel.innerHTML = "";
  sel.appendChild(el("<option value=''>理由を選ぶ…</option>"));
  for (const t of ((S.info && S.info["手順"]) || [])) {
    const o = document.createElement("option");
    o.value = t.key; o.textContent = t.label;
    sel.appendChild(o);
  }
}

function syncReasonPicker() {
  const sel = $("skipReason");
  const on = skipSet().has(S.page);
  sel.hidden = !on;
  if (!on) return;
  sel.value = skipReason(S.page);
  sel.classList.toggle("unset", !sel.value);
}

$("skipReason").onchange = () => toggleSkip(S.page, true, $("skipReason").value);

function updateSkipUI() {
  const list = S.st.skip_pages || [];
  const noReason = list.filter((r) => !r.reason).length;
  $("skipCount").textContent = list.length
    ? `${list.length}ページを除外中: ${list.map((r) => r.page).join(", ")}` +
      (noReason ? `　うち${noReason}件は理由が未設定` : "")
    : "";
}

$("loadPages").onclick = () => loadPageList(true);

/** ページ一覧。手順一覧からも見出しを引きたいので、結果を S.pages に残す。 */
async function loadPageList(force) {
  if (S.pages && !force) return S.pages;
  $("loadPages").textContent = "読み込み中…";
  const rows = await api(`/api/doc/${encodeURIComponent(S.name)}/pages`, { settings: S.st });
  S.pages = rows;
  $("loadPages").textContent = "ページ一覧を読み直す";
  const wrap = $("pageList");
  wrap.innerHTML = "";
  for (const r of rows) {
    const b = document.createElement("button");
    b.textContent = r["ページ"];
    b.title = `${r["見出し"] || "(見出しなし)"}\n` +
              KINDS.filter((k) => r[k]).map((k) => `${k}${r[k]}`).join(" ");
    b.dataset.page = r["ページ"];
    if (r["表数"] > 0) b.classList.add("tbl");
    if (S.ctx && S.ctx["ページ別"] && S.ctx["ページ別"][r["ページ"]]) b.classList.add("hit");
    if (r["候補"]) b.title += `\n自動候補：${r["候補"]}（${r["候補の根拠"]}）`;
    b.onclick = (e) => {
      if (e.shiftKey) {                       // Shift+クリックで除外の切り替え
        const on = !skipSet().has(r["ページ"]);
        toggleSkip(r["ページ"], on);
        if (r["ページ"] === S.page) $("skipThis").checked = on;
      } else go(r["ページ"]);
    };
    wrap.appendChild(b);
  }
  markPageList();
  markThumbs();             // 表のあるページの印は、ページ一覧を読んで初めて付けられる
  return rows;
}

/** ページ番号 → 見出し。読み込み前は空。 */
const headingOf = (p) => {
  const r = (S.pages || []).find((x) => x["ページ"] === p);
  return r ? (r["見出し"] || "") : "";
};

function markPageList() {
  const skip = skipSet();
  const cand = {};
  for (const c of ((S.info && S.info["候補"]) || [])) cand[c.page] = c.reason;
  document.querySelectorAll("#pageList button").forEach((b) => {
    const p = Number(b.dataset.page);
    b.classList.toggle("skip", skip.has(p));
    b.classList.toggle("cur", p === S.page);
    b.classList.toggle("cand", !!cand[p] && !skip.has(p));
  });
}

// ---------- 保存・書き出し ----------

$("saveBtn").onclick = saveSettings;

// 書き出し。既存ファイルがあれば、上書きするか別名で残すかを選ばせる。
// 設定を変えて書き出し → KH Coder で見て → また変えて…を繰り返すので、
// **前の版を潰したかどうかが分からないと比較にならない。**
$("exportBtn").onclick = () => askUnfinished(() => runExport({}));

/** 手順が片付いていなければ、書き出す前に知らせる。
 *
 * ⚠️ **止めはしない。** 途中の状態で試しに書き出すのは普通の使い方で、
 *    そこで止めると「確認を無視する癖」がつく。**気づかずに通り過ぎるのを防ぐだけ。**
 */
function askUnfinished(next) {
  const rows = taskStatus();
  const must = rows.filter((r) => r.must && r.state === "未確認");
  const other = rows.filter((r) => !r.must && r.state === "未確認");
  const orphan = (S.st.skip_pages || []).filter((r) => !r.reason);
  if (!must.length && !other.length && !orphan.length) return next();

  const li = (title, items) => items.length
    ? `<p class="note"><b>${title}</b>：${items.join(" ／ ")}</p>` : "";
  const body = el(
    `<p class="note">このまま書き出せます。ただし<b>前処理の手順がまだ残っています</b>。
      他社と同じ手順を踏んだと言うには、ここを埋めておく必要があります。</p>` +
    li("未確認（外すのが既定）", must.map((r) => r.label)) +
    li("未確認（見て判断する）", other.map((r) => r.label)) +
    li("理由が未設定の除外ページ", orphan.map((r) => `p.${r.page}`)) +
    `<p class="note">記録は設定JSONに残り、そのまま<b>卒論の付録</b>になります。</p>`);

  modal("手順がまだ残っています", body, [
    { label: "記録を開く", run: showManual },
    { label: "このまま書き出す", kind: "ghost", run: next },
    { label: "やめる", kind: "ghost" },
  ]);
}

async function runExport(opts) {
  toast("書き出しています…（文書全体を解析中）", "busy");
  try {
    const j = await api(`/api/doc/${encodeURIComponent(S.name)}/export`,
                        { settings: S.st, ...opts });
    S.saved = JSON.stringify(S.st);      // 書き出しは設定JSONも保存する（＝未保存ではなくなる）
    syncSaveState();
    const skip = (S.st.skip_pages || []).length;
    toast(`書き出しました：${j["単位数"].toLocaleString()}文` +
      `（${KINDS.map((k) => `${k}${j["内訳"][k] || 0}`).join(" ")}）` +
      ` ／ ${j["文字数"].toLocaleString()}字 ／ ${j["ページ単位数"].toLocaleString()}ページ` +
      (skip ? `\n${skip}ページを除外した結果です` : "") +
      (ENV["公開モード"] ? "" :
        `\n${j.csv}\n${j.page}\n${j.txt}\n設定JSONも一緒に保存しました` +
        (j["退避"] ? `\n前の設定は ${j["退避"]} に退避しました` : "")),
      "ok",
      { "文単位CSV": j["落とす"].csv, "ページ単位CSV": j["落とす"].page,
        "txtを落とす": j["落とす"].txt });
  } catch (e) {
    if (e.status === 409) { askOverwrite(e.data); return; }
    toast(String(e.message || e), "err");
  }
}

function askOverwrite(d) {
  $("toast").hidden = true;
  const body = el(`
    <p class="note">同じ名前のファイルが既にあります。上書きすると<b>前の結果は戻せません</b>。</p>
    <div class="filelist">${d["既存"].map((f) =>
      `<div class="f"><b>${esc(f.name)}</b><span>${f["更新"]}</span><span>${f.mb} MB</span></div>`
    ).join("")}</div>
    <label class="param lbl">別名で残すときの目印
      <input id="expLabel" placeholder="例: 除外なし" maxlength="24"></label>
    <p class="hint" id="expPreview"></p>
    <p class="note" style="margin:10px 0 0">
      前の版と比べたいときは<b>別名</b>で残してください。KH Coder には両方読ませられます。</p>`);
  modal("同じ名前のファイルがあります", body, [
    { label: "別名で残す", id: "expSaveAs",
      run: () => runExport({ label: $("expLabel").value.trim() }) },
    { label: "上書きする", kind: "danger", run: () => runExport({ overwrite: true }) },
    { label: "やめる", kind: "ghost" },
  ]);

  // 目印を入れるまで「残す」は押せない（空だと上書きと同じ結果になってしまうため）
  const stem = d["候補"].replace(/\.csv$/, "");
  const sync = () => {
    const v = $("expLabel").value.trim();
    $("expSaveAs").disabled = !v;
    $("expPreview").textContent = v ? `→ ${stem}_${v}.csv` : "";
  };
  $("expLabel").oninput = sync;
  sync();
  $("expLabel").focus();
}

// ---------- 読み込み中の表示 ----------
// PDFを開くときは全ページを走査するので数秒〜十数秒かかる。その間なにも出ないと
// 「押せていない」と思って二度押しする。必ず出す（→ #loading）

function showLoading(msg, sub, withBar = false) {
  $("loadMsg").textContent = msg || "読み込んでいます…";
  $("loadSub").textContent = sub || "";
  $("loadBar").hidden = !withBar;
  $("loadPct").hidden = !withBar;
  $("loadStep").hidden = true;
  if (withBar) { $("loadBarFill").style.width = "0%"; $("loadPct").textContent = "0%"; }
  $("loading").hidden = false;
}
const setLoading = (msg, sub) => {
  $("loadMsg").textContent = msg;
  if (sub !== undefined) $("loadSub").textContent = sub;
};
const hideLoading = () => { $("loading").hidden = true; };

// ---------- ジョブ（時間のかかる処理。進捗％と「いま何をしているか」を出す） ----------
// サーバー側は POST /api/job で始めて GET /api/job で進捗を返す（→ ui/app.py のジョブ）。
// 未解析の文書の一括解析・確認モードの入口・Excelプレビュー・全冊書き出しが全部ここを通る。

function setLoadProgress(j) {
  $("loadBarFill").style.width = (j.percent || 0) + "%";
  $("loadPct").textContent = (j.percent || 0) + "%";
  $("loadStep").hidden = !j.step;
  $("loadStep").textContent = j.step || "";
  $("loadSub").textContent = j.detail || "";
}

async function runJob(params, title) {
  try {
    await api("/api/job", params);
  } catch (e) {
    if (e.status === 409) throw new Error("別の処理が実行中です。終わってからもう一度どうぞ");
    throw e;
  }
  showLoading(title, "", true);
  try {
    for (;;) {
      const j = await api("/api/job");
      if (j.state === "running") setLoadProgress(j);
      else if (j.state === "done") {
        // 解析に失敗した文書があっても止めない（結果は残りの文書ぶん）。ただし黙らない
        const errs = j.result && j.result["エラー"];
        if (errs) toast("解析に失敗した文書があります：" +
          Object.entries(errs).map(([k, v]) => `${k}（${v}）`).join(" ／ "), "err");
        return j.result;
      } else {
        throw new Error(j.error || "処理に失敗しました");
      }
      await new Promise((r) => setTimeout(r, 400));
    }
  } finally {
    hideLoading();
  }
}

// ---------- 文脈窓（2026-08-22 夜に追加） ----------
// 「生成AI」を含む文の前後N文を1つの窓にして一覧する。文（狭い）とページ（広い）の間を
// 固定幅で見るための補助（→ core.context_windows）。文書全体を解析するので初回は時間がかかる。
// サーバー側は設定ごとに結果を持つ（同じ設定なら2回目以降は一瞬）。

const CTX_EMPTY = `<p class="note empty">「解析する」を押すと、文書全体を解析して一覧します。<br>
  <span class="hint">初回は数秒〜数十秒かかります（設定を変えると解析し直し）。</span></p>`;

function openCtx(on) {
  const show = on === undefined ? $("ctxPane").hidden : on;
  if (show) showTab("ctx");
  else if (S.sbTab === "ctx") showTab("pages");
}
$("ctxRun").onclick = () => runCtx();
$("ctxKw").onkeydown = (e) => { if (e.key === "Enter") runCtx(); };
$("ctxN").onchange = () => runCtx();
$("ctxExport").onclick = () => exportCtx();

const ctxKeywords = () => $("ctxKw").value.split(/[,、\n]/).map((s) => s.trim()).filter(Boolean);
const ctxKey = () => JSON.stringify([S.st, $("ctxN").value, ctxKeywords()]);

async function runCtx() {
  if (!S.name) return;
  const body = $("ctxBody");
  body.innerHTML = `<div class="skel-note">文書全体を解析しています…（初回は数十秒かかることがあります）</div>` +
    [3, 2, 3].map((k) => `<div class="skel">${"<div class='ln'></div>".repeat(k)}<div class="ln s"></div></div>`).join("");
  $("ctxRun").disabled = true;
  $("ctxRun").textContent = "解析中…";
  const key = ctxKey();
  try {
    const j = await api(`/api/doc/${encodeURIComponent(S.name)}/context`,
                        { settings: S.st, n: Number($("ctxN").value), keywords: ctxKeywords() });
    if (ctxKey() !== key) return;            // 待っている間に条件が変わった（新しい方が後で返る）
    S.ctx = j;
    S.ctxKey = key;
    drawCtx(j);
    markThumbs();
    markPageList();
  } catch (e) {
    body.innerHTML = `<p class="note empty">解析に失敗しました：${esc(String(e.message || e))}</p>`;
  } finally {
    $("ctxRun").disabled = false;
    $("ctxRun").textContent = "解析する";
  }
}

/** 検索語をハイライトする正規表現（サーバーと同じく、英字だけの語は単語として当てる） */
function ctxRegex(words) {
  const parts = words.map((k) => {
    const e = k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return /^[A-Za-z0-9 .\-]+$/.test(k) ? `(?<![A-Za-z])${e}(?![A-Za-z])` : e;
  });
  return parts.length ? new RegExp(parts.join("|"), "gi") : null;
}

function drawCtx(j) {
  const body = $("ctxBody");
  body.innerHTML = "";
  const wins = j["窓"] || [];
  $("ctxCount").textContent = wins.length ? `${j["ヒット数"]}文 → ${wins.length}窓` : "0件";
  $("ctxExport").disabled = !wins.length;
  const kw = j["検索語"] || [];
  const rx = ctxRegex(kw);
  const hl = (s) => rx ? esc(s).replace(rx, (m) => `<mark>${m}</mark>`) : esc(s);
  body.appendChild(el(`<p class="ctx-sum"><span>検索語 <b>${esc(kw.join(" / "))}</b></span>
    <span>前後 <b>${j.n}</b> 文</span><span>全 <b>${Number(j["文数"]).toLocaleString()}</b> 文中</span></p>`));
  if (!wins.length) {
    body.appendChild(el(`<p class="note empty">この文書に検索語を含む文はありませんでした。<br>
      <span class="hint">検索語を増やす（例：AI, DX）か、前後の文数を変えて「解析する」。</span></p>`));
    return;
  }
  for (const w of wins) {
    const card = document.createElement("div");
    card.className = "win";
    card.title = `p.${w["ページ"]} へ飛ぶ`;
    const pages = w["ページまたぎ"] ? `p.${w["ページ"]}〜${w["最終ページ"]}` : `p.${w["ページ"]}`;
    card.appendChild(el(`<div class="win-head"><span class="pg">${pages}</span>
      <span class="kw">${esc(w["ヒット語"])}</span>
      <span class="cnt">${w["ヒット数"]}ヒット ／ ${w["文数"]}文</span></div>`));
    const b = document.createElement("div");
    b.className = "win-body";
    let lastPage = null;
    for (const s of w["文"]) {
      const sp = document.createElement("span");
      sp.className = "s" + (s.hit ? " hit" : "");
      // ページをまたぐ窓では、ページが変わる所に小さな印
      sp.innerHTML = (w["ページまたぎ"] && s["ページ"] !== lastPage ? `<span class="pb">p.${s["ページ"]}</span>` : "") + hl(s["文"]);
      lastPage = s["ページ"];
      sp.title = "クリックでこの文のブロックへ";
      sp.onclick = (e) => { e.stopPropagation(); jumpToSentence(s["ページ"], s["文"]); };
      b.appendChild(sp);
    }
    card.appendChild(b);
    card.onclick = () => jumpToSentence(w["ページ"], (w["文"].find((s) => s.hit) || w["文"][0])["文"]);
    body.appendChild(card);
  }
}

/** そのページへ飛び、その文を含むブロックを光らせる（解析が返るのを少し待つ） */
function jumpToSentence(page, text) {
  go(page);
  let tries = 0;
  const tick = () => {
    const d = S.cache[page];
    if (!d) { if (tries++ < 120) setTimeout(tick, 200); return; }   // 解析待ち（最長24秒）
    const g = d.groups.find((x) => x.units.some((u) => u.text === text));
    if (!g) return;
    if (S.page !== page) return;
    const row = $("unitList").querySelector(`.grp[data-gid="${g.gid}"]`);
    if (row) {
      row.scrollIntoView({ block: "center", behavior: "smooth" });
      row.classList.remove("flash"); void row.offsetWidth; row.classList.add("flash");
    }
    hl(g.gid, true);
    setTimeout(() => hl(g.gid, false), 1400);
  };
  setTimeout(tick, 200);
}

/** 設定を変えると窓は古くなる。黙って古い一覧を見せない */
function markCtxStale() {
  if (!S.ctx || $("ctxPane").hidden) return;
  if (ctxKey() === S.ctxKey) return;
  if ($("ctxBody").querySelector(".stale")) return;
  const w = el(`<div class="stale">設定が変わりました。この一覧は前の設定のものです。
    <button class="mini">解析し直す</button></div>`);
  w.querySelector("button").onclick = () => runCtx();
  $("ctxBody").prepend(w);
}

async function exportCtx() {
  if (!S.name || !S.ctx) return;
  toast("文脈窓を書き出しています…", "busy");
  try {
    const j = await api(`/api/doc/${encodeURIComponent(S.name)}/context/export`,
                        { settings: S.st, n: Number($("ctxN").value), keywords: ctxKeywords() });
    toast(`文脈窓 ${j["窓数"]}件（${j["ヒット数"]}ヒット）を書き出しました` +
          (ENV["公開モード"] ? "" : `\n${j.csv}\n${j.txt}\n${j.vars}`), "ok",
          { "CSV": j["落とす"].csv, "KH Coder用txt": j["落とす"].txt, "外部変数CSV": j["落とす"].vars });
  } catch (e) {
    toast(String(e.message || e), "err");
  }
}

// ---------- 抽出単位（L2。2026-08-25 追加） ----------
// 検索語のヒット箇所を類型規則で単位化して一覧する（→ core.extract_units）。
// 規則：本文→その文 ／ 表→その行 ／ ラベル（小・極小）→その文（手で結合できる）／ 見出し→その行。
// 手作業は「足す」（unit_merges）と「外す」（unit_excludes）の2つだけで、全部 S.st に溜まり、
// 保存すると設定JSONに残る（＝再生すれば同一の出力＝卒論3.5節の監査記録そのもの）。

const UNIT_EMPTY = `<p class="note empty">検索語のヒット箇所を規則で単位化します：本文→その文 ／ 表→その行 ／ ラベル→その文（結合できる）／ 見出し→その行。<br>
  <span class="hint">薄い文をクリックすると単位に足せます。単位ごとに「外す」で理由を付けて除外できます。手作業はすべて設定JSONに残ります。</span></p>`;

const extractKeywords = () => $("unitKw").value.split(/[,、\n]/).map((s) => s.trim()).filter(Boolean);
const extractKey = () => JSON.stringify([S.st, extractKeywords()]);

$("unitRun").onclick = () => runExtract();
$("unitKw").onkeydown = (e) => { if (e.key === "Enter") runExtract(); };
$("unitExport").onclick = () => exportExtract();
$("unitTable").onclick = () => showUnitTable([S.name]);

// 検索語をグローバルの既定として保存する（設定\検索語.json。バッチ・文脈窓も同じ語を読む）
$("unitKwSave").onclick = () => {
  const kws = extractKeywords();
  if (!kws.length) { toast("検索語欄に語を入れてから押してください（カンマ区切り）", "err"); return; }
  const body = el(`<div><p class="note">この検索語を<b>全文書共通の既定</b>として保存します。
    バッチ（pdf2txt.py）・文脈窓も同じ語で抽出するようになります。<br>
    🔴 検索語は<b>全文書・全時点で共通</b>が原則です（片方だけ変えると比較が壊れます）。
    変えたら卒論の表3.3も更新してください。</p>
    <p><code>${esc(kws.join(" / "))}</code></p></div>`);
  modal("検索語を既定として保存しますか？", body, [
    { label: "保存する", kind: "primary", run: async () => {
        const j = await api("/api/keywords", { keywords: kws });
        if (S.info) S.info["検索語"] = j.keywords;
        toast("検索語を保存しました（設定\\検索語.json）", "ok");
        runExtract();
      } },
    { label: "やめる", kind: "ghost" },
  ]);
};

/** ホバーで、原本の該当ブロックを光らせる（そのページが描画済みのときだけ）。 */
function hoverSentence(page, text, on) {
  const d = S.cache && S.cache[page];
  if (!d) return;
  const g = d.groups.find((x) => x.units.some((u) => u.text === text));
  if (!g) return;
  document.querySelectorAll(`.ov .box[data-gid="${g.gid}"][data-pg="${page}"]`)
    .forEach((e) => e.classList.toggle("hl", on));
}

async function runExtract() {
  if (!S.name) return;
  const body = $("unitBody");
  body.innerHTML = `<div class="skel-note">文書全体を解析しています…（初回は数十秒かかることがあります）</div>` +
    [3, 2, 3].map((k) => `<div class="skel">${"<div class='ln'></div>".repeat(k)}<div class="ln s"></div></div>`).join("");
  $("unitRun").disabled = true;
  $("unitRun").textContent = "解析中…";
  const key = extractKey();
  try {
    const j = await api(`/api/doc/${encodeURIComponent(S.name)}/units`,
                        { settings: S.st, keywords: extractKeywords() });
    if (extractKey() !== key) return;           // 待っている間に条件が変わった
    S.extract = j;
    S.extractKey = key;
    drawExtract(j);
  } catch (e) {
    body.innerHTML = `<p class="note empty">解析に失敗しました：${esc(String(e.message || e))}</p>`;
  } finally {
    $("unitRun").disabled = false;
    $("unitRun").textContent = "解析する";
  }
}

/** 単位の照合キー（アンカー＝ヒット文とそのページ）。手作業のルールはこれで名指しする */
const unitAnchor = (u) => {
  const a = (u["文"] || []).find((s) => s.i === u.anchor) || u["文"][0];
  return { page: a["ページ"], text: a["文"] };
};

function unitMergeRule(u, create) {
  S.st.unit_merges = S.st.unit_merges || [];
  const a = unitAnchor(u);
  let r = S.st.unit_merges.find((m) => m.page === a.page && m.hit === a.text);
  if (!r && create) {
    r = { page: a.page, hit: a.text, add: [], reason: "" };
    S.st.unit_merges.push(r);
  }
  return r;
}

function afterUnitEdit() {
  syncSaveState();
  if (!$("taskPane").hidden) showManual();
  runExtract();
}

function drawExtract(j) {
  const body = $("unitBody");
  body.innerHTML = "";
  const units = j["単位"] || [];
  const nAdopt = j["採用数"];
  $("unitCount").textContent = units.length
    ? `${units.length}単位（採用 ${nAdopt}）` : "0件";
  $("unitExport").disabled = !units.length;
  $("unitTable").disabled = !units.length;
  const kw = j["検索語"] || [];
  const rx = ctxRegex(kw);
  const hl = (s) => rx ? esc(s).replace(rx, (m) => `<mark>${m}</mark>`) : esc(s);
  const exChoices = ((S.info && S.info["操作の理由"]) || {}).unit_excludes || [];
  const mgChoices = ((S.info && S.info["操作の理由"]) || {}).unit_merges || [];

  body.appendChild(el(`<p class="ctx-sum"><span>検索語 <b>${esc(kw.join(" / "))}</b></span>
    <span>全 <b>${Number(j["文数"]).toLocaleString()}</b> 文中</span></p>`));
  if (!units.length) {
    body.appendChild(el(`<p class="note empty">この文書に検索語を含む文はありませんでした。</p>`));
    return;
  }

  for (const u of units) {
    const excluded = !!u["採用"];
    const card = document.createElement("div");
    card.className = "win unit" + (excluded ? " off" : "");
    const pages = u["ページ"] !== u["最終ページ"] ? `p.${u["ページ"]}〜${u["最終ページ"]}` : `p.${u["ページ"]}`;
    card.appendChild(el(`<div class="win-head"><span class="pg">${pages}</span>
      <span class="rulechip" title="単位化の規則">${esc(u["規則"])}</span>
      <span class="kw">${esc(u["ヒット語"])}</span>
      <span class="cnt">${u["文数"]}文</span>
      ${u["手作業"] ? `<span class="rtag" title="手で文を足した単位">結合</span>` : ""}
      ${excluded ? `<span class="rtag off" title="${esc(u["除外理由"] || "")}">除外${u["除外理由"] ? "：" + esc(u["除外理由"]) : ""}</span>` : ""}
      </div>`));

    const b = document.createElement("div");
    b.className = "win-body";
    const mkSent = (s, mode) => {
      // mode: "in"＝単位の中 ／ "near"＝近傍（クリックで足す）
      const sp = document.createElement("span");
      const inMerge = (unitMergeRule(u) && (unitMergeRule(u).add || []).includes(s["文"]));
      sp.className = "s" + (s.hit ? " hit" : "") + (mode === "near" ? " near" : "") + (inMerge ? " added" : "");
      sp.innerHTML = hl(s["文"]);
      if (mode === "near") {
        sp.title = "クリックでこの文を単位に足す";
        sp.onclick = (e) => {
          e.stopPropagation();
          const r = unitMergeRule(u, true);
          if (!r.add.includes(s["文"])) r.add.push(s["文"]);
          afterUnitEdit();
        };
      } else if (inMerge) {
        sp.title = "手で足した文。クリックで外す";
        sp.onclick = (e) => {
          e.stopPropagation();
          const r = unitMergeRule(u);
          r.add = (r.add || []).filter((t) => t !== s["文"]);
          if (!r.add.length) S.st.unit_merges = S.st.unit_merges.filter((m) => m !== r);
          afterUnitEdit();
        };
      } else {
        sp.title = "クリックでこの文のブロックへ";
        sp.onclick = (e) => { e.stopPropagation(); jumpToSentence(s["ページ"], s["文"]); };
      }
      // ホバーで原本の該当ブロックが光る（描画済みのページのみ。クリックで飛べば必ず光る）
      sp.onmouseenter = () => hoverSentence(s["ページ"], s["文"], true);
      sp.onmouseleave = () => hoverSentence(s["ページ"], s["文"], false);
      return sp;
    };
    for (const s of u["前"] || []) b.appendChild(mkSent(s, "near"));
    for (const s of u["文"] || []) b.appendChild(mkSent(s, "in"));
    for (const s of u["後"] || []) b.appendChild(mkSent(s, "near"));
    card.appendChild(b);

    // 操作行：外す（理由つき）／戻す
    const bar = document.createElement("div");
    bar.className = "bulkbar";
    if (!excluded) {
      const sel = document.createElement("select");
      sel.className = "treason";
      sel.appendChild(el("<option value=''>外す（理由を選ぶ）…</option>"));
      for (const c of exChoices) {
        const op = document.createElement("option");
        op.value = c.key; op.textContent = c.label;
        if (c.note) op.title = c.note;
        sel.appendChild(op);
      }
      sel.onclick = (e) => e.stopPropagation();
      sel.onchange = () => {
        if (!sel.value) return;
        const a = unitAnchor(u);
        S.st.unit_excludes = S.st.unit_excludes || [];
        S.st.unit_excludes.push({ page: a.page, text: a.text, reason: sel.value });
        afterUnitEdit();
      };
      bar.appendChild(sel);
    } else {
      const back = document.createElement("button");
      back.className = "x back";
      back.textContent = "採用に戻す";
      back.onclick = (e) => {
        e.stopPropagation();
        const a = unitAnchor(u);
        S.st.unit_excludes = (S.st.unit_excludes || []).filter(
          (r) => !(r.page === a.page && r.text === a.text));
        afterUnitEdit();
      };
      bar.appendChild(back);
    }
    if (u["手作業"]) {
      const r = unitMergeRule(u);
      const sel = document.createElement("select");
      sel.className = "treason";
      sel.appendChild(el(`<option value=''>${r && r.reason ? "結合の理由：" + esc(r.reason) : "結合の理由を選ぶ…"}</option>`));
      for (const c of mgChoices) {
        const op = document.createElement("option");
        op.value = c.key; op.textContent = c.label;
        if (c.note) op.title = c.note;
        sel.appendChild(op);
      }
      sel.onclick = (e) => e.stopPropagation();
      sel.onchange = () => {
        if (!sel.value || !r) return;
        r.reason = sel.value;
        syncSaveState();
        if (!$("taskPane").hidden) showManual();
        drawExtract(S.extract);
      };
      bar.appendChild(sel);
    }
    card.appendChild(bar);

    card.onclick = () => {
      const s = (u["文"] || []).find((x) => x.hit) || (u["文"] || [])[0];
      if (s) jumpToSentence(s["ページ"], s["文"]);
    };
    body.appendChild(card);
  }
}

/** 設定・手作業が変わると一覧は古くなる。黙って古いものを見せない */
function markExtractStale() {
  if (!S.extract || $("unitPane").hidden) return;
  if (extractKey() === S.extractKey) return;
  if ($("unitBody").querySelector(".stale")) return;
  const w = el(`<div class="stale">設定が変わりました。この一覧は前の設定のものです。
    <button class="mini">解析し直す</button></div>`);
  w.querySelector("button").onclick = () => runExtract();
  $("unitBody").prepend(w);
}

async function exportExtract() {
  if (!S.name || !S.extract) return;
  toast("抽出単位を書き出しています…", "busy");
  try {
    const j = await api(`/api/doc/${encodeURIComponent(S.name)}/units/export`,
                        { settings: S.st, keywords: extractKeywords() });
    S.saved = JSON.stringify(S.st);       // 書き出しは設定も一緒に保存する（サーバー側）
    syncSaveState();
    toast(`抽出単位 ${j["単位数"]}件（採用 ${j["採用数"]}）を書き出しました` +
          (ENV["公開モード"] ? "" : `\n${j.csv}\n${j.xlsx}`), "ok",
          { "CSV（全件）": j["落とす"].csv, "KH Coder用xlsx（採用のみ）": j["落とす"].xlsx });
  } catch (e) {
    toast(String(e.message || e), "err");
  }
}

// ---------- サイドバー（ページ／設定／記録／文脈窓／抽出） ----------
// 左の列は1本。タブで中身を切り替える。出たり消えたりする横のパネルをやめ、置き場所を固定した
const SB_TABS = { pages: "thumbs", settings: "drawer", record: "taskPane", ctx: "ctxPane", units: "unitPane" };
function showTab(name, toggle = false) {
  if (!SB_TABS[name]) name = "pages";
  const collapsed = $("work").classList.contains("nosb");
  if (toggle && S.sbTab === name && !collapsed) { collapseSidebar(true); return; }
  collapseSidebar(false);
  S.sbTab = name;
  for (const [k, id] of Object.entries(SB_TABS)) $(id).hidden = k !== name;
  for (const b of document.querySelectorAll(".sbtabs button")) b.classList.toggle("on", b.dataset.tab === name);
  try { localStorage.setItem("sbTab", name); } catch (e) { /* noop */ }
  if (name === "pages") scrollThumbIntoView(S.page);
  if (name === "record") showManual();
  if (name === "ctx" && !S.ctx) runCtx();
  if (name === "ctx") markCtxStale();
  if (name === "units" && !S.extract) runExtract();
  if (name === "units") markExtractStale();
}
function collapseSidebar(on) {
  $("work").classList.toggle("nosb", on);
  $("sbToggle").classList.toggle("on", !on);
  try { localStorage.setItem("sbOpen", on ? "0" : "1"); } catch (e) { /* noop */ }
}
for (const b of document.querySelectorAll(".sbtabs button")) b.onclick = () => showTab(b.dataset.tab, true);
$("sbToggle").onclick = () => collapseSidebar(!$("work").classList.contains("nosb"));

loadEnv();
loadDocs();

// ---------- 外観（自動／ライト／ダーク） ----------
// 既定は「自動」＝ブラウザ／OSの設定に従う（Apple のアプリと同じ）。選んだ値は localStorage に残る。
// 描画前の適用は index.html の <head> のスクリプトがやっている（ちらつき防止）
function applyTheme(t) {
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
  else { delete document.documentElement.dataset.theme; t = "auto"; }
  try { localStorage.setItem("theme", t); } catch (e) { /* プライベートモードなど */ }
  for (const b of $("themeSeg").querySelectorAll("button")) b.classList.toggle("on", b.dataset.theme === t);
}
for (const b of $("themeSeg").querySelectorAll("button")) b.onclick = () => applyTheme(b.dataset.theme);
applyTheme((() => { try { return localStorage.getItem("theme") || "auto"; } catch (e) { return "auto"; } })());

// ---------- Excelプレビュー（2026-08-25） ----------
// **実際にExcelにできる行と列**をそのまま表で見せる（→ /api/units/table ＝ 書き出しと同じ経路）。
// 「書き出したら思っていた形と違った」を書き出す前に潰すためのもの。

async function showUnitTable(docs) {
  try {
    const j = await runJob({ kind: "table", docs: docs || null },
      "Excelプレビューを組み立てています…");
    const box = document.createElement("div");
    box.appendChild(el(`<p class="note">KH Coder に読ませる xlsx はこの表の<b>「採用＝○」の行だけ</b>です
      （打ち消しの行＝除外は、監査記録として 抽出単位.csv にのみ残ります）。<br>
      全 <b>${j.rows.length}</b> 行 ／ 採用 <b>${j["採用数"]}</b> 行 ／ <b>${j.cols.length}</b> 列。集計単位は KH Coder 側で「H5」（1行＝1単位）。</p>`));
    const wrap = document.createElement("div");
    wrap.className = "xtable";
    const t = document.createElement("table");
    t.innerHTML = "<thead><tr>" + j.cols.map((c) => `<th>${esc(c)}</th>`).join("") + "</tr></thead>";
    const tb = document.createElement("tbody");
    for (const r of j.rows) {
      const tr = document.createElement("tr");
      if (r["採用"] !== "○") tr.className = "off";
      tr.innerHTML = j.cols.map((c) => {
        const v = r[c] == null ? "" : String(r[c]);
        return `<td title="${esc(v)}">${esc(v)}</td>`;
      }).join("");
      tb.appendChild(tr);
    }
    t.appendChild(tb);
    wrap.appendChild(t);
    box.appendChild(wrap);
    modal("Excelプレビュー（抽出単位）", box, [{ label: "閉じる", kind: "ghost" }]);
  } catch (e) {
    toast(String(e.message || e), "err");
  }
}

// ---------- 確認モード（2026-08-25。卒論では「監査」と呼ぶ手続き） ----------
// **選んだ文書（未選択なら全部）のヒット箇所を、1本のキューで上から順に確認する。**
// ✓（確認）は unit_checks として文書ごとの設定JSONに残り、確認までの秒数も記録される
// ＝「1サイトあたりの確認時間」の実測がそのまま取れる。
// 足す・外すも同じ画面からでき、操作のたびに自動保存する（途中でやめても再開できる）。

const AU = { docs: [], sets: {}, byDoc: {}, curKey: null, t0: 0, kw: null,
             names: null,     // openAudit に渡した文書名（検索語を変えた後の読み直しに使う）
             reasons: null,   // core.OP_REASONS（ジョブの応答に同梱される。→ auReasons）
             pageData: null };// いま右に出しているページの解析結果（繋ぐ操作の照合に使う）

// ⚠️ 確認モードの入口にゲート：切り取り未点検の冊があれば、先に点検へ誘導する。
//    「フッターで本文が切れている」という発想は抜けやすい（2026-08-26 に実際に見落とした）
$("auditBtn").onclick = () => {
  const targets = SEL.size ? [...SEL] : null;
  const un = bdUnchecked(targets);
  if (un.length) {
    modal("先に「切り取りの点検」をどうぞ", el(`<p class="note">
      対象のうち <b>${un.length}冊</b>が、ヘッダー・フッターの切り取りをまだ点検していません。<br>
      切り取り線が本文を巻き込んでいると、<b>文が途中で切れたまま</b>確認・書き出しに進んでしまいます
      （実例：D社 2024 p76 — 段の最後の2行がフッター扱いで消えていました）。</p>`), [
      { label: `点検する（${un.length}冊）`, kind: "primary", run: () => openBoundary(un) },
      { label: "点検せずに進む", kind: "ghost", run: () => openAudit(targets) },
    ]);
    return;
  }
  openAudit(targets);
};

// ---------- 切り取りの点検（2026-08-26 追加） ----------
// きっかけ：D社 2024 p76 で段の最後の2行が footer_margin に巻き込まれ、生成AIを含む
// 文が途中で切れたまま抽出されていた。全冊調査では50冊前後に同種の取りこぼし
// （検索語入りの行も8件）。診断はサーバー（core.boundary_scan）が機械的に行い、
// **適用するかは人が文書ごとに決める**。判断は設定JSONの boundary_check に残る。

const BD_CHANGED = new Set();        // この画面で余白を変えた冊（解析の作り直しを促す）

function bdUnchecked(targets) {
  const names = targets || DOCS.map((d) => d.name);
  return names.filter((n) => {
    const d = DOCS.find((x) => x.name === n);
    return d && !d["点検"];
  });
}

function updateBdUI() {
  const n = bdUnchecked(null).length;
  $("bdBtnLabel").textContent = n ? `切り取りの点検（未 ${n}冊）` : "切り取りの点検";
  $("bdBtn").classList.toggle("attn", n > 0);
}

$("bdBtn").onclick = () => openBoundary(SEL.size ? [...SEL] : null);

async function openBoundary(names) {
  let r;
  try {
    r = await runJob({ kind: "boundary", docs: names || null },
      names ? `選んだ${names.length}冊の切り取りを点検しています…`
            : "全冊の切り取り（ヘッダー・フッター）を点検しています…");
  } catch (e) { toast(String(e.message || e), "err"); return; }
  bdShow(r.docs || []);
}

function bdSideLine(d, side, label) {
  const s = d[side];
  if (!s) return "";
  if (!s["件数"]) return `<div class="bd-side clean">✓ ${label}：本文の巻き込みなし</div>`;
  const kw = s["検索語入り"] ? `・<b class="bd-kw">検索語入り ${s["検索語入り"]}件</b>` : "";
  let tail = "";
  if (s["干渉"]) {
    tail = `<span class="bd-clash">⚠ ページ番号が境界の内側にあり、値だけでは分けられません（「開く」で原本を確認）</span>`;
  } else if (s["提案"] != null) {
    // ⚠️ 適用はサイドごと。実例（D社 2024）：フッターは本文の巻き込みで適用すべきだが、
    //    ヘッダーの「本文らしき行」はアクティブタブの柱（位置が微妙に違い反復から漏れる）で、
    //    適用してはいけない。まとめて1ボタンにすると、こういう非対称を人が選べない
    tail = `<span class="bd-prop">${label === "フッター" ? "余白" : "上端"} ${s["現在"]} → <b>${s["提案"]}</b></span>
      <button class="mini primary bd-apply" data-side="${side}">この値を適用</button>`;
  }
  const ex = (s["例"] || []).map((e) =>
    `<div class="bd-ex">p${e["ページ"]} ${e["検索語"] ? "🔎" : ""}${esc(e.text)}</div>`).join("");
  return `<div class="bd-side warn">⚠ ${label}：本文らしき行 <b>${s["件数"]}件</b>${kw} ${tail}
    ${ex ? `<details><summary class="hint">落ちている行の例（長い順）</summary>${ex}</details>` : ""}</div>`;
}

function bdShow(docs) {
  // 並び：検索語入り → 件数の多い順。判断済みは最後（たたむ）
  const score = (d) => {
    const f = d.footer || {}, h = d.header || {};
    return (f["検索語入り"] || 0) * 100000 + (h["検索語入り"] || 0) * 100000
         + (f["件数"] || 0) + (h["件数"] || 0);
  };
  docs.sort((a, b) => (!!a["判断"]) - (!!b["判断"]) || score(b) - score(a));
  const wrap = document.createElement("div");
  wrap.className = "bdlist";
  wrap.appendChild(el(`<p class="note">機械の診断です。落ちた行を
    <b>備品</b>（柱・ロゴ・ページ番号＝切り捨てが正しい）と<b>本文らしき行</b>に分け、
    本文を全部残せる境界を提案します。<b>適用するかは1冊ずつ判断してください</b>
    （判断は設定JSONの <code>boundary_check</code> に日付つきで残ります）。</p>`));
  for (const d of docs) {
    const box = document.createElement("div");
    const f = d.footer || {}, h = d.header || {};
    const clean = !(f["件数"] || h["件数"]);
    box.className = "bd-doc" + (d["判断"] ? " done" : "");
    box.innerHTML = `<div class="bd-head"><b>${esc(d.name)}</b>
        ${d["判断"] ? `<span class="tag">済 ${esc(d["判断"]["判断"] || "")}（${esc(d["判断"]["日"] || "")}）</span>` : ""}
      </div>` +
      bdSideLine(d, "footer", "フッター") + bdSideLine(d, "header", "ヘッダー") +
      `<div class="bd-acts">
        <button class="mini ghost bd-ok">${clean ? "問題なしと記録" : "このままでよい（問題なし）"}</button>
        <button class="mini ghost bd-open">開く</button>
      </div>`;
    const done = (note) => {
      box.classList.add("done");
      box.querySelector(".bd-head").insertAdjacentHTML("beforeend",
        ` <span class="tag">済 ${esc(note)}</span>`);
    };
    for (const ap of box.querySelectorAll(".bd-apply")) {
      ap.onclick = () => bdDecide(d, ap.dataset.side).then((note) => {
        if (note) { ap.disabled = true; done(note); }
      });
    }
    box.querySelector(".bd-ok").onclick = () => bdDecide(d, null).then((note) => {
      if (note) {
        done(note);
        for (const b of box.querySelectorAll(".bd-acts .bd-ok")) b.disabled = true;
      }
    });
    box.querySelector(".bd-open").onclick = () => { closeModal(); openDoc(d.name); };
    wrap.appendChild(box);
  }
  modal("切り取りの点検 — ヘッダー・フッターが本文を巻き込んでいないか", wrap, [
    { label: "変えた設定で解析を作り直す", kind: "primary", run: bdRebuild },
    { label: "閉じる", kind: "ghost" },
  ]);
}

/** 判断を設定JSONに書く。side="footer"|"header" なら提案値をそのサイドだけ反映、
 *  null なら「問題なし」。戻り値＝記録した判断（失敗なら null）。
 *  ⚠️ 2回目の判断は前の判断に**追記**する（フッター適用→ヘッダーは問題なし、が1冊に共存する） */
async function bdDecide(d, side) {
  const name = d.name;
  try {
    const conf = await api(`/api/doc/${encodeURIComponent(name)}/conf`);
    const st = conf["設定"];
    let note;
    if (side === "footer" && d.footer && d.footer["提案"] != null) {
      st.footer_margin = d.footer["提案"];
      note = `フッター余白 ${d.footer["現在"]}→${d.footer["提案"]}`;
      BD_CHANGED.add(name);
    } else if (side === "header" && d.header && d.header["提案"] != null) {
      st.header_y = d.header["提案"];
      note = `ヘッダー上端 ${d.header["現在"]}→${d.header["提案"]}`;
      BD_CHANGED.add(name);
    } else if (side === null) {
      note = "問題なし";
    } else {
      toast("適用できる提案がありません", "err");
      return null;
    }
    const prev = (st.boundary_check || {})["判断"];
    st.boundary_check = { "日": new Date().toISOString().slice(0, 10),
                          "判断": prev && prev !== note ? `${prev}・${note}` : note };
    await api(`/api/doc/${encodeURIComponent(name)}/settings`, { settings: st });
    const row = DOCS.find((x) => x.name === name);
    if (row) row["点検"] = true;
    updateBdUI();
    return note;
  } catch (e) {
    toast(`${name}: ${e.message || e}`, "err");
    return null;
  }
}

async function bdRebuild() {
  if (!BD_CHANGED.size) {
    toast("余白を変えた冊はありません（作り直しは不要です）", "ok");
    return;
  }
  const names = [...BD_CHANGED];
  try {
    await runJob({ kind: "warm", docs: names }, `${names.length}冊を新しい設定で解析し直しています…`);
    BD_CHANGED.clear();
    toast(`${names.length}冊の解析を作り直しました`, "ok");
    loadDocs();
  } catch (e) { toast(String(e.message || e), "err"); }
}
$("auditBack").onclick = () => closeAudit();
$("auditOnlyTodo").onchange = () => renderAuditList();
$("auditPreview").onclick = () => showUnitTable(AU.docs.length ? AU.docs : null);
$("auditExport").onclick = () => auditExportDialog();
// 検索語（全文書・全時点で共通）。確認モードから見えず、どう足すか分からなかったため（2026-08-26）
$("auditKwBtn").onclick = () => {
  const kws = AU.kw || [];
  const body = el(`<div style="max-width:520px">
    <p class="note">ヒットの検索に使っている語（<b>全文書・全時点で共通</b>）：</p>
    <p class="kwchips">${kws.map((k) => `<code>${esc(k)}</code>`).join(" ")}</p>
    <label class="note" style="display:block">語を編集（カンマ区切り。例：AIエージェント を足す）
      <textarea id="kwEdit" rows="3" style="width:100%;margin-top:4px">${esc(kws.join(", "))}</textarea></label>
    <p class="note">英字だけの語（LLM など）は<b>単語として</b>当たります（Fulfillment の中の llm には当たりません）。<br>
    保存すると <code>設定\\検索語.json</code> が更新され（前の版は履歴に退避）、
    バッチ・文脈窓・全冊書き出しも同じ語を使います。語を変えたら<b>卒論の表3.3</b>も更新してください。</p></div>`);
  const ta = body.querySelector("#kwEdit");
  modal("検索語を見る・変える", body, [
    { label: "保存して集め直す", kind: "primary", run: async () => {
        const list = ta.value.split(/[,、\n]/).map((s) => s.trim()).filter(Boolean);
        if (!list.length) { toast("検索語が空です", "err"); return; }
        try {
          await api("/api/keywords", { keywords: list });
          toast("検索語を保存しました。ヒットを集め直します…", "ok");
          openAudit(AU.names);
        } catch (e) {
          toast(String(e.message || e), "err");
        }
      } },
    { label: "やめる", kind: "ghost" },
  ]);
};

$("auditHelp").onclick = () => {
  modal("確認モードでできること", el(`<div class="note" style="max-width:560px">
    <p><b>1件＝1単位。</b>左上のカードが、いま見ている単位です。
    <b>青い枠の中のテキスト</b>が、そのまま Excel の「テキスト」列に入ります。
    薄い文（前後の文脈）は書き出されません。</p>
    <p><b>原本（右）</b>：<b>濃い印が検索語そのもの</b>、細い枠が単位の範囲です。</p>
    <p><b>直す</b>：青い枠の中の文にマウスを載せると操作が出ます。
    薄い文はクリックで操作を選べます。どれも自動保存で、すぐ「元に戻す」できます。</p>
    <ul>
      <li><b>足す</b> — 同じ図解・続きの文を単位に入れる（Excelのテキストに含まれます）</li>
      <li><b>前と繋ぐ</b> — 切れてしまった1つの文を、前のブロックの続きとして直す</li>
      <li><b>分ける</b> — 見出しが本文と癒着して1つの文になったブロックを、行の境目で2つに分ける。
        切り離した見出しは単独の文（前後の文脈）になり、単位には本文だけが残ります</li>
      <li><b>除外</b> — 断片・リンク表記など、文書の記述でないものをデータから消す（全文データからも消えます）</li>
      <li><b>表の検出をやめる</b>（「表の行」の単位だけ）— 図解の枠を表と誤検出して、枠の中を丸ごと抜き出してしまったとき。
        検出をやめると、ヒットした文だけが単位として取り直されます</li>
      <li><b>単位ごと外す</b>（下の欄）— ヒット自体を分析に入れない。監査記録には残ります</li>
    </ul>
    <p><b>進める</b>：<b>Enter</b> か「✓確認して次へ」。判断に迷ったら「あとで」。
    手作業はすべて設定JSONに残り、同じ入力から同じ出力が再現されます。</p></div>`),
    [{ label: "閉じる", kind: "ghost" }]);
};

// 原本の表示倍率（この画面の主役はPDF。よく見えないと確認にならない）
let AUZOOM = (() => {
  const v = parseInt(localStorage.getItem("auZoom") || "100", 10);
  return Number.isFinite(v) ? Math.max(50, Math.min(300, v)) : 100;
})();
const AU_IMG_ZOOM = 2.2;   // 取得する画像の解像度。表示の大小はCSS幅で変える（取り直さない）

function setAuZoom(v) {
  AUZOOM = Math.max(50, Math.min(300, Math.round(v / 10) * 10));
  try { localStorage.setItem("auZoom", String(AUZOOM)); } catch (e) { /* 保存できなくても動く */ }
  $("auZoom").value = AUZOOM;
  $("auZoomPct").textContent = AUZOOM + "%";
  const zw = document.querySelector("#auditPageBox .apage-zw");
  if (zw) zw.style.width = AUZOOM + "%";
}
$("auZoom").oninput = () => setAuZoom(+$("auZoom").value);
$("auZoomOut").onclick = () => setAuZoom(AUZOOM - 20);
$("auZoomIn").onclick = () => setAuZoom(AUZOOM + 20);
// Ctrl+ホイールでも変えられる（ブラウザ全体のズームは抑える）
$("auditRight").addEventListener("wheel", (e) => {
  if (!e.ctrlKey) return;
  e.preventDefault();
  setAuZoom(AUZOOM + (e.deltaY < 0 ? 10 : -10));
}, { passive: false });

// キー操作：Enter ＝「✓ 確認して次へ」。246サイトを順に見る作業なので、マウスに戻らず進めるようにする。
// 入力欄・プルダウン・モーダルにフォーカスがあるときは奪わない
document.addEventListener("keydown", (e) => {
  if ($("audit").hidden || !$("modal").hidden || AU_MENU || e.key !== "Enter") return;
  const tag = (document.activeElement && document.activeElement.tagName) || "";
  if (/INPUT|SELECT|TEXTAREA|BUTTON/.test(tag)) return;
  const ok = $("auditOk");
  if (ok) { e.preventDefault(); ok.click(); }
});

function closeAudit() {
  closeMiniMenu();
  AU.pageData = null;
  $("audit").hidden = true;
  $("pane-open").hidden = false;
  loadDocs();                       // 一覧のバッジ（単位数・確認数）を更新する
}

const auKey = (it) => {
  const a = unitAnchor(it.u);
  return `${it.doc}|${a.page}|${a.text}`;
};
const auDone = (u) => !!(u["確認"] || u["採用"]);

function auItems() {
  const out = [];
  for (const d of AU.docs) for (const u of (AU.byDoc[d] || [])) out.push({ doc: d, u });
  return out;
}

async function openAudit(names) {
  // 重い部分（未解析の文書の一括解析＋読み込み）はジョブで。進捗％が出る
  let r;
  try {
    r = await runJob({ kind: "units_all", docs: names || null },
      names ? `選んだ${names.length}冊のヒット箇所を集めています…`
            : "全冊のヒット箇所を集めています…");
  } catch (e) {
    toast(String(e.message || e), "err");
    return;
  }
  $("pane-open").hidden = true;
  $("audit").hidden = false;
  AU.byDoc = {}; AU.sets = {}; AU.curKey = null; AU.pageData = null;
  AU.names = names || null;
  AU.kw = r["検索語"] || null;
  AU.reasons = r["操作の理由"] || AU.reasons;
  // 何の語で当てているかを、いつでも見える場所に出す（クリックで編集）
  $("auditKwLabel").textContent = `検索語 ${(AU.kw || []).length}語`;
  $("auditKwBtn").title = "ヒットの検索に使っている語（全文書共通）：\n" + (AU.kw || []).join(" / ");
  AU.docs = r.docs.map((d) => d.name);
  for (const d of r.docs) {
    AU.sets[d.name] = d["設定"];
    AU.byDoc[d.name] = d["単位"];
  }
  setAuZoom(AUZOOM);                 // スライダー表示を保存値に合わせる
  renderAuditList();
  gotoNextTodo();
}

function auditStats() {
  const items = auItems();
  const done = items.filter((it) => auDone(it.u)).length;
  const zero = AU.docs.filter((d) => (AU.byDoc[d] || []).length === 0).length;
  return { total: items.length, done, zero };
}

function renderAuditList() {
  const st = auditStats();
  $("auditProg").textContent = `確認 ${st.done} / ${st.total} 単位` + (st.zero ? `（ヒットなし ${st.zero}冊）` : "");
  $("auditBarFill").style.width = st.total ? `${st.done / st.total * 100}%` : "0%";

  const left = $("auditList");
  left.innerHTML = "";
  const onlyTodo = $("auditOnlyTodo").checked;
  for (const d of AU.docs) {
    const units = AU.byDoc[d] || [];
    const todo = units.filter((u) => !auDone(u)).length;
    const hasCur = units.some((u) => auKey({ doc: d, u }) === AU.curKey);
    if (onlyTodo && units.length && !todo && !hasCur) continue;
    if (onlyTodo && !units.length) continue;      // ヒットなしの冊は「未確認だけ」では出さない
    const h = el(`<div class="au-doc">${esc(d)} <span class="hint">${
      units.length ? `${units.length}単位・残り${todo}` : "ヒットなし"}</span></div>`);
    left.appendChild(h);
    for (const u of units) {
      if (onlyTodo && auDone(u) && auKey({ doc: d, u }) !== AU.curKey) continue;
      const row = document.createElement("div");
      const state = u["採用"] ? '<span class="rtag off">除外</span>'
        : u["確認"] ? '<span class="rtag">✓</span>' : '<span class="rtag none">未</span>';
      row.className = "au-row" + (auKey({ doc: d, u }) === AU.curKey ? " cur" : "");
      row.innerHTML = `<span class="pg">p.${u["ページ"]}</span>
        <span class="rulechip">${esc(u["規則"])}</span>
        <span class="ut">${esc((u["ヒット文"] || "").slice(0, 34))}</span>${state}`;
      row.onclick = () => selectAudit({ doc: d, u });
      left.appendChild(row);
    }
  }
  if (!left.children.length) {
    left.appendChild(el(`<div class="au-finish"><h3>すべて確認済みです 🎉</h3>
      <p class="note">右上の<b>全冊書き出し</b>で KHCoder_抽出単位.xlsx（＋分割）を作れます。<br>
      「未確認だけ」のチェックを外すと、確認済みの一覧を見直せます。</p></div>`));
  }
  // いま見ている単位が一覧の外に流れたら追いかける（✓で次へ進むたびに迷子にならない）
  const cur = left.querySelector(".au-row.cur");
  if (cur) cur.scrollIntoView({ block: "nearest" });
}

function gotoNextTodo() {
  const next = auItems().find((it) => !auDone(it.u));
  if (next) { selectAudit(next); return; }
  AU.curKey = null;
  const st = auditStats();
  const secs = auItems().map((it) => it.u["確認秒"]).filter((s) => s > 0);
  const avg = secs.length ? (secs.reduce((a, b) => a + b, 0) / secs.length).toFixed(1) : "—";
  $("auditPageBox").innerHTML = "";
  $("auditCard").innerHTML = "";
  $("auditCard").appendChild(el(`<div class="au-finish"><h3>すべて確認済みです 🎉</h3>
    <p class="note">確認 ${st.done}／${st.total} 単位 ／ 1サイトあたり平均 <b>${avg}秒</b>（設定JSONに記録済み＝実測の材料）。<br>
    右上の<b>全冊書き出し</b> → KH Coder へ。</p></div>`));
  renderAuditList();
}

async function auditSave(doc) {
  await api(`/api/doc/${encodeURIComponent(doc)}/settings`, { settings: AU.sets[doc] });
}

async function auditRefetch(doc, keepKey) {
  const j = await api(`/api/doc/${encodeURIComponent(doc)}/units`, { settings: AU.sets[doc] });
  AU.byDoc[doc] = j["単位"];
  renderAuditList();
  if (keepKey) {
    const it = auItems().find((x) => auKey(x) === keepKey);
    if (it) selectAudit(it, true);
  }
}

function auMergeRule(doc, u, create) {
  const st = AU.sets[doc];
  st.unit_merges = st.unit_merges || [];
  const a = unitAnchor(u);
  let r = st.unit_merges.find((m) => m.page === a.page && m.hit === a.text);
  if (!r && create) {
    r = { page: a.page, hit: a.text, add: [], reason: "" };
    st.unit_merges.push(r);
  }
  return r;
}

const AU_PRIMED = new Set();

function selectAudit(it, keepTimer) {
  AU.curKey = auKey(it);
  if (!keepTimer) AU.t0 = Date.now();
  // この文書の表検出キャッシュを裏で温めておく（→ /prime）。
  // カードを読んでいる数秒の間に終わり、最初の「除外・繋ぐ」から1秒弱で返るようになる
  if (!AU_PRIMED.has(it.doc)) {
    AU_PRIMED.add(it.doc);
    api(`/api/doc/${encodeURIComponent(it.doc)}/prime`, {}).catch(() => AU_PRIMED.delete(it.doc));
  }
  renderAuditList();
  renderAuditDetail(it);
}

// --- 小さな操作メニュー（確認モードの文の上で使う） -----------------------
// select だと「なぜそうするか」（note）を並べて見せられないので、ポップオーバーにする。
// 2段目（理由の一覧）も同じ部品を使い回す。外をクリック／Esc で閉じる。

let AU_MENU = null;
function closeMiniMenu() {
  if (!AU_MENU) return;
  AU_MENU.remove();
  AU_MENU = null;
  document.removeEventListener("pointerdown", _mmDocDown, true);
  document.removeEventListener("keydown", _mmKey, true);
}
function _mmDocDown(e) { if (AU_MENU && !AU_MENU.contains(e.target)) closeMiniMenu(); }
function _mmKey(e) { if (e.key === "Escape") { e.stopPropagation(); closeMiniMenu(); } }

/** @param items [{icon, label, hint, danger, run}] */
function miniMenu(anchor, title, items) {
  closeMiniMenu();
  const m = document.createElement("div");
  m.className = "minimenu glass";
  if (title) m.appendChild(el(`<div class="mm-title">${esc(title)}</div>`));
  for (const item of items) {
    const b = document.createElement("button");
    b.className = "mm-item" + (item.danger ? " danger" : "");
    b.innerHTML = `<span class="mm-l">${item.icon ? ICON(item.icon) : ""}${esc(item.label)}</span>` +
      (item.hint ? `<span class="mm-hint">${esc(item.hint)}</span>` : "");
    b.onclick = (e) => { e.stopPropagation(); closeMiniMenu(); item.run(); };
    m.appendChild(b);
  }
  document.body.appendChild(m);
  const r = anchor.getBoundingClientRect();
  m.style.left = Math.max(8, Math.min(r.left, innerWidth - m.offsetWidth - 8)) + "px";
  m.style.top = (r.bottom + 4 + m.offsetHeight > innerHeight
    ? Math.max(8, r.top - m.offsetHeight - 4) : r.bottom + 4) + "px";
  AU_MENU = m;
  document.addEventListener("pointerdown", _mmDocDown, true);
  document.addEventListener("keydown", _mmKey, true);
}

// --- 理由の一覧（正はサーバーの core.OP_REASONS。ジョブの応答に同梱される） -----
// 確認モードは文書を開かずに直行できるので、応答に無いときのための最低限も持つ
const AU_FALLBACK_REASONS = {
  unit_excludes: [
    { key: "商標注記", label: "商標・登録商標の注記" },
    { key: "出典注記", label: "出典・参照先の注記" },
    { key: "誤ヒット", label: "検索語の誤ヒット" },
    { key: "断片", label: "語として意味を成さない断片" },
    { key: "柱の取り残し", label: "柱・ナビの取り残し" },
  ],
  excluded: [
    { key: "参照表記", label: "参照・リンク表記（「詳しくはP.○○」など）" },
    { key: "ロゴ商標", label: "ロゴ・商標・意匠の文字" },
    { key: "ページ表記", label: "ページ番号・柱（座標で切れなかったもの）" },
    { key: "図表の断片", label: "図表の目盛り・単位・記号だけの断片" },
    { key: "二重描画", label: "二重描画の取りこぼし" },
  ],
  joins: [
    { key: "段またぎ", label: "段をまたいで続く本文" },
    { key: "列ずれ", label: "同じ段だが左端が揃っていない" },
    { key: "サイズ違い", label: "1つの文だが文字の大きさが違う" },
    { key: "行間", label: "1つの本文だが行の間隔が広い" },
    { key: "割り込み", label: "間に図・アイコン・注記が入って分断された" },
  ],
  splits: [
    { key: "見出しの癒着", label: "本文と同じptの見出しが、本文と同じブロックに入っていた" },
    { key: "別の文の癒着", label: "別々の文・ラベルが1つのブロックに入っていた" },
  ],
  unit_merges: [
    { key: "ラベル一体", label: "同じ図解・カードで一体の意味をなすラベル" },
    { key: "文の続き", label: "機械で繋がらなかった文の続き" },
    { key: "表の続き", label: "同じ表で一体の行" },
  ],
  table_off: [
    { key: "図解の枠", label: "表ではなく図解の枠線だった（組織図・フロー図など）" },
    { key: "レイアウトの枠", label: "ページ全体や段組みを囲む枠線だった" },
  ],
};

function auReasons(op) {
  const src = ((AU.reasons || (S.info && S.info["操作の理由"]) || {})[op]) || [];
  return src.length ? src : (AU_FALLBACK_REASONS[op] || []);
}

function auReasonItems(op, run) {
  const items = auReasons(op).map((c) => ({ label: c.label, hint: c.note, run: () => run(c.key) }));
  items.push({ label: "理由を付けずに実行", hint: "理由は後から「開いて直す」の記録タブでも付けられます", run: () => run("") });
  return items;
}

// --- L1 の手作業（文の除外・ブロックの結合）を確認モードから -----------------
// 「開いて直す」で毎回画面を往復しなくて済むように、カードの上で同じ操作を作る。
// ⚠️ どちらも**全文データ（L1）ごと直す**操作。ルールの形も照合キーも作業画面と同じで、
//    設定JSONに残る（→ core の excluded / joins）。単位だけの操作（足す・外す）とは層が違う。

/** L1 を触った後の共通処理：保存 → 単位を取り直す → いま見ていた単位（か同じページ）へ戻る。
 *  文の切れ方が変わるので、文書全体の再解析が走る（数秒かかることがある）。 */
async function auAfterL1(it, msg, undo) {
  const { doc, u } = it;
  const page = u["ページ"];
  AU.pageData = null;                        // ブロックの切れ方が変わったので取り直す
  $("auditCard").classList.add("busy");
  try {
    await auditSave(doc);
    const j = await api(`/api/doc/${encodeURIComponent(doc)}/units`, { settings: AU.sets[doc] });
    AU.byDoc[doc] = j["単位"];
  } catch (e) {
    toast(String(e.message || e), "err");
    $("auditCard").classList.remove("busy");
    return;
  }
  $("auditCard").classList.remove("busy");
  renderAuditList();
  // 文言が変わると鍵（ヒット文）も変わるので、同じ鍵 → 同じページ → 次の未確認 の順で探す
  let next = auItems().find((x) => auKey(x) === AU.curKey);
  if (!next) next = auItems().find((x) => x.doc === doc && x.u["ページ"] === page && !auDone(x.u))
                 || auItems().find((x) => x.doc === doc && x.u["ページ"] === page);
  if (next) selectAudit(next, true); else gotoNextTodo();
  if (msg) auUndoToast(msg, undo);
}

/** 通知＋「元に戻す」。直したそばから戻せるようにしておく（開いて直すへ行かずに済む） */
function auUndoToast(msg, undo) {
  toast(msg, "ok");
  if (!undo) return;
  const b = document.createElement("button");
  b.className = "mini";
  b.innerHTML = ICON("undo") + "元に戻す";
  b.onclick = () => { $("toast").hidden = true; undo(); };
  $("toastLinks").appendChild(b);
}

/** 文をデータから除外する（L1）。作業画面の × と同じルールを作る（文言＋ページ＋pt） */
function auExcludeSentence(it, s, reason) {
  const st = AU.sets[it.doc];
  st.excluded = st.excluded || [];
  const rule = { text: s["文"], page: s["ページ"],
                 pt: s["pt"] === undefined ? null : s["pt"], reason: reason || "" };
  st.excluded.push(rule);
  auAfterL1(it, "文をデータから除外しました（全文データからも消えます）", () => {
    st.excluded = st.excluded.filter((r) => r !== rule);
    auAfterL1(it);
  });
}

/** いま右に出しているページの解析結果。無ければ取り寄せる（繋ぐ操作の照合に使う） */
async function auPageData(doc, page) {
  const pd = AU.pageData;
  if (pd && pd.doc === doc && pd.page === page) return pd.d;
  return api(`/api/doc/${encodeURIComponent(doc)}/page/${page}`, { settings: AU.sets[doc] });
}

/** この文のブロックを、ひとつ前のブロックと繋ぐ（L1 の joins）。
 *  ⚠️ ルールの鍵は**結合前の生text**（→ core.apply_joins）。だから parts の端を使う */
async function auJoinPrev(it, s, reason) {
  const doc = it.doc, page = s["ページ"];
  let d;
  try { d = await auPageData(doc, page); }
  catch (e) { toast(String(e.message || e), "err"); return; }
  const gi = d.groups.findIndex((g) => (g.units || []).some((x) => x.text === s["文"]));
  if (gi < 0) { toast("この文のブロックが原本ページで見つかりませんでした", "err"); return; }
  if (gi === 0) {
    toast("ページの最初のブロックなので、前とは繋げません（ページをまたいで続く文は自動で繋がります）", "err");
    return;
  }
  const prev = d.groups[gi - 1], g = d.groups[gi];
  const st = AU.sets[doc];
  st.joins = st.joins || [];
  const rule = { page, a: prev.parts[prev.parts.length - 1], b: g.parts[0], reason: reason || "" };
  if (st.joins.some((r) => r.page === page && r.a === rule.a && r.b === rule.b)) {
    toast("この2つはもう繋いであります", "err");
    return;
  }
  st.joins.push(rule);
  auAfterL1(it, `前のブロック「${(prev.raw || "").trim().slice(0, 16)}…」と繋ぎました`, () => {
    st.joins = st.joins.filter((r) => r !== rule);
    auAfterL1(it);
  });
}

function auJoinMenu(it, s, anchor) {
  miniMenu(anchor, "前のブロックと繋ぐ — なぜ機械で切れた？",
    auReasonItems("joins", (k) => auJoinPrev(it, s, k)));
}

/** このブロックを行の境目で2つに分ける（L1 の splits。joins の逆）。
 *  本文と同じptの見出しは、下の本文と同じブロックに入ることがある。見出しは句点で
 *  終わらないので、そのままだと**見出し＋本文が1つの文**になる（D社 2023 p62）。
 *  ⚠️ ルールの鍵は**結合前の生text**＋左上の位置（→ core.apply_splits）。
 *     結合済みのブロックでは、境目を含むパーツを名指しし、行番号もパーツ内で数え直す */
async function auSplitMenu(it, s, anchor) {
  const doc = it.doc, page = s["ページ"];
  let d;
  try { d = await auPageData(doc, page); }
  catch (e) { toast(String(e.message || e), "err"); return; }
  const g = d.groups.find((x) => (x.units || []).some((u) => u.text === s["文"]));
  if (!g) { toast("この文のブロックが原本ページで見つかりませんでした", "err"); return; }
  if (g.table !== undefined && g.table !== null) {
    toast("表の行は分けられません（枠の誤検出なら「表の検出をやめる」で直してください）", "err");
    return;
  }
  const lines = g.lines || [], parts = g.parts || [];
  if (lines.length < 2) { toast("このブロックは1行だけなので、分ける場所がありません", "err"); return; }

  // 行の境目 → どのパーツ（結合前ブロック）の何行目か。
  // 通常ブロックの raw は行textの連結なので、文字数を数えればパーツの境目が分かる
  const bounds = [];
  let pi = 0, len = 0, partStart = 0, partLen = (parts[0] || "").length;
  for (let k = 1; k < lines.length; k++) {
    len += (lines[k - 1].text || "").length;
    if (len === partLen && pi < parts.length - 1) {
      // ここはパーツの継ぎ目＝元から別ブロック（結合で繋いだ場所）。分ける対象ではない
      pi++; partStart = k; partLen += (parts[pi] || "").length;
      continue;
    }
    bounds.push({ part: pi, line: k - partStart, k });
  }
  if (!bounds.length) {
    toast("このブロックの行の境目は、すべて結合の継ぎ目です。分けるなら結合のほうを戻してください", "err");
    return;
  }
  const clip = (t, n, tail) => {
    t = (t || "").trim();
    return t.length > n ? (tail ? "…" + t.slice(-n) : t.slice(0, n) + "…") : t;
  };
  miniMenu(anchor, "どこで分ける？ — 行の境目を選ぶ", bounds.map((b) => ({
    icon: "scissors",
    label: `${clip(lines[b.k - 1].text, 11, true)} ✂ ${clip(lines[b.k].text, 11)}`,
    hint: `${b.k}行目の後で分ける`,
    run: () => miniMenu(anchor, "ブロックを分ける — なぜ1つのブロックに？",
      auReasonItems("splits", (reason) => auSplit(it, page, g, b, reason))),
  })));
}

function auSplit(it, page, g, b, reason) {
  const st = AU.sets[it.doc];
  st.splits = st.splits || [];
  // ⚠️ 位置（at）は持たせない。ブロックの生text全体＋ページで実質一意で、
  //    結合（joins）の a/b と同じ考え方。座標を持たせると、解析ごとのわずかな
  //    座標の揺れでルールが黙って外れる事故のもとになる（2026-08-26 に実際に起きた）
  const rule = { page, text: (g.parts || [])[b.part] ?? g.raw,
                 line: b.line, reason: reason || "" };
  st.splits.push(rule);
  auAfterL1(it, "ブロックを2つに分けました。文の切れ方を取り直しています", () => {
    st.splits = st.splits.filter((r) => r !== rule);
    auAfterL1(it);
  });
}
function auExcludeMenu(it, s, anchor) {
  miniMenu(anchor, "この文をデータから除外 — 記述でないものだけ",
    auReasonItems("excluded", (k) => auExcludeSentence(it, s, k)));
}

/** 図解の枠線を表と誤検出して、枠の中を丸ごと1行に潰してしまったページ用（L1 の table_off）。
 *  ⚠️ **除外で対処しない。** 単位ごと除外すると本物のヒットまでデータから消える。
 *     表の検出をやめれば、ブロックが普通に切り直され、ヒットした文だけが単位として取れる。 */
function auTableOff(it, reason) {
  const { doc, u } = it;
  const page = u["ページ"];
  const st = AU.sets[doc];
  st.table_off = st.table_off || [];
  const rule = { page, reason: reason || "" };
  st.table_off.push(rule);
  auAfterL1(it, `p.${page} の表の検出をやめました。ヒットを文として取り直しています`, () => {
    st.table_off = st.table_off.filter((r) => r !== rule);
    auAfterL1(it);
  });
}

function auTableOffMenu(it, anchor) {
  const page = it.u["ページ"];
  if ((AU.sets[it.doc].table_off || []).some((r) => r.page === page)) {
    toast(`p.${page} の表の検出はもう止めてあります`, "err");
    return;
  }
  miniMenu(anchor, `p.${page} の表の検出をやめる — 罫線の正体は？`,
    auReasonItems("table_off", (k) => auTableOff(it, k)));
}

// --- カード（左上段）。前の文脈 → Excelに入るテキスト → 後の文脈 の3層 --------
// 「どの部分が書き出されるのか分かりにくい」という指摘から、書き出される部分を
// **青い枠のゾーン**として明示する。青＝メイン色（選択・主要）で、ここが本体という意味。

function auSentRow(it, s, hl2, multiPage) {
  const { doc, u } = it;
  const row = document.createElement("div");
  const r = auMergeRule(doc, u);
  const added = r && (r.add || []).includes(s["文"]);
  row.className = "au-s" + (s.hit ? " hit" : "") + (added ? " added" : "");
  const t = document.createElement("span");
  t.className = "au-s-t";
  t.innerHTML = (multiPage ? `<span class="pb">p.${s["ページ"]}</span>` : "") + hl2(s["文"]);
  row.appendChild(t);

  const tools = document.createElement("span");
  tools.className = "au-s-tools";
  const mk = (icon, label, title, danger) => {
    const b = document.createElement("button");
    b.className = "mini ghost aub" + (danger ? " danger" : "");
    b.innerHTML = ICON(icon) + label;
    b.title = title;
    return b;
  };
  if (added) {
    const back = mk("undo", "戻す", "手で足した文です。単位から外します（自動保存）");
    back.onclick = async (e) => {
      e.stopPropagation();
      r.add = (r.add || []).filter((x) => x !== s["文"]);
      if (!r.add.length) AU.sets[doc].unit_merges = AU.sets[doc].unit_merges.filter((m) => m !== r);
      await auditSave(doc);
      auditRefetch(doc, AU.curKey);
    };
    tools.appendChild(back);
  } else {
    const j = mk("link", "前と繋ぐ", "切れてしまった文を、前のブロックの続きとして1つの文に直します");
    j.onclick = (e) => { e.stopPropagation(); auJoinMenu(it, s, j); };
    tools.appendChild(j);
    const sp = mk("scissors", "分ける", "見出しなどが癒着して1つの文になったブロックを、行の境目で2つに分けます");
    sp.onclick = (e) => { e.stopPropagation(); auSplitMenu(it, s, sp); };
    tools.appendChild(sp);
    const x = mk("x", "除外", "この文をデータから除外します（全文データからも消えます）", true);
    x.onclick = (e) => { e.stopPropagation(); auExcludeMenu(it, s, x); };
    tools.appendChild(x);
  }
  row.appendChild(tools);
  return row;
}

function auNearZone(it, sents, label, hl2) {
  const z = document.createElement("div");
  z.className = "au-near";
  if (!sents.length) { z.hidden = true; return z; }
  z.appendChild(el(`<div class="au-near-label">${esc(label)}
    <span class="hint">書き出されません。クリックで 足す・繋ぐ・分ける・除外</span></div>`));
  const flow = document.createElement("div");
  flow.className = "au-near-flow";
  for (const s of sents) {
    const sp = document.createElement("span");
    sp.className = "s near";
    sp.innerHTML = hl2(s["文"]);
    sp.title = "クリックで操作を選ぶ（単位に足す／前と繋ぐ／ブロックを分ける／データから除外）";
    sp.onclick = (e) => {
      e.stopPropagation();
      miniMenu(sp, (s["文"] || "").slice(0, 26), [
        { icon: "plus", label: "この文を単位に足す", hint: "Excelの「テキスト」に含めます（同じ図解・続きの文）",
          run: async () => {
            const rule = auMergeRule(it.doc, it.u, true);
            if (!rule.add.includes(s["文"])) rule.add.push(s["文"]);
            await auditSave(it.doc);
            auditRefetch(it.doc, AU.curKey);
          } },
        { icon: "link", label: "前のブロックと繋ぐ…", hint: "切れてしまった1つの文を繋いで直します",
          run: () => auJoinMenu(it, s, sp) },
        { icon: "scissors", label: "ブロックを分ける…", hint: "見出しの癒着など、別のものが1つの文になったブロックを行で分けます",
          run: () => auSplitMenu(it, s, sp) },
        { icon: "x", label: "データから除外…", hint: "断片・リンク表記など、文書の記述でないもの", danger: true,
          run: () => auExcludeMenu(it, s, sp) },
      ]);
    };
    flow.appendChild(sp);
  }
  z.appendChild(flow);
  return z;
}

function renderAuditDetail(it) {
  const { doc, u } = it;
  // カードは左パネルの上段へ。右は原本PDFだけ（→ auditShowPage）
  const box = $("auditCard");
  box.innerHTML = "";
  closeMiniMenu();
  const kw = AU.kw || (S.info && S.info["検索語"]) || null;
  const rx = ctxRegex(kw || ["生成AI"]);
  const hl2 = (s) => rx ? esc(s).replace(rx, (m) => `<mark>${m}</mark>`) : esc(s);
  const excluded = !!u["採用"];

  const card = document.createElement("div");
  card.className = "win unit au-card" + (excluded ? " off" : "");
  const pages = u["ページ"] !== u["最終ページ"] ? `p.${u["ページ"]}〜${u["最終ページ"]}` : `p.${u["ページ"]}`;
  card.appendChild(el(`<div class="win-head"><b>${esc(doc)}</b><span class="pg">${pages}</span>
    <span class="rulechip">${esc(u["規則"])}</span><span class="kw">${esc(u["ヒット語"])}</span>
    ${u["手作業"] ? '<span class="rtag">結合</span>' : ""}
    ${excluded ? `<span class="rtag off">除外${u["除外理由"] ? "：" + esc(u["除外理由"]) : ""}</span>` : ""}
    ${u["確認"] ? '<span class="rtag">✓確認済</span>' : ""}</div>`));

  const b = document.createElement("div");
  b.className = "win-body";
  const multiPage = u["ページ"] !== u["最終ページ"];

  b.appendChild(auNearZone(it, u["前"] || [], "前の文脈", hl2));

  const zone = document.createElement("div");
  zone.className = "auz" + (excluded ? " off" : "");
  zone.appendChild(el(`<div class="auz-head">${ICON("table")}<b>Excelに入るテキスト</b>
    <span class="hint">「テキスト」列 ─ ${u["文数"]}文・${u["文字数"]}字</span></div>`));
  if (excluded) {
    zone.appendChild(el(`<p class="auz-note">この単位は<b>除外</b>として書き出されます
      （監査記録には残り、KH Coder 用の本体には入りません）。</p>`));
  }
  for (const s of u["文"] || []) zone.appendChild(auSentRow(it, s, hl2, multiPage));
  if (u["手作業"]) {
    const r2 = auMergeRule(doc, u);
    const sel = document.createElement("select");
    sel.className = "treason auz-reason";
    sel.appendChild(el(`<option value=''>${r2 && r2.reason
      ? "足した理由：" + esc(r2.reason) : "足した理由を選ぶ…"}</option>`));
    for (const c of auReasons("unit_merges")) {
      const op = document.createElement("option");
      op.value = c.key; op.textContent = c.label;
      if (c.note) op.title = c.note;
      sel.appendChild(op);
    }
    sel.onchange = async () => {
      if (!sel.value || !r2) return;
      r2.reason = sel.value;
      await auditSave(doc);
      auditRefetch(doc, AU.curKey);
    };
    zone.appendChild(sel);
  }
  b.appendChild(zone);

  b.appendChild(auNearZone(it, u["後"] || [], "後の文脈", hl2));
  card.appendChild(b);

  const bar = document.createElement("div");
  bar.className = "bulkbar";
  const okBtn = document.createElement("button");
  okBtn.className = "primary";
  okBtn.id = "auditOk";                       // Enter キーでも押せる（→ 確認モードのキー操作）
  okBtn.textContent = "✓ 確認して次へ";
  okBtn.title = "Enter キーでも進めます";
  okBtn.onclick = async () => {
    const a = unitAnchor(u);
    const st2 = AU.sets[doc];
    st2.unit_checks = (st2.unit_checks || []).filter((c) => !(c.page === a.page && c.hit === a.text));
    st2.unit_checks.push({ page: a.page, hit: a.text,
      "秒": Math.round((Date.now() - AU.t0) / 100) / 10,
      "日": new Date().toISOString().slice(0, 10) });
    u["確認"] = true;
    await auditSave(doc);
    renderAuditList();
    gotoNextTodo();
  };
  bar.appendChild(okBtn);

  if (!excluded) {
    const sel = document.createElement("select");
    sel.className = "treason";
    sel.appendChild(el("<option value=''>単位ごと外す（理由を選ぶ）…</option>"));
    for (const c of auReasons("unit_excludes")) {
      const op = document.createElement("option");
      op.value = c.key; op.textContent = c.label;
      if (c.note) op.title = c.note;
      sel.appendChild(op);
    }
    sel.onchange = async () => {
      if (!sel.value) return;
      const a = unitAnchor(u);
      const st2 = AU.sets[doc];
      st2.unit_excludes = st2.unit_excludes || [];
      st2.unit_excludes.push({ page: a.page, text: a.text, reason: sel.value });
      await auditSave(doc);
      await auditRefetch(doc);
      gotoNextTodo();
    };
    bar.appendChild(sel);
  } else {
    const back = document.createElement("button");
    back.className = "x back";
    back.textContent = "採用に戻す";
    back.onclick = async () => {
      const a = unitAnchor(u);
      AU.sets[doc].unit_excludes = (AU.sets[doc].unit_excludes || []).filter(
        (r2) => !(r2.page === a.page && r2.text === a.text));
      await auditSave(doc);
      auditRefetch(doc, AU.curKey);
    };
    bar.appendChild(back);
  }

  // 表の行の単位にだけ出す：図解の枠線を表と誤検出して、枠の中を丸ごと抜き出してしまったとき用。
  // ⚠️ 単位の除外では対処しない（本物のヒットごと消えてデータが歪む）。→ auTableOff
  if (u["規則"] === "表の行" && !excluded) {
    const toff = document.createElement("button");
    toff.className = "ghost";
    toff.innerHTML = ICON("table") + "表の検出をやめる";
    toff.title = "このページの表の検出をやめて、ヒットした文だけを取り直します（自動保存・元に戻せます）。" +
      "図解の枠線を表と誤検出して、枠の中を丸ごと1つの単位にしてしまったとき用。" +
      "⚠️ このページに本物の表もあるときは、その表も行に組まれなくなります";
    toff.onclick = () => auTableOffMenu(it, toff);
    bar.appendChild(toff);
  }

  const open = document.createElement("button");
  open.className = "ghost";
  open.textContent = "開いて直す";
  open.title = "この文書をワークベンチで開きます。文の除外・結合はこのカードの上でできるので、" +
    "表の範囲指定・ページの除外・並べ替え・パラメータ調整のときだけ使います";
  open.onclick = () => {
    closeAudit();
    openDoc(doc).then(() => go(u["ページ"]));
  };
  bar.appendChild(open);

  const skip = document.createElement("button");
  skip.className = "ghost";
  skip.textContent = "あとで";
  skip.title = "確認せずに次の未確認へ";
  skip.onclick = () => {
    const items = auItems();
    const i = items.findIndex((x) => auKey(x) === AU.curKey);
    const next = items.slice(i + 1).find((x) => !auDone(x.u)) || items.find((x) => !auDone(x.u));
    if (next && auKey(next) !== AU.curKey) selectAudit(next);
  };
  bar.appendChild(skip);
  card.appendChild(bar);
  box.appendChild(card);

  // 原本ページ（ヒット箇所に赤枠）
  auditShowPage(it);
}

async function auditShowPage(it) {
  const { doc, u } = it;
  const key = AU.curKey;
  const page = u["ページ"];
  const box = $("auditPageBox");
  box.innerHTML = "";                         // 右は原本だけ（カードは左パネルが持つ）
  // 画像は高解像度（AU_IMG_ZOOM）で1回だけ取り、表示の大小は .apage-zw の幅で変える
  // （拡大するたびに取り直すとキャッシュが利かない）。枠は幅％なので拡大しても付いてくる
  const holder = el(`<div class="apage"><div class="apage-cap hint">${esc(doc)}　p.${page}（印刷上：${esc(String(u["ページ表示"] || page))}）
    <span class="apage-legend"><span class="lg-box"></span>単位の範囲　<span class="lg-word"></span>検索語</span></div>
    <div class="apage-img"><div class="apage-zw" style="width:${AUZOOM}%"><img src="/api/doc/${encodeURIComponent(doc)}/page/${page}.jpg?zoom=${AU_IMG_ZOOM}" alt=""></div></div></div>`);
  // ⚠️ el() は DocumentFragment を返す。append すると中身が移って空になるので、
  //    参照は**append する前に**取っておく（取った要素は移動後もそのまま使える）
  const imgbox = holder.querySelector(".apage-zw");
  box.appendChild(holder);
  // 単位の枠（ページ解析）と、検索語そのものの矩形（→ /hits）を並行で取る。
  // /hits には**この単位の文**も渡す：同じブロックに別の単位のヒット文があるとき、
  // その単位の語まで光らせないため（サーバー側で文へ帰属させて絞る）
  const words = (u["ヒット語"] || "").split("／").map((w) => w.trim()).filter(Boolean);
  const texts = (u["文"] || []).filter((s) => s["ページ"] === page).map((s) => s["文"]);
  try {
    const [d, hj] = await Promise.all([
      api(`/api/doc/${encodeURIComponent(doc)}/page/${page}`, { settings: AU.sets[doc] }),
      words.length
        ? api(`/api/doc/${encodeURIComponent(doc)}/page/${page}/hits`,
              { words, texts, settings: AU.sets[doc] }).catch(() => null)
        : Promise.resolve(null),
    ]);
    if (AU.curKey !== key) return;                    // 待っている間に別の単位へ移った
    AU.pageData = { doc, page, d };                   // 「前と繋ぐ」の照合に使い回す
    const textSet = new Set(texts);
    let firstBox = null;
    for (const g of d.groups) {
      if (!(g.units || []).some((un) => textSet.has(un.text))) continue;
      const bx = document.createElement("div");
      bx.className = "abox";
      bx.style.left = (g.bbox[0] / d.width * 100) + "%";
      bx.style.top = (g.bbox[1] / d.height * 100) + "%";
      bx.style.width = ((g.bbox[2] - g.bbox[0]) / d.width * 100) + "%";
      bx.style.height = ((g.bbox[3] - g.bbox[1]) / d.height * 100) + "%";
      imgbox.appendChild(bx);
      if (!firstBox) firstBox = bx;
    }
    // 語のピンポイント。**この単位の文に属する出現だけ**がサーバーから返ってくる
    // （同じブロック・同じページの別の単位の語は光らせない。→ /hits の _hit_in_texts）
    let firstWord = null;
    if (hj) {
      for (const h of hj["ヒット"] || []) {
        const [x0, y0, x1, y1] = h.rect;
        const px = 1.5, py = 1;                       // 語を囲む余白(pt)。文字ぴったりだと窮屈
        const w = document.createElement("div");
        w.className = "aword";
        w.title = h["語"];
        w.style.left = ((x0 - px) / hj.width * 100) + "%";
        w.style.top = ((y0 - py) / hj.height * 100) + "%";
        w.style.width = ((x1 - x0 + 2 * px) / hj.width * 100) + "%";
        w.style.height = ((y1 - y0 + 2 * py) / hj.height * 100) + "%";
        imgbox.appendChild(w);
        if (!firstWord) firstWord = w;
      }
    }
    const target = firstWord || firstBox;             // 語が取れなくても枠へは飛べる
    if (target) setTimeout(() => target.scrollIntoView({ block: "center", behavior: "smooth" }), 250);
  } catch (e) { /* ページ解析に失敗しても画像は見える */ }
}

function auditExportDialog() {
  const body = el(`<div>
    <p class="note">全文書ぶんをまとめて書き出します：<br>
    ・<code>抽出単位.csv</code> — 全件（除外と理由を含む<b>監査記録</b>）<br>
    ・<code>KHCoder_抽出単位.xlsx</code> — 採用のみ（KH Coder に読ませる本体）</p>
    <p class="note"><b>分割</b>を選ぶと、その列の値ごとに <code>KHCoder_抽出単位_列_値.xlsx</code> も作ります
    （年ごと・企業ごとに別プロジェクトで分析したいとき用）。</p>
    <label>分割：<select id="auditGroupBy">
      <option value="">なし（全部で1つ）</option>
      <option value="年度">年度ごと</option>
      <option value="企業名">企業ごと</option>
      <option value="群">群ごと</option>
      <option value="種別">種別ごと</option>
    </select></label></div>`);
  // ⚠️ el() は DocumentFragment：modal に渡すと中身が移るので、参照は先に取る
  const gbSel = body.querySelector("#auditGroupBy");
  modal("全冊書き出し", body, [
    { label: "書き出す", kind: "primary", run: async () => {
        const gb = gbSel.value || null;
        try {
          const j = await runJob({ kind: "export_all", group_by: gb }, "全冊を書き出しています…");
          const list = j.files.map((f) => `<li><code>${esc(f.path)}</code> — ${f["件数"]}件（${esc(f["中身"])}）</li>`).join("");
          modal("書き出しました", el(`<div><p class="note">全 ${j["全件"]} 件 ／ 採用 ${j["採用"]} 件</p><ul class="note">${list}</ul>
            <p class="note">KH Coder：新規プロジェクト → 対象ファイルに xlsx → 列「テキスト」→ 強制抽出語 → 前処理 → 共起ネットワーク（単位 H5）。</p></div>`),
            [{ label: "閉じる", kind: "ghost" }]);
        } catch (e) {
          toast(String(e.message || e), "err");
        }
      } },
    { label: "やめる", kind: "ghost" },
  ]);
}
