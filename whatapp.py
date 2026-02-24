import os
import threading
import time
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO
from flask_compress import Compress
from supabase import create_client, Client

# ================= LOAD ENV =================
load_dotenv()

# ================= CHECK RENDER =================
IS_RENDER = os.environ.get("RENDER") == "true"

# ================= FLASK APP =================
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super-secret")

CORS(app)
Compress(app)

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading"
)
# ================= SUPABASE CONFIG =================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise Exception("SUPABASE_URL और SUPABASE_KEY environment variables ज़रूरी हैं!")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
print("✅ Supabase क्लाइंट तैयार")

# ================= WHATSAPP NOTIFIER =================
notifier = None
if not IS_RENDER:
    try:
        from whatsapp_notifier import get_whatsapp_notifier
        notifier = get_whatsapp_notifier(headless=False)
        print("✅ WhatsApp notifier loaded")
    except Exception as e:
        print("WhatsApp notifier load error:", e)

# ================= WHATSAPP LISTENER =================
def start_whatsapp_listener():
    print("🟢 WhatsApp Polling Listener Started...")
    last_checked_id = 0

    while True:
        try:
            response = supabase.table("seat_bookings") \
                .select("*") \
                .gt("id", last_checked_id) \
                .order("id") \
                .execute()

            bookings = response.data

            if bookings:
                for booking in bookings:
                    booking_id = booking["id"]
                    print("📩 New Booking:", booking_id)

                    if notifier:
                        notifier.send_booking_confirmation_by_id(booking_id)

                    last_checked_id = booking_id

        except Exception as e:
            print("Listener Error:", e)

        time.sleep(5)

# ================= TEST ROUTE =================
@app.route("/")
def home():
    return "🚀 My Bus AI App Running"

# ================= BOOKING API EXAMPLE =================
@app.route("/book", methods=["POST"])
def book_seat():
    try:
        data = request.json

        response = supabase.table("seat_bookings").insert({
            "name": data.get("name"),
            "mobile": data.get("mobile"),
            "seat_number": data.get("seat_number"),
            "created_at": datetime.now().isoformat()
        }).execute()

        # 🔴 Realtime update
        socketio.emit("seat_update", {
            "seat_number": data.get("seat_number")
        })

        return jsonify({"status": "success", "data": response.data})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ================= SOCKET EVENT =================
@socketio.on("connect")
def handle_connect():
    print("🔌 Client Connected")

# ================= MAIN =================
if __name__ == "__main__":
    print("🚀 Starting My Bus AI Application...")

    if not IS_RENDER:
        listener_thread = threading.Thread(target=start_whatsapp_listener)
        listener_thread.daemon = True
        listener_thread.start()

    port = int(os.environ.get("PORT", 5001))
    port = 6000   
    # Windows-safe: debug=False, use_reloader=False
    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        debug=False,        # Debug off
        use_reloader=False  # Prevent double socket bind
    )
