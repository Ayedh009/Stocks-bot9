import os, json, requests
import bot
from sector_flow import run_sector_report

s = bot.load_state()

# أول تشغيل بدون حالة محفوظة: تجاهل الرسائل القديمة بدل الرد عليها دفعة واحدة
if "offset" not in json.load(open(bot.STATE)) if os.path.exists(bot.STATE) else True:
    r = requests.get(f"{bot.API}/getUpdates", params={"offset": -1}, timeout=20).json()
    res = r.get("result", [])
    if res:
        s["offset"] = res[-1]["update_id"] + 1

if not s["chat_id"] and os.getenv("CHAT_ID"):
    s["chat_id"] = int(os.environ["CHAT_ID"])

bot.poll_commands(s)    # الرد على status وأسماء الأسهم والأوامر
bot.due_summaries(s)    # ملخص 8:00 ص و4:15 م نيويورك
bot.scan(s)             # التنبيهات (8:30 – 4:00 فقط، تتجاهل نفسها خارجها)

# تقرير السيولة بين القطاعات (بداية السوق + كل ساعة + عند الانتقال)
if s.get("chat_id"):
    try:
        run_sector_report(
            s,
            lambda t: requests.post(
                f"{bot.API}/sendMessage",
                data={"chat_id": s["chat_id"], "text": t, "parse_mode": "HTML"},
                timeout=20,
            ),
        )
    except Exception as e:
        print("sector report error:", e)

bot.save_state(s)
