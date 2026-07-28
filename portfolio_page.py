# -*- coding: utf-8 -*-
"""
持倉追蹤模組（daily_screener.py 於「收盤正式版」結尾呼叫；預估版不呼叫）

產出（皆在腳本所在資料夾）：
  list_archive.json   逐日合格池歸檔——回答「我買進那天，清單上還有誰」。
                      每次執行先從 GitHub Pages 抓回線上版合併（Pages 設 keep_files，
                      檔案會保留），再加上今天，因此 CI 的乾淨環境也能逐日累積。
  portfolio.html      持倉追蹤頁。歸檔與近一年收盤價直接內嵌頁內，開頁即算。

交易記錄本身「不」存在 repo：由 portfolio.html 前端寫入使用者的私密 Gist
（與主清單頁「跨裝置同步」同一個 Gist、同一個 token，檔名 daily_screener_trades.json），
未啟用同步時退回瀏覽器 localStorage。本模組不經手交易資料。

主清單頁 daily_list.html 完全不受影響；把本模組與掛接點移除即可整組還原。
"""

import datetime as dt
import json
import os

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_FILE = os.path.join(BASE, "list_archive.json")
OUT_HTML = os.path.join(BASE, "portfolio.html")

PAGES_URL = "https://josh59408-ui.github.io/daily-stock-screener/"

ARCHIVE_KEEP = 250   # 歸檔保留天數（約一年交易日）
PRICE_DAYS = 260     # 每檔內嵌近 N 個交易日收盤價（需 >= ARCHIVE_KEEP）


# ---------------------------------------------------------------- 歸檔

def _load_local_archive():
    try:
        with open(ARCHIVE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _fetch_pages_archive():
    """從 Pages 抓回線上歸檔（CI 每次都是乾淨環境，靠這個接續昨天的資料）"""
    try:
        r = requests.get(PAGES_URL + "list_archive.json",
                         timeout=20, headers={"Cache-Control": "no-cache"})
        if r.ok:
            data = r.json()
            if isinstance(data, dict):
                return data
        print(f"線上歸檔回應 HTTP {r.status_code}，視為尚未建立")
    except Exception as e:
        print(f"線上歸檔讀取失敗（{type(e).__name__}: {e}），視為尚未建立")
    return {}


def _valid_date(k):
    try:
        dt.date.fromisoformat(k)
        return True
    except ValueError:
        return False


def update_archive(ref_date, tickers):
    """合併 線上 Pages 版 + 本機檔 + 今日名單，寫回 list_archive.json"""
    arch = _fetch_pages_archive()
    arch.update(_load_local_archive())          # 本機（手動補跑）優先於線上
    arch = {k: v for k, v in arch.items()
            if _valid_date(k) and isinstance(v, list)}
    arch[ref_date.isoformat()] = sorted(set(tickers))
    keep = sorted(arch)[-ARCHIVE_KEEP:]
    arch = {k: arch[k] for k in keep}
    with open(ARCHIVE_FILE, "w", encoding="utf-8") as f:
        json.dump(arch, f, ensure_ascii=False, separators=(",", ":"))
    print(f"清單歸檔：累積 {len(arch)} 個交易日"
          f"（{keep[0]} ~ {keep[-1]}，今日 {len(arch[keep[-1]])} 檔）")
    return arch


# ---------------------------------------------------------------- 價格資料

def build_price_data(arch, frames, names):
    """歸檔內出現過的每檔股票，內嵌近 PRICE_DAYS 日收盤價（還原權值價，
    沿用 history_cache 的除權息重抓機制，報酬率不因配股配息失真）"""
    universe = sorted({t for lst in arch.values() for t in lst})
    out, missing = {}, 0
    for t in universe:
        df = frames.get(t)
        if df is None or not len(df):
            missing += 1
            continue
        closes = df["Close"].iloc[-PRICE_DAYS:]
        out[t] = {
            "n": names.get(t, ""),
            "d": [i.date().isoformat() for i in closes.index],
            "c": [round(float(x), 2) for x in closes],
        }
    print(f"價格內嵌：{len(out)} 檔（歸檔曾出現 {len(universe)} 檔，"
          f"缺價格 {missing} 檔）")
    return out


# ---------------------------------------------------------------- 產生頁面

def generate(ref_date, list_tickers, frames, names):
    arch = update_archive(ref_date, list_tickers)
    prices = build_price_data(arch, frames, names)
    html = (PAGE_TEMPLATE
            .replace("__PAGE_DATE__", ref_date.isoformat())
            .replace("__ARCHIVE__", json.dumps(arch, separators=(",", ":")))
            .replace("__PRICES__", json.dumps(prices, ensure_ascii=False,
                                              separators=(",", ":"))))
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"已輸出 {OUT_HTML}")


# ---------------------------------------------------------------- 頁面模板
# 注意：一般字串（非 f-string），佔位符以 __TOKEN__ 置換，CSS/JS 大括號免跳脫

PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>持倉追蹤</title>
<style>
  :root { --bg:#101418; --panel:#1a2027; --line:#2a323c;
          --txt:#d7dde3; --dim:#8a94a0; --up:#e5484d; --dn:#3dd68c;
          --acc:#f5b93e; --new:#4cc2ff; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--txt);
         font-family:"Noto Sans TC",system-ui,sans-serif; padding:24px 16px 64px; }
  main { max-width:860px; margin:0 auto; }
  h1 { font-size:1.15rem; letter-spacing:.12em; margin:0; }
  .date { color:var(--dim); font-size:.85rem; margin:4px 0 24px;
          font-variant-numeric:tabular-nums; }
  a { color:var(--txt); text-decoration:none; border-bottom:1px dotted var(--dim); }
  a:hover { color:var(--acc); }
  a.switch { margin-left:12px; font-size:.8rem; color:var(--dim); }
  h2 { font-size:.9rem; color:var(--acc); letter-spacing:.2em;
       border-left:3px solid var(--acc); padding-left:10px; margin:32px 0 12px; }
  h2 small { color:var(--dim); letter-spacing:0; font-weight:400; margin-left:8px; }
  .form { background:var(--panel); border:1px solid var(--line); border-radius:6px;
          padding:14px 16px; display:flex; flex-wrap:wrap; gap:10px 12px;
          align-items:flex-end; }
  .form label { display:flex; flex-direction:column; gap:4px;
                font-size:.72rem; color:var(--dim); }
  .form input { background:var(--bg); border:1px solid var(--line); color:var(--txt);
                padding:7px 10px; border-radius:4px; font:inherit; font-size:.85rem; }
  .form input:focus { outline:none; border-color:var(--acc); }
  #f-code { width:90px; } #f-date { width:150px; } #f-price { width:100px; }
  #f-amt { width:110px; } #f-odds { width:90px; } #f-note { width:160px; }
  #f-name { font-size:.8rem; color:var(--new); min-width:70px; padding-bottom:9px; }
  button { background:none; border:1px solid var(--line); color:var(--dim);
           font:inherit; font-size:.78rem; padding:6px 14px; border-radius:4px;
           cursor:pointer; }
  button:hover { color:var(--acc); border-color:var(--acc); }
  button.pri { color:var(--acc); border-color:var(--acc); }
  button.sm { font-size:.7rem; padding:2px 8px; }
  button.danger:hover { color:var(--up); border-color:var(--up); }
  .summary { color:var(--dim); font-size:.82rem; margin:10px 2px 0; line-height:1.9; }
  .summary b { color:var(--txt); font-weight:500; }
  table { width:100%; border-collapse:collapse; background:var(--panel);
          border:1px solid var(--line); font-size:.86rem; }
  th { text-align:right; color:var(--dim); font-weight:500; padding:9px 10px;
       border-bottom:1px solid var(--line); font-size:.74rem; white-space:nowrap; }
  th:first-child, td:first-child { text-align:left; }
  td { padding:9px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
  td.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
  .pos { color:var(--up); } .neg { color:var(--dn); }
  .drop { color:var(--up); font-size:.72rem; border:1px solid var(--up);
          border-radius:3px; padding:1px 5px; margin-left:6px; white-space:nowrap; }
  .onlist { color:var(--dim); font-size:.72rem; margin-left:6px; }
  .odds { color:var(--acc); }
  .note { color:var(--dim); font-size:.76rem; display:block; margin-top:2px; }
  .peer { font-size:.76rem; color:var(--dim); display:block; margin-top:2px;
          white-space:nowrap; }
  .empty { color:var(--dim); background:var(--panel); border:1px solid var(--line);
           border-top:none; padding:14px 16px; font-size:.85rem; }
  .sellbox { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-top:6px; }
  .sellbox input { background:var(--bg); border:1px solid var(--line); color:var(--txt);
                   padding:4px 8px; border-radius:4px; font:inherit; font-size:.78rem;
                   width:120px; }
  details.bl { margin-top:26px; color:var(--dim); font-size:.85rem; line-height:2.0; }
  details.bl summary { cursor:pointer; }
  #sync-status { margin-left:10px; font-size:.78rem; color:var(--dim); }
  #io-text { width:100%; background:var(--bg); border:1px solid var(--line);
             color:var(--txt); border-radius:4px; padding:8px 10px;
             font:inherit; font-size:.78rem; font-family:ui-monospace,monospace; }
  #io-text:focus { outline:none; border-color:var(--acc); }
  code { color:var(--new); font-size:.9em; word-break:break-all; }
  footer { color:var(--dim); font-size:.75rem; margin-top:40px; line-height:1.8; }
  @media (max-width:640px) {
    body { padding:16px 8px 48px; }
    table { font-size:.78rem; }
    th, td { padding:7px 5px; }
    .hide-m { display:none; }
  }
</style></head><body><main>
<h1>持倉追蹤</h1>
<div class="date">資料日 __PAGE_DATE__・還原權值價
<a class="switch" href="daily_list.html">⇄ 每日清單</a>
<span id="sync-status"></span></div>

<h2>新增交易 <small>買進即記，賠率填你手算的值</small></h2>
<div class="form">
  <label>代號<input id="f-code" inputmode="numeric" maxlength="4" placeholder="2330"></label>
  <span id="f-name"></span>
  <label>買進日<input id="f-date" type="date"></label>
  <label>買進價<input id="f-price" inputmode="decimal" placeholder="自動帶收盤"></label>
  <label>投入金額<input id="f-amt" inputmode="numeric" placeholder="元，選填"></label>
  <label>賠率<input id="f-odds" placeholder="如 2.5"></label>
  <label>備註<input id="f-note" placeholder="選填"></label>
  <button class="pri" id="f-add">新增</button>
</div>

<h2>未平倉 <small id="open-count"></small></h2>
<div class="summary" id="open-summary"></div>
<table><thead><tr>
  <th>股票</th><th>買進日</th><th>買進價</th><th>現價</th>
  <th>報酬</th><th>vs 買進日清單</th><th class="hide-m">賠率／備註</th><th></th>
</tr></thead><tbody id="open-body"></tbody></table>
<div class="empty" id="open-empty" style="display:none">
  尚無持倉。上方輸入代號即可記錄第一筆交易。</div>

<details class="bl" id="closed-box"><summary>已平倉（<span id="closed-count">0</span> 筆）</summary>
<div class="summary" id="closed-summary"></div>
<table style="margin-top:10px"><thead><tr>
  <th>股票</th><th>買進→賣出</th><th>價格</th><th>報酬</th>
  <th>vs 同期清單</th><th class="hide-m">賠率／備註</th><th></th>
</tr></thead><tbody id="closed-body"></tbody></table></details>

<details class="bl"><summary>⇅ 批次匯入／匯出（截圖交由 Claude 整理後貼入）</summary>
<p style="line-height:1.9">把券商成交截圖傳給 Claude 整理成 JSON 貼進下方框按「匯入」，
依 id 去重合併，不會蓋掉既有記錄；「匯出」則把目前全部記錄倒進框內供備份。<br>
格式：<code>[{"t":"2330","buy_d":"2026-07-01","buy_p":1050,"amt":105000,"odds":"2.5",
"note":"","sell_d":null,"sell_p":null,"r_pct":null,"r_amt":null}]</code><br>
amt＝買進投入金額；賣出後 r_pct／r_amt＝券商結算的實際報酬%與損益金額（含費稅，選填）；
代號可省略 .TW／.TWO</p>
<textarea id="io-text" rows="5" spellcheck="false"></textarea>
<div style="margin-top:8px"><button id="io-import" class="pri">匯入</button>
<button id="io-export">匯出</button><span id="io-msg" style="margin-left:10px;font-size:.78rem"></span></div>
</details>

<footer>
交易記錄與主清單頁共用「跨裝置同步」：主清單頁啟用過 Gist 同步的裝置，
此頁自動用同一個 token 把交易存到同一個私密 Gist（檔名 daily_screener_trades.json）；
未啟用則僅存本機瀏覽器。<br>
「vs 買進日清單」＝你買進當天合格池裡的其他股票，從那天到最新資料日的漲跌
（平均／中位數，以及你贏過幾檔）。已平倉者比較區間為買進日→賣出日。
比較基準固定為買進日名單，不隨清單每日汰換飄移。<br>
報酬欄大字為「實際報酬」（你的成交價起算）；小字「同基準」為該股以買進日收盤價起算的報酬，
與同儕股票同一把尺——「贏幾檔」與摘要統計皆以同基準計算，
排除盤中成交價高低造成的起跑點差異，純粹衡量選股相對表現。<br>
投入金額選填：有填則未平倉顯示估計損益金額與金額加權總報酬（依內嵌收盤價估算，未含費稅）。
賣出時可填券商結算的實際報酬%與損益金額（含費稅）——已平倉報酬以券商實際%為準顯示，
但與清單比較仍用價差同基準（清單同儕為無摩擦報酬，混入你的費稅反而不公平）。
心算參考：現股買賣來回成本約 0.6%（手續費依折數 + 賣出證交稅 0.3%），
價差報酬扣掉它約等於實際落袋。
未實現損益%不需自行輸入，頁面每日以最新收盤即時計算。<br>
價格為還原權值價（含除權息調整），與買進成本價計算之報酬可能與券商損益略有差異，
用於「與清單其他股票比較」時基準一致。<br>
清單剔除 ≠ 賣出訊號：股票被剔除後仍持續追蹤到你記錄賣出為止，剔除天數僅供參考。
</footer>
</main>
<script>
var PAGE_DATE = '__PAGE_DATE__';
var ARCHIVE = __ARCHIVE__;
var PRICES = __PRICES__;
var TRADES_FILE = 'daily_screener_trades.json';
var SYNC_FILE = 'daily_screener_sync.json';

var ARCH_DATES = Object.keys(ARCHIVE).sort();
var LATEST_ARCH = ARCH_DATES[ARCH_DATES.length - 1] || '';

function lsGet(k, d) { try { return JSON.parse(localStorage.getItem(k)) || d; } catch (e) { return d; } }
function lsSet(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }

var trades = lsGet('ds_trades', []);
var syncToken = lsGet('ds_sync_token', '');   // 與主清單頁同一把 token
var gistId = lsGet('ds_gist_id', '');
var pushTimer = null;

// ---------- 小工具 ----------
function fmtPct(x) {
  var s = (x * 100).toFixed(1) + '%';
  return (x > 0 ? '+' : '') + s;
}
function fmtAmt(x) {   // 帶正負號的千分位金額
  var s = Math.round(Math.abs(x)).toLocaleString('en-US');
  return (x < 0 ? '-' : '+') + s;
}
function pctCls(x) { return x > 0 ? 'pos' : (x < 0 ? 'neg' : ''); }
function median(a) {
  if (!a.length) return null;
  var s = a.slice().sort(function (x, y) { return x - y; });
  var m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}
function resolveTicker(code) {
  if (PRICES[code + '.TW']) return code + '.TW';
  if (PRICES[code + '.TWO']) return code + '.TWO';
  return null;
}
// 已排序日期陣列中，最後一個 <= date 的索引；無則 -1
function idxOnOrBefore(dArr, date) {
  for (var i = dArr.length - 1; i >= 0; i--) if (dArr[i] <= date) return i;
  return -1;
}
function archDateOnOrBefore(date) {
  for (var i = ARCH_DATES.length - 1; i >= 0; i--)
    if (ARCH_DATES[i] <= date) return ARCH_DATES[i];
  return null;
}
function daysBetween(d1, d2) {
  return Math.round((new Date(d2) - new Date(d1)) / 86400000);
}

// ---------- 同期清單比較 ----------
// 同基準報酬：自己的股票也用「買進日名單基準日收盤 → 終點」計算，
// 與同儕完全同一把尺（排除盤中成交價高低造成的起跑點差異）
function baseRet(trade, endDate) {
  var ad = archDateOnOrBefore(trade.buy_d);
  var p = PRICES[trade.t];
  if (!ad || !p) return null;
  var i0 = idxOnOrBefore(p.d, ad);
  if (i0 < 0 || p.d[i0] !== ad) return null;
  var i1 = endDate ? idxOnOrBefore(p.d, endDate) : p.c.length - 1;
  if (i1 < 0 || i1 < i0) return null;
  return p.c[i1] / p.c[i0] - 1;
}
// endDate 為 null → 比到最新資料日（未平倉）；否則比到賣出日（已平倉）
function peerStats(trade, endDate) {
  var ad = archDateOnOrBefore(trade.buy_d);
  if (!ad) return null;
  var rets = [];
  ARCHIVE[ad].forEach(function (t) {
    if (t === trade.t) return;
    var p = PRICES[t];
    if (!p) return;
    var i0 = idxOnOrBefore(p.d, ad);
    if (i0 < 0 || p.d[i0] !== ad) return;   // 基準日缺 K（停牌）→ 不列入
    var i1 = endDate ? idxOnOrBefore(p.d, endDate) : p.c.length - 1;
    if (i1 < 0 || i1 <= i0 && endDate) return;
    rets.push(p.c[i1] / p.c[i0] - 1);
  });
  if (!rets.length) return null;
  return { archDate: ad, n: rets.length,
           avg: rets.reduce(function (a, b) { return a + b; }, 0) / rets.length,
           med: median(rets), rets: rets };
}
function dropInfo(t) {
  if (!LATEST_ARCH) return null;
  if (ARCHIVE[LATEST_ARCH].indexOf(t) >= 0) return { on: true };
  for (var i = ARCH_DATES.length - 1; i >= 0; i--)
    if (ARCHIVE[ARCH_DATES[i]].indexOf(t) >= 0)
      return { on: false, days: daysBetween(ARCH_DATES[i], LATEST_ARCH) };
  return { on: false, days: null };   // 追蹤範圍內不曾入榜
}

// ---------- Gist 同步（沿用主清單頁機制）----------
function setSync(msg) { document.getElementById('sync-status').textContent = msg; }
function gh(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({
    'Authorization': 'Bearer ' + syncToken,
    'Accept': 'application/vnd.github+json'
  }, opts.headers || {});
  return fetch('https://api.github.com' + path, opts).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  });
}
function findGist() {
  if (gistId) return Promise.resolve(gistId);
  return gh('/gists?per_page=100').then(function (list) {
    var g = list.find(function (x) {
      return x.files && (x.files[SYNC_FILE] || x.files[TRADES_FILE]);
    });
    if (g) { gistId = g.id; lsSet('ds_gist_id', gistId); }
    return gistId || null;
  });
}
function pullTrades() {
  if (!syncToken) return Promise.resolve();
  setSync('☁ 同步中…');
  return findGist().then(function (id) {
    if (!id) return null;
    return gh('/gists/' + id).then(function (g) {
      var f = g.files[TRADES_FILE];
      if (!f) return null;
      var st = JSON.parse(f.content);
      if (st && Array.isArray(st.trades)) { trades = st.trades; lsSet('ds_trades', trades); }
    });
  }).then(function () { setSync('☁ 已同步 ' + new Date().toLocaleTimeString()); })
    .catch(function (e) { setSync('☁ 同步失敗：' + e.message + '（僅存本機）'); });
}
function pushNow() {
  if (!syncToken) return;
  var files = {};
  files[TRADES_FILE] = { content: JSON.stringify({ trades: trades, ts: Date.now() }) };
  var p = gistId
    ? gh('/gists/' + gistId, { method: 'PATCH', body: JSON.stringify({ files: files }) })
    : gh('/gists', { method: 'POST', body: JSON.stringify(
        { description: '每日股票清單 跨裝置同步', public: false, files: files }) })
        .then(function (g) { gistId = g.id; lsSet('ds_gist_id', gistId); });
  p.then(function () { setSync('☁ 已同步 ' + new Date().toLocaleTimeString()); })
   .catch(function (e) { setSync('☁ 同步失敗：' + e.message + '（僅存本機）'); });
}
function schedulePush() {
  if (!syncToken) { setSync('僅存本機（到主清單頁啟用跨裝置同步可雲端備份）'); return; }
  clearTimeout(pushTimer);
  pushTimer = setTimeout(pushNow, 1200);
}
function persist() { lsSet('ds_trades', trades); schedulePush(); }

// ---------- 新增表單 ----------
var elCode = document.getElementById('f-code');
var elName = document.getElementById('f-name');
var elDate = document.getElementById('f-date');
var elPrice = document.getElementById('f-price');
elDate.value = PAGE_DATE;
elDate.max = PAGE_DATE;

function refreshFormPrice() {
  var t = resolveTicker(elCode.value.trim());
  if (!t) { elName.textContent = elCode.value.length === 4 ? '不在追蹤範圍' : ''; return; }
  var p = PRICES[t];
  elName.textContent = p.n || t;
  var i = idxOnOrBefore(p.d, elDate.value || PAGE_DATE);
  if (i >= 0 && !elPrice.dataset.touched) elPrice.value = p.c[i];
}
elCode.addEventListener('input', function () { delete elPrice.dataset.touched; refreshFormPrice(); });
elDate.addEventListener('change', refreshFormPrice);
elPrice.addEventListener('input', function () { elPrice.dataset.touched = 1; });

document.getElementById('f-add').addEventListener('click', function () {
  var code = elCode.value.trim();
  var t = resolveTicker(code);
  var price = parseFloat(elPrice.value);
  if (!t) { alert('代號不在追蹤範圍（近一年內曾入榜的股票才有價格資料）'); return; }
  if (!elDate.value) { alert('請選買進日'); return; }
  if (!(price > 0)) { alert('請填買進價'); return; }
  var amt = parseFloat(document.getElementById('f-amt').value);
  trades.push({
    id: Date.now() + '-' + code,
    t: t, buy_d: elDate.value, buy_p: price,
    amt: amt > 0 ? amt : null,
    odds: document.getElementById('f-odds').value.trim(),
    note: document.getElementById('f-note').value.trim(),
    sell_d: null, sell_p: null, r_amt: null, r_pct: null
  });
  elCode.value = ''; elPrice.value = ''; elName.textContent = '';
  document.getElementById('f-amt').value = '';
  document.getElementById('f-odds').value = '';
  document.getElementById('f-note').value = '';
  delete elPrice.dataset.touched;
  persist(); render();
});

// ---------- 呈現 ----------
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}
function stockCell(tr) {
  var p = PRICES[tr.t] || {};
  var code = tr.t.split('.')[0];
  var di = dropInfo(tr.t);
  var tag = '';
  if (di) {
    if (di.on) tag = "<span class='onlist'>在榜</span>";
    else if (di.days != null) tag = "<span class='drop'>已剔除 " + di.days + " 天</span>";
    else tag = "<span class='drop'>不在榜</span>";
  }
  return "<td><a href='https://tw.tradingview.com/chart/?symbol=" +
         (tr.t.indexOf('.TWO') > 0 ? 'TPEX' : 'TWSE') + '%3A' + code +
         "' target='_blank'>" + code + ' ' + esc(p.n || '') + '</a>' + tag + '</td>';
}
function oddsNoteCell(tr) {
  return "<td class='hide-m'>" +
         (tr.odds ? "<span class='odds'>賠率 " + esc(tr.odds) + '</span>' : '') +
         (tr.note ? "<span class='note'>" + esc(tr.note) + '</span>' : '') + '</td>';
}
function peerCell(myRet, ps) {
  if (!ps) return '<td class="num">—</td>';
  var beat = ps.rets.filter(function (r) { return myRet > r; }).length;
  return "<td class='num'>平均 <span class='" + pctCls(ps.avg) + "'>" + fmtPct(ps.avg) +
         "</span>・中位 <span class='" + pctCls(ps.med) + "'>" + fmtPct(ps.med) + '</span>' +
         "<span class='peer'>贏 " + beat + '／' + ps.n + ' 檔（' + ps.archDate + ' 名單）</span></td>';
}

function render() {
  var open = trades.filter(function (x) { return !x.sell_d; });
  var closed = trades.filter(function (x) { return x.sell_d; });
  document.getElementById('open-count').textContent = open.length + ' 檔';
  document.getElementById('open-empty').style.display = open.length ? 'none' : '';
  document.getElementById('closed-count').textContent = closed.length;

  var beatAvgCnt = 0, withPeer = 0, retSum = 0, retCnt = 0;
  var investSum = 0, plSum = 0;
  var body = open.map(function (tr) {
    var p = PRICES[tr.t];
    var cur = null, myRet = null;
    if (p) { cur = p.c[p.c.length - 1]; myRet = cur / tr.buy_p - 1; }
    var bRet = baseRet(tr, null);           // 與清單同一把尺的報酬
    var cmpRet = bRet != null ? bRet : myRet;   // 比較用；無基準時退回實際報酬
    var ps = peerStats(tr, null);
    if (myRet != null) { retSum += myRet; retCnt++; }
    if (cmpRet != null && ps) { withPeer++; if (cmpRet > ps.avg) beatAvgCnt++; }
    var estAmt = (tr.amt > 0 && myRet != null) ? tr.amt * myRet : null;
    if (tr.amt > 0 && myRet != null) { investSum += tr.amt; plSum += estAmt; }
    var sell = "<button class='sm sell' data-id='" + tr.id + "'>賣出</button> " +
               "<button class='sm danger del' data-id='" + tr.id + "'>刪</button>" +
               "<div class='sellbox' id='sb-" + tr.id + "' style='display:none'>" +
               "<input type='date' class='s-date' value='" + PAGE_DATE + "' max='" + PAGE_DATE + "'>" +
               "<input class='s-price' inputmode='decimal' placeholder='賣出價' value='" +
               (cur != null ? cur : '') + "'>" +
               "<input class='s-pct' inputmode='decimal' placeholder='券商實際%（選填）'>" +
               "<input class='s-amt' inputmode='numeric' placeholder='實際損益金額（選填）'>" +
               "<button class='sm ok' data-id='" + tr.id + "'>確認</button>" +
               "<button class='sm cancel' data-id='" + tr.id + "'>取消</button></div>";
    return '<tr>' + stockCell(tr) +
      "<td class='num'>" + tr.buy_d + '</td>' +
      "<td class='num'>" + tr.buy_p + '</td>' +
      "<td class='num'>" + (cur != null ? cur : '<span class="note">無價格資料</span>') + '</td>' +
      "<td class='num " + (myRet != null ? pctCls(myRet) : '') + "'>" +
        (myRet != null ? fmtPct(myRet) : '—') +
        (estAmt != null ? "<span class='peer'>估損益 <span class='" + pctCls(estAmt) +
          "'>" + fmtAmt(estAmt) + '</span> 元</span>' : '') +
        (bRet != null ? "<span class='peer'>同基準 <span class='" + pctCls(bRet) +
          "'>" + fmtPct(bRet) + '</span></span>' : '') + '</td>' +
      (cmpRet != null ? peerCell(cmpRet, ps) : '<td class="num">—</td>') +
      oddsNoteCell(tr) + "<td class='num'>" + sell + '</td></tr>';
  }).join('');
  document.getElementById('open-body').innerHTML = body;

  var sm = document.getElementById('open-summary');
  sm.innerHTML = retCnt
    ? '平均報酬 <b class="' + pctCls(retSum / retCnt) + '">' + fmtPct(retSum / retCnt) +
      '</b>' + (withPeer ? '・<b>' + beatAvgCnt + '／' + withPeer +
      '</b> 檔贏過買進日清單同期平均（同基準計）' : '') +
      (investSum > 0 ? '<br>投入 ' + Math.round(investSum).toLocaleString('en-US') +
        ' 元・估計損益 <b class="' + pctCls(plSum) + '">' + fmtAmt(plSum) +
        '</b> 元・金額加權 <b class="' + pctCls(plSum / investSum) + '">' +
        fmtPct(plSum / investSum) + '</b>（未含費稅）' : '')
    : '';

  var rAmtSum = 0, rAmtAny = false;
  document.getElementById('closed-body').innerHTML = closed.map(function (tr) {
    var priceRet = tr.sell_p / tr.buy_p - 1;
    var myRet = tr.r_pct != null ? tr.r_pct / 100 : priceRet;   // 券商實際%優先
    var bRet = baseRet(tr, tr.sell_d);
    var cmpRet = bRet != null ? bRet : priceRet;
    var ps = peerStats(tr, tr.sell_d);
    if (tr.r_amt != null) { rAmtSum += tr.r_amt; rAmtAny = true; }
    return '<tr>' + stockCell(tr) +
      "<td class='num'>" + tr.buy_d + '→' + tr.sell_d +
        "<span class='peer'>持有 " + daysBetween(tr.buy_d, tr.sell_d) + ' 天</span></td>' +
      "<td class='num'>" + tr.buy_p + '→' + tr.sell_p + '</td>' +
      "<td class='num " + pctCls(myRet) + "'>" + fmtPct(myRet) +
        (tr.r_pct != null ? "<span class='peer'>券商實際（含費稅）・價差 " +
          fmtPct(priceRet) + '</span>' : '') +
        (tr.r_amt != null ? "<span class='peer'>損益 <span class='" + pctCls(tr.r_amt) +
          "'>" + fmtAmt(tr.r_amt) + '</span> 元</span>' : '') +
        (bRet != null ? "<span class='peer'>同基準 <span class='" + pctCls(bRet) +
          "'>" + fmtPct(bRet) + '</span></span>' : '') + '</td>' +
      peerCell(cmpRet, ps) + oddsNoteCell(tr) +
      "<td class='num'><button class='sm unsell' data-id='" + tr.id +
      "'>撤銷賣出</button> <button class='sm danger del' data-id='" + tr.id +
      "'>刪</button></td></tr>";
  }).join('');
  document.getElementById('closed-summary').innerHTML = rAmtAny
    ? '已實現總損益 <b class="' + pctCls(rAmtSum) + '">' + fmtAmt(rAmtSum) +
      '</b> 元（券商實際，含費稅）'
    : '';
}

function findTrade(id) {
  for (var i = 0; i < trades.length; i++) if (trades[i].id === id) return trades[i];
  return null;
}
document.addEventListener('click', function (e) {
  var b = e.target;
  if (!b.classList || !b.dataset) return;
  var id = b.dataset.id;
  if (b.classList.contains('sell')) {
    var sb = document.getElementById('sb-' + id);
    if (sb) sb.style.display = sb.style.display === 'none' ? 'flex' : 'none';
  } else if (b.classList.contains('cancel')) {
    document.getElementById('sb-' + id).style.display = 'none';
  } else if (b.classList.contains('ok')) {
    var box = document.getElementById('sb-' + id);
    var d = box.querySelector('.s-date').value;
    var p = parseFloat(box.querySelector('.s-price').value);
    var rp = parseFloat(box.querySelector('.s-pct').value);
    var ra = parseFloat(box.querySelector('.s-amt').value);
    var tr = findTrade(id);
    if (!tr || !d || !(p > 0)) { alert('請填賣出日與賣出價'); return; }
    if (d < tr.buy_d) { alert('賣出日不可早於買進日'); return; }
    tr.sell_d = d; tr.sell_p = p;
    tr.r_pct = isNaN(rp) ? null : rp;
    tr.r_amt = isNaN(ra) ? null : ra;
    persist(); render();
  } else if (b.classList.contains('unsell')) {
    var t2 = findTrade(id);
    if (t2) { t2.sell_d = null; t2.sell_p = null;
              t2.r_pct = null; t2.r_amt = null; persist(); render(); }
  } else if (b.classList.contains('del')) {
    var t3 = findTrade(id);
    if (t3 && confirm('刪除這筆交易記錄？（' + t3.t.split('.')[0] + ' ' +
        t3.buy_d + '）此動作無法復原')) {
      trades = trades.filter(function (x) { return x.id !== id; });
      persist(); render();
    }
  }
});

// ---------- 批次匯入／匯出 ----------
function ioMsg(s) { document.getElementById('io-msg').textContent = s; }
document.getElementById('io-export').addEventListener('click', function () {
  document.getElementById('io-text').value = JSON.stringify(trades, null, 1);
  ioMsg('已匯出 ' + trades.length + ' 筆，全選複製即可備份');
});
document.getElementById('io-import').addEventListener('click', function () {
  try {
    var arr = JSON.parse(document.getElementById('io-text').value);
    if (!Array.isArray(arr)) throw new Error('最外層必須是陣列 [ ... ]');
    var seen = {}, fp = {};   // id 去重 + 內容指紋去重（同檔同日同價視為同一筆）
    trades.forEach(function (x) {
      seen[x.id] = 1;
      fp[x.t + '|' + x.buy_d + '|' + x.buy_p] = 1;
    });
    var added = 0, skipped = 0, noPrice = [];
    arr.forEach(function (x) {
      if (!x || !x.t || !x.buy_d || !(+x.buy_p > 0)) { skipped++; return; }
      var t = String(x.t).trim();
      if (t.indexOf('.') < 0) {           // 代號未帶後綴 → 自動判斷上市/上櫃
        var r = resolveTicker(t);
        if (!r) { noPrice.push(t); skipped++; return; }
        t = r;
      }
      var id = x.id ? String(x.id) : Date.now() + '-' + t.split('.')[0] + '-' + added;
      var key = t + '|' + String(x.buy_d) + '|' + (+x.buy_p);
      if (seen[id] || fp[key]) { skipped++; return; }
      fp[key] = 1;
      trades.push({ id: id, t: t, buy_d: String(x.buy_d), buy_p: +x.buy_p,
                    amt: +x.amt > 0 ? +x.amt : null,
                    odds: x.odds == null ? '' : String(x.odds),
                    note: x.note == null ? '' : String(x.note),
                    sell_d: x.sell_d || null,
                    sell_p: +x.sell_p > 0 ? +x.sell_p : null,
                    r_pct: (x.r_pct == null || isNaN(+x.r_pct)) ? null : +x.r_pct,
                    r_amt: (x.r_amt == null || isNaN(+x.r_amt)) ? null : +x.r_amt });
      seen[id] = 1; added++;
    });
    persist(); render();
    ioMsg('匯入 ' + added + ' 筆' +
          (skipped ? '，略過 ' + skipped + ' 筆' : '') +
          (noPrice.length ? '（不在追蹤範圍：' + noPrice.join('、') + '）' : ''));
  } catch (e) { ioMsg('匯入失敗：' + e.message); }
});

// 啟動：先用本機資料立即顯示，有 token 再拉雲端覆蓋重繪
render();
if (syncToken) pullTrades().then(render);
else setSync('僅存本機（到主清單頁啟用跨裝置同步可雲端備份）');
</script></body></html>"""
