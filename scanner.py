# pip install yfinance pandas
import pandas as pd
import yfinance as yf

SYMBOLS = ["TSLA", "META", "AMD", "NVDA", "MU", "AAPL", "MSFT"]
L = R = 3            # طول البيفوت (يسار / يمين)
IFVG_LOOKBACK = 40   # كم شمعة نرجع للخلف لنبحث عن IFVG قبل الكسر
IFVG_TF = "1H"       # "1H" أو "4H" أو "BOTH"
TARGET_TF = "4H"     # فريم الأهداف: "1H" أو "4H"
NEAR_USD = 2.0       # نطاق تنبيه الاقتراب بالدولار
MAX_DAYS = 3         # انتهاء الصفقة بعد 3 أيام تداول

STAGE_TXT = {
    "broke": "🔔 كسر — بانتظار إعادة اختبار",
    "retest": "🔁 إعادة اختبار تمت",
    "t1": "🎯 الهدف 1 تحقق",
    "t2": "🏁 الهدف 2 تحقق — انتهت الصفقة",
    "stopped": "🛑 ألغيت الصفقة (كسر وقف 2)",
    "chase": "🚫 لا تطارد — وصل الهدف 1 بدون إعادة اختبار",
    "expired": "⌛ انتهت (3 أيام تداول)",
}
TERMINAL = {"t2", "stopped", "chase", "expired"}


def load(symbol):
    df = yf.download(symbol, period="60d", interval="60m",
                     auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    h4 = df.resample("4h", offset="9h30min").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last",
         "Volume": "sum"}).dropna()
    return df, h4


def analyze(df):
    o, h, l, c = (df[k].values for k in ["Open", "High", "Low", "Close"])
    idx = df.index
    trend, sh, sl = 0, None, None
    sh_used = sl_used = False
    fvgs, events, swing_highs, swing_lows = [], [], [], []

    for i in range(len(df)):
        # --- FVG ---
        if i >= 2:
            if l[i] > h[i - 2]:
                fvgs.append(dict(kind="bull", bot=h[i - 2], top=l[i], idx=i, inv=None))
            elif h[i] < l[i - 2]:
                fvgs.append(dict(kind="bear", bot=h[i], top=l[i - 2], idx=i, inv=None))
        # --- Inversion: إغلاق شمعة عبر الفجوة ---
        for f in fvgs:
            if f["inv"] is None and f["idx"] < i:
                if f["kind"] == "bull" and c[i] < f["bot"]:
                    f["inv"] = i
                elif f["kind"] == "bear" and c[i] > f["top"]:
                    f["inv"] = i
        # --- Pivots (تتأكد بعد R شموع) ---
        p = i - R
        if p >= L:
            if h[p] == h[p - L:p + R + 1].max():
                sh, sh_used = h[p], False
                swing_highs.append((idx[p], h[p]))
            if l[p] == l[p - L:p + R + 1].min():
                sl, sl_used = l[p], False
                swing_lows.append((idx[p], l[p]))
        # --- كسر الهيكل ---
        ev = None
        if sh is not None and not sh_used and c[i] > sh:
            ev = dict(dir="bull", kind="MSS" if trend == -1 else "BOS", level=sh, i=i)
            sh_used, trend = True, 1
        elif sl is not None and not sl_used and c[i] < sl:
            ev = dict(dir="bear", kind="MSS" if trend == 1 else "BOS", level=sl, i=i)
            sl_used, trend = True, -1
        if ev:
            want = "bear" if ev["dir"] == "bull" else "bull"
            cands = [f for f in fvgs if f["kind"] == want and f["inv"] is not None
                     and i - IFVG_LOOKBACK <= f["inv"] <= i]
            ev["ifvg"] = max(cands, key=lambda f: f["inv"]) if cands else None
            ev["retest"] = False
            events.append(ev)
        # --- تحديث إعادة الاختبار ---
        for e in events:
            if e["i"] < i and not e["retest"]:
                zone_hi = e["ifvg"]["top"] if e["ifvg"] else e["level"]
                zone_lo = e["ifvg"]["bot"] if e["ifvg"] else e["level"]
                if e["dir"] == "bull" and l[i] <= max(e["level"], zone_hi):
                    e["retest"] = True
                if e["dir"] == "bear" and h[i] >= min(e["level"], zone_lo):
                    e["retest"] = True

    return dict(df=df, events=events, trend=trend,
                swing_highs=swing_highs, swing_lows=swing_lows,
                sh=sh, sl=sl, sh_used=sh_used, sl_used=sl_used)


def last_mss(res):
    mss = [e for e in res["events"] if e["kind"] == "MSS"]
    return mss[-1] if mss else None


def near_info(r1, r4, price):
    """مستوى MSS لم ينكسر بعد والسعر ضمن NEAR_USD منه، لكل فريم."""
    out = {}
    for tf, r in (("1H", r1), ("4H", r4)):
        info = None
        # ترند صاعد: MSS هابط يحتاج كسر آخر قاع (السعر فوقه)
        if r["trend"] == 1 and r["sl"] is not None and not r["sl_used"]:
            d = price - r["sl"]
            if 0 <= d <= NEAR_USD:
                info = dict(dir="bear", level=float(r["sl"]), dist=float(d))
        # ترند هابط: MSS صاعد يحتاج كسر آخر قمة (السعر تحتها)
        elif r["trend"] == -1 and r["sh"] is not None and not r["sh_used"]:
            d = r["sh"] - price
            if 0 <= d <= NEAR_USD:
                info = dict(dir="bull", level=float(r["sh"]), dist=float(d))
        out[tf] = info
    return out


def pick_ifvg(m1, m4, confirmed):
    ifs = []
    if confirmed:
        if IFVG_TF in ("1H", "BOTH") and m1["ifvg"]: ifs.append(m1["ifvg"])
        if IFVG_TF in ("4H", "BOTH") and m4["ifvg"]: ifs.append(m4["ifvg"])
    elif m1["ifvg"]:
        ifs.append(m1["ifvg"])
    return ifs[0] if ifs else None


def lifecycle(r1, r4, m1):
    """دورة الصفقة منذ شمعة الكسر: broke → retest → t1 → t2
    أو نهاية: stopped / chase / expired. وتحذير warn عند كسر وقف 1."""
    df = r1["df"]
    h, l, c = df["High"].values, df["Low"].values, df["Close"].values
    idx = df.index
    i0 = m1["i"]
    bull = m1["dir"] == "bull"
    m4 = last_mss(r4)
    confirmed = bool(m4) and m4["dir"] == m1["dir"]
    ifvg = pick_ifvg(m1, m4, confirmed)

    # وقف 1: خلف الـ IFVG (أو مستوى الكسر إن لم يوجد)
    if bull:
        stop1 = (ifvg["bot"] if ifvg else m1["level"]) - 0.01
        prev = [p for t, p in r1["swing_lows"] if t < idx[i0]]
        stop2 = min((prev[-1] if prev else stop1) - 0.01, stop1)
        ret_lvl = max(m1["level"], ifvg["top"]) if ifvg else m1["level"]
    else:
        stop1 = (ifvg["top"] if ifvg else m1["level"]) + 0.01
        prev = [p for t, p in r1["swing_highs"] if t < idx[i0]]
        stop2 = max((prev[-1] if prev else stop1) + 0.01, stop1)
        ret_lvl = min(m1["level"], ifvg["bot"]) if ifvg else m1["level"]

    # الأهداف ثابتة من مستوى الكسر (لا تتحرك مع السعر)
    src = r4 if TARGET_TF == "4H" else r1
    if bull:
        tg = sorted(p for _, p in src["swing_highs"] if p > m1["level"])[:2]
    else:
        tg = sorted((p for _, p in src["swing_lows"] if p < m1["level"]),
                    reverse=True)[:2]
    t1 = tg[0] if len(tg) > 0 else None
    t2 = tg[1] if len(tg) > 1 else None

    stage, warn, retest = "broke", False, False

    def out(st):
        return dict(stage=st, warn=warn, retest=retest, stop1=stop1,
                    stop2=stop2, targets=tg, ifvg=ifvg, confirmed=confirmed)

    for j in range(i0 + 1, len(df)):
        # إلغاء الصفقة: إغلاق خلف وقف 2
        if (bull and c[j] < stop2) or (not bull and c[j] > stop2):
            return out("stopped")
        # تحذير: إغلاق خلف وقف 1
        if (bull and c[j] < stop1) or (not bull and c[j] > stop1):
            warn = True
        # إعادة الاختبار
        if not retest and ((bull and l[j] <= ret_lvl) or (not bull and h[j] >= ret_lvl)):
            retest, stage = True, "retest"
        # الأهداف
        if t1 is not None:
            hit1 = h[j] >= t1 if bull else l[j] <= t1
            if hit1 and not retest:
                return out("chase")
            if hit1 and stage in ("broke", "retest"):
                stage = "t1"
        if t2 is not None and stage == "t1":
            hit2 = h[j] >= t2 if bull else l[j] <= t2
            if hit2:
                return out("t2")
        # انتهاء المدة: 3 أيام تداول بعد يوم الكسر
        days = len({ts.date() for ts in idx[i0:j + 1]}) - 1
        if days >= MAX_DAYS:
            return out("expired")
    return out(stage)


def report(symbol, pre=None):
    if pre is None:
        d1, d4 = load(symbol)
        r1, r4 = analyze(d1), analyze(d4)
    else:
        d1, d4, r1, r4 = pre
    m1 = last_mss(r1)
    price = d1["Close"].iloc[-1]
    if not m1:
        return f"{symbol}: لا يوجد MSS على الساعة"

    lc = lifecycle(r1, r4, m1)
    arrow = "صعود 🟢" if m1["dir"] == "bull" else "هبوط 🔴"
    status = f"{arrow} {'مثبت (الساعة + 4س)' if lc['confirmed'] else 'بانتظار تأكيد 4س'}"
    ifvg = lc["ifvg"]
    ifvg_txt = f"{ifvg['bot']:.2f} → {ifvg['top']:.2f}" if ifvg else "لا يوجد"
    warn = "\n  ⚠️ تم كسر وقف 1" if lc["warn"] else ""

    return (f"{symbol} | السعر {price:.2f}\n"
            f"  الحالة: {status}\n"
            f"  المرحلة: {STAGE_TXT[lc['stage']]}{warn}\n"
            f"  سعر الكسر: {m1['level']:.2f}\n"
            f"  IFVG قبل الكسر: {ifvg_txt}\n"
            f"  وقف 1 (خلف IFVG): {lc['stop1']:.2f}\n"
            f"  وقف 2 (إلغاء الهيكل): {lc['stop2']:.2f}\n"
            f"  الأهداف: {', '.join(f'{t:.2f}' for t in lc['targets']) or 'لا يوجد'}\n")


if __name__ == "__main__":
    for s in SYMBOLS:
        try:
            print(report(s))
        except Exception as e:
            print(f"{s}: خطأ {e}")
