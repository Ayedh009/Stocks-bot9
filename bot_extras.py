"""
bot_extras.py  -  إضافات بوت الأسهم: سحب السيولة + فلتر الفوليوم + صورة الشارت
تعتمد على DataFrame من yfinance بأعمدة: Open, High, Low, Close, Volume
(الفهرس زمني، الشموع المغلقة فقط - احذف الشمعة الجارية قبل الاستدعاء)

requirements.txt:  pandas numpy matplotlib mplfinance requests yfinance
"""
import io
import numpy as np
import pandas as pd
import requests
import matplotlib
matplotlib.use("Agg")  # ضروري على GitHub Actions (بدون شاشة)
import mplfinance as mpf
from matplotlib.patches import Rectangle


# ───────────────────────── 1) سحب السيولة ─────────────────────────
def _pivots(df, length=3):
    """قمم وقيعان محورية: [(موضع, سعر)]"""
    h, l = df["High"].values, df["Low"].values
    ph, pl = [], []
    for i in range(length, len(df) - length):
        if h[i] == h[i - length:i + length + 1].max():
            ph.append((i, h[i]))
        if l[i] == l[i - length:i + length + 1].min():
            pl.append((i, l[i]))
    return ph, pl


def detect_sweep(df, direction, brk_idx, lookback=40, pivot_len=3,
                 tol_pct=0.15, max_pen=2, need_equal=True):
    """
    هل سبق كسرَ الهيكل سحبُ سيولة؟
    direction:  1 = كسر صاعد (يُبحث عن سحب قيعان EQL)
               -1 = كسر هابط (يُبحث عن سحب قمم EQH)
    brk_idx:   موضع شمعة الكسر في df (موجب، مثل len(df)-1)
    tol_pct:   أقصى فرق % بين القمتين/القاعين ليُعتبرا متساويين
    max_pen:   عدد الشموع المسموح بها للعودة بعد الاختراق الوهمي
    need_equal: True = يشترط EQH/EQL ، False = يكفي قمة/قاع محوري واحد
    يرجع dict أو None
    """
    start = max(0, brk_idx - lookback)
    seg = df.iloc[start:brk_idx + 1]
    ph, pl = _pivots(seg, pivot_len)
    piv = pl if direction == 1 else ph
    lows, highs, closes = seg["Low"].values, seg["High"].values, seg["Close"].values

    # مستويات السيولة المرشحة: (سعر المستوى, موضع تكوّنه, النوع)
    cands = []
    for a in range(len(piv)):
        for b in range(a + 1, len(piv)):
            pa, pb = piv[a][1], piv[b][1]
            if abs(pa - pb) / pa * 100 <= tol_pct:
                lvl = min(pa, pb) if direction == 1 else max(pa, pb)
                cands.append((lvl, piv[b][0], "EQL" if direction == 1 else "EQH"))
    if not need_equal:
        for i, p in piv:
            cands.append((p, i, "Swing Low" if direction == 1 else "Swing High"))

    best = None
    for lvl, formed, kind in cands:
        for j in range(formed + 1, len(seg)):
            pierced = lows[j] < lvl if direction == 1 else highs[j] > lvl
            if not pierced:
                continue
            # عودة الإغلاق خلف المستوى خلال max_pen شمعة (اختراق وهمي)
            for k in range(j, min(j + max_pen, len(seg))):
                back = closes[k] > lvl if direction == 1 else closes[k] < lvl
                if back:
                    ext = lows[j:k + 1].min() if direction == 1 else highs[j:k + 1].max()
                    cand = {"level": float(lvl), "kind": kind, "sweep_idx": start + j,
                            "extreme": float(ext)}
                    if best is None or cand["sweep_idx"] > best["sweep_idx"]:
                        best = cand
                    break
            break  # أول اختراق للمستوى فقط
    return best


# ───────────────────────── 2) فلتر الفوليوم ─────────────────────────
def volume_check(df, idxs, mult=1.5, n=20):
    """
    يقارن فوليوم الشموع المهمة (شمعة السحب / شمعة الكسر) بمتوسط آخر n شمعة.
    ok = أي منهما >= mult × المتوسط.   يرجع (ok, أعلى نسبة)
    """
    best = 0.0
    for i in idxs:
        if i is None or i < n:
            continue
        avg = df["Volume"].iloc[i - n:i].mean()
        if avg > 0:
            best = max(best, df["Volume"].iloc[i] / avg)
    return best >= mult, round(best, 2)


# ───────────────────────── 3) الرسم بـ mplfinance ─────────────────────────
def make_chart(df, symbol, tf_name, signal, sweep=None, bars=60):
    """
    signal = {
      "direction": 1/-1, "kind": "MSS"/"BOS",
      "brk_idx": int, "brk_level": float, "brk_from_idx": int (اختياري: بداية خط الكسر),
      "ifvg": {"top":..,"bot":..,"start_idx":..} أو None,
      "stop": float|None, "targets": [t1, t2]|[]
    }
    يرجع BytesIO بصيغة PNG
    """
    off = max(0, len(df) - bars)
    d = df.iloc[off:]
    n = len(d)
    rel = lambda i: max(0, min(n - 1, i - off))
    bull = signal["direction"] == 1
    col = "#00c853" if bull else "#ff1744"

    fig, axes = mpf.plot(
        d, type="candle", style="nightclouds", volume=True, returnfig=True,
        figsize=(11, 7), title=f"\n{symbol}  {tf_name}  {'Bullish' if bull else 'Bearish'} {signal['kind']}",
        warn_too_much_data=10_000,
    )
    ax = axes[0]

    # خط الكسر الأفقي (أخضر/أحمر)
    x0 = rel(signal.get("brk_from_idx", signal["brk_idx"] - 8))
    ax.hlines(signal["brk_level"], x0, n + 3, colors=col, linestyles="--", linewidth=1.6)
    ax.text(x0, signal["brk_level"], f" {signal['kind']} {signal['brk_level']:.2f}",
            color=col, fontsize=9, va="bottom", fontweight="bold")

    # منطقة IFVG (مستطيل شبه شفاف)
    z = signal.get("ifvg")
    if z:
        zx = rel(z["start_idx"])
        ax.add_patch(Rectangle((zx, z["bot"]), n + 3 - zx, z["top"] - z["bot"],
                               facecolor="#2979ff", alpha=0.22, edgecolor="#2979ff", linewidth=1))
        ax.text(zx, z["top"], " IFVG", color="#82b1ff", fontsize=9, va="bottom")

    # مستوى سحب السيولة
    if sweep:
        sx = rel(sweep["sweep_idx"])
        ax.hlines(sweep["level"], max(0, sx - 12), sx + 2, colors="#ffd600", linestyles=":", linewidth=1.4)
        ax.annotate(f"{sweep['kind']} Sweep", (sx, sweep["extreme"]), color="#ffd600", fontsize=9,
                    xytext=(0, -16 if bull else 16), textcoords="offset points", ha="center",
                    arrowprops=dict(arrowstyle="->", color="#ffd600"))

    # الوقف والأهداف
    if signal.get("stop"):
        ax.hlines(signal["stop"], x0, n + 3, colors="#ff9100", linewidth=1.2)
        ax.text(n - 1, signal["stop"], f"Stop {signal['stop']:.2f} ", color="#ff9100", fontsize=8, ha="right", va="top")
    for k, t in enumerate(signal.get("targets") or [], 1):
        ax.hlines(t, x0, n + 3, colors="#18ffff", linestyles="-.", linewidth=1)
        ax.text(n - 1, t, f"T{k} {t:.2f} ", color="#18ffff", fontsize=8, ha="right", va="bottom")

    # توسيع المحور ليشمل كل المستويات
    lv = [signal["brk_level"], signal.get("stop"), *(signal.get("targets") or [])]
    if z:
        lv += [z["top"], z["bot"]]
    lv = [v for v in lv if v]
    lo, hi = ax.get_ylim()
    pad = (hi - lo) * 0.03
    ax.set_ylim(min(lo, min(lv) - pad), max(hi, max(lv) + pad))
    ax.set_xlim(-1, n + 4)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
    matplotlib.pyplot.close(fig)
    buf.seek(0)
    return buf


# ───────────────────────── 4) الإرسال لتيليجرام ─────────────────────────
def send_photo(token, chat_id, png, caption):
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data={"chat_id": chat_id, "caption": caption[:1000]},
        files={"photo": ("chart.png", png, "image/png")},
        timeout=30,
    )
    r.raise_for_status()


# ───────────────────────── 5) الدالة الجاهزة للربط ─────────────────────────
def enrich_and_send(df, symbol, tf_name, signal, token, chat_id,
                    require_sweep=False, require_volume=False,
                    vol_mult=1.5, tol_pct=0.15):
    """
    تستدعيها عند كل MSS/BOS جديد.
    require_sweep / require_volume = True  → لا تُرسل إلا إذا تحقق الشرط.
    False → تُرسل دائماً مع وسم ✅/❌ في الرسالة.
    يرجع True إذا أُرسلت.
    """
    d = signal["direction"]
    sweep = detect_sweep(df, d, signal["brk_idx"], tol_pct=tol_pct)
    vol_ok, ratio = volume_check(
        df, [signal["brk_idx"], sweep["sweep_idx"] if sweep else None], mult=vol_mult)

    if (require_sweep and not sweep) or (require_volume and not vol_ok):
        return False

    side = "صاعد 🟢" if d == 1 else "هابط 🔴"
    lines = [f"{symbol} | {signal['kind']} {side} على {tf_name}",
             f"سعر الكسر: {signal['brk_level']:.2f}"]
    if signal.get("ifvg"):
        z = signal["ifvg"]
        lines.append(f"IFVG: {z['bot']:.2f} - {z['top']:.2f}")
    lines.append(f"سحب سيولة: ✅ {sweep['kind']} عند {sweep['level']:.2f}" if sweep
                 else "سحب سيولة: ❌ لا يوجد")
    lines.append(f"الفوليوم: {'✅' if vol_ok else '❌'} ({ratio}× المتوسط)")
    if signal.get("stop"):
        lines.append(f"الوقف: {signal['stop']:.2f}")
    for k, t in enumerate(signal.get("targets") or [], 1):
        lines.append(f"هدف {k}: {t:.2f}")

    png = make_chart(df, symbol, tf_name, signal, sweep)
    send_photo(token, chat_id, png, "\n".join(lines))
    return True


# ───────────────────────── مثال الاستخدام ─────────────────────────
if __name__ == "__main__":
    import os
    import yfinance as yf
    df = yf.download("TSLA", period="30d", interval="1h", progress=False, auto_adjust=True)
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df.dropna()
    i = len(df) - 1
    sig = {"direction": 1, "kind": "MSS", "brk_idx": i, "brk_level": float(df["High"].iloc[-6:-1].max()),
           "ifvg": None, "stop": float(df["Low"].iloc[-10:].min()), "targets": []}
    enrich_and_send(df, "TSLA", "1H", sig, os.environ["TG_TOKEN"], os.environ["TG_CHAT_ID"])
