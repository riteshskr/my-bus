from dotenv import load_dotenv
import os, time, json, hashlib, random, threading, traceback
from datetime import date, datetime
from functools import wraps
from contextlib import contextmanager

from flask import Flask, request, jsonify, render_template, redirect, session, g
from flask_socketio import SocketIO, emit
from flask_compress import Compress
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

import redis
import logging
import uuid
import schedule
import psutil
import razorpay
from concurrent.futures import ThreadPoolExecutor

# ================= CONFIG =================
load_dotenv()

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "secret")
    DATABASE_URL = os.getenv("DATABASE_URL")
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

# ================= APP =================
app = Flask(__name__)
app.secret_key = Config.SECRET_KEY
Compress(app)
CORS(app)
csrf = CSRFProtect(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================= REDIS =================
redis_client = None
try:
    redis_client = redis.from_url(Config.REDIS_URL, decode_responses=True)
except:
    pass

# ================= DB POOL =================
pool = ConnectionPool(
    conninfo=Config.DATABASE_URL,
    min_size=2,
    max_size=10
)

# ================= DB CONTEXT =================
@contextmanager
def get_database_connection():
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            yield conn, cur

def safe_database(func):
    @wraps(func)
    def wrapper(*a, **k):
        try:
            return func(*a, **k)
        except Exception as e:
            traceback.print_exc()
            return "Database error", 500
    return wrapper

# ================= SOCKET =================
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ================= INIT TABLES =================
def initialize_database():
    with get_database_connection() as (conn, cur):
        cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            id SERIAL PRIMARY KEY,
            username TEXT,
            password TEXT,
            role TEXT
        )
        """)
        conn.commit()

        cur.execute("SELECT COUNT(*) FROM admins")
        if cur.fetchone()["count"] == 0:
            cur.execute(
                "INSERT INTO admins(username,password,role) VALUES(%s,%s,%s)",
                ("admin", generate_password_hash("admin123"), "admin")
            )
            conn.commit()

initialize_database()

# ================= ROUTES =================

@app.route("/")
def home():
    return "MYBUS RUNNING 🚍"

@app.route("/login", methods=["GET","POST"])
@safe_database
def login():
    if request.method == "POST":
        u = request.form["username"]
        p = request.form["password"]
        with get_database_connection() as (conn, cur):
            cur.execute("SELECT * FROM admins WHERE username=%s",(u,))
            user = cur.fetchone()
            if user and check_password_hash(user["password"], p):
                session["user_logged_in"] = True
                session["role"] = user["role"]
                return redirect("/dashboard")
    return "Login page"

@app.route("/dashboard")
def dashboard():
    if not session.get("user_logged_in"):
        return redirect("/login")
    return "Admin dashboard"

# ================= GPS BATCH =================

class GPSBatchProcessor:
    def __init__(self):
        self.batch = []
        self.executor = ThreadPoolExecutor(2)

    def add(self, data):
        self.batch.append(data)
        if len(self.batch) >= 10:
            self.flush()

    def flush(self):
        data = self.batch.copy()
        self.batch = []
        self.executor.submit(self.process, data)

    def process(self, batch):
        with get_database_connection() as (conn, cur):
            for b in batch:
                cur.execute("""
                INSERT INTO gps_logs(schedule_id, latitude, longitude)
                VALUES(%s,%s,%s)
                """,(b["sid"], b["lat"], b["lng"]))
            conn.commit()

gps_processor = GPSBatchProcessor()

# ================= SOCKET EVENTS =================

@socketio.on("driver_gps")
def handle_driver_gps(data):
    gps_processor.add(data)

# ================= METRICS =================

@app.route("/health")
def health():
    with get_database_connection() as (conn, cur):
        cur.execute("SELECT 1")
    return jsonify({
        "db":"ok",
        "cpu": psutil.cpu_percent()
    })

# ================= CLEANUP JOB =================

def cleanup_old_data():
    with get_database_connection() as (conn, cur):
        cur.execute("DELETE FROM gps_logs WHERE timestamp < NOW() - INTERVAL '30 days'")
        conn.commit()

def scheduler():
    while True:
        schedule.run_pending()
        time.sleep(60)

schedule.every().day.at("02:00").do(cleanup_old_data)
threading.Thread(target=scheduler, daemon=True).start()

# ================= RUN =================

if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=10000,
        debug=True,
        allow_unsafe_werkzeug=True
    )
