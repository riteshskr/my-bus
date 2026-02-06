# ================= IMPORT SECTION =================
from dotenv import load_dotenv
import os
import json
import time
import hashlib
from datetime import date, datetime, timedelta
from functools import wraps
from contextlib import contextmanager
from flask import Flask, request, jsonify, render_template, redirect, session, g, url_for
from flask_socketio import SocketIO, emit
from flask_compress import Compress
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
import atexit
import razorpay
import random
import traceback
import redis
from concurrent.futures import ThreadPoolExecutor
import logging
import uuid
import structlog
import schedule
import threading
import psutil

# ================= CONFIGURATION =================
load_dotenv()

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "super-secret-key")
    DATABASE_URL = os.getenv("DATABASE_URL")
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
    RATE_LIMITS = os.getenv("RATE_LIMITS", "200 per minute,50 per second")
    GPS_BATCH_SIZE = int(os.getenv("GPS_BATCH_SIZE", 50))
    GPS_FLUSH_INTERVAL = int(os.getenv("GPS_FLUSH_INTERVAL", 5))
    SOCKETIO_PING_TIMEOUT = int(os.getenv("SOCKETIO_PING_TIMEOUT", 60))
    SOCKETIO_PING_INTERVAL = int(os.getenv("SOCKETIO_PING_INTERVAL", 25))
    RAZORPAY_ENABLED = os.getenv("RAZORPAY_ENABLED", "false").lower() == "true"
    RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
    RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")

# ================= LOGGING SETUP =================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================= REDIS SETUP =================
redis_client = None
try:
    redis_client = redis.from_url(Config.REDIS_URL, decode_responses=True)
    logger.info("✅ Redis connected successfully")
except Exception as e:
    logger.warning(f"⚠️ Redis not available: {e}")

# ================= RAZORPAY SETUP =================
razorpay_client = None
if Config.RAZORPAY_ENABLED and Config.RAZORPAY_KEY_ID and Config.RAZORPAY_KEY_SECRET:
    razorpay_client = razorpay.Client(auth=(
        Config.RAZORPAY_KEY_ID,
        Config.RAZORPAY_KEY_SECRET
    ))

# ================= FLASK APP SETUP =================
app = Flask(__name__)
app.secret_key = Config.SECRET_KEY
Compress(app)

csrf = CSRFProtect(app)

CORS(app, resources={
    r"/api/*": {"origins": ["*"]},
    r"/driver/*": {"origins": ["*"]}
})

# ================= RATE LIMITING =================
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=[Config.RATE_LIMITS],
    storage_uri=Config.REDIS_URL if redis_client else "memory://"
)

# ================= SOCKET.IO SETUP =================
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    logger=True,
    engineio_logger=True,
    ping_timeout=Config.SOCKETIO_PING_TIMEOUT,
    ping_interval=Config.SOCKETIO_PING_INTERVAL,
    max_http_buffer_size=10_000_000,
    message_queue=Config.REDIS_URL if redis_client else None
)

# ================= DATABASE CONNECTION POOL =================
def create_connection_pool():
    max_retries = 5
    for i in range(max_retries):
        try:
            pool = ConnectionPool(
                conninfo=Config.DATABASE_URL,
                min_size=5,
                max_size=20,
                timeout=30,
                max_idle=300,
                max_lifetime=3600
            )
            logger.info("✅ Database pool created successfully")
            return pool
        except Exception as e:
            logger.error(f"❌ Pool creation attempt {i + 1} failed: {e}")
            time.sleep(2 ** i)
    raise Exception("Failed to create database pool")

pool = create_connection_pool()

# ================= DATABASE HELPER FUNCTIONS =================
@contextmanager
def get_database_connection(retry_count=3):
    conn = None
    cur = None
    for attempt in range(retry_count):
        try:
            conn = pool.getconn()
            cur = conn.cursor(row_factory=dict_row)
            yield conn, cur
            return
        except Exception as e:
            logger.error(f"DB attempt {attempt + 1} failed: {e}")
            if conn:
                try:
                    conn.rollback()
                except:
                    pass
            if attempt < retry_count - 1:
                time.sleep(0.1 * (2 ** attempt))
            else:
                raise e
        finally:
            if cur:
                try:
                    cur.close()
                except:
                    pass
            if conn:
                try:
                    pool.putconn(conn)
                except Exception as e:
                    logger.error(f"Error returning connection: {e}")

def safe_database(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Database error in {func.__name__}: {str(e)}")
            traceback.print_exc()
            return jsonify({"error": "Service temporarily unavailable"}), 503
    return wrapper

# ================= GPS BATCH PROCESSOR =================
class GPSBatchProcessor:
    def __init__(self):
        self.batch = []
        self.last_flush = time.time()
        self.lock = False
        self.executor = ThreadPoolExecutor(max_workers=3)

    def add(self, data):
        data_hash = hashlib.md5(
            f"{data['sid']}_{data['lat']}_{data['lng']}_{int(time.time() / 10)}".encode()
        ).hexdigest()
        data['_hash'] = data_hash
        data['_timestamp'] = time.time()
        self.batch.append(data)
        if len(self.batch) >= Config.GPS_BATCH_SIZE or (time.time() - self.last_flush) > Config.GPS_FLUSH_INTERVAL:
            self.flush()

    def flush(self):
        if self.lock or not self.batch:
            return
        self.lock = True
        batch_to_process = self.batch.copy()
        self.batch = []
        try:
            self.executor.submit(self._process_batch, batch_to_process)
        finally:
            self.lock = False
            self.last_flush = time.time()

    def _process_batch(self, batch):
        try:
            with get_database_connection() as (conn, cur):
                values = [(
                    b['sid'], b['lat'], b['lng'],
                    b.get('speed', 0), b.get('accuracy', 0),
                    datetime.fromtimestamp(b['_timestamp'])
                ) for b in batch]

                cur.executemany(
                    """
                    INSERT INTO gps_logs (schedule_id, latitude, longitude, speed, accuracy, timestamp)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    values
                )

                latest_positions = {}
                for b in batch:
                    sid = b['sid']
                    if sid not in latest_positions or b['_timestamp'] > latest_positions[sid]['_timestamp']:
                        latest_positions[sid] = b

                for sid, pos in latest_positions.items():
                    cur.execute("""
                        UPDATE schedules 
                        SET current_lat=%s, current_lng=%s, last_gps_update=NOW()
                        WHERE id=%s
                    """, (pos['lat'], pos['lng'], sid))

                conn.commit()
                logger.info(f"✅ Processed {len(batch)} GPS points")

                for pos in latest_positions.values():
                    socketio.emit("bus_location", {
                        "sid": pos['sid'],
                        "lat": pos['lat'],
                        "lng": pos['lng'],
                        "speed": pos.get('speed', 0),
                        "timestamp": datetime.now().isoformat()
                    })
        except Exception as e:
            logger.error(f"❌ Batch processing failed: {e}")
            self.batch.extend(batch)

gps_processor = GPSBatchProcessor()

# ================= ADMIN DECORATOR =================
def admin_required(f):
    @wraps(f)
    def wrap(*a, **k):
        if not session.get("user_logged_in"):
            return redirect("/login")
        if session.get("role") != "admin":
            return "Access Denied", 403
        return f(*a, **k)
    return wrap

# ================= DATABASE INITIALIZATION =================
def initialize_database():
    conn = None
    cur = None
    try:
        conn = pool.getconn()
        cur = conn.cursor()
        tables = [
            """
            CREATE TABLE IF NOT EXISTS faces (
                id SERIAL PRIMARY KEY,
                bus_id INT NOT NULL,
                face_data BYTEA NOT NULL,
                face_image BYTEA NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS face_logs (
                id SERIAL PRIMARY KEY,
                face_id INT NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
                bus_id INT NOT NULL,
                entry_time TIMESTAMP NOT NULL,
                latitude DOUBLE PRECISION,
                longitude DOUBLE PRECISION,
                created_at TIMESTAMP DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS admins (
                id SERIAL PRIMARY KEY,
                username VARCHAR(50) UNIQUE,
                password VARCHAR(255),
                role VARCHAR(20) DEFAULT 'admin',
                counter_no INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                schedule_id INT,
                seat_number INT,
                order_id VARCHAR(100),
                payment_id VARCHAR(100),
                amount INT,
                status VARCHAR(20),
                created_at TIMESTAMP DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS routes (
                id SERIAL PRIMARY KEY, 
                route_name VARCHAR(100) UNIQUE, 
                distance_km INT,
                created_at TIMESTAMP DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS schedules (
                id SERIAL PRIMARY KEY, 
                route_id INT REFERENCES routes(id), 
                bus_name VARCHAR(100),
                departure_time TIME, 
                current_lat DOUBLE PRECISION,
                current_lng DOUBLE PRECISION,
                total_seats INT DEFAULT 40,
                last_gps_update TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS seat_bookings (
                id SERIAL PRIMARY KEY,
                schedule_id INT REFERENCES schedules(id) ON DELETE CASCADE,
                seat_number INT,
                passenger_name VARCHAR(100),
                mobile VARCHAR(15),
                from_station VARCHAR(50),
                to_station VARCHAR(50),
                travel_date DATE,
                status VARCHAR(20) DEFAULT 'confirmed',
                fare INT,
                payment_mode VARCHAR(10) DEFAULT 'cash',
                booked_by_type VARCHAR(10) DEFAULT 'user',
                booked_by_id INT,
                counter_id INT,
                order_id VARCHAR(100),
                payment_id VARCHAR(100),
                booking_hash VARCHAR(64) UNIQUE,
                created_at TIMESTAMP DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS route_stations (
                id SERIAL PRIMARY KEY, 
                route_id INT REFERENCES routes(id), 
                station_name VARCHAR(50), 
                station_order INT,
                lat DOUBLE PRECISION DEFAULT 27.2,
                lng DOUBLE PRECISION DEFAULT 75.2,
                created_at TIMESTAMP DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS gps_logs (
                id BIGSERIAL PRIMARY KEY,
                schedule_id INT NOT NULL,
                latitude DOUBLE PRECISION NOT NULL,
                longitude DOUBLE PRECISION NOT NULL,
                speed DOUBLE PRECISION DEFAULT 0,
                accuracy DOUBLE PRECISION,
                timestamp TIMESTAMP DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS driver_ratings (
                id SERIAL PRIMARY KEY,
                driver_id INT NOT NULL,
                rating INT CHECK (rating >= 1 AND rating <= 5),
                comment TEXT,
                booking_id INT,
                created_at TIMESTAMP DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id SERIAL PRIMARY KEY,
                user_id INT NOT NULL,
                subscription_json JSONB NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_gps_logs_schedule_time 
            ON gps_logs(schedule_id, timestamp DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_gps_logs_time 
            ON gps_logs(timestamp DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_bookings_schedule_date 
            ON seat_bookings(schedule_id, travel_date, status)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_bookings_hash 
            ON seat_bookings(booking_hash)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_schedules_active 
            ON schedules(is_active, id)
            """
        ]

        for sql in tables:
            cur.execute(sql)

        conn.commit()

        cur.execute("SELECT COUNT(*) FROM admins")
        if cur.fetchone()[0] == 0:
            hashed_password = generate_password_hash("admin123")
            cur.execute("""
                INSERT INTO admins (username, password, role, counter_no)
                VALUES('admin', %s, 'admin', 1)
                ON CONFLICT DO NOTHING
            """, (hashed_password,))

        cur.execute("SELECT COUNT(*) FROM routes")
        if cur.fetchone()[0] == 0:
            routes = [
                (1, 'Bikaner → Jaipur', 336),
                (2, 'Bikaner → Jodhpur', 252),
                (3, 'Jaipur → Jodhpur', 330)
            ]
            for r in routes:
                cur.execute("INSERT INTO routes VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", r)

            schedules = [
                (1, 1, 'Volvo AC Sleeper', '08:00'),
                (2, 1, 'Semi Sleeper AC', '10:30'),
                (3, 2, 'Volvo AC Seater', '09:00'),
                (4, 3, 'Deluxe AC', '07:30')
            ]
            for s in schedules:
                cur.execute("""
                    INSERT INTO schedules (id, route_id, bus_name, departure_time, total_seats)
                    VALUES (%s,%s,%s,%s::time,40) ON CONFLICT DO NOTHING
                """, s)

            stations = [
                (1, 'Bikaner', 1, 28.0229, 73.3119),
                (1, 'Jaipur', 2, 26.9124, 75.7873),
                (2, 'Bikaner', 1, 28.0229, 73.3119),
                (2, 'Jodhpur', 2, 26.2389, 73.0243),
                (3, 'Jaipur', 1, 26.9124, 75.7873),
                (3, 'Jodhpur', 2, 26.2389, 73.0243)
            ]
            for st in stations:
                cur.execute("""
                    INSERT INTO route_stations (route_id, station_name, station_order, lat, lng)
                    VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
                """, st)

            conn.commit()
        logger.info("✅ Database initialization complete!")
    except Exception as e:
        logger.error(f"❌ Database initialization error: {e}")
        traceback.print_exc()
        if conn:
            try:
                conn.rollback()
            except:
                pass
        raise
    finally:
        if cur:
            try:
                cur.close()
            except:
                pass
        if conn:
            try:
                pool.putconn(conn)
            except:
                pass

initialize_database()

# ================= REQUEST ID TRACKING =================
@app.before_request
def assign_request_id():
    g.request_id = str(uuid.uuid4())

@app.after_request
def add_request_id(response):
    response.headers['X-Request-ID'] = getattr(g, 'request_id', '')
    return response

# ================= ROUTES =================
@app.route("/")
@safe_database
def home():
    if "role" not in session:
        session.clear()
        session["role"] = "guest"
    today = date.today().isoformat()
    with get_database_connection() as (conn, cur):
        cur.execute("SELECT DISTINCT station_name FROM route_stations ORDER BY station_name")
        stations = [r["station_name"] for r in cur.fetchall()]
    return render_template("home.html", stations=stations, today=today)

@app.route("/login", methods=["GET", "POST"])
@safe_database
def login():
    error = ""
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        with get_database_connection() as (conn, cur):
            cur.execute("""
                SELECT id, password, role FROM admins
                WHERE username=%s
            """, (username,))
            user = cur.fetchone()
            if user and check_password_hash(user["password"], password):
                session.clear()
                session["user_logged_in"] = True
                session["user_id"] = user["id"]
                session["role"] = user["role"]
                session["username"] = username
                return redirect("/dashboard")
            else:
                error = "Invalid username or password"
    return render_template("login.html", error=error)

@app.route("/dashboard")
@safe_database
def dashboard():
    if not session.get("user_logged_in"):
        return redirect("/login")
    role = session.get("role", "user")
    username = session.get("username", "User")
    stats = {}
    if role == "admin":
        with get_database_connection() as (conn, cur):
            cur.execute("""
                SELECT COUNT(*) as count FROM seat_bookings 
                WHERE DATE(created_at) = CURRENT_DATE
            """)
            stats["today_bookings"] = cur.fetchone()["count"]
            cur.execute("SELECT COUNT(*) as count FROM schedules WHERE is_active=true")
            stats["active_buses"] = cur.fetchone()["count"]
            cur.execute("SELECT COUNT(*) as count FROM routes")
            stats["total_routes"] = cur.fetchone()["count"]
            cur.execute("""
                SELECT COALESCE(SUM(fare), 0) as total FROM seat_bookings 
                WHERE DATE(created_at) = CURRENT_DATE AND status='confirmed'
            """)
            stats["today_revenue"] = cur.fetchone()["total"]
            cur.execute("""
                SELECT sb.passenger_name, s.bus_name, sb.seat_number, sb.fare, sb.created_at
                FROM seat_bookings sb
                JOIN schedules s ON sb.schedule_id = s.id
                WHERE DATE(sb.created_at) = CURRENT_DATE
                ORDER BY sb.created_at DESC
                LIMIT 5
            """)
            stats["recent_bookings"] = cur.fetchall()
    return render_template("dashboard.html", stats=stats, role=role, username=username)

@app.route("/search", methods=["POST"])
@safe_database
def search_buses():
    from_station = request.form.get("from", "").strip()
    to_station = request.form.get("to", "").strip()
    travel_date = request.form.get("date", date.today().isoformat())
    session["from"] = from_station
    session["to"] = to_station
    session["date"] = travel_date
    if not from_station or not to_station:
        return "Please select both stations", 400
    with get_database_connection() as (conn, cur):
        cur.execute("""
            SELECT DISTINCT r.id, r.route_name, r.distance_km
            FROM routes r
            JOIN route_stations rs1 ON rs1.route_id = r.id
            JOIN route_stations rs2 ON rs2.route_id = r.id
            WHERE LOWER(rs1.station_name) = LOWER(%s)
              AND LOWER(rs2.station_name) = LOWER(%s)
              AND rs1.station_order < rs2.station_order
        """, (from_station, to_station))
        route = cur.fetchone()
    if not route:
        return render_template("search.html", error="No direct buses found", 
                               from_station=from_station, to_station=to_station)
    return redirect(f"/buses/{route['id']}")

@app.route("/buses/<int:route_id>")
@safe_database
def buses_list(route_id):
    with get_database_connection() as (conn, cur):
        cur.execute("""
            SELECT r.route_name, r.distance_km, 
                   string_agg(rs.station_name, ' → ' ORDER BY rs.station_order) as stations
            FROM routes r 
            LEFT JOIN route_stations rs ON r.id = rs.route_id 
            WHERE r.id = %s 
            GROUP BY r.id, r.route_name, r.distance_km
        """, (route_id,))
        route = cur.fetchone()
        if not route:
            return "Route not found", 404
        cur.execute("""
            SELECT s.id, s.bus_name, s.departure_time, s.total_seats,
                   s.current_lat, s.current_lng, s.last_gps_update,
                   COALESCE(bk.count, 0) as booked_count
            FROM schedules s 
            LEFT JOIN (
                SELECT schedule_id, COUNT(*) as count 
                FROM seat_bookings 
                WHERE travel_date = CURRENT_DATE AND status='confirmed'
                GROUP BY schedule_id
            ) bk ON s.id = bk.schedule_id
            WHERE s.route_id = %s AND s.is_active = true
            ORDER BY s.departure_time
        """, (route_id,))
        buses = cur.fetchall()
    return render_template("buses.html", route=route, buses=buses)

@app.route("/seats/<int:schedule_id>")
@safe_database
def seat_selection(schedule_id):
    with get_database_connection() as (conn, cur):
        cur.execute("""
            SELECT s.id, s.bus_name, s.departure_time, r.route_name,
                   s.current_lat, s.current_lng, s.total_seats
            FROM schedules s
            JOIN routes r ON s.route_id = r.id
            WHERE s.id = %s
        """, (schedule_id,))
        schedule = cur.fetchone()
        if not schedule:
            return "Schedule not found", 404
        today = session.get("date", date.today().isoformat())
        cur.execute("""
            SELECT seat_number
            FROM seat_bookings
            WHERE schedule_id=%s AND travel_date=%s AND status='confirmed'
        """, (schedule_id, today))
        booked_seats = set(r['seat_number'] for r in cur.fetchall())
    return render_template("seats.html", schedule=schedule, booked_seats=booked_seats, today=today)

@app.route("/book", methods=["POST"])
@safe_database
@limiter.limit("10 per minute")
def book_seat():
    data = request.get_json()
    booking_hash = hashlib.sha256(
        f"{data['schedule_id']}_{data['seat_number']}_{data['date']}_{data['passenger_name']}".encode()
    ).hexdigest()
    with get_database_connection() as (conn, cur):
        cur.execute("""
            SELECT id FROM seat_bookings
            WHERE schedule_id=%s AND seat_number=%s AND travel_date=%s AND status='confirmed'
        """, (data['schedule_id'], data['seat_number'], data['date']))
        if cur.fetchone():
            return jsonify({"ok": False, "error": "Seat already booked"}), 409
        cur.execute("SELECT id FROM seat_bookings WHERE booking_hash=%s", (booking_hash,))
        if cur.fetchone():
            return jsonify({"ok": True, "fare": 0, "message": "Already booked"})
        fare = random.randint(250, 450)
        user_role = session.get("role", "user")
        cur.execute("""
        INSERT INTO seat_bookings (
            schedule_id, seat_number, passenger_name, mobile,
            from_station, to_station, travel_date,
            fare, status, payment_mode,
            booked_by_type, booked_by_id, booking_hash
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            int(data['schedule_id']),
            int(data['seat_number']),
            data['passenger_name'],
            data['mobile'],
            session.get("from", "Unknown"),
            session.get("to", "Unknown"),
            data['date'],
            int(fare),
            'confirmed',
            'cash',
            user_role,
            int(session.get("user_id", 0)),
            booking_hash
        ))
        conn.commit()
    socketio.emit("seat_update", {
        "sid": data['schedule_id'],
        "seat": data['seat_number'],
        "date": data['date']
    })
    return jsonify({"ok": True, "fare": fare})

@app.route("/live-bus/<int:schedule_id>")
@safe_database
def live_tracking(schedule_id):
    with get_database_connection() as (conn, cur):
        cur.execute("""
            SELECT s.id, s.bus_name, s.departure_time, r.route_name,
                   s.current_lat as lat, s.current_lng as lng,
                   s.last_gps_update
            FROM schedules s 
            JOIN routes r ON s.route_id = r.id 
            WHERE s.id = %s
        """, (schedule_id,))
        bus = cur.fetchone()
        if not bus:
            return "Bus not found", 404
        cur.execute("""
            SELECT station_name, lat, lng
            FROM route_stations
            WHERE route_id = (
                SELECT route_id FROM schedules WHERE id = %s
            )
            ORDER BY station_order
        """, (schedule_id,))
        stations = cur.fetchall()
    return render_template("live_tracking.html", bus=bus, stations=stations)

@app.route("/driver/<int:bus_id>")
def driver_app(bus_id):
    return render_template("driver_app.html", bus_id=bus_id)

@app.route("/admin/metrics")
@admin_required
@safe_database
def system_metrics():
    with get_database_connection() as (conn, cur):
        cur.execute("""
            SELECT 
                (SELECT COUNT(*) FROM gps_logs 
                 WHERE timestamp > NOW() - INTERVAL '1 hour') as gps_points_1h,
                (SELECT COUNT(*) FROM seat_bookings 
                 WHERE created_at > NOW() - INTERVAL '1 hour') as bookings_1h,
                (SELECT COUNT(*) FROM schedules 
                 WHERE is_active=true) as active_buses,
                (SELECT COUNT(*) FROM pg_stat_activity 
                 WHERE state='active') as active_connections,
                (SELECT COALESCE(SUM(fare), 0) FROM seat_bookings 
                 WHERE created_at > NOW() - INTERVAL '1 hour') as revenue_1h
        """)
        db_metrics = cur.fetchone()
    redis_metrics = {}
    if redis_client:
        try:
            redis_info = redis_client.info()
            redis_metrics = {
                "connected_clients": redis_info.get('connected_clients', 0),
                "used_memory": redis_info.get('used_memory_human', '0'),
                "total_keys": redis_client.dbsize(),
                "uptime": redis_info.get('uptime_in_seconds', 0)
            }
        except:
            redis_metrics = {"status": "not_available"}
    system_metrics = {
        "cpu_percent": psutil.cpu_percent(),
        "memory_percent": psutil.virtual_memory().percent,
        "disk_usage": psutil.disk_usage('/').percent
    }
    return render_template("metrics.html", db_metrics=db_metrics, 
                          redis_metrics=redis_metrics, system_metrics=system_metrics)

@app.route("/health")
def health_check():
    try:
        with get_database_connection() as (conn, cur):
            cur.execute("SELECT 1")
        if redis_client:
            redis_client.ping()
        return jsonify({
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "database": "connected",
            "redis": "connected" if redis_client else "not_configured"
        }), 200
    except Exception as e:
        return jsonify({
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }), 503

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

def cleanup_old_data():
    try:
        with get_database_connection() as (conn, cur):
            cur.execute("""
                DELETE FROM gps_logs 
                WHERE timestamp < NOW() - INTERVAL '30 days'
            """)
            cur.execute("""
                DELETE FROM seat_bookings 
                WHERE created_at < NOW() - INTERVAL '1 year'
                AND status = 'confirmed'
            """)
            conn.commit()
            logger.info("✅ Cleaned up old data")
    except Exception as e:
        logger.error(f"❌ Cleanup failed: {e}")

# ================= SOCKET.IO EVENTS =================
@socketio.on("connect")
def handle_connect():
    logger.info(f"✅ Client connected: {request.sid}")

@socketio.on("driver_gps")
def handle_driver_gps(data):
    try:
        sid = int(data.get('sid', 0))
        lat = float(data.get('lat', 0))
        lng = float(data.get('lng', 0))
        speed = float(data.get('speed', 0))
        accuracy = float(data.get('accuracy', 999))
        if not sid or not lat or not lng:
            return
        gps_processor.add({
            'sid': sid,
            'lat': lat,
            'lng': lng,
            'speed': speed,
            'accuracy': accuracy
        })
    except Exception as e:
        logger.error(f"GPS handling error: {e}")

# ================= RUN APPLICATION =================
if __name__ == "__main__":
    logger.info("🚀 Starting Heavy Traffic Optimized Bus App...")
    
    def run_scheduled_jobs():
        while True:
            schedule.run_pending()
            time.sleep(60)
    
    schedule.every().day.at("02:00").do(cleanup_old_data)
    scheduler_thread = threading.Thread(target=run_scheduled_jobs, daemon=True)
    scheduler_thread.start()
    
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        debug=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True
    )