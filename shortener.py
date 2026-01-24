# shortener.py (Webhook Mode - Final Version)

import os
import json
from urllib.parse import urlparse
from pymongo import MongoClient
from dotenv import load_dotenv
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
import requests

load_dotenv()

BOT_TOKEN = os.getenv("SHORTENER_BOT_TOKEN")
API_KEY = os.getenv("API_KEY")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

MONGODB_URI = os.getenv("MONGODB_URI")
DB_NAME = os.getenv("MONGO_DB_NAME", "viralbox_db")
PORT = int(os.getenv("PORT", "8000"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = set([int(id.strip()) for id in ADMIN_IDS_STR.split(",") if id.strip()])

if not BOT_TOKEN or not MONGODB_URI or not API_KEY:
    raise RuntimeError("BOT_TOKEN, MONGODB_URI and API_KEY must be set")

if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS must be set")

if not WEBHOOK_URL:
    raise RuntimeError("WEBHOOK_URL must be set")

# DB Setup
client = MongoClient(MONGODB_URI, maxPoolSize=50)
db = client[DB_NAME]
links_col = db["links"]

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
                "admins": len(ADMIN_IDS),
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
            
            commands = [
                {"command": "start", "description": "Start the bot"},
                {"command": "myid", "description": "Get your User ID"},
                {"command": "status", "description": "Bot status"}
            ]
            requests.post(f"{TELEGRAM_API}/setMyCommands", json={"commands": commands})
            print("✅ Commands set")
            
            send_startup_notification()
        else:
            print(f"❌ Webhook failed: {response.text}")
            
    except Exception as e:
        print(f"❌ Webhook error: {e}")


def send_startup_notification():
    try:
        for admin_id in ADMIN_IDS:
            message = (
                "🚀 *Bot Restarted!*\n\n"
                f"⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
                f"👥 Admins: {len(ADMIN_IDS)}\n"
                f"🌐 Mode: Webhook\n\n"
                "Ready to shorten links! 🔗"
            )
            requests.post(
                f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": admin_id, "text": message, "parse_mode": "Markdown"}
            )
        print(f"✅ Notified {len(ADMIN_IDS)} admin(s)")
    except Exception as e:
        print(f"⚠️  Notification failed: {e}")


# Admin Check
def is_admin(user_id):
    return user_id in ADMIN_IDS


# Helpers
def shorten_url(longURL):
    try:
        api = f"https://viralbox.in/api?api={API_KEY}&url={requests.utils.requote_uri(longURL)}"
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


def extract_url(text):
    if not text:
        return None
    import re
    m = re.search(r"(https?://[^\s]+)", text)
    return m.group(0) if m else None


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
        send_message(chat_id, caption)
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

        if not is_admin(user_id):
            print(f"⛔ Unauthorized: {username} ({user_id})")
            send_message(
                chat_id,
                f"❌ *Access Denied!*\n\nYour ID: `{user_id}`\nContact bot owner."
            )
            return

        # /start
        if msg.get("text", "").startswith("/start"):
            send_message(
                chat_id,
                f"👋 *Welcome {first_name}!*\n\n"
                f"🔗 Send any link to shorten.\n"
                f"📷 Or media with URL in caption.\n\n"
                f"Your ID: `{user_id}`"
            )
            return

        # /myid
        if msg.get("text", "").startswith("/myid"):
            send_message(chat_id, f"Your ID: `{user_id}`")
            return

        # /status
        if msg.get("text", "").startswith("/status"):
            global total_requests
            uptime = (datetime.utcnow() - bot_start_time).total_seconds()
            h = int(uptime // 3600)
            m = int((uptime % 3600) // 60)
            
            send_message(
                chat_id,
                f"📊 *Status*\n\n"
                f"✅ Online (Webhook)\n"
                f"⏰ Uptime: {h}h {m}m\n"
                f"👥 Admins: {len(ADMIN_IDS)}\n"
                f"📨 Requests: {total_requests}\n"
                f"📅 Last: {last_activity.strftime('%H:%M:%S')}"
            )
            return

        # Text URL
        if msg.get("text"):
            url = extract_url(msg["text"])
            if not url:
                send_message(chat_id, "❌ Send a valid link.")
                return

            short = shorten_url(url)
            if not short:
                send_message(chat_id, "❌ Shortening failed.")
                return

            save_to_db(url, short)
            send_message(chat_id, f"✅ *Shortened:*\n\n{short}")
            print(f"✅ {username}: {short}")
            return

        # Media URL
        media_types = ["photo", "video", "document", "audio", "voice", "animation"]
        media_type = None
        file_id = None

        for t in media_types:
            if msg.get(t):
                media_type = t
                file_id = msg["photo"][-1]["file_id"] if t == "photo" else msg[t]["file_id"]
                break

        if media_type:
            url = extract_url(msg.get("caption", ""))
            if not url:
                send_message(chat_id, "❌ Add URL in caption.")
                return

            short = shorten_url(url)
            if not short:
                send_message(chat_id, "❌ Failed.")
                return

            save_to_db(url, short)
            resend_media(chat_id, media_type, file_id, f"✅ *Short:*\n\n{short}")
            print(f"✅ {username} (media): {short}")
            return

    except Exception as e:
        print(f"Process error: {e}")


# Start Server
def run_server():
    server = HTTPServer(('0.0.0.0', PORT), WebhookHandler)
    print(f"🤖 Webhook server running on port {PORT}")
    print(f"👥 Admins: {ADMIN_IDS}")
    print(f"🌐 Webhook mode - Platform friendly!")
    server.serve_forever()


# Main
if __name__ == "__main__":
    setup_webhook()
    run_server()
