import eventlet
# MUST BE FIRST: Monkey patch before any other imports
eventlet.monkey_patch()

import json
import time
from datetime import datetime, date
from dotenv import load_dotenv
load_dotenv()
import setuptools
import os
import random
from functools import wraps, lru_cache
from flask import Flask, request, jsonify, render_template_string, redirect, g, session
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_compress import Compress
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
import atexit
import razorpay
import threading
from contextlib import contextmanager

# ================= SIMPLE IN-MEMORY CACHE =================
class MemoryCache:
    def __init__(self):
        self.store = {}
        self.ttl = {}
    
    def get(self, key):
        if key in self.store:
            if key in self.ttl and time.time() > self.ttl[key]:
                del self.store[key]
                del self.ttl[key]
                return None
            return self.store[key]
        return None
    
    def set(self, key, value, ex=None):
        self.store[key] = value
        if ex:
            self.ttl[key] = time.time() + ex
    
    def delete(self, key):
        if key in self.store:
            del self.store[key]
        if key in self.ttl:
            del self.ttl[key]
    
    def flushall(self):
        self.store.clear()
        self.ttl.clear()

cache = MemoryCache()
print("✅ Using in-memory cache")

# ================= RAZORPAY CLIENT =================
RAZORPAY_ENABLED = bool(
    os.getenv("RAZORPAY_KEY_ID") and
    os.getenv("RAZORPAY_KEY_SECRET")
)

if RAZORPAY_ENABLED:
    razor_client = razorpay.Client(auth=(
        os.getenv("RAZORPAY_KEY_ID"),
        os.getenv("RAZORPAY_KEY_SECRET")
    ))
else:
    razor_client = None
    print("⚠️ Razorpay not configured")

# ================= APP INITIALIZATION =================
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key-change-in-production")
Compress(app)

# ================= RATE LIMITER =================
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per minute", "50 per second"],
    storage_uri="memory://",
    strategy="fixed-window",
    headers_enabled=True
)

# ================= SOCKETIO OPTIMIZED =================
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='eventlet',
    logger=True,
    engineio_logger=False,
    ping_timeout=30,
    ping_interval=25,
    max_http_buffer_size=1e8,
    async_handlers=True,
    manage_session=False,
    http_compression=True,
    compression_threshold=1024,
    cookie=None
)

# SocketIO connection tracking
socket_connections = {}
MAX_CONNECTIONS_PER_IP = 50
SOCKET_RATE_LIMITS = {}

# ================= DATABASE CONFIGURATION =================
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("❌ DATABASE_URL not found in environment variables")
    print("⚠️ Using default PostgreSQL URL for Render.com")
    # Render.com provides DATABASE_URL automatically
    DATABASE_URL = "postgresql://localhost/busdb"

# Create connection pool
try:
    pool = ConnectionPool(
        conninfo=DATABASE_URL,
        min_size=2,
        max_size=10,
        timeout=15,
        open=True,
        max_idle=300,
        num_workers=2,
       kwargs={"keepalives": 1, "keepalives_idle": 30}
    )
    print(f"✅ Connection pool ready: min={pool.min_size}, max={pool.max_size}")
except Exception as e:
    print(f"❌ Failed to create connection pool: {e}")
    pool = None

# ================= DATABASE CONNECTION MANAGEMENT =================
db_context = threading.local()

@contextmanager
def get_db_connection():
    if not pool:
        raise Exception("Database pool not initialized")

    conn = None
    cur = None
    try:
        conn = pool.getconn()
        cur = conn.cursor(row_factory=dict_row)
        yield cur
        conn.commit()
    except Exception as e:
        if "connection is lost" in str(e).lower():
            print("🔄 Reconnecting DB...")
            pool.close()
            pool.open()

        if conn:
            conn.rollback()
        raise e
    finally:
        if cur:
            try: cur.close()
            except: pass
        if conn:
            try: pool.putconn(conn)
            except: pass

# ================= CLEANUP FUNCTIONS =================
@app.teardown_appcontext
def close_db(error=None):
    """Cleanup database connections"""
    if hasattr(db_context, 'conn'):
        try:
            if db_context.conn and not db_context.conn.closed:
                pool.putconn(db_context.conn)
        except:
            pass
        finally:
            db_context.conn = None
            db_context.cur = None

# ================= SOCKETIO RATE LIMITING =================
def check_socket_rate_limit(sid, event_type, max_per_minute=60):
    """Rate limiting for SocketIO events"""
    now = time.time()
    key = f"{sid}:{event_type}"

    if key not in SOCKET_RATE_LIMITS:
        SOCKET_RATE_LIMITS[key] = []

    # Clean old timestamps
    SOCKET_RATE_LIMITS[key] = [ts for ts in SOCKET_RATE_LIMITS[key] if now - ts < 60]

    if len(SOCKET_RATE_LIMITS[key]) >= max_per_minute:
        return False

    SOCKET_RATE_LIMITS[key].append(now)
    return True

# ================= SOCKETIO EVENT HANDLERS =================
@socketio.on("connect")
def handle_connect():
    """Handle new connections with rate limiting"""
    client_ip = request.remote_addr
    client_id = request.sid

    # Rate limiting per IP
    if client_ip in socket_connections:
        if len(socket_connections[client_ip]) >= MAX_CONNECTIONS_PER_IP:
            print(f"⚠️ Connection limit exceeded for {client_ip}")
            return False

    # Track connection
    if client_ip not in socket_connections:
        socket_connections[client_ip] = set()
    socket_connections[client_ip].add(client_id)

    print(f"✅ Client connected: {client_id} from {client_ip}")
    return True

@socketio.on("disconnect")
def handle_disconnect():
    """Handle disconnections"""
    client_ip = request.remote_addr
    client_id = request.sid

    if client_ip in socket_connections and client_id in socket_connections[client_ip]:
        socket_connections[client_ip].remove(client_id)
        if not socket_connections[client_ip]:
            del socket_connections[client_ip]

    print(f"❌ Client disconnected: {client_id}")

@socketio.on("join_room")
def handle_join_room(data):
    """Join specific room for targeted updates"""
    room_name = data.get('room')
    if room_name:
        join_room(room_name)
        print(f"✅ {request.sid} joined room: {room_name}")

@socketio.on("leave_room")
def handle_leave_room(data):
    """Leave room"""
    room_name = data.get('room')
    if room_name:
        leave_room(room_name)
        print(f"✅ {request.sid} left room: {room_name}")

@socketio.on("driver_gps")
def handle_gps(data):
    """Handle GPS updates with rate limiting"""
    if not check_socket_rate_limit(request.sid, "driver_gps", max_per_minute=30):
        print(f"⚠️ Rate limited GPS from {request.sid}")
        return

    sid = data.get('sid')
    lat = float(data.get('lat', 27.5))
    lng = float(data.get('lng', 75.0))
    speed = float(data.get('speed', 0))

    print(f"📍 LIVE: Bus-{sid} @ [{lat:.5f},{lng:.5f}] {speed}km/h")

    try:
        with get_db_connection() as cur:
            cur.execute("""
                UPDATE schedules 
                SET current_lat=%s, current_lng=%s
                WHERE id=%s
            """, (lat, lng, sid))
    except Exception as e:
        print(f"Database update error: {e}")

    # Send only to bus-specific room
    room_name = f"bus_{sid}"
    emit("bus_location", {
        "sid": sid,
        "lat": lat,
        "lng": lng,
        "speed": speed,
        "timestamp": data.get('timestamp', datetime.now().isoformat())
    }, room=room_name, namespace='/')

# ================= CACHING FUNCTIONS =================
@lru_cache(maxsize=128)
def get_route_stations(route_id):
    """Cache route stations"""
    try:
        with get_db_connection() as cur:
            cur.execute("""
                SELECT station_name, station_order, lat, lng
                FROM route_stations
                WHERE route_id = %s
                ORDER BY station_order
            """, (route_id,))
            return cur.fetchall()
    except:
        return []

@lru_cache(maxsize=256)
def get_booked_seats(schedule_id, travel_date):
    """Cache booked seats for 30 seconds"""
    try:
        with get_db_connection() as cur:
            cur.execute("""
                SELECT seat_number
                FROM seat_bookings
                WHERE schedule_id = %s 
                AND travel_date = %s
                AND status = 'confirmed'
            """, (schedule_id, travel_date))
            return [r['seat_number'] for r in cur.fetchall()]
    except:
        return []

# ================= ADMIN REQUIRED DECORATOR =================
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
def init_db():
    """Initialize database tables"""
    print("🔄 Initializing database...")
    
    if pool is None:
        print("❌ Database pool not available, skipping initialization")
        return

    try:
        with get_db_connection() as cur:
            # Create tables if they don't exist
            cur.execute("""
                CREATE TABLE IF NOT EXISTS faces (
                    id SERIAL PRIMARY KEY,
                    bus_id INT NOT NULL,
                    face_data BYTEA NOT NULL,
                    face_image BYTEA NOT NULL
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS face_logs (
                    id SERIAL PRIMARY KEY,
                    face_id INT NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
                    bus_id INT NOT NULL,
                    entry_time TIMESTAMP NOT NULL,
                    latitude DOUBLE PRECISION,
                    longitude DOUBLE PRECISION
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS admins (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(50) UNIQUE,
                    password VARCHAR(100),
                    role VARCHAR(20) DEFAULT 'admin',
                    counter_no INTEGER DEFAULT 0
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id SERIAL PRIMARY KEY,
                    schedule_id INT,
                    seat_number INT,
                    order_id VARCHAR(100),
                    payment_id VARCHAR(100),
                    amount INT,
                    status VARCHAR(20),
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS routes (
                    id SERIAL PRIMARY KEY, 
                    route_name VARCHAR(100) UNIQUE, 
                    distance_km INT
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS schedules (
                    id SERIAL PRIMARY KEY, 
                    route_id INT REFERENCES routes(id), 
                    bus_name VARCHAR(100),
                    departure_time TIME, 
                    current_lat DOUBLE PRECISION,
                    current_lng DOUBLE PRECISION,
                    total_seats INT DEFAULT 40
                );
            """)

            cur.execute("""
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
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS route_stations (
                    id SERIAL PRIMARY KEY, 
                    route_id INT REFERENCES routes(id), 
                    station_name VARCHAR(50), 
                    station_order INT,
                    lat DOUBLE PRECISION DEFAULT 27.2,
                    lng DOUBLE PRECISION DEFAULT 75.2
                );
            """)

            # Check if tables are empty and insert default data
            cur.execute("SELECT COUNT(*) as count FROM admins")
            admin_count = cur.fetchone()
            if admin_count and admin_count['count'] == 0:
                cur.execute("""
                    INSERT INTO admins (username, password, role, counter_no)
                    VALUES ('admin', 'admin123', 'admin', 1)
                    ON CONFLICT (username) DO NOTHING;
                """)
                print("✅ Default admin created")

            cur.execute("SELECT COUNT(*) as count FROM routes")
            route_count = cur.fetchone()
            if route_count and route_count['count'] == 0:
                # Insert default routes
                routes_data = [
                    (1, 'बीकानेर → जयपुर', 336),
                    (2, 'बीकानेर → जोधपुर', 252),
                    (3, 'जयपुर → जोधपुर', 330)
                ]
                for route in routes_data:
                    cur.execute(
                        "INSERT INTO routes (id, route_name, distance_km) VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING",
                        route
                    )

                # Insert default schedules
                schedules_data = [
                    (1, 1, 'Volvo AC Sleeper', '08:00'),
                    (2, 1, 'Semi Sleeper AC', '10:30'),
                    (3, 2, 'Volvo AC Seater', '09:00'),
                    (4, 3, 'Deluxe AC', '07:30')
                ]
                for schedule in schedules_data:
                    cur.execute("""
                        INSERT INTO schedules (id, route_id, bus_name, departure_time, total_seats)
                        VALUES (%s, %s, %s, %s::time, 40)
                        ON CONFLICT (id) DO NOTHING
                    """, schedule)

                # Insert default stations
                stations_data = [
                    (1, 'बीकानेर', 1, 27.2, 75.2),
                    (1, 'जयपुर', 2, 26.9, 75.8),
                    (2, 'बीकानेर', 1, 27.2, 75.2),
                    (2, 'जोधपुर', 2, 26.3, 73.0),
                    (3, 'जयपुर', 1, 26.9, 75.8),
                    (3, 'जोधपुर', 2, 26.3, 73.0)
                ]
                for station in stations_data:
                    cur.execute("""
                        INSERT INTO route_stations (route_id, station_name, station_order, lat, lng)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                    """, station)
                
                print("✅ Default routes, schedules and stations created")

        print("✅ Database initialization completed successfully!")

    except Exception as e:
        print(f"⚠️ Database initialization warning: {e}")
        # Continue anyway - tables might already exist

# Initialize database
init_db()

# ================= HTML TEMPLATES =================
BASE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>My Bus AI - Book Your Journey</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600;700&display=swap" rel="stylesheet">

<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:'Poppins',sans-serif;}
body{background:#f5f7fb;color:#222;}

/* Navbar */
.navbar{
  position:fixed;
  top:0;left:0;width:100%;
  background:white;
  display:flex;
  justify-content:space-between;
  align-items:center;
  padding:15px 8%;
  box-shadow:0 5px 20px rgba(0,0,0,.1);
  z-index:1000;
}
.logo{font-size:1.5rem;font-weight:700;color:#ff512f;}
.navbar a{margin-left:20px;text-decoration:none;color:#333;font-weight:500;}

/* Hero */
.hero{
  height:100vh;
  background:
    linear-gradient(rgba(0,0,0,.6),rgba(0,0,0,.8)),
    url("https://images.unsplash.com/photo-1544620347-c4fd4a3d5957");
  background-size:cover;
  background-position:center;
  display:flex;
  align-items:center;
  justify-content:center;
  text-align:center;
  color:white;
  padding-top:70px;
}

/* Search Box */
.search-box{
  background:white;
  padding:20px;
  border-radius:15px;
  display:flex;
  gap:10px;
  max-width:800px;
  margin:0 auto;
}
.search-box input{
  padding:12px;
  border:none;
  border-radius:8px;
  outline:none;
  flex:1;
}
.search-box button{
  padding:12px 30px;
  border:none;
  border-radius:10px;
  background:#ff512f;
  color:white;
  font-weight:600;
  cursor:pointer;
}

/* Cards */
.card{
  background:white;
  border-radius:15px;
  box-shadow:0 10px 25px rgba(0,0,0,.1);
  padding:20px;
  margin-bottom:20px;
}

/* Mobile Responsive */
@media(max-width:768px){
  .navbar{
    flex-direction:column;
    gap:10px;
    padding:10px 20px;
  }
  .navbar a{
    margin:5px;
  }
  .search-box{
    flex-direction:column;
    width:90%;
  }
  .search-box input,
  .search-box button{
    width:100%;
  }
  .hero h1{
    font-size:1.6rem;
    padding:0 10px;
  }
}
</style>
</head>
<body>

<div class="navbar">
  <div class="logo">🚌 My Bus AI</div>
  <div>
    <a href="/login">Admin login</a>
    <a href="/counter">Counter</a>
    <a href="/admin/monitor" style="color:red;">📊 Monitor</a>
  </div>
</div>

{% if not content %}
<section class="hero">
  <div>
    <h1>India's Smart Bus Platform</h1>
    <p>Book | Track | Face Boarding | Live Seats</p>

    <form class="search-box" action="/search" method="POST">
      <input name="from" placeholder="From" required>
      <input name="to" placeholder="To" required>
      <input type="date" name="date" required>
      <button type="submit">Search</button>
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

        <h3 class="text-center mb-4">Admin Login</h3>

       <form method="POST" autocomplete="on">
          <input type="text" name="username"
                 class="form-control mb-3"
                 placeholder="Username" required>

          <input type="password" name="password"
                 class="form-control mb-3"
                 placeholder="Password" required>

          <button class="btn btn-success w-100">
            Login
          </button>
        </form>

        {% if error %}
          <div class="text-danger text-center mt-3">
            {{ error }}
          </div>
        {% endif %}

      </div>
    </div>
  </div>
</div>
"""

# ================= ROUTES =================
@app.route("/")
@limiter.limit("100 per minute")
def home():
    if "role" not in session:
        session["role"] = "guest"
    return render_template_string(BASE_HTML, content=None)

@app.route("/dashboard")
@limiter.limit("50 per minute")
def dashboard():
    if not session.get("user_logged_in"):
        return redirect("/login")

    role = session.get("role", "user")

    admin_links = ""
    if role.lower() == "admin":
        admin_links = """
        <div class="mt-3">
            <a href="/routes" class="btn btn-info me-2">🛣️ Manage Routes</a>
            <a href="/schedules" class="btn btn-warning me-2">🚌 Manage Schedules</a>
            <a href="/bookings" class="btn btn-success">🎫 View Bookings</a>
            <a href="/create-counter" class="btn btn-primary">🎫 Create Counter</a>
        </div>
        """

    return render_template_string(
        BASE_HTML,
        content=f"""
        <div class="text-center mt-5">
            <h2>Welcome 🎉</h2>
            <h4>Role: <b>{role.upper()}</b></h4>

            <div class="mt-4">
                <a href="/" class="btn btn-primary">🏠 Home</a>
                <a href="/logout" class="btn btn-danger ms-2">🚪 Logout</a>
            </div>

            {admin_links}
        </div>
        """
    )

@app.route("/buses/<int:rid>")
@limiter.limit("30 per minute")
def buses(rid):
    try:
        with get_db_connection() as cur:
            # Get route details
            cur.execute("""
                SELECT r.route_name, r.distance_km
                FROM routes r 
                WHERE r.id = %s
            """, (rid,))
            route = cur.fetchone()

            if not route:
                return "Route not found", 404
            
            # Get stations for this route
            cur.execute("""
                SELECT station_name, station_order
                FROM route_stations 
                WHERE route_id = %s 
                ORDER BY station_order
            """, (rid,))
            stations = cur.fetchall()
            
            # Format stations string
            station_names = [s['station_name'] for s in stations]
            route['stations'] = " → ".join(station_names) if station_names else "Route stations"

            # Get buses for this route
            cur.execute("""
                SELECT s.id, s.bus_name, s.departure_time, s.total_seats,
                       s.current_lat, s.current_lng,
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
    except Exception as e:
        return f"Database error: {e}", 500

    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🚌 {route['route_name']} - Premium Booking</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
    body {{ font-family:'Poppins',sans-serif; margin:0; background:linear-gradient(135deg,#00c6ff,#0072ff); color:#fff; }}
    header {{ text-align:center; padding:60px 20px 40px; }}
    header h1 {{ font-size:42px; font-weight:700; margin-bottom:10px; }}
    header p {{ font-size:18px; opacity:0.9; }}
    .bus-card {{background: rgba(255,255,255,0.15); border-radius:20px; padding:20px; margin-bottom:25px; backdrop-filter: blur(10px); transition: transform 0.3s;}}
    .bus-card:hover {{transform:translateY(-5px);}}
    .bus-card h5 {{font-weight:700; font-size:22px;}}
    .bus-card .badge {{font-weight:500; padding:8px 14px; font-size:14px; border-radius:12px;}}
    .bus-card p {{margin:5px 0; font-size:15px;}}
    .bus-card .btn {{border-radius:50px; font-weight:600; padding:10px 25px;}}
    .bus-card .btn:hover {{transform: scale(1.05);}}
    .bus-info i {{margin-right:8px; color:#ffd700;}}
    footer {{text-align:center; padding:20px 0; background: rgba(0,0,0,0.2); color:#fff;}}
    @media(max-width:768px){{header h1{{font-size:28px;}} .bus-card h5{{font-size:18px;}}}}
    </style>
    </head>
    <body>

    <header>
        <h1>🚌 {route['route_name']}</h1>
        <p>📍 {route['stations']} | 🛣️ {route['distance_km']} km</p>
    </header>

    <div class="container mt-5">
        <div class="row justify-content-center">
            <div class="col-md-8">
                {"".join([f'''
                <div class="bus-card">
                    <div class="d-flex justify-content-between align-items-center">
                        <h5>{bus['bus_name']} <i class="fas fa-bus"></i></h5>
                        <span class="badge {'bg-success' if bus['current_lat'] else 'bg-secondary'}">
                            {'🟢 LIVE' if bus['current_lat'] else '⚪ Offline'}
                        </span>
                    </div>
                    <div class="bus-info mt-2">
                        <p><i class="fas fa-clock"></i> Departure: {bus['departure_time'].strftime('%H:%M')}</p>
                        <p><i class="fas fa-chair"></i> Seats Left: {bus['total_seats'] - bus['booked_count']} | Total Seats: {bus['total_seats']}</p>
                    </div>
                    <div class="d-flex flex-wrap gap-2 mt-3">
                        <a href="/live-bus/{bus['id']}" class="btn btn-primary flex-fill">🗺️ Live GPS</a>
                        <a href="/seats/{bus['id']}" class="btn btn-success flex-fill">🎫 Book Seat</a>
                    </div>
                </div>
                ''' for bus in buses_data]) if buses_data else '<div class="alert alert-warning text-center">No buses available today</div>'}
            </div>
        </div>
    </div>

    <footer>
        &copy; 2026 MyBus. All Rights Reserved.
    </footer>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
    </body>
    </html>
    """

    return html

@app.route("/create-counter", methods=["GET", "POST"])
@admin_required
@limiter.limit("10 per minute")
def create_counter():
    error = ""
    success = ""

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        if not username or not password:
            error = "Please fill all fields"
        else:
            try:
                with get_db_connection() as cur:
                    cur.execute("""
                        INSERT INTO admins (username, password, role)
                        VALUES (%s, %s, 'counter')
                        ON CONFLICT (username) DO NOTHING
                    """, (username, password))
                    success = f"Counter '{username}' created successfully ✅"
            except Exception as e:
                error = str(e)

    form_html = f"""
    <div class="card mx-auto" style="max-width:500px; margin-top:40px;">
        <div class="card-body">
            <h4 class="card-title text-center mb-4">➕ Create New Counter</h4>
            
            <form method="POST">
                <div class="mb-3">
                    <label class="form-label">Username</label>
                    <input type="text" name="username" class="form-control" required>
                </div>
                <div class="mb-3">
                    <label class="form-label">Password</label>
                    <input type="password" name="password" class="form-control" required>
                </div>
                
                <button class="btn btn-success w-100">Create Counter</button>
            </form>
            
            {f"<div class='text-success mt-3'>{success}</div>" if success else ""}
            {f"<div class='text-danger mt-3'>{error}</div>" if error else ""}
        </div>
    </div>
    """

    return render_template_string(BASE_HTML, content=form_html)

@app.route("/login", methods=["GET", "POST"])
@limiter.limit("20 per minute")
def login():
    error = ""

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        try:
            with get_db_connection() as cur:
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
                    error = "Invalid username or password"

        except Exception as e:
            print(f"Login error: {e}")
            error = "Server error"

    return render_template_string(
        BASE_HTML,
        content=render_template_string(LOGIN_HTML, error=error)
    )

@app.route("/counter", methods=["GET", "POST"])
@limiter.limit("20 per minute")
def counter():
    error = ""

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        try:
            with get_db_connection() as cur:
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
                    error = "Invalid username or password"

        except Exception as e:
            print(f"Counter login error: {e}")
            error = "Server error"

    return render_template_string(
        BASE_HTML,
        content=render_template_string(LOGIN_HTML, error=error)
    )

@app.route("/seats/<int:sid>")
@limiter.limit("30 per minute")
def seat_page(sid):
    try:
        with get_db_connection() as cur:
            # Get schedule details
            cur.execute("""
                SELECT s.id, s.bus_name, s.departure_time, r.route_name,
                       r.id as route_id, s.current_lat, s.current_lng
                FROM schedules s
                JOIN routes r ON s.route_id = r.id
                WHERE s.id = %s
            """, (sid,))
            schedule = cur.fetchone()

            if not schedule:
                return "Schedule not found", 404

            # Get booked seats
            today = session.get("date", date.today().isoformat())
            booked_seats = set(get_booked_seats(sid, today))

            # Generate seat buttons
            seat_buttons = ""
            for i in range(1, 41):
                if i in booked_seats:
                    seat_buttons += f'''
                    <button id="seat-{i}" class="btn btn-danger seat" disabled>X{i}</button>
                    '''
                else:
                    seat_buttons += f'''
                    <button id="seat-{i}" class="btn btn-success seat" onclick="bookSeat({i})">{i}</button>
                    '''

    except Exception as e:
        return f"Database error: {e}", 500

    # User info
    user_role = session.get("role", "guest")
    counter_id = session.get("user_id") if user_role in ("counter", "conductor") else None
    bus_lat = schedule['current_lat'] if schedule['current_lat'] else 27.5
    bus_lon = schedule['current_lng'] if schedule['current_lng'] else 75.0

    html_content = f"""
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    
    <div class="container" style="max-width:900px;margin:auto;">
        <h2>Bus: {schedule['bus_name']} | Route: {schedule['route_name']}</h2>
        <h4>Departure: {schedule['departure_time'].strftime('%H:%M')}</h4>
        <h5>Role: <span style="color:{'red' if user_role=='admin' else 'green' if user_role=='counter' else 'blue'};font-weight:bold;">{user_role.upper()}</span></h5>
        
        <h5>Live Location</h5>
        <div id="map" style="width:100%;height:300px;border-radius:12px;"></div>
        
        <h5 style="margin-top:30px;">Select Seat</h5>
        <div style="display:flex;flex-wrap:wrap;gap:10px;">
            {seat_buttons}
        </div>
    </div>
    
    <script>
    const socket = io(window.location.origin);
    const SID = {sid};
    const TODAY = "{today}";
    const BUS_LAT = {bus_lat};
    const BUS_LNG = {bus_lon};
    const COUNTER_ID = {counter_id if counter_id else 'null'};
    
    socket.emit("join_room", {{ room: `bus_${{SID}}` }});
    
    const map = L.map('map').setView([BUS_LAT, BUS_LNG], 15);
    L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
        attribution: '© OpenStreetMap',
        subdomains: 'abcd',
        maxZoom: 19
    }}).addTo(map);
    
    let busMarker = L.marker([BUS_LAT, BUS_LNG]).addTo(map);
    
    socket.on("bus_location", data => {{
        if(data.sid == SID){{
            let lat = parseFloat(data.lat);
            let lng = parseFloat(data.lng);
            busMarker.setLatLng([lat, lng]);
            map.flyTo([lat, lng], map.getZoom());
        }}
    }});
    
    socket.on("seat_update", function(data) {{
        if(SID != data.sid || TODAY != data.date) return;
        
        let btn = document.getElementById("seat-" + data.seat);
        if(btn){{
            btn.classList.remove("btn-success");
            btn.classList.add("btn-danger");
            btn.disabled = true;
            btn.innerText = "X" + data.seat;
        }}
    }});
    
    function bookSeat(seatId){{
        let name = prompt("Passenger Name:");
        if(!name) return;
        
        let mobile = prompt("Mobile Number:");
        if(!mobile) return;
        
        let btn = document.getElementById("seat-" + seatId);
        let oldText = btn.innerText;
        btn.innerText = "⏳ Booking...";
        btn.disabled = true;
        
        let fare = null;
        let payment_mode = "cash";
        
        if(COUNTER_ID !== null){{
            fare = prompt("Fare amount:");
            if(!fare || isNaN(fare)){{
                alert("Invalid fare");
                btn.innerText = oldText;
                btn.disabled = false;
                return;
            }}
            
            payment_mode = prompt("Payment mode: cash / online", "cash");
            if(payment_mode !== "cash" && payment_mode !== "online"){{
                alert("Only cash or online allowed");
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
                alert("Seat booked! Fare: ₹" + res.fare);
            }} else {{
                alert(res.error || res.msg);
                btn.innerText = oldText;
                btn.disabled = false;
            }}
        }})
        .catch(err => {{
            console.error(err);
            btn.innerText = oldText;
            btn.disabled = false;
        }});
    }}
    
    window.addEventListener('beforeunload', () => {{
        socket.emit("leave_room", {{ room: `bus_${{SID}}` }});
    }});
    </script>
    """

    return render_template_string(BASE_HTML, content=html_content)

@app.route("/heartbeat")
def heartbeat():
    return "ok", 200

@app.route("/book", methods=["POST"])
@limiter.limit("20 per minute")
booked_by_id = session.get("user_id")
if not booked_by_id:
    booked_by_id = 0
def book():
    data = request.get_json()

    try:
        with get_db_connection() as cur:
            # Check if seat already booked
            cur.execute("""
                SELECT id FROM seat_bookings
                WHERE schedule_id=%s 
                AND seat_number=%s 
                AND travel_date=%s
                AND status='confirmed'
            """, (data['schedule_id'], data['seat_number'], data['date']))

            if cur.fetchone():
                return jsonify({"ok": False, "error": "Seat already booked"}), 409

            # Determine fare and payment mode
            user_role = session.get("role", "user")
            if user_role == "counter":
                fare = int(data.get("fare", 0))
                payment_mode = data.get("payment_mode", "cash")
            else:
                fare = random.randint(250, 450)
                payment_mode = "cash"

            # Insert booking
            cur.execute("""
                INSERT INTO seat_bookings
                (schedule_id, seat_number, passenger_name, mobile,
                 from_station, to_station, travel_date,
                 fare, status, payment_mode,
                 booked_by_type, booked_by_id, counter_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                int(data['schedule_id']),
                int(data['seat_number']),
                data['passenger_name'],
                data['mobile'],
                session.get("from", ""),
                session.get("to", ""),
                data['date'],
                int(fare),
                'confirmed',
                payment_mode,
                user_role,
               int(session.get("user_id", 0))
               # int(session.get("user_id", 0)),
                int(data.get("counter_id") or 0)
            ))

            # Emit seat update
            room_name = f"bus_{data['schedule_id']}"
            socketio.emit("seat_update", {
                "sid": data['schedule_id'],
                "seat": data['seat_number'],
                "date": data['date']
            }, room=room_name, namespace='/')

            return jsonify({"ok": True, "fare": fare})

    except Exception as e:
        print(f"Booking error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/driver/<int:bus_id>")
@limiter.limit("30 per minute")
def driver_advanced(bus_id):
    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>🚗 Driver GPS - Bus {bus_id}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{ font-family: Arial; padding: 20px; }}
            button {{ padding: 15px; margin: 10px; font-size: 16px; }}
            #status {{ padding: 10px; border-radius: 5px; margin: 10px 0; }}
        </style>
    </head>
    <body>
        <h1>🚗 Driver GPS (Optimized)</h1>
        <div id="status" style="background: #eee;">Status: Ready</div>
        
        <button onclick="startGPS()">🚀 Start GPS</button>
        <button onclick="stopGPS()" disabled>🛑 Stop GPS</button>
        
        <div id="logs" style="margin-top: 20px; height: 200px; overflow-y: auto; 
              border: 1px solid #ccc; padding: 10px; font-family: monospace; font-size: 12px;">
        </div>
        
        <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
        <script>
        const socket = io();
        const busId = {bus_id};
        let watchId = null;
        
        function addLog(msg) {{
            const logs = document.getElementById('logs');
            const time = new Date().toLocaleTimeString();
            logs.innerHTML = `[${{time}}] ${{msg}}<br>` + logs.innerHTML;
        }}
        
        function startGPS() {{
            if (!navigator.geolocation) {{
                alert('GPS not supported');
                return;
            }}
            
            const options = {{
                enableHighAccuracy: true,
                maximumAge: 0,
                timeout: 10000
            }};
            
            watchId = navigator.geolocation.watchPosition(
                (position) => {{
                    const lat = position.coords.latitude;
                    const lng = position.coords.longitude;
                    const speed = (position.coords.speed || 0) * 3.6;
                    
                    document.getElementById('status').innerHTML = 
                        `📍 Location: ${{lat.toFixed(4)}}, ${{lng.toFixed(4)}}<br>` +
                        `🚀 Speed: ${{speed.toFixed(1)}} km/h<br>` +
                        `⏰ ${{new Date().toLocaleTimeString()}}`;
                    document.getElementById('status').style.background = '#d4edda';
                    
                    socket.emit('driver_gps', {{
                        sid: busId,
                        lat: lat,
                        lng: lng,
                        speed: speed,
                        timestamp: new Date().toISOString()
                    }});
                    
                    addLog(`📍 Update: ${{lat.toFixed(6)}}, ${{lng.toFixed(6)}}`);
                }},
                
                (error) => {{
                    let msg = 'GPS Error: ';
                    switch(error.code) {{
                        case 1: msg += 'Permission denied'; break;
                        case 2: msg += 'Position unavailable'; break;
                        case 3: msg += 'Timeout'; break;
                        default: msg += 'Unknown';
                    }}
                    
                    document.getElementById('status').innerHTML = msg;
                    document.getElementById('status').style.background = '#f8d7da';
                    addLog('❌ ' + msg);
                }},
                
                options
            );
            
            document.querySelector('button[onclick="startGPS()"]').disabled = true;
            document.querySelector('button[onclick="stopGPS()"]').disabled = false;
            addLog('✅ GPS Started');
        }}
        
        function stopGPS() {{
            if (watchId) {{
                navigator.geolocation.clearWatch(watchId);
                watchId = null;
            }}
            
            document.getElementById('status').innerHTML = 'GPS Stopped';
            document.getElementById('status').style.background = '#f8f9fa';
            document.querySelector('button[onclick="startGPS()"]').disabled = false;
            document.querySelector('button[onclick="stopGPS()"]').disabled = true;
            
            addLog('🛑 GPS Stopped');
        }}
        
        addLog('Page loaded');
        addLog('Click "Start GPS" to begin tracking');
        </script>
    </body>
    </html>
    '''

    return html

@app.route("/live-bus/<int:bus_id>")
@limiter.limit("30 per minute")
def live_bus(bus_id):
    try:
        with get_db_connection() as cur:
            cur.execute("""
                SELECT s.id, s.bus_name, s.current_lat, s.current_lng, 
                       r.route_name, s.departure_time, r.distance_km
                FROM schedules s
                LEFT JOIN routes r ON s.route_id = r.id
                WHERE s.id = %s
            """, (bus_id,))
            bus = cur.fetchone()
    except:
        bus = None

    if not bus:
        bus = {
            'id': bus_id,
            'bus_name': f'Bus {bus_id}',
            'current_lat': 27.5,
            'current_lng': 75.0,
            'route_name': 'Unknown Route',
            'departure_time': '08:00',
            'distance_km': 0
        }

    bus_lat = bus['current_lat'] or 27.5
    bus_lng = bus['current_lng'] or 75.0

    html_content = f'''
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>🚌 Live Tracking - {bus['bus_name']}</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
        <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
        <style>
            body {{
                background: linear-gradient(135deg, #0f2027, #203a43, #2c5364);
                color: white;
                padding: 20px;
                min-height: 100vh;
            }}
            #map {{
                height: 400px;
                width: 100%;
                border-radius: 15px;
                margin: 20px 0;
                border: 3px solid #00ff88;
                box-shadow: 0 10px 30px rgba(0,255,136,0.3);
            }}
            .info-card {{
                background: rgba(255,255,255,0.1);
                backdrop-filter: blur(10px);
                border-radius: 15px;
                padding: 15px;
                margin-bottom: 15px;
                border: 1px solid rgba(0,255,136,0.3);
            }}
            .live-badge {{
                background: #ff0000;
                color: white;
                padding: 5px 15px;
                border-radius: 20px;
                font-weight: bold;
                animation: pulse 1.5s infinite;
            }}
            @keyframes pulse {{
                0% {{ opacity: 1; }}
                50% {{ opacity: 0.5; }}
                100% {{ opacity: 1; }}
            }}
        </style>
    </head>
    <body>
    
    <div class="container">
        <div class="d-flex justify-content-between align-items-center mb-4">
            <h1 class="mb-0">🚌 Live Bus Tracking</h1>
            <span class="live-badge">LIVE</span>
        </div>
    
        <div class="info-card">
            <h3>{bus['bus_name']}</h3>
            <p class="mb-1"><strong>Route:</strong> {bus['route_name']}</p>
            <p class="mb-1"><strong>Departure:</strong> {bus['departure_time']}</p>
            <p class="mb-0"><strong>Distance:</strong> {bus['distance_km']} km</p>
        </div>
    
        <div id="map"></div>
    
        <div class="row mt-4">
            <div class="col-md-6">
                <div class="info-card">
                    <h4>📍 Current Location</h4>
                    <div class="row">
                        <div class="col-6">
                            <p><strong>Latitude:</strong><br>
                            <span id="busLat" class="h5">{bus_lat:.6f}</span></p>
                        </div>
                        <div class="col-6">
                            <p><strong>Longitude:</strong><br>
                            <span id="busLng" class="h5">{bus_lng:.6f}</span></p>
                        </div>
                    </div>
                    <div class="row mt-2">
                        <div class="col-6">
                            <p><strong>Speed:</strong><br>
                            <span id="busSpeed" class="h5">0 km/h</span></p>
                        </div>
                        <div class="col-6">
                            <p><strong>Last Update:</strong><br>
                            <span id="busUpdate" class="h6">-</span></p>
                        </div>
                    </div>
                </div>
            </div>
    
            <div class="col-md-6">
                <div class="info-card">
                    <h4>📊 Controls</h4>
                    <div class="d-grid gap-2">
                        <a href="/driver/{bus_id}" class="btn btn-success btn-lg">🚗 Driver Mode</a>
                        <a href="/seats/{bus_id}" class="btn btn-primary btn-lg">🎫 Book Seat</a>
                        <a href="/" class="btn btn-secondary">🏠 Home</a>
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    
    <script>
    const socket = io();
    const busId = {bus_id};
    let map = null;
    let marker = null;
    
    function initMap() {{
        map = L.map('map').setView([{bus_lat}, {bus_lng}], 13);
        
        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
            attribution: '© OpenStreetMap',
            maxZoom: 19
        }}).addTo(map);
        
        socket.emit("join_room", {{ room: `bus_${{busId}}` }});
        
        const busIcon = L.divIcon({{
            html: '<div style="background: linear-gradient(135deg, #ff0000, #ff8800); color: white; padding: 15px; border-radius: 50%; font-size: 24px; border: 4px solid white; box-shadow: 0 0 25px rgba(255,0,0,0.7);">🚌</div>',
            className: 'live-bus-icon',
            iconSize: [70, 70]
        }});
        
        marker = L.marker([{bus_lat}, {bus_lng}], {{icon: busIcon}})
            .addTo(map)
            .bindPopup('<b>{bus['bus_name']}</b><br>Live Tracking<br>Speed: 0 km/h')
            .openPopup();
    }}
    
    socket.on('bus_location', function(data) {{
        if (data.sid == busId) {{
            const lat = parseFloat(data.lat);
            const lng = parseFloat(data.lng);
            const speed = parseFloat(data.speed) || 0;
    
            document.getElementById('busLat').textContent = lat.toFixed(6);
            document.getElementById('busLng').textContent = lng.toFixed(6);
            document.getElementById('busSpeed').textContent = speed.toFixed(1) + ' km/h';
            document.getElementById('busUpdate').textContent = new Date().toLocaleTimeString();
    
            if (map && marker) {{
                marker.setLatLng([lat, lng]);
                map.panTo([lat, lng]);
                marker.setPopupContent('<b>{bus['bus_name']}</b><br>Speed: ' + speed.toFixed(1) + ' km/h<br>' + new Date().toLocaleTimeString());
            }}
        }}
    }});
    
    document.addEventListener('DOMContentLoaded', function() {{
        initMap();
        
        window.addEventListener('beforeunload', () => {{
            socket.emit("leave_room", {{ room: `bus_${{busId}}` }});
        }});
    }});
    </script>
    </body>
    </html>
    '''

    return html_content

@app.route("/search", methods=["POST"])
@limiter.limit("30 per minute")
def search():
    fs_input = request.form.get("from", "").strip()
    ts_input = request.form.get("to", "").strip()
    travel_date = request.form.get("date", date.today().isoformat())

    session["from"] = fs_input
    session["to"] = ts_input
    session["date"] = travel_date

    if not fs_input or not ts_input:
        return "Please select both From and To stations", 400

    fs = fs_input.lower()
    ts = ts_input.lower()

    try:
        with get_db_connection() as cur:
            # Find routes with both stations
            cur.execute("""
                SELECT DISTINCT route_id
                FROM route_stations
                WHERE LOWER(station_name) = %s OR LOWER(station_name) = %s
            """, (fs, ts))

            candidate_routes = [r["route_id"] for r in cur.fetchall()]

            if not candidate_routes:
                return render_template_string(
                    BASE_HTML,
                    content=f"<h3 class='text-center mt-5 text-danger'>🚫 No buses for {fs_input} → {ts_input}</h3>"
                )

            # Check correct order
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
                    content=f"<h3 class='text-center mt-5 text-danger'>🚫 No valid route for {fs_input} → {ts_input}</h3>"
                )

            return redirect(f"/buses/{route['id']}")

    except Exception as e:
        return f"Search error: {e}", 500

# ================= ADMIN ENDPOINTS =================
@app.route("/admin/monitor")
@admin_required
def admin_monitor():
    html = """
    <div class="container mt-4">
        <h2>🚨 System Monitor Dashboard</h2>
        
        <div class="row mt-4">
            <div class="col-md-4">
                <div class="card">
                    <div class="card-header bg-primary text-white">📊 Database Stats</div>
                    <div class="card-body">
                        <p>SocketIO Clients: <strong id="socketClients">0</strong></p>
                        <p>Active Connections: <strong id="activeConns">0</strong></p>
                        <p>Memory Usage: <strong id="memoryUsage">0 MB</strong></p>
                    </div>
                </div>
            </div>
            
            <div class="col-md-8">
                <div class="card">
                    <div class="card-header bg-success text-white">⚡ Quick Actions</div>
                    <div class="card-body">
                        <div class="d-grid gap-2 d-md-block">
                            <button class="btn btn-warning me-2" onclick="clearCache()">🗑️ Clear Cache</button>
                            <button class="btn btn-info me-2" onclick="healthCheck()">🏥 Health Check</button>
                            <a href="/dashboard" class="btn btn-primary">🏠 Dashboard</a>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <script>
    function clearCache() {
        fetch('/admin/clear-cache', {method: 'POST'})
            .then(r => r.json())
            .then(data => alert(data.message || data.error))
            .catch(err => alert('Error: ' + err));
    }
    
    function healthCheck() {
        fetch('/health')
            .then(r => r.json())
            .then(data => {
                let status = data.status === 'healthy' ? '✅ Healthy' : '❌ Unhealthy';
                alert(`Health Status: ${status}\\nDatabase: ${data.database}\\nSocketIO: ${data.socketio}`);
            })
            .catch(err => alert('Health check failed: ' + err));
    }
    </script>
    """
    return render_template_string(BASE_HTML, content=html)

@app.route("/admin/clear-cache", methods=["POST"])
@admin_required
def clear_cache():
    """Clear all caches"""
    try:
        # Clear LRU caches
        get_route_stations.cache_clear()
        get_booked_seats.cache_clear()
        
        # Clear memory cache
        cache.flushall()
        
        return jsonify({"message": "Cache cleared successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/health")
def health_check():
    """Health check endpoint"""
    try:
        # Test database
        with get_db_connection() as cur:
            cur.execute("SELECT 1 as test")
            db_test = cur.fetchone()
            db_ok = db_test and db_test['test'] == 1

        # Test SocketIO
        socket_ok = socketio.async_mode is not None

        # Test pool
        pool_ok = pool is not None and not pool.closed

        return jsonify({
            "status": "healthy" if all([db_ok, socket_ok, pool_ok]) else "unhealthy",
            "database": "ok" if db_ok else "error",
            "socketio": "ok" if socket_ok else "error",
            "pool": "ok" if pool_ok else "error",
            "timestamp": datetime.now().isoformat(),
            "version": "1.0.0"
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500

@app.route("/logout")
def logout():
    """Logout user"""
    session.clear()
    return redirect("/")

@app.route("/routes")
@admin_required
def manage_routes():
    """Manage routes page"""
    try:
        with get_db_connection() as cur:
            cur.execute("SELECT * FROM routes ORDER BY id")
            routes = cur.fetchall()
    except Exception as e:
        routes = []
    
    routes_html = "<h2>Manage Routes</h2><table class='table'><tr><th>ID</th><th>Route</th><th>Distance</th></tr>"
    for r in routes:
        routes_html += f"<tr><td>{r['id']}</td><td>{r['route_name']}</td><td>{r['distance_km']} km</td></tr>"
    routes_html += "</table>"
    
    return render_template_string(BASE_HTML, content=routes_html)

@app.route("/schedules")
@admin_required
def manage_schedules():
    """Manage schedules page"""
    try:
        with get_db_connection() as cur:
            cur.execute("""
                SELECT s.*, r.route_name 
                FROM schedules s 
                LEFT JOIN routes r ON s.route_id = r.id
                ORDER BY s.id
            """)
            schedules = cur.fetchall()
    except Exception as e:
        schedules = []
    
    schedules_html = "<h2>Manage Schedules</h2><table class='table'><tr><th>ID</th><th>Bus</th><th>Route</th><th>Time</th></tr>"
    for s in schedules:
        schedules_html += f"<tr><td>{s['id']}</td><td>{s['bus_name']}</td><td>{s['route_name']}</td><td>{s['departure_time']}</td></tr>"
    schedules_html += "</table>"
    
    return render_template_string(BASE_HTML, content=schedules_html)

@app.route("/bookings")
@admin_required
def view_bookings():
    """View bookings page"""
    try:
        with get_db_connection() as cur:
            cur.execute("""
                SELECT b.*, s.bus_name, r.route_name
                FROM seat_bookings b
                LEFT JOIN schedules s ON b.schedule_id = s.id
                LEFT JOIN routes r ON s.route_id = r.id
                ORDER BY b.created_at DESC
                LIMIT 50
            """)
            bookings = cur.fetchall()
    except Exception as e:
        bookings = []
    
    bookings_html = "<h2>Recent Bookings</h2><table class='table'><tr><th>ID</th><th>Passenger</th><th>Bus</th><th>Route</th><th>Seat</th><th>Fare</th><th>Date</th></tr>"
    for b in bookings:
        bookings_html += f"<tr><td>{b['id']}</td><td>{b['passenger_name']}</td><td>{b['bus_name']}</td><td>{b['route_name']}</td><td>{b['seat_number']}</td><td>₹{b['fare']}</td><td>{b['travel_date']}</td></tr>"
    bookings_html += "</table>"
    
    return render_template_string(BASE_HTML, content=bookings_html)

# ================= CLEANUP =================
def cleanup():
    """Cleanup on application exit"""
    print("🔄 Cleaning up resources...")
    try:
        if pool and not pool.closed:
            pool.close()
        socket_connections.clear()
        print("✅ Cleanup completed")
    except Exception as e:
        print(f"❌ Cleanup error: {e}")

atexit.register(cleanup)

# ================= STARTUP =================
def startup_check():
    """Run startup health checks"""
    print("\n" + "="*50)
    print("🚀 Starting Health Checks...")
    
    # Check environment variables
    if os.getenv("DATABASE_URL"):
        print("✅ DATABASE_URL: Set")
    else:
        print("⚠️ DATABASE_URL: Not set")
    
    if os.getenv("SECRET_KEY"):
        print("✅ SECRET_KEY: Set")
    else:
        print("⚠️ SECRET_KEY: Not set")
    
    # Check database connection
    try:
        with get_db_connection() as cur:
            cur.execute("SELECT version()")
            db_version = cur.fetchone()
            print(f"✅ Database: Connected")
    except Exception as e:
        print(f"❌ Database: Connection failed - {str(e)[:100]}")
    
    print("="*50 + "\n")

# Run startup checks
startup_check()

# ================= MAIN =================
if __name__ == "__main__":
    print("🚀 Bus Booking App Starting...")
    port = int(os.environ.get("PORT", 10000))
    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        debug=False,
        allow_unsafe_werkzeug=True,
        log_output=False
    )
