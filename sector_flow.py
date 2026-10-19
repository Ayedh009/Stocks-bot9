# sector_flow.py
import datetime as dt
from zoneinfo import ZoneInfo
import numpy as np
import yfinance as yf

NY = ZoneInfo("America/New_York")
MIN_RVOL = 1.2  # أقل حجم نسبي نعتبره دخول سيولة حقيقي

# القطاعات: الصندوق + أسهمك + قادة القطاع (عدّلها كما تريد)
SECTORS = {
    "أشباه الموصلات": {"etf": "SMH", "stocks": ["NVDA", "AMD", "MU", "AVGO", "QCOM", "INTC", "SNDK"]},
    "التقنية":        {"etf": "XLK", "stocks": ["AAPL", "MSFT", "ORCL", "IBM", "CRWV"]},
    "الاتصالات":      {"etf": "XLC", "stocks": ["META", "RDDT", "GOOGL"]},
    "الاستهلاكي":     {"etf": "XLY", "stocks": ["AMZN", "TSLA", "HD"]},
    "الكريبتو":       {"etf": "IBIT", "stocks": ["COIN", "CRCL", "MSTR"]},
    "المالية":        {"etf": "XLF", "stocks": ["JPM", "BAC", "GS", "V"]},
    "الصناعة والفضاء": {"etf": "XLI", "stocks": ["BA", "RKLB", "GE", "CAT"]},
    "الطاقة":         {"etf": "XLE", "stocks": ["XOM", "CVX", "COP"]},
    "الصحة":          {"etf": "XLV", "stocks": ["LLY", "UNH", "JNJ", "ABBV"]},
    "السلع الأساسية": {"etf": "XLP", "stocks": ["WMT", "COST", "PG", "KO"]},
    "المرافق":        {"etf": "XLU", "stocks": ["NEE", "SO", "DUK"]},
    "المواد":         {"etf": "XLB", "stocks": ["LIN", "FCX", "NEM"]},
    "العقار":         {"etf": "XLRE", "stocks": ["PLD", "AMT", "EQIX"]},
}


# ---------- قياس السيولة لكل قطاع ----------
def measure_flow():
    etfs = [v["etf"] for v in SECTORS.values()]
    d = yf.download(etfs, period="30d", interval="5m", progress=False, auto_adjust=True)
    vol, close = d["Volume"], d["Close"]
    vol.index = vol.index.tz_convert(NY)
    close.index = close.index.tz_convert(NY)
    today = vol.index[-1].date()
    cutoff = vol.index[-1].time()

    out = {}
    for name, v in SECTORS.items():
        e = v["etf"]
        if e not in vol:
            continue
        ve, ce = vol[e].dropna(), close[e].dropna()
        if ve.empty:
            continue
        cum = {day: g[g.index.time <= cutoff].sum() for day, g in ve.groupby(ve.index.date)}
        prev = [x for day, x in cum.items() if day != today and x > 0][-20:]
        if len(prev) < 5 or cum.get(today, 0) == 0:
            continue
        ct = ce[ce.index.date == today]
        pc = ce[ce.index.date < today]
        if ct.empty or pc.empty:
            continue
        out[name] = {
            "etf": e,
            "rvol": cum[today] / np.mean(prev),
            "chg": (ct.iloc[-1] / pc.iloc[-1] - 1) * 100,
        }
    return out


# ---------- الشركات المستفيدة دائماً من السيولة ----------
def beneficiaries(sector, n=4):
    info = SECTORS[sector]
    tickers = [info["etf"]] + info["stocks"]
    d = yf.download(tickers, period="6mo", interval="1d", progress=False, auto_adjust=True)
    r = d["Close"].pct_change()
    vol = d["Volume"][info["etf"]]
    inflow_days = r.index[(r[info["etf"]] > 0) & (vol > vol.rolling(20).mean())]
    rows = []
    for s in info["stocks"]:
        if s not in r:
            continue
        rs = r.loc[inflow_days, s].dropna()
        if len(rs) < 6:
            continue
        rows.append((s, (rs > 0).mean() * 100, rs.mean() * 100))
    rows.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return rows[:n], len(inflow_days)


# ---------- بناء الرسالة ----------
def build_message(title, flows, leader, note=""):
    ranked = sorted(flows.items(), key=lambda kv: kv[1]["rvol"], reverse=True)
    inflow = [(n, f) for n, f in ranked if f["chg"] > 0 and f["rvol"] >= MIN_RVOL][:3]
    outflow = [(n, f) for n, f in ranked if f["chg"] < 0 and f["rvol"] >= MIN_RVOL][:2]

    lines = [f"<b>{title}</b>"]
    if note:
        lines.append(note)
    lines.append("")
    lines.append("<b>دخول سيولة:</b>")
    if inflow:
        for n, f in inflow:
            lines.append(f"🔥 {n} ({f['etf']}) | حجم {f['rvol']:.1f}x | {f['chg']:+.2f}%")
    else:
        lines.append("لا يوجد قطاع بدخول سيولة واضح الآن")
    if outflow:
        lines.append("")
        lines.append("<b>خروج سيولة:</b>")
        for n, f in outflow:
            lines.append(f"🔻 {n} ({f['etf']}) | حجم {f['rvol']:.1f}x | {f['chg']:+.2f}%")

    if leader:
        rows, days = beneficiaries(leader)
        if rows:
            lines.append("")
            lines.append(f"<b>الأسهم المستفيدة في {leader}:</b>")
            for s, hit, avg in rows:
                lines.append(f"• {s}: صعد في {hit:.0f}% من أيام دخول السيولة (متوسط {avg:+.1f}%)")
            lines.append(f"<i>على {days} يوم دخول سيولة خلال 6 أشهر</i>")
    return "\n".join(lines)


# ---------- التشغيل (يُستدعى كل 5 دقائق من bot_once.py) ----------
def run_sector_report(s, send):
    now = dt.datetime.now(NY)
    if now.weekday() >= 5 or not (dt.time(9, 30) <= now.time() <= dt.time(16, 0)):
        return

    today = str(now.date())
    state = s.get("sector")
    if not state or state.get("date") != today:
        state = {"date": today, "open_sent": False, "leader": None, "pending": None, "last_sent": None}
    s["sector"] = state

    flows = measure_flow()
    if not flows:
        return

    cands = [(n, f) for n, f in flows.items() if f["chg"] > 0 and f["rvol"] >= MIN_RVOL]
    candidate = max(cands, key=lambda kv: kv[1]["rvol"])[0] if cands else None

    message = None
    if not state["open_sent"]:
        state["open_sent"] = True
        state["leader"] = candidate
        message = build_message("🔔 بداية السوق: أين السيولة؟", flows, candidate)
    elif candidate and candidate != state["leader"]:
        # تأكيد مرتين متتاليتين قبل إعلان الانتقال
        if state["pending"] == candidate:
            old = state["leader"]
            state["leader"], state["pending"] = candidate, None
            note = f"انتقلت القيادة من {old} إلى {candidate}" if old else f"السيولة تدخل في {candidate}"
            message = build_message("🔄 انتقال السيولة بين القطاعات", flows, candidate, note)
        else:
            state["pending"] = candidate
    else:
        state["pending"] = None

    if message is None:
        last = state["last_sent"]
        if last is None or (now - dt.datetime.fromisoformat(last)).total_seconds() >= 3600:
            message = build_message("⏱ تحديث السيولة (كل ساعة)", flows, state["leader"] or candidate)

    if message:
        send(message)
        state["last_sent"] = now.isoformat()
