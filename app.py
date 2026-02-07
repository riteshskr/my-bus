from dotenv import load_dotenv
import json
import time
from datetime import datetime, date
load_dotenv()
import setuptools
import os, random
from datetime import date
from functools import wraps, lru_cache
from flask import Flask, request, jsonify, render_template_string, redirect, g, session
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_compress import Compress
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
import atexit
import razorpay
import threading
import eventlet
from contextlib import contextmanager
from collections import defaultdict

# Monkey patch for eventlet
eventlet.monkey_patch()

# Initialize Razorpay if available
RAZORPAY_ENABLED = bool(os.getenv("RAZORPAY_KEY_ID") and os.getenv("RAZORPAY_KEY_SECRET"))

if RAZORPAY_ENABLED:
    razor_client = razorpay.Client(auth=(
        os.getenv("RAZORPAY_KEY_ID"),
        os.getenv("RAZORPAY_KEY_SECRET")
    ))
else:
    razor_client = None
    print("⚠️ Razorpay not configured, payments will be cash only")

# ================= APP =================
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key-12345")
Compress(app)

# ================= SIMPLE RATE LIMITER =================
class SimpleRateLimiter:
    def __init__(self):
        self.requests = defaultdict(list)
    
    def is_allowed(self, key, max_requests=100, window=60):
        """Simple in-memory rate limiter"""
        now = time.time()
        
        # Clean old requests
        self.requests[key] = [req_time for req_time in self.requests[key] 
                             if now - req_time < window]
        
        if len(self.requests[key]) >= max_requests:
            return False
        
        self.requests[key].append(now)
        return True

rate_limiter = SimpleRateLimiter()

def rate_limit(max_requests=100, window=60):
    """Decorator for rate limiting"""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            client_ip = request.remote_addr or "unknown"
            endpoint = f.__name__
            key = f"{client_ip}:{endpoint}"
            
            if not rate_limiter.is_allowed(key, max_requests, window):
                return jsonify({"error": "Rate limit exceeded. Try again later."}), 429
            
            return f(*args, **kwargs)
        return wrapper
    return decorator

# ================= SOCKETIO =================
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='eventlet',
    logger=False,  # Production में False रखें
    engineio_logger=False,
    ping_timeout=30,
    ping_interval=25,
    max_http_buffer_size=1e6,
    async_handlers=True,
    manage_session=False,
    http_compression=True,
    compression_threshold=1024,
    cookie=None
)

# SocketIO connection tracking
socket_connections = defaultdict(set)
MAX_CONNECTIONS_PER_IP = 10

# ================= DATABASE POOL =================
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    # Render.com provides DATABASE_URL automatically
    raise Exception("DATABASE_URL environment variable is missing!")

# Create connection pool with minimal configuration for Render.com
pool = None

def init_database_pool():
    """Initialize database pool with proper cleanup"""
    global pool
    
    try:
        # Close existing pool if any
        if pool and not pool.closed:
            try:
                pool.close()
            except:
                pass
        
        # Create new pool - VERY SMALL for Render.com free tier
        pool = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=1,      # Minimum connections
            max_size=3,      # MAXIMUM 3 connections only for free tier
            timeout=10,      # Short timeout
            open=False,      # Don't open automatically
            max_idle=30,     # 30 seconds idle timeout
            num_workers=0    # No background workers
        )
        
        # Open pool manually
        pool.open()
        print(f"✅ Database pool initialized: min=1, max=3")
        
        # Simple connection test
        try:
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    print("✅ Database connection test successful")
        except:
            print("⚠️ Database test failed, but continuing...")
        
        return True
        
    except Exception as e:
        print(f"❌ Database pool initialization error: {e}")
        # Try to create a simple connection as fallback
        try:
            import psycopg
            conn = psycopg.connect(DATABASE_URL)
            conn.close()
            print("✅ Fallback database connection successful")
        except:
            print("❌ Fallback also failed")
        return False

# Initialize pool
if not init_database_pool():
    print("⚠️ Database pool initialization failed, retrying...")
    time.sleep(2)
    init_database_pool()

# ================= CONNECTION CONTEXT MANAGER =================
@contextmanager
def get_db_connection():
    """LEAK-PROOF database connection context manager"""
    conn = None
    cur = None
    try:
        conn = pool.getconn()
        cur = conn.cursor(row_factory=dict_row)
        yield cur
        conn.commit()
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except:
                pass
        raise e
    finally:
        # GUARANTEED CLEANUP - यही main fix है
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

# ================= CLEANUP HANDLERS =================
def cleanup_resources():
    """Cleanup all resources properly"""
    print("\n🔄 Cleaning up resources before shutdown...")
    
    # Clear socket connections
    socket_connections.clear()
    
    # Close database pool properly
    if pool and not pool.closed:
        try:
            print("Closing database pool...")
            pool.close()
            time.sleep(1)  # Give time for threads to stop
            print("✅ Database pool closed")
        except Exception as e:
            print(f"⚠️ Pool close error: {e}")
    
    print("✅ Cleanup completed")

# Register cleanup
atexit.register(cleanup_resources)

# ================= SOCKETIO EVENT HANDLERS =================
@socketio.on("connect")
def handle_connect():
    """Handle new connections"""
    client_ip = request.remote_addr or "unknown"
    client_id = request.sid
    
    # Simple connection limit
    if len(socket_connections[client_ip]) >= MAX_CONNECTIONS_PER_IP:
        print(f"⚠️ Connection limit exceeded for {client_ip}")
        return False
    
    socket_connections[client_ip].add(client_id)
    print(f"✅ Client connected: {client_id} from {client_ip}")
    return True

@socketio.on("disconnect")
def handle_disconnect():
    """Handle disconnections"""
    client_ip = request.remote_addr or "unknown"
    client_id = request.sid
    
    if client_id in socket_connections[client_ip]:
        socket_connections[client_ip].remove(client_id)
        if not socket_connections[client_ip]:
            del socket_connections[client_ip]
    
    print(f"❌ Client disconnected: {client_id}")

@socketio.on("join_room")
def handle_join_room(data):
    """Join specific room"""
    room_name = data.get('room')
    if room_name:
        join_room(room_name)

@socketio.on("leave_room")
def handle_leave_room(data):
    """Leave room"""
    room_name = data.get('room')
    if room_name:
        leave_room(room_name)

@socketio.on("driver_gps")
def handle_gps(data):
    """Handle GPS updates"""
    sid = data.get('sid')
    lat = float(data.get('lat', 27.5))
    lng = float(data.get('lng', 75.0))
    speed = float(data.get('speed', 0))
    
    # Update database
    try:
        with get_db_connection() as cur:
            cur.execute("""
                UPDATE schedules 
                SET current_lat=%s, current_lng=%s
                WHERE id=%s
            """, (lat, lng, sid))
    except Exception as e:
        print(f"Database update error: {e}")
    
    # Send to bus room only
    room_name = f"bus_{sid}"
    emit("bus_location", {
        "sid": sid,
        "lat": lat,
        "lng": lng,
        "speed": speed,
        "timestamp": datetime.now().isoformat()
    }, room=room_name, namespace='/')

# ================= CACHING =================
@lru_cache(maxsize=50)
def get_cached_route_stations(route_id):
    """Cache route stations"""
    try:
        with get_db_connection() as cur:
            cur.execute("""
                SELECT station_name, station_order
                FROM route_stations
                WHERE route_id = %s
                ORDER BY station_order
            """, (route_id,))
            return cur.fetchall()
    except:
        return []

@lru_cache(maxsize=100)
def get_cached_booked_seats(schedule_id, travel_date):
    """Cache booked seats"""
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

# ================= ADMIN REQUIRED =================
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
    
    try:
        with get_db_connection() as cur:
            # Create essential tables only
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
            
            # Add default admin if not exists
            cur.execute("SELECT COUNT(*) FROM admins")
            if cur.fetchone()[0] == 0:
                cur.execute("""
                    INSERT INTO admins (username, password, role, counter_no)
                    VALUES ('admin', 'admin123', 'admin', 1)
                    ON CONFLICT DO NOTHING;
                """)
                print("✅ Default admin created")
            
            # Add sample data if no routes exist
            cur.execute("SELECT COUNT(*) FROM routes")
            if cur.fetchone()[0] == 0:
                # Sample routes
                routes = [
                    (1, 'बीकानेर → जयपुर', 336),
                    (2, 'बीकानेर → जोधपुर', 252),
                    (3, 'जयपुर → जोधपुर', 330)
                ]
                
                for r in routes:
                    cur.execute(
                        "INSERT INTO routes VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                        r
                    )
                
                # Sample schedules
                schedules = [
                    (1, 1, 'Volvo AC Sleeper', '08:00'),
                    (2, 1, 'Semi Sleeper AC', '10:30'),
                    (3, 2, 'Volvo AC Seater', '09:00'),
                    (4, 3, 'Deluxe AC', '07:30')
                ]
                
                for s in schedules:
                    cur.execute("""
                        INSERT INTO schedules
                        (id, route_id, bus_name, departure_time, total_seats)
                        VALUES (%s,%s,%s,%s::time,40)
                        ON CONFLICT DO NOTHING
                    """, s)
                
                # Sample stations
                stations = [
                    (1, 'बीकानेर', 1),
                    (1, 'जयपुर', 2),
                    (2, 'बीकानेर', 1),
                    (2, 'जोधपुर', 2),
                    (3, 'जयपुर', 1),
                    (3, 'जोधपुर', 2)
                ]
                
                for st in stations:
                    cur.execute("""
                        INSERT INTO route_stations
                        (route_id,station_name,station_order)
                        VALUES (%s,%s,%s)
                        ON CONFLICT DO NOTHING
                    """, st)
                
                print("✅ Sample data added")
        
        print("✅ Database initialization complete!")
        
    except Exception as e:
        print(f"⚠️ Database init warning: {e}")

# Initialize database
init_db()

# ================= HTML TEMPLATES =================
BASE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>My Bus AI</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
body{font-family:Arial,sans-serif;background:#f8f9fa;}
.navbar{background:white;box-shadow:0 2px 10px rgba(0,0,0,0.1);}
.logo{color:#ff6b35;font-weight:bold;font-size:1.5rem;}
.hero{
    background:linear-gradient(rgba(0,0,0,0.7),rgba(0,0,0,0.7)),
    url('https://images.unsplash.com/photo-1544620347-c4fd4a3d5957');
    background-size:cover;background-position:center;
    color:white;padding:100px 20px;text-align:center;
}
.search-box{background:white;padding:20px;border-radius:10px;margin-top:30px;}
.btn-bus{background:#ff6b35;color:white;border:none;}
.btn-bus:hover{background:#ff5722;}
</style>
</head>
<body>

<nav class="navbar navbar-expand-lg">
  <div class="container">
    <a class="navbar-brand logo" href="/">🚌 My Bus AI</a>
    <div>
      <a href="/login" class="btn btn-outline-primary btn-sm me-2">Admin</a>
      <a href="/counter" class="btn btn-outline-success btn-sm">Counter</a>
    </div>
  </div>
</nav>

{% if not content %}
<section class="hero">
  <div class="container">
    <h1 class="mb-3">India's Smart Bus Platform</h1>
    <p class="mb-4">Book | Track | Live Updates</p>
    
    <div class="search-box">
      <form action="/search" method="POST" class="row g-3">
        <div class="col-md-4">
          <input type="text" name="from" class="form-control" placeholder="From" required>
        </div>
        <div class="col-md-4">
          <input type="text" name="to" class="form-control" placeholder="To" required>
        </div>
        <div class="col-md-3">
          <input type="date" name="date" class="form-control" required>
        </div>
        <div class="col-md-1">
          <button type="submit" class="btn btn-bus w-100">Search</button>
        </div>
      </form>
    </div>
  </div>
</section>
{% endif %}

{% if content %}
<div class="container py-5">
  {{ content|safe }}
</div>
{% endif %}

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>"""

LOGIN_HTML = """<div class="row justify-content-center mt-5">
  <div class="col-md-4">
    <div class="card shadow">
      <div class="card-body p-4">
        <h3 class="text-center mb-4">Login</h3>
        <form method="POST">
          <div class="mb-3">
            <input type="text" name="username" class="form-control" placeholder="Username" required>
          </div>
          <div class="mb-3">
            <input type="password" name="password" class="form-control" placeholder="Password" required>
          </div>
          <button class="btn btn-primary w-100">Login</button>
        </form>
        {% if error %}<div class="alert alert-danger mt-3 text-center">{{ error }}</div>{% endif %}
      </div>
    </div>
  </div>
</div>"""

# ================= MAIN ROUTES =================
@app.route("/")
@rate_limit(max_requests=50, window=60)
def home():
    """Home page"""
    session.setdefault("role", "guest")
    return render_template_string(BASE_HTML, content=None)

@app.route("/dashboard")
@rate_limit(max_requests=30, window=60)
def dashboard():
    """Dashboard page"""
    if not session.get("user_logged_in"):
        return redirect("/login")
    
    role = session.get("role", "user")
    
    admin_links = ""
    if role == "admin":
        admin_links = """
        <div class="mt-4">
          <a href="/create-counter" class="btn btn-success me-2">Create Counter</a>
          <a href="/health" class="btn btn-info">System Health</a>
        </div>
        """
    
    return render_template_string(BASE_HTML, content=f"""
    <div class="text-center">
      <h2>Welcome, {role.title()}!</h2>
      <div class="mt-4">
        <a href="/" class="btn btn-primary">🏠 Home</a>
        <a href="/logout" class="btn btn-danger ms-2">Logout</a>
      </div>
      {admin_links}
    </div>
    """)

@app.route("/login", methods=["GET", "POST"])
@rate_limit(max_requests=20, window=60)
def login():
    """Login page"""
    error = ""
    
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        
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
                    error = "Invalid credentials"
        except Exception as e:
            print(f"Login error: {e}")
            error = "Server error"
    
    return render_template_string(BASE_HTML, content=render_template_string(LOGIN_HTML, error=error))

@app.route("/counter", methods=["GET", "POST"])
@rate_limit(max_requests=20, window=60)
def counter():
    """Counter login"""
    error = ""
    
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        
        try:
            with get_db_connection() as cur:
                cur.execute("""
                    SELECT id, role FROM admins
                    WHERE username=%s AND password=%s AND role IN ('counter', 'admin')
                """, (username, password))
                
                user = cur.fetchone()
                
                if user:
                    session.clear()
                    session["user_logged_in"] = True
                    session["user_id"] = user["id"]
                    session["role"] = user["role"]
                    return redirect("/dashboard")
                else:
                    error = "Invalid credentials or not authorized"
        except Exception as e:
            print(f"Counter login error: {e}")
            error = "Server error"
    
    return render_template_string(BASE_HTML, content=render_template_string(LOGIN_HTML, error=error))

@app.route("/buses/<int:rid>")
@rate_limit(max_requests=30, window=60)
def buses(rid):
    """Show buses for a route"""
    try:
        with get_db_connection() as cur:
            # Get route info
            cur.execute("""
                SELECT r.route_name, r.distance_km
                FROM routes r WHERE r.id = %s
            """, (rid,))
            route = cur.fetchone()
            
            if not route:
                return "Route not found", 404
            
            # Get buses
            cur.execute("""
                SELECT s.id, s.bus_name, s.departure_time, s.total_seats,
                       s.current_lat, s.current_lng,
                       COUNT(b.id) as booked_count
                FROM schedules s
                LEFT JOIN seat_bookings b ON s.id = b.schedule_id 
                    AND b.travel_date = CURRENT_DATE 
                    AND b.status = 'confirmed'
                WHERE s.route_id = %s
                GROUP BY s.id
                ORDER BY s.departure_time
            """, (rid,))
            buses_data = cur.fetchall()
            
    except Exception as e:
        return f"Error: {e}", 500
    
    buses_html = ""
    for bus in buses_data:
        available_seats = bus['total_seats'] - bus['booked_count']
        buses_html += f"""
        <div class="card mb-3">
          <div class="card-body">
            <div class="d-flex justify-content-between align-items-center">
              <h5 class="mb-0">{bus['bus_name']}</h5>
              <span class="badge {'bg-success' if bus['current_lat'] else 'bg-secondary'}">
                {'🟢 Live' if bus['current_lat'] else 'Offline'}
              </span>
            </div>
            <p class="mt-2 mb-1">Departure: {bus['departure_time'].strftime('%H:%M')}</p>
            <p class="mb-3">Seats Available: {available_seats} / {bus['total_seats']}</p>
            <div class="d-flex gap-2">
              <a href="/live-bus/{bus['id']}" class="btn btn-primary btn-sm">Live GPS</a>
              <a href="/seats/{bus['id']}" class="btn btn-success btn-sm">Book Seat</a>
            </div>
          </div>
        </div>
        """
    
    content = f"""
    <div class="mb-4">
      <h2>{route['route_name']}</h2>
      <p class="text-muted">Distance: {route['distance_km']} km</p>
    </div>
    
    {buses_html if buses_html else '<div class="alert alert-warning">No buses available</div>'}
    
    <div class="mt-4">
      <a href="/" class="btn btn-secondary">← Back to Home</a>
    </div>
    """
    
    return render_template_string(BASE_HTML, content=content)

@app.route("/seats/<int:sid>")
@rate_limit(max_requests=30, window=60)
def seat_page(sid):
    """Seat selection page"""
    try:
        with get_db_connection() as cur:
            # Get bus info
            cur.execute("""
                SELECT s.id, s.bus_name, s.departure_time, r.route_name,
                       s.current_lat, s.current_lng
                FROM schedules s
                JOIN routes r ON s.route_id = r.id
                WHERE s.id = %s
            """, (sid,))
            bus = cur.fetchone()
            
            if not bus:
                return "Bus not found", 404
            
            # Get booked seats
            today = session.get("date", date.today().isoformat())
            booked_seats = set(get_cached_booked_seats(sid, today))
            
    except Exception as e:
        return f"Error: {e}", 500
    
    # Generate seat layout
    seat_html = '<div class="d-flex flex-wrap gap-2 mb-4">'
    for seat in range(1, 41):
        if seat in booked_seats:
            seat_html += f'<button class="btn btn-danger" disabled>X{seat}</button>'
        else:
            seat_html += f'<button class="btn btn-success" onclick="bookSeat({seat})">{seat}</button>'
    seat_html += '</div>'
    
    bus_lat = bus['current_lat'] or 27.5
    bus_lng = bus['current_lng'] or 75.0
    
    content = f"""
    <div class="mb-4">
      <h2>{bus['bus_name']}</h2>
      <p class="text-muted">Route: {bus['route_name']} | Departure: {bus['departure_time'].strftime('%H:%M')}</p>
    </div>
    
    <div id="map" style="height: 200px; width: 100%; border-radius: 10px; margin-bottom: 20px;"></div>
    
    <h5 class="mb-3">Select Seat:</h5>
    {seat_html}
    
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <script>
    const socket = io();
    const busId = {sid};
    const today = "{today}";
    
    // Join bus room
    socket.emit("join_room", {{ room: "bus_" + busId }});
    
    // Handle seat updates
    socket.on("seat_update", function(data) {{
        if(data.sid == busId && data.date == today) {{
            const btn = document.querySelector(`button:contains('${{data.seat}}')`);
            if(btn) {{
                btn.className = "btn btn-danger";
                btn.disabled = true;
                btn.innerHTML = "X" + data.seat;
            }}
        }}
    }});
    
    function bookSeat(seatNumber) {{
        const name = prompt("Passenger Name:");
        if(!name) return;
        
        const mobile = prompt("Mobile Number:");
        if(!mobile) return;
        
        fetch("/book", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{
                schedule_id: busId,
                seat_number: seatNumber,
                passenger_name: name,
                mobile: mobile,
                date: today
            }})
        }})
        .then(r => r.json())
        .then(data => {{
            if(data.ok) {{
                alert("Seat booked successfully!");
                location.reload();
            }} else {{
                alert(data.error || "Booking failed");
            }}
        }})
        .catch(err => alert("Error: " + err));
    }}
    </script>
    
    <div class="mt-4">
      <a href="/buses/{session.get('route_id', 1)}" class="btn btn-secondary">← Back to Buses</a>
    </div>
    """
    
    return render_template_string(BASE_HTML, content=content)

@app.route("/book", methods=["POST"])
@rate_limit(max_requests=20, window=60)
def book():
    """Book a seat"""
    data = request.get_json()
    
    try:
        with get_db_connection() as cur:
            # Check if seat is available
            cur.execute("""
                SELECT id FROM seat_bookings
                WHERE schedule_id = %s 
                AND seat_number = %s 
                AND travel_date = %s
                AND status = 'confirmed'
            """, (data['schedule_id'], data['seat_number'], data['date']))
            
            if cur.fetchone():
                return jsonify({"ok": False, "error": "Seat already booked"}), 409
            
            # Book the seat
            cur.execute("""
                INSERT INTO seat_bookings
                (schedule_id, seat_number, passenger_name, mobile,
                 from_station, to_station, travel_date, fare, status,
                 booked_by_type, booked_by_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                data['schedule_id'],
                data['seat_number'],
                data['passenger_name'],
                data['mobile'],
                session.get("from", ""),
                session.get("to", ""),
                data['date'],
                random.randint(250, 450),  # Random fare
                'confirmed',
                session.get("role", "user"),
                session.get("user_id", 0)
            ))
            
            # Clear cache for this schedule
            get_cached_booked_seats.cache_clear()
            
            # Notify others
            socketio.emit("seat_update", {
                "sid": data['schedule_id'],
                "seat": data['seat_number'],
                "date": data['date']
            }, room=f"bus_{data['schedule_id']}")
            
            return jsonify({"ok": True, "message": "Seat booked successfully"})
            
    except Exception as e:
        print(f"Booking error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/live-bus/<int:bus_id>")
@rate_limit(max_requests=30, window=60)
def live_bus(bus_id):
    """Live bus tracking"""
    try:
        with get_db_connection() as cur:
            cur.execute("""
                SELECT s.bus_name, s.current_lat, s.current_lng, r.route_name
                FROM schedules s
                JOIN routes r ON s.route_id = r.id
                WHERE s.id = %s
            """, (bus_id,))
            bus = cur.fetchone()
    except:
        bus = None
    
    if not bus:
        bus = {'bus_name': f'Bus {bus_id}', 'route_name': 'Unknown', 'current_lat': 27.5, 'current_lng': 75.0}
    
    lat = bus['current_lat'] or 27.5
    lng = bus['current_lng'] or 75.0
    
    content = f"""
    <div class="text-center mb-4">
      <h2>🚌 {bus['bus_name']}</h2>
      <p class="text-muted">{bus['route_name']}</p>
      <div class="badge bg-danger p-2">LIVE TRACKING</div>
    </div>
    
    <div id="map" style="height: 400px; width: 100%; border-radius: 10px;"></div>
    
    <div class="row mt-4">
      <div class="col-md-6">
        <div class="card">
          <div class="card-body">
            <h5>📍 Current Location</h5>
            <p>Latitude: <span id="lat">{lat:.6f}</span></p>
            <p>Longitude: <span id="lng">{lng:.6f}</span></p>
            <p>Speed: <span id="speed">0 km/h</span></p>
          </div>
        </div>
      </div>
      <div class="col-md-6">
        <div class="card">
          <div class="card-body">
            <h5>🚗 Actions</h5>
            <div class="d-grid gap-2">
              <a href="/driver/{bus_id}" class="btn btn-success">Driver Mode</a>
              <a href="/seats/{bus_id}" class="btn btn-primary">Book Seat</a>
              <a href="/" class="btn btn-secondary">Home</a>
            </div>
          </div>
        </div>
      </div>
    </div>
    
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <script>
    const socket = io();
    const busId = {bus_id};
    
    socket.emit("join_room", {{ room: "bus_" + busId }});
    
    socket.on("bus_location", function(data) {{
        if(data.sid == busId) {{
            document.getElementById("lat").textContent = data.lat.toFixed(6);
            document.getElementById("lng").textContent = data.lng.toFixed(6);
            document.getElementById("speed").textContent = data.speed.toFixed(1) + " km/h";
        }}
    }});
    </script>
    """
    
    return render_template_string(BASE_HTML, content=content)

@app.route("/search", methods=["POST"])
@rate_limit(max_requests=30, window=60)
def search():
    """Search buses"""
    from_station = request.form.get("from", "").strip()
    to_station = request.form.get("to", "").strip()
    travel_date = request.form.get("date", date.today().isoformat())
    
    # Store in session
    session["from"] = from_station
    session["to"] = to_station
    session["date"] = travel_date
    
    if not from_station or not to_station:
        return "Please enter both stations", 400
    
    try:
        with get_db_connection() as cur:
            # Find route containing both stations
            cur.execute("""
                SELECT r.id, r.route_name
                FROM routes r
                WHERE EXISTS (
                    SELECT 1 FROM route_stations rs1 
                    WHERE rs1.route_id = r.id AND rs1.station_name ILIKE %s
                )
                AND EXISTS (
                    SELECT 1 FROM route_stations rs2 
                    WHERE rs2.route_id = r.id AND rs2.station_name ILIKE %s
                )
                LIMIT 1
            """, (f"%{from_station}%", f"%{to_station}%"))
            
            route = cur.fetchone()
            
            if route:
                session["route_id"] = route['id']
                return redirect(f"/buses/{route['id']}")
            else:
                return render_template_string(BASE_HTML, content="""
                <div class="alert alert-warning text-center">
                    <h4>🚫 No Buses Found</h4>
                    <p>No routes found between these stations.</p>
                    <a href="/" class="btn btn-primary mt-2">Try Again</a>
                </div>
                """)
                
    except Exception as e:
        return f"Search error: {e}", 500

@app.route("/create-counter", methods=["GET", "POST"])
@admin_required
def create_counter():
    """Create counter user"""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        
        if username and password:
            try:
                with get_db_connection() as cur:
                    cur.execute("""
                        INSERT INTO admins (username, password, role)
                        VALUES (%s, %s, 'counter')
                        ON CONFLICT (username) DO NOTHING
                    """, (username, password))
                    
                    return render_template_string(BASE_HTML, content="""
                    <div class="alert alert-success text-center">
                        <h4>✅ Counter Created</h4>
                        <p>Counter user '{username}' created successfully.</p>
                        <a href="/dashboard" class="btn btn-primary mt-2">Back to Dashboard</a>
                    </div>
                    """.format(username=username))
            except Exception as e:
                return f"Error: {e}", 500
    
    content = """
    <div class="row justify-content-center">
      <div class="col-md-6">
        <div class="card">
          <div class="card-body">
            <h4 class="text-center mb-4">Create Counter User</h4>
            <form method="POST">
              <div class="mb-3">
                <label class="form-label">Username</label>
                <input type="text" name="username" class="form-control" required>
              </div>
              <div class="mb-3">
                <label class="form-label">Password</label>
                <input type="password" name="password" class="form-control" required>
              </div>
              <button type="submit" class="btn btn-success w-100">Create Counter</button>
            </form>
            <div class="mt-3">
              <a href="/dashboard" class="btn btn-secondary w-100">Cancel</a>
            </div>
          </div>
        </div>
      </div>
    </div>
    """
    
    return render_template_string(BASE_HTML, content=content)

@app.route("/health")
def health():
    """Health check endpoint"""
    try:
        # Test database
        with get_db_connection() as cur:
            cur.execute("SELECT 1")
            db_ok = cur.fetchone()[0] == 1
        
        status = "✅ Healthy" if db_ok else "❌ Unhealthy"
        
        content = f"""
        <div class="card">
          <div class="card-body">
            <h4 class="text-center mb-4">System Health</h4>
            <div class="alert {'alert-success' if db_ok else 'alert-danger'}">
              <h5>Status: {status}</h5>
            </div>
            <div class="mt-3">
              <p><strong>Database:</strong> {'✅ Connected' if db_ok else '❌ Disconnected'}</p>
              <p><strong>SocketIO Connections:</strong> {sum(len(v) for v in socket_connections.values())}</p>
              <p><strong>Time:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
            </div>
            <div class="mt-4 text-center">
              <a href="/" class="btn btn-primary">Home</a>
              <a href="/dashboard" class="btn btn-secondary ms-2">Dashboard</a>
            </div>
          </div>
        </div>
        """
        
        return render_template_string(BASE_HTML, content=content)
        
    except Exception as e:
        return render_template_string(BASE_HTML, content=f"""
        <div class="alert alert-danger">
          <h4>❌ System Error</h4>
          <p>{str(e)}</p>
        </div>
        """)

@app.route("/logout")
def logout():
    """Logout"""
    session.clear()
    return redirect("/")

# ================= MAIN =================
if __name__ == "__main__":
    print("=" * 50)
    print("🚀 My Bus AI Application Starting...")
    print(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("✅ Database pool initialized")
    print("✅ SocketIO ready")
    print("✅ Rate limiting active")
    print("=" * 50)
    
    # Get port from environment (Render.com provides PORT)
    port = int(os.environ.get("PORT", 10000))
    
    # For Render.com - use production settings
    if os.environ.get("RENDER"):
        print("🌐 Production mode: Render.com detected")
        socketio.run(
            app,
            host="0.0.0.0",
            port=port,
            debug=False,
            allow_unsafe_werkzeug=False,
            log_output=False
        )
    else:
        # Development mode
        print("🔧 Development mode")
        socketio.run(
            app,
            host="0.0.0.0",
            port=port,
            debug=True,
            allow_unsafe_werkzeug=True
        )