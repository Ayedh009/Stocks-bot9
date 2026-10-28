# -*- coding: utf-8 -*-
"""
spx_logic.py — منطق السباكس (أوردربلوك + فجوات FVG + خط الكنترول + إشارات CALL/PUT)
مطبّق على كل أسهم بوت الأسهم، على فريم 5 دقائق.

- إشارة CALL/PUT عند ارتداد مؤكد (إغلاق شمعة) من أوردربلوك أو فجوة
- دخول + وقف + هدفان، ومتابعة الصفقة (TP1 / TP2 / وقف) برسائل قصيرة
- خط الكنترول = متوسط (VWAP الجلسة ، منتصف مدى اليوم) يظهر في الإشارة وفي /spx
- أمر /spx TSLA : لقطة فورية للسهم
لا يحتاج مكتبات جديدة (yfinance و pandas موجودتان).
"""
import os
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

NY = ZoneInfo("America/New_York")
LOCAL = ZoneInfo(os.getenv("LOCAL_TZ", "America/Chicago"))
LOCAL_NAME = os.getenv("LOCAL_TZ_NAME", "شيكاغو")

OPEN, CLOSE = dtime(9, 30), dtime(16, 0)
SIGNAL_START = dtime(9, 45)      # لا إشارات قبلها (شموع الافتتاح عشوائية)
NO_NEW_AFTER = dtime(15, 30)     # لا إشارات جديدة بعدها
MAX_SIGNALS_PER_SYMBOL = 2       # أقصى عدد إشارات للسهم الواحد يومياً
COOLDOWN_MIN = 30                # أقل فاصل بين إشارتين بنفس الاتجاه (لنفس السهم)
MAX_AGE_MIN = 25                 # لا نرسل إشارة شمعتها أقدم من هذا


def _now():
    return datetime.now(NY)


@dataclass
class Candle:
    t: datetime   # وقت فتح الشمعة بتوقيت نيويورك
    o: float
    h: float
    l: float
    c: float
    v: float


# ───────────────────────── جلب البيانات (طلب واحد لكل الأسهم) ─────────────────────────
_CACHE = {}


def _to_candles(df):
    idx = df.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    idx = idx.tz_convert(NY)
    out = []
    for t, o, h, l, c, v in zip(idx, df["Open"], df["High"], df["Low"],
                                df["Close"], df["Volume"]):
        if pd.isna(o) or pd.isna(h) or pd.isna(l) or pd.isna(c):
            continue
        out.append(Candle(t.to_pydatetime(), float(o), float(h), float(l),
                          float(c), 0.0 if pd.isna(v) else float(v)))
    return out


def _fetch(symbols):
    need = [x for x in symbols if x not in _CACHE]
    if not need:
        return
    d = None
    try:
        d = yf.download(need, period="5d", interval="5m", auto_adjust=True,
                        progress=False, group_by="ticker", threads=True)
    except Exception as e:
        print("spx fetch error:", e)
    for sym in need:
        _CACHE[sym] = []
        if d is None or d.empty:
            continue
        try:
            df = d[sym] if isinstance(d.columns, pd.MultiIndex) else d
            df = df[["Open", "High", "Low", "Close", "Volume"]]
            df = df.dropna(subset=["Open", "High", "Low", "Close"])
            _CACHE[sym] = _to_candles(df)
        except Exception as e:
            print(sym, "spx data error:", e)


# ───────────────────────── أدوات حسابية ─────────────────────────
def atr(cs, n=14):
    if len(cs) < 2:
        return 0.0
    trs = [max(cs[i].h - cs[i].l, abs(cs[i].h - cs[i - 1].c),
               abs(cs[i].l - cs[i - 1].c)) for i in range(1, len(cs))][-n:]
    return sum(trs) / len(trs)


def _ema(vals, n):
    k = 2.0 / (n + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def htf_bias(closes):
    """+1 صاعد، -1 هابط، 0 محايد (من إغلاقات الساعة)"""
    if len(closes) < 30:
        return 0
    e_now, e_prev = _ema(closes, 20), _ema(closes[:-3], 20)
    if closes[-1] > e_now and e_now > e_prev:
        return 1
    if closes[-1] < e_now and e_now < e_prev:
        return -1
    return 0


def _bias_from(d1):
    try:
        return htf_bias([float(x) for x in d1["Close"].tolist()])
    except Exception:
        return 0


def bias_text(b):
    return {1: "صاعد 🟢", -1: "هابط 🔴", 0: "محايد ⚪"}[b]


def control_series(cs):
    """خط الكنترول = متوسط (VWAP الجلسة ، منتصف مدى اليوم المتراكم)"""
    out, vw_list = [], []
    pv = vol = 0.0
    hi, lo = -1e18, 1e18
    for c in cs:
        tp = (c.h + c.l + c.c) / 3.0
        w = c.v if c.v > 0 else 1.0
        pv += tp * w
        vol += w
        hi, lo = max(hi, c.h), min(lo, c.l)
        vw = pv / vol
        vw_list.append(vw)
        out.append((vw + (hi + lo) / 2.0) / 2.0)
    return out, (vw_list[-1] if vw_list else 0.0)


@dataclass
class Zone:
    kind: str    # OB_D طلب، OB_S عرض، FVG_B صاعدة، FVG_S هابطة
    lo: float
    hi: float
    t: datetime

    @property
    def id(self):
        return f"{self.kind}-{int(self.t.timestamp())}-{self.lo:.2f}"

    @property
    def bullish(self):
        return self.kind in ("OB_D", "FVG_B")

    @property
    def name(self):
        return {"OB_D": "أوردربلوك طلب", "OB_S": "أوردربلوك عرض",
                "FVG_B": "فجوة سعرية صاعدة", "FVG_S": "فجوة سعرية هابطة"}[self.kind]


def find_obs(cs, a):
    d, s = [], []
    for i in range(len(cs) - 1):
        x, y = cs[i], cs[i + 1]
        later = cs[i + 2:]
        if x.c < x.o and y.c > y.o and (y.c - y.o) >= 1.2 * a and y.c > x.h:
            if not any(z.c < x.l for z in later):          # لم يُكسر
                d.append(Zone("OB_D", x.l, x.h, x.t))
        if x.c > x.o and y.c < y.o and (y.o - y.c) >= 1.2 * a and y.c < x.l:
            if not any(z.c > x.h for z in later):
                s.append(Zone("OB_S", x.l, x.h, x.t))
    return d[-4:] + s[-4:]


def find_fvgs(cs, a):
    out = []
    for i in range(2, len(cs)):
        c0, c2 = cs[i - 2], cs[i]
        later = cs[i + 1:]
        if c2.l > c0.h and (c2.l - c0.h) >= 0.2 * a:
            if not any(z.l <= c0.h for z in later):         # لم تُملأ
                out.append(Zone("FVG_B", c0.h, c2.l, c2.t))
        if c2.h < c0.l and (c0.l - c2.h) >= 0.2 * a:
            if not any(z.h >= c0.l for z in later):
                out.append(Zone("FVG_S", c2.h, c0.l, c2.t))
    return out[-6:]


def _f(x):
    return f"{x:,.2f}"


def _stamp(t, label="شمعة"):
    end = t + timedelta(minutes=5)
    sent = _now()
    state = "مغلقة" if end <= sent else "جارية"
    return (f"🕯 {label}: {t:%H:%M} → {end:%H:%M} نيويورك "
            f"({LOCAL_NAME} {t.astimezone(LOCAL):%H:%M}) — {state}\n"
            f"🕒 أُرسلت {sent:%H:%M:%S}")


# ───────────────────────── لقطة السهم ─────────────────────────
def _snap(sym):
    cs = _CACHE.get(sym) or []
    now = _now()
    sess = [c for c in cs if OPEN <= c.t.time() < CLOSE]
    today = [c for c in sess if c.t.date() == now.date()]
    closed = [c for c in sess if c.t + timedelta(minutes=5) <= now]
    if len(today) < 3 or len(closed) < 20:
        return None
    ctrl, vwap = control_series(today)
    a = atr(closed[-60:]) or 0.01
    ns = SimpleNamespace(
        now=now, sess=sess, today=today, closed=closed,
        closed_today=[c for c in closed if c.t.date() == now.date()],
        price=sess[-1].c, last_t=sess[-1].t, atr5=a,
        control=ctrl[-1], vwap=vwap,
        day_high=max(c.h for c in today), day_low=min(c.l for c in today))
    zc = closed[-300:]
    ns.zones = find_obs(zc, a) + find_fvgs(zc, a)
    return ns


# ───────────────────────── حالة السهم (تُحفظ في state.json) ─────────────────────────
def _state(s, sym, now):
    root = s.setdefault("spx", {})
    st = root.get(sym)
    day = str(now.date())
    if not st or st.get("day") != day:
        st = {"day": day, "count": 0, "signaled": [], "last": {}, "trades": []}
        root[sym] = st
    return st


# ───────────────────────── الإشارات ─────────────────────────
def _open(sym, side, z, k, ns, st, bias):
    a = ns.atr5
    i = ns.closed.index(k)
    recent = ns.closed[max(0, i - 2): i + 1]
    entry = k.c
    if side == "CALL":
        sl = min(z.lo, min(c.l for c in recent)) - 0.1 * a
        sl = max(sl, entry - 3.0 * a)
        tp1, tp2 = entry + 2.0 * a, entry + 3.6 * a
    else:
        sl = max(z.hi, max(c.h for c in recent)) + 0.1 * a
        sl = min(sl, entry + 3.0 * a)
        tp1, tp2 = entry - 2.0 * a, entry - 3.6 * a

    st["signaled"].append(z.id)
    st["last"][side] = k.t.timestamp()
    st["count"] += 1
    st["trades"].append({"side": side, "entry": entry, "sl": sl, "tp1": tp1,
                         "tp2": tp2, "from": k.t.timestamp(), "upto": k.t.timestamp(),
                         "tp1_hit": False, "tp1_ts": 0, "done": False})

    htf = bias if side == "CALL" else -bias
    htf_txt = ("متوافقة مع الفريم الكبير (1س) ✅" if htf > 0 else
               "مخالفة للفريم الكبير (1س) ⚠️ صفقة ارتداد" if htf < 0 else
               "الفريم الكبير (1س) محايد")
    above = ns.price > ns.control
    ctrl_ok = (above and side == "CALL") or ((not above) and side == "PUT")
    emoji = "🟢" if side == "CALL" else "🔴"
    return (
        f"🚨 {sym} — إشارة {side} {emoji}\n━━━━━━━━━━━━━━\n"
        f"السبب: ارتداد من {z.name} ({_f(z.lo)} – {_f(z.hi)}) مع إغلاق شمعة مؤكد\n"
        f"سعر الدخول: {_f(entry)}\nوقف الخسارة: {_f(sl)}\n"
        f"الهدف الأول TP1: {_f(tp1)}\nالهدف الثاني TP2: {_f(tp2)}\n"
        f"السعر {'فوق' if above else 'تحت'} خط الكنترول ({_f(ns.control)}) "
        f"{'✅' if ctrl_ok else '⚠️'}\n"
        f"{htf_txt}\n\n"
        f"{_stamp(k.t, 'شمعة التأكيد')}\n"
        f"💱 السعر الآن: {_f(ns.price)} ({ns.price - entry:+.2f} عن الدخول)\n\n"
        "⚠️ إشارة آلية للمساعدة وليست نصيحة مالية؛ التزم بإدارة المخاطر."
    )


def _signals(sym, ns, st, bias, cid, send):
    t = ns.now.time()
    if t < SIGNAL_START or t > NO_NEW_AFTER:
        return
    for k in ns.closed_today[-2:]:        # آخر شمعتين مغلقتين (تعويض تأخر الجدولة)
        age = (ns.now - (k.t + timedelta(minutes=5))).total_seconds() / 60.0
        if age > MAX_AGE_MIN or st["count"] >= MAX_SIGNALS_PER_SYMBOL:
            continue
        for z in sorted(ns.zones, key=lambda z: abs(k.c - (z.lo + z.hi) / 2)):
            if z.id in st["signaled"] or z.t >= k.t:
                continue
            mid = (z.lo + z.hi) / 2
            if z.bullish and k.l <= z.hi and k.c > k.o and k.c > mid:
                side = "CALL"
            elif (not z.bullish) and k.h >= z.lo and k.c < k.o and k.c < mid:
                side = "PUT"
            else:
                continue
            last = st["last"].get(side)
            if last and k.t.timestamp() - last < COOLDOWN_MIN * 60:
                continue
            send(cid, _open(sym, side, z, k, ns, st, bias))
            break


# ───────────────────────── متابعة الصفقات ─────────────────────────
def _track(sym, ns, st, cid, send):
    for tr in st["trades"]:
        if tr["done"]:
            continue
        long = tr["side"] == "CALL"
        for c in ns.sess:
            ts = c.t.timestamp()
            if ts <= tr["upto"]:
                continue
            hit_sl = c.l <= tr["sl"] if long else c.h >= tr["sl"]
            hit1 = c.h >= tr["tp1"] if long else c.l <= tr["tp1"]
            hit2 = c.h >= tr["tp2"] if long else c.l <= tr["tp2"]
            msg = None
            if not tr["tp1_hit"]:
                if hit_sl:
                    tr["done"] = True
                    note = "\n(الشمعة لمست الهدف والوقف معاً — احتُسب الوقف)" if hit1 else ""
                    msg = (f"🛑 {sym}: ضُرب وقف الخسارة لصفقة {tr['side']}\n"
                           f"الدخول: {_f(tr['entry'])} | الوقف: {_f(tr['sl'])}{note}")
                elif hit2:
                    tr["done"] = True
                    msg = (f"🎯🎯 {sym}: تحقق الهدف الثاني لصفقة {tr['side']} ✅\n"
                           f"TP2: {_f(tr['tp2'])} — الصفقة مكتملة.")
                elif hit1:
                    tr["tp1_hit"], tr["tp1_ts"] = True, ts
                    tr["sl"] = tr["entry"]
                    msg = (f"🎯 {sym}: تحقق الهدف الأول لصفقة {tr['side']} ✅\n"
                           f"TP1: {_f(tr['tp1'])} — يُنصح بنقل الوقف إلى سعر الدخول.")
            else:
                if hit_sl and ts > tr["tp1_ts"]:
                    tr["done"] = True
                    msg = f"↩️ {sym}: أُغلقت صفقة {tr['side']} عند الدخول بعد تحقق الهدف الأول."
                elif hit2:
                    tr["done"] = True
                    msg = (f"🎯🎯 {sym}: تحقق الهدف الثاني لصفقة {tr['side']} ✅\n"
                           f"TP2: {_f(tr['tp2'])} — الصفقة مكتملة.")
            if msg:
                send(cid, msg + "\n" + _stamp(c.t, "عند شمعة"))
            if c.t + timedelta(minutes=5) <= ns.now:
                tr["upto"] = ts           # الشمعة الجارية تُعاد قراءتها لاحقاً
            if tr["done"]:
                break


# ───────────────────────── الواجهة لبوت الأسهم ─────────────────────────
def spx_process(sym, s, d1, send):
    """تُستدعى لكل سهم داخل process(). send(chat_id, text)"""
    cid = s.get("chat_id")
    if not cid:
        return
    try:
        _fetch(s.get("symbols") or [sym])
        ns = _snap(sym)
        if ns is None:
            return
        st = _state(s, sym, ns.now)
        _track(sym, ns, st, cid, send)
        _signals(sym, ns, st, _bias_from(d1), cid, send)
    except Exception as e:
        print(sym, "spx error:", e)


def spx_report(sym, d1=None):
    """لقطة فورية لأمر /spx SYM"""
    _fetch([sym])
    ns = _snap(sym)
    if ns is None:
        return f"{sym}: لا توجد بيانات جلسة اليوم (السوق مغلق أو لا توجد شموع كافية)."
    above = ns.price > ns.control
    L = [f"📈 {sym} — منطق السباكس (5 دقائق)",
         f"السعر: {_f(ns.price)}",
         f"السعر {'فوق' if above else 'تحت'} خط الكنترول ({_f(ns.control)}) | VWAP {_f(ns.vwap)}",
         f"قمة اليوم {_f(ns.day_high)} | قاع اليوم {_f(ns.day_low)}",
         f"ATR (5 دقائق): {_f(ns.atr5)}"]
    if d1 is not None:
        L.append(f"الفريم الكبير (1س): {bias_text(_bias_from(d1))}")
    zs = sorted(ns.zones, key=lambda z: abs(ns.price - (z.lo + z.hi) / 2))[:3]
    if zs:
        L.append("\nأقرب المناطق:")
        for z in zs:
            L.append(f"• {z.name}: {_f(z.lo)} – {_f(z.hi)} "
                     f"(يبعد {abs(ns.price - (z.lo + z.hi) / 2):.2f})")
    else:
        L.append("\nلا توجد مناطق نشطة قريبة.")
    L.append("\n" + _stamp(ns.last_t, "آخر شمعة في البيانات"))
    return "\n".join(L)
