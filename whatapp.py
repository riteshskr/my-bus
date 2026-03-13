import os
import time
import threading
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO
from flask_compress import Compress
from supabase import create_client

load_dotenv()

# Environment Setup
IS_RENDER = os.environ.get("RENDER") == "true"
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# WhatsApp Notifier (Fixed Chrome Crash)
notifier = None
if not IS_RENDER:
    try:
        from whatsapp_notifier import get_whatsapp_notifier

        notifier = get_whatsapp_notifier(headless=False)
        print("✅ Local WhatsApp Notifier Started")
    except Exception as e:
        print("⚠️ WhatsApp Disabled (Error):", e)
        notifier = None
else:
    print("⚠️ Running on Render - WhatsApp Disabled")

# Flask + SocketIO Setup
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "bus-secret-2026")
CORS(app)
Compress(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")


# ================= WHATSAPP LISTENER =================
def background_whatsapp_listener():
    print("🟢 WhatsApp Automation Listener Started...")
    while True:
        try:
            if notifier:
                notifier.run_all()
                print("✅ WhatsApp Cycle Complete")
        except Exception as e:
            print("❌ Listener Error:", e)
        time.sleep(60)  # 1 min anti-spam


# ================= SOCKET EVENTS =================
@socketio.on('connect')
def handle_connect():
    print("🔌 Client Connected")
    emit('status', {'message': 'Connected to Bus Server'})


@socketio.on('disconnect')
def handle_disconnect():
    print("🔌 Client Disconnected")


# ================= API ROUTES =================
@app.route("/")
def home():
    return jsonify({
        "status": "🚀 Bus AI Server Running",
        "whatsapp": notifier is not None,
        "render": IS_RENDER
    })


@app.route("/bookings")
def get_bookings():
    try:
        bookings = supabase.table("seat_bookings").select("*").execute().data
        socketio.emit('seat_update', bookings, broadcast=True)
        return jsonify({"bookings": bookings})
    except:
        return jsonify({"error": "DB Error"}), 500


@app.route("/book", methods=["POST"])
def book_seat():
    data = request.json
    name = data.get("name")
    mobile = data.get("mobile")
    seat = data.get("seat_number")
    bus = data.get("bus_number")

    try:
        # Insert booking (whatsapp=null for pending)
        new_booking = supabase.table("seat_bookings").insert({
            "passenger_name": name,
            "mobile": mobile,
            "seat_number": seat,
            "bus_number": bus,
            "whatsapp": None  # Pending WhatsApp
        }).execute().data[0]

        print(f"🎫 NEW BOOKING: {name} - {mobile} - {seat}")

        # Broadcast live update
        socketio.emit('seat_update', [new_booking], broadcast=True)

        # Trigger WhatsApp (async)
        if notifier:
            threading.Thread(target=notifier.send_booking_confirmation).start()

        return jsonify({"status": "✅ Booked", "booking": new_booking})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ================= START SERVER =================
if __name__ == "__main__":
    print("🚀 Starting Bus AI Server...")

    # Start WhatsApp listener (local only)
    if notifier and not IS_RENDER:
        socketio.start_background_task(background_whatsapp_listener)

    # Dynamic port for Render
    port = int(os.environ.get("PORT", 10000))
    print(f"🌐 Server on http://localhost:{port}")
    port = 5000  # 👈 यहाँ fix कर दो
    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True
    )
