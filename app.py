from dotenv import load_dotenv
import os
import json
import time
import hashlib
from datetime import date, datetime, timedelta
from functools import wraps
from contextlib import contextmanager
from flask import Flask, request, jsonify, render_template_string, redirect, session
from flask_socketio import SocketIO, emit
from flask_compress import Compress
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
from psycopg.extras import execute_values
import atexit
import razorpay
import random
import traceback
import redis
from concurrent.futures import ThreadPoolExecutor
import logging

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

# Redis for caching and queue
redis_client = None
try:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    redis_client = redis.from_url(redis_url, decode_responses=True)
    logger.info("✅ Redis connected")
except Exception as e:
    logger.warning(f"⚠️ Redis not available: {e}")

# Razorpay setup
razor_client = None
if os.getenv("RAZORPAY_KEY_ID") and os.getenv("RAZORPAY_KEY_SECRET"):
    razor_client = razorpay.Client(auth=(
        os.getenv("RAZORPAY_KEY_ID"),
        os.getenv("RAZORPAY_KEY_SECRET")
    ))

# Flask app setup
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key")
Compress(app)

# Rate limiter
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per minute", "50 per second"],
    storage_uri=os.getenv("REDIS_URL", "memory://")
)

# SocketIO with Redis adapter for multi-server
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    logger=True,
    engineio_logger=True,
    ping_timeout=60,
    ping_interval=25,
    max_http_buffer_size=10_000_000,
    message_queue=os.getenv("REDIS_URL") if redis_client else None
)


# PostgreSQL connection pool with retry
def create_pool():
    max_retries = 5
    for i in range(max_retries):
        try:
            pool = ConnectionPool(
                conninfo=os.getenv("DATABASE_URL"),
                min_size=5,
                max_size=20,
                timeout=30,
                max_idle=300,
                max_lifetime=3600
            )
            logger.info("✅ Database pool created")
            return pool
        except Exception as e:
            logger.error(f"❌ Pool creation attempt {i + 1} failed: {e}")
            time.sleep(2 ** i)  # Exponential backoff
    raise Exception("Failed to create database pool")


pool = create_pool()


@atexit.register
def shutdown_pool():
    try:
        pool.close()
        logger.info("✅ Connection pool closed")
    except Exception as e:
        logger.error(f"⚠️ Error closing pool: {e}")


# ================= DB Helper Functions =================
@contextmanager
def get_db(retry_count=3):
    """Thread-safe database connection with retry logic"""
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
                time.sleep(0.1 * (2 ** attempt))  # Exponential backoff
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


def safe_db(func):
    """Decorator with circuit breaker pattern"""

    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Database error in {func.__name__}: {str(e)}")
            traceback.print_exc()
            return jsonify({"error": "Service temporarily unavailable"}), 503

    return wrapper


# ================= GPS Batch Processor =================
class GPSBatchProcessor:
    def __init__(self):
        self.batch = []
        self.last_flush = time.time()
        self.lock = False
        self.executor = ThreadPoolExecutor(max_workers=3)

    def add(self, data):
        """Add GPS point to batch"""
        # Deduplication using hash
        data_hash = hashlib.md5(
            f"{data['sid']}_{data['lat']}_{data['lng']}_{int(time.time() / 10)}".encode()
        ).hexdigest()

        data['_hash'] = data_hash
        data['_timestamp'] = time.time()

        self.batch.append(data)

        # Flush if batch is full or time exceeded
        if len(self.batch) >= 50 or (time.time() - self.last_flush) > 5:
            self.flush()

    def flush(self):
        """Flush batch to database"""
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
        """Process batch in background"""
        try:
            with get_db() as (conn, cur):
                # Bulk insert to gps_logs
                values = [(
                    b['sid'], b['lat'], b['lng'],
                    b.get('speed', 0), b.get('accuracy', 0),
                    datetime.fromtimestamp(b['_timestamp'])
                ) for b in batch]

                execute_values(
                    cur,
                    """
                    INSERT INTO gps_logs (schedule_id, latitude, longitude, speed, accuracy, timestamp)
                    VALUES %s ON CONFLICT DO NOTHING
                    """,
                    values
                )

                # Update latest position for each bus
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

                # Emit to clients
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
            # Re-queue failed items
            self.batch.extend(batch)


gps_processor = GPSBatchProcessor()


# ================= Admin Role Decorator =================
def admin_required(f):
    @wraps(f)
    def wrap(*a, **k):
        if not session.get("user_logged_in"):
            return redirect("/login")
        if session.get("role") != "admin":
            return "Access Denied", 403
        return f(*a, **k)

    return wrap


# ================= DB Initialization =================
def init_db():
    conn = None
    cur = None
    try:
        conn = pool.getconn()
        cur = conn.cursor()

        # All tables with optimizations
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
                password VARCHAR(100),
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
                booking_hash VARCHAR(64) UNIQUE,  -- For idempotency
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
            CREATE INDEX IF NOT EXISTS idx_gps_logs_schedule_time 
            ON gps_logs(schedule_id, timestamp DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_bookings_schedule_date 
            ON seat_bookings(schedule_id, travel_date, status)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_schedules_active 
            ON schedules(is_active, id)
            """
        ]

        for sql in tables:
            cur.execute(sql)

        conn.commit()

        # Default data
        cur.execute("SELECT COUNT(*) FROM admins")
        if cur.fetchone()[0] == 0:
            cur.execute("""
                INSERT INTO admins (username, password, role, counter_no)
                VALUES('admin', 'admin123', 'admin', 1)
                ON CONFLICT DO NOTHING
            """)

        cur.execute("SELECT COUNT(*) FROM routes")
        if cur.fetchone()[0] == 0:
            routes = [(1, 'बीकानेर → जयपुर', 336), (2, 'बीकानेर → जोधपुर', 252), (3, 'जयपुर → जोधपुर', 330)]
            for r in routes:
                cur.execute("INSERT INTO routes VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", r)

            schedules = [(1, 1, 'Volvo AC Sleeper', '08:00'), (2, 1, 'Semi Sleeper AC', '10:30'),
                         (3, 2, 'Volvo AC Seater', '09:00'), (4, 3, 'Deluxe AC', '07:30')]
            for s in schedules:
                cur.execute("""
                    INSERT INTO schedules (id, route_id, bus_name, departure_time, total_seats)
                    VALUES (%s,%s,%s,%s::time,40) ON CONFLICT DO NOTHING
                """, s)

            stations = [(1, 'बीकानेर', 1, 28.0229, 73.3119), (1, 'जयपुर', 2, 26.9124, 75.7873),
                        (2, 'बीकानेर', 1, 28.0229, 73.3119), (2, 'जोधपुर', 2, 26.2389, 73.0243),
                        (3, 'जयपुर', 1, 26.9124, 75.7873), (3, 'जोधपुर', 2, 26.2389, 73.0243)]
            for st in stations:
                cur.execute("""
                    INSERT INTO route_stations (route_id, station_name, station_order, lat, lng)
                    VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
                """, st)

            conn.commit()
        logger.info("✅ DB Init Complete!")

    except Exception as e:
        logger.error(f"❌ DB Init Error: {e}")
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


init_db()


# ================= Optimized Socket Events =================
@socketio.on("connect")
def handle_connect():
    logger.info(f"✅ Client connected: {request.sid}")


@socketio.on("driver_gps")
def handle_driver_gps(data):
    """Handle single GPS point - optimized for heavy traffic"""
    try:
        sid = int(data.get('sid', 0))
        lat = float(data.get('lat', 0))
        lng = float(data.get('lng', 0))
        speed = float(data.get('speed', 0))
        accuracy = float(data.get('accuracy', 999))

        if not sid or not lat or not lng:
            return

        # Add to batch processor (non-blocking)
        gps_processor.add({
            'sid': sid,
            'lat': lat,
            'lng': lng,
            'speed': speed,
            'accuracy': accuracy
        })

    except Exception as e:
        logger.error(f"GPS handling error: {e}")


@socketio.on("driver_gps_batch")
def handle_gps_batch(data):
    """Handle batch GPS data from driver app"""
    try:
        locations = data.get('locations', [])
        if not locations:
            return

        for loc in locations:
            gps_processor.add({
                'sid': int(loc.get('sid', 0)),
                'lat': float(loc.get('lat', 0)),
                'lng': float(loc.get('lng', 0)),
                'speed': float(loc.get('speed', 0)),
                'accuracy': float(loc.get('accuracy', 999))
            })

        emit('batch_received', {'count': len(locations)})

    except Exception as e:
        logger.error(f"Batch GPS error: {e}")


# ================= HTML Templates =================
BASE_HTML = """
<!DOCTYPE html>
<html lang="hi">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>माई बस एआई - Heavy Traffic Optimized</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet"/>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600;700&display=swap" rel="stylesheet"/>
<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:'Poppins',sans-serif;}
body{background:#f5f7fb;color:#222;}
.navbar{
  position:fixed;top:0;left:0;width:100%;
  background:white;
  display:flex;justify-content:space-between;align-items:center;
  padding:15px 8%;box-shadow:0 5px 20px rgba(0,0,0,.1);z-index:1000;
}
.logo{font-size:1.5rem;font-weight:700;color:#ff512f;}
.navbar a{margin-left:20px;text-decoration:none;color:#333;font-weight:500;}
.hero{
  height:100vh;
  background:linear-gradient(rgba(0,0,0,.6),rgba(0,0,0,.8)),
  url("https://images.unsplash.com/photo-1544620347-c4fd4a3d5957");
  background-size:cover; background-position:center;
  display:flex; align-items:center; justify-content:center;
  text-align:center; color:white; padding-top:70px;
}
.search-box{
  background:white; padding:20px; border-radius:15px; display:flex; gap:10px;
}
.search-box input{
  padding:12px; border:none; border-radius:8px; outline:none;
}
.search-box button{
  padding:12px 30px; border:none; border-radius:10px; background:#ff512f; color:white; font-weight:600; cursor:pointer;
}
.card{
  background:white; border-radius:15px; box-shadow:0 10px 25px rgba(0,0,0,.1); padding:20px; margin-bottom:20px;
}
@media(max-width:768px){
  .navbar{flex-direction:column; gap:10px; padding:10px 20px;}
  .search-box{flex-direction:column; width:100%;}
  .search-box input, .search-box button{width:100%;}
  .hero h1{font-size:1.6rem;}
}
</style>
</head>
<body>
<div class="navbar">
  <div class="logo">🚌 माई बस एआई</div>
  <div>
    <a href="/login">व्यवस्थापक लॉगिन</a>
    <a href="/counter">काउंटर</a>
  </div>
</div>

{% if not content %}
<section class="hero">
  <div>
    <h1>भारत का स्मार्ट बस प्लेटफॉर्म</h1>
    <p>बुक करें | ट्रैक करें | Heavy Traffic Optimized</p>
    <form class="search-box" action="/search" method="POST">
      <input name="from" placeholder="कहाँ से" required>
      <input name="to" placeholder="कहाँ तक" required>
      <input type="date" name="date" required>
      <button type="submit">खोजें</button>
    </form>
  </div>
</section>
{% endif %}

{% if content %}
<div style="padding:100px 10%;">
    {{ content|safe }}
</div>
{% endif %}
</body>
</html>
"""

LOGIN_HTML = """
<div class="row justify-content-center mt-5">
  <div class="col-md-4">
    <div class="card shadow-lg border-0 rounded-4">
      <div class="card-body p-4">
        <h3 class="text-center mb-4">व्यवस्थापक लॉगिन</h3>
        <form method="POST" autocomplete="on">
          <input type="text" style="display:none">
          <input type="password" style="display:none">
          <input type="text" name="username" class="form-control mb-3" placeholder="यूज़रनेम" required>
          <input type="password" name="password" class="form-control mb-3" placeholder="पासवर्ड" required>
          <button class="btn btn-success w-100">लॉगिन</button>
        </form>
        {% if error %}
          <div class="text-danger text-center mt-3">{{ error }}</div>
        {% endif %}
      </div>
    </div>
  </div>
</div>
"""


# ================== Main Routes ==================
@app.route("/")
@safe_db
def home():
    if "role" not in session:
        session.clear()
        session["role"] = "guest"

    with get_db() as (conn, cur):
        cur.execute("SELECT id, route_name, distance_km FROM routes ORDER BY id")
        routes = cur.fetchall()
        cur.execute("SELECT DISTINCT station_name FROM route_stations ORDER BY station_name")
        stations = [r["station_name"] for r in cur.fetchall()]

    return render_template_string(BASE_HTML, stations=stations, routes=routes, content=None)


@app.route("/dashboard")
def dashboard():
    if not session.get("user_logged_in"):
        return redirect("/login")
    role = session.get("role", "user")
    admin_links = ""
    if role.lower() == "admin":
        admin_links = """
        <div class="mt-3">
            <a href="/routes" class="btn btn-info me-2">🛣️ मार्ग प्रबंधन</a>
            <a href="/schedules" class="btn btn-warning me-2">🚌 कार्यक्रम प्रबंधन</a>
            <a href="/bookings" class="btn btn-success">🎫 बुकिंग देखें</a>
            <a href="/create-counter" class="btn btn-success">🎫 काउंटर बनाएं</a>
        </div>
        """
    return render_template_string(
        BASE_HTML,
        content=f"""
        <div class="text-center mt-5">
            <h2>स्वागत है 🎉</h2>
            <h4>भूमिका: <b>{role.upper()}</b></h4>
            <div class="mt-4">
                <a href="/" class="btn btn-primary">🏠 होम</a>
                <a href="/logout" class="btn btn-danger ms-2">🚪 लॉगआउट</a>
            </div>
            {admin_links}
        </div>
        """
    )


@app.route("/buses/<int:rid>")
@safe_db
def buses(rid):
    with get_db() as (conn, cur):
        cur.execute("""
            SELECT r.route_name, r.distance_km, 
                   string_agg(rs.station_name, ' → ' ORDER BY rs.station_order) as stations
            FROM routes r 
            LEFT JOIN route_stations rs ON r.id = rs.route_id 
            WHERE r.id = %s 
            GROUP BY r.id, r.route_name, r.distance_km
        """, (rid,))
        route = cur.fetchone()
        if not route:
            return "मार्ग नहीं मिला", 404

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
            WHERE s.route_id = %s 
            ORDER BY s.departure_time
        """, (rid,))
        buses_data = cur.fetchall()

    html = """
    <!DOCTYPE html>
    <html lang="hi">
    <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
    <title>🚌 {{ route.route_name }} - Heavy Traffic Optimized</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet"/>
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600;700&display=swap" rel="stylesheet"/>
    <style>
    body { font-family:'Poppins',sans-serif; margin:0; background:linear-gradient(135deg,#00c6ff,#0072ff); color:#fff; overflow-x:hidden; }
    header { text-align:center; padding:60px 20px 40px; }
    header h1 { font-size:42px; font-weight:700; margin-bottom:10px; text-shadow:2px 2px 10px rgba(0,0,0,0.3);}
    header p { font-size:18px; opacity:0.9; }
    .circle { position:absolute; border-radius:50%; opacity:0.6; animation: float 15s infinite alternate; }
    .circle1 {width:250px;height:250px;background:#ff6a00;top:-50px;left:-50px;}
    .circle2 {width:350px;height:350px;background:#ffd500;bottom:-100px;right:-80px;}
    .circle3 {width:150px;height:150px;background:#00ffb0;top:200px;right:50px;}
    @keyframes float{0%{transform:translateY(0) translateX(0);}50%{transform:translateY(-40px) translateX(20px);}100%{transform:translateY(0) translateX(0);}}
    .bus-card {background: rgba(255,255,255,0.15); border-radius:20px; padding:20px; margin-bottom:25px; box-shadow:10px 10px 20px rgba(0,0,0,0.2), -10px -10px 20px rgba(255,255,255,0.1); backdrop-filter: blur(10px); transition: transform 0.3s, box-shadow 0.3s;}
    .bus-card:hover {transform:translateY(-10px); box-shadow:0 20px 40px rgba(0,0,0,0.3);}
    .bus-card h5 {font-weight:700; font-size:22px;}
    .bus-card .badge {font-weight:500; padding:8px 14px; font-size:14px; border-radius:12px;}
    .bus-card p {margin:5px 0; font-size:15px;}
    .bus-card .btn {border-radius:50px; font-weight:600; padding:10px 25px; transition: all 0.3s;}
    .bus-card .btn:hover {transform: scale(1.05);}
    .bus-info i {margin-right:8px; color:#ffd700;}
    footer {text-align:center; padding:20px 0; background: rgba(0,0,0,0.2); color:#fff; backdrop-filter: blur(5px);}
    @media(max-width:768px){header h1{font-size:28px;} .bus-card h5{font-size:18px;}}
    </style>
    </head>
    <body>
    <div class="circle circle1"></div>
    <div class="circle circle2"></div>
    <div class="circle circle3"></div>
    <header>
        <h1>🚌 {{ route.route_name }}</h1>
        <p>📍 {{ route.stations }} | 🛣️ {{ route.distance_km }} किमी</p>
    </header>
    <div class="container mt-5">
        <div class="row justify-content-center">
            <div class="col-md-6">
                {% if buses %}
                    {% for bus in buses %}
                    <div class="bus-card">
                        <div class="d-flex justify-content-between align-items-center">
                            <h5>{{ bus.bus_name }}</h5>
                            {% set time_diff = (now - bus.last_gps_update).total_seconds() if bus.last_gps_update else 999999 %}
                            <span class="badge {{ 'bg-success' if time_diff < 300 else 'bg-warning' if time_diff < 600 else 'bg-secondary' }}">
                                {{ '🟢 LIVE' if time_diff < 300 else '🟡 DELAYED' if time_diff < 600 else '⚪ OFFLINE' }}
                            </span>
                        </div>
                        <div class="bus-info mt-2">
                            <p><i class="fas fa-clock"></i> प्रस्थान: {{ bus.departure_time.strftime('%H:%M') }}</p>
                            <p><i class="fas fa-chair"></i> बची सीटें: {{ bus.total_seats - bus.booked_count }}</p>
                            {% if bus.last_gps_update %}
                            <p style="font-size:12px; opacity:0.8;">
                                अंतिम अपडेट: {{ bus.last_gps_update.strftime('%H:%M:%S') }}
                            </p>
                            {% endif %}
                        </div>
                        <div class="d-flex flex-wrap gap-2 mt-2">
                            <a href="/live-bus/{{ bus.id }}" class="btn btn-primary flex-fill">🗺️ लाइव जीपीएस</a>
                            <a href="/select/{{ bus.id }}" class="btn btn-success flex-fill">🎫 सीट बुक करें</a>
                        </div>
                    </div>
                    {% endfor %}
                {% else %}
                    <div class="alert alert-warning text-center">आज कोई बस नहीं है</div>
                {% endif %}
            </div>
        </div>
    </div>
    <footer>
        &copy; 2026 माईबस. सर्वाधिकार सुरक्षित.
    </footer>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
    </body>
    </html>
    """

    from datetime import datetime
    return render_template_string(html, route=route, buses=buses_data, now=datetime.now())


@app.route("/create-counter", methods=["GET", "POST"])
@admin_required
@safe_db
def create_counter():
    error = ""
    success = ""

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        if not username or not password:
            error = "सभी फील्ड भरें"
        else:
            with get_db() as (conn, cur):
                cur.execute("""
                    INSERT INTO admins (username, password, role)
                    VALUES (%s, %s, 'counter')
                    ON CONFLICT (username) DO NOTHING
                """, (username, password))
                conn.commit()
                success = f"काउंटर '{username}' सफलतापूर्वक बनाया गया ✅"

    form_html = f"""
    <div class="card mx-auto" style="max-width:500px; margin-top:40px;">
        <div class="card-body">
            <h4 class="card-title text-center mb-4">➕ नया काउंटर बनाएं</h4>
            <form method="POST">
                <div class="mb-3">
                    <label class="form-label">यूज़रनेम</label>
                    <input type="text" name="username" class="form-control" required>
                </div>
                <div class="mb-3">
                    <label class="form-label">पासवर्ड</label>
                    <input type="password" name="password" class="form-control" required>
                </div>
                <button class="btn btn-success w-100">काउंटर बनाएं</button>
            </form>
            {f"<div class='text-success mt-3'>{success}</div>" if success else ""}
            {f"<div class='text-danger mt-3'>{error}</div>" if error else ""}
        </div>
    </div>
    """

    return render_template_string(BASE_HTML, content=form_html)


@app.route("/login", methods=["GET", "POST"])
@safe_db
def login():
    error = ""

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        with get_db() as (conn, cur):
            cur.execute("""
                SELECT id, role FROM admins
                WHERE username=%s AND password=%s
            """, (username, password))
            user = cur.fetchone()

            if user:
                session.clear()
                session["user_logged_in"] = True
                session["user_id"] = user["id"]
                session["role"] = user["role"]
                return redirect("/dashboard")
            else:
                error = "गलत यूज़रनेम या पासवर्ड"

    return render_template_string(BASE_HTML, content=render_template_string(LOGIN_HTML, error=error))


@app.route("/counter", methods=["GET", "POST"])
@safe_db
def counter():
    error = ""
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        with get_db() as (conn, cur):
            cur.execute("""
                SELECT id, role FROM admins
                WHERE username=%s AND password=%s
            """, (username, password))
            user = cur.fetchone()

            if user:
                session.clear()
                session["user_logged_in"] = True
                session["user_id"] = user["id"]
                session["role"] = user["role"]
                return redirect("/dashboard")
            else:
                error = "गलत यूज़रनेम या पासवर्ड"

    return render_template_string(BASE_HTML, content=render_template_string(LOGIN_HTML, error=error))


@app.route("/select/<int:sid>")
def select(sid):
    fs = session.get("from")
    ts = session.get("to")
    d = session.get("date")
    if not fs or not ts or not d:
        return redirect("/")
    return redirect(f"/seats/{sid}?fs={fs}&ts={ts}&d={d}")


@app.route("/seats/<int:sid>")
@safe_db
def seat_page(sid):
    with get_db() as (conn, cur):
        cur.execute("""
            SELECT s.id, s.bus_name, s.departure_time, r.route_name,
                   r.id as route_id, s.current_lat, s.current_lng
            FROM schedules s
            JOIN routes r ON s.route_id = r.id
            WHERE s.id = %s
        """, (sid,))
        schedule = cur.fetchone()
        if not schedule:
            return "कार्यक्रम नहीं मिला", 404

        cur.execute("""
            SELECT station_name, station_order
            FROM route_stations
            WHERE route_id=%s
            ORDER BY station_order
        """, (schedule['route_id'],))
        stations = cur.fetchall()

        today = session.get("date", date.today().isoformat())
        cur.execute("""
            SELECT seat_number
            FROM seat_bookings
            WHERE schedule_id=%s AND travel_date=%s AND status='confirmed'
        """, (sid, today))
        booked = cur.fetchall()
        booked_seats = set(r['seat_number'] for r in booked)

    seat_buttons = ""
    for i in range(1, 41):
        if i in booked_seats:
            seat_buttons += f'<button id="seat-{i}" class="btn btn-danger seat" disabled>X{i}</button>'
        else:
            seat_buttons += f'<button id="seat-{i}" class="btn btn-success seat" onclick="bookSeat({i})">{i}</button>'

    user_role = session.get("role", "guest")
    bus_lat = schedule['current_lat'] if schedule['current_lat'] else 27.5
    bus_lon = schedule['current_lng'] if schedule['current_lng'] else 75.0
    counter_js = session.get("user_id") if session.get("role") == "counter" else "null"

    map_div = """
    <div id="map" style="width:100%; max-width:900px; height:300px; border-radius:12px; overflow:hidden; box-shadow:0 4px 10px rgba(0,0,0,0.2);"></div>
    """

    role_color = {
        "admin": "red",
        "counter": "green",
        "conductor": "blue",
        "user": "orange"
    }.get(user_role, "gray")

    html_content = f"""
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>

<div class="container" style="max-width:900px;margin:auto;">
<h2>बस: {schedule['bus_name']} | मार्ग: {schedule['route_name']}</h2>
<h4>प्रस्थान: {schedule['departure_time'].strftime('%H:%M')}</h4>
<h5>भूमिका:
<span style="color:{role_color};font-weight:bold;">{user_role.upper()}</span></h5>
{map_div}
<h5 style="margin-top:30px;">सीट चुनें</h5>
<div style="display:flex;flex-wrap:wrap;gap:10px;">
{seat_buttons}
</div>
</div>

<script>
const socket = io(window.location.origin, {{
    transports: ['websocket', 'polling'],
    reconnection: true,
    reconnectionAttempts: Infinity,
    reconnectionDelay: 1000,
    reconnectionDelayMax: 5000,
    randomizationFactor: 0.5
}});

const SID = {sid};
const TODAY = "{today}";
const BUS_LAT = {bus_lat};
const BUS_LNG = {bus_lon};
const COUNTER_ID = {counter_js};

const map = L.map('map').setView([BUS_LAT, BUS_LNG], 10);
L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/">CARTO</a>',
    subdomains: 'abcd',
    maxZoom: 19
}}).addTo(map);
let busMarker = L.marker([BUS_LAT, BUS_LNG]).addTo(map);
let routeLine = null;

socket.on('connect', () => {{
    console.log('✅ Connected');
}});

socket.on('disconnect', () => {{
    console.log('❌ Disconnected, retrying...');
}});

socket.on('bus_location', data => {{
    if(data.sid == SID){{
        const lat = parseFloat(data.lat);
        const lng = parseFloat(data.lng);
        busMarker.setLatLng([lat,lng]);
        map.panTo([lat,lng], {{animate:true}});
    }}
}});

function bookSeat(seatId){{
    let name = prompt("यात्री का नाम:");
    if(!name) return;
    let mobile = prompt("मोबाइल नंबर:");
    if(!mobile) return;
    let btn = document.getElementById("seat-" + seatId);
    let oldText = btn.innerText;
    btn.innerText = "⏳ बुक हो रहा है...";
    btn.disabled = true;
    let fare = null;
    let payment_mode = "cash";

    if(COUNTER_ID !== null){{
        fare = prompt("किराया राशि:");
        if(!fare || isNaN(fare)){{
            alert("अमान्य किराया");
            btn.innerText = oldText;
            btn.disabled = false;
            return;
        }}
        payment_mode = prompt("भुगतान मोड: cash / online", "cash");
        if(payment_mode !== "cash" && payment_mode !== "online"){{
            alert("केवल cash या online स्वीकार्य है");
            btn.innerText = oldText;
            btn.disabled = false;
            return;
        }}
    }}

    fetch("/book", {{
        method: "POST",
        headers: {{ "Content-Type":"application/json" }},
        body: JSON.stringify({{
            schedule_id: SID,
            seat_number: seatId,
            passenger_name: name,
            mobile: mobile,
            date: TODAY,
            fare: fare,
            payment_mode: payment_mode,
            counter_id: COUNTER_ID
        }})
    }})
    .then(r => r.json())
    .then(res => {{
        if(res.ok){{
            alert("सीट बुक हो गई! किराया: ₹" + res.fare);
            btn.className = "btn btn-danger seat";
            btn.innerText = "X" + seatId;
            btn.disabled = true;
        }} else {{
            alert(res.error || res.msg);
            btn.innerText = oldText;
            btn.disabled = false;
        }}
    }})
    .catch(err => {{
        console.error(err);
        alert("नेटवर्क त्रुटि, पुनः प्रयास करें");
        btn.innerText = oldText;
        btn.disabled = false;
    }});
}}
</script>
"""

    return render_template_string(BASE_HTML, content=html_content)


@app.route("/heartbeat")
def heartbeat():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})


@app.route("/book", methods=["POST"])
@safe_db
@limiter.limit("10 per minute")
def book():
    data = request.get_json()

    # Idempotency check
    booking_hash = hashlib.sha256(
        f"{data['schedule_id']}_{data['seat_number']}_{data['date']}_{data['passenger_name']}".encode()
    ).hexdigest()

    with get_db() as (conn, cur):
        # Check if already booked
        cur.execute("""
            SELECT id FROM seat_bookings
            WHERE schedule_id=%s AND seat_number=%s AND travel_date=%s AND status='confirmed'
        """, (data['schedule_id'], data['seat_number'], data['date']))
        if cur.fetchone():
            return jsonify({"ok": False, "error": "सीट पहले से बुक है"}), 409

        # Check for duplicate booking (idempotency)
        cur.execute("SELECT id FROM seat_bookings WHERE booking_hash=%s", (booking_hash,))
        if cur.fetchone():
            return jsonify({"ok": True, "fare": data.get('fare', 0), "message": "पहले से बुक है"})

        user_role = session.get("role", "user")
        if user_role == "counter":
            fare = int(data.get("fare", 0))
            payment_mode = data.get("payment_mode", "cash")
        else:
            fare = random.randint(250, 450)
            payment_mode = "cash"

        cur.execute("""
        INSERT INTO seat_bookings (
            schedule_id, seat_number, passenger_name, mobile,
            from_station, to_station, travel_date,
            fare, status, payment_mode,
            booked_by_type, booked_by_id, counter_id, booking_hash
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            int(data['schedule_id']),
            int(data['seat_number']),
            data['passenger_name'],
            data['mobile'],
            session.get("from"),
            session.get("to"),
            data['date'],
            int(fare),
            'confirmed',
            payment_mode,
            user_role,
            int(session.get("user_id", 0)),
            int(data.get("counter_id") or 0),
            booking_hash
        ))
        conn.commit()

    socketio.emit("seat_update", {
        "sid": data['schedule_id'],
        "seat": data['seat_number'],
        "date": data['date']
    })

    return jsonify({"ok": True, "fare": fare})


# ================= HEAVY TRAFFIC OPTIMIZED DRIVER APP =================

@app.route("/driver/<int:sid>")
def driver(sid):
    """Heavy Traffic Optimized Driver GPS App"""
    return f"""
<!DOCTYPE html>
<html lang="hi">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no"/>
<meta name="theme-color" content="#28a745"/>
<title>बस {sid} - Heavy Traffic GPS</title>
<link rel="manifest" href="/manifest.json?id={sid}">
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; font-family: 'Segoe UI', sans-serif; }}
body {{ 
    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
    min-height: 100vh; 
    color: white;
    overflow-x: hidden;
}}
.container {{ max-width: 100%; padding: 15px; }}
.header {{ 
    background: rgba(255,255,255,0.1); 
    padding: 20px; 
    border-radius: 15px; 
    margin-bottom: 15px;
    backdrop-filter: blur(10px);
}}
.bus-id {{ font-size: 32px; font-weight: bold; color: #00d9ff; }}
.status-grid {{ 
    display: grid; 
    grid-template-columns: repeat(2, 1fr); 
    gap: 10px; 
    margin-bottom: 15px;
}}
.status-card {{ 
    background: rgba(255,255,255,0.05); 
    padding: 15px; 
    border-radius: 12px; 
    text-align: center;
    border: 1px solid rgba(255,255,255,0.1);
}}
.status-label {{ font-size: 11px; color: #888; text-transform: uppercase; margin-bottom: 5px; }}
.status-value {{ font-size: 20px; font-weight: bold; color: #fff; }}
.status-value.good {{ color: #00ff88; }}
.status-value.warning {{ color: #ffaa00; }}
.status-value.bad {{ color: #ff4444; }}
.gps-btn {{ 
    width: 100%; 
    padding: 20px; 
    border: none; 
    border-radius: 15px; 
    font-size: 18px; 
    font-weight: bold;
    margin-bottom: 10px;
    cursor: pointer;
    transition: all 0.3s;
    text-transform: uppercase;
}}
.gps-btn.start {{ 
    background: linear-gradient(45deg, #00ff88, #00cc6a);
    color: #000;
    box-shadow: 0 5px 20px rgba(0,255,136,0.3);
}}
.gps-btn.stop {{ 
    background: linear-gradient(45deg, #ff4444, #cc0000);
    color: #fff;
    box-shadow: 0 5px 20px rgba(255,68,68,0.3);
}}
.gps-btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
.network-status {{ 
    position: fixed; 
    top: 10px; 
    right: 10px; 
    padding: 8px 15px; 
    border-radius: 20px; 
    font-size: 12px;
    font-weight: bold;
    z-index: 1000;
}}
.network-status.online {{ background: #00ff88; color: #000; }}
.network-status.offline {{ background: #ff4444; color: #fff; }}
.network-status.syncing {{ background: #ffaa00; color: #000; animation: pulse 1s infinite; }}
@keyframes pulse {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: 0.5; }} }}
.log-container {{ 
    background: rgba(0,0,0,0.3); 
    padding: 15px; 
    border-radius: 10px; 
    margin-top: 15px;
    max-height: 200px;
    overflow-y: auto;
    font-family: monospace;
    font-size: 12px;
}}
.log-entry {{ margin-bottom: 5px; padding: 3px 0; border-bottom: 1px solid rgba(255,255,255,0.1); }}
.log-entry.success {{ color: #00ff88; }}
.log-entry.error {{ color: #ff4444; }}
.log-entry.warning {{ color: #ffaa00; }}
.stats-bar {{ 
    display: flex; 
    justify-content: space-between; 
    padding: 10px; 
    background: rgba(255,255,255,0.05);
    border-radius: 8px;
    margin-bottom: 10px;
    font-size: 12px;
}}
</style>
</head>
<body>
<div class="network-status online" id="networkStatus">🟢 ONLINE</div>

<div class="container">
    <div class="header">
        <div class="bus-id">🚌 बस {sid}</div>
        <div style="color: #888; margin-top: 5px;">Heavy Traffic Optimized GPS</div>
    </div>

    <div class="stats-bar">
        <span id="queueCount">क्यू: 0</span>
        <span id="sentCount">भेजे: 0</span>
        <span id="failedCount">फेल: 0</span>
        <span id="batteryLevel">🔋 --%</span>
    </div>

    <div class="status-grid">
        <div class="status-card">
            <div class="status-label">अक्षांश (Lat)</div>
            <div class="status-value" id="latValue">--</div>
        </div>
        <div class="status-card">
            <div class="status-label">देशांतर (Lng)</div>
            <div class="status-value" id="lngValue">--</div>
        </div>
        <div class="status-card">
            <div class="status-label">गति (Speed)</div>
            <div class="status-value" id="speedValue">-- km/h</div>
        </div>
        <div class="status-card">
            <div class="status-label">सटीकता (Accuracy)</div>
            <div class="status-value" id="accValue">-- m</div>
        </div>
    </div>

    <button id="startBtn" class="gps-btn start" onclick="startTracking()">
        🚀 GPS ट्रैकिंग शुरू करें
    </button>

    <button id="stopBtn" class="gps-btn stop" onclick="stopTracking()" disabled>
        🛑 ट्रैकिंग बंद करें
    </button>

    <div class="log-container" id="logContainer">
        <div class="log-entry">📱 ऐप तैयार...</div>
    </div>
</div>

<script>
const BUS_ID = {sid};
const API_BASE = window.location.origin;

// Configuration for heavy traffic
const CONFIG = {{
    GPS_INTERVAL: 5000,        // 5 seconds between GPS reads
    BATCH_SIZE: 10,            // Send 10 points at once
    SEND_INTERVAL: 15000,      // Try to send every 15 seconds
    MAX_RETRIES: 5,            // Retry failed sends 5 times
    RETRY_DELAY: 3000,         // Wait 3 seconds between retries
    MAX_QUEUE_SIZE: 100        // Keep last 100 points
}};

// State
let state = {{
    isTracking: false,
    gpsQueue: [],
    sentCount: 0,
    failedCount: 0,
    watchId: null,
    sendInterval: null,
    wakeLock: null,
    retryCount: 0,
    lastSentTime: 0,
    isOnline: navigator.onLine
}};

// Service Worker Registration
if ('serviceWorker' in navigator) {{
    navigator.serviceWorker.register('/sw.js')
        .then(reg => log('Service Worker registered', 'success'))
        .catch(err => log('SW registration failed: ' + err, 'error'));
}}

// Battery API
if ('getBattery' in navigator) {{
    navigator.getBattery().then(battery => {{
        updateBattery(battery);
        battery.addEventListener('levelchange', () => updateBattery(battery));
    }});
}}

function updateBattery(battery) {{
    const level = Math.round(battery.level * 100);
    document.getElementById('batteryLevel').textContent = `🔋 ${{level}}%`;
    if (level < 20) {{
        document.getElementById('batteryLevel').style.color = '#ff4444';
    }}
}}

// Logging
function log(message, type = 'info') {{
    const container = document.getElementById('logContainer');
    const entry = document.createElement('div');
    entry.className = `log-entry ${{type}}`;
    const time = new Date().toLocaleTimeString('hi-IN');
    entry.textContent = `[${{time}}] ${{message}}`;
    container.insertBefore(entry, container.firstChild);

    // Keep only last 50 logs
    while (container.children.length > 50) {{
        container.removeChild(container.lastChild);
    }}

    console.log(`[${{type}}] ${{message}}`);
}}

// Network Status
function updateNetworkStatus() {{
    const status = document.getElementById('networkStatus');
    if (state.gpsQueue.length > 0 && navigator.onLine) {{
        status.textContent = '🟡 SYNCING';
        status.className = 'network-status syncing';
    }} else if (navigator.onLine) {{
        status.textContent = '🟢 ONLINE';
        status.className = 'network-status online';
    }} else {{
        status.textContent = '🔴 OFFLINE';
        status.className = 'network-status offline';
    }}
}}

window.addEventListener('online', () => {{
    state.isOnline = true;
    log('इंटरनेट वापस आ गया', 'success');
    updateNetworkStatus();
    flushQueue();
}});

window.addEventListener('offline', () => {{
    state.isOnline = false;
    log('इंटरनेट गया - क्यू मोड', 'warning');
    updateNetworkStatus();
}});

// Wake Lock
async function requestWakeLock() {{
    try {{
        if ('wakeLock' in navigator) {{
            state.wakeLock = await navigator.wakeLock.request('screen');
            log('स्क्रीन लॉक सक्रिय', 'success');
        }}
    }} catch (err) {{
        log('Wake Lock असफल: ' + err.message, 'error');
    }}
}}

function releaseWakeLock() {{
    if (state.wakeLock) {{
        state.wakeLock.release();
        state.wakeLock = null;
    }}
}}

// GPS Handling
function handlePosition(position) {{
    const lat = position.coords.latitude;
    const lng = position.coords.longitude;
    const speed = position.coords.speed ? (position.coords.speed * 3.6).toFixed(1) : 0;
    const accuracy = position.coords.accuracy.toFixed(0);

    // Update UI
    document.getElementById('latValue').textContent = lat.toFixed(5);
    document.getElementById('lngValue').textContent = lng.toFixed(5);
    document.getElementById('speedValue').textContent = speed + ' km/h';
    document.getElementById('accValue').textContent = accuracy + ' m';

    // Color coding
    document.getElementById('accValue').className = 'status-value ' + 
        (accuracy < 20 ? 'good' : accuracy < 100 ? 'warning' : 'bad');

    // Add to queue
    const point = {{
        sid: BUS_ID,
        lat: lat,
        lng: lng,
        speed: parseFloat(speed),
        accuracy: parseFloat(accuracy),
        timestamp: new Date().toISOString(),
        id: Date.now() + '_' + Math.random().toString(36).substr(2, 9)
    }};

    state.gpsQueue.push(point);

    // Limit queue size
    if (state.gpsQueue.length > CONFIG.MAX_QUEUE_SIZE) {{
        state.gpsQueue = state.gpsQueue.slice(-CONFIG.MAX_QUEUE_SIZE);
    }}

    updateStats();

    // Immediate send if batch is full
    if (state.gpsQueue.length >= CONFIG.BATCH_SIZE) {{
        flushQueue();
    }}
}}

function handleError(error) {{
    let msg = '';
    switch(error.code) {{
        case error.PERMISSION_DENIED:
            msg = "GPS अनुमति अस्वीकृत"; break;
        case error.POSITION_UNAVAILABLE:
            msg = "GPS उपलब्ध नहीं"; break;
        case error.TIMEOUT:
            msg = "GPS टाइमआउट"; break;
        default:
            msg = "GPS त्रुटि: " + error.message;
    }}
    log(msg, 'error');
}}

// Queue Management
async function flushQueue() {{
    if (state.gpsQueue.length === 0 || !navigator.onLine) {{
        updateNetworkStatus();
        return;
    }}

    const batch = state.gpsQueue.splice(0, CONFIG.BATCH_SIZE);
    updateStats();

    try {{
        const response = await fetch(API_BASE + '/api/gps-batch', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{locations: batch}}),
            signal: AbortSignal.timeout(10000) // 10 second timeout
        }});

        if (response.ok) {{
            const result = await response.json();
            state.sentCount += batch.length;
            state.retryCount = 0;
            log(`✅ ${{batch.length}} लोकेशन सिंक हुए`, 'success');
            updateNetworkStatus();
        }} else {{
            throw new Error('Server error: ' + response.status);
        }}
    }} catch (err) {{
        // Put back in queue
        state.gpsQueue.unshift(...batch);
        state.failedCount += batch.length;
        state.retryCount++;

        log(`❌ सिंक फेल (प्रयास ${{state.retryCount}}): ${{err.message}}`, 'error');

        // Exponential backoff
        if (state.retryCount < CONFIG.MAX_RETRIES) {{
            const delay = CONFIG.RETRY_DELAY * Math.pow(2, state.retryCount - 1);
            setTimeout(flushQueue, delay);
        }}

        updateNetworkStatus();
    }}

    updateStats();
}}

function updateStats() {{
    document.getElementById('queueCount').textContent = `क्यू: ${{state.gpsQueue.length}}`;
    document.getElementById('sentCount').textContent = `भेजे: ${{state.sentCount}}`;
    document.getElementById('failedCount').textContent = `फेल: ${{state.failedCount}}`;
}}

// Start/Stop Tracking
async function startTracking() {{
    if (!navigator.geolocation) {{
        alert('GPS सपोर्ट नहीं है');
        return;
    }}

    state.isTracking = true;
    document.getElementById('startBtn').disabled = true;
    document.getElementById('stopBtn').disabled = false;

    await requestWakeLock();

    // Start GPS with high accuracy
    state.watchId = navigator.geolocation.watchPosition(
        handlePosition,
        handleError,
        {{
            enableHighAccuracy: true,
            timeout: 20000,
            maximumAge: 0,
            distanceFilter: 10
        }}
    );

    // Periodic flush
    state.sendInterval = setInterval(flushQueue, CONFIG.SEND_INTERVAL);

    log('🚀 ट्रैकिंग शुरू - Heavy Traffic Mode', 'success');

    // Request notification permission
    if ('Notification' in window && Notification.permission === 'default') {{
        Notification.requestPermission();
    }}
}}

function stopTracking() {{
    state.isTracking = false;

    if (state.watchId !== null) {{
        navigator.geolocation.clearWatch(state.watchId);
        state.watchId = null;
    }}

    if (state.sendInterval) {{
        clearInterval(state.sendInterval);
    }}

    releaseWakeLock();

    // Final flush
    if (state.gpsQueue.length > 0) {{
        flushQueue();
    }}

    document.getElementById('startBtn').disabled = false;
    document.getElementById('stopBtn').disabled = true;

    log('🛑 ट्रैकिंग बंद', 'warning');
}}

// Page visibility handling
document.addEventListener('visibilitychange', () => {{
    if (document.hidden && state.isTracking) {{
        log('बैकग्राउंड मोड - GPS जारी', 'warning');
    }} else {{
        log('फोरग्राउंड मोड', 'info');
    }}
}});

// Before unload - save data
window.addEventListener('beforeunload', (e) => {{
    if (state.gpsQueue.length > 0) {{
        // Try to send immediately
        navigator.sendBeacon(API_BASE + '/api/gps-batch', 
            JSON.stringify({{locations: state.gpsQueue}}));
    }}
}});

// Initial status
updateNetworkStatus();
</script>
</body>
</html>
"""


@app.route("/manifest.json")
def manifest():
    bus_id = request.args.get('id', '1')
    return jsonify({
        "name": f"बस ट्रैकर {bus_id}",
        "short_name": f"बस {bus_id}",
        "start_url": f"/driver/{bus_id}",
        "display": "standalone",
        "background_color": "#1a1a2e",
        "theme_color": "#00d9ff",
        "orientation": "portrait",
        "icons": [
            {
                "src": "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🚌</text></svg>",
                "sizes": "192x192",
                "type": "image/svg+xml"
            }
        ]
    })


@app.route("/sw.js")
def service_worker():
    return """
const CACHE_NAME = 'bus-gps-v1';
const STATIC_ASSETS = [
    '/',
    '/driver/1',
    'https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css'
];

// Install - cache static assets
self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => {
            return cache.addAll(STATIC_ASSETS);
        })
    );
    self.skipWaiting();
});

// Activate
self.addEventListener('activate', (event) => {
    event.waitUntil(clients.claim());
});

// Background Sync Queue
let syncQueue = [];
let isProcessing = false;

self.addEventListener('message', (event) => {
    if (event.data.type === 'GPS_DATA') {
        syncQueue.push(...event.data.data);
        processQueue(event.data.apiBase);
    }
});

async function processQueue(apiBase) {
    if (isProcessing || syncQueue.length === 0) return;

    isProcessing = true;
    const batch = syncQueue.splice(0, 10);

    try {
        const response = await fetch(apiBase + '/api/gps-batch', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({locations: batch})
        });

        if (!response.ok) throw new Error('Failed');

        // Notify clients
        const clients = await self.clients.matchAll();
        clients.forEach(client => {
            client.postMessage({
                type: 'SYNC_SUCCESS',
                count: batch.length
            });
        });

    } catch (err) {
        // Put back
        syncQueue.unshift(...batch);
    } finally {
        isProcessing = false;
        if (syncQueue.length > 0) {
            setTimeout(() => processQueue(apiBase), 5000);
        }
    }
}

// Fetch strategy: Network first, fallback to cache
self.addEventListener('fetch', (event) => {
    if (event.request.method !== 'GET') return;

    event.respondWith(
        fetch(event.request)
            .then(response => {
                // Update cache
                const clone = response.clone();
                caches.open(CACHE_NAME).then(cache => {
                    cache.put(event.request, clone);
                });
                return response;
            })
            .catch(() => {
                return caches.match(event.request);
            })
    );
});
""", 200, {'Content-Type': 'application/javascript'}


@app.route("/api/gps-batch", methods=["POST"])
@limiter.limit("30 per minute")
@safe_db
def gps_batch():
    """Optimized batch GPS endpoint for heavy traffic"""
    try:
        data = request.get_json()
        locations = data.get('locations', [])

        if not locations or len(locations) > 50:  # Max 50 per batch
            return jsonify({"ok": False, "error": "Invalid batch size"}), 400

        # Process through batch processor
        for loc in locations:
            gps_processor.add({
                'sid': int(loc.get('sid', 0)),
                'lat': float(loc.get('lat', 0)),
                'lng': float(loc.get('lng', 0)),
                'speed': float(loc.get('speed', 0)),
                'accuracy': float(loc.get('accuracy', 999))
            })

        return jsonify({
            "ok": True,
            "queued": len(locations),
            "message": "डेटा क्यू में डाला गया"
        })

    except Exception as e:
        logger.error(f"GPS batch error: {e}")
        return jsonify({"ok": False, "error": "Processing failed"}), 500


@app.route("/live-bus/<int:sid>")
@safe_db
def live_bus(sid):
    with get_db() as (conn, cur):
        cur.execute("""
            SELECT s.id, s.bus_name, s.departure_time,
                   r.id as route_id, r.route_name, r.distance_km,
                   s.current_lat as lat, s.current_lng as lng,
                   s.last_gps_update
            FROM schedules s 
            JOIN routes r ON s.route_id = r.id 
            WHERE s.id = %s
        """, (sid,))
        bus = cur.fetchone()
        if not bus:
            return "बस नहीं मिली", 404

        lat = float(bus.get('lat', 27.2))
        lng = float(bus.get('lng', 74.2))

        # Calculate status
        last_update = bus.get('last_gps_update')
        is_live = False
        status_text = "⚪ ऑफलाइन"
        status_color = "secondary"

        if last_update:
            time_diff = (datetime.now() - last_update).total_seconds()
            if time_diff < 60:
                is_live = True
                status_text = "🟢 LIVE"
                status_color = "success"
            elif time_diff < 300:
                status_text = "🟡 DELAYED"
                status_color = "warning"

        cur.execute("""
            SELECT lat, lng, station_name
            FROM route_stations
            WHERE route_id=%s
            ORDER BY station_order
        """, (bus['route_id'],))
        stations = cur.fetchall()

    import json
    stations_json = json.dumps(stations)

    content = f'''
    <style>
    #map{{height:70vh;width:100%;border-radius:20px;box-shadow:0 20px 40px rgba(0,0,0,0.3);}}
    .live-indicator{{animation:pulse 2s infinite;width:20px;height:20px;background:#28a745;border-radius:50%;display:inline-block;margin-right:10px;}}
    @keyframes pulse{{0%,100%{{transform:scale(1);opacity:1;}}50%{{transform:scale(1.2);opacity:0.7;}}}}
    </style>

    <div class="text-center mb-4">
        <h2>🚌 {bus['bus_name']}</h2>
        <p class="text-muted">{bus['route_name']} ({bus['distance_km']}किमी)</p>
        <span class="badge bg-{status_color} fs-6">{status_text}</span>
        {f'<div class="small text-muted mt-2">अंतिम अपडेट: {last_update.strftime("%H:%M:%S")}</div>' if last_update else ''}
    </div>

    <div id="map" class="rounded-4"></div>

    <div class="alert alert-info mt-3 d-flex align-items-center" id="connStatus">
        <span class="spinner-border spinner-border-sm me-2"></span>
        कनेक्ट हो रहा है...
    </div>

    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <script>
    const map = L.map('map').setView([{lat}, {lng}], 13);
    L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
        subdomains: 'abcd',
        maxZoom: 19
    }}).addTo(map);

    const stations = {stations_json};
    let routePoints = [];
    stations.forEach(st => {{
        const lat = parseFloat(st.lat);
        const lng = parseFloat(st.lng);
        if(!isNaN(lat) && !isNaN(lng)){{
            routePoints.push([lat,lng]);
            L.marker([lat,lng]).addTo(map).bindPopup("📍 " + st.station_name);
        }}
    }});

    if(routePoints.length > 1){{
        L.polyline(routePoints, {{color: 'blue', weight: 6}}).addTo(map);
    }}

    const busIcon = L.divIcon({{
        html: '<div class="live-indicator"></div>',
        className: '',
        iconSize: [20,20]
    }});

    let busMarker = L.marker([{lat},{lng}], {{icon: busIcon}}).addTo(map);
    const statusDiv = document.getElementById('connStatus');

    // Optimized SocketIO for bad networks
    const socket = io(window.location.origin, {{
        transports: ['websocket', 'polling'],
        reconnection: true,
        reconnectionAttempts: Infinity,
        reconnectionDelay: 1000,
        reconnectionDelayMax: 10000,
        timeout: 20000
    }});

    socket.on('connect', () => {{
        statusDiv.className = 'alert alert-success mt-3';
        statusDiv.innerHTML = '✅ लाइव कनेक्टेड';
    }});

    socket.on('disconnect', () => {{
        statusDiv.className = 'alert alert-warning mt-3';
        statusDiv.innerHTML = '⚠️ डिस्कनेक्टेड - पुनः प्रयास...';
    }});

    socket.on('bus_location', data => {{
        if(data.sid == {sid}){{
            const lat = parseFloat(data.lat);
            const lng = parseFloat(data.lng);
            busMarker.setLatLng([lat,lng]);
            map.panTo([lat,lng], {{animate:true}});

            statusDiv.className = 'alert alert-success mt-3';
            statusDiv.innerHTML = `🚌 ${{data.speed ? data.speed.toFixed(1) : 0}} km/h | ${{new Date().toLocaleTimeString('hi-IN')}}`;
        }}
    }});
    </script>
    '''
    return render_template_string(BASE_HTML, content=content)


@app.route("/create-payment", methods=["POST"])
@limiter.limit("5 per minute")
def create_payment():
    RAZORPAY_ENABLED = os.getenv("RAZORPAY_ENABLED", "false").lower() == "true"
    if not RAZORPAY_ENABLED:
        return jsonify({"ok": False, "error": "भुगतान गेटवे कॉन्फ़िगर नहीं है"}), 400

    try:
        data = request.get_json()
        order = razor_client.order.create({
            "amount": int(data['fare']) * 100,
            "currency": "INR",
            "receipt": f"seat_{data['sid']}_{data['seat']}_{int(time.time())}",
            "payment_capture": 1
        })

        return jsonify({
            "ok": True,
            "order_id": order['id'],
            "key": os.getenv("RAZORPAY_KEY_ID")
        })
    except Exception as e:
        logger.error(f"Payment creation failed: {e}")
        return jsonify({"ok": False, "error": "भुगतान निर्माण असफल"}), 500


@app.route("/verify-payment", methods=["POST"])
@safe_db
@limiter.limit("10 per minute")
def verify():
    data = request.get_json()

    RAZORPAY_ENABLED = os.getenv("RAZORPAY_ENABLED", "false").lower() == "true"

    if RAZORPAY_ENABLED:
        try:
            razor_client.utility.verify_payment_signature({
                'razorpay_order_id': data['order_id'],
                'razorpay_payment_id': data['payment_id'],
                'razorpay_signature': data['signature']
            })
        except Exception as e:
            logger.error(f"Payment verification failed: {e}")
            return jsonify({"ok": False, "error": "अमान्य भुगतान"}), 400

    with get_db() as (conn, cur):
        cur.execute("""
            UPDATE seat_bookings
            SET status='confirmed', payment_id=%s
            WHERE schedule_id=%s AND seat_number=%s
        """, (data.get('payment_id'), data['sid'], data['seat']))
        conn.commit()

    socketio.emit("seat_update", {
        "sid": data['sid'],
        "seat": data['seat']
    })

    return jsonify({"ok": True})


@app.route("/search", methods=["POST"])
@safe_db
def search():
    fs_input = request.form.get("from", "").strip()
    ts_input = request.form.get("to", "").strip()
    travel_date = request.form.get("date", date.today().isoformat())

    session["from"] = fs_input
    session["to"] = ts_input
    session["date"] = travel_date

    if not fs_input or not ts_input:
        return "कृपया दोनों स्टेशन चुनें", 400

    fs = fs_input.lower()
    ts = ts_input.lower()

    with get_db() as (conn, cur):
        cur.execute("""
            SELECT DISTINCT route_id
            FROM route_stations
            WHERE LOWER(station_name) = %s OR LOWER(station_name) = %s
        """, (fs, ts))
        candidate_routes = [r["route_id"] for r in cur.fetchall()]

        if not candidate_routes:
            return render_template_string(
                BASE_HTML,
                content=f"<h3 class='text-center mt-5 text-danger'>🚫 {fs_input} → {ts_input} के लिए कोई बस नहीं</h3>"
            )

        cur.execute("""
            SELECT r.id
            FROM routes r
            JOIN route_stations rs_from ON rs_from.route_id = r.id
            JOIN route_stations rs_to   ON rs_to.route_id = r.id
            WHERE r.id = ANY(%s::int[])
              AND LOWER(rs_from.station_name) = %s
              AND LOWER(rs_to.station_name) = %s
              AND rs_from.station_order < rs_to.station_order
            LIMIT 1
        """, (candidate_routes, fs, ts))
        route = cur.fetchone()

    if not route:
        return render_template_string(
            BASE_HTML,
            content=f"<h3 class='text-center mt-5 text-danger'>🚫 {fs_input} → {ts_input} के लिए कोई वैध मार्ग नहीं</h3>"
        )

    return redirect(f"/buses/{route['id']}")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


# ================= RUN SERVER =================
if __name__ == "__main__":
    logger.info("🚀 Heavy Traffic Optimized Bus App Starting...")
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        debug=False,  # Production mode
        use_reloader=False,
        log_output=True
    )