import os
import json
from pymongo import MongoClient
from dotenv import load_dotenv
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
import requests
import re

load_dotenv()

BOT_TOKEN = os.getenv("SHORTENER_BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

MONGODB_URI = os.getenv("MONGODB_URI")
DB_NAME = os.getenv("MONGO_DB_NAME", "viralbox_db")
PORT = int(os.getenv("PORT", "8000"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

if not BOT_TOKEN or not MONGODB_URI:
    raise RuntimeError("BOT_TOKEN and MONGODB_URI must be set")

if not WEBHOOK_URL:
    raise RuntimeError("WEBHOOK_URL must be set")

# DB Setup
client = MongoClient(MONGODB_URI, maxPoolSize=50)
db = client[DB_NAME]
links_col = db["links"]
user_apis_col = db["user_apis"]
user_settings_col = db["user_settings"]

# Stats
bot_start_time = datetime.utcnow()
total_requests = 0
last_activity = datetime.utcnow()


# ─────────────────────────────────────────────
# Webhook Handler
# ─────────────────────────────────────────────
class WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global total_requests, last_activity

        if self.path == '/health' or self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()

            uptime = (datetime.utcnow() - bot_start_time).total_seconds()
            response = {
                "status": "healthy",
                "bot": "shortener-webhook",
                "mode": "webhook",
                "timestamp": datetime.utcnow().isoformat(),
                "uptime_seconds": int(uptime),
                "total_requests": total_requests,
                "last_activity": last_activity.isoformat()
            }
            self.wfile.write(json.dumps(response).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        global total_requests, last_activity

        if self.path == f'/{BOT_TOKEN}':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)

            try:
                update = json.loads(post_data.decode('utf-8'))

                if "message" in update:
                    total_requests += 1
                    last_activity = datetime.utcnow()
                    Thread(target=process_message, args=(update["message"],)).start()

                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            except Exception as e:
                print(f"Webhook error: {e}")
                self.send_response(500)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        if "error" in format.lower():
            print(format % args)


# ─────────────────────────────────────────────
# Setup Webhook
# ─────────────────────────────────────────────
def setup_webhook():
    webhook_url = f"{WEBHOOK_URL}/{BOT_TOKEN}"

    try:
        requests.post(f"{TELEGRAM_API}/deleteWebhook")
        print("Deleted old webhook")

        response = requests.post(
            f"{TELEGRAM_API}/setWebhook",
            json={
                "url": webhook_url,
                "max_connections": 100,
                "allowed_updates": ["message"]
            }
        )

        if response.json().get("ok"):
            print(f"Webhook set: {webhook_url}")
        else:
            print(f"Webhook failed: {response.text}")

    except Exception as e:
        print(f"Webhook error: {e}")


# ─────────────────────────────────────────────
# DB Helpers — API Key
# ─────────────────────────────────────────────
def get_user_api_key(user_id):
    try:
        doc = user_apis_col.find_one({"userId": user_id})
        return doc.get("apiKey") if doc else None
    except Exception as e:
        print(f"DB get API error: {e}")
        return None


def save_user_api_key(user_id, api_key):
    try:
        user_apis_col.update_one(
            {"userId": user_id},
            {"$set": {"userId": user_id, "apiKey": api_key}},
            upsert=True
        )
        return True
    except Exception as e:
        print(f"DB save API error: {e}")
        return False


# ─────────────────────────────────────────────
# DB Helpers — User Settings
# ─────────────────────────────────────────────
def get_user_settings(user_id):
    """
    Returns dict: { header, footer, caption_mode }
    caption_mode: 'keep' | 'remove'  (default: 'remove')
    """
    try:
        doc = user_settings_col.find_one({"userId": user_id})
        if doc:
            return {
                "header": doc.get("header", ""),
                "footer": doc.get("footer", ""),
                "caption_mode": doc.get("caption_mode", "remove")
            }
        return {"header": "", "footer": "", "caption_mode": "remove"}
    except Exception as e:
        print(f"DB get settings error: {e}")
        return {"header": "", "footer": "", "caption_mode": "remove"}


def update_user_setting(user_id, field, value):
    try:
        user_settings_col.update_one(
            {"userId": user_id},
            {"$set": {"userId": user_id, field: value}},
            upsert=True
        )
        return True
    except Exception as e:
        print(f"DB update setting error: {e}")
        return False


def delete_user_setting(user_id, field):
    try:
        user_settings_col.update_one(
            {"userId": user_id},
            {"$unset": {field: ""}}
        )
        return True
    except Exception as e:
        print(f"DB delete setting error: {e}")
        return False


# ─────────────────────────────────────────────
# Caption Builder
# ─────────────────────────────────────────────
def build_caption(shortened_links, original_caption, settings):
    """
    REMOVE mode: header + short links only + footer  (original caption discarded)
    KEEP mode:   header + original caption as-is + footer  (long URLs untouched)
    """
    mode = settings.get("caption_mode", "remove")
    header = settings.get("header", "").strip()
    footer = settings.get("footer", "").strip()

    parts = []

    if mode == "remove":
        if header:
            parts.append(header)
        for link in shortened_links:
            parts.append(link)
        if footer:
            parts.append(footer)

    elif mode == "keep":
        if header:
            parts.append(header)
        if original_caption and original_caption.strip():
            parts.append(original_caption.strip())
        if footer:
            parts.append(footer)

    return "\n".join(parts)


# ─────────────────────────────────────────────
# Core Helpers
# ─────────────────────────────────────────────
def shorten_url(long_url, api_key):
    try:
        api = f"https://viralbox.in/api?api={api_key}&url={requests.utils.requote_uri(long_url)}"
        r = requests.get(api, timeout=15)
        j = r.json()
        if j.get("status") == "success":
            return j.get("shortenedUrl")
        return None
    except Exception as e:
        print(f"Shortening error: {e}")
        return None


def save_to_db(longURL, shortURL):
    try:
        links_col.insert_one({
            "longURL": longURL,
            "shortURL": shortURL,
            "created_at": datetime.utcnow()
        })
    except Exception as e:
        print(f"DB error: {e}")


def extract_urls(text):
    if not text:
        return []
    return re.findall(r'(https?://[^\s]+)', text)


def send_message(chat_id, text):
    try:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        print(f"Send error: {e}")


def resend_media(chat_id, media_type, file_id, caption):
    endpoint = {
        "photo": "sendPhoto",
        "video": "sendVideo",
        "document": "sendDocument",
        "audio": "sendAudio",
        "voice": "sendVoice",
        "animation": "sendAnimation",
    }.get(media_type)

    if not endpoint:
        return

    try:
        payload = {"chat_id": chat_id, "parse_mode": "Markdown"}
        if caption:
            payload["caption"] = caption
        payload[media_type] = file_id
        requests.post(f"{TELEGRAM_API}/{endpoint}", json=payload, timeout=10)
    except Exception as e:
        print(f"Media error: {e}")


# ─────────────────────────────────────────────
# Process Message
# ─────────────────────────────────────────────
def process_message(msg):
    try:
        chat_id = msg["chat"]["id"]
        user_id = msg["from"]["id"]
        username = msg["from"].get("username", "Unknown")
        first_name = msg["from"].get("first_name", "User")

        text = msg.get("text", "").strip()

        # ── /start ──────────────────────────────────────────────────────────
        if text.startswith("/start"):
            send_message(
                chat_id,
                f"👋 *Welcome {first_name}!*\n\n"
                f"Send any link or media with URLs in caption to shorten them.\n\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"*⚙️ Setup*\n"
                f"`/set_api YOUR_KEY` — Set your Viralbox API key\n\n"
                f"*✏️ Caption Customization*\n"
                f"`/set_header TEXT` — Add text above links\n"
                f"`/set_footer TEXT` — Add text below links\n"
                f"`/delete_header` — Remove header\n"
                f"`/delete_footer` — Remove footer\n\n"
                f"*🔄 Caption Mode*\n"
                f"`/remove` — Remove original caption *(default)*\n"
                f"`/keep` — Keep original caption as-is\n"
                f"━━━━━━━━━━━━━━━━"
            )
            return

        # ── /set_api ─────────────────────────────────────────────────────────
        if text.startswith("/set_api"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                send_message(
                    chat_id,
                    "❌ *Usage:* `/set_api YOUR_API_KEY`\n\n"
                    "Example:\n`/set_api 030cd48a49cc4002ec50aeb10f3dc03ca0e84ce5`"
                )
                return
            api_key = parts[1].strip()
            if save_user_api_key(user_id, api_key):
                send_message(chat_id, "✅ *API Key saved!* You can now send links to shorten.")
                print(f"API key set by {username} ({user_id})")
            else:
                send_message(chat_id, "❌ Failed to save API key. Please try again.")
            return

        # ── /set_header ───────────────────────────────────────────────────────
        if text.startswith("/set_header"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                send_message(chat_id, "❌ *Usage:* `/set_header Your header text here`")
                return
            header_text = parts[1].strip()
            if update_user_setting(user_id, "header", header_text):
                send_message(chat_id, f"✅ *Header set:*\n\n{header_text}")
            else:
                send_message(chat_id, "❌ Failed to save header. Try again.")
            return

        # ── /delete_header ────────────────────────────────────────────────────
        if text.startswith("/delete_header"):
            if delete_user_setting(user_id, "header"):
                send_message(chat_id, "✅ Header removed.")
            else:
                send_message(chat_id, "❌ Failed to remove header. Try again.")
            return

        # ── /set_footer ───────────────────────────────────────────────────────
        if text.startswith("/set_footer"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                send_message(chat_id, "❌ *Usage:* `/set_footer Your footer text here`")
                return
            footer_text = parts[1].strip()
            if update_user_setting(user_id, "footer", footer_text):
                send_message(chat_id, f"✅ *Footer set:*\n\n{footer_text}")
            else:
                send_message(chat_id, "❌ Failed to save footer. Try again.")
            return

        # ── /delete_footer ────────────────────────────────────────────────────
        if text.startswith("/delete_footer"):
            if delete_user_setting(user_id, "footer"):
                send_message(chat_id, "✅ Footer removed.")
            else:
                send_message(chat_id, "❌ Failed to remove footer. Try again.")
            return

        # ── /keep ─────────────────────────────────────────────────────────────
        if text == "/keep":
            if update_user_setting(user_id, "caption_mode", "keep"):
                send_message(
                    chat_id,
                    "✅ *Mode: KEEP*\n\n"
                    "Original caption will be kept as-is.\n"
                    "Your header/footer (if set) will be added above/below it."
                )
            else:
                send_message(chat_id, "❌ Failed to update mode. Try again.")
            return

        # ── /remove ───────────────────────────────────────────────────────────
        if text == "/remove":
            if update_user_setting(user_id, "caption_mode", "remove"):
                send_message(
                    chat_id,
                    "✅ *Mode: REMOVE*\n\n"
                    "Original caption will be removed.\n"
                    "Only shortened links will be sent (with header/footer if set)."
                )
            else:
                send_message(chat_id, "❌ Failed to update mode. Try again.")
            return

        # ── Check API key before URL processing ───────────────────────────────
        api_key = get_user_api_key(user_id)
        if not api_key:
            send_message(
                chat_id,
                "⚠️ *API Key not set!*\n\n"
                "Set your Viralbox API key first:\n"
                "`/set_api YOUR_API_KEY`"
            )
            return

        # Load settings once
        settings = get_user_settings(user_id)

        # ── Plain text message with URLs ──────────────────────────────────────
        if text:
            urls = extract_urls(text)
            if not urls:
                return

            shortened_links = []
            for url in urls:
                short = shorten_url(url, api_key)
                if short:
                    save_to_db(url, short)
                    shortened_links.append(short)
                    print(f"{username} (text): {url} -> {short}")

            if not shortened_links:
                send_message(chat_id, "❌ Could not shorten URL(s). Check your API key or try again.")
                return

            final_text = build_caption(shortened_links, text, settings)
            send_message(chat_id, final_text)
            return

        # ── Media with caption ────────────────────────────────────────────────
        media_types = ["photo", "video", "document", "audio", "voice", "animation"]
        media_type = None
        file_id = None

        for t in media_types:
            if msg.get(t):
                media_type = t
                file_id = msg["photo"][-1]["file_id"] if t == "photo" else msg[t]["file_id"]
                break

        if media_type:
            original_caption = msg.get("caption", "")
            urls = extract_urls(original_caption)

            if not urls:
                # No URLs — just resend with header/footer if set, otherwise ignore
                if settings["header"] or settings["footer"]:
                    new_caption = build_caption([], original_caption, settings)
                    resend_media(chat_id, media_type, file_id, new_caption or original_caption)
                return

            shortened_links = []
            for url in urls:
                short = shorten_url(url, api_key)
                if short:
                    save_to_db(url, short)
                    shortened_links.append(short)
                    print(f"{username} (media): {url} -> {short}")

            if not shortened_links:
                send_message(chat_id, "❌ Could not shorten URL(s). Check your API key or try again.")
                return

            final_caption = build_caption(shortened_links, original_caption, settings)
            resend_media(chat_id, media_type, file_id, final_caption)
            return

    except Exception as e:
        print(f"Process error: {e}")


# ─────────────────────────────────────────────
# Start Server
# ─────────────────────────────────────────────
def run_server():
    server = HTTPServer(('0.0.0.0', PORT), WebhookHandler)
    print(f"Bot running on port {PORT}")
    server.serve_forever()


if __name__ == "__main__":
    setup_webhook()
    run_server()
