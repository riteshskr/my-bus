from dotenv import load_dotenv
import os, time, json, hashlib, random, threading, traceback, uuid
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
import schedule
import psutil
from concurrent.futures import ThreadPoolExecutor

# ================= CONFIG =================
load_dotenv()


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "secret-key-change-this-in-production")
    DATABASE_URL = os.getenv("DATABASE_URL")
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
    PORT = int(os.getenv("PORT", 10000))


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
    logger.info("✅ Redis connected")
except Exception as e:
    logger.warning(f"⚠️ Redis not available: {e}")

# ================= DB POOL =================
try:
    pool = ConnectionPool(
        conninfo=Config.DATABASE_URL,
        min_size=2,
        max_size=10,
        timeout=30
    )
    logger.info("✅ Database pool created")
except Exception as e:
    logger.error(f"❌ Database pool creation failed: {e}")
    pool = None


# ================= DB CONTEXT =================
@contextmanager
def get_database_connection():
    if not pool:
        raise Exception("Database pool not available")
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            yield conn, cur


def safe_database(func):
    @wraps(func)
    def wrapper(*a, **k):
        try:
            return func(*a, **k)
        except Exception as e:
            logger.error(f"Database error in {func.__name__}: {e}")
            traceback.print_exc()
            return jsonify({"error": "Database error"}), 500

    return wrapper


# ================= SOCKET =================
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="eventlet",
    logger=False,
    engineio_logger=False
)


# ================= INIT TABLES =================
def initialize_database():
    try:
        with get_database_connection() as (conn, cur):
            # Drop all existing tables (for clean start in development)
            cur.execute("""
            DROP TABLE IF EXISTS seat_bookings CASCADE;
            DROP TABLE IF EXISTS gps_logs CASCADE;
            DROP TABLE IF EXISTS schedules CASCADE;
            DROP TABLE IF EXISTS route_stations CASCADE;
            DROP TABLE IF EXISTS routes CASCADE;
            DROP TABLE IF EXISTS payments CASCADE;
            DROP TABLE IF EXISTS admins CASCADE;
            """)

            # Create tables
            tables = [
                """
                CREATE TABLE admins (
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE,
                    password TEXT,
                    role TEXT DEFAULT 'admin',
                    created_at TIMESTAMP DEFAULT NOW()
                )
                """,
                """
                CREATE TABLE routes (
                    id SERIAL PRIMARY KEY,
                    route_name TEXT,
                    distance_km INTEGER,
                    created_at TIMESTAMP DEFAULT NOW()
                )
                """,
                """
                CREATE TABLE route_stations (
                    id SERIAL PRIMARY KEY,
                    route_id INTEGER REFERENCES routes(id),
                    station_name TEXT,
                    station_order INTEGER,
                    lat DECIMAL(10, 6),
                    lng DECIMAL(10, 6),
                    created_at TIMESTAMP DEFAULT NOW()
                )
                """,
                """
                CREATE TABLE schedules (
                    id SERIAL PRIMARY KEY,
                    route_id INTEGER REFERENCES routes(id),
                    bus_name TEXT,
                    departure_time TIME,
                    total_seats INTEGER DEFAULT 40,
                    available_seats INTEGER DEFAULT 40,
                    current_lat DECIMAL(10, 6),
                    current_lng DECIMAL(10, 6),
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
                """,
                """
                CREATE TABLE seat_bookings (
                    id SERIAL PRIMARY KEY,
                    schedule_id INTEGER REFERENCES schedules(id),
                    seat_number INTEGER,
                    passenger_name TEXT,
                    mobile TEXT,
                    from_station TEXT,
                    to_station TEXT,
                    travel_date DATE,
                    fare INTEGER,
                    status TEXT DEFAULT 'confirmed',
                    payment_mode TEXT DEFAULT 'cash',
                    booking_hash TEXT UNIQUE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
                """,
                """
                CREATE TABLE gps_logs (
                    id SERIAL PRIMARY KEY,
                    schedule_id INTEGER,
                    latitude DECIMAL(10, 6),
                    longitude DECIMAL(10, 6),
                    speed DECIMAL(5, 2),
                    accuracy DECIMAL(5, 2),
                    timestamp TIMESTAMP DEFAULT NOW()
                )
                """,
                """
                CREATE TABLE payments (
                    id SERIAL PRIMARY KEY,
                    booking_id INTEGER,
                    order_id TEXT,
                    payment_id TEXT,
                    amount INTEGER,
                    status TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
                """
            ]

            for table_sql in tables:
                cur.execute(table_sql)

            # Insert default admin
            cur.execute("SELECT COUNT(*) FROM admins")
            if cur.fetchone()["count"] == 0:
                cur.execute(
                    "INSERT INTO admins(username,password,role) VALUES(%s,%s,%s)",
                    ("admin", generate_password_hash("admin123"), "admin")
                )

            # Insert sample routes
            cur.execute("SELECT COUNT(*) FROM routes")
            if cur.fetchone()["count"] == 0:
                # Add routes
                routes = [
                    ("Delhi → Jaipur", 280),
                    ("Jaipur → Udaipur", 400),
                    ("Mumbai → Pune", 150),
                    ("Bangalore → Chennai", 350)
                ]
                for route_name, distance in routes:
                    cur.execute(
                        "INSERT INTO routes(route_name, distance_km) VALUES(%s,%s) RETURNING id",
                        (route_name, distance)
                    )
                    route_id = cur.fetchone()["id"]

                    # Add stations for this route
                    if route_name == "Delhi → Jaipur":
                        stations = [
                            (route_id, "Delhi", 1, 28.6139, 77.2090),
                            (route_id, "Gurgaon", 2, 28.4595, 77.0266),
                            (route_id, "Jaipur", 3, 26.9124, 75.7873)
                        ]
                    elif route_name == "Jaipur → Udaipur":
                        stations = [
                            (route_id, "Jaipur", 1, 26.9124, 75.7873),
                            (route_id, "Ajmer", 2, 26.4499, 74.6399),
                            (route_id, "Udaipur", 3, 24.5854, 73.7125)
                        ]
                    else:
                        stations = [(route_id, "Start", 1, 0, 0), (route_id, "End", 2, 0, 0)]

                    for station in stations:
                        cur.execute(
                            "INSERT INTO route_stations(route_id, station_name, station_order, lat, lng) VALUES(%s,%s,%s,%s,%s)",
                            station
                        )

                    # Add schedules for this route
                    for i in range(1, 4):
                        cur.execute(
                            "INSERT INTO schedules(route_id, bus_name, departure_time) VALUES(%s,%s,%s)",
                            (route_id, f"Bus {i} - AC Sleeper", f"{7 + i}:00:00")
                        )

            conn.commit()
            logger.info("✅ Database initialized")

    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}")
        raise


if os.getenv("INIT_DB", "true").lower() == "true":
    try:
        initialize_database()
    except Exception as e:
        logger.warning(f"Database initialization skipped: {e}")


# ================= HELPER FUNCTIONS =================
def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_logged_in") or session.get("role") != "admin":
            return redirect("/login")
        return f(*args, **kwargs)

    return wrapper


def generate_booking_hash(data):
    return hashlib.sha256(
        f"{data['schedule_id']}_{data['seat_number']}_{data['date']}".encode()
    ).hexdigest()


# ================= ROUTES =================
@app.route("/")
@safe_database
def home():
    try:
        with get_database_connection() as (conn, cur):
            cur.execute("SELECT * FROM routes LIMIT 3")
            routes = cur.fetchall()
            cur.execute("SELECT COUNT(*) FROM schedules WHERE is_active = true")
            active_buses = cur.fetchone()["count"]
        return render_template(
            "index.html",
            routes=routes,
            active_buses=active_buses,
            today=date.today().isoformat()
        )
    except Exception as e:
        logger.error(f"Home route error: {e}")
        return "MyBus is starting up... Please refresh in a moment.", 503


@app.route("/login", methods=["GET", "POST"])
@safe_database
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        try:
            with get_database_connection() as (conn, cur):
                cur.execute("SELECT * FROM admins WHERE username = %s", (username,))
                user = cur.fetchone()

                if user and check_password_hash(user["password"], password):
                    session.clear()
                    session["user_logged_in"] = True
                    session["user_id"] = user["id"]
                    session["username"] = user["username"]
                    session["role"] = user["role"]
                    return redirect("/dashboard")
                else:
                    error = "Invalid username or password"
        except Exception as e:
            logger.error(f"Login error: {e}")
            error = "Database error"

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/dashboard")
@admin_required
@safe_database
def dashboard():
    try:
        with get_database_connection() as (conn, cur):
            cur.execute("SELECT COUNT(*) as total FROM seat_bookings WHERE DATE(created_at) = CURRENT_DATE")
            today_bookings = cur.fetchone()["total"]

            cur.execute("SELECT COUNT(*) as total FROM schedules WHERE is_active = true")
            active_buses = cur.fetchone()["total"]

            cur.execute(
                "SELECT COALESCE(SUM(fare), 0) as total FROM seat_bookings WHERE DATE(created_at) = CURRENT_DATE")
            today_revenue = cur.fetchone()["total"]

            cur.execute("""
                SELECT sb.*, s.bus_name 
                FROM seat_bookings sb
                JOIN schedules s ON sb.schedule_id = s.id
                ORDER BY sb.created_at DESC LIMIT 5
            """)
            recent_bookings = cur.fetchall()

        return render_template(
            "dashboard.html",
            today_bookings=today_bookings,
            active_buses=active_buses,
            today_revenue=today_revenue,
            recent_bookings=recent_bookings
        )
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        return "Database error", 500


@app.route("/health")
def health():
    try:
        # Check database
        db_status = "ok"
        try:
            with get_database_connection() as (conn, cur):
                cur.execute("SELECT 1")
        except Exception as e:
            db_status = f"error: {e}"

        return jsonify({
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "database": db_status,
            "redis": "connected" if redis_client else "not_configured",
            "cpu": psutil.cpu_percent(),
            "memory": psutil.virtual_memory().percent,
            "service": "MyBus"
        })
    except Exception as e:
        return jsonify({
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }), 500


# ================= SIMPLE GPS PROCESSOR =================
class GPSBatchProcessor:
    def __init__(self):
        self.batch = []
        self.batch_size = 5
        self.lock = threading.Lock()

    def add(self, data):
        with self.lock:
            self.batch.append(data)
            if len(self.batch) >= self.batch_size:
                self._flush()

    def _flush(self):
        if not self.batch:
            return

        batch_to_process = self.batch.copy()
        self.batch = []

        try:
            with get_database_connection() as (conn, cur):
                for data in batch_to_process:
                    cur.execute("""
                        INSERT INTO gps_logs (schedule_id, latitude, longitude, speed, accuracy)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (
                        data.get("sid"),
                        data.get("lat"),
                        data.get("lng"),
                        data.get("speed", 0),
                        data.get("accuracy", 0)
                    ))

                conn.commit()
                logger.info(f"Processed {len(batch_to_process)} GPS points")

        except Exception as e:
            logger.error(f"GPS processing error: {e}")


gps_processor = GPSBatchProcessor()


# ================= SOCKET EVENTS =================
@socketio.on("connect")
def handle_connect():
    logger.info(f"Client connected: {request.sid}")


@socketio.on("driver_gps")
def handle_driver_gps(data):
    try:
        gps_processor.add(data)
        emit("gps_ack", {"status": "received"})
    except Exception as e:
        logger.error(f"GPS error: {e}")


# ================= CLEANUP JOB =================
def cleanup_old_data():
    try:
        with get_database_connection() as (conn, cur):
            cur.execute("DELETE FROM gps_logs WHERE timestamp < NOW() - INTERVAL '7 days'")
            conn.commit()
            logger.info("✅ Cleaned up old GPS data")
    except Exception as e:
        logger.error(f"Cleanup error: {e}")


def scheduler_thread():
    while True:
        schedule.run_pending()
        time.sleep(60)


if os.getenv("ENABLE_SCHEDULER", "true").lower() == "true":
    schedule.every().day.at("02:00").do(cleanup_old_data)
    threading.Thread(target=scheduler_thread, daemon=True).start()


# ================= ERROR HANDLERS =================
@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


@app.errorhandler(500)
def internal_error(e):
    return render_template("500.html"), 500


# ================= RUN APPLICATION =================
if __name__ == "__main__":
    logger.info("🚀 Starting MyBus App...")
    logger.info(f"🌐 Port: {Config.PORT}")
    logger.info(f"📊 Redis: {'Connected' if redis_client else 'Not configured'}")

    # Use eventlet if available, otherwise use threading
    try:
        import eventlet

        socketio.run(
            app,
            host="0.0.0.0",
            port=Config.PORT,
            debug=os.getenv("DEBUG", "false").lower() == "true",
            use_reloader=False,
            log_output=True
        )
    except ImportError:
        logger.warning("eventlet not found, using threading")
        socketio.run(
            app,
            host="0.0.0.0",
            port=Config.PORT,
            debug=os.getenv("DEBUG", "false").lower() == "true",
            use_reloader=False,
            log_output=True,
            allow_unsafe_werkzeug=True
        )