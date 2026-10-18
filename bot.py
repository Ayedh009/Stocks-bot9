import os, json, time, requests
from datetime import datetime
from zoneinfo import ZoneInfo
from scanner import (load, analyze, last_mss, report, near_info, NEAR_USD)

TOKEN = os.getenv("BOT_TOKEN", "8903554903:AAEIYKzWbc1P0QxLcHrXwBTET7w_z25zOaY")
API = f"https://api.telegram.org/bot{TOKEN}"
STATE = "state.json"
INTERVAL = 15 * 60
TEST_WORDS = ("/test", "test", "تست")
VERSION = 2
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
    s.setdefault("sent", {})
    s.setdefault("near", {})
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


def market_open():
    now = datetime.now(ZoneInfo("America/New_York"))
    mins = now.hour * 60 + now.minute
    return now.weekday() < 5 and 570 <= mins <= 960


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
            send(cid, "تم التفعيل ✅\n/add TSLA NVDA  إضافة\n"
                      "/remove TSLA  حذف\n/list  عرض القائمة\n"
                      "/now TSLA  فحص فوري\ntest  اختبار البوت")
        elif cid != s["chat_id"]:
            continue
        elif cmd in TEST_WORDS:
            status = "مفتوح 🟢" if market_open() else "مغلق 🔴 (الفحص يبدأ 9:30 نيويورك)"
            send(cid, "✅ البوت شغال\n"
                      f"السوق الآن: {status}\n"
                      f"عدد الشركات: {len(s['symbols'])}\n"
                      f"{', '.join(s['symbols'])}\n"
                      f"تنبيه الاقتراب: ضمن {NEAR_USD:g}$ من كسر هيكل MSS (الساعة و4س)\n"
                      "الفحص القادم بعد 5 دقائق تقريباً، وسترسل لك الإشارات عند ظهورها.")
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
        elif cmd == "/now":
            for a in args:
                try:
                    send(cid, report(a))
                except Exception as e:
                    send(cid, f"{a}: خطأ {e}")
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


def process(sym, s):
    d1, d4 = load(sym)
    r1, r4 = analyze(d1), analyze(d4)
    price = float(d1["Close"].iloc[-1])
    ts = d1.index[-1].strftime("%H:%M")
    cid = s["chat_id"]

    # 1) إشارة الكسر (تتغير حالتها فقط)
    m1, m4 = last_mss(r1), last_mss(r4)
    if m1:
        confirmed = bool(m4) and m4["dir"] == m1["dir"]
        key = f"{m1['dir']}|{round(m1['level'], 2)}|{confirmed}|{m1['retest']}"
        if key != s["sent"].get(sym):
            send(cid, "🔔 " + report(sym, (d1, d4, r1, r4)))
            s["sent"][sym] = key

    # 2) تنبيه الاقتراب
    near = near_info(r1, r4, price)
    new = False
    for tf in ("1H", "4H"):
        v = near[tf]
        nk = f"{v['dir']}|{round(v['level'], 2)}" if v else None
        old = s["near"].get(f"{sym}|{tf}")
        if nk and nk != old:
            new = True
        s["near"][f"{sym}|{tf}"] = nk
    if new:
        send(cid, near_text(sym, price, near, ts))


def scan(s):
    if not s["chat_id"]:
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
            if time.time() - last >= INTERVAL:
                scan(s)
                last = time.time()
        except Exception as e:
            print("خطأ:", e)
        time.sleep(5)
