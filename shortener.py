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

# Stats
bot_start_time = datetime.utcnow()
total_requests = 0
last_activity = datetime.utcnow()


# Webhook Handler
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


# Setup Webhook
def setup_webhook():
    webhook_url = f"{WEBHOOK_URL}/{BOT_TOKEN}"

    try:
        requests.post(f"{TELEGRAM_API}/deleteWebhook")
        print("🗑️  Deleted old webhook")

        response = requests.post(
            f"{TELEGRAM_API}/setWebhook",
            json={
                "url": webhook_url,
                "max_connections": 100,
                "allowed_updates": ["message"]
            }
        )

        if response.json().get("ok"):
            print(f"✅ Webhook set: {webhook_url}")
            print(f"🌐 Webhook mode - Platform friendly!")
        else:
            print(f"❌ Webhook failed: {response.text}")

    except Exception as e:
        print(f"❌ Webhook error: {e}")


# Get user API key from DB
def get_user_api_key(user_id):
    try:
        doc = user_apis_col.find_one({"userId": user_id})
        if doc:
            return doc.get("apiKey")
        return None
    except Exception as e:
        print(f"DB get API error: {e}")
        return None


# Save user API key to DB
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


# Helpers
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
    urls = re.findall(r'(https?://[^\s]+)', text)
    return urls


def send_message(chat_id, text):
    try:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        print(f"Send error: {e}")


def resend_media(chat_id, type, file_id, caption):
    endpoint = {
        "photo": "sendPhoto",
        "video": "sendVideo",
        "document": "sendDocument",
        "audio": "sendAudio",
        "voice": "sendVoice",
        "animation": "sendAnimation",
    }.get(type)

    if not endpoint:
        return

    try:
        payload = {"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"}
        payload[type] = file_id
        requests.post(f"{TELEGRAM_API}/{endpoint}", json=payload, timeout=10)
    except Exception as e:
        print(f"Media error: {e}")


# Process Message
def process_message(msg):
    try:
        chat_id = msg["chat"]["id"]
        user_id = msg["from"]["id"]
        username = msg["from"].get("username", "Unknown")
        first_name = msg["from"].get("first_name", "User")

        text = msg.get("text", "")

        # /start command
        if text.startswith("/start"):
            send_message(
                chat_id,
                f"👋 *Welcome {first_name}!*\n\n"
                f"🔗 Send any link to shorten.\n"
                f"📷 Or media with URL in caption.\n\n"
                f"⚙️ *Setup:* Use `/set_api YOUR_API_KEY` to set your Viralbox API key before using the bot.\n\n"
                f"Your ID: `{user_id}`"
            )
            return

        # /set_api command
        if text.startswith("/set_api"):
            parts = text.strip().split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                send_message(
                    chat_id,
                    "❌ *Usage:* `/set_api YOUR_API_KEY`\n\nExample:\n`/set_api 030cd48a49cc4002ec50aeb10f3dc03ca0e84ce5`"
                )
                return

            api_key = parts[1].strip()
            success = save_user_api_key(user_id, api_key)
            if success:
                send_message(
                    chat_id,
                    f"✅ *API Key saved successfully!*\n\nYou can now send links to shorten them."
                )
                print(f"✅ API key set by {username} ({user_id})")
            else:
                send_message(chat_id, "❌ Failed to save API key. Please try again.")
            return

        # Get user's API key
        api_key = get_user_api_key(user_id)
        if not api_key:
            send_message(
                chat_id,
                "⚠️ *API Key not set!*\n\nPlease set your Viralbox API key first:\n`/set_api YOUR_API_KEY`"
            )
            return

        # Text URL processing
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
                    print(f"✅ {username} (text): {short}")

            if not shortened_links:
                send_message(chat_id, "❌ Could not shorten the URL. Please check your API key or try again.")
                return

            response_parts = []
            for short_link in shortened_links:
                response_parts.append(f"✅ Video Link 👇\n{short_link}\n")

            response_parts.append("Join Backup channel ✅\n➤  https://t.me/+s1UUnoka8CdhYWVl")

            final_response = "\n".join(response_parts)
            send_message(chat_id, final_response)
            return

        # Media with caption
        media_types = ["photo", "video", "document", "audio", "voice", "animation"]
        media_type = None
        file_id = None

        for t in media_types:
            if msg.get(t):
                media_type = t
                file_id = msg["photo"][-1]["file_id"] if t == "photo" else msg[t]["file_id"]
                break

        if media_type:
            urls = extract_urls(msg.get("caption", ""))
            if not urls:
                return

            shortened_links = []
            for url in urls:
                short = shorten_url(url, api_key)
                if short:
                    save_to_db(url, short)
                    shortened_links.append(short)
                    print(f"✅ {username} (media): {short}")

            if not shortened_links:
                send_message(chat_id, "❌ Could not shorten the URL. Please check your API key or try again.")
                return

            caption_parts = []
            for short_link in shortened_links:
                caption_parts.append(f"✅ Video Link 👇\n{short_link}\n")

            caption_parts.append("Join Backup channel ✅\n➤  https://t.me/+s1UUnoka8CdhYWVl")

            final_caption = "\n".join(caption_parts)
            resend_media(chat_id, media_type, file_id, final_caption)
            return

    except Exception as e:
        print(f"Process error: {e}")


# Start Server
def run_server():
    server = HTTPServer(('0.0.0.0', PORT), WebhookHandler)
    print(f"🤖 Webhook server running on port {PORT}")
    server.serve_forever()


# Main
if __name__ == "__main__":
    setup_webhook()
    run_server()
