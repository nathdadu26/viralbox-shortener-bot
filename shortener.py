# shortener.py (Webhook Mode - Platform Friendly)
# No polling, no self-ping, no abuse!

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
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # Your Koyeb URL

# Admin User IDs
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = set([int(id.strip()) for id in ADMIN_IDS_STR.split(",") if id.strip()])

if not BOT_TOKEN or not MONGODB_URI or not API_KEY:
    raise RuntimeError("BOT_TOKEN, MONGODB_URI and API_KEY must be in .env")

if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS must be set in .env")

if not WEBHOOK_URL:
    raise RuntimeError("WEBHOOK_URL must be set in .env")

# ------------------ DB SETUP ------------------ #
client = MongoClient(MONGODB_URI, maxPoolSize=50)
db = client[DB_NAME]
links_col = db["links"]

# ------------------ STATS ------------------ #
bot_start_time = datetime.utcnow()
total_requests = 0
last_activity = datetime.utcnow()


# ------------------ WEBHOOK HANDLER ------------------ #
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
            # Webhook endpoint
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            
            try:
                update = json.loads(post_data.decode('utf-8'))
                
                # Process in background thread
                if "message" in update:
                    total_requests += 1
                    last_activity = datetime.utcnow()
                    Thread(target=process_message, args=(update["message"],)).start()
                
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"ok": true}')
                
            except Exception as e:
                print(f"Error processing webhook: {e}")
                self.send_response(500)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        # Only log errors
        if "error" in format.lower():
            print(format % args)


# ------------------ WEBHOOK SETUP ------------------ #
def setup_webhook():
    """Set webhook on Telegram"""
    webhook_url = f"{WEBHOOK_URL}/{BOT_TOKEN}"
    
    try:
        # Delete existing webhook first
        requests.post(f"{TELEGRAM_API}/deleteWebhook")
        print("🗑️  Deleted old webhook")
        
        # Set new webhook
        response = requests.post(
            f"{TELEGRAM_API}/setWebhook",
            json={
                "url": webhook_url,
                "max_connections": 100,
                "allowed_updates": ["message"]
            }
        )
        
        if response.json().get("ok"):
            print(f"✅ Webhook set successfully: {webhook_url}")
            
            # Set bot commands
            commands = [
                {"command": "start", "description": "Start the bot"},
                {"command": "myid", "description": "Get your User ID"},
                {"command": "status", "description": "Check bot status"}
            ]
            requests.post(f"{TELEGRAM_API}/setMyCommands", json={"commands": commands})
            print("✅ Bot commands set")
            
            # Send notification to admins
            send_startup_notification()
            
        else:
            print(f"❌ Webhook setup failed: {response.text}")
            
    except Exception as e:
        print(f"❌ Error setting webhook: {e}")


def send_startup_notification():
    """Send startup notification to admins"""
    try:
        for admin_id in ADMIN_IDS:
            message = (
                "🚀 *Bot Restarted Successfully!*\n\n"
                f"⏰ Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
                f"👥 Authorized Admins: {len(ADMIN_IDS)}\n"
                f"🌐 Mode: Webhook (Platform Friendly)\n\n"
                "Bot is ready! Send any link to shorten. 🔗"
            )
            requests.post(
                f"{TELEGRAM_API}/sendMessage",
                json={
                    "chat_id": admin_id,
                    "text": message,
                    "parse_mode": "Markdown"
                }
            )
        print(f"✅ Startup notification sent to {len(ADMIN_IDS)} admin(s)")
    except Exception as e:
        print(f"⚠️  Failed to send notification: {e}")


# ------------------ ADMIN CHECK ------------------ #
def is_admin(user_id):
    return user_id in ADMIN_IDS


# ------------------ HELPERS ------------------ #
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
        print(f"DB Error: {e}")


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
        print(f"Error sending message: {e}")


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
        print(f"Error sending media: {e}")


# ------------------ PROCESS MESSAGE ------------------ #
def process_message(msg):
    try:
        chat_id = msg["chat"]["id"]
        user_id = msg["from"]["id"]
        username = msg["from"].get("username", "Unknown")
        first_name = msg["from"].get("first_name", "User")

        # --- ADMIN CHECK ---
        if not is_admin(user_id):
            print(f"⛔ Unauthorized: {username} (ID: {user_id})")
            send_message(
                chat_id,
                "❌ *Access Denied!*\n\n"
                f"Your ID: `{user_id}`\n"
                "Contact bot owner for access."
            )
            return

        # --- /start ---
        if msg.get("text", "").startswith("/start"):
            send_message(
                chat_id,
                f"👋 *Welcome {first_name}!*\n\n"
                f"🔗 Send any link to shorten.\n"
                f"📷 Or send media with URL in caption.\n\n"
                f"Your ID: `{user_id}`"
            )
            return

        # --- /myid ---
        if msg.get("text", "").startswith("/myid"):
            send_message(chat_id, f"Your User ID: `{user_id}`")
            return

        # --- /status ---
        if msg.get("text", "").startswith("/status"):
            global total_requests
            uptime = (datetime.utcnow() - bot_start_time).total_seconds()
            hours = int(uptime // 3600)
            minutes = int((uptime % 3600) // 60)
            
            status_msg = (
                f"📊 *Bot Status*\n\n"
                f"✅ Status: Online (Webhook)\n"
                f"⏰ Uptime: {hours}h {minutes}m\n"
                f"👥 Admins: {len(ADMIN_IDS)}\n"
                f"📨 Total Requests: {total_requests}\n"
                f"📅 Last Activity: {last_activity.strftime('%H:%M:%S')}"
            )
            send_message(chat_id, status_msg)
            return

        # --- TEXT URL ---
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
            print(f"✅ {username}: {url} → {short}")
            return

        # --- MEDIA URL ---
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
            print(f"✅ {username} (media): {url} → {short}")
            return

    except Exception as e:
        print(f"Error: {e}")


# ------------------ START SERVER ------------------ #
def run_server():
    server = HTTPServer(('0.0.0.0', PORT), WebhookHandler)
    print(f"🤖 Webhook server running on port {PORT}")
    print(f"👥 Admins: {ADMIN_IDS}")
    print(f"🌐 Webhook mode - No polling, platform friendly!")
    server.serve_forever()


# ------------------ MAIN ------------------ #
if __name__ == "__main__":
    # Setup webhook
    setup_webhook()
    
    # Start server
    run_server()
