# -*- coding: utf-8 -*-
"""
每日股票清單系統（排程版）
邏輯：Minervini 趨勢樣板位階濾網，產出合格池清單
合格池內另標記「過」＝三盤過+紅K+量增（進場仍由使用者自行看圖確認）

執行：
  python daily_screener.py --preview   盤中預估版（排程 12:55）
  python daily_screener.py --final     收盤正式版（排程 13:55）
  清單內容一律看網頁；LINE 只在「過」訊號觸發時發簡短提醒（排程失敗另由 workflow 通知）
  加 --force   資料非今日也照跑（手動補跑用）
  加 --no-push 只產檔不推播（測試用）
  加 --rebuild 忽略歷史快取，全量重抓（每週日排程自動重建；此參數供手動除錯）

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

# 金鑰本身不含空白；清掉任何位置的空白/換行（從終端機複製時常混入）
LINE_TOKEN = "".join(os.environ.get("LINE_TOKEN", "").split())      # Channel access token
LINE_USER_ID = "".join(os.environ.get("LINE_USER_ID", "").split())  # U 開頭的 user ID

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

GROUP_TOP_N = 3         # 強勢族群顯示前 N 名（主流族群通常 1-2 個，取 3 保留鑑別度）
GROUP_MIN_STOCKS = 5    # 族群至少 N 檔才列入（避免小族群被單一股票暴衝失真）

MIN_AVG_VOL_SHARES = 500_000   # 流動性過濾：20 日均量 > 500 張（1 張 = 1000 股）
MIN_BARS = 260                 # 上市未滿 260 個交易日（約一年）的股票排除
MIN_PRICE = 10.0               # 排除低價股（不想過濾就改成 0）

# 排除產業：產業別含任一關鍵字即不進清單（RS 排名仍計入全市場，分數意義不變）
EXCLUDE_INDUSTRIES = ("金融", "保險", "生技醫療")

COVERAGE_WARN = 0.98    # 有效資料低於母體 98% 時在 LINE 警告

PAGES_URL = "https://josh59408-ui.github.io/daily-stock-screener/"  # 清單網頁（GitHub Pages）

BASE = os.path.dirname(os.path.abspath(__file__))

# ========================= 1. 取得上市櫃股票清單 =========================

UNIVERSE_CACHE = os.path.join(BASE, "universe_cache.csv")

def fetch_universe():
    """從 TWSE ISIN 網頁抓上市(.TW)與上櫃(.TWO)普通股清單（失敗會 raise）"""
    urls = {
        ".TW": "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2",   # 上市
        ".TWO": "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4",  # 上櫃
    }
    tickers, names, inds = [], {}, {}
    for suffix, url in urls.items():
        text = None
        for attempt in range(3):
            try:
                r = requests.get(url, timeout=30)
                r.raise_for_status()
                r.encoding = "big5"
                text = r.text
                break
            except Exception as e:
                print(f"抓取母體清單失敗（{suffix} 第 {attempt + 1}/3 次）: {e}")
                time.sleep(10)
        if text is None:
            raise RuntimeError(f"TWSE ISIN 網頁（{suffix}）連續 3 次抓取失敗")
        tables = pd.read_html(io.StringIO(text))
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
    if len(tickers) < 1500:   # 正常約 1900+ 檔；明顯偏少代表網頁改版或回傳不完整
        raise RuntimeError(f"母體僅解析出 {len(tickers)} 檔，疑似 TWSE 網頁異常")
    print(f"母體：共 {len(tickers)} 檔上市櫃普通股")
    return tickers, names, inds

def get_universe():
    """抓母體清單；成功就更新快取，失敗退回用上次的快取檔"""
    try:
        tickers, names, inds = fetch_universe()
        pd.DataFrame({
            "ticker": tickers,
            "name": [names[t] for t in tickers],
            "industry": [inds[t] for t in tickers],
        }).to_csv(UNIVERSE_CACHE, index=False, encoding="utf-8-sig")
        return tickers, names, inds
    except Exception as e:
        print(f"母體清單取得失敗：{e}")
        if os.path.exists(UNIVERSE_CACHE):
            df = pd.read_csv(UNIVERSE_CACHE, dtype=str).fillna("")
            tickers = df["ticker"].tolist()
            print(f"改用快取母體清單（{len(tickers)} 檔，母體最多一天舊，影響極小）")
            return (tickers,
                    dict(zip(df["ticker"], df["name"])),
                    dict(zip(df["ticker"], df["industry"])))
        raise   # 沒有快取可退 → 讓執行失敗，由排程端發失敗通知

# ========================= 2. 下載歷史價量 =========================

HISTORY_CACHE = os.path.join(BASE, "history_cache.pkl")
HISTORY_META = os.path.join(BASE, "history_cache_meta.json")

MAX_BARS = 300            # 快取每檔保留的 K 棒數（指標最深需 260 根，留餘裕）
FULL_LOOKBACK_DAYS = 550  # 全量下載回看的日曆天數（約 370 個交易日 > MAX_BARS）
REBUILD_EVERY_DAYS = 7    # 快取滿 N 天強制全量重建（防還原價/資料修正累積誤差）
ADJ_TOLERANCE = 0.002     # 重疊日收盤價相對差超過 0.2% → 視為除權息還原，整檔重抓

def full_start_date():
    return (dt.date.today() - dt.timedelta(days=FULL_LOOKBACK_DAYS)).isoformat()

def download_history(tickers, chunk=100, **yf_range):
    """分批向 yfinance 下載日 K（還原權值價）；範圍由 yf_range 指定（start= 或 period=）"""
    frames = {}
    for i in range(0, len(tickers), chunk):
        batch = tickers[i:i + chunk]
        print(f"下載中 {i + 1}-{min(i + chunk, len(tickers))} / {len(tickers)} ...")
        data = yf.download(
            batch, interval="1d",
            auto_adjust=True, group_by="ticker",
            progress=False, threads=True, **yf_range,
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

def download_with_retry(tickers, **yf_range):
    frames = download_history(tickers, **yf_range)
    missing = [t for t in tickers if t not in frames]
    if missing:
        print(f"第一輪缺 {len(missing)} 檔，重試一次 ...")
        frames.update(download_history(missing, **yf_range))
        missing = [t for t in tickers if t not in frames]
    print(f"成功取得 {len(frames)} 檔資料（缺 {len(missing)} 檔）")
    return frames

def load_cache():
    """讀歷史快取；回傳 (frames, 全量建立日)。不存在、損毀或到期 → None（觸發全量）"""
    if not (os.path.exists(HISTORY_CACHE) and os.path.exists(HISTORY_META)):
        return None
    try:
        with open(HISTORY_META, encoding="utf-8") as f:
            built = dt.date.fromisoformat(json.load(f)["built"])
        age = (dt.date.today() - built).days
        if age >= REBUILD_EVERY_DAYS:
            print(f"歷史快取距上次全量重建已 {age} 天，本次全量重抓")
            return None
        frames = pd.read_pickle(HISTORY_CACHE)
        if not isinstance(frames, dict) or not frames:
            return None
        print(f"讀入歷史快取 {len(frames)} 檔（{built} 全量建立）")
        return frames, built
    except Exception as e:
        print(f"歷史快取讀取失敗（{type(e).__name__}: {e}），改全量重抓")
        return None

def save_cache(frames, built=None, drop_date=None):
    """drop_date：存檔前剔除該日期的最後一根 K 棒。
    預估版（盤中）執行時傳入今日，確保快取只含已收盤資料——否則正式版增量
    若剛好缺某檔，會沿用快取裡 12:55 的盤中暫存值且日期驗證攔不住。"""
    trimmed = {}
    for t, df in frames.items():
        if drop_date is not None and len(df) and df.index[-1].date() == drop_date:
            df = df.iloc[:-1]
        if len(df):
            trimmed[t] = df.iloc[-MAX_BARS:]
    pd.to_pickle(trimmed, HISTORY_CACHE)
    with open(HISTORY_META, "w", encoding="utf-8") as f:
        json.dump({"built": (built or dt.date.today()).isoformat()}, f)

def merge_history(old, new):
    """把增量資料接上快取。回傳 (合併結果, 需整檔重抓)"""
    overlap = old.index.intersection(new.index)
    if len(overlap) == 0:
        return None, True   # 快取太舊接不上 → 整檔重抓
    # 還原價偵測：比對「已收盤」的重疊日（排除快取最後一根，可能是先前盤中暫存值）。
    # 除權息時 Yahoo 會回頭調整整段歷史，只接新 K 棒會讓均線/RS 全錯，必須整檔重抓。
    settled = overlap[overlap < old.index[-1]]
    if len(settled):
        a = old.loc[settled, "Close"].to_numpy(dtype=float)
        b = new.loc[settled, "Close"].to_numpy(dtype=float)
        if np.nanmax(np.abs(a - b) / np.abs(a)) > ADJ_TOLERANCE:
            return None, True
    # 重疊區間以新資料為準（盤中暫存 K 棒在收盤後被正式值覆蓋）
    merged = pd.concat([old[old.index < new.index[0]], new])
    return merged.iloc[-MAX_BARS:], False

def get_history(tickers, rebuild=False, unsettled_date=None):
    """取得全市場歷史價量：有快取就只補近一個月，否則全量下載。
    unsettled_date：該日 K 棒尚未收盤（預估版），不寫入快取。"""
    cached = None if rebuild else load_cache()
    if cached is None:
        print(f"全量下載 {len(tickers)} 檔（回看 {FULL_LOOKBACK_DAYS} 天）...")
        frames = download_with_retry(tickers, start=full_start_date())
        frames = {t: df.iloc[-MAX_BARS:] for t, df in frames.items()}
        if frames:
            save_cache(frames, drop_date=unsettled_date)
        return frames

    cached_frames, built = cached
    known = [t for t in tickers if t in cached_frames]
    to_full = [t for t in tickers if t not in cached_frames]  # 新上市或上次缺漏
    print(f"增量更新 {len(known)} 檔（近一個月）；{len(to_full)} 檔不在快取，需全量")
    inc = download_with_retry(known, period="1mo")

    frames = {}
    adj_refetch = 0
    for t in known:
        if t not in inc:
            frames[t] = cached_frames[t]  # 增量抓不到 → 沿用舊資料，主流程會依日期剔除
            continue
        merged, needs_full = merge_history(cached_frames[t], inc[t])
        if needs_full:
            to_full.append(t)
            adj_refetch += 1
        else:
            frames[t] = merged
    if adj_refetch:
        print(f"{adj_refetch} 檔偵測到除權息還原或斷檔，整檔重抓")
    if to_full:
        frames.update(download_with_retry(to_full, start=full_start_date()))

    frames = {t: df.iloc[-MAX_BARS:] for t, df in frames.items()}
    # 保留原全量建立日，讓 7 天重建週期正常運作
    save_cache(frames, built=built, drop_date=unsettled_date)
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

def build_watchlist(list_a):
    """TradingView 匯入檔：逗號分隔、###開頭為分組標題（匯入後清單內分兩區）"""
    sig = [tv_symbol(r.ticker) for r in list_a.itertuples() if r.signal]
    rest = [tv_symbol(r.ticker) for r in list_a.itertuples() if not r.signal]
    parts = []
    if sig:
        parts += ["###過訊號"] + sig
    if rest:
        parts += ["###樣板合格池"] + rest
    return ",".join(parts)

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
var PAGE_DATE = '__PAGE_DATE__';   // 本頁資料日期（ISO，可直接字串比大小）
var SYNC_FILE = 'daily_screener_sync.json';   // Gist 內的檔名（各裝置以此互認）

function lsGet(k, d) { try { return JSON.parse(localStorage.getItem(k)) || d; } catch (e) { return d; } }
function lsSet(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }

var bl = new Set(lsGet('ds_blacklist', []));   // 永久汰除名單
var watch = lsGet('ds_watch', {});             // 曾進清單、預期還在 TV 清單上的股票
var namesMap = lsGet('ds_names', {});
var syncToken = lsGet('ds_sync_token', '');    // GitHub token（只存本裝置，不在網頁原始碼）
var gistId = lsGet('ds_gist_id', '');
var pushTimer = null;

// 正式版才更新「曾出現」名單（預估版盤中浮動，不記錄）
function applyToday() {
  TODAY.forEach(function (x) { namesMap[x.c] = x.n; });
  if (IS_FINAL) { TODAY.forEach(function (x) { if (!bl.has(x.c)) watch[x.c] = x.n; }); }
}

// ---------- 今日新增（今日在榜、但先前不在 watch 名單）----------
var newSet = new Set();
var newOnly = false;    // 「只看新增」篩選開關

function calcNewCodes() {
  return TODAY.filter(function (x) { return !(x.c in watch) && !bl.has(x.c); })
              .map(function (x) { return x.c; });
}
// 必須在 applyToday() 把今日名單併入 watch「之前」呼叫。
// 正式版把結果連同日期凍結到 ds_new，當天重整頁面徽章才不會消失；
// st 為雲端同步狀態：若雲端已有同日紀錄則直接採用（另一台裝置先開過頁面時較準）。
function computeNew(st) {
  if (!IS_FINAL) { newSet = new Set(calcNewCodes()); return; }
  var stored = lsGet('ds_new', null);
  var rec;
  if (st && st.newRec && st.newRec.d === PAGE_DATE) rec = st.newRec;
  else if (st) rec = { d: PAGE_DATE, codes: calcNewCodes() };   // 以雲端 watch 重算
  else if (stored && stored.d === PAGE_DATE) rec = stored;
  else rec = { d: PAGE_DATE, codes: calcNewCodes() };
  newSet = new Set(rec.codes);
  // 開到舊日期的頁面時只顯示、不回寫，避免蓋掉今天的紀錄
  if (!stored || stored.d <= PAGE_DATE) lsSet('ds_new', rec);
}

function saveLocal() {
  lsSet('ds_blacklist', Array.from(bl));
  lsSet('ds_watch', watch);
  lsSet('ds_names', namesMap);
}
function persist() { saveLocal(); schedulePush(); }

// ---------- 跨裝置同步（GitHub Gist）----------
function setSync(msg) {
  var el = document.getElementById('sync-status');
  if (el) el.textContent = msg;
}
function updateSyncState() {
  var el = document.getElementById('sync-state');
  if (el) el.textContent = syncToken ? '已啟用' : '未啟用';
}
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
    var g = list.find(function (x) { return x.files && x.files[SYNC_FILE]; });
    if (g) { gistId = g.id; lsSet('ds_gist_id', gistId); }
    return gistId || null;
  });
}
function pullState() {
  setSync('同步中…');
  return findGist().then(function (id) {
    if (!id) return null;   // 第一台裝置：雲端還沒有資料
    return gh('/gists/' + id).then(function (g) {
      var f = g.files[SYNC_FILE];
      if (!f) return null;
      var st = JSON.parse(f.content);
      bl = new Set(st.bl || []);
      watch = st.watch || {};
      var rn = st.names || {};
      Object.keys(rn).forEach(function (k) { if (!namesMap[k]) namesMap[k] = rn[k]; });
      return st;
    });
  }).then(function (st) {
    setSync('已同步 ' + new Date().toLocaleTimeString());
    return st;
  });
}
function pushNow() {
  if (!syncToken) return;
  var files = {};
  files[SYNC_FILE] = { content: JSON.stringify(
    { bl: Array.from(bl), watch: watch, names: namesMap,
      newRec: lsGet('ds_new', null), ts: Date.now() }) };
  var p = gistId
    ? gh('/gists/' + gistId, { method: 'PATCH', body: JSON.stringify({ files: files }) })
    : gh('/gists', { method: 'POST', body: JSON.stringify(
        { description: '每日股票清單 跨裝置同步', public: false, files: files }) })
        .then(function (g) { gistId = g.id; lsSet('ds_gist_id', gistId); });
  p.then(function () { setSync('已同步 ' + new Date().toLocaleTimeString()); })
   .catch(function (e) { setSync('同步失敗：' + e.message + '（名單仍存於本機）'); });
}
function schedulePush() {
  if (!syncToken) return;
  clearTimeout(pushTimer);
  pushTimer = setTimeout(pushNow, 1200);   // 連續操作合併成一次上傳
}
function render() {
  document.querySelectorAll('tr[data-code]').forEach(function (tr) {
    var c = tr.dataset.code;
    tr.classList.toggle('is-new', newSet.has(c));
    tr.style.display = (bl.has(c) || (newOnly && !newSet.has(c))) ? 'none' : '';
  });
  var nb = document.getElementById('new-only');
  if (nb) {
    nb.style.display = newSet.size ? '' : 'none';
    nb.textContent = (newOnly ? '顯示全部' : '只看新增') + '（' + newSet.size + '）';
    nb.classList.toggle('on', newOnly);
  }
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
  else if (b.id === 'new-only') { newOnly = !newOnly; render(); }
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

// 同步設定 UI
document.getElementById('sync-on').addEventListener('click', function () {
  var t = document.getElementById('sync-token').value.replace(/\\s+/g, '');
  if (!t) { setSync('請先貼上 token'); return; }
  syncToken = t; lsSet('ds_sync_token', t);
  document.getElementById('sync-token').value = '';
  updateSyncState();
  pullState().then(function (st) { computeNew(st); applyToday(); saveLocal(); render(); schedulePush(); })
    .catch(function (e) { setSync('啟用失敗：' + e.message + '，請確認 token 正確且有 gist 權限'); });
});
document.getElementById('sync-off').addEventListener('click', function () {
  syncToken = ''; gistId = '';
  lsSet('ds_sync_token', ''); lsSet('ds_gist_id', '');
  updateSyncState(); setSync('已停用，名單僅存於本機');
});

// 啟動：先用本機資料立即顯示，有 token 再拉雲端覆蓋後重繪並回寫
// computeNew 一定要在 applyToday 之前（今日名單併入 watch 後就分不出誰是新的）
computeNew(null); applyToday(); saveLocal(); render(); updateSyncState();
if (syncToken) {
  pullState().then(function (st) { computeNew(st); applyToday(); saveLocal(); render(); schedulePush(); })
    .catch(function (e) { setSync('同步失敗：' + e.message + '（名單仍存於本機）'); });
}
</script>"""

THEAD = ("<thead><tr><th data-k='1'>股票</th><th data-k='1'>收盤</th>"
         "<th data-k='1'>漲跌%</th><th data-k='1'>RS</th>"
         "<th data-k='1'>55MA乖離</th><th></th></tr></thead>")

def build_html(list_a, names, inds, date_label, final=True, top_groups=None):
    def row_html(r):
        name = names.get(r.ticker, "")
        code = r.ticker.split(".")[0]
        ind = short_industry(inds.get(r.ticker, ""))
        tag = f"<span class='tag'>{ind}</span>" if ind else ""
        sig = "<span class='sig'>過</span>" if r.signal else ""
        newtag = "<span class='newtag'>新</span>"   # 是否顯示由前端 JS 判斷
        chg_cls = "pos" if r.chg_pct > 0 else ("neg" if r.chg_pct < 0 else "")
        bias_cls = " warn" if r.bias55 >= BIAS_WARN else ""
        return (
            f"<tr data-code='{code}' data-sym='{tv_symbol(r.ticker)}'>"
            f"<td><a href='{tv_link(r.ticker)}' target='_blank'>{code} {name}</a>"
            f"<a class='ylink' href='https://tw.stock.yahoo.com/quote/{r.ticker}'"
            f" target='_blank' title='Yahoo股市（即時報價）'>Y</a>{sig}{newtag}{tag}</td>"
            f"<td class='num'>{r.close}</td>"
            f"<td class='num {chg_cls}'>{r.chg_pct:+.1f}%</td>"
            f"<td class='num'>{r.rs}</td>"
            f"<td class='num{bias_cls}'>{r.bias55}%</td>"
            f"<td class='act'><button class='del' data-c='{code}' "
            f"title='汰除：之後清單永久隱藏此股票'>✕</button></td></tr>"
        )

    a_rows = "".join(row_html(r) for r in list_a.itertuples())
    grp_html = ""
    if top_groups:
        items = "・".join(f"<b>{n}</b> {c:+.1f}%（{k}檔）" for n, c, k in top_groups)
        grp_html = f"<span class='grp'>強勢族群：{items}</span>"
    wl_name = "watchlist.txt" if final else "watchlist_preview.txt"
    btn_a = ((f"<button class='copy' data-target='tb-a'>📋 複製代號</button>"
              f"<a class='dl' href='{wl_name}' download>⬇ TV 匯入檔</a>"
              f"<button id='new-only' style='display:none'></button>")
             if len(list_a) else "")
    today_json = json.dumps(
        [{"c": r.ticker.split(".")[0], "n": names.get(r.ticker, "")}
         for r in list_a.itertuples()],
        ensure_ascii=False)
    page_js = (PAGE_JS
               .replace("__TODAY__", today_json)
               .replace("__IS_FINAL__", "true" if final else "false")
               .replace("__PAGE_DATE__", date_label.split("・")[0]))
    return f"""<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>每日股票清單 {date_label}</title>
<style>
  :root {{ --bg:#101418; --panel:#1a2027; --line:#2a323c;
           --txt:#d7dde3; --dim:#8a94a0; --up:#e5484d; --acc:#f5b93e; --new:#4cc2ff; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--txt);
          font-family:"Noto Sans TC",system-ui,sans-serif; padding:24px 16px 64px; }}
  main {{ max-width:760px; margin:0 auto; }}
  h1 {{ font-size:1.15rem; letter-spacing:.12em; margin:0; }}
  .date {{ color:var(--dim); font-size:.85rem; margin:4px 0 28px;
           font-variant-numeric:tabular-nums; }}
  h2 {{ font-size:.9rem; color:var(--acc); letter-spacing:.2em;
        border-left:3px solid var(--acc); padding-left:10px; margin:36px 0 12px;
        display:flex; align-items:center; flex-wrap:wrap; gap:4px 0; }}
  h2 small {{ color:var(--dim); letter-spacing:0; font-weight:400; margin-left:8px; }}
  .grp {{ margin-left:auto; color:var(--dim); font-size:.75rem; font-weight:400;
        letter-spacing:.02em; white-space:nowrap; }}
  .grp b {{ color:var(--txt); font-weight:500; }}
  button.copy, button.done, button.undo {{ background:none; border:1px solid var(--line);
        color:var(--dim); font:inherit; font-size:.72rem; padding:3px 10px;
        margin-left:10px; border-radius:4px; cursor:pointer; letter-spacing:0;
        vertical-align:middle; }}
  button.copy:hover {{ color:var(--acc); border-color:var(--acc); }}
  a.dl {{ border:1px solid var(--line); color:var(--dim); font-size:.72rem;
        padding:3px 10px; margin-left:8px; border-radius:4px; letter-spacing:0;
        vertical-align:middle; border-bottom:1px solid var(--line); }}
  a.dl:hover {{ color:var(--acc); border-color:var(--acc); }}
  a.ylink {{ color:var(--dim); border:1px solid var(--line); border-radius:3px;
        font-size:.68rem; padding:1px 5px; margin-left:8px; vertical-align:middle; }}
  a.ylink:hover {{ color:var(--acc); border-color:var(--acc); }}
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
  details.sync {{ margin-top:14px; }}
  details.sync p {{ line-height:1.9; margin:10px 0; }}
  details.sync input {{ background:var(--bg); border:1px solid var(--line); color:var(--txt);
        padding:6px 10px; border-radius:4px; width:min(320px,60vw); font:inherit; font-size:.8rem; }}
  details.sync button {{ background:none; border:1px solid var(--line); color:var(--dim);
        font:inherit; font-size:.75rem; padding:4px 12px; margin-left:8px;
        border-radius:4px; cursor:pointer; }}
  details.sync button:hover {{ color:var(--acc); border-color:var(--acc); }}
  #sync-status {{ display:inline-block; margin-left:10px; font-size:.78rem; }}
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
  .newtag {{ display:none; color:var(--new); border:1px solid var(--new); font-size:.7rem;
          padding:1px 5px; margin-left:8px; border-radius:3px; white-space:nowrap; }}
  tr.is-new .newtag {{ display:inline-block; }}
  #new-only {{ background:none; border:1px solid var(--line); color:var(--dim);
          font:inherit; font-size:.72rem; padding:3px 10px; margin-left:8px;
          border-radius:4px; cursor:pointer; letter-spacing:0; vertical-align:middle; }}
  #new-only:hover, #new-only.on {{ color:var(--new); border-color:var(--new); }}
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
<h2>樣板合格池 <small>五條件全過（{len(list_a)} 檔）</small>{btn_a}{grp_html}</h2>
<table id='tb-a'>{THEAD}<tbody>{a_rows}</tbody></table>
<details class="bl"><summary>已汰除股票（<span id="bl-count">0</span> 檔）──點開管理／復原</summary>
<div id="bl-list"></div></details>
<details class="bl sync"><summary>☁ 跨裝置同步（<span id="sync-state">未啟用</span>）</summary>
<p>啟用後，汰除名單與消失警示會存到你 GitHub 帳號的私密 Gist，
電腦與手機看到同一份。每台裝置各貼一次 token 即可（token 只存在該裝置的瀏覽器裡）。</p>
<p>建立 token：GitHub → Settings → Developer settings → Personal access tokens →
Tokens (classic) → Generate new token，權限只勾 <b>gist</b>。</p>
<input id="sync-token" type="password" placeholder="貼上 GitHub token"
       autocomplete="off"><button id="sync-on">啟用</button><button
       id="sync-off">停用</button><span id="sync-status"></span></details>
<footer>條件：收盤 > 55MA > 150MA > 200MA・200MA 較一個月前上升・
高於52週低點130%・高於52週高點75%・RS ≥ {RS_THRESHOLD}・
排除產業：{"、".join(EXCLUDE_INDUSTRIES)}。<br>
「過」＝三盤過+紅K+量增（收盤突破前{SIG_LOOKBACK}盤高點・收紅・量>昨量），觸發股排在最前；
盤中預估版的訊號以收盤為準。進場時機自行看圖判斷。點股名開啟 TradingView。<br>
<b>新</b>＝今日新進榜（先前不曾入榜或已被移除後重新入榜），優先看圖；
「只看新增」可暫時只顯示這些股票（複製代號也只會複製顯示中的）。<br>
📋 複製代號後，到 TradingView 商品清單面板按 Ctrl+V 即可整批加入清單。<br>
⬇ TV 匯入檔：下載後在 TradingView（電腦版）商品清單選單點「匯入清單…」選取此檔，
會建立含「過訊號／樣板合格池」分組的新清單，並自動同步到手機 App；
重複匯入會多一份清單，記得刪掉舊的。
每檔股名旁的 <b>Y</b> 連到 Yahoo 股市（手機看即時報價方便）。<br>
點欄位標題可排序・55MA乖離 ≥ {BIAS_WARN}% 以橘字提示過熱。<br>
強勢族群＝全市場（通過流動性過濾、排除產業除外）依產業別平均漲跌%取前
{GROUP_TOP_N} 名，族群至少 {GROUP_MIN_STOCKS} 檔才列入，避免單一股票暴衝失真。<br>
✕ 汰除的股票之後不再顯示；汰除與消失警示預設存於瀏覽器本機，
啟用上方「跨裝置同步」後電腦與手機共用同一份名單。</footer>
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
    ap.add_argument("--rebuild", action="store_true", help="忽略歷史快取，全量重抓")
    args = ap.parse_args()
    preview = args.preview

    now = dt.datetime.now()
    today = now.date()

    tickers, names, inds = get_universe()
    frames = get_history(tickers, rebuild=args.rebuild,
                         unsettled_date=today if preview else None)
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

    # 今日強勢族群：全市場（通過流動性過濾、排除產業除外）依產業平均漲幅取前 3
    grp_src = result[~result["excluded_industry"] & (result["industry"].astype(str) != "")]
    grp = grp_src.groupby("industry")["chg_pct"].agg(chg="mean", n="size")
    grp = grp[grp["n"] >= GROUP_MIN_STOCKS].sort_values("chg", ascending=False).head(GROUP_TOP_N)
    top_groups = [(short_industry(str(i)), row["chg"], int(row["n"]))
                  for i, row in grp.iterrows()]

    tag = "preview" if preview else "final"
    out_html = os.path.join(BASE, f"daily_list_preview.html" if preview else "daily_list.html")
    out_csv = os.path.join(BASE, f"screener_result_{tag}.csv")

    if preview:
        date_label = f"{ref_date}・盤中 {now:%H:%M} 快照・名單以收盤為準"
    else:
        date_label = f"{ref_date}・收盤正式・還原權值價"

    result.to_csv(out_csv, index=False, encoding="utf-8-sig")
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(build_html(list_a, names, inds, date_label,
                           final=not preview, top_groups=top_groups))
    out_wl = os.path.join(BASE, "watchlist.txt" if not preview else "watchlist_preview.txt")
    with open(out_wl, "w", encoding="utf-8") as f:
        f.write(build_watchlist(list_a))
    print(f"樣板合格 {len(list_a)} 檔")
    print(f"已輸出 {out_html}、{out_csv} 與 {out_wl}")

    # --- LINE 訊息：只在「過」訊號觸發時發簡短提醒，清單內容一律看網頁 ---
    sig_rows = list_a[list_a["signal"]]
    if len(sig_rows):
        page_url = PAGES_URL + ("daily_list_preview.html" if preview else "daily_list.html")
        tag_txt = f"盤中 {now:%H:%M}，以收盤為準" if preview else "收盤正式"
        lines = [f"🔸 {ref_date} 三盤過+紅K+量增 觸發 {len(sig_rows)} 檔（{tag_txt}）"]
        lines += [f"{r.ticker.split('.')[0]} {r.name}" for r in sig_rows.itertuples()]
        lines.append(page_url)
        if coverage < COVERAGE_WARN:
            lines.append(f"⚠ 資料涵蓋率僅 {coverage:.1%}（缺漏/過期偏多），排名可能失真")
        if args.no_push:
            print("（--no-push 測試模式，訊息內容如下）")
            print("\n".join(lines))
        else:
            push_line("\n".join(lines))
    else:
        print("三盤過+紅K+量增：今日無觸發，不發 LINE（清單請看網頁）")

if __name__ == "__main__":
    main()
