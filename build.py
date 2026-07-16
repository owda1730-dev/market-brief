# -*- coding: utf-8 -*-
# ================= BUILD VERSION: v7 =================
# 確認方法：data.json 的 status.version 應為 "v7"。若不是，代表上傳到舊檔。
"""
市場總覽 · 每日自動建置腳本（v4）
在 GitHub Actions（每天排程）上執行：抓官方/免費資料 → Gemini 產生敘事 → 填 template.html → index.html
任何來源失敗都退回 DEFAULTS，頁面永遠完整。每次執行把各來源成敗寫入 data.json 的 "status"，方便遠端診斷。
"""
import os, sys, json, re, io, csv, html, time, datetime, traceback
import urllib.parse
import requests

TZ = datetime.timezone(datetime.timedelta(hours=8))
NOW = datetime.datetime.now(TZ)
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
HERE = os.path.dirname(os.path.abspath(__file__))
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
STATUS = {}   # 來源成敗診斷，寫進 data.json

def log(*a): print("[build]", *a, flush=True)

def http_get(url, timeout=25, is_json=False, params=None):
    r = requests.get(url, headers=UA, timeout=timeout, params=params)
    r.raise_for_status()
    return r.json() if is_json else r.text

# ---- 格式工具（台灣慣例：紅漲綠跌；增加=紅，減少=綠）----
def cls(v):   return "up" if v > 0 else ("down" if v < 0 else "flat")
def arrow(v): return "▲" if v > 0 else ("▼" if v < 0 else "—")
def sign(v, nd=2): return ("+" if v > 0 else ("-" if v < 0 else "")) + f"{abs(v):,.{nd}f}"
def comma(v, nd=2):
    try: return f"{v:,.{nd}f}"
    except Exception: return str(v)
def mmdd(iso):
    try: return iso[5:7] + "/" + iso[8:10]
    except Exception: return str(iso)

# ===========================================================================
# 指數與行情：Yahoo Finance chart API（主）＋ Stooq（備援）
# ===========================================================================
YF = "https://query1.finance.yahoo.com/v8/finance/chart/{s}?range=1y&interval=1d"
YAHOO_SYMBOLS = {
    "taiex":"^TWII","kospi":"^KS11","nikkei":"^N225","sox":"^SOX","ndq":"^IXIC","spx":"^GSPC",
    "dxy":"DX-Y.NYB","wti":"CL=F","brent":"BZ=F","gold":"GC=F","us10y":"^TNX","vix":"^VIX","usdtwd":"TWD=X",
}
STOOQ_SYMBOLS = {  # 備援
    "taiex":"^twse","kospi":"^kospi","nikkei":"^nkx","sox":"^sox","ndq":"^ndq","spx":"^spx",
    "wti":"cl.f","brent":"cb.f","gold":"xauusd","vix":"^vix","usdtwd":"usdtwd",
}

def _pack(closes_dates, key):
    closes = [(d, c) for d, c in closes_dates if c is not None]
    if len(closes) < 2: return None
    last = closes[-1][1]; prev = closes[-2][1]
    window = [c for _, c in closes[-252:]]
    if key == "us10y" and last and last > 20:      # ^TNX 有時以 ×10 報價
        last, prev = last/10.0, prev/10.0; window = [c/10.0 for c in window]
    return {"last": last, "prev": prev, "lo": min(window), "hi": max(window), "date": closes[-1][0]}

def _parse_yahoo(j, key):
    res = j["chart"]["result"][0]
    ts = res.get("timestamp") or []
    closes = res["indicators"]["quote"][0]["close"]
    cd = [(datetime.datetime.utcfromtimestamp(ts[i]).date().isoformat() if i < len(ts) else "", c)
          for i, c in enumerate(closes)]
    return _pack(cd, key)

def _parse_stooq(txt, key):
    cd = []
    for r in csv.DictReader(io.StringIO(txt)):
        try: cd.append((r["Date"], float(r["Close"])))
        except Exception: pass
    return _pack(cd, key)

def fetch_prices_yf():
    """yfinance 批次下載（一次請求、自動處理 Yahoo 驗證，較不易被 429）。"""
    import yfinance as yf
    syms = list(dict.fromkeys(YAHOO_SYMBOLS.values()))
    df = yf.download(syms, period="1y", interval="1d", progress=False,
                     group_by="ticker", threads=False, auto_adjust=False)
    out = {}; diag = {}
    lvl0 = set(df.columns.get_level_values(0)) if hasattr(df.columns, "get_level_values") else set()
    for key, sym in YAHOO_SYMBOLS.items():
        try:
            sub = df[sym] if sym in lvl0 else (df if len(syms) == 1 else None)
            if sub is None or "Close" not in sub:
                diag[key] = "yfin:no-col"; continue
            s = sub["Close"].dropna()
            cd = [(idx.date().isoformat(), float(v)) for idx, v in s.items()]
            got = _pack(cd, key)
            if got: got["src"] = "yfin"; out[key] = got
            else: diag[key] = "yfin:parse-none"
        except Exception as e:
            diag[key] = f"yfin:{type(e).__name__}"
    return out, diag

def fetch_prices_raw(keys):
    """備援：逐一打 Yahoo v8 / Stooq CSV。"""
    out = {}; diag = {}
    for key in keys:
        got = None
        sym = YAHOO_SYMBOLS.get(key)
        if sym:
            try:
                r = requests.get(YF.format(s=urllib.parse.quote(sym)), headers={**UA, "Accept": "application/json"}, timeout=20)
                if r.status_code == 200:
                    got = _parse_yahoo(r.json(), key)
                    if got: got["src"] = "yf"
                    else: diag[key] = "yf:parse-none"
                else: diag[key] = f"yf:HTTP{r.status_code}"
            except Exception as e: diag[key] = f"yf:{type(e).__name__}"
        if not got:
            ssym = STOOQ_SYMBOLS.get(key)
            if ssym:
                try:
                    r = requests.get(f"https://stooq.com/q/d/l/?s={urllib.parse.quote(ssym)}&i=d", headers=UA, timeout=20)
                    txt = r.text
                    if "Date" in txt and "Close" in txt:
                        got = _parse_stooq(txt, key)
                        if got: got["src"] = "stooq"
                    else: diag[key] = "stooq:" + txt.strip().replace("\n", " ")[:40]
                except Exception as e: diag[key] = f"stooq:{type(e).__name__}"
        if got: out[key] = got
        time.sleep(0.2)
    return out, diag

def fetch_prices():
    out = {}; diag = {}
    try:
        out, diag = fetch_prices_yf()
    except Exception as e:
        STATUS["prices_yf_exc"] = f"{type(e).__name__}:{str(e)[:80]}"
    missing = [k for k in YAHOO_SYMBOLS if k not in out]
    if missing:
        raw_out, raw_diag = fetch_prices_raw(missing)
        out.update(raw_out); diag.update({k: v for k, v in raw_diag.items() if k not in out})
    STATUS["prices_ok"] = list(out.keys())
    STATUS["prices_fail"] = [k for k in YAHOO_SYMBOLS if k not in out]
    if diag: STATUS["price_diag"] = {k: v for k, v in diag.items() if k not in out}
    log("prices ok:", list(out.keys()))
    return out

# ===========================================================================
# 期交所三大法人（臺股期貨 未平倉多空淨額「口數」= 倒數第二個數字欄）
# ===========================================================================
def fetch_taifex():
    import pandas as pd
    txt = http_get("https://www.taifex.com.tw/cht/3/futContractsDateExcel", timeout=25)
    m = re.search(r"日期\s*(\d{4})/(\d{2})/(\d{2})", txt)
    date_iso = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else NOW.date().isoformat()
    tables = pd.read_html(io.StringIO(txt))
    for df in tables:
        flat = df.astype(str)
        rows = flat.values.tolist()
        for i, row in enumerate(rows):
            if "臺股期貨" in " ".join(row):
                def oi(r):
                    ints = re.findall(r"-?\d[\d,]*", " ".join(r))
                    # 未平倉多空淨額有「口數、契約金額」兩欄，口數＝倒數第二
                    return int(ints[-2].replace(",", "")) if len(ints) >= 2 else None
                dealer, trust, foreign = oi(rows[i]), oi(rows[i+1]), oi(rows[i+2])
                if None in (dealer, trust, foreign):
                    raise ValueError("taifex 口數解析失敗")
                return {"date": date_iso, "foreign": foreign, "trust": trust,
                        "dealer": dealer, "total": foreign + trust + dealer}
    raise ValueError("找不到臺股期貨列")

# ===========================================================================
# 證交所 融資融券 市場合計（MI_MARGN, selectType=MS）
# ===========================================================================
def _num(x):
    try: return float(str(x).replace(",", ""))
    except Exception: return None

def fetch_margin_for(yyyymmdd):
    j = http_get("https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN", is_json=True,
                 params={"response": "json", "date": yyyymmdd, "selectType": "MS"}, timeout=20)
    if not j or j.get("stat") != "OK":
        return None
    tables = j.get("tables") or []
    if not tables and j.get("data"):
        tables = [{"data": j["data"]}]
    margin_yi = short_zhang = None
    for t in tables:
        for row in (t.get("data") or []):
            if not row: continue
            head = str(row[0])
            nums = [_num(x) for x in row[1:] if _num(x) is not None]
            if not nums: continue
            today = _num(row[5]) if len(row) > 5 and _num(row[5]) is not None else nums[-1]
            if "融資金額" in head:                    # 仟元 → 億（÷100000）
                margin_yi = round(today / 100000.0, 2)
            elif head.startswith("融券") and ("交易單位" in head or "張" in head):
                short_zhang = int(today)
    if margin_yi is None or short_zhang is None:
        STATUS.setdefault("margin_err", {})[yyyymmdd] = f"shape={[ (str(r[0]) if r else '') for tb in tables for r in (tb.get('data') or []) ][:6]}"
        return None
    return margin_yi, short_zhang

def fetch_margin_history(prev_hist):
    hist = dict(prev_hist or {}); got_any = False
    for back in range(0, 9):
        d = (NOW - datetime.timedelta(days=back)).date()
        if d.weekday() >= 5: continue
        iso = d.isoformat()
        if iso in hist: continue
        try:
            r = fetch_margin_for(d.strftime("%Y%m%d"))
            if r: hist[iso] = {"margin_yi": r[0], "short_zhang": r[1]}; got_any = True
        except Exception as e:
            STATUS.setdefault("margin_exc", str(type(e).__name__))
        time.sleep(0.3)
    STATUS["margin_days"] = len([k for k in hist])
    return hist

# ===========================================================================
# 臺灣銀行 USD/TWD（備援；主用 Yahoo TWD=X）
# ===========================================================================
def fetch_bot_usdtwd():
    txt = http_get("https://rate.bot.com.tw/xrt/flcsv/0/day/USD", timeout=20)
    rows = [r for r in csv.reader(io.StringIO(txt))]
    # 臺銀 CSV 第 4 欄通常為「即期買入」、第 5 欄「即期賣出」；取最後一列即期賣出
    for r in reversed(rows):
        if len(r) >= 5:
            v = _num(r[4]) or _num(r[3])
            if v and 20 < v < 45: return v
    return None

# ===========================================================================
# Google News RSS -> Gemini
# ===========================================================================
NEWS_FEEDS = {"tw":"台股 加權指數", "us":"美股 標普 那斯達克 半導體",
              "jp":"日經 225 東京股市", "kr":"KOSPI 韓國股市",
              "trump":"Trump tariffs oil Fed stock market"}

def fetch_truth_social(n=15):
    """抓 trumpstruth.org 的川普 Truth Social 貼文 RSS（真實即時、正確日期）。"""
    txt = http_get("https://trumpstruth.org/feed", timeout=20)
    items = []
    for m in re.finditer(r"<item>(.*?)</item>", txt, re.S):
        b = m.group(1)
        d = re.search(r"<description>(.*?)</description>", b, re.S)
        t = re.search(r"<title>(.*?)</title>", b, re.S)
        p = re.search(r"<pubDate>(.*?)</pubDate>", b, re.S)
        raw = (d.group(1) if d else (t.group(1) if t else ""))
        text = html.unescape(re.sub(r"<[^>]*>", " ", raw)).strip()
        text = re.sub(r"\s+", " ", text)
        date = ""
        if p:
            try: date = datetime.datetime.strptime(p.group(1).strip(), "%a, %d %b %Y %H:%M:%S %Z").strftime("%Y/%m/%d")
            except Exception: date = ""
        if text and len(text) > 3:
            items.append({"date": date, "title": text[:240]})
        if len(items) >= n: break
    return items
def gnews(query, en=False, n=8):
    hl, gl, ceid = ("en-US","US","US:en") if en else ("zh-TW","TW","TW:zh-Hant")
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(query) + f"&hl={hl}&gl={gl}&ceid={ceid}"
    txt = http_get(url, timeout=20)
    items = []
    for m in re.finditer(r"<item>(.*?)</item>", txt, re.S):
        b = m.group(1)
        t = re.search(r"<title>(.*?)</title>", b, re.S)
        p = re.search(r"<pubDate>(.*?)</pubDate>", b, re.S)
        title = html.unescape(re.sub(r"<.*?>", "", t.group(1)).strip()) if t else ""
        pub = ""
        if p:
            try: pub = datetime.datetime.strptime(p.group(1).strip(), "%a, %d %b %Y %H:%M:%S %Z").strftime("%m/%d")
            except Exception: pub = ""
        if title: items.append({"date": pub, "title": title})
        if len(items) >= n: break
    return items

def gemini_candidates():
    """問金鑰有哪些可用型號，組出一串「文字型 flash」候選（逐一試到成功）。"""
    avail = []
    try:
        j = http_get(f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_KEY}", is_json=True, timeout=20)
        avail = [m["name"] for m in j.get("models", []) if "generateContent" in (m.get("supportedGenerationMethods") or [])]
        STATUS["gemini_models_avail"] = avail[:25]
    except Exception as e:
        STATUS["gemini_list_err"] = f"{type(e).__name__}:{str(e)[:80]}"
    def usable(n):
        bad = ("image", "tts", "vision", "embedding", "aqa", "gemma", "nano", "tuning", "thinking")
        return ("flash" in n) and not any(b in n for b in bad)
    preferred = ["models/gemini-flash-latest", "models/gemini-2.0-flash", "models/gemini-2.0-flash-001",
                 "models/gemini-2.5-flash-lite", "models/gemini-3-flash-preview", "models/gemini-3.1-flash-lite"]
    cands = [m for m in preferred if (not avail or m in avail)]
    cands += [m for m in avail if usable(m) and m not in cands]
    return cands or ["models/gemini-flash-latest"]

def gemini_call(prompt):
    last = None
    for model in gemini_candidates():
        url = f"https://generativelanguage.googleapis.com/v1beta/{model}:generateContent?key={GEMINI_KEY}"
        for cfg in ({"temperature": 0.4, "responseMimeType": "application/json"}, {"temperature": 0.4}):
            try:
                r = requests.post(url, headers={"Content-Type": "application/json"},
                                  json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg}, timeout=60)
                if r.status_code != 200:
                    last = f"{model}:HTTP{r.status_code}:{r.text[:80]}"; continue
                txt = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                if txt.startswith("```"):
                    txt = re.sub(r"^```[a-zA-Z]*\n?", "", txt); txt = re.sub(r"\n?```$", "", txt)
                data = json.loads(txt)
                STATUS["gemini_model"] = model
                return data
            except Exception as e:
                last = f"{model}:{type(e).__name__}:{str(e)[:60]}"
    STATUS["gemini_err"] = last
    return None

def gemini_narrative():
    if not GEMINI_KEY:
        STATUS["gemini_err"] = "no GEMINI_API_KEY secret"; return None
    feeds = {}
    for k, q in NEWS_FEEDS.items():
        try: feeds[k] = gnews(q, en=(k == "trump"))
        except Exception as e:
            feeds[k] = []; STATUS.setdefault("news_err", {})[k] = type(e).__name__
    # 川普：優先用 trumpstruth.org 的真實 Truth Social 貼文（正確即時日期）
    try:
        ts = fetch_truth_social()
        STATUS["truth_count"] = len(ts)
        if ts: feeds["trump"] = ts
    except Exception as e:
        STATUS["truth_err"] = type(e).__name__
    STATUS["news_counts"] = {k: len(v) for k, v in feeds.items()}
    today = NOW.strftime("%Y/%m/%d")
    prompt = f"""你是台灣財經編輯。今天 {today}（台北）。以下為各市場新聞標題與日期（Google News RSS）。
只根據這些標題，用繁體中文輸出 JSON（不要多餘文字）：
{{"news":{{"tw":[{{"date":"MM/DD","text":"重點"}}],"us":[...],"jp":[...],"kr":[...]}},
"trump":[{{"date":"YYYY/MM/DD","tag":"關稅/對Fed/突發/股市看法/人事/外交/其他","text":"重點","impact":"影響哪類資產","alert":true}}],
"watch":["值得關注1"],"interp":"即時行情綜合解讀一段"}}
規則：每則標確切日期，無法確定寫「日期未確認」不可捏造；tw/us 各3-4則、jp/kr 各2-3則、watch 6-7條。
trump 區塊：素材中的 trump 是川普 Truth Social 的真實貼文（含實際發文日期），請「只根據這些貼文」摘要，**用貼文的實際日期**、時間倒序（最新在最前），挑最近且與市場/政策相關的 4-6 則，忠實不杜撰引文；最具市場衝擊的一則 alert=true。
素材：{json.dumps(feeds, ensure_ascii=False)}"""
    return gemini_call(prompt)

# ===========================================================================
# DEFAULTS（全失敗時後備）
# ===========================================================================
DEFAULTS = {
  "TOP_NOTE": "🗓️ 本頁每日自動更新。某項當日資料尚未釋出時，該區塊標示待更新、不以估值填充。",
  "RT_JSON": json.dumps([
    {"n":"美元指數 DXY","q":"—","chg":"—","dir":"flat","lo":95.55,"hi":101.80,"cur":98,"est":True},
    {"n":"USD/TWD 新台幣","q":"—","chg":"—","dir":"flat","lo":29.0,"hi":33.0,"cur":31,"est":True},
    {"n":"WTI 原油","q":"—","chg":"—","dir":"flat","lo":54.98,"hi":119.47,"cur":78,"est":True},
    {"n":"Brent 原油","q":"—","chg":"—","dir":"flat","lo":60,"hi":124,"cur":83,"est":True},
    {"n":"黃金","q":"—","chg":"—","dir":"flat","lo":3314.3,"hi":5626.8,"cur":4000,"est":True},
    {"n":"美 10Y 公債殖利率","q":"—","chg":"—","dir":"flat","lo":3.80,"hi":4.80,"cur":4.3,"est":True},
    {"n":"VIX 波動率","q":"—","chg":"—","dir":"flat","lo":13,"hi":55,"cur":18,"est":True},
  ], ensure_ascii=False),
}

# ===========================================================================
# 渲染
# ===========================================================================
IDX_ROWS = [("台灣加權","taiex"),("韓國股市","kospi"),("日本股市","nikkei"),
            ("費城半導體","sox"),("NASDAQ","ndq"),("S&amp;P 500","spx")]
def render_index(prices):
    rows = []; any_ok = False
    for name, key in IDX_ROWS:
        p = prices.get(key)
        if not p:
            rows.append(f'<tr><td>{name}</td><td class="sub">待更新</td><td class="sub">—</td><td class="sub">—</td><td class="sub">—</td></tr>'); continue
        any_ok = True
        chg = p["last"] - p["prev"]; pct = chg/p["prev"]*100 if p["prev"] else 0; c = cls(chg)
        rows.append(f'<tr><td>{name}</td><td>{comma(p["last"])}</td><td class="{c}">{arrow(chg)}{comma(abs(chg))}</td>'
                    f'<td class="{c}">{sign(pct)}%</td><td class="sub">{mmdd(p["date"])}</td></tr>')
    note = "各列標資料日；紅漲綠跌。" if any_ok else "⚠️ 指數當日暫時抓取失敗，稍後自動重試即會恢復。"
    return (f'<div class="date-tag">各指數收盤（資料日見每列）</div><table>'
            f'<tr><th>指數</th><th>收盤</th><th>漲跌</th><th>幅度</th><th>資料日</th></tr>'
            + "".join(rows) + f'</table><div class="note">{note}</div>')

def render_futures(fut_hist):
    dates = sorted(fut_hist.keys(), reverse=True)[:5]
    if not dates:
        return ('<div class="date-tag">臺股期貨 · 單位：口</div>'
                '<div class="note">⚠️ 期交所三大法人資料尚未取得，稍後排程重跑即會補上。</div>')
    d0 = dates[0]; cur = fut_hist[d0]; prev = fut_hist[dates[1]] if len(dates) > 1 else None
    def net(v, bold=False):
        s = ("+" if v > 0 else "") + format(v, ","); return f'<td class="{cls(v)}">{"<b>" if bold else ""}{s}{"</b>" if bold else ""}</td>'
    def delta(cv, pv, bold=False):
        if pv is None: return '<td class="sub">—</td>'
        d = cv - pv; s = arrow(d)+" "+("+" if d>0 else ("-" if d<0 else ""))+format(abs(d), ","); return f'<td class="{cls(d)}">{"<b>" if bold else ""}{s}{"</b>" if bold else ""}</td>'
    def ddir(v, bold=False):
        lab = ("偏空" if v<0 else ("偏多" if v>0 else "持平")) if bold else ("淨空" if v<0 else ("淨多" if v>0 else "持平"))
        return f'<td class="{cls(v)}">{"<b>" if bold else ""}{lab}{"</b>" if bold else ""}</td>'
    all_rows = (f'<tr><td><b>法人合計</b></td>{net(cur["total"],1)}{delta(cur["total"], prev["total"] if prev else None,1)}{ddir(cur["total"],1)}</tr>'
                f'<tr><td>外資</td>{net(cur["foreign"])}{delta(cur["foreign"], prev["foreign"] if prev else None)}{ddir(cur["foreign"])}</tr>'
                f'<tr><td>投信</td>{net(cur["trust"])}{delta(cur["trust"], prev["trust"] if prev else None)}{ddir(cur["trust"])}</tr>'
                f'<tr><td>自營商</td>{net(cur["dealer"])}{delta(cur["dealer"], prev["dealer"] if prev else None)}{ddir(cur["dealer"])}</tr>')
    def five(field):
        r = []
        for i, d in enumerate(dates):
            v = fut_hist[d][field]; pv = fut_hist[dates[i+1]][field] if i+1 < len(dates) else None; b = (i == 0)
            r.append(f'<tr><td>{"<b>" if b else ""}{mmdd(d)}{"</b>" if b else ""}</td>{net(v,b)}{delta(v,pv,b)}</tr>')
        for _ in range(5 - len(dates)): r.append('<tr><td class="sub">待補</td><td class="sub">—</td><td class="sub">—</td></tr>')
        return "".join(r)
    h5 = '<tr><th>日期</th><th>淨額(口)</th><th>較前日增減</th></tr>'
    st = ('<div class="subtabs" id="futSub"><div class="subtab active" data-s="f-all">全部</div>'
          '<div class="subtab" data-s="f-sum">法人</div><div class="subtab" data-s="f-fore">外資</div>'
          '<div class="subtab" data-s="f-trust">投信</div><div class="subtab" data-s="f-deal">自營商</div></div>')
    return (f'<div class="date-tag">臺股期貨 · 單位：口 · 綠＝淨空／減，紅＝淨多／增 · 最新 {mmdd(d0)}</div>' + st +
            f'<div class="subpage active" id="f-all"><div class="date-tag">{mmdd(d0)} 盤後 · 「較前日」＝對比 {mmdd(dates[1]) if len(dates)>1 else "—"}</div>'
            f'<table><tr><th>法人</th><th>未平倉淨額(口)</th><th>較前日增減</th><th>方向</th></tr>{all_rows}</table></div>'
            f'<div class="subpage" id="f-sum"><div class="date-tag">法人合計 · 近五個交易日</div><table>{h5}{five("total")}</table></div>'
            f'<div class="subpage" id="f-fore"><div class="date-tag">外資 · 近五個交易日</div><table>{h5}{five("foreign")}</table></div>'
            f'<div class="subpage" id="f-trust"><div class="date-tag">投信 · 近五個交易日</div><table>{h5}{five("trust")}</table></div>'
            f'<div class="subpage" id="f-deal"><div class="date-tag">自營商 · 近五個交易日</div><table>{h5}{five("dealer")}</table></div>'
            f'<div class="note">每天自動抓當日官方快照並逐日累積，五日歷史於後續執行天補齊，不以估值填充。</div>')

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
            dmc = dsc = '<td class="sub">—</td>'
        rows.append(f'<tr><td>{mmdd(d)}</td><td>{comma(cur["margin_yi"])}</td>{dmc}<td>{format(cur["short_zhang"],",")}</td>{dsc}</tr>')
    return (f'<div class="date-tag">集中市場 · 近五個交易日（最新 {mmdd(dates[0])}）</div>'
            f'<table><tr><th>日期</th><th>融資(億)</th><th>日增減</th><th>融券(張)</th><th>日增減</th></tr>' + "".join(rows) + '</table>'
            '<div class="note">融資增加(紅)＝散戶槓桿升溫；融券下降(綠)＝放空回補、降溫。</div>')

def render_news(nar):
    news = (nar or {}).get("news") or {}
    def block(key):
        items = news.get(key) or []
        if not items: return '<div class="news"><span class="t">今日暫無擷取到新聞，稍後自動更新。</span></div>'
        return "".join(f'<div class="news"><span class="d">{html.escape(str(it.get("date") or "日期未確認"))}</span>'
                       f'<span class="t">{html.escape(str(it.get("text") or ""))}</span></div>' for it in items[:4])
    return ('<div class="subtabs" id="newsSub"><div class="subtab active" data-s="n-tw">台股</div>'
            '<div class="subtab" data-s="n-us">美股</div><div class="subtab" data-s="n-jp">日本股市</div>'
            '<div class="subtab" data-s="n-kr">韓國股市</div></div>'
            f'<div class="subpage active" id="n-tw">{block("tw")}</div><div class="subpage" id="n-us">{block("us")}</div>'
            f'<div class="subpage" id="n-jp">{block("jp")}</div><div class="subpage" id="n-kr">{block("kr")}</div>')

def render_trump(nar):
    items = (nar or {}).get("trump") or []
    if not items:
        return '<div class="date-tag">時間倒序（最新在上）</div><div class="note">今日暫無擷取到川普相關發言，稍後自動更新。</div>'
    out = ['<div class="date-tag">時間倒序（最新在上）· 每則標日期與類型 · 市場衝擊最大者置頂</div>']
    for a in [x for x in items if x.get("alert")][:1]:
        out.append('<div class="alert"><span class="lbl">🚨 突發 · 市場衝擊大</span>'
                   f'<div class="tp" style="border:none;padding-left:0"><span class="d">{html.escape(str(a.get("date","")))}</span> — {html.escape(str(a.get("text","")))}'
                   f'<div class="imp">🏷 {html.escape(str(a.get("tag","")))} · 影響：{html.escape(str(a.get("impact","")))}</div></div></div>')
    for t in [x for x in items if not x.get("alert")]:
        out.append(f'<div class="tp"><span class="d">{html.escape(str(t.get("date","")))}</span>'
                   f'<span class="tag">{html.escape(str(t.get("tag","其他")))}</span> — {html.escape(str(t.get("text","")))}'
                   f'<div class="imp">影響：{html.escape(str(t.get("impact","")))}</div></div>')
    out.append('<div class="note">時間倒序、以川普 Truth Social 實際貼文為準；查無確切日期者標「日期未確認」。</div>')
    return "".join(out)

def render_watch(nar):
    items = (nar or {}).get("watch") or ["美國物價與 Fed 利率路徑","中東地緣與油價","台積電與 AI 供應鏈訂單能見度",
             "外資台指期部位與新台幣走勢","川普關稅進度","亞股晶片股情緒"]
    return '<ol class="watch">' + "".join(f'<li>{html.escape(str(x))}</li>' for x in items[:8]) + '</ol>'

def render_rt(prices):
    dflt = json.loads(DEFAULTS["RT_JSON"])
    def item(name, key, is_pct=False, money=True, di=0):
        p = prices.get(key)
        if not p: return dflt[di]
        last = p["last"]; chg = last - p["prev"]; pct = chg/p["prev"]*100 if p["prev"] else 0
        q = (f"{comma(last)}%" if is_pct else (f"${comma(last)}" if money else comma(last)))
        return {"n":name,"q":q,"chg":sign(pct)+"%","dir":cls(chg),
                "lo":round(p["lo"],2),"hi":round(p["hi"],2),"cur":round(last,2),"est":False}
    rt = [
      item("美元指數 DXY","dxy",money=False,di=0),
      item("USD/TWD 新台幣","usdtwd",money=False,di=1),
      item("WTI 原油","wti",di=2), item("Brent 原油","brent",di=3),
      item("黃金","gold",di=4), item("美 10Y 公債殖利率","us10y",is_pct=True,money=False,di=5),
      item("VIX 波動率","vix",money=False,di=6),
    ]
    tag = f'資料日 {NOW.strftime("%m/%d")}；位階＝52 週高低區間百分位'
    return json.dumps(rt, ensure_ascii=False), tag

# ===========================================================================
def main():
    STATUS["version"] = "v7"
    data_path = os.path.join(HERE, "data.json")
    try:
        with open(data_path, encoding="utf-8") as f: store = json.load(f)
    except Exception: store = {"futures": {}, "margin": {}}
    fut_hist = store.get("futures", {}); mgn_hist = store.get("margin", {})

    prices = {}
    try: prices = fetch_prices()
    except Exception as e: STATUS["prices_exc"] = repr(e); traceback.print_exc()

    fut = None
    try: fut = fetch_taifex(); STATUS["taifex"] = "ok"
    except Exception as e: STATUS["taifex"] = f"err:{type(e).__name__}:{str(e)[:100]}"; traceback.print_exc()

    # USD/TWD：優先 Yahoo（已在 prices），否則臺銀
    if "usdtwd" not in prices:
        try:
            v = fetch_bot_usdtwd()
            if v: STATUS["bot_fx"] = v
        except Exception as e: STATUS["bot_fx"] = f"err:{type(e).__name__}"

    try: mgn_hist = fetch_margin_history(mgn_hist)
    except Exception as e: STATUS["margin_exc"] = repr(e); traceback.print_exc()

    nar = None
    try: nar = gemini_narrative()
    except Exception as e: STATUS["gemini_err"] = repr(e); traceback.print_exc()

    if fut:
        fut_hist[fut["date"]] = {k: fut[k] for k in ("foreign","trust","dealer","total")}
    trim = lambda d: {k: d[k] for k in sorted(d.keys(), reverse=True)[:10]}
    fut_hist = trim(fut_hist); mgn_hist = trim(mgn_hist)

    rt_json, rt_tag = render_rt(prices)
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
        "INTERP_BODY": (f'<div class="interp">{html.escape(nar["interp"])}</div><div class="note">位階為 52 週高低區間之百分位，僅供參考。</div>')
                       if (nar and nar.get("interp")) else
                       ('<div class="interp">今日綜合解讀暫由自動摘要提供；請參考上方各項位階與當日漲跌。</div><div class="note">位階為 52 週高低區間之百分位，僅供參考。</div>'),
        "RT_JSON": rt_json,
    }
    with open(os.path.join(HERE, "template.html"), encoding="utf-8") as f:
        out = f.read()
    # 清掉空字元/控制字元（避免瀏覽器 JS 解析中斷、整頁按鈕失效）
    ctrl = lambda s: re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    out = ctrl(out)
    out = re.sub(r"\s*<b>資料來源：</b>.*?<br>", "", out, flags=re.S)  # 移除頁尾來源標註
    for k, v in tokens.items():
        out = out.replace("%%" + k + "%%", str(v))
    out = ctrl(out)
    # 自我修復：若樣板結尾被截斷（沒有 </html>），補回完整的 JS 結尾與收尾標籤
    if "</html>" not in out:
        out = out.rstrip()
        if not out.rstrip().endswith("</div></div>'+"):
            out += (":'+pct+'%\"></div></div>'+\n"
                    "      '<div class=\"bar-lbl\"><span>低 '+fmt(r.lo)+'</span><span>52週區間'+(r.est?'（估）':'')+'</span><span>高 '+fmt(r.hi)+'</span></div>'+\n"
                    "    '</div>');\n")
        out += ("});\n"
                "if('serviceWorker' in navigator){window.addEventListener('load',function(){navigator.serviceWorker.register('./sw.js').catch(function(){});});}\n"
                "</script>\n</body>\n</html>\n")
    with open(os.path.join(HERE, "index.html"), "w", encoding="utf-8") as f:
        f.write(out)

    store = {"futures": fut_hist, "margin": mgn_hist, "updated": NOW.isoformat(), "status": STATUS}
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=1)
    log("done. status:", json.dumps(STATUS, ensure_ascii=False))

if __name__ == "__main__":
    main()
