# shortener.py (Admin-Only Concurrent Version with Health Check)
# Only authorized admins can use this bot

import os
import time
import requests
from urllib.parse import urlparse
from pymongo import MongoClient
from dotenv import load_dotenv
from datetime import datetime
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor

load_dotenv()

BOT_TOKEN = os.getenv("SHORTENER_BOT_TOKEN")
API_KEY = os.getenv("API_KEY")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

MONGODB_URI = os.getenv("MONGODB_URI")
DB_NAME = os.getenv("MONGO_DB_NAME", "viralbox_db")
HEALTH_CHECK_PORT = int(os.getenv("PORT", "8000"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "10"))

# Admin User IDs (comma-separated in .env)
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = set([int(id.strip()) for id in ADMIN_IDS_STR.split(",") if id.strip()])

if not BOT_TOKEN or not MONGODB_URI or not API_KEY:
    raise RuntimeError("BOT_TOKEN, MONGODB_URI and API_KEY must be in .env")

if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS must be set in .env")

# ------------------ DB SETUP ------------------ #
client = MongoClient(MONGODB_URI, maxPoolSize=50)
db = client[DB_NAME]
links_col = db["links"]


# ------------------ HEALTH CHECK SERVER ------------------ #
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health' or self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            response = {
                "status": "healthy",
                "bot": "shortener-admin",
                "timestamp": datetime.utcnow().isoformat(),
                "workers": MAX_WORKERS,
                "admins": len(ADMIN_IDS)
            }
            self.wfile.write(str(response).encode())
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        pass


def start_health_server():
    server = HTTPServer(('0.0.0.0', HEALTH_CHECK_PORT), HealthCheckHandler)
    print(f"✅ Health check server running on port {HEALTH_CHECK_PORT}")
    server.serve_forever()


# ------------------ ADMIN CHECK ------------------ #

def is_admin(user_id):
    """Check if user is admin"""
    return user_id in ADMIN_IDS


# ------------------ HELPERS ------------------ #

def shorten_url(longURL):
    """Shorten URL using ViralBox API"""
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
    """Save link to database"""
    try:
        links_col.insert_one({
            "longURL": longURL,
            "shortURL": shortURL,
            "created_at": datetime.utcnow()
        })
    except Exception as e:
        print(f"DB Error: {e}")


def extract_url(text):
    """Extract URL from text"""
    if not text:
        return None
    import re
    m = re.search(r"(https?://[^\s]+)", text)
    return m.group(0) if m else None


def send_message(chat_id, text):
    """Send text message"""
    try:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10
        )
    except Exception as e:
        print(f"Error sending message: {e}")


def resend_media(chat_id, type, file_id, caption):
    """Resend media with caption"""
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
        payload = {"chat_id": chat_id, "caption": caption}
        payload[type] = file_id
        requests.post(f"{TELEGRAM_API}/{endpoint}", json=payload, timeout=10)
    except Exception as e:
        print(f"Error sending media: {e}")


# ------------------ PROCESS MESSAGE ------------------ #

def process_message(msg):
    """Process a single message (runs in thread pool)"""
    try:
        chat_id = msg["chat"]["id"]
        user_id = msg["from"]["id"]
        username = msg["from"].get("username", "Unknown")
        first_name = msg["from"].get("first_name", "User")

        # --- ADMIN CHECK (CRITICAL) ---
        if not is_admin(user_id):
            print(f"⛔ Unauthorized access attempt by {username} (ID: {user_id})")
            send_message(
                chat_id,
                "❌ Access Denied!\n\n"
                "⚠️ This bot is for authorized admins only.\n"
                f"Your User ID: `{user_id}`\n\n"
                "Contact the bot owner to get access."
            )
            return

        # --- /start ---
        if msg.get("text", "").startswith("/start"):
            send_message(
                chat_id,
                f"👋 Welcome Admin {first_name}!\n\n"
                f"🔗 Send me any link to shorten.\n"
                f"📷 You can also send media with URL in caption.\n\n"
                f"Your User ID: `{user_id}`"
            )
            return

        # --- /myid ---
        if msg.get("text", "").startswith("/myid"):
            send_message(chat_id, f"Your User ID: `{user_id}`")
            return

        # --- TEXT URL ---
        if msg.get("text"):
            url = extract_url(msg["text"])
            if not url:
                send_message(chat_id, "❌ Please send a valid link.")
                return

            short = shorten_url(url)
            if not short:
                send_message(chat_id, "❌ URL shortener failed. Please try again.")
                return

            save_to_db(url, short)
            send_message(chat_id, f"✅ Shortened URL:\n\n{short}")
            print(f"✅ Link shortened by {username} (ID: {user_id})")
            return

        # --- MEDIA URL ---
        media_types = ["photo", "video", "document", "audio", "voice", "animation"]
        media_type = None
        file_id = None

        for t in media_types:
            if msg.get(t):
                media_type = t
                if t == "photo":
                    file_id = msg["photo"][-1]["file_id"]
                else:
                    file_id = msg[t]["file_id"]
                break

        if media_type:
            url = extract_url(msg.get("caption", ""))
            if not url:
                send_message(chat_id, "❌ Caption me valid link send kare.")
                return

            short = shorten_url(url)
            if not short:
                send_message(chat_id, "❌ Shortener failed.")
                return

            save_to_db(url, short)
            resend_media(chat_id, media_type, file_id, f"✅ Short Link:\n\n{short}")
            print(f"✅ Media link shortened by {username} (ID: {user_id})")
            return

    except Exception as e:
        print(f"Error processing message: {e}")
        try:
            send_message(msg["chat"]["id"], "❌ An error occurred. Please try again.")
        except:
            pass


# ------------------ BOT POLLING LOOP (CONCURRENT) ------------------ #

def polling_loop():
    print(f"🤖 Admin-Only Shortener Bot Running")
    print(f"👥 Authorized Admins: {len(ADMIN_IDS)}")
    print(f"⚡ Concurrent Workers: {MAX_WORKERS}")
    print(f"🔐 Admin IDs: {ADMIN_IDS}")
    offset = None
    
    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

    while True:
        try:
            r = requests.get(
                f"{TELEGRAM_API}/getUpdates",
                params={"timeout": 50, "offset": offset},
                timeout=60
            ).json()

            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                if "message" in upd:
                    executor.submit(process_message, upd["message"])

        except Exception as e:
            print("Polling Error:", e)
            time.sleep(2)


# ------------------ START BOT ------------------ #

if __name__ == "__main__":
    health_thread = Thread(target=start_health_server, daemon=True)
    health_thread.start()
    
    polling_loop()
