import os, json, requests
import bot

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
bot.save_state(s)
