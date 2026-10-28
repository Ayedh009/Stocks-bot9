import os, json, time, requests
from datetime import datetime
from zoneinfo import ZoneInfo
from scanner import (load, analyze, last_mss, report, near_info, lifecycle,
                     NEAR_USD, STAGE_TXT, TERMINAL)
from bot_extras import detect_sweep, volume_check, make_chart, send_photo
from spx_logic import spx_process, spx_report

TOKEN = os.environ["BOT_TOKEN"]          # يُقرأ من المتغيرات السرية، لا يُكتب هنا
API = f"https://api.telegram.org/bot{TOKEN}"
STATE = "state.json"
INTERVAL = 5 * 60                        # فحص كل 5 دقائق
TEST_WORDS = ("/test", "test", "تست")
STATUS_WORDS = ("/status", "status", "ستاتوس", "الحالة", "حالة")
VERSION = 3
NY = ZoneInfo("America/New_York")
SCAN_START = 8 * 60 + 30                 # 8:30 صباحاً نيويورك
SCAN_END = 16 * 60                       # 4:00 عصراً نيويورك
SUMMARIES = {"am": 8 * 60, "pm": 16 * 60 + 15}   # 8:00 و 4:15
SUMMARY_GRACE = 30
DEFAULT_SYMBOLS = [
    "TSLA", "META", "AMD", "NVDA", "MU", "AAPL", "MSFT",
    "SNDK", "QQQ", "CRCL", "COIN", "CRWV", "QCOM", "AMZN", "BA",
    "IBM", "INTC", "ORCL", "RDDT", "SPCX", "AVGO", "PLTR", "RKLB",
]
TF_NAME = {"1H": "الساعة", "4H": "4 ساعات"}


def load_state():
    s = json.load(open(STATE)) if os.path.exists(STATE) else {}
    s.setdefault("chat_id", None)
    s.setdefault("offset", 0)
    s.setdefault("life", {})
    s.setdefault("near", {})
    s.setdefault("h4", {})
    s.setdefault("summaries", {})
    if s.get("ver") != VERSION:           # تحديث قائمة الشركات مرة واحدة
        s["symbols"] = list(DEFAULT_SYMBOLS)
        s["ver"] = VERSION
    s.setdefault("symbols", list(DEFAULT_SYMBOLS))
    return s


def save_state(s):
    json.dump(s, open(STATE, "w"))


def send(chat_id, text):
    requests.post(f"{API}/sendMessage",
                  data={"chat_id": chat_id, "text": text}, timeout=20)
    time.sleep(0.5)


def ny_now():
    now = datetime.now(NY)
    return now, now.hour * 60 + now.minute


def scan_window_open():
    now, mins = ny_now()
    return now.weekday() < 5 and SCAN_START <= mins <= SCAN_END


def status_board(s, title):
    """لوحة حالة: الصفقات النشطة أولاً، والمنتهية مختصرة في سطر."""
    active, done = [], []
    for sym in s["symbols"]:
        try:
            d1, d4 = load(sym)
            r1, r4 = analyze(d1), analyze(d4)
            price = float(d1["Close"].iloc[-1])
            m1 = last_mss(r1)
            if not m1:
                done.append(sym)
                continue
            lc = lifecycle(r1, r4, m1)
            if lc["stage"] in TERMINAL:
                done.append(sym)
                continue
            arrow = "🟢" if m1["dir"] == "bull" else "🔴"
            conf = " ✅4س" if lc["confirmed"] else ""
            warn = " ⚠️" if lc["warn"] else ""
            active.append(f"• {sym} {price:.2f} {arrow} كسر {m1['level']:.2f}{conf}"
                          f" — {STAGE_TXT[lc['stage']]}{warn}")
        except Exception as e:
            active.append(f"• {sym} — خطأ {e}")
    lines = [title, ""]
    lines.append("🟢 صفقات نشطة:" if active else "لا توجد صفقات نشطة الآن.")
    lines += active
    if done:
        lines += ["", "⚪ بدون صفقة نشطة: " + ", ".join(done)]
    return "\n".join(lines)


def due_summaries(s):
    now, mins = ny_now()
    if now.weekday() >= 5:
        return
    day = now.strftime("%Y-%m-%d")
    for name, at in SUMMARIES.items():
        key = f"{day}|{name}"
        if at <= mins <= at + SUMMARY_GRACE and key not in s["summaries"]:
            if s["chat_id"]:
                title = ("🌅 ملخص ما قبل الافتتاح" if name == "am"
                         else "🌇 ملخص نهاية اليوم")
                send(s["chat_id"], status_board(s, title))
            s["summaries"][key] = True
    s["summaries"] = {k: v for k, v in s["summaries"].items()
                      if k.startswith(day)}
    save_state(s)


def poll_commands(s):
    r = requests.get(f"{API}/getUpdates",
                     params={"offset": s["offset"], "timeout": 0},
                     timeout=20).json()
    for u in r.get("result", []):
        s["offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        txt = (m.get("text") or "").strip()
        cid = m.get("chat", {}).get("id")
        if not txt or not cid:
            continue
        parts = txt.split()
        cmd, args = parts[0].lower(), [p.upper() for p in parts[1:]]

        if cmd in TEST_WORDS and s["chat_id"] is None:
            s["chat_id"] = cid

        if cmd == "/start":
            s["chat_id"] = cid
            send(cid, "تم التفعيل ✅\n"
                      "اكتب اسم سهم (مثل TSLA) ويرسل لك تقريره\n"
                      "اكتب status أو ستاتوس للوحة كل الأسهم\n"
                      "/add TSLA NVDA  إضافة\n"
                      "/remove TSLA  حذف\n/list  عرض القائمة\n"
                      "/spx TSLA  لقطة السباكس للسهم\n"
                      "test  اختبار البوت")
        elif cid != s["chat_id"]:
            continue
        elif cmd in TEST_WORDS:
            status = ("مفتوح 🟢" if scan_window_open()
                      else "مغلق 🔴 (الفحص من 8:30 إلى 4:00 نيويورك)")
            send(cid, "✅ البوت شغال\n"
                      f"الفحص الآن: {status}\n"
                      f"عدد الشركات: {len(s['symbols'])}\n"
                      f"{', '.join(s['symbols'])}\n"
                      f"تنبيه الاقتراب: ضمن {NEAR_USD:g}$ من كسر هيكل MSS (الساعة و4س)\n"
                      "الفحص كل 5 دقائق، وملخص يومي 8:00 ص و4:15 م نيويورك.")
        elif cmd in STATUS_WORDS:
            send(cid, status_board(s, "📊 حالة الأسهم الآن"))
        elif cmd == "/add":
            for a in args:
                if a not in s["symbols"]:
                    s["symbols"].append(a)
            send(cid, "القائمة: " + ", ".join(s["symbols"]))
        elif cmd == "/remove":
            s["symbols"] = [x for x in s["symbols"] if x not in args]
            send(cid, "القائمة: " + ", ".join(s["symbols"]))
        elif cmd == "/list":
            send(cid, "القائمة: " + ", ".join(s["symbols"]))
        elif cmd == "/spx":
            for a in args:
                try:
                    send(cid, spx_report(a, load(a)[0]))
                except Exception as e:
                    send(cid, f"{a}: خطأ {e}")
        elif cmd == "/now":
            for a in args:
                try:
                    send(cid, report(a))
                except Exception as e:
                    send(cid, f"{a}: خطأ {e}")
        else:
            # رسالة فيها أسماء أسهم فقط (TSLA أو NVDA AMD أو $TSLA)
            names = [t.lstrip("$").upper() for t in parts]
            if all(n in s["symbols"] for n in names):
                for n in names:
                    try:
                        send(cid, report(n))
                    except Exception as e:
                        send(cid, f"{n}: خطأ {e}")
    save_state(s)


def near_text(sym, price, near, ts):
    active = {tf: v for tf, v in near.items() if v}
    both = (len(active) == 2 and active["1H"]["dir"] == active["4H"]["dir"])
    head = "🔥 تقاطع" if both else "⚠️"
    lines = [f"{head} {sym} قريب من كسر هيكل (MSS)",
             f"السعر: {price:.2f}  (آخر شمعة {ts} نيويورك)"]
    for tf, v in active.items():
        kind = "هابط 🔴" if v["dir"] == "bear" else "صاعد 🟢"
        lines.append(f"• {TF_NAME[tf]}: كسر {kind} عند {v['level']:.2f} "
                     f"(المسافة {v['dist']:.2f}$)")
    return "\n".join(lines)


def h4_text(sym, price, m4, m1, r4):
    arrow = "🟢 صاعد" if m4["dir"] == "bull" else "🔴 هابط"
    t = r4["df"].index[m4["i"]].strftime("%m-%d %H:%M")
    lines = [f"🔷 كسر هيكل 4 ساعات (MSS) | {sym} {arrow}",
             f"مستوى الكسر: {m4['level']:.2f} | السعر {price:.2f}",
             f"شمعة الكسر: {t} نيويورك"]
    if m1 and m1["dir"] == m4["dir"]:
        lines.append("✅ يؤكد كسر الساعة الحالي")
    elif m1:
        lines.append("⚠️ عكس اتجاه كسر الساعة الحالي")
    else:
        lines.append("لا يوجد كسر ساعة حالياً")
    return "\n".join(lines)


def short_text(sym, price, m1, lc, old):
    """رسالة قصيرة عند تغيّر المرحلة فقط."""
    arrow = "🟢 صاعد" if m1["dir"] == "bull" else "🔴 هابط"
    lines = []
    if lc["stage"] != old["stage"]:
        lines.append(f"{STAGE_TXT[lc['stage']]} | {sym} {arrow}")
    if lc["warn"] and not old["warn"]:
        lines.append(f"⚠️ {sym}: كسر وقف 1 ({lc['stop1']:.2f}) — الإلغاء عند وقف 2 ({lc['stop2']:.2f})")
    lines.append(f"السعر {price:.2f} | كسر {m1['level']:.2f}")
    if lc["stage"] not in TERMINAL and lc["targets"]:
        lines.append("الأهداف: " + ", ".join(f"{t:.2f}" for t in lc["targets"]))
    return "\n".join(lines)


def send_signal(cid, sym, d1, m1, lc, text):
    """إشارة الكسر كصورة شارت + نص. إن فشل الرسم يرسل النص فقط."""
    try:
        d = 1 if m1["dir"] == "bull" else -1
        sweep = detect_sweep(d1, d, m1["i"])
        vol_ok, ratio = volume_check(
            d1, [m1["i"], sweep["sweep_idx"] if sweep else None])
        z = lc["ifvg"]
        sig = {
            "direction": d, "kind": m1["kind"],
            "brk_idx": m1["i"], "brk_level": float(m1["level"]),
            "ifvg": ({"top": float(z["top"]), "bot": float(z["bot"]),
                      "start_idx": int(z["idx"])} if z else None),
            "stop": float(lc["stop1"]),
            "targets": [float(t) for t in lc["targets"]],
        }
        extra = ("\n  سحب سيولة: ✅ " + sweep["kind"] + f" عند {sweep['level']:.2f}"
                 if sweep else "\n  سحب سيولة: ❌ لا يوجد")
        extra += f"\n  الفوليوم: {'✅' if vol_ok else '❌'} ({ratio}× المتوسط)"
        png = make_chart(d1, sym, "1H", sig, sweep)
        send_photo(TOKEN, cid, png, text + extra)
        time.sleep(0.5)
    except Exception as e:
        print(sym, "chart خطأ", e)
        send(cid, text)


def process(sym, s):
    d1, d4 = load(sym)
    r1, r4 = analyze(d1), analyze(d4)
    price = float(d1["Close"].iloc[-1])
    ts = d1.index[-1].strftime("%H:%M")
    cid = s["chat_id"]

    # 1) دورة الصفقة (المحرك: كسر الساعة)
    m1 = last_mss(r1)
    if m1:
        lc = lifecycle(r1, r4, m1)
        trade = f"{m1['dir']}|{round(m1['level'], 2)}"
        cur = dict(trade=trade, stage=lc["stage"], warn=lc["warn"])
        old = s["life"].get(sym)
        if old is None and lc["stage"] in TERMINAL:
            pass                           # صفقة قديمة منتهية: نسجلها بصمت
        elif old is None or old["trade"] != trade:
            prefix = ""
            if (old and old["stage"] not in TERMINAL
                    and old["trade"].split("|")[0] != m1["dir"]):
                prefix = "❌ انتهت الصفقة السابقة (MSS معاكس على الساعة)\n\n"
            send_signal(cid, sym, d1, m1, lc,
                        prefix + "🔔 " + report(sym, (d1, d4, r1, r4)))
        elif old["stage"] != cur["stage"] or old["warn"] != cur["warn"]:
            send(cid, short_text(sym, price, m1, lc, old))
        s["life"][sym] = cur

    # 2) تنبيه مستقل: كسر هيكل 4 ساعات
    m4 = last_mss(r4)
    if m4:
        k4 = f"{m4['dir']}|{round(m4['level'], 2)}"
        old4 = s["h4"].get(sym)
        if k4 != old4:
            recent = m4["i"] >= len(r4["df"]) - 2   # حدث في آخر شمعتين
            if old4 is not None or recent:
                send(cid, h4_text(sym, price, m4, m1, r4))
            s["h4"][sym] = k4

    # 3) تنبيه الاقتراب
    near = near_info(r1, r4, price)
    new = False
    for tf in ("1H", "4H"):
        v = near[tf]
        nk = f"{v['dir']}|{round(v['level'], 2)}" if v else None
        old_n = s["near"].get(f"{sym}|{tf}")
        if nk and nk != old_n:
            new = True
        s["near"][f"{sym}|{tf}"] = nk
    if new:
        send(cid, near_text(sym, price, near, ts))

    # 4) إشارات السباكس (أوردربلوك + كنترول) على هذا السهم
    spx_process(sym, s, d1, send)


def scan(s):
    if not s["chat_id"] or not scan_window_open():
        return
    for sym in s["symbols"]:
        try:
            process(sym, s)
        except Exception as e:
            print(sym, "خطأ", e)
    save_state(s)


if __name__ == "__main__":
    s = load_state()
    last = 0
    while True:
        try:
            poll_commands(s)
            due_summaries(s)
            if time.time() - last >= INTERVAL:
                scan(s)
                last = time.time()
        except Exception as e:
            print("خطأ:", e)
        time.sleep(5)
