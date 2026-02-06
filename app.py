# ================= IMPORT SECTION =================
from dotenv import load_dotenv
import os
import json
import time
import logging
import hashlib
from datetime import date, datetime, timedelta
from functools import wraps
from contextlib import contextmanager
from flask import Flask, request, jsonify, render_template_string, redirect, session, g
from flask_socketio import SocketIO, emit
from flask_compress import Compress
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
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
import uuid
import structlog

# ================= CONFIGURATION =================
# लोड environment variables
load_dotenv()
logger = logging.getLogger("mybus")

# कॉन्फिगरेशन क्लास
class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "super-secret-key")
    DATABASE_URL = os.getenv("DATABASE_URL")
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
    RATE_LIMITS = os.getenv("RATE_LIMITS", "200 per minute,50 per second")

    # GPS सेटिंग्स
    GPS_BATCH_SIZE = int(os.getenv("GPS_BATCH_SIZE", 50))
    GPS_FLUSH_INTERVAL = int(os.getenv("GPS_FLUSH_INTERVAL", 5))

    # Socket.IO सेटिंग्स
    SOCKETIO_PING_TIMEOUT = int(os.getenv("SOCKETIO_PING_TIMEOUT", 60))
    SOCKETIO_PING_INTERVAL = int(os.getenv("SOCKETIO_PING_INTERVAL", 25))

    # Razorpay सेटिंग्स
    RAZORPAY_ENABLED = os.getenv("RAZORPAY_ENABLED", "false").lower() == "true"
    RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
    RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")


# ================= REDIS SETUP =================
# Redis को कैशिंग और क्यू के लिए सेटअप करें
redis_client = None
try:
    redis_client = redis.from_url(Config.REDIS_URL, decode_responses=True)
    logger.info("✅ Redis connected successfully")
except Exception as e:
    logger.warning(f"⚠️ Redis not available: {e}")

# ================= RAZORPAY SETUP =================
# ऑनलाइन पेमेंट के लिए Razorpay सेटअप
razorpay_client = None
if Config.RAZORPAY_ENABLED and Config.RAZORPAY_KEY_ID and Config.RAZORPAY_KEY_SECRET:
    razorpay_client = razorpay.Client(auth=(
        Config.RAZORPAY_KEY_ID,
        Config.RAZORPAY_KEY_SECRET
    ))

# ================= FLASK APP SETUP =================
# Flask application को इनिशियलाइज़ करें
app = Flask(__name__)
app.secret_key = Config.SECRET_KEY
Compress(app)  # कॉम्प्रेशन के लिए

# सुरक्षा के लिए CSRF प्रोटेक्शन
csrf = CSRFProtect(app)

# मोबाइल ऐप्स के लिए CORS enable करें
CORS(app, resources={
    r"/api/*": {"origins": ["*"]},
    r"/driver/*": {"origins": ["*"]}
})

# ================= RATE LIMITING =================
# API को abuse से बचाने के लिए rate limiting
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=[Config.RATE_LIMITS],
    storage_uri=Config.REDIS_URL if redis_client else "memory://"
)

# ================= SOCKET.IO SETUP =================
# Real-time communication के लिए Socket.IO
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
# डेटाबेस connection pool बनाएं (रिट्री लॉजिक के साथ)
def create_connection_pool():
    max_retries = 5
    for i in range(max_retries):
        try:
            pool = ConnectionPool(
                conninfo=Config.DATABASE_URL,
                min_size=5,  # minimum connections
                max_size=20,  # maximum connections
                timeout=30,  # connection timeout
                max_idle=300,  # maximum idle time
                max_lifetime=3600  # maximum connection lifetime
            )
            logger.info("✅ Database pool created successfully")
            return pool
        except Exception as e:
            logger.error(f"❌ Pool creation attempt {i + 1} failed: {e}")
            time.sleep(2 ** i)  # Exponential backoff
    raise Exception("Failed to create database pool")


pool = create_connection_pool()


# ================= DATABASE HELPER FUNCTIONS =================
@contextmanager
def get_database_connection(retry_count=3):
    """Database connection with retry logic"""
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


def safe_database(func):
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


# ================= GPS BATCH PROCESSOR =================
class GPSBatchProcessor:
    def __init__(self):
        self.batch = []  # GPS points का बैच
        self.last_flush = time.time()  # अंतिम फ्लश का समय
        self.lock = False  # concurrent access को रोकने के लिए
        self.executor = ThreadPoolExecutor(max_workers=3)  # थ्रेड पूल

    def add(self, data):
        """Add GPS point to batch"""
        # Deduplication using hash
        data_hash = hashlib.md5(
            f"{data['sid']}_{data['lat']}_{data['lng']}_{int(time.time() / 10)}".encode()
        ).hexdigest()

        data['_hash'] = data_hash
        data['_timestamp'] = time.time()

        self.batch.append(data)

        # बैच पूरा होने या टाइम एक्सपायर होने पर फ्लश करें
        if len(self.batch) >= Config.GPS_BATCH_SIZE or (time.time() - self.last_flush) > Config.GPS_FLUSH_INTERVAL:
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
            with get_database_connection() as (conn, cur):
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

                # हर बस के लिए latest position update करें
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

                # क्लाइंट्स को real-time update भेजें
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
            # Failed items को वापस क्यू में डालें
            self.batch.extend(batch)


gps_processor = GPSBatchProcessor()


# ================= ADMIN DECORATOR =================
def admin_required(f):
    """Only admin can access these routes"""

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
    """Create all necessary tables with indexes"""
    conn = None
    cur = None
    try:
        conn = pool.getconn()
        cur = conn.cursor()

        # All tables with optimizations
        tables = [
            # Faces table (for facial recognition)
            """
            CREATE TABLE IF NOT EXISTS faces (
                id SERIAL PRIMARY KEY,
                bus_id INT NOT NULL,
                face_data BYTEA NOT NULL,
                face_image BYTEA NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
            """,
            # Face logs table
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
            # Admins table (with hashed passwords)
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
            # Payments table
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
            # Routes table
            """
            CREATE TABLE IF NOT EXISTS routes (
                id SERIAL PRIMARY KEY, 
                route_name VARCHAR(100) UNIQUE, 
                distance_km INT,
                created_at TIMESTAMP DEFAULT NOW()
            )
            """,
            # Schedules table
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
            # Seat bookings table
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
            # Route stations table
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
            # GPS logs table
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
            # Driver ratings table
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
            # Push notifications subscriptions
            """
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id SERIAL PRIMARY KEY,
                user_id INT NOT NULL,
                subscription_json JSONB NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
            """,
            # Indexes for performance
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

        # Default admin user
        cur.execute("SELECT COUNT(*) FROM admins")
        if cur.fetchone()[0] == 0:
            hashed_password = generate_password_hash("admin123")
            cur.execute("""
                INSERT INTO admins (username, password, role, counter_no)
                VALUES('admin', %s, 'admin', 1)
                ON CONFLICT DO NOTHING
            """, (hashed_password,))

        # Default routes
        cur.execute("SELECT COUNT(*) FROM routes")
        if cur.fetchone()[0] == 0:
            routes = [
                (1, 'Bikaner → Jaipur', 336),
                (2, 'Bikaner → Jodhpur', 252),
                (3, 'Jaipur → Jodhpur', 330)
            ]
            for r in routes:
                cur.execute("INSERT INTO routes VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", r)

            # Default schedules
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

            # Default stations
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


# Initialize database
initialize_database()


# ================= REQUEST ID TRACKING =================
@app.before_request
def assign_request_id():
    """Assign unique ID to each request for tracking"""
    g.request_id = str(uuid.uuid4())


@app.after_request
def add_request_id(response):
    """Add request ID to response headers"""
    response.headers['X-Request-ID'] = getattr(g, 'request_id', '')
    return response


# ================= HTML TEMPLATES =================
BASE_HTML = """
<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
    <title>My Bus AI - Heavy Traffic Optimized</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet"/>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <link href="https://fonts.googleapis.com/css2?family=Hind:wght@300;400;500;600;700&family=Poppins:wght@300;400;600;700&display=swap" rel="stylesheet"/>
    <style>
    * { margin:0; padding:0; box-sizing:border-box; }
    body { background:#f8f9fa; color:#333; font-family:'Hind', sans-serif; }

    .navbar {
        position:fixed; top:0; left:0; width:100%;
        background: linear-gradient(90deg, #2c3e50 0%, #4a6491 100%);
        display:flex; justify-content:space-between; align-items:center;
        padding:12px 5%; box-shadow:0 4px 12px rgba(0,0,0,.15); z-index:1000;
    }

    .logo { 
        font-size:1.8rem; font-weight:700; color:#fff; 
        font-family:'Hind', sans-serif;
    }

    .hero {
        height:100vh;
        background:linear-gradient(rgba(0,0,0,0.7), rgba(0,0,0,0.8)),
        url("https://images.unsplash.com/photo-1544620347-c4fd4a3d5957");
        background-size:cover; background-position:center;
        display:flex; align-items:center; justify-content:center;
        text-align:center; color:white; padding-top:80px;
    }

    .search-box {
        background:rgba(255,255,255,0.95); padding:30px; border-radius:20px; 
        display:flex; gap:15px; box-shadow:0 15px 35px rgba(0,0,0,0.2);
        max-width:900px; margin:0 auto;
    }

    @media(max-width:768px){
        .search-box { flex-direction:column; }
        .hero h1 { font-size:2.2rem; }
    }
    </style>
</head>
<body>
<div class="navbar">
    <div class="logo"><i class="fas fa-bus"></i> माई बस एआई</div>
    <div class="nav-links">
        <a href="/login" style="color:white; text-decoration:none; margin-left:20px;">
            <i class="fas fa-user-shield"></i> Admin Login
        </a>
        <a href="/counter" style="color:white; text-decoration:none; margin-left:20px;">
            <i class="fas fa-desktop"></i> Counter
        </a>
        <a href="/driver/1" target="_blank" style="color:white; text-decoration:none; margin-left:20px;">
            <i class="fas fa-mobile-alt"></i> Driver App
        </a>
    </div>
</div>

{% if not content %}
<section class="hero">
    <div class="container">
        <h1 style="font-size:3.5rem; margin-bottom:20px;">भारत का स्मार्ट बस प्लेटफॉर्म</h1>
        <p style="font-size:1.3rem; margin-bottom:40px;">Book | Track | Heavy Traffic Optimized</p>
        <form class="search-box" action="/search" method="POST">
            <input name="from" placeholder="From (e.g., Bikaner)" required 
                   style="padding:15px 20px; border:2px solid #e0e0e0; border-radius:12px; flex:1;">
            <input name="to" placeholder="To (e.g., Jaipur)" required
                   style="padding:15px 20px; border:2px solid #e0e0e0; border-radius:12px; flex:1;">
            <input type="date" name="date" required min="{{ today }}"
                   style="padding:15px 20px; border:2px solid #e0e0e0; border-radius:12px;">
            <button type="submit" 
                    style="padding:15px 40px; border:none; border-radius:12px; 
                           background:linear-gradient(45deg, #4a6491, #2c3e50); 
                           color:white; font-weight:600; cursor:pointer;">
                <i class="fas fa-search"></i> Search
            </button>
        </form>
    </div>
</section>
{% endif %}

{% if content %}
<div style="padding:100px 5% 50px;">
    {{ content|safe }}
</div>
{% endif %}

<footer style="text-align:center; padding:20px; background:#2c3e50; color:white; margin-top:50px;">
    <p>© 2024 My Bus AI. All rights reserved.</p>
    <p class="small">Developed for India ❤️</p>
</footer>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

LOGIN_HTML = """
<div class="row justify-content-center mt-5">
    <div class="col-md-4 col-sm-8">
        <div class="card shadow-lg border-0 rounded-4">
            <div class="card-body p-4">
                <h3 class="text-center mb-4"><i class="fas fa-sign-in-alt"></i> Login</h3>
                <form method="POST" autocomplete="on">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                    <div class="mb-3">
                        <label class="form-label">Username</label>
                        <input type="text" name="username" class="form-control" required>
                    </div>
                    <div class="mb-4">
                        <label class="form-label">Password</label>
                        <input type="password" name="password" class="form-control" required>
                    </div>
                    <button class="btn btn-success w-100" style="padding:12px;">
                        <i class="fas fa-sign-in-alt"></i> Login
                    </button>
                </form>
                {% if error %}
                    <div class="alert alert-danger mt-3 text-center">{{ error }}</div>
                {% endif %}
            </div>
        </div>
    </div>
</div>
"""


# ================= HEALTH CHECK =================
@app.route("/health")
def health_check():
    """Health check for load balancers"""
    try:
        # Check database connection
        with get_database_connection() as (conn, cur):
            cur.execute("SELECT 1")

        # Check Redis
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


# ================= HOME PAGE =================
@app.route("/")
@safe_database
def home():
    logger.info("Home page opened")
    return "OK"
    """Home page with search form"""
    if "role" not in session:
        session.clear()
        session["role"] = "guest"

    today = date.today().isoformat()

    with get_database_connection() as (conn, cur):
        cur.execute("SELECT DISTINCT station_name FROM route_stations ORDER BY station_name")
        stations = [r["station_name"] for r in cur.fetchall()]

    return render_template_string(BASE_HTML, stations=stations, today=today, content=None)


# ================= LOGIN =================
@app.route("/login", methods=["GET", "POST"])
@safe_database
def login():
    """User login"""
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

    return render_template_string(BASE_HTML, content=render_template_string(LOGIN_HTML, error=error))


# ================= DASHBOARD =================
@app.route("/dashboard")
@safe_database
def dashboard():
    """User dashboard"""
    if not session.get("user_logged_in"):
        return redirect("/login")

    role = session.get("role", "user")
    username = session.get("username", "User")

    # Get statistics for admin
    stats = {}
    if role == "admin":
        with get_database_connection() as (conn, cur):
            # Today's bookings
            cur.execute("""
                SELECT COUNT(*) as count FROM seat_bookings 
                WHERE DATE(created_at) = CURRENT_DATE
            """)
            stats["today_bookings"] = cur.fetchone()["count"]

            # Active buses
            cur.execute("SELECT COUNT(*) as count FROM schedules WHERE is_active=true")
            stats["active_buses"] = cur.fetchone()["count"]

            # Total routes
            cur.execute("SELECT COUNT(*) as count FROM routes")
            stats["total_routes"] = cur.fetchone()["count"]

            # Today's revenue
            cur.execute("""
                SELECT COALESCE(SUM(fare), 0) as total FROM seat_bookings 
                WHERE DATE(created_at) = CURRENT_DATE AND status='confirmed'
            """)
            stats["today_revenue"] = cur.fetchone()["total"]

            # Recent bookings
            cur.execute("""
                SELECT sb.passenger_name, s.bus_name, sb.seat_number, sb.fare, sb.created_at
                FROM seat_bookings sb
                JOIN schedules s ON sb.schedule_id = s.id
                WHERE DATE(sb.created_at) = CURRENT_DATE
                ORDER BY sb.created_at DESC
                LIMIT 5
            """)
            stats["recent_bookings"] = cur.fetchall()

    # Dashboard HTML
    dashboard_html = f"""
    <div class="container mt-4">
        <div class="row">
            <div class="col-md-3">
                <div class="card text-center mb-4">
                    <div class="card-body">
                        <i class="fas fa-user-circle fa-3x mb-3" style="color:#4a6491;"></i>
                        <h5>{username}</h5>
                        <p class="text-muted">{role.upper()}</p>
                    </div>
                </div>

                <div class="card">
                    <div class="card-body">
                        <h6 class="card-title mb-3"><i class="fas fa-bars"></i> Menu</h6>
                        <div class="list-group list-group-flush">
                            <a href="/" class="list-group-item list-group-item-action">
                                <i class="fas fa-home"></i> Home
                            </a>
                            <a href="/search-buses" class="list-group-item list-group-item-action">
                                <i class="fas fa-search"></i> Search Buses
                            </a>
                            <a href="/my-bookings" class="list-group-item list-group-item-action">
                                <i class="fas fa-ticket-alt"></i> My Bookings
                            </a>
    """

    if role == "admin":
        dashboard_html += """
                            <a href="/admin/routes" class="list-group-item list-group-item-action">
                                <i class="fas fa-route"></i> Route Management
                            </a>
                            <a href="/admin/schedules" class="list-group-item list-group-item-action">
                                <i class="fas fa-bus"></i> Schedule Management
                            </a>
                            <a href="/admin/bookings" class="list-group-item list-group-item-action">
                                <i class="fas fa-list"></i> All Bookings
                            </a>
                            <a href="/admin/create-counter" class="list-group-item list-group-item-action">
                                <i class="fas fa-plus-circle"></i> Create Counter
                            </a>
                            <a href="/admin/metrics" class="list-group-item list-group-item-action">
                                <i class="fas fa-chart-line"></i> System Metrics
                            </a>
        """

    dashboard_html += f"""
                            <a href="/logout" class="list-group-item list-group-item-action text-danger">
                                <i class="fas fa-sign-out-alt"></i> Logout
                            </a>
                        </div>
                    </div>
                </div>
            </div>

            <div class="col-md-9">
                <div class="card mb-4">
                    <div class="card-body">
                        <h4 class="card-title mb-4"><i class="fas fa-tachometer-alt"></i> Dashboard</h4>

                        {f'''
                        <div class="row">
                            <div class="col-md-3 col-6 mb-3">
                                <div class="card bg-primary text-white text-center">
                                    <div class="card-body">
                                        <h2>{stats["today_bookings"]}</h2>
                                        <p class="small">Today's Bookings</p>
                                    </div>
                                </div>
                            </div>
                            <div class="col-md-3 col-6 mb-3">
                                <div class="card bg-success text-white text-center">
                                    <div class="card-body">
                                        <h2>{stats["active_buses"]}</h2>
                                        <p class="small">Active Buses</p>
                                    </div>
                                </div>
                            </div>
                            <div class="col-md-3 col-6 mb-3">
                                <div class="card bg-info text-white text-center">
                                    <div class="card-body">
                                        <h2>{stats["total_routes"]}</h2>
                                        <p class="small">Total Routes</p>
                                    </div>
                                </div>
                            </div>
                            <div class="col-md-3 col-6 mb-3">
                                <div class="card bg-warning text-white text-center">
                                    <div class="card-body">
                                        <h2>₹{stats["today_revenue"]}</h2>
                                        <p class="small">Today's Revenue</p>
                                    </div>
                                </div>
                            </div>
                        </div>
                        ''' if role == "admin" else ""}

                        <div class="mt-4">
                            <h5>Quick Actions</h5>
                            <div class="d-flex flex-wrap gap-2 mt-2">
                                <a href="/search-buses" class="btn btn-primary">
                                    <i class="fas fa-search"></i> Search Buses
                                </a>
                                <a href="/live-tracking" class="btn btn-success">
                                    <i class="fas fa-map-marker-alt"></i> Live Tracking
                                </a>
                                {'''
                                <a href="/admin/backup" class="btn btn-dark">
                                    <i class="fas fa-database"></i> Take Backup
                                </a>
                                ''' if role == "admin" else ""}
                            </div>
                        </div>
                    </div>
                </div>

                {f'''
                <div class="card">
                    <div class="card-body">
                        <h5 class="card-title"><i class="fas fa-chart-bar"></i> Recent Bookings</h5>
                        <div class="table-responsive">
                            <table class="table table-hover">
                                <thead>
                                    <tr>
                                        <th>Passenger</th>
                                        <th>Bus</th>
                                        <th>Seat</th>
                                        <th>Fare</th>
                                        <th>Time</th>
                                    </tr>
                                </thead>
                                <tbody>
                ''' + "".join([f'''
                                    <tr>
                                        <td>{b["passenger_name"]}</td>
                                        <td>{b["bus_name"]}</td>
                                        <td>{b["seat_number"]}</td>
                                        <td>₹{b["fare"]}</td>
                                        <td>{b["created_at"].strftime("%H:%M")}</td>
                                    </tr>
                ''' for b in stats.get("recent_bookings", [])]) + '''
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>
                ''' if role == "admin" and stats.get("recent_bookings") else ""}
            </div>
        </div>
    </div>
    """

    return render_template_string(BASE_HTML, content=dashboard_html)


# ================= BUS SEARCH =================
@app.route("/search", methods=["POST"])
@safe_database
def search_buses():
    """Search buses between stations"""
    from_station = request.form.get("from", "").strip()
    to_station = request.form.get("to", "").strip()
    travel_date = request.form.get("date", date.today().isoformat())

    session["from"] = from_station
    session["to"] = to_station
    session["date"] = travel_date

    if not from_station or not to_station:
        return "Please select both stations", 400

    with get_database_connection() as (conn, cur):
        # Find routes that connect these stations
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
        return render_template_string(
            BASE_HTML,
            content=f"""
            <div class="alert alert-danger text-center mt-5" style="max-width:500px; margin:auto;">
                <h4><i class="fas fa-exclamation-triangle"></i> No Buses Found</h4>
                <p>No direct buses found from {from_station} to {to_station}</p>
                <a href="/" class="btn btn-primary mt-3">Search Again</a>
            </div>
            """
        )

    return redirect(f"/buses/{route['id']}")


# ================= BUSES LIST =================
@app.route("/buses/<int:route_id>")
@safe_database
def buses_list(route_id):
    """Show buses for a specific route"""
    with get_database_connection() as (conn, cur):
        # Get route details
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

        # Get buses for this route
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

    buses_html = ""
    for bus in buses:
        # Calculate status based on last GPS update
        time_diff = 999999
        if bus['last_gps_update']:
            time_diff = (datetime.now() - bus['last_gps_update']).total_seconds()

        if time_diff < 300:  # 5 minutes
            status = "🟢 LIVE"
            status_class = "success"
        elif time_diff < 600:  # 10 minutes
            status = "🟡 DELAYED"
            status_class = "warning"
        else:
            status = "⚪ OFFLINE"
            status_class = "secondary"

        available_seats = bus['total_seats'] - bus['booked_count']

        buses_html += f"""
        <div class="card mb-3">
            <div class="card-body">
                <div class="d-flex justify-content-between align-items-center">
                    <div>
                        <h5>{bus['bus_name']}</h5>
                        <p class="text-muted mb-1">
                            <i class="fas fa-clock"></i> Departure: {bus['departure_time'].strftime('%H:%M')}
                        </p>
                    </div>
                    <span class="badge bg-{status_class}">{status}</span>
                </div>

                <div class="row mt-3">
                    <div class="col-md-6">
                        <p><i class="fas fa-chair"></i> Available Seats: {available_seats}</p>
                        <p><i class="fas fa-bus"></i> Total Seats: {bus['total_seats']}</p>
                    </div>
                    <div class="col-md-6">
                        {f'<p class="small text-muted"><i class="fas fa-sync-alt"></i> Last Update: {bus["last_gps_update"].strftime("%H:%M:%S")}</p>' if bus['last_gps_update'] else ''}
                    </div>
                </div>

                <div class="d-flex gap-2 mt-3">
                    <a href="/live-bus/{bus['id']}" class="btn btn-primary flex-fill">
                        <i class="fas fa-map-marker-alt"></i> Live GPS
                    </a>
                    <a href="/seats/{bus['id']}" class="btn btn-success flex-fill">
                        <i class="fas fa-ticket-alt"></i> Book Seat
                    </a>
                </div>
            </div>
        </div>
        """

    content = f"""
    <div class="container">
        <div class="card mb-4">
            <div class="card-body text-center">
                <h2>{route['route_name']}</h2>
                <p class="text-muted">
                    <i class="fas fa-route"></i> {route['stations']} | 
                    <i class="fas fa-road"></i> {route['distance_km']} km
                </p>
            </div>
        </div>

        {buses_html if buses else '''
        <div class="alert alert-warning text-center">
            <h4><i class="fas fa-bus-slash"></i> No Buses Available</h4>
            <p>No active buses found for this route today.</p>
            <a href="/" class="btn btn-primary mt-2">Search Other Routes</a>
        </div>
        '''}
    </div>
    """

    return render_template_string(BASE_HTML, content=content)


# ================= SEAT SELECTION =================
@app.route("/seats/<int:schedule_id>")
@safe_database
def seat_selection(schedule_id):
    """Seat selection page"""
    with get_database_connection() as (conn, cur):
        # Get schedule details
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

        # Get booked seats for today
        today = session.get("date", date.today().isoformat())
        cur.execute("""
            SELECT seat_number
            FROM seat_bookings
            WHERE schedule_id=%s AND travel_date=%s AND status='confirmed'
        """, (schedule_id, today))
        booked_seats = set(r['seat_number'] for r in cur.fetchall())

    # Generate seat buttons
    seat_buttons = ""
    for i in range(1, schedule['total_seats'] + 1):
        if i in booked_seats:
            seat_buttons += f'<button class="btn btn-danger m-1" disabled>X{i}</button>'
        else:
            seat_buttons += f'<button class="btn btn-success m-1" onclick="selectSeat({i})">{i}</button>'

    content = f"""
    <div class="container">
        <div class="card mb-4">
            <div class="card-body">
                <h3>{schedule['bus_name']}</h3>
                <p class="text-muted">
                    Route: {schedule['route_name']} | 
                    Departure: {schedule['departure_time'].strftime('%H:%M')}
                </p>
                <p>Travel Date: {today}</p>
            </div>
        </div>

        <div class="card">
            <div class="card-body">
                <h4 class="mb-4">Select Your Seat</h4>
                <div class="seat-grid mb-4">
                    {seat_buttons}
                </div>

                <div id="bookingForm" style="display:none;">
                    <h5>Passenger Details</h5>
                    <form id="passengerForm">
                        <input type="hidden" id="selectedSeat" value="">
                        <div class="row">
                            <div class="col-md-6 mb-3">
                                <label>Passenger Name</label>
                                <input type="text" id="passengerName" class="form-control" required>
                            </div>
                            <div class="col-md-6 mb-3">
                                <label>Mobile Number</label>
                                <input type="tel" id="mobileNumber" class="form-control" required>
                            </div>
                        </div>
                        <button type="button" class="btn btn-primary" onclick="confirmBooking()">
                            Confirm Booking
                        </button>
                    </form>
                </div>
            </div>
        </div>
    </div>

    <style>
    .seat-grid {{
        display: grid;
        grid-template-columns: repeat(10, 1fr);
        gap: 10px;
    }}
    @media (max-width: 768px) {{
        .seat-grid {{
            grid-template-columns: repeat(5, 1fr);
        }}
    }}
    </style>

    <script>
    function selectSeat(seatNumber) {{
        document.getElementById('selectedSeat').value = seatNumber;
        document.getElementById('bookingForm').style.display = 'block';
        window.scrollTo({{
            top: document.getElementById('bookingForm').offsetTop,
            behavior: 'smooth'
        }});
    }}

    function confirmBooking() {{
        const seatNumber = document.getElementById('selectedSeat').value;
        const passengerName = document.getElementById('passengerName').value;
        const mobileNumber = document.getElementById('mobileNumber').value;

        if (!passengerName || !mobileNumber) {{
            alert('Please fill all details');
            return;
        }}

        fetch('/book', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{
                schedule_id: {schedule_id},
                seat_number: seatNumber,
                passenger_name: passengerName,
                mobile: mobileNumber,
                date: '{today}'
            }})
        }})
        .then(response => response.json())
        .then(data => {{
            if (data.ok) {{
                alert('Booking confirmed! Fare: ₹' + data.fare);
                window.location.reload();
            }} else {{
                alert('Error: ' + data.error);
            }}
        }})
        .catch(error => {{
            console.error('Error:', error);
            alert('Network error, please try again');
        }});
    }}
    </script>
    """

    return render_template_string(BASE_HTML, content=content)


# ================= BOOKING API =================
@app.route("/book", methods=["POST"])
@safe_database
@limiter.limit("10 per minute")
def book_seat():
    """API to book a seat"""
    data = request.get_json()

    # Generate booking hash for idempotency
    booking_hash = hashlib.sha256(
        f"{data['schedule_id']}_{data['seat_number']}_{data['date']}_{data['passenger_name']}".encode()
    ).hexdigest()

    with get_database_connection() as (conn, cur):
        # Check if seat is already booked
        cur.execute("""
            SELECT id FROM seat_bookings
            WHERE schedule_id=%s AND seat_number=%s AND travel_date=%s AND status='confirmed'
        """, (data['schedule_id'], data['seat_number'], data['date']))

        if cur.fetchone():
            return jsonify({"ok": False, "error": "Seat already booked"}), 409

        # Check for duplicate booking
        cur.execute("SELECT id FROM seat_bookings WHERE booking_hash=%s", (booking_hash,))
        if cur.fetchone():
            return jsonify({"ok": True, "fare": 0, "message": "Already booked"})

        # Calculate fare (random for demo, in real app use fixed fares)
        fare = random.randint(250, 450)
        user_role = session.get("role", "user")

        # Insert booking
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

    # Notify all connected clients
    socketio.emit("seat_update", {
        "sid": data['schedule_id'],
        "seat": data['seat_number'],
        "date": data['date']
    })

    return jsonify({"ok": True, "fare": fare})


# ================= LIVE TRACKING =================
@app.route("/live-bus/<int:schedule_id>")
@safe_database
def live_tracking(schedule_id):
    """Live bus tracking page"""
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

        # Get route stations
        cur.execute("""
            SELECT station_name, lat, lng
            FROM route_stations
            WHERE route_id = (
                SELECT route_id FROM schedules WHERE id = %s
            )
            ORDER BY station_order
        """, (schedule_id,))
        stations = cur.fetchall()

    content = f"""
    <div class="container">
        <div class="card mb-4">
            <div class="card-body">
                <h3><i class="fas fa-bus"></i> {bus['bus_name']}</h3>
                <p class="text-muted">
                    Route: {bus['route_name']} | 
                    Departure: {bus['departure_time'].strftime('%H:%M')}
                </p>
                <div id="status" class="alert alert-info">
                    <i class="fas fa-sync-alt fa-spin"></i> Connecting...
                </div>
            </div>
        </div>

        <div class="card">
            <div class="card-body">
                <div id="map" style="height: 500px; border-radius: 10px;"></div>
            </div>
        </div>

        <div class="card mt-4">
            <div class="card-body">
                <h5><i class="fas fa-map-marker-alt"></i> Route Stations</h5>
                <div class="row">
                    {"".join([f'''
                    <div class="col-md-3 col-6 mb-2">
                        <div class="card">
                            <div class="card-body text-center">
                                <i class="fas fa-map-pin text-primary"></i>
                                <p class="mb-0">{station['station_name']}</p>
                            </div>
                        </div>
                    </div>
                    ''' for station in stations])}
                </div>
            </div>
        </div>
    </div>

    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>

    <script>
    const map = L.map('map').setView([{bus['lat'] or 26.9124}, {bus['lng'] or 75.7873}], 10);

    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
        attribution: '© OpenStreetMap contributors'
    }}).addTo(map);

    // Add stations
    {"".join([f'''
    L.marker([{station['lat']}, {station['lng']}])
        .addTo(map)
        .bindPopup("📍 {station['station_name']}");
    ''' for station in stations if station['lat'] and station['lng']])}

    // Bus marker
    const busIcon = L.divIcon({{
        html: '<div style="background: #dc3545; width: 20px; height: 20px; border-radius: 50%;"></div>',
        className: 'bus-marker'
    }});

    let busMarker = L.marker([{bus['lat'] or 26.9124}, {bus['lng'] or 75.7873}], {{icon: busIcon}})
        .addTo(map)
        .bindPopup("<b>{bus['bus_name']}</b>");

    // Socket.IO connection
    const socket = io();

    socket.on('connect', () => {{
        document.getElementById('status').className = 'alert alert-success';
        document.getElementById('status').innerHTML = '<i class="fas fa-check-circle"></i> Connected - Live Tracking Active';
    }});

    socket.on('bus_location', data => {{
        if(data.sid == {schedule_id}){{
            const lat = parseFloat(data.lat);
            const lng = parseFloat(data.lng);
            busMarker.setLatLng([lat, lng]);
            map.panTo([lat, lng]);

            document.getElementById('status').innerHTML = 
               `<i class="fas fa-bus"></i> Live - Speed: ${'data.speed' | 0} km/h | ` +
                `Updated: ${'new Date().toLocaleTimeString()'}`;
        }}
    }});

    socket.on('disconnect', () => {{
        document.getElementById('status').className = 'alert alert-warning';
        document.getElementById('status').innerHTML = '<i class="fas fa-exclamation-triangle"></i> Disconnected - Reconnecting...';
    }});
    </script>
    """

    return render_template_string(BASE_HTML, content=content)


# ================= DRIVER APP =================
@app.route("/driver/<int:bus_id>")
def driver_app(bus_id):
    """Driver GPS tracking app"""
    return f"""
<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bus {bus_id} - Driver App</title>
    <style>
    body {{ 
        margin: 0; 
        padding: 20px; 
        background: #f0f2f5; 
        font-family: Arial, sans-serif;
    }}

    .container {{ 
        max-width: 800px; 
        margin: 0 auto; 
    }}

    .header {{ 
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
        padding: 20px;
        border-radius: 10px;
        text-align: center;
        margin-bottom: 20px;
    }}

    .status-card {{ 
        background: white;
        padding: 15px;
        border-radius: 10px;
        margin-bottom: 10px;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    }}

    .btn {{ 
        background: #4CAF50;
        color: white;
        border: none;
        padding: 15px 30px;
        border-radius: 5px;
        cursor: pointer;
        font-size: 16px;
        width: 100%;
        margin-bottom: 10px;
    }}

    .btn.stop {{ background: #f44336; }}

    .coordinates {{ 
        display: grid;
        grid-template-columns: repeat(2, 1fr);
        gap: 10px;
        margin-bottom: 20px;
    }}

    .log {{ 
        background: #1a1a1a;
        color: #00ff00;
        padding: 10px;
        border-radius: 5px;
        height: 200px;
        overflow-y: auto;
        font-family: monospace;
        margin-top: 20px;
    }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1><i class="fas fa-bus"></i> Bus {bus_id} - Driver App</h1>
            <p>Heavy Traffic Optimized GPS Tracking</p>
        </div>

        <div class="coordinates">
            <div class="status-card">
                <h4>Latitude</h4>
                <p id="lat">--</p>
            </div>
            <div class="status-card">
                <h4>Longitude</h4>
                <p id="lng">--</p>
            </div>
            <div class="status-card">
                <h4>Speed</h4>
                <p id="speed">-- km/h</p>
            </div>
            <div class="status-card">
                <h4>Accuracy</h4>
                <p id="accuracy">-- meters</p>
            </div>
        </div>

        <button class="btn" id="startBtn" onclick="startTracking()">
            <i class="fas fa-play"></i> START TRACKING
        </button>

        <button class="btn stop" id="stopBtn" onclick="stopTracking()" disabled>
            <i class="fas fa-stop"></i> STOP TRACKING
        </button>

        <div class="log" id="log">
            <div>Driver App Ready...</div>
        </div>
    </div>

    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <script>
    const socket = io();
    let watchId = null;
    let isTracking = false;

    function log(message) {{
        const logDiv = document.getElementById('log');
        const time = new Date().toLocaleTimeString();
        logDiv.innerHTML = `<div>[${'time'}] ${'message'}</div>` + logDiv.innerHTML;
    }}

    function startTracking() {{
        if (!navigator.geolocation) {{
            alert("GPS not supported by your browser");
            return;
        }}

        log("Starting GPS tracking...");
        document.getElementById('startBtn').disabled = true;
        document.getElementById('stopBtn').disabled = false;
        isTracking = true;

        watchId = navigator.geolocation.watchPosition(
            (position) => {{
                const lat = position.coords.latitude;
                const lng = position.coords.longitude;
                const speed = position.coords.speed ? (position.coords.speed * 3.6).toFixed(1) : 0;
                const accuracy = position.coords.accuracy.toFixed(0);

                // Update display
                document.getElementById('lat').textContent = lat.toFixed(6);
                document.getElementById('lng').textContent = lng.toFixed(6);
                document.getElementById('speed').textContent = speed + ' km/h';
                document.getElementById('accuracy').textContent = accuracy + ' meters';

                // Send to server
                socket.emit('driver_gps', {{
                    sid: {bus_id},
                    lat: lat,
                    lng: lng,
                    speed: speed,
                    accuracy: accuracy
                }});

                log(`GPS: ${'lat.toFixed(6)'}, ${'lng.toFixed(6)'}, Speed: ${'speed'} km/h`);
            }},
            (error) => {{
                log("GPS Error: " + error.message);
            }},
            {{
                enableHighAccuracy: true,
                timeout: 10000,
                maximumAge: 0
            }}
        );
    }}

    function stopTracking() {{
        if (watchId) {{
            navigator.geolocation.clearWatch(watchId);
            watchId = null;
        }}

        document.getElementById('startBtn').disabled = false;
        document.getElementById('stopBtn').disabled = true;
        isTracking = false;
        log("GPS tracking stopped");
    }}

    // Socket.IO events
    socket.on('connect', () => {{
        log("Connected to server");
    }});

    socket.on('disconnect', () => {{
        log("Disconnected from server");
    }});

    // Keep app awake
    let wakeLock = null;
    async function requestWakeLock() {{
        try {{
            wakeLock = await navigator.wakeLock.request('screen');
            log("Screen wake lock active");
        }} catch (err) {{
            log("Wake lock failed: " + err.message);
        }}
    }}

    // Request wake lock when starting
    if ('wakeLock' in navigator) {{
        requestWakeLock();
    }}

    // Handle page visibility
    document.addEventListener('visibilitychange', () => {{
        if (document.hidden && isTracking) {{
            log("App in background - GPS continues");
        }}
    }});
    </script>
</body>
</html>
"""


# ================= SOCKET.IO EVENTS =================
@socketio.on("connect")
def handle_connect():
    """Handle client connection"""
    logger.info(f"✅ Client connected: {request.sid}")


@socketio.on("driver_gps")
def handle_driver_gps(data):
    """Handle GPS data from driver app"""
    try:
        # Validate data
        sid = int(data.get('sid', 0))
        lat = float(data.get('lat', 0))
        lng = float(data.get('lng', 0))
        speed = float(data.get('speed', 0))
        accuracy = float(data.get('accuracy', 999))

        if not sid or not lat or not lng:
            return

        # Add to batch processor
        gps_processor.add({
            'sid': sid,
            'lat': lat,
            'lng': lng,
            'speed': speed,
            'accuracy': accuracy
        })

    except Exception as e:
        logger.error(f"GPS handling error: {e}")


# ================= SYSTEM METRICS =================
@app.route("/admin/metrics")
@admin_required
@safe_database
def system_metrics():
    """System performance metrics"""
    with get_database_connection() as (conn, cur):
        # Database metrics
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

    # Redis metrics
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

    # System metrics
    import psutil
    system_metrics = {
        "cpu_percent": psutil.cpu_percent(),
        "memory_percent": psutil.virtual_memory().percent,
        "disk_usage": psutil.disk_usage('/').percent
    }

    metrics_html = f"""
    <div class="container">
        <div class="card mb-4">
            <div class="card-body">
                <h4><i class="fas fa-chart-line"></i> System Metrics</h4>
                <p class="text-muted">Last Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
            </div>
        </div>

        <div class="row">
            <div class="col-md-4 mb-3">
                <div class="card">
                    <div class="card-body">
                        <h5><i class="fas fa-database"></i> Database</h5>
                        <ul class="list-unstyled">
                            <li>GPS Points (1h): {db_metrics['gps_points_1h']}</li>
                            <li>Bookings (1h): {db_metrics['bookings_1h']}</li>
                            <li>Active Buses: {db_metrics['active_buses']}</li>
                            <li>Active Connections: {db_metrics['active_connections']}</li>
                            <li>Revenue (1h): ₹{db_metrics['revenue_1h']}</li>
                        </ul>
                    </div>
                </div>
            </div>

            <div class="col-md-4 mb-3">
                <div class="card">
                    <div class="card-body">
                        <h5><i class="fas fa-memory"></i> Redis</h5>
                        <ul class="list-unstyled">
                            {f'''
                            <li>Connected Clients: {redis_metrics.get('connected_clients', 'N/A')}</li>
                            <li>Used Memory: {redis_metrics.get('used_memory', 'N/A')}</li>
                            <li>Total Keys: {redis_metrics.get('total_keys', 'N/A')}</li>
                            <li>Uptime: {redis_metrics.get('uptime', 'N/A')}s</li>
                            ''' if redis_metrics.get('status') != 'not_available' else '<li>Redis not available</li>'}
                        </ul>
                    </div>
                </div>
            </div>

            <div class="col-md-4 mb-3">
                <div class="card">
                    <div class="card-body">
                        <h5><i class="fas fa-server"></i> System</h5>
                        <ul class="list-unstyled">
                            <li>CPU Usage: {system_metrics['cpu_percent']}%</li>
                            <li>Memory Usage: {system_metrics['memory_percent']}%</li>
                            <li>Disk Usage: {system_metrics['disk_usage']}%</li>
                            <li>Request ID: {g.request_id}</li>
                        </ul>
                    </div>
                </div>
            </div>
        </div>

        <div class="card mt-3">
            <div class="card-body">
                <h5><i class="fas fa-cog"></i> Quick Actions</h5>
                <div class="d-flex gap-2">
                    <a href="/admin/backup" class="btn btn-primary">
                        <i class="fas fa-database"></i> Take Backup
                    </a>
                    <button class="btn btn-warning" onclick="clearCache()">
                        <i class="fas fa-broom"></i> Clear Cache
                    </button>
                    <a href="/health" class="btn btn-info">
                        <i class="fas fa-heartbeat"></i> Health Check
                    </a>
                </div>
            </div>
        </div>
    </div>

    <script>
    function clearCache() {{
        fetch('/admin/clear-cache', {{ method: 'POST' }})
        .then(response => response.json())
        .then(data => {{
            alert(data.message || 'Cache cleared');
            location.reload();
        }});
    }}
    </script>
    """

    return render_template_string(BASE_HTML, content=metrics_html)


# ================= LOGOUT =================
@app.route("/logout")
def logout():
    """Logout user"""
    session.clear()
    return redirect("/")


# ================= CLEANUP TASK =================
def cleanup_old_data():
    """Remove old data periodically"""
    try:
        with get_database_connection() as (conn, cur):
            # Delete GPS logs older than 30 days
            cur.execute("""
                DELETE FROM gps_logs 
                WHERE timestamp < NOW() - INTERVAL '30 days'
            """)

            # Archive bookings older than 1 year
            cur.execute("""
                DELETE FROM seat_bookings 
                WHERE created_at < NOW() - INTERVAL '1 year'
                AND status = 'confirmed'
            """)

            conn.commit()
            logger.info("✅ Cleaned up old data")

    except Exception as e:
        logger.error(f"❌ Cleanup failed: {e}")


# ================= RUN APPLICATION =================
if __name__ == "__main__":
    logger.info("🚀 Starting Heavy Traffic Optimized Bus App...")

    # Schedule cleanup job (every day at 2 AM)
    import schedule
    import threading


    def run_scheduled_jobs():
        while True:
            schedule.run_pending()
            time.sleep(60)


    schedule.every().day.at("02:00").do(cleanup_old_data)

    # Start scheduler in background thread
    scheduler_thread = threading.Thread(target=run_scheduled_jobs, daemon=True)
    scheduler_thread.start()

    # Run the application
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        debug=False,  # Production mode
        use_reloader=False,
        log_output=True
    )