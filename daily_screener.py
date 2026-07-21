# -*- coding: utf-8 -*-
"""
每日股票清單系統（排程版）
邏輯：Minervini 趨勢樣板位階濾網，產出合格池清單
合格池內另標記「過」＝三盤過+紅K+量增（進場仍由使用者自行看圖確認）

執行：
  python daily_screener.py --preview   盤中預估版（排程 12:55，LINE 約 13:00 送達）
  python daily_screener.py --final     收盤正式版（排程 13:55，LINE 約 14:00 送達）
  加 --force   資料非今日也照跑（手動補跑用）
  加 --no-push 只產檔不推播（測試用）

產出： daily_list.html / daily_list_preview.html 與對應 CSV（皆在腳本所在資料夾）
"""

import argparse
import io
import json
import os
import sys
import time
import datetime as dt

import numpy as np
import pandas as pd
import requests
import yfinance as yf

try:
    sys.stdout.reconfigure(line_buffering=True)
except AttributeError:
    pass

# ========================= 設定區（可調整的都在這） =========================

LINE_TOKEN = os.environ.get("LINE_TOKEN", "").strip()      # Channel access token
LINE_USER_ID = os.environ.get("LINE_USER_ID", "").strip()  # U 開頭的 user ID

LOOKBACK = 250          # 52 週高低點的回看天數（交易日）
MA_SHORT = 55           # 照你的腳本用 55MA（書上是 50）
MA_MID = 150
MA_LONG = 200
MA200_RISING_DAYS = 22  # 200MA 上升判定：今日 > 22 個交易日（約 1 個月）前
RS_THRESHOLD = 70       # RS 百分位門檻（0-100）
BIAS_WARN = 40          # 55MA 乖離超過此 % 時橘字警示（過熱、追進風險高）

# 三盤過+紅K+量增 訊號（與 三盤過訊號.pine 預設一致）
SIG_LOOKBACK = 2        # 三盤過：收盤需突破前 N 根 K 棒最高點（含今日共三盤）
SIG_VOL_MULT = 1.0      # 量增門檻：今日量 > 昨日量 × 此倍數

MIN_AVG_VOL_SHARES = 500_000   # 流動性過濾：20 日均量 > 500 張（1 張 = 1000 股）
MIN_BARS = 260                 # 上市未滿 260 個交易日（約一年）的股票排除
MIN_PRICE = 10.0               # 排除低價股（不想過濾就改成 0）

# 排除產業：產業別含任一關鍵字即不進清單（RS 排名仍計入全市場，分數意義不變）
EXCLUDE_INDUSTRIES = ("金融", "保險", "生技醫療")

COVERAGE_WARN = 0.98    # 有效資料低於母體 98% 時在 LINE 警告

BASE = os.path.dirname(os.path.abspath(__file__))

# ========================= 1. 取得上市櫃股票清單 =========================

def fetch_universe():
    """從 TWSE ISIN 網頁抓上市(.TW)與上櫃(.TWO)普通股清單"""
    urls = {
        ".TW": "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2",   # 上市
        ".TWO": "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4",  # 上櫃
    }
    tickers, names, inds = [], {}, {}
    for suffix, url in urls.items():
        r = requests.get(url, timeout=30)
        r.encoding = "big5"
        tables = pd.read_html(io.StringIO(r.text))
        df = tables[0]
        df.columns = df.iloc[0]
        df = df.iloc[1:]
        # CFICode = ESVUFR 是普通股；順便排除 ETF、權證、特別股等
        df = df[df["CFICode"] == "ESVUFR"]
        for _, row in df.iterrows():
            parts = str(row[df.columns[0]]).split("　")  # 全形空白分隔「代號　名稱」
            if len(parts) != 2:
                continue
            code, name = parts[0].strip(), parts[1].strip()
            if len(code) == 4 and code.isdigit():  # 只留 4 碼普通股
                t = code + suffix
                tickers.append(t)
                names[t] = name
                ind = row.get("產業別", "")
                inds[t] = "" if pd.isna(ind) else str(ind).strip()
    print(f"母體：共 {len(tickers)} 檔上市櫃普通股")
    return tickers, names, inds

# ========================= 2. 下載歷史價量 =========================

def download_history(tickers, chunk=100):
    """分批向 yfinance 下載 2 年日 K（還原權值價）"""
    frames = {}
    for i in range(0, len(tickers), chunk):
        batch = tickers[i:i + chunk]
        print(f"下載中 {i + 1}-{min(i + chunk, len(tickers))} / {len(tickers)} ...")
        data = yf.download(
            batch, period="2y", interval="1d",
            auto_adjust=True, group_by="ticker",
            progress=False, threads=True,
        )
        for t in batch:
            try:
                df = data[t].dropna(subset=["Close"])
                if len(df) > 0:
                    frames[t] = df
            except (KeyError, TypeError):
                continue
        time.sleep(1)  # 對資料源客氣一點
    return frames

def download_with_retry(tickers):
    frames = download_history(tickers)
    missing = [t for t in tickers if t not in frames]
    if missing:
        print(f"第一輪缺 {len(missing)} 檔，重試一次 ...")
        frames.update(download_history(missing))
        missing = [t for t in tickers if t not in frames]
    print(f"成功取得 {len(frames)} 檔資料（缺 {len(missing)} 檔）")
    return frames

# ========================= 3. 逐檔計算條件 =========================

def analyze(frames, skip_last_in_avg=False):
    """skip_last_in_avg：預估版計算 20 日均量時排除今日未完成量"""
    rows = []
    for t, df in frames.items():
        if len(df) < MIN_BARS:
            continue  # 上市未滿約一年 → 排除

        close = df["Close"]
        high, low = df["High"], df["Low"]
        vol = df["Volume"]

        c = close.iloc[-1]
        c_prev = close.iloc[-2]

        # 流動性 / 價格過濾
        avg_vol20 = vol.iloc[-21:-1].mean() if skip_last_in_avg else vol.iloc[-20:].mean()
        if avg_vol20 < MIN_AVG_VOL_SHARES or c < MIN_PRICE:
            continue

        ma_s = close.rolling(MA_SHORT).mean().iloc[-1]
        ma_m = close.rolling(MA_MID).mean().iloc[-1]
        ma_l_series = close.rolling(MA_LONG).mean()
        ma_l = ma_l_series.iloc[-1]
        ma_l_past = ma_l_series.iloc[-1 - MA200_RISING_DAYS]

        low52 = low.iloc[-LOOKBACK:].min()
        high52 = high.iloc[-LOOKBACK:].max()

        # --- 趨勢樣板五條件（RS 之後全市場一起算） ---
        cond_ma = c > ma_s > ma_m > ma_l                # 1. 多頭排列
        cond_200_rising = ma_l > ma_l_past              # 2. 200MA 上升 ≥ 1 個月
        cond_low = c > low52 * 1.30                     # 3. 高於 52 週低點 130%
        cond_high = c > high52 * 0.75                   # 4. 在 52 週高點 75% 之上

        # --- RS 原始分數（IBD 加權式：近 3 個月 ×2） ---
        def ret(n):
            return c / close.iloc[-1 - n] - 1.0
        rs_raw = 2 * ret(63) + ret(126) + ret(189) + ret(252)

        bias55 = (c - ma_s) / ma_s * 100  # 55MA 乖離，僅顯示參考

        # --- 三盤過+紅K+量增（預估版為盤中值，以收盤為準） ---
        o = df["Open"].iloc[-1]
        prev_n_high = high.iloc[-1 - SIG_LOOKBACK:-1].max()
        signal = bool(
            c > prev_n_high                          # 三盤過
            and c > o                                # 紅K
            and vol.iloc[-1] > vol.iloc[-2] * SIG_VOL_MULT  # 量增
        )

        rows.append({
            "ticker": t, "close": round(c, 2),
            "chg_pct": round((c / c_prev - 1) * 100, 1),
            "cond_ma": cond_ma, "cond_200_rising": cond_200_rising,
            "cond_low": cond_low, "cond_high": cond_high,
            "rs_raw": rs_raw,
            "signal": signal,
            "bias55": round(bias55, 1),
            "avg_vol20_lots": int(avg_vol20 / 1000),  # 換算成「張」
        })

    result = pd.DataFrame(rows)
    if result.empty:
        return result
    # RS 百分位：全市場排名
    result["rs"] = (result["rs_raw"].rank(pct=True) * 100).round(0).astype(int)
    result["setup_ok"] = (
        result["cond_ma"] & result["cond_200_rising"]
        & result["cond_low"] & result["cond_high"]
        & (result["rs"] >= RS_THRESHOLD)
    )
    return result

# ========================= 4. 產出網頁 =========================

def tv_link(ticker):
    code, suffix = ticker.split(".")
    exch = "TWSE" if suffix == "TW" else "TPEX"
    return f"https://tw.tradingview.com/chart/?symbol={exch}%3A{code}"

def tv_symbol(ticker):
    code, suffix = ticker.split(".")
    return ("TWSE:" if suffix == "TW" else "TPEX:") + code

def short_industry(ind):
    """產業別簡寫：塑膠工業→塑膠、觀光事業→觀光、半導體業→半導體"""
    for suf in ("工業", "事業", "業"):
        if ind.endswith(suf) and len(ind) > len(suf):
            return ind.removesuffix(suf)
    return ind

# 頁面互動：汰除(blacklist)、消失警示(watch)、複製代號。狀態存瀏覽器 localStorage。
PAGE_JS = """<script>
var TODAY = __TODAY__;      // 今日合格池 [{c:代號, n:名稱}, ...]
var IS_FINAL = __IS_FINAL__;

function lsGet(k, d) { try { return JSON.parse(localStorage.getItem(k)) || d; } catch (e) { return d; } }
function lsSet(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }

var bl = new Set(lsGet('ds_blacklist', []));   // 永久汰除名單
var watch = lsGet('ds_watch', {});             // 曾進清單、預期還在 TV 清單上的股票
var namesMap = lsGet('ds_names', {});
TODAY.forEach(function (x) { namesMap[x.c] = x.n; });

// 正式版才更新「曾出現」名單（預估版盤中浮動，不記錄）
if (IS_FINAL) { TODAY.forEach(function (x) { if (!bl.has(x.c)) watch[x.c] = x.n; }); }

function persist() {
  lsSet('ds_blacklist', Array.from(bl));
  lsSet('ds_watch', watch);
  lsSet('ds_names', namesMap);
}
function render() {
  document.querySelectorAll('tr[data-code]').forEach(function (tr) {
    tr.style.display = bl.has(tr.dataset.code) ? 'none' : '';
  });
  var blArr = Array.from(bl);
  document.getElementById('bl-count').textContent = blArr.length;
  document.getElementById('bl-list').innerHTML = blArr.length
    ? blArr.map(function (c) {
        return "<span class='bl-item'>" + c + " " + (namesMap[c] || '') +
               " <button class='undo' data-c='" + c + "'>復原</button></span>";
      }).join('')
    : "<span>（目前沒有汰除的股票）</span>";
  if (IS_FINAL) {
    var todaySet = new Set(TODAY.map(function (x) { return x.c; }));
    var dropped = Object.keys(watch).filter(function (c) { return !todaySet.has(c) && !bl.has(c); });
    document.getElementById('drop-box').style.display = dropped.length ? '' : 'none';
    document.getElementById('drop-list').innerHTML = dropped.map(function (c) {
      return "<span class='drop-item'>" + c + " " + (namesMap[c] || watch[c] || '') +
             " <button class='done' data-c='" + c + "'>已從TV移除 ✓</button></span>";
    }).join('');
  }
}
document.addEventListener('click', function (e) {
  var b = e.target;
  if (!b.classList) return;
  if (b.classList.contains('del')) { bl.add(b.dataset.c); delete watch[b.dataset.c]; persist(); render(); }
  else if (b.classList.contains('undo')) { bl.delete(b.dataset.c); persist(); render(); }
  else if (b.classList.contains('done')) { delete watch[b.dataset.c]; persist(); render(); }
});
document.querySelectorAll('button.copy').forEach(function (btn) {
  btn.addEventListener('click', async function () {
    var tbl = document.getElementById(btn.dataset.target);
    var syms = Array.from(tbl.querySelectorAll('tr[data-sym]'))
      .filter(function (tr) { return tr.style.display !== 'none'; })
      .map(function (tr) { return tr.dataset.sym; }).join(',');
    try { await navigator.clipboard.writeText(syms); }
    catch (e2) {
      var t = document.createElement('textarea');
      t.value = syms; document.body.appendChild(t);
      t.select(); document.execCommand('copy'); t.remove();
    }
    var old = btn.textContent;
    btn.textContent = '已複製 ✓';
    setTimeout(function () { btn.textContent = old; }, 1500);
  });
});
// 點欄位標題排序：第一次點由大到小，再點反向
document.querySelectorAll('th[data-k]').forEach(function (th) {
  th.addEventListener('click', function () {
    var table = th.closest('table');
    var tbody = table.querySelector('tbody');
    if (!tbody) return;
    var idx = Array.prototype.indexOf.call(th.parentNode.children, th);
    var asc = th.dataset.asc === '0';
    table.querySelectorAll('th[data-k]').forEach(function (h) { if (h !== th) delete h.dataset.asc; });
    th.dataset.asc = asc ? '1' : '0';
    function val(tr) {
      var t = tr.children[idx].textContent.replace(/[,%+]/g, '');
      var n = parseFloat(t);
      return isNaN(n) ? t : n;
    }
    Array.from(tbody.querySelectorAll('tr'))
      .sort(function (a, b) {
        var va = val(a), vb = val(b);
        if (typeof va === 'string' || typeof vb === 'string')
          return String(va).localeCompare(String(vb)) * (asc ? 1 : -1);
        return (va - vb) * (asc ? 1 : -1);
      })
      .forEach(function (r) { tbody.appendChild(r); });
  });
});
persist(); render();
</script>"""

THEAD = ("<thead><tr><th data-k='1'>股票</th><th data-k='1'>收盤</th>"
         "<th data-k='1'>漲跌%</th><th data-k='1'>RS</th>"
         "<th data-k='1'>55MA乖離</th><th></th></tr></thead>")

def build_html(list_a, names, inds, date_label, final=True):
    def row_html(r):
        name = names.get(r.ticker, "")
        code = r.ticker.split(".")[0]
        ind = short_industry(inds.get(r.ticker, ""))
        tag = f"<span class='tag'>{ind}</span>" if ind else ""
        sig = "<span class='sig'>過</span>" if r.signal else ""
        chg_cls = "pos" if r.chg_pct > 0 else ("neg" if r.chg_pct < 0 else "")
        bias_cls = " warn" if r.bias55 >= BIAS_WARN else ""
        return (
            f"<tr data-code='{code}' data-sym='{tv_symbol(r.ticker)}'>"
            f"<td><a href='{tv_link(r.ticker)}' target='_blank'>{code} {name}</a>{sig}{tag}</td>"
            f"<td class='num'>{r.close}</td>"
            f"<td class='num {chg_cls}'>{r.chg_pct:+.1f}%</td>"
            f"<td class='num'>{r.rs}</td>"
            f"<td class='num{bias_cls}'>{r.bias55}%</td>"
            f"<td class='act'><button class='del' data-c='{code}' "
            f"title='汰除：之後清單永久隱藏此股票'>✕</button></td></tr>"
        )

    a_rows = "".join(row_html(r) for r in list_a.itertuples())
    btn_a = ("<button class='copy' data-target='tb-a'>📋 複製代號</button>"
             if len(list_a) else "")
    today_json = json.dumps(
        [{"c": r.ticker.split(".")[0], "n": names.get(r.ticker, "")}
         for r in list_a.itertuples()],
        ensure_ascii=False)
    page_js = (PAGE_JS
               .replace("__TODAY__", today_json)
               .replace("__IS_FINAL__", "true" if final else "false"))
    return f"""<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>每日股票清單 {date_label}</title>
<style>
  :root {{ --bg:#101418; --panel:#1a2027; --line:#2a323c;
           --txt:#d7dde3; --dim:#8a94a0; --up:#e5484d; --acc:#f5b93e; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--txt);
          font-family:"Noto Sans TC",system-ui,sans-serif; padding:24px 16px 64px; }}
  main {{ max-width:760px; margin:0 auto; }}
  h1 {{ font-size:1.15rem; letter-spacing:.12em; margin:0; }}
  .date {{ color:var(--dim); font-size:.85rem; margin:4px 0 28px;
           font-variant-numeric:tabular-nums; }}
  h2 {{ font-size:.9rem; color:var(--acc); letter-spacing:.2em;
        border-left:3px solid var(--acc); padding-left:10px; margin:36px 0 12px; }}
  h2 small {{ color:var(--dim); letter-spacing:0; font-weight:400; }}
  button.copy, button.done, button.undo {{ background:none; border:1px solid var(--line);
        color:var(--dim); font:inherit; font-size:.72rem; padding:3px 10px;
        margin-left:10px; border-radius:4px; cursor:pointer; letter-spacing:0;
        vertical-align:middle; }}
  button.copy:hover {{ color:var(--acc); border-color:var(--acc); }}
  button.done:hover, button.undo:hover {{ color:var(--txt); border-color:var(--dim); }}
  td.act {{ width:30px; text-align:center; padding:10px 6px; }}
  button.del {{ background:none; border:none; color:var(--dim); cursor:pointer;
        font-size:.85rem; opacity:.45; }}
  tr:hover button.del {{ opacity:1; }}
  button.del:hover {{ color:var(--up); }}
  #drop-box {{ border:1px solid var(--up); background:rgba(229,72,77,.08);
        color:var(--up); padding:14px 16px; margin:0 0 20px; font-size:.88rem;
        border-radius:6px; line-height:2.1; }}
  #drop-box b {{ letter-spacing:.1em; }}
  #drop-box button {{ border-color:rgba(229,72,77,.5); color:var(--up); }}
  .drop-item, .bl-item {{ display:inline-block; margin:2px 14px 2px 0; white-space:nowrap; }}
  details.bl {{ margin-top:28px; color:var(--dim); font-size:.85rem; line-height:2.1; }}
  details.bl summary {{ cursor:pointer; }}
  table {{ width:100%; border-collapse:collapse; background:var(--panel);
           border:1px solid var(--line); font-size:.9rem; }}
  th {{ text-align:right; color:var(--dim); font-weight:500; padding:10px 12px;
        border-bottom:1px solid var(--line); font-size:.78rem; }}
  th:first-child, td:first-child {{ text-align:left; }}
  th[data-k] {{ cursor:pointer; user-select:none; }}
  th[data-k]:hover {{ color:var(--txt); }}
  .tag {{ color:var(--dim); font-size:.72rem; margin-left:8px; white-space:nowrap; }}
  .sig {{ color:var(--acc); border:1px solid var(--acc); font-size:.7rem;
          padding:1px 5px; margin-left:8px; border-radius:3px; white-space:nowrap; }}
  td.pos {{ color:var(--up); }}
  td.neg {{ color:#3dd68c; }}
  td.warn {{ color:var(--acc); }}
  td {{ padding:10px 12px; border-bottom:1px solid var(--line); }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  a {{ color:var(--txt); text-decoration:none; border-bottom:1px dotted var(--dim); }}
  a:hover {{ color:var(--acc); }}
  footer {{ color:var(--dim); font-size:.75rem; margin-top:40px; line-height:1.7; }}
</style></head><body><main>
<h1>每日股票清單</h1>
<div class="date">{date_label}</div>
<div id="drop-box" style="display:none"><b>⚠ 已從清單消失</b>──
以下股票先前曾入榜、今日已不符合資格，記得從 TradingView 清單移除：<br>
<span id="drop-list"></span></div>
<h2>樣板合格池 <small>五條件全過（{len(list_a)} 檔）</small>{btn_a}</h2>
<table id='tb-a'>{THEAD}<tbody>{a_rows}</tbody></table>
<details class="bl"><summary>已汰除股票（<span id="bl-count">0</span> 檔）──點開管理／復原</summary>
<div id="bl-list"></div></details>
<footer>條件：收盤 > 55MA > 150MA > 200MA・200MA 較一個月前上升・
高於52週低點130%・高於52週高點75%・RS ≥ {RS_THRESHOLD}・
排除產業：{"、".join(EXCLUDE_INDUSTRIES)}。<br>
「過」＝三盤過+紅K+量增（收盤突破前{SIG_LOOKBACK}盤高點・收紅・量>昨量），觸發股排在最前；
盤中預估版的訊號以收盤為準。進場時機自行看圖判斷。點股名開啟 TradingView。<br>
📋 複製代號後，到 TradingView 商品清單面板按 Ctrl+V 即可整批加入清單。<br>
點欄位標題可排序・55MA乖離 ≥ {BIAS_WARN}% 以橘字提示過熱。<br>
✕ 汰除的股票之後不再顯示；汰除與消失警示記錄存於瀏覽器，請固定用同一瀏覽器開啟。</footer>
</main>{page_js}</body></html>"""

# ========================= 5. LINE 推播 =========================

def push_line(text):
    if not LINE_TOKEN or not LINE_USER_ID:
        print("（未設定 LINE_TOKEN / LINE_USER_ID，略過推播）")
        return
    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {LINE_TOKEN}"},
            json={"to": LINE_USER_ID,
                  "messages": [{"type": "text", "text": text[:4900]}]},
            timeout=15,
        )
    except Exception as e:
        print(f"LINE 推播失敗（連線/金鑰格式問題）: {type(e).__name__}: {e}")
        sys.exit(1)
    print("LINE 推播:", resp.status_code, resp.text[:200])
    if resp.status_code != 200:
        print("LINE 推播被拒絕：請檢查 LINE_TOKEN / LINE_USER_ID 是否正確"
              "（401=Token 錯誤，400=User ID 錯誤或格式問題）")
        sys.exit(1)

# ========================= 主流程 =========================

def main():
    ap = argparse.ArgumentParser()
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--preview", action="store_true", help="盤中預估版")
    grp.add_argument("--final", action="store_true", help="收盤正式版（預設）")
    ap.add_argument("--force", action="store_true", help="資料非今日也照跑")
    ap.add_argument("--no-push", action="store_true", help="只產檔不推播")
    args = ap.parse_args()
    preview = args.preview

    now = dt.datetime.now()
    today = now.date()

    tickers, names, inds = fetch_universe()
    frames = download_with_retry(tickers)
    if not frames:
        print("沒有取得任何資料，請檢查網路或資料源。")
        sys.exit(1)

    # --- 資料日期驗證：以全市場最新資料日為基準，剔除過期個股 ---
    ref_date = max(df.index[-1].date() for df in frames.values())
    fresh = {t: df for t, df in frames.items() if df.index[-1].date() == ref_date}
    stale_cnt = len(frames) - len(fresh)
    coverage = len(fresh) / len(tickers)
    print(f"資料日期 {ref_date}｜當日有效 {len(fresh)} 檔｜過期剔除 {stale_cnt} 檔")

    if coverage < 0.5:
        msg = (f"資料源異常：全市場僅 {len(fresh)} 檔更新到 {ref_date}"
               f"（涵蓋率 {coverage:.1%}），疑似資料源維護/回滾中，未產出清單。")
        print(msg)
        if not preview and not args.no_push and today.weekday() < 5:
            push_line("⚠️ " + msg)
        sys.exit(0)

    if ref_date != today and not args.force:
        msg = (f"今日 {today} 無最新資料（資料源最新為 {ref_date}），"
               f"可能休市或資料源延遲，未產出清單。手動補跑請加 --force。")
        print(msg)
        if not preview and not args.no_push and today.weekday() < 5:
            push_line(f"ℹ️ {msg}")
        sys.exit(0)

    result = analyze(fresh, skip_last_in_avg=preview)
    if result.empty:
        print("沒有任何股票通過基本過濾。")
        sys.exit(1)
    result.insert(1, "name", result["ticker"].map(names))
    result.insert(2, "industry", result["ticker"].map(inds))
    result["excluded_industry"] = result["industry"].apply(
        lambda s: any(k in str(s) for k in EXCLUDE_INDUSTRIES))

    list_a = (result[result["setup_ok"] & ~result["excluded_industry"]]
              .sort_values(["signal", "rs"], ascending=[False, False]))

    tag = "preview" if preview else "final"
    out_html = os.path.join(BASE, f"daily_list_preview.html" if preview else "daily_list.html")
    out_csv = os.path.join(BASE, f"screener_result_{tag}.csv")

    if preview:
        date_label = f"{ref_date}・盤中 {now:%H:%M} 快照・名單以收盤為準"
    else:
        date_label = f"{ref_date}・收盤正式・還原權值價"

    result.to_csv(out_csv, index=False, encoding="utf-8-sig")
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(build_html(list_a, names, inds, date_label, final=not preview))
    print(f"樣板合格 {len(list_a)} 檔")
    print(f"已輸出 {out_html} 與 {out_csv}")

    # --- LINE 訊息 ---
    header = (f"⏳ {ref_date} 盤中快照（{now:%H:%M}）"
              if preview else f"✅ {ref_date} 收盤正式清單")
    lines = [header,
             f"樣板合格 {len(list_a)} 檔，請開清單看圖"]

    # 觸發「三盤過+紅K+量增」的股票直接列在訊息裡（手機點連結開 TradingView 圖）
    sig_rows = list_a[list_a["signal"]]
    if len(sig_rows):
        lines.append(f"🔸 三盤過+紅K+量增 觸發 {len(sig_rows)} 檔：")
        for r in sig_rows.itertuples():
            code = r.ticker.split(".")[0]
            lines.append(f"{code} {r.name}  {r.close} ({r.chg_pct:+.1f}%)")
            lines.append(tv_link(r.ticker))
    else:
        lines.append("三盤過+紅K+量增：今日無觸發")

    if preview:
        lines.append("※ 盤中快照，名單與訊號以收盤為準")
    if coverage < COVERAGE_WARN:
        lines.append(f"⚠ 資料涵蓋率僅 {coverage:.1%}（缺漏/過期偏多），排名可能失真")

    if args.no_push:
        print("（--no-push 測試模式，訊息內容如下）")
        print("\n".join(lines))
    else:
        push_line("\n".join(lines))

if __name__ == "__main__":
    main()
