import os, json, time, requests
from datetime import datetime
from zoneinfo import ZoneInfo
from scanner import load, analyze, last_mss, report

TOKEN = os.getenv("BOT_TOKEN", "8903554903:AAEIYKzWbc1P0QxLcHrXwBTET7w_z25zOaY")
API = f"https://api.telegram.org/bot{TOKEN}"
STATE = "state.json"
INTERVAL = 15 * 60
TEST_WORDS = ("/test", "test", "تست")


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"chat_id": None, "offset": 0, "sent": {},
            "symbols": ["TSLA", "META", "AMD", "NVDA", "MU", "AAPL", "MSFT"]}


def save_state(s):
    json.dump(s, open(STATE, "w"))


def send(chat_id, text):
    requests.post(f"{API}/sendMessage",
                  data={"chat_id": chat_id, "text": text}, timeout=20)


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
                      f"الشركات: {', '.join(s['symbols'])}\n"
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


def signal_key(symbol):
    d1, d4 = load(symbol)
    m1 = last_mss(analyze(d1))
    m4 = last_mss(analyze(d4))
    if not m1:
        return None
    confirmed = bool(m4) and m4["dir"] == m1["dir"]
    return f"{m1['dir']}|{round(m1['level'], 2)}|{confirmed}|{m1['retest']}"


def scan(s):
    if not s["chat_id"]:
        return
    for sym in s["symbols"]:
        try:
            key = signal_key(sym)
            if key and key != s["sent"].get(sym):
                send(s["chat_id"], "🔔 " + report(sym))
                s["sent"][sym] = key
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
