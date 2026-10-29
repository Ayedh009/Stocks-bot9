import os, json, time, requests
import bot
from sector_flow import run_sector_report

T0 = time.time()
LOOP_SECONDS = 270   # يبقى يستقبل الأوامر ~4.5 دقيقة بعد كل فحص (0 = بدون انتظار)

s = bot.load_state()

# أول تشغيل بدون حالة محفوظة: تجاهل الرسائل القديمة بدل الرد عليها دفعة واحدة
if "offset" not in json.load(open(bot.STATE)) if os.path.exists(bot.STATE) else True:
    r = requests.get(f"{bot.API}/getUpdates", params={"offset": -1}, timeout=20).json()
    res = r.get("result", [])
    if res:
        s["offset"] = res[-1]["update_id"] + 1

bot.poll_commands(s)    # الرد على status وأسماء الأسهم والأوامر
bot.due_summaries(s)    # ملخص 8:00 ص و4:15 م نيويورك
bot.scan(s)             # التنبيهات (8:30 – 4:00 فقط، تتجاهل نفسها خارجها)


def send_html(t):
    for cid in bot.ALERT_IDS:
        try:
            requests.post(
                f"{bot.API}/sendMessage",
                data={"chat_id": cid, "text": t, "parse_mode": "HTML"},
                timeout=20,
            )
        except Exception as e:
            print("sector send error:", cid, e)


# تقرير السيولة بين القطاعات (بداية السوق + كل ساعة + عند الانتقال)
if s.get("chat_id"):
    try:
        run_sector_report(s, send_html)
    except Exception as e:
        print("sector report error:", e)

bot.save_state(s)

# الرد السريع: يبقى يستقبل الرسائل حتى قبل بداية التشغيل القادم
while time.time() - T0 < LOOP_SECONDS:
    time.sleep(3)
    try:
        bot.poll_commands(s)
    except Exception as e:
        print("poll error:", e)

bot.save_state(s)
