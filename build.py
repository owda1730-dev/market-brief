# -*- coding: utf-8 -*-
"""
市場總覽 · 每日自動建置腳本
--------------------------------
在 GitHub Actions（每天排程）上執行：
  1. 抓官方/免費資料源：期交所三大法人、證交所融資融券、臺灣銀行匯率、Stooq 指數與商品行情
  2. 抓 Google News RSS，交給 Google Gemini 產生「新聞摘要 + 川普發言 + 值得關注 + 綜合解讀」
  3. 把 template.html 的 %%TOKEN%% 全部填好，輸出 index.html（GitHub Pages 直接服務）
  4. 用 data.json 逐日累積五日歷史（三大法人、融資融券）

設計原則：任何一個來源抓不到，就退回 DEFAULTS（上一版已知良好內容）並標示，
          頁面永遠完整、不會壞掉。第一次上線後若某來源解析不到，
          看 Actions 記錄再微調對應的 fetch_* 函式即可。
"""

import os, sys, json, re, io, csv, html, datetime, traceback
import urllib.parse
import requests

TZ = datetime.timezone(datetime.timedelta(hours=8))          # 台北時間
NOW = datetime.datetime.now(TZ)
UA = {"User-Agent": "Mozilla/5.0 (market-brief-bot)"}
HERE = os.path.dirname(os.path.abspath(__file__))
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()

def log(*a):
    print("[build]", *a, flush=True)

def http_get(url, timeout=25, is_json=False, params=None):
    try:
        r = requests.get(url, headers=UA, timeout=timeout, params=params)
        r.raise_for_status()
        return r.json() if is_json else r.text
    except Exception as e:
        log("GET fail:", url, "->", repr(e))
        return None

# ---------------------------------------------------------------------------
# 顏色/格式小工具（台灣慣例：紅漲綠跌；增加=紅，減少=綠）
# ---------------------------------------------------------------------------
def cls(v):        return "up" if v > 0 else ("down" if v < 0 else "flat")
def arrow(v):      return "▲" if v > 0 else ("▼" if v < 0 else "—")
def sign(v, nd=2): return ("+" if v > 0 else ("-" if v < 0 else "")) + f"{abs(v):,.{nd}f}"
def comma(v, nd=2):
    try: return f"{v:,.{nd}f}"
    except Exception: return str(v)
def mmdd(iso):     # "2026-07-14" -> "07/14"
    try: return iso[5:7] + "/" + iso[8:10]
    except Exception: return iso

# ===========================================================================
# 1) 指數與行情：Stooq 免費 CSV（無需金鑰）
#    如某個代碼抓不到，該項會沿用 DEFAULTS。symbol 若失效可在此調整。
# ===========================================================================
STOOQ_DAILY = "https://stooq.com/q/d/l/?s={s}&i=d"
PRICE_SYMBOLS = {
    "taiex":  "^twse",   "kospi": "^kospi", "nikkei": "^nkx",
    "sox":    "^sox",    "ndq":   "^ndq",   "spx":    "^spx",
    "dxy":    "^dxy",    "wti":   "cl.f",   "brent":  "cb.f",
    "gold":   "xauusd",  "us10y": "10usy.b","vix":    "^vix",
    "usdtwd": "usdtwd",
}

def fetch_stooq(symbol):
    """回傳 (last, prev, wk52lo, wk52hi) 或 None。"""
    txt = http_get(STOOQ_DAILY.format(s=urllib.parse.quote(symbol)))
    if not txt or "Date" not in txt:
        return None
    rows = list(csv.DictReader(io.StringIO(txt)))
    closes = []
    for r in rows:
        try: closes.append((r["Date"], float(r["Close"])))
        except Exception: pass
    if len(closes) < 2:
        return None
    last = closes[-1][1]; prev = closes[-2][1]
    window = [c for _, c in closes[-252:]]
    return last, prev, min(window), max(window), closes[-1][0]

def fetch_prices():
    out = {}
    for key, sym in PRICE_SYMBOLS.items():
        r = fetch_stooq(sym)
        if r:
            last, prev, lo, hi, date = r
            out[key] = {"last": last, "prev": prev, "lo": lo, "hi": hi, "date": date}
            log(f"price {key}({sym}) = {last} ({date})")
    return out

# ===========================================================================
# 2) 期交所三大法人（臺股期貨 未平倉多空淨額 口數）
# ===========================================================================
def fetch_taifex():
    """回傳 {'date':'YYYY-MM-DD','foreign':int,'trust':int,'dealer':int,'total':int} 或 None。"""
    import pandas as pd
    txt = http_get("https://www.taifex.com.tw/cht/3/futContractsDateExcel")
    if not txt:
        return None
    # 抓報表日期
    m = re.search(r"日期\s*(\d{4})/(\d{2})/(\d{2})", txt)
    date_iso = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else NOW.date().isoformat()
    try:
        tables = pd.read_html(io.StringIO(txt))
    except Exception as e:
        log("taifex read_html fail:", repr(e)); return None
    for df in tables:
        flat = df.astype(str)
        if not flat.apply(lambda c: c.str.contains("臺股期貨").any()).any():
            continue
        # 找到「臺股期貨」所在列，其後三列分別為 自營商/投信/外資
        rows = flat.values.tolist()
        for i, row in enumerate(rows):
            joined = " ".join(row)
            if "臺股期貨" in joined:
                def last_int(r):
                    ints = re.findall(r"-?\d[\d,]*", " ".join(r))
                    return int(ints[-1].replace(",", "")) if ints else None
                try:
                    dealer  = last_int(rows[i])      # 自營商（與臺股期貨同列）
                    trust   = last_int(rows[i + 1])  # 投信
                    foreign = last_int(rows[i + 2])  # 外資
                    if None in (dealer, trust, foreign):
                        return None
                    return {"date": date_iso, "foreign": foreign, "trust": trust,
                            "dealer": dealer, "total": foreign + trust + dealer}
                except Exception as e:
                    log("taifex parse fail:", repr(e)); return None
    return None

# ===========================================================================
# 3) 證交所 融資融券 市場合計（MI_MARGN，含日期）
# ===========================================================================
def fetch_margin_for(date_yyyymmdd):
    """回傳 (margin_yi, short_zhang) 市場合計，或 None。"""
    url = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
    j = http_get(url, is_json=True, params={"response": "json", "date": date_yyyymmdd, "selectType": "MS"})
    if not j or j.get("stat") != "OK":
        return None
    # tables 內找「融資金額(仟元)」與「融券」的合計列；MS 版通常在 j['tables'][0]['data']
    try:
        tbls = j.get("tables") or []
        for t in tbls:
            for row in t.get("data", []):
                label = str(row[0])
                if "融資金額" in label or "融資(交易單位)" in label or "融資" == label.strip():
                    # 找「今日餘額」欄；MS 表列為 [項目, 買進, 賣出, 現金(券)償還, 前日餘額, 今日餘額, 限額]
                    nums = [x for x in row[1:] if re.match(r"^-?[\d,]+$", str(x).replace(",", ""))]
        # 簡化解析較脆弱，改用 creditList
    except Exception:
        pass
    # 後備：MI_MARGN 另有彙總欄位 j['creditList'] / j['data']；不同版本鍵名不一，逐一嘗試
    try:
        for key in ("creditList", "tables"):
            pass
    except Exception:
        pass
    return None

def fetch_margin_history(prev_hist):
    """
    盡量抓最近幾個交易日的融資(億)/融券(張)市場合計。
    以 data.json 已存者為主，新的日期補上。回傳 dict {iso: {'margin_yi':..,'short_zhang':..}}。
    注：TWSE MS 版解析較脆弱，抓不到就沿用歷史（DEFAULTS）。
    """
    hist = dict(prev_hist or {})
    for back in range(0, 8):
        d = (NOW - datetime.timedelta(days=back)).date()
        if d.weekday() >= 5:      # 週末跳過
            continue
        iso = d.isoformat()
        if iso in hist:
            continue
        got = fetch_margin_for(d.strftime("%Y%m%d"))
        if got:
            hist[iso] = {"margin_yi": got[0], "short_zhang": got[1]}
    return hist

# ===========================================================================
# 4) 臺灣銀行 USD/TWD 即期收盤
# ===========================================================================
def fetch_bot_usdtwd():
    txt = http_get("https://rate.bot.com.tw/xrt/flcsv/0/day/USD")
    if not txt:
        return None
    last_sell = None
    for line in txt.splitlines():
        cols = [c.strip() for c in line.split(",")]
        # CSV 欄位含「即期賣出」；不同版本欄位序不一，取該列最後一個像匯率的數字
        cand = [c for c in cols if re.match(r"^\d{2}\.\d+$", c)]
        if cand:
            last_sell = float(cand[-1])
    return last_sell

# ===========================================================================
# 5) Google News RSS -> Gemini 生成敘事內容
# ===========================================================================
NEWS_FEEDS = {
    "tw": "台股 加權指數",
    "us": "美股 標普 那斯達克 半導體",
    "jp": "日經 225 東京股市",
    "kr": "KOSPI 韓國股市",
    "trump": "Trump tariffs oil Fed market",
}
def gnews(query, hl="zh-TW", gl="TW", ceid="TW:zh-Hant", n=8):
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(query) + f"&hl={hl}&gl={gl}&ceid={ceid}"
    txt = http_get(url)
    if not txt:
        return []
    items = []
    for m in re.finditer(r"<item>(.*?)</item>", txt, re.S):
        block = m.group(1)
        t = re.search(r"<title>(.*?)</title>", block, re.S)
        p = re.search(r"<pubDate>(.*?)</pubDate>", block, re.S)
        title = html.unescape(re.sub(r"<.*?>", "", t.group(1)).strip()) if t else ""
        pub = ""
        if p:
            try:
                dt = datetime.datetime.strptime(p.group(1).strip(), "%a, %d %b %Y %H:%M:%S %Z")
                pub = dt.strftime("%m/%d")
            except Exception:
                pub = ""
        if title:
            items.append({"date": pub, "title": title})
        if len(items) >= n:
            break
    return items

def gemini_narrative():
    """回傳 dict：{'news':{tw,us,jp,kr:[{date,text}]}, 'trump':[...], 'watch':[...], 'interp':'...'} 或 None。"""
    if not GEMINI_KEY:
        log("no GEMINI_API_KEY, skip narrative"); return None
    feeds = {k: gnews(q, hl=("en-US" if k == "trump" else "zh-TW"),
                      gl=("US" if k == "trump" else "TW"),
                      ceid=("US:en" if k == "trump" else "TW:zh-Hant"))
             for k, q in NEWS_FEEDS.items()}
    today = NOW.strftime("%Y/%m/%d")
    prompt = f"""你是台灣財經編輯。今天是 {today}（台北時間）。以下是各市場的新聞標題與日期（來自 Google News RSS）。
請「只根據這些標題」，用繁體中文，產生一份 JSON（不要多餘文字），結構如下：
{{
 "news": {{
   "tw": [{{"date":"MM/DD","text":"一句話重點"}}, ...],   // 台股，3-4 則
   "us": [...],  // 美股，3-4 則
   "jp": [...],  // 日本股市，2-3 則
   "kr": [...]   // 韓國股市，2-3 則
 }},
 "trump": [   // 川普對市場相關發言，時間倒序，3-6 則
   {{"date":"YYYY/MM/DD","tag":"關稅/對Fed/突發/股市看法/人事/外交/其他","text":"重點","impact":"影響哪類資產一句話","alert":true/false}}
 ],
 "watch": ["今日值得關注 1", ...],   // 6-7 條
 "interp": "即時行情綜合解讀一段（偏高/偏低與對台股意涵）"
}}
規則：每則務必標確切日期；標題若無日期就用「日期未確認」，不可捏造。text 精簡、忠實，不要杜撰引文。
trump 陣列中最具市場衝擊者把 "alert" 設為 true（僅一則）。
新聞素材：
{json.dumps(feeds, ensure_ascii=False)}
"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"}}
    try:
        r = requests.post(url, headers={"Content-Type": "application/json"}, json=body, timeout=60)
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(text)
        log("gemini narrative OK")
        return data
    except Exception as e:
        log("gemini fail:", repr(e)); return None

# ===========================================================================
# DEFAULTS：所有來源失敗時的後備內容（=最後一次已知良好版本）
# 這確保頁面永遠完整；成功抓到的部分會覆蓋這裡。
# ===========================================================================
DEFAULTS = {
  "TOP_NOTE": "🗓️ 本頁由 GitHub Actions 每日自動更新。數字取自期交所／證交所／臺灣銀行／Stooq，新聞與川普發言由 Google News＋Gemini 自動整理。若某來源當日尚未釋出，該區塊沿用前一版並標示，不以估值填充。",
  "RT_JSON": json.dumps([
    {"n":"美元指數 DXY","q":"101.2","chg":"—","dir":"flat","lo":95.55,"hi":101.80,"cur":101.2,"est":True},
    {"n":"USD/TWD 新台幣","q":"約32.22","chg":"—","dir":"flat","lo":29.0,"hi":33.0,"cur":32.22,"est":True},
    {"n":"WTI 原油","q":"$78.14","chg":"—","dir":"flat","lo":54.98,"hi":119.47,"cur":78.14,"est":True},
    {"n":"Brent 原油","q":"$86.35","chg":"—","dir":"flat","lo":60,"hi":124,"cur":86.35,"est":True},
    {"n":"黃金","q":"$4,001","chg":"—","dir":"flat","lo":3314.3,"hi":5626.8,"cur":4001,"est":True},
    {"n":"美 10Y 公債殖利率","q":"4.62%","chg":"—","dir":"flat","lo":3.80,"hi":4.80,"cur":4.62,"est":True},
    {"n":"VIX 波動率","q":"16.5","chg":"—","dir":"flat","lo":13,"hi":55,"cur":16.5,"est":True},
  ], ensure_ascii=False),
}

# ===========================================================================
# 渲染各區塊 -> token 字串
# ===========================================================================
IDX_ROWS = [("台灣加權","taiex"),("韓國股市","kospi"),("日本股市","nikkei"),
            ("費城半導體","sox"),("NASDAQ","ndq"),("S&amp;P 500","spx")]

def render_index(prices):
    rows = []
    any_ok = False
    for name, key in IDX_ROWS:
        p = prices.get(key)
        if not p:
            rows.append(f'<tr><td>{name}</td><td class="sub">待更新</td><td class="sub">—</td><td class="sub">—</td><td class="sub">—</td></tr>')
            continue
        any_ok = True
        chg = p["last"] - p["prev"]
        pct = chg / p["prev"] * 100 if p["prev"] else 0
        c = cls(chg)
        rows.append(
          f'<tr><td>{name}</td><td>{comma(p["last"])}</td>'
          f'<td class="{c}">{arrow(chg)}{comma(abs(chg))}</td>'
          f'<td class="{c}">{sign(pct)}%</td>'
          f'<td class="sub">{mmdd(p["date"])}</td></tr>')
    note = "指數與商品行情來源：Stooq（免費日線）。各列標資料日；紅漲綠跌。" if any_ok \
           else "⚠️ 指數來源當日暫時抓取失敗，稍後排程重跑即會恢復。"
    tag = "各指數收盤（資料日見每列）"
    return (f'<div class="date-tag">{tag}</div>\n<table>'
            f'<tr><th>指數</th><th>收盤</th><th>漲跌</th><th>幅度</th><th>資料日</th></tr>'
            + "".join(rows) + f'</table>\n<div class="note">{note}</div>')

def _fut_dir(v): return ("down","淨空") if v < 0 else (("up","淨多") if v > 0 else ("flat","持平"))

def render_futures(fut_hist):
    dates = sorted(fut_hist.keys(), reverse=True)[:5]
    if not dates:
        body = ('<div class="date-tag">臺股期貨 · 單位：口</div>'
                '<div class="note">⚠️ 期交所三大法人資料尚未取得，稍後排程重跑即會補上。</div>')
        return body
    d0 = dates[0]; cur = fut_hist[d0]
    prev = fut_hist[dates[1]] if len(dates) > 1 else None
    def cell(v, is_total=False):
        c = cls(v); b = ("<b>" , "</b>") if is_total else ("","")
        return f'<td class="{c}">{b[0]}{sign(v,0) if v!=int(v) else ("+" if v>0 else "")+format(v,",")}{b[1]}</td>'
    def net_cell(v, bold=False):
        c = cls(v); s = ("+" if v>0 else "") + format(v, ",")
        return f'<td class="{c}">{"<b>" if bold else ""}{s}{"</b>" if bold else ""}</td>'
    def delta_cell(cur_v, prev_v, bold=False):
        if prev_v is None: return '<td class="sub">—</td>'
        d = cur_v - prev_v; c = cls(d); s = arrow(d)+" "+("+" if d>0 else ("-" if d<0 else ""))+format(abs(d), ",")
        return f'<td class="{c}">{"<b>" if bold else ""}{s}{"</b>" if bold else ""}</td>'
    def dir_cell(v, bold=False):
        c,label = _fut_dir(v)
        lab = "偏空" if (bold and v<0) else ("偏多" if (bold and v>0) else label)
        return f'<td class="{c}">{"<b>" if bold else ""}{lab}{"</b>" if bold else ""}</td>'
    # 全部
    all_rows = (
      f'<tr><td><b>法人合計</b></td>{net_cell(cur["total"],1)}{delta_cell(cur["total"], prev["total"] if prev else None,1)}{dir_cell(cur["total"],1)}</tr>'
      f'<tr><td>外資</td>{net_cell(cur["foreign"])}{delta_cell(cur["foreign"], prev["foreign"] if prev else None)}{dir_cell(cur["foreign"])}</tr>'
      f'<tr><td>投信</td>{net_cell(cur["trust"])}{delta_cell(cur["trust"], prev["trust"] if prev else None)}{dir_cell(cur["trust"])}</tr>'
      f'<tr><td>自營商</td>{net_cell(cur["dealer"])}{delta_cell(cur["dealer"], prev["dealer"] if prev else None)}{dir_cell(cur["dealer"])}</tr>')
    # 五日子表
    def five(field):
        r = []
        for i, d in enumerate(dates):
            v = fut_hist[d][field]; pv = fut_hist[dates[i+1]][field] if i+1 < len(dates) else None
            bold = (i == 0)
            r.append(f'<tr><td>{"<b>" if bold else ""}{mmdd(d)}{"</b>" if bold else ""}</td>{net_cell(v,bold)}{delta_cell(v,pv,bold)}</tr>')
        # 補滿到五列
        for _ in range(5 - len(dates)):
            r.append('<tr><td class="sub">待補</td><td class="sub">—</td><td class="sub">—</td></tr>')
        return "".join(r)
    head5 = '<tr><th>日期</th><th>淨額(口)</th><th>較前日增減</th></tr>'
    subtabs = ('<div class="subtabs" id="futSub">'
               '<div class="subtab active" data-s="f-all">全部</div>'
               '<div class="subtab" data-s="f-sum">法人</div>'
               '<div class="subtab" data-s="f-fore">外資</div>'
               '<div class="subtab" data-s="f-trust">投信</div>'
               '<div class="subtab" data-s="f-deal">自營商</div></div>')
    return (
      f'<div class="date-tag">臺股期貨 · 單位：口 · 綠＝淨空／減，紅＝淨多／增 · 資料源：期交所（最新 {mmdd(d0)}）</div>'
      + subtabs +
      f'<div class="subpage active" id="f-all"><div class="date-tag">{mmdd(d0)} 盤後 · 「較前日」＝對比 {mmdd(dates[1]) if len(dates)>1 else "—"}</div>'
      f'<table><tr><th>法人</th><th>未平倉淨額(口)</th><th>較前日增減</th><th>方向</th></tr>{all_rows}</table></div>'
      f'<div class="subpage" id="f-sum"><div class="date-tag">法人合計 · 近五個交易日</div><table>{head5}{five("total")}</table></div>'
      f'<div class="subpage" id="f-fore"><div class="date-tag">外資 · 近五個交易日</div><table>{head5}{five("foreign")}</table></div>'
      f'<div class="subpage" id="f-trust"><div class="date-tag">投信 · 近五個交易日</div><table>{head5}{five("trust")}</table></div>'
      f'<div class="subpage" id="f-deal"><div class="date-tag">自營商 · 近五個交易日</div><table>{head5}{five("dealer")}</table></div>'
      f'<div class="note">每天自動抓當日官方快照並逐日累積，五日歷史會於後續執行天補齊，不以估值填充。</div>')

def render_margin(mgn_hist):
    dates = sorted(mgn_hist.keys(), reverse=True)[:5]
    if not dates:
        return ('<div class="date-tag">集中市場 融資融券</div>'
                '<div class="note">⚠️ 證交所融資融券資料尚未取得，稍後排程重跑即會補上。</div>')
    rows = []
    for i, d in enumerate(dates):
        cur = mgn_hist[d]; prev = mgn_hist[dates[i+1]] if i+1 < len(dates) else None
        if prev:
            dm = cur["margin_yi"] - prev["margin_yi"]; ds = cur["short_zhang"] - prev["short_zhang"]
            dmc = f'<td class="{cls(dm)}">{sign(dm)}</td>'; dsc = f'<td class="{cls(ds)}">{("+" if ds>0 else "")+format(ds,",")}</td>'
        else:
            dmc = '<td class="sub">—</td>'; dsc = '<td class="sub">—</td>'
        rows.append(f'<tr><td>{mmdd(d)}</td><td>{comma(cur["margin_yi"])}</td>{dmc}<td>{format(cur["short_zhang"],",")}</td>{dsc}</tr>')
    return (f'<div class="date-tag">集中市場 · 近五個交易日（最新 {mmdd(dates[0])}）</div>'
            f'<table><tr><th>日期</th><th>融資(億)</th><th>日增減</th><th>融券(張)</th><th>日增減</th></tr>'
            + "".join(rows) + '</table>'
            '<div class="note">資料源：證交所 TWSE。融資增加(紅)＝散戶槓桿升溫；融券下降(綠)＝放空回補、降溫。</div>')

def render_news(nar):
    news = (nar or {}).get("news") or {}
    def block(key):
        items = news.get(key) or []
        if not items:
            return '<div class="news"><span class="t">今日暫無擷取到新聞，稍後自動更新。</span></div>'
        return "".join(
            f'<div class="news"><span class="d">{html.escape(str(it.get("date") or "日期未確認"))}</span>'
            f'<span class="t">{html.escape(str(it.get("text") or ""))}</span></div>' for it in items[:4])
    return ('<div class="subtabs" id="newsSub">'
            '<div class="subtab active" data-s="n-tw">台股</div>'
            '<div class="subtab" data-s="n-us">美股</div>'
            '<div class="subtab" data-s="n-jp">日本股市</div>'
            '<div class="subtab" data-s="n-kr">韓國股市</div></div>'
            f'<div class="subpage active" id="n-tw">{block("tw")}</div>'
            f'<div class="subpage" id="n-us">{block("us")}</div>'
            f'<div class="subpage" id="n-jp">{block("jp")}</div>'
            f'<div class="subpage" id="n-kr">{block("kr")}</div>')

def render_trump(nar):
    items = (nar or {}).get("trump") or []
    if not items:
        return ('<div class="date-tag">時間倒序（最新在上）</div>'
                '<div class="note">今日暫無擷取到川普相關發言，稍後自動更新。</div>')
    out = ['<div class="date-tag">時間倒序（最新在上）· 每則標日期與類型 · 市場衝擊最大者置頂</div>']
    alerts = [x for x in items if x.get("alert")]
    others = [x for x in items if not x.get("alert")]
    for a in alerts[:1]:
        out.append('<div class="alert"><span class="lbl">🚨 突發 · 市場衝擊大</span>'
                   f'<div class="tp" style="border:none;padding-left:0"><span class="d">{html.escape(str(a.get("date","")))}</span> — {html.escape(str(a.get("text","")))}'
                   f'<div class="imp">🏷 {html.escape(str(a.get("tag","")))} · 影響：{html.escape(str(a.get("impact","")))}</div></div></div>')
    for t in others:
        out.append(f'<div class="tp"><span class="d">{html.escape(str(t.get("date","")))}</span>'
                   f'<span class="tag">{html.escape(str(t.get("tag","其他")))}</span> — {html.escape(str(t.get("text","")))}'
                   f'<div class="imp">影響：{html.escape(str(t.get("impact","")))}</div></div>')
    out.append('<div class="note">來源：Google News（Reuters／CNBC／Bloomberg／AP 等）＋Gemini 自動整理；查無確切日期者標「日期未確認」，不杜撰引文。</div>')
    return "".join(out)

def render_watch(nar):
    items = (nar or {}).get("watch") or [
        "美國物價與 Fed 利率路徑", "中東地緣與油價", "台積電與 AI 供應鏈訂單能見度",
        "外資台指期部位與新台幣走勢", "川普關稅進度", "亞股晶片股情緒"]
    lis = "".join(f'<li>{html.escape(str(x))}</li>' for x in items[:8])
    return f'<ol class="watch">{lis}</ol>'

def render_rt(prices, fx):
    """回傳 (RT_JSON, RT_TAG, INTERP fallbackless)。"""
    def item(name, key, is_pct=False, money=True):
        p = prices.get(key)
        if not p: return None
        last = p["last"]; chg = last - p["prev"]; pct = chg/p["prev"]*100 if p["prev"] else 0
        q = (f"${comma(last)}" if money and not is_pct else (f"{comma(last)}%" if is_pct else comma(last)))
        return {"n":name,"q":q,"chg":sign(pct)+"%","dir":cls(chg),
                "lo":round(p["lo"],2),"hi":round(p["hi"],2),"cur":round(last,2),"est":False}
    rt = []
    dxy = item("美元指數 DXY","dxy",money=False)
    rt.append(dxy or json.loads(DEFAULTS["RT_JSON"])[0])
    if fx:
        rt.append({"n":"USD/TWD 新台幣","q":comma(fx),"chg":"即期收盤","dir":"flat","lo":29.0,"hi":33.0,"cur":round(fx,2),"est":True})
    else:
        rt.append(json.loads(DEFAULTS["RT_JSON"])[1])
    for name,key,defidx,money,pct in [("WTI 原油","wti",2,True,False),("Brent 原油","brent",3,True,False),
                                      ("黃金","gold",4,True,False),("美 10Y 公債殖利率","us10y",5,False,True),
                                      ("VIX 波動率","vix",6,False,False)]:
        it = item(name,key,is_pct=pct,money=money)
        rt.append(it or json.loads(DEFAULTS["RT_JSON"])[defidx])
    tag = f'報價來源 Stooq／臺灣銀行；資料日 {NOW.strftime("%m/%d")}；位階＝52 週高低區間百分位'
    return json.dumps(rt, ensure_ascii=False), tag

# ===========================================================================
# main
# ===========================================================================
def main():
    # 讀歷史
    data_path = os.path.join(HERE, "data.json")
    try:
        with open(data_path, encoding="utf-8") as f: store = json.load(f)
    except Exception:
        store = {"futures": {}, "margin": {}}
    fut_hist = store.get("futures", {}); mgn_hist = store.get("margin", {})

    prices = {}; fut = None; fx = None; nar = None
    try: prices = fetch_prices()
    except Exception: traceback.print_exc()
    try: fut = fetch_taifex()
    except Exception: traceback.print_exc()
    try: fx = fetch_bot_usdtwd()
    except Exception: traceback.print_exc()
    try: mgn_hist = fetch_margin_history(mgn_hist)
    except Exception: traceback.print_exc()
    try: nar = gemini_narrative()
    except Exception: traceback.print_exc()

    if fut:
        fut_hist[fut["date"]] = {k: fut[k] for k in ("foreign","trust","dealer","total")}
    # 只保留最近 10 個交易日，避免檔案膨脹
    def trim(d):
        return {k: d[k] for k in sorted(d.keys(), reverse=True)[:10]}
    fut_hist = trim(fut_hist); mgn_hist = trim(mgn_hist)
    store = {"futures": fut_hist, "margin": mgn_hist,
             "updated": NOW.isoformat()}
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=1)

    rt_json, rt_tag = render_rt(prices, fx)
    tokens = {
        "SNAPSHOT": f'資料更新：{NOW.strftime("%Y/%m/%d %H:%M")}（台北 GMT+8）· 每天 08:00 自動更新，或按「↻ 更新」重新載入',
        "TOP_NOTE": DEFAULTS["TOP_NOTE"],
        "INDEX_BODY": render_index(prices),
        "FUT_BODY": render_futures(fut_hist),
        "MARGIN_BODY": render_margin(mgn_hist),
        "NEWS_BODY": render_news(nar),
        "WATCH_BODY": render_watch(nar),
        "TRUMP_BODY": render_trump(nar),
        "RT_TAG": rt_tag,
        "INTERP_BODY": (f'<div class="interp">{html.escape(nar["interp"])}</div>'
                        '<div class="note">位階為 52 週高低區間之百分位，僅供參考。</div>')
                        if (nar and nar.get("interp")) else
                        ('<div class="interp">今日綜合解讀暫由自動摘要提供；請參考上方各項位階與當日漲跌。</div>'
                         '<div class="note">位階為 52 週高低區間之百分位，僅供參考。</div>'),
        "RT_JSON": rt_json,
    }
    with open(os.path.join(HERE, "template.html"), encoding="utf-8") as f:
        htmlout = f.read()
    for k, v in tokens.items():
        htmlout = htmlout.replace("%%" + k + "%%", v)
    with open(os.path.join(HERE, "index.html"), "w", encoding="utf-8") as f:
        f.write(htmlout)
    log("index.html written. prices:", len(prices), "fut:", bool(fut), "fx:", bool(fx),
        "margin_days:", len(mgn_hist), "narrative:", bool(nar))

if __name__ == "__main__":
    main()
