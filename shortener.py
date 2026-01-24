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
            print(f"👥 Admins: {len(ADMIN_IDS)}")
            print(f"🌐 Webhook mode - Platform friendly!")
        else:
            print(f"❌ Webhook failed: {response.text}")
            
    except Exception as e:
        print(f"❌ Webhook error: {e}")


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

        # Only admins can use the bot - silently ignore others
        if not is_admin(user_id):
            print(f"⛔ Unauthorized: {username} ({user_id})")
            return

        # /start command
        if msg.get("text", "").startswith("/start"):
            send_message(
                chat_id,
                f"👋 *Welcome {first_name}!*\n\n"
                f"🔗 Send any link to shorten.\n"
                f"📷 Or media with URL in caption.\n\n"
                f"Your ID: `{user_id}`"
            )
            return

        # Text URL processing
        if msg.get("text"):
            urls = extract_urls(msg["text"])
            if not urls:
                return

            shortened_links = []
            for url in urls:
                short = shorten_url(url)
                if short:
                    save_to_db(url, short)
                    shortened_links.append(short)
                    print(f"✅ {username} (text): {short}")

            if not shortened_links:
                return

            # Build response with formatted links
            response_parts = []
            for short_link in shortened_links:
                response_parts.append(f"✅ Video Link 👇\n{short_link}\n")
            
            response_parts.append("Join Backup channel ✅\n➤  https://t.me/+oI8Y9HQJV1A4ZDg1")
            
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
                short = shorten_url(url)
                if short:
                    save_to_db(url, short)
                    shortened_links.append(short)
                    print(f"✅ {username} (media): {short}")

            if not shortened_links:
                return

            # Build caption with formatted links
            caption_parts = []
            for short_link in shortened_links:
                caption_parts.append(f"✅ Video Link 👇\n{short_link}\n")
            
            caption_parts.append("Join Backup channel ✅\n➤  https://t.me/+oI8Y9HQJV1A4ZDg1")
            
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
