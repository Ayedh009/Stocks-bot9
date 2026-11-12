# -*- coding: utf-8 -*-
"""
overnight.py — توقعات الليل للأسهم (دخول آخر 30 دقيقة، بيع بعد الافتتاح)

المراحل (بتوقيت نيويورك، تُكتشف تلقائياً من الوقت):
  pick    ~3:30 م : توقع كل الأسهم + ترشيح أفضل 3 + عقد (آمن / متوازن / جريء) لكل مرشح
  after   ~4:20 م : فحص حركة ما بعد الإغلاق مقابل التوقع
  pre     ~9:00 ص : فحص ما قبل الافتتاح مقابل التوقع
  result  ~9:50 ص : نتائج كل الشركات (الافتتاح، 9:45، أعلى/أدنى أول 15 دقيقة) وأيها الأفضل
  stats           : إحصائيات تراكمية من السجل

التشغيل:  python overnight.py            (يكتشف المرحلة من الوقت)
          python overnight.py pick|after|pre|result|stats   (إجبار مرحلة)

المتغيرات: BOT_TOKEN, CHAT_ID (أو يأخذها من bot.py إن وُجد)
اختياري : OVERNIGHT_STOCKS=TSLA,NVDA,...  لتحديد القائمة يدوياً
المتطلبات: pip install yfinance requests
"""

import os
import sys
import csv
import json
import math
import re
import time
from datetime import datetime, date, timedelta, time as dtime
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

# ───────────────────────── الإعدادات ─────────────────────────
NY = ZoneInfo("America/New_York")
LOCAL = ZoneInfo(os.getenv("LOCAL_TZ", "America/Chicago"))
LOCAL_NAME = os.getenv("LOCAL_TZ_NAME", "شيكاغو")

STATE_FILE = os.getenv("OVERNIGHT_STATE", "overnight_state.json")
STATS_DIR = os.getenv("STATS_DIR", "stats")
LOG_CSV = os.path.join(STATS_DIR, "overnight_log.csv")

OPEN_T, CLOSE_T = dtime(9, 30), dtime(16, 0)

# القائمة الافتراضية إن لم تُقرأ من حالة البوت (QQQ و SNDK تُضاف دائماً)
DEFAULT_STOCKS = ["TSLA", "META", "AMD", "NVDA", "MU", "AAPL", "MSFT", "SNDK", "QQQ",
                  "CRCL", "COIN", "CRWV", "QCOM", "AMZN", "BA", "IBM", "INTC", "ORCL",
                  "RDDT", "SPCX", "AVGO", "PLTR", "RKLB"]
MUST_HAVE = ["QQQ", "SNDK"]
PREF_KEYS = ("stocks", "symbols", "tickers", "watchlist", "watch", "list", "wl", "universe")
TICK = re.compile(r"^[A-Z][A-Z0-9.\-]{0,5}$")

DIR_MIN = 20            # أقل درجة لاعتبار الاتجاه CALL/PUT (غير ذلك محايد)
TOP_N = 3               # عدد المرشحين
EARN_DAYS = 3           # استبعاد السهم لو الأرباح خلال هذا العدد من الأيام
GAP_LOOKBACK = 40       # عدد الجلسات لحساب الفجوة المعتادة
CONFIRM_PCT = 0.2       # حركة بعد/قبل السوق (%) تُعتبر تأكيداً أو معاكسة

OPT_MIN_DTE, OPT_MAX_DTE = 1, 7   # العقد المرشح ينتهي بعد 1–7 أيام
OPT_MIN_OI = 50                   # أقل open interest (أو حجم 20)
OPT_MAX_SPREAD = 0.30             # أقصى سبريد نسبي
RISK_FREE = 0.04

WEEKDAY_AR = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
LOG_HEADER = ["date", "target_day", "symbol", "dir", "score", "picked", "earn",
              "alert_price", "exp_gap", "prev_close", "open", "p945", "hi15", "lo15",
              "mv_open", "mv945", "mfe_dir", "hit_open"]


# ───────────────────────── تيليجرام ─────────────────────────
def get_api():
    try:
        import bot as _b
        if getattr(_b, "API", None):
            return _b.API
    except Exception as e:
        print("bot import skipped:", e)
    return f"https://api.telegram.org/bot{os.getenv('BOT_TOKEN', '')}"


def get_chats():
    ids = []
    try:
        import bot as _b
        ids += [str(x) for x in (getattr(_b, "ALERT_IDS", []) or [])]
    except Exception:
        pass
    ids += [c.strip() for c in os.getenv("CHAT_ID", "").split(",") if c.strip()]
    out = []
    for i in ids:
        if i not in out:
            out.append(i)
    return out


def tg_send(text):
    api, chats = get_api(), get_chats()
    if not chats:
        print(text)
        return
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = line
        else:
            cur = cur + "\n" + line if cur else line
    if cur:
        chunks.append(cur)
    for cid in chats:
        for c in chunks:
            try:
                r = requests.post(f"{api}/sendMessage",
                                  data={"chat_id": cid, "text": c,
                                        "disable_web_page_preview": True}, timeout=20)
                if not r.ok:
                    print("telegram refused", cid, r.text[:200])
            except Exception as e:
                print("send error", cid, e)


# ───────────────────────── أدوات ─────────────────────────
def f2(x):
    return f"{x:,.2f}"


def num(x, default=0.0):
    try:
        v = float(x)
        return default if v != v else v
    except Exception:
        return default


def hm(dt, tz=NY):
    return dt.astimezone(tz).strftime("%H:%M")


def fmt_delay(sec):
    sec = max(sec, 0)
    return f"{sec:.0f} ث" if sec < 90 else f"{sec / 60:.1f} د"


def stamp(last_t):
    """نفس أسلوب ختم بوت SPX: الشمعة، وقت الإرسال، عمر البيانات."""
    sent = datetime.now(NY)
    end = last_t + timedelta(minutes=5)
    closed = end <= sent
    line = (f"🕯 آخر شمعة: {hm(last_t)} → {hm(end)} نيويورك "
            f"({LOCAL_NAME} {hm(last_t, LOCAL)}) — {'مغلقة' if closed else 'جارية'}")
    second = f"🕒 أُرسلت {sent.strftime('%H:%M:%S')} نيويورك ({LOCAL_NAME} {hm(sent, LOCAL)})"
    if closed:
        second += f" (بعد إغلاقها بـ {fmt_delay((sent - end).total_seconds())})"
    age = max((sent - last_t).total_seconds() / 60.0, 0.0)
    second += f" | عمر البيانات: {age:.1f} د " + ("✅" if age <= 6 else "⚠️ متأخرة")
    return line + "\n" + second


def ema(vals, n):
    k = 2.0 / (n + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def next_trading_day(d):
    n = d + timedelta(days=1)
    while n.weekday() >= 5:
        n += timedelta(days=1)
    return n


def to_date(x):
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    try:
        return datetime.fromisoformat(str(x)[:10]).date()
    except Exception:
        return None


# ───────────────────────── البيانات ─────────────────────────
def hist(sym, period, interval, prepost=False, tries=2):
    for _ in range(tries):
        try:
            df = yf.Ticker(sym).history(period=period, interval=interval,
                                        prepost=prepost, auto_adjust=False)
            if df is not None and len(df):
                df = df.dropna(subset=["Open", "High", "Low", "Close"])
                if len(df):
                    if df.index.tz is None:
                        df.index = df.index.tz_localize(NY)
                    else:
                        df.index = df.index.tz_convert(NY)
                    return df
        except Exception as e:
            print("hist error", sym, interval, e)
        time.sleep(1.5)
    return None


def session_bars(df, day):
    mask = [(ts.date() == day and OPEN_T <= ts.time() < CLOSE_T) for ts in df.index]
    return df[mask]


def earnings_soon(sym, today):
    """تاريخ الأرباح لو وقع خلال EARN_DAYS يوم، وإلا نص فارغ."""
    try:
        cal = yf.Ticker(sym).calendar
        vals = []
        if isinstance(cal, dict):
            v = cal.get("Earnings Date", [])
            vals = list(v) if isinstance(v, (list, tuple)) else [v]
        elif cal is not None and hasattr(cal, "loc"):
            vals = list(cal.loc["Earnings Date"].tolist())
        for x in vals:
            d = to_date(x)
            if d and today <= d <= today + timedelta(days=EARN_DAYS):
                return str(d)
    except Exception:
        pass
    return ""


def clean_list(items):
    out = []
    for x in items:
        x = str(x).strip().upper()
        if x and TICK.match(x) and x not in out:
            out.append(x)
    for m in MUST_HAVE:
        if m not in out:
            out.append(m)
    return out


def load_watchlist():
    env = os.getenv("OVERNIGHT_STOCKS", "").strip()
    if env:
        return clean_list(env.split(",")), "متغير OVERNIGHT_STOCKS"
    try:
        import bot as _b
        path = getattr(_b, "STATE", "state.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                st = json.load(fh)
            for k in PREF_KEYS:
                v = st.get(k)
                if isinstance(v, dict):
                    v = list(v.keys())
                if isinstance(v, list) and v and all(
                        isinstance(x, str) and TICK.match(x.strip().upper()) for x in v):
                    return clean_list(v), f"قائمة البوت ({k})"
    except Exception as e:
        print("watchlist from bot skipped:", e)
    return clean_list(DEFAULT_STOCKS), "القائمة الافتراضية"


# ───────────────────────── التحليل والدرجة ─────────────────────────
def market_ctx(now):
    d, m5 = hist("QQQ", "6mo", "1d"), hist("QQQ", "5d", "5m")
    if d is None or m5 is None:
        return None
    today = now.date()
    dd = d[[ts.date() < today for ts in d.index]]
    t5 = session_bars(m5, today)
    if len(dd) < 25 or len(t5) < 3:
        return None
    price, pc = float(t5["Close"].iloc[-1]), float(dd["Close"].iloc[-1])
    e20 = ema(list(dd["Close"]) + [price], 20)
    return {"chg": (price / pc - 1) * 100, "above": price > e20, "last_t": t5.index[-1]}


def analyze(sym, mkt, now):
    today = now.date()
    d, m5 = hist(sym, "6mo", "1d"), hist(sym, "5d", "5m")
    if d is None or m5 is None:
        return None
    dd = d[[ts.date() < today for ts in d.index]]
    t5 = session_bars(m5, today)
    if len(dd) < 25 or len(t5) < 8:
        return None
    h1 = hist(sym, "1mo", "60m")

    price = float(t5["Close"].iloc[-1])
    hi, lo = float(t5["High"].max()), float(t5["Low"].min())
    prev = dd.iloc[-1]
    phi, plo = float(prev["High"]), float(prev["Low"])

    cs = m5.tail(61)
    H, L, C = list(cs["High"]), list(cs["Low"]), list(cs["Close"])
    trs = [max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1]))
           for i in range(1, len(cs))]
    a5 = max(sum(trs) / len(trs) if trs else 0.01, 1e-6)

    tail = dd.tail(GAP_LOOKBACK + 1)
    op_, cl_ = list(tail["Open"]), list(tail["Close"])
    gaps = [op_[i] - cl_[i - 1] for i in range(1, len(tail))]
    exp_gap = sum(abs(g) for g in gaps) / len(gaps) if gaps else price * 0.01
    gap_pct = exp_gap / price * 100

    s, tags = 0.0, []
    # 1) موقع السعر من مدى اليوم
    pos = (price - lo) / (hi - lo) if hi > lo else 0.5
    s += (pos - 0.5) * 2 * 15
    if pos >= 0.8:
        tags.append("قرب قمة اليوم")
    elif pos <= 0.2:
        tags.append("قرب قاع اليوم")
    # 2) زخم آخر 30 دقيقة
    cl5 = list(t5["Close"])
    ref = cl5[-7] if len(cl5) >= 7 else cl5[0]
    mom = max(-1.5, min(1.5, (price - ref) / (a5 * math.sqrt(6))))
    s += mom / 1.5 * 15
    if mom > 0.6:
        tags.append("زخم 30د صاعد")
    elif mom < -0.6:
        tags.append("زخم 30د هابط")
    # 3) الاتجاه اليومي
    cd = list(dd["Close"])
    e_now, e_prev = ema(cd + [price], 20), ema(cd[:-3], 20)
    s += 7 if price > e_now else -7
    s += 5 if e_now > e_prev else -5
    tags.append("فوق EMA20" if price > e_now else "تحت EMA20")
    # 4) كسر هيكل الساعة
    if h1 is not None and len(h1) >= 15:
        hs, ls = list(h1["High"])[-13:-1], list(h1["Low"])[-13:-1]
        if price > max(hs):
            s += 15
            tags.append("كسر قمة 1س")
        elif price < min(ls):
            s -= 15
            tags.append("كسر قاع 1س")
    # 5) سحب السيولة (قاع/قمة أمس)
    if lo < plo < price:
        s += 15
        tags.append("سحب سيولة قاع أمس")
    if hi > phi > price:
        s -= 15
        tags.append("سحب سيولة قمة أمس")
    # 6) السوق (QQQ)
    if mkt and sym != "QQQ":
        s += max(-1.0, min(1.0, mkt["chg"] / 1.0)) * 7 + (3 if mkt["above"] else -3)

    score = int(round(max(-100, min(100, s))))
    direction = "CALL" if score >= DIR_MIN else "PUT" if score <= -DIR_MIN else "NEUTRAL"
    earn = "" if sym == "QQQ" else earnings_soon(sym, today)
    return {"sym": sym, "price": price, "score": score, "dir": direction,
            "exp_gap": exp_gap, "gap_pct": gap_pct, "tags": tags, "earn": earn,
            "last_t": t5.index[-1], "picked": False}


# ───────────────────────── العقود ─────────────────────────
def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(S, K, T, sig, call, r=RISK_FREE):
    if T <= 0 or sig <= 0:
        return max(S - K, 0.0) if call else max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    if call:
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)


def pick_contracts(sym, side, price, exp_move, now):
    """يرجع (قائمة عقود, None) أو (None, سبب الفشل)."""
    try:
        tk = yf.Ticker(sym)
        exps = list(tk.options or [])
    except Exception as e:
        return None, f"تعذر جلب العقود ({type(e).__name__})"
    chosen = None
    for e in exps:
        try:
            ed = datetime.strptime(e, "%Y-%m-%d").date()
        except Exception:
            continue
        if OPT_MIN_DTE <= (ed - now.date()).days <= OPT_MAX_DTE:
            chosen = (e, ed)
            break
    if not chosen:
        return None, "لا عقود بانتهاء 1–7 أيام"
    e, ed = chosen
    call = side == "CALL"
    try:
        ch = tk.option_chain(e)
        df = ch.calls if call else ch.puts
    except Exception as ex:
        return None, f"تعذر جلب سلسلة العقود ({type(ex).__name__})"

    rows = []
    for _, r in df.iterrows():
        K = num(r.get("strike"))
        if K <= 0 or abs(K - price) > price * 0.15:
            continue
        bid, ask, last = num(r.get("bid")), num(r.get("ask")), num(r.get("lastPrice"))
        if bid > 0 and ask >= bid:
            mid, spread, src = (bid + ask) / 2, (ask - bid) / ((bid + ask) / 2), "mid"
        elif last > 0:
            mid, spread, src = last, None, "last"
        else:
            continue
        oi, vol = num(r.get("openInterest")), num(r.get("volume"))
        if oi < OPT_MIN_OI and vol < 20:
            continue
        if spread is not None and spread > OPT_MAX_SPREAD:
            continue
        rows.append({"K": K, "mid": mid, "spread": spread, "src": src, "oi": oi,
                     "vol": vol, "iv": num(r.get("impliedVolatility"))})
    if not rows:
        return None, "لا عقد سائل بسبريد مقبول"

    sg = 1 if call else -1
    targets = {"safe": price - sg * 0.5 * exp_move, "mid": price,
               "bold": price + sg * 0.8 * exp_move}
    used, out = set(), []
    exp_dt = datetime.combine(ed, CLOSE_T, tzinfo=NY)
    open_next = datetime.combine(next_trading_day(now.date()), dtime(9, 35), tzinfo=NY)
    yr = 365 * 86400.0
    T = max((exp_dt - now).total_seconds(), 0) / yr
    T2 = max(T - (open_next - now).total_seconds() / yr, 1 / (365 * 24))
    for label, tgt in targets.items():
        for r in sorted(rows, key=lambda x: abs(x["K"] - tgt)):
            if r["K"] in used:
                continue
            used.add(r["K"])
            c = dict(r, label=label, side=side, ed=str(ed), dte=(ed - now.date()).days,
                     fav=None, adv=None, ratio=None)
            if 0.05 < r["iv"] < 3.0:
                fav = bs_price(price + sg * exp_move, r["K"], T2, r["iv"], call) / r["mid"] - 1
                adv = bs_price(price - sg * exp_move, r["K"], T2, r["iv"], call) / r["mid"] - 1
                c["fav"], c["adv"] = fav * 100, adv * 100
                c["ratio"] = fav / max(abs(adv), 1e-6) if adv < 0 else None
            out.append(c)
            break
    ranked = [c for c in out if c["ratio"] is not None]
    if ranked:
        max(ranked, key=lambda c: c["ratio"])["star"] = True
    return out, None


def contract_line(c, price, exp_move):
    nm = {"safe": "آمن", "mid": "متوازن", "bold": "جريء"}[c["label"]]
    itm = (c["K"] < price) if c["side"] == "CALL" else (c["K"] > price)
    money = f"{'داخل' if itm else 'خارج'} المال {abs(price - c['K']):.1f}$"
    px = f"${c['mid']:.2f}" + ("" if c["src"] == "mid" else " (آخر سعر)")
    sp = f" | سبريد {c['spread'] * 100:.0f}%" if c["spread"] is not None else ""
    line = (f"• {nm}{' ⭐' if c.get('star') else ''}: {c['side']} {c['K']:g} ({money}) — "
            f"{c['ed']} ({c['dte']} يوم) — {px}{sp}")
    if c["fav"] is not None:
        line += (f"\n   لو تحرك السهم {exp_move:.2f}$ لصالحك ≈ {c['fav']:+.0f}% | "
                 f"عكسك ≈ {c['adv']:+.0f}%")
    return line


# ───────────────────────── الحالة والسجل ─────────────────────────
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False)


def csv_append(rows):
    os.makedirs(STATS_DIR, exist_ok=True)
    new = not os.path.exists(LOG_CSV)
    with open(LOG_CSV, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(LOG_HEADER)
        for r in rows:
            w.writerow([r.get(h, "") for h in LOG_HEADER])


def csv_read():
    try:
        with open(LOG_CSV, encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))
    except FileNotFoundError:
        return []
    except Exception as e:
        print("csv read error", e)
        return []


ICON = {"CALL": "🟢", "PUT": "🔴", "NEUTRAL": "⚪"}


# ───────────────────────── المرحلة 1: التوقع والترشيح ─────────────────────────
def run_pick(now):
    syms, src = load_watchlist()
    mkt = market_ctx(now)
    res, failed = [], []
    for sym in syms:
        try:
            r = analyze(sym, mkt, now)
        except Exception as e:
            print("analyze error", sym, e)
            r = None
        if r:
            res.append(r)
        else:
            failed.append(sym)
        time.sleep(0.2)
    if len(res) < 3:
        tg_send("⚠️ توقعات الليل: بيانات غير كافية (أقل من 3 أسهم). "
                "ربما السوق مغلق أو Yahoo لا يرد؛ سيُعاد المحاولة في التشغيل القادم.")
        return False

    target = next_trading_day(now.date())
    tname = WEEKDAY_AR[target.weekday()]
    elig = [r for r in res if r["dir"] != "NEUTRAL" and not r["earn"]]
    elig.sort(key=lambda r: abs(r["score"]) * r["gap_pct"], reverse=True)
    picks = elig[:TOP_N]
    for r in picks:
        r["picked"] = True

    last_t = (mkt or {}).get("last_t") or res[0]["last_t"]
    order = sorted(res, key=lambda r: (bool(r["earn"]), -abs(r["score"])))
    L = [f"🌙 توقعات الليل — حركة {tname} {target}",
         f"الوقت: {now.strftime('%H:%M')} نيويورك ({LOCAL_NAME} {hm(now, LOCAL)}) | "
         f"القائمة: {src} ({len(res)} سهم)",
         "━━━━━━━━━━━━━━"]
    if mkt:
        L.append(f"QQQ اليوم: {mkt['chg']:+.2f}% — {'فوق' if mkt['above'] else 'تحت'} EMA20")
    L.append("")
    for r in order:
        if r["earn"]:
            L.append(f"⚠️ {r['sym']} — أرباح بتاريخ {r['earn']} (مستبعد من الترشيح)")
            continue
        mark = "🏆 " if r["picked"] else ""
        tg = "، ".join(r["tags"][:3])
        L.append(f"{mark}{ICON[r['dir']]} {r['sym']} {r['score']:+d} "
                 f"{'' if r['dir'] == 'NEUTRAL' else r['dir']} | {f2(r['price'])} | "
                 f"فجوة معتادة {r['exp_gap']:.2f}$ ({r['gap_pct']:.1f}%) — {tg}")
    if failed:
        L += ["", "بدون بيانات: " + "، ".join(failed)]
    L += ["", stamp(last_t)]
    tg_send("\n".join(L))

    # الرسالة الثانية: المرشحون والعقود
    P = [f"🏆 أفضل {len(picks)} لحركة {tname}", "━━━━━━━━━━━━━━"]
    if not picks:
        P.append("لا توجد إشارات كافية اليوم (كلها محايدة أو قريبة من أرباح). الأفضل الانتظار.")
    elif len(picks) < TOP_N:
        P.append(f"وُجد {len(picks)} فقط بإشارة واضحة؛ لا أُكمل العدد بإشارات ضعيفة.")
    for i, r in enumerate(picks, 1):
        P += ["", f"{i}) {ICON[r['dir']]} {r['sym']} — {r['dir']} | الدرجة {r['score']:+d} | "
                  f"السعر {f2(r['price'])}",
              "الأسباب: " + "، ".join(r["tags"]),
              f"الفجوة المعتادة: {r['exp_gap']:.2f}$ ({r['gap_pct']:.1f}%)"]
        cons, why = pick_contracts(r["sym"], r["dir"], r["price"], r["exp_gap"], now)
        if cons:
            P.append("🎯 العقود المرشحة:")
            P += [contract_line(c, r["price"], r["exp_gap"]) for c in cons]
        else:
            P.append(f"🎯 العقد: غير متاح — {why}")
        time.sleep(0.3)
    P += ["", "ℹ️ تقدير الربح/الخسارة محسوب بنموذج Black-Scholes من IV الحالي ويفترض ثبات IV؛ "
              "قد يختلف سعرك الفعلي صباحاً. البيانات متأخرة وسعر العقد تقريبي؛ أكّده في منصتك.",
          "⚠️ توقع إحصائي بسيط للمساعدة وليس نصيحة مالية. الدقة تُقاس في /stats ولا يُحكم "
          "عليها قبل 30 يوم تداول."]
    tg_send("\n".join(P))

    st = {"alert_day": str(now.date()), "target_day": str(target),
          "alert_time": now.isoformat(), "done": {"pick": str(now.date())},
          "stocks": {r["sym"]: {"price": r["price"], "score": r["score"], "dir": r["dir"],
                                "exp_gap": r["exp_gap"], "gap_pct": r["gap_pct"],
                                "earn": bool(r["earn"]), "picked": r["picked"],
                                "tags": r["tags"]} for r in res}}
    save_state(st)
    return True


# ───────────────────────── المرحلتان 2 و3: بعد الإغلاق / قبل الافتتاح ─────────────────────────
def run_ext(st, now, kind):
    ref_day = date.fromisoformat(st["alert_day"])
    title = ("🌆 فحص ما بعد الإغلاق" if kind == "after" else "🌅 فحص ما قبل الافتتاح")
    rows, last_t = [], None
    for sym, v in st["stocks"].items():
        df = hist(sym, "5d", "5m", prepost=True)
        time.sleep(0.2)
        if df is None:
            continue
        reg = [ts for ts in df.index if ts.date() == ref_day and OPEN_T <= ts.time() < CLOSE_T]
        if not reg or df.index[-1] <= reg[-1]:
            continue
        ref = float(df.loc[reg[-1], "Close"])
        px = float(df["Close"].iloc[-1])
        last_t = df.index[-1]
        pct = (px / ref - 1) * 100
        if v["dir"] == "NEUTRAL":
            verdict = "⚪"
        elif (v["dir"] == "CALL" and pct >= CONFIRM_PCT) or (v["dir"] == "PUT" and pct <= -CONFIRM_PCT):
            verdict = "✅ يؤكد"
        elif (v["dir"] == "CALL" and pct <= -CONFIRM_PCT) or (v["dir"] == "PUT" and pct >= CONFIRM_PCT):
            verdict = "❌ يعاكس"
        else:
            verdict = "⚪ هادئ"
        rows.append({"sym": sym, "v": v, "px": px, "d": px - ref, "pct": pct, "verdict": verdict})
    if not rows:
        tg_send(f"{title}\nلا توجد بيانات تداول ممتد متاحة الآن من المصدر المجاني.")
        return
    rows.sort(key=lambda r: (not r["v"]["picked"], -abs(r["pct"])))
    L = [f"{title} — توقع حركة {WEEKDAY_AR[date.fromisoformat(st['target_day']).weekday()]}",
         "━━━━━━━━━━━━━━"]
    for r in rows:
        v = r["v"]
        tagx = "⚠️ أرباح " if v["earn"] else ""
        L.append(f"{'🏆 ' if v['picked'] else ''}{tagx}{ICON[v['dir']]} {r['sym']} "
                 f"{'' if v['dir'] == 'NEUTRAL' else v['dir']} | {r['d']:+.2f}$ ({r['pct']:+.2f}%) "
                 f"| {r['verdict']}")
    ok = sum(1 for r in rows if r["verdict"].startswith("✅"))
    bad = sum(1 for r in rows if r["verdict"].startswith("❌"))
    L += ["", f"المؤكد {ok} | المعاكس {bad} (من الأسهم ذات الاتجاه)",
          "حد التأكيد: ±" + f"{CONFIRM_PCT}%" + " عن إغلاق الجلسة النظامية.", ""]
    if last_t is not None:
        L.append(stamp(last_t))
    tg_send("\n".join(L))


# ───────────────────────── المرحلة 4: النتائج ─────────────────────────
def run_result(st, now):
    today = now.date()
    if today <= date.fromisoformat(st["alert_day"]):
        print("لم يحن يوم النتائج بعد")
        return False
    rows, last_t = [], None
    for sym, v in st["stocks"].items():
        df = hist(sym, "5d", "5m")
        time.sleep(0.2)
        if df is None:
            continue
        bars = session_bars(df, today)
        prevb = [ts for ts in df.index if ts.date() < today and OPEN_T <= ts.time() < CLOSE_T]
        if not len(bars) or not prevb:
            continue
        pc = float(df.loc[prevb[-1], "Close"])
        first = bars.iloc[:3]
        op, p15 = float(first["Open"].iloc[0]), float(first["Close"].iloc[-1])
        hi, lo = float(first["High"].max()), float(first["Low"].min())
        ap = v["price"]
        sg = 1 if v["dir"] == "CALL" else -1 if v["dir"] == "PUT" else 0
        mv_open, mv15 = op - ap, p15 - ap
        if sg:
            mfe = (hi - ap) if sg > 0 else (ap - lo)
            hit = 1 if sg * mv_open > 0 else 0
        else:
            mfe, hit = max(hi - ap, ap - lo), ""
        last_t = first.index[-1]
        rows.append({"sym": sym, "v": v, "ap": ap, "pc": pc, "op": op, "p15": p15,
                     "hi": hi, "lo": lo, "mv_open": mv_open, "mv15": mv15,
                     "mfe": mfe, "hit": hit, "sg": sg, "n": len(first)})
    if not rows:
        print("لا بيانات افتتاح بعد")
        return False

    csv_append([{"date": st["alert_day"], "target_day": st["target_day"], "symbol": r["sym"],
                 "dir": r["v"]["dir"], "score": r["v"]["score"],
                 "picked": 1 if r["v"]["picked"] else 0, "earn": 1 if r["v"]["earn"] else 0,
                 "alert_price": f"{r['ap']:.2f}", "exp_gap": f"{r['v']['exp_gap']:.2f}",
                 "prev_close": f"{r['pc']:.2f}", "open": f"{r['op']:.2f}",
                 "p945": f"{r['p15']:.2f}", "hi15": f"{r['hi']:.2f}", "lo15": f"{r['lo']:.2f}",
                 "mv_open": f"{r['mv_open']:.2f}", "mv945": f"{r['mv15']:.2f}",
                 "mfe_dir": f"{r['mfe']:.2f}", "hit_open": r["hit"]} for r in rows])

    L = [f"📋 نتائج الليل — {st['target_day']} (التوقع من {st['alert_day']})",
         "كل الأرقام بالدولار من سعر وقت التنبيه (3:30 م)", "━━━━━━━━━━━━━━"]
    for r in sorted(rows, key=lambda r: -abs(r["mv_open"])):
        v = r["v"]
        if v["earn"]:
            mark = "⚠️"
        elif r["sg"] == 0:
            mark = "⚪"
        else:
            mark = "✅" if r["hit"] == 1 else "❌"
        L.append(f"{'🏆' if v['picked'] else '  '}{ICON[v['dir']]} {r['sym']} {mark} | "
                 f"الافتتاح {r['mv_open']:+.2f}$ (فجوة {r['op'] - r['pc']:+.2f}$) | "
                 f"9:45 {r['mv15']:+.2f}$ | أعلى {r['hi'] - r['ap']:+.2f}$ أدنى {r['lo'] - r['ap']:+.2f}$")
    L.append("")
    best_any = max(rows, key=lambda r: max(r["hi"] - r["ap"], r["ap"] - r["lo"]))
    up_ex, dn_ex = best_any["hi"] - best_any["ap"], best_any["ap"] - best_any["lo"]
    L.append(f"🥇 الأكبر حركة (أي اتجاه): {best_any['sym']} "
             f"({'صعود' if up_ex >= dn_ex else 'هبوط'} حتى {max(up_ex, dn_ex):.2f}$ خلال أول 15 د)")
    dirs = [r for r in rows if r["sg"] and not r["v"]["earn"]]
    if dirs:
        bp = max(dirs, key=lambda r: r["mfe"])
        if bp["mfe"] > 0:
            L.append(f"🎯 الأفضل مع توقعنا: {bp['sym']} ({bp['v']['dir']}) أقصى {bp['mfe']:+.2f}$ لصالحك")
        else:
            L.append("🎯 لا سهم تحرك لصالح التوقع خلال أول 15 دقيقة.")
    pk = [r for r in rows if r["v"]["picked"]]
    if pk:
        L += ["", "🏆 المرشحون الثلاثة:"]
        for r in pk:
            L.append(f"• {r['sym']} {r['v']['dir']}: عند الافتتاح "
                     f"{r['sg'] * r['mv_open']:+.2f}$ لصالحك | أقصى {r['mfe']:+.2f}$ "
                     f"{'✅' if r['hit'] == 1 else '❌'}")
        best_pick = max(pk, key=lambda r: r["mfe"])
        if best_pick["mfe"] > 0:
            L.append(f"الأفضل بين المرشحين: {best_pick['sym']}")
        else:
            L.append("لا مرشح تحرك لصالحه اليوم.")
    hits = [r for r in dirs if r["hit"] == 1]
    L += ["", f"📊 اليوم: اتجاه صحيح عند الافتتاح {len(hits)} من {len(dirs)}"
              + (f" | المرشحون {sum(1 for r in pk if r['hit'] == 1)} من {len(pk)}" if pk else ""),
          stats_short()]
    if last_t is not None:
        L += ["", stamp(last_t)]
    tg_send("\n".join(L))
    return True


# ───────────────────────── الإحصائيات ─────────────────────────
def _hit_rate(rows):
    r = [x for x in rows if x.get("hit_open") in ("0", "1", 0, 1)]
    h = sum(1 for x in r if str(x["hit_open"]) == "1")
    return h, len(r)


def stats_short():
    rows = csv_read()
    if not rows:
        return "📈 لا سجل تراكمي بعد."
    dirs = [r for r in rows if r["dir"] in ("CALL", "PUT") and r["earn"] == "0"]
    h, n = _hit_rate(dirs)
    days = len({r["date"] for r in rows})
    return f"📈 التراكمي ({days} يوم): اتجاه صحيح {h} من {n}" + (f" ({h / n * 100:.0f}%)" if n else "")


def stats_text():
    rows = csv_read()
    L = ["📈 إحصائيات توقعات الليل", "━━━━━━━━━━━━━━"]
    if not rows:
        L.append("لا سجل بعد؛ تُسجَّل النتائج صباح كل يوم تداول بعد التوقع.")
        return "\n".join(L)
    days = len({r["date"] for r in rows})
    dirs = [r for r in rows if r["dir"] in ("CALL", "PUT") and r["earn"] == "0"]
    pk = [r for r in dirs if r["picked"] == "1"]
    h, n = _hit_rate(dirs)
    hp, npk = _hit_rate(pk)
    L.append(f"الأيام: {days}" + ("  ⚠️ عينة صغيرة (أقل من 30)" if days < 30 else ""))
    if n:
        L.append(f"كل الإشارات: اتجاه صحيح {h} من {n} ({h / n * 100:.0f}%)")
    if npk:
        L.append(f"المرشحون: {hp} من {npk} ({hp / npk * 100:.0f}%)")
    for lo_, hi_ in ((20, 39), (40, 59), (60, 100)):
        b = [r for r in dirs if lo_ <= abs(int(float(r["score"]))) <= hi_]
        hb, nb = _hit_rate(b)
        if nb:
            L.append(f"درجة {lo_}–{hi_}: {hb} من {nb} ({hb / nb * 100:.0f}%)")
    for sd, nm in (("CALL", "CALL"), ("PUT", "PUT")):
        b = [r for r in dirs if r["dir"] == sd]
        hb, nb = _hit_rate(b)
        if nb:
            L.append(f"{nm}: {hb} من {nb} ({hb / nb * 100:.0f}%)")
    try:
        mp = [float(r["mfe_dir"]) for r in pk]
        mo = [float(r["mfe_dir"]) for r in dirs if r["picked"] != "1"]
        if mp and mo:
            L.append(f"متوسط أقصى حركة لصالحك (15 د): المرشحون {sum(mp) / len(mp):.2f}$ "
                     f"| غيرهم {sum(mo) / len(mo):.2f}$")
    except Exception:
        pass
    per = {}
    for r in dirs:
        d = per.setdefault(r["symbol"], [0, 0])
        if r["hit_open"] in ("0", "1"):
            d[1] += 1
            d[0] += r["hit_open"] == "1"
    best = sorted(((k, v) for k, v in per.items() if v[1] >= 3),
                  key=lambda kv: -kv[1][0] / kv[1][1])
    if best:
        L.append("حسب السهم (3 إشارات فأكثر): " + " | ".join(
            f"{k} {v[0]}/{v[1]}" for k, v in best[:8]))
    return "\n".join(L)


# ───────────────────────── التشغيل ─────────────────────────
WINDOWS = {"pick": (dtime(15, 20), dtime(15, 59)), "after": (dtime(16, 15), dtime(18, 0)),
           "pre": (dtime(8, 55), dtime(9, 28)), "result": (dtime(9, 46), dtime(11, 0))}


def detect_phase(now):
    if now.weekday() >= 5:
        return None
    for ph, (a, b) in WINDOWS.items():
        if a <= now.time() <= b:
            return ph
    return None


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    now = datetime.now(NY)
    if mode == "stats":
        tg_send(stats_text())
        return
    forced = mode != "auto"
    if not forced:
        mode = detect_phase(now)
        if not mode:
            print("خارج نوافذ التشغيل؛ إنهاء.")
            return
    st = load_state()
    done = st.get("done", {})
    try:
        if mode == "pick":
            if not forced and done.get("pick") == str(now.date()):
                print("تم الترشيح اليوم")
                return
            run_pick(now)
        elif mode in ("after", "pre"):
            if not st.get("stocks"):
                print("لا توقع محفوظ")
                return
            if not forced:
                if done.get(mode) == str(now.date()):
                    print("تم الفحص")
                    return
                if mode == "after" and st["alert_day"] != str(now.date()):
                    return
                if mode == "pre" and str(now.date()) <= st["alert_day"]:
                    return
            run_ext(st, now, mode)
            st.setdefault("done", {})[mode] = str(now.date())
            save_state(st)
        elif mode == "result":
            if not st.get("stocks"):
                print("لا توقع محفوظ")
                return
            if not forced and done.get("result") == str(now.date()):
                print("تم تسجيل النتائج")
                return
            if run_result(st, now):
                st.setdefault("done", {})["result"] = str(now.date())
                save_state(st)
        else:
            print("مرحلة غير معروفة:", mode)
    except Exception as e:
        print("overnight error:", repr(e))
        tg_send(f"⚠️ عطل في توقعات الليل ({mode})\nالنوع: {type(e).__name__}\n"
                f"التفاصيل: {str(e)[:300]}")


if __name__ == "__main__":
    main()
