import os
from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, request, redirect, session
from flask_socketio import SocketIO, emit
import psycopg2
from psycopg2.extras import RealDictCursor
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "secret123")

socketio = SocketIO(app, async_mode="eventlet", cors_allowed_origins="*")

def get_db():
    url = os.getenv("DATABASE_URL")
    if not url:
        return None

    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    try:
        return psycopg2.connect(url, cursor_factory=RealDictCursor, sslmode="require")
    except:
        return None

# ---------------- HOME ----------------
@app.route("/")
def home():
    return render_template("home.html")

# ---------------- REGISTER ----------------
@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        u = request.form["username"]
        p = generate_password_hash(request.form["password"])

        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO users (username,password) VALUES (%s,%s)", (u,p))
        conn.commit()
        cur.close(); conn.close()
        return redirect("/login")

    return render_template("register.html")

# ---------------- LOGIN ----------------
@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        u = request.form["username"]
        p = request.form["password"]

        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE username=%s", (u,))
        user = cur.fetchone()
        cur.close(); conn.close()

        if user and check_password_hash(user["password"], p):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect("/dashboard")

        return "Wrong login"

    return render_template("login.html")

# ---------------- DASHBOARD ----------------
@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect("/login")

    return render_template("dashboard.html", user=session["username"])

# ---------------- BUS ----------------
@app.route("/bus/<int:schedule_id>")
def bus(schedule_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM seats WHERE schedule_id=%s", (schedule_id,))
    seats = cur.fetchall()
    cur.close(); conn.close()

    return render_template("bus.html", seats=seats)

# ---------------- SOCKET ----------------
@socketio.on("book_seat")
def book_seat(data):
    seat_id = data["seat_id"]

    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE seats SET status='booked' WHERE id=%s", (seat_id,))
    conn.commit()
    cur.close(); conn.close()

    emit("seat_update", {"seat_id": seat_id}, broadcast=True)

# ---------------- LOGOUT ----------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# ---------------- MAIN ----------------
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000)