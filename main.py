import os
import re
import asyncio
import threading
from datetime import datetime, timedelta

import requests
from flask import Flask
from telethon import TelegramClient, events, utils

# =========================
# CONFIG FROM ENV VARIABLES
# =========================
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

SOURCE_CHANNEL = int(os.environ["SOURCE_CHANNEL"])
DESTINATION_CHANNEL = int(os.environ["DESTINATION_CHANNEL"])
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
BOT_TOKEN = os.environ["BOT_TOKEN"]

if SOURCE_CHANNEL == DESTINATION_CHANNEL or SOURCE_CHANNEL == CHANNEL_ID:
    raise Exception("SAFETY CHECK FAILED: source channel must not equal destination/channel id")

ANALYSIS_IMAGE_URL = os.environ.get(
    "ANALYSIS_IMAGE_URL",
    "https://i.postimg.cc/wvJRv9DS/Whisk-d34745068f338f18b684a7ffb5cd969fdr.jpg"
)

outside_session_notice_sent = False
recent_signals = set()

# Telethon session file will be created in the service filesystem
from telethon.sessions import StringSession

SESSION = os.environ["SESSION"]

client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)

# =========================
# FLASK APP FOR RENDER
# =========================
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running v2", 200

@app.route("/health")
def health():
    return "OK", 200

# =========================
# FLAG MAP
# =========================
CURRENCY_FLAGS = {
    "AUD": "🇦🇺",
    "USD": "🇺🇸",
    "GBP": "🇬🇧",
    "EUR": "🇪🇺",
    "JPY": "🇯🇵",
    "NZD": "🇳🇿",
    "CAD": "🇨🇦",
    "CHF": "🇨🇭",
    "MXN": "🇲🇽",
}

# =========================
# CLEAN TEXT FOR PARSING
# =========================
def clean_text(text: str) -> str:
    return re.sub(r"[^\x00-\x7F]+", " ", text)

# =========================
# ADD FLAGS BACK TO PAIR
# =========================
def add_flags_to_pair(pair: str) -> str:
    otc = " (OTC)" if "(OTC)" in pair else ""
    pure_pair = pair.replace("(OTC)", "").strip()

    if "/" not in pure_pair:
        return pair

    base, quote = [p.strip().upper() for p in pure_pair.split("/")]

    base_flag = CURRENCY_FLAGS.get(base, "")
    quote_flag = CURRENCY_FLAGS.get(quote, "")

    if base_flag or quote_flag:
        return f"{base_flag} {base}/{quote} {quote_flag}{otc}".strip()

    return f"{base}/{quote}{otc}"

# =========================
# PARSER
# =========================
def parse_signal(text: str):
    flat = clean_text(text)
    flat = flat.replace("\n", " ").replace("\r", "")
    flat = re.sub(r"\s+", " ", flat).strip()

    pair = re.search(r"([A-Z]{3}/[A-Z]{3}\s*\(OTC\))", flat, re.I)
    tf = re.search(r"Timeframe:\s*(\d+)\s*-?\s*min\s*expiry", flat, re.I)
    entry = re.search(r"Entry Window:\s*([0-9]{1,2}:\d{2}\s*[APMapm]{2})", flat, re.I)
    direction = re.search(r"Direction:\s*(BUY|SELL)", flat, re.I)
    conf = re.search(r"AI Confidence:\s*(\d+)%", flat, re.I)

    mg1 = re.search(r"Level\s*1\s*.*?([0-9]{1,2}:\d{2}\s*[APMapm]{2})", flat, re.I)
    mg2 = re.search(r"Level\s*2\s*.*?([0-9]{1,2}:\d{2}\s*[APMapm]{2})", flat, re.I)
    mg3 = re.search(r"Level\s*3\s*.*?([0-9]{1,2}:\d{2}\s*[APMapm]{2})", flat, re.I)

    if not pair or not direction or not tf:
        return None

    return {
        "pair": pair.group(1).strip().upper(),
        "direction": direction.group(1).strip().upper(),
        "entry": entry.group(1).strip().upper() if entry else None,
        "expiry": f"M{tf.group(1)}",
        "confidence": f"{conf.group(1)}%" if conf else None,
        "mg1": mg1.group(1).strip().upper() if mg1 else None,
        "mg2": mg2.group(1).strip().upper() if mg2 else None,
        "mg3": mg3.group(1).strip().upper() if mg3 else None,
    }

# =========================
# FORMAT MESSAGE
# =========================
def format_signal(d):
    flagged_pair = add_flags_to_pair(d["pair"])

    return f"""
🚨 TRADE NOW!!!

📊 PAIR: {flagged_pair}
📈 DIRECTION: {d['direction']}
⏱ ENTRY: {d['entry'] or 'N/A'}
⌛ EXPIRY: {d['expiry']}
🤖 CONFIDENCE: {d['confidence'] or 'N/A'}

📊 MARTINGALE:
• Level 1 → {d['mg1'] or 'N/A'}
• Level 2 → {d['mg2'] or 'N/A'}
• Level 3 → {d['mg3'] or 'N/A'}

⚠️ Manage Risk Properly
""".strip()

# =========================
# DUPLICATE CHECK
# =========================
def signal_signature(d):
    return "|".join([
        d.get("pair") or "",
        d.get("direction") or "",
        d.get("entry") or "",
        d.get("expiry") or "",
        d.get("mg1") or "",
        d.get("mg2") or "",
        d.get("mg3") or "",
    ])

# =========================
# SEND TO DESTINATION CHANNEL
# =========================
def send_to_channel(message: str):
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": DESTINATION_CHANNEL,
            "text": message
        },
        timeout=20
    )
    print(f"[BOT API STATUS] {response.status_code}")
    print(f"[BOT API RESPONSE] {response.text}")
    response.raise_for_status()

# =========================
# TRADING TIME CHECK
# =========================
def is_trading_time():
    now = datetime.utcnow() + timedelta(hours=1)  # WAT
    hour = now.hour
    minute = now.minute

    if 8 <= hour < 12:
        return True

    if (14 <= hour < 17) or (hour == 17 and minute <= 30):
        return True

    if 20 <= hour < 23:
        return True

    return False

# =========================
# FORMAT ANALYSIS MESSAGE
# =========================
def format_analysis_message():
    now = datetime.utcnow() + timedelta(hours=1)
    current_time = now.strftime("%I:%M %p").lstrip("0")

    return (
        f"🕒 {current_time} (WAT)\n\n"
        f"Well-done! Current session completed.\n"
        f"AI is currently analyzing the market for opportunities.\n"
        f"Prepare for the next session.\n\n"
        f"⚡ Mondayb Trading AI"
    )

# =========================
# SEND ANALYSIS PHOTO
# =========================
async def send_analysis_photo(client_obj):
    caption = format_analysis_message()
    await client_obj.send_file(
        CHANNEL_ID,
        ANALYSIS_IMAGE_URL,
        caption=caption
    )

# =========================
# PERIODIC SESSION MONITOR
# =========================
async def monitor_trading_session(client_obj):
    global outside_session_notice_sent, recent_signals
    last_clear_time = datetime.now() # Initialize last clear time

    while True:
        now = datetime.now()
        # Clear recent_signals every hour
        if (now - last_clear_time).total_seconds() >= 3600: # 3600 seconds = 1 hour
            recent_signals.clear()
            last_clear_time = now
            print("[INFO] recent_signals set cleared.")

        if not is_trading_time():
            if not outside_session_notice_sent:
                print("Outside trading session. Analysis photo skipped.")
                outside_session_notice_sent = True
                print("Outside trading session. Bot paused.")
            await asyncio.sleep(60)
        else:
            if outside_session_notice_sent:
                outside_session_notice_sent = False
                print("Trading session resumed.")
            await asyncio.sleep(5)

# =========================
# MAIN HANDLER
# =========================
@client.on(events.NewMessage(incoming=True))
async def handler(event):
    try:
        print(f"[INCOMING] chat_id={event.chat_id} sender_id={event.sender_id}")

        if event.chat_id != SOURCE_CHANNEL:
            print(f"[SKIP] Not source channel: {event.chat_id}")
            return

        if not is_trading_time():
            print(f"[SKIP] Not trading session. Message from {SOURCE_CHANNEL} ignored.")
            return

        now = datetime.now()
        print(f"[RECEIVED] {now.strftime('%H:%M:%S')}")

        text = event.raw_text or ""
        print(f"[SOURCE RAW] {text}")

        parsed = parse_signal(text)

        if not parsed:
            print("[SKIP] Not signal")
            print(f"[SOURCE SKIPPED RAW] {text}")
            return

        print(f"[PARSED] pair={parsed['pair']}, direction={parsed['direction']}, expiry={parsed['expiry']}")

        if parsed["expiry"] != "M2":
            print(f"[SKIP] Not M2: {parsed['expiry']}")
            print(f"[SOURCE SKIPPED RAW] {text}")
            return

        sig = signal_signature(parsed)

        if sig in recent_signals:
            print(f"[SKIP] Duplicate signal: {sig}")
            print(f"[SOURCE DUPLICATE RAW] {text}")
            return

        formatted_message = format_signal(parsed)

        print(f"[FORWARDING] {datetime.now().strftime('%H:%M:%S')}")
        print(f"[FORWARDED MESSAGE]\n{formatted_message}")

        send_to_channel(formatted_message)

        print(f"[SENT] {datetime.now().strftime('%H:%M:%S')}")
        print("--------")

        recent_signals.add(sig)

    except Exception as e:
        print("Error in handler:", e)
# =========================
# TELEGRAM BOT LOOP
# =========================
async def telegram_main():
    print("Starting Telegram client...")
    await client.start()
    print("Telegram bot is running...")

    me = await client.get_me()
    print(f"[SESSION USER] id={me.id} username={getattr(me, 'username', None)}")

    try:
        source_entity = await client.get_entity(SOURCE_CHANNEL)
        print(f"[SOURCE RESOLVED] title={getattr(source_entity, 'title', None)} id={utils.get_peer_id(source_entity)}")
    except Exception as e:
        print(f"[SOURCE RESOLVE FAILED] {e}")

    asyncio.create_task(monitor_trading_session(client))
    await client.run_until_disconnected()

def run_telegram_bot():
    print("run_telegram_bot started")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(telegram_main())

# =========================
# START EVERYTHING
# =========================
if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_telegram_bot, daemon=True)
    bot_thread.start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
    
