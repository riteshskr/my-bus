import os
from flask import Flask, render_template, request, jsonify, session, redirect
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor
from werkzeug.security import generate_password_hash, check_password_hash
import razorpay

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "secret123")
socketio = SocketIO(app, cors_allowed_origins="*")

DATABASE_URL = os.getenv("DATABASE_URL")

RAZORPAY_KEY = os.getenv("RAZORPAY_KEY")
RAZORPAY_SECRET = os.getenv("RAZORPAY_SECRET")
RAZORPAY_ENABLED = bool(RAZORPAY_KEY and RAZORPAY_SECRET)

razor_client = None
if RAZORPAY_ENABLED:
    razor_client = razorpay.Client(auth=(RAZORPAY_KEY, RAZORPAY_SECRET))


def get_db():
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(
        url,
        cursor_factory=RealDictCursor,
        sslmode="require"
    )


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/register", methods=["POST"])
def register():
    data = request.json
    username = data["username"]
    password = generate_password_hash(data["password"])

    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO users (username, password) VALUES (%s, %s)",
                (username, password))
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"status": "ok"})


@app.route("/login", methods=["POST"])
def login():
    data = request.json
    username = data["username"]
    password = data["password"]

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username=%s", (username,))
    user = cur.fetchone()
    cur.close()
    conn.close()

    if user and check_password_hash(user["password"], password):
        session["user"] = username
        return jsonify({"status": "ok"})
    return jsonify({"status": "fail"}), 401


@app.route("/book", methods=["POST"])
def book():
    if "user" not in session:
        return jsonify({"error": "login required"}), 403

    seat = request.json["seat"]
    user = session["user"]

    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO bookings (username, seat) VALUES (%s, %s)",
                (user, seat))
    conn.commit()
    cur.close()
    conn.close()

    socketio.emit("seat_update", {"seat": seat, "user": user})
    return jsonify({"status": "booked"})


@app.route("/payment", methods=["POST"])
def payment():
    if not RAZORPAY_ENABLED:
        return jsonify({"error": "payments disabled"}), 400

    order = razor_client.order.create({
        "amount": 10000,
        "currency": "INR",
        "payment_capture": 1
    })
    return jsonify(order)


@socketio.on("connect")
def handle_connect():
    print("Client connected")


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0",
                 port=int(os.environ.get("PORT", 10000)))