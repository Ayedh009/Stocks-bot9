import os
from bot import load_state, poll_commands, scan, save_state

s = load_state()
if not s["chat_id"]:
    s["chat_id"] = os.getenv("CHAT_ID") or None
poll_commands(s)
scan(s)
save_state(s)
