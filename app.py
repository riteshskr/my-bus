from dotenv import load_dotenv
import json
import time
from datetime import datetime, date
load_dotenv()
import setuptools
import os, random
from datetime import date
from functools import wraps
from flask import Flask, request, jsonify, render_template_string, redirect, g, session
from flask_socketio import SocketIO, emit
from flask_compress import Compress
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
import atexit
import razorpay

# import psycopg2
razor_client = razorpay.Client(auth=(
    os.getenv("RAZORPAY_KEY_ID"),
    os.getenv("RAZORPAY_KEY_SECRET")
))
# ===== PAYMENT CONFIG =====
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
# ================= APP =================
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key")
Compress(app)

# ✅ PERFECT SocketIO Configuration
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                    logger=True, engineio_logger=True, ping_timeout=60)

# ================= DATABASE =================
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise Exception("DATABASE_URL environment variable is missing!")

# IMPROVED POOL CONFIGURATION
pool = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=1,           # Minimum connections
    max_size=5,           # ✅ REDUCE from 10 to 5 (Render.com free tier के लिए)
    timeout=30,           # ✅ INCREASE from 20 to 30
    open=False            # Don't open immediately
)
pool.open()  # Manually open pool
print("✅ Connection pool ready")


@app.teardown_appcontext
def close_db(error=None):
    cur = g.pop('db_cur', None)
    conn = g.pop('db_conn', None)

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


# ================= DB CONTEXT =================
def get_db():
    try:
        if 'db_conn' not in g or g.db_conn.closed:
            g.db_conn = pool.getconn()
        if 'db_cur' not in g:
            g.db_cur = g.db_conn.cursor(row_factory=dict_row)
        return g.db_conn, g.db_cur
    except Exception as e:
        # FIXED: Use correct method
        try:
            pool.close()  # ✅ close() use करें, closeall() नहीं
        except:
            pass

        # New connection
        g.db_conn = pool.getconn()
        g.db_cur = g.db_conn.cursor(row_factory=dict_row)
        return g.db_conn, g.db_cur


@app.teardown_appcontext
def close_db(error=None):
    cur = g.pop('db_cur', None)
    conn = g.pop('db_conn', None)

    if cur:
        cur.close()
    if conn:
        pool.putconn(conn)


def safe_db(func):
    @wraps(func)
    def wrapper(*a, **kw):
        try:
            return func(*a, **kw)
        except Exception as e:
            print(f"Database error in {func.__name__}: {e}")
            raise e
        finally:
            # Ensure connection is returned to pool
            close_db()
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrap(*a, **k):
        if not session.get("user_logged_in"):
            return redirect("/login")

        if session.get("role") != "admin":
            return "Access Denied", 403

        return f(*a, **k)

    return wrap


# ================= DB INIT =================
def init_db():
    try:
        conn = pool.getconn()
        cur = conn.cursor()

        # ===== TABLES =====
        # cur.execute("DROP TABLE IF EXISTS admin CASCADE;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS faces (
                id SERIAL PRIMARY KEY,
                bus_id INT NOT NULL,
                face_data BYTEA NOT NULL,
                face_image BYTEA NOT NULL
            );
            """)

        # face_logs table
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
        )
        """)
        cur.execute("SELECT COUNT(*) FROM admins ")
        count = cur.fetchone()[0]

        if count == 0:
            cur.execute("""
            INSERT INTO admins (username, password)
            VALUES ('admin', '1234')
            ON CONFLICT DO NOTHING
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
        )""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS routes (
            id SERIAL PRIMARY KEY, 
            route_name VARCHAR(100) UNIQUE, 
            distance_km INT
        )""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id SERIAL PRIMARY KEY, 
            route_id INT REFERENCES routes(id), 
            bus_name VARCHAR(100),
            departure_time TIME, 
            current_lat DOUBLE PRECISION,
            current_lng DOUBLE PRECISION,
            total_seats INT DEFAULT 40
        )""")

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
        )""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS route_stations (
            id SERIAL PRIMARY KEY, 
            route_id INT REFERENCES routes(id), 
            station_name VARCHAR(50), 
            station_order INT,
            lat DOUBLE PRECISION DEFAULT 27.2,
            lng DOUBLE PRECISION DEFAULT 75.2
        )""")

        conn.commit()

        # ===== DEFAULT DATA =====
        cur.execute("SELECT COUNT(*) FROM admins")
        count = cur.fetchone()[0]

        if count == 0:
            cur.execute(""" INSERT INTO  admins(username, password, role, counter_no)
            VALUES('admin', 'admin123', 'admin', 1);""")

        cur.execute("SELECT COUNT(*) FROM routes")
        count = cur.fetchone()[0]

        if count == 0:
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

            conn.commit()

            cur.close()
            pool.putconn(conn)  # ✅ सबसे important line

            print("✅ DB Init Complete!")


    except Exception as e:

        import traceback

        print("❌ DB INIT REAL ERROR ↓")

        traceback.print_exc()

        try:
            conn.rollback()
            pool.putconn(conn, close=True)
        except:
            pass


print("✅ Connection pool ready")
init_db()


# ================= SOCKET EVENTS =================
@socketio.on("connect")
def handle_connect():
    print(f"✅ Client connected: {request.sid}")


@socketio.on("driver_gps")
def gps(data):
    sid = data.get('sid')
    lat = float(data.get('lat', 27.5))
    lng = float(data.get('lng', 75.0))
    speed = float(data.get('speed', 0))

    print(f"📍 LIVE: Bus-{sid} @ [{lat:.5f},{lng:.5f}] {speed}km/h")

    try:
        with app.app_context():
            conn, cur = get_db()
            cur.execute("""
                UPDATE schedules 
                SET current_lat=%s, current_lng=%s
                WHERE id=%s
            """, (lat, lng, sid))
            conn.commit()
    except:
        pass

    # 🔥 यही main fix है
    socketio.emit("bus_location", {
        "sid": sid,
        "lat": lat,
        "lng": lng,
        "speed": speed,
        "timestamp": data.get('timestamp', '')
    })


# ================= HTML BASE =================

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
}
.search-box input{
  padding:12px;
  border:none;
  border-radius:8px;
  outline:none;
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

/* ---------- Mobile Fixes ---------- */
@media(max-width:768px){

  .navbar{
    flex-direction:column;
    gap:10px;
    padding:10px 20px;
  }

  .search-box{
    flex-direction:column;
    width:100%;
  }

  .search-box input,
  .search-box button{
    width:100%;
  }

  .hero h1{font-size:1.6rem;}
}
</style>
</head>
<body>

<div class="navbar">
  <div class="logo">🚌 My Bus AI</div>
  <div>
    <a href="/login">Admin login</a>
    <a href="/counter">Counter</a>
  </div>
</div>

{% if not content %}
<section class="hero">
  <div>
    <h1>India’s Smart Bus Platform</h1>
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

HOME_HTML = """
<div class="row g-3 mb-4">
  <div class="col-md-4">
    <select class="form-select" id="from">
      <option selected disabled>From</option>
      <option>Delhi</option>
      <option>Mumbai</option>
      <option>Bengaluru</option>
      <option>Jaipur</option>
    </select>
  </div>

  <div class="col-md-4">
    <select class="form-select" id="to">
      <option selected disabled>To</option>
      <option>Jaipur</option>
      <option>Pune</option>
      <option>Chennai</option>
      <option>Hyderabad</option>
    </select>
  </div>

  <div class="col-md-3">
    <input type="date" class="form-control" id="date">
  </div>

  <div class="col-md-1 d-grid">
    <button class="btn btn-danger" onclick="searchBus()">Search</button>
  </div>
</div>

<script>
function searchBus(){
  let f = document.getElementById("from").value;
  let t = document.getElementById("to").value;
  let d = document.getElementById("date").value;

  if(!f || !t || !d){
    alert("Please fill all fields");
    return;
  }
  alert("Searching buses from " + f + " to " + t + " on " + d);
}
</script>
"""

# ===== login html =======
LOGIN_HTML = """
<div class="row justify-content-center mt-5">
  <div class="col-md-4">
    <div class="card shadow-lg border-0 rounded-4">
      <div class="card-body p-4">

        <h3 class="text-center mb-4">Admin Login</h3>

       <form method="POST" autocomplete="on">
       <!-- Hidden fields (Chrome autofill रोकने के लिए) -->
          <input type="text" style="display:none">
          <input type="password" style="display:none">

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
@safe_db
def home():
    if "role" not in session:
        session.clear()
        session["role"] = "guest"

    conn, cur = get_db()

    # Fetch all routes for route cards
    cur.execute("SELECT id, route_name, distance_km FROM routes ORDER BY id")
    routes = cur.fetchall()

    # Fetch all unique stations for search
    cur.execute("SELECT DISTINCT station_name FROM route_stations ORDER BY station_name")
    stations = [r["station_name"] for r in cur.fetchall()]

    return render_template_string(BASE_HTML, stations=stations, routes=routes, content=None)


@app.route("/dashboard")
def dashboard():
    if not session.get("user_logged_in"):
        return redirect("/login")

    role = session.get("role", "user")

    # Admin को extra links
    admin_links = ""
    if role.lower() == "admin":
        admin_links = """
        <div class="mt-3">
            <a href="/routes" class="btn btn-info me-2">🛣️ Manage Routes</a>
            <a href="/schedules" class="btn btn-warning me-2">🚌 Manage Schedules</a>
            <a href="/bookings" class="btn btn-success">🎫 View Bookings</a>
            <a href="/create-counter" class="btn btn-success">🎫 Create Counter</a>

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
# @safe_db
def buses(rid):
    conn, cur = get_db()

    # Route details + stations
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
        return "Route not found", 404

    # All buses of this route
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

    # Full HTML inline
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🚌 {{ route.route_name }} - Premium Booking</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
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
        <h1>🚌 {{ route.route_name }} - Premium Booking</h1>
        <p>📍 {{ route.stations }} | 🛣️ {{ route.distance_km }} km</p>
    </header>

    <div class="container mt-5">
        <div class="row justify-content-center">
            <div class="col-md-6">
                {% if buses %}
                    {% for bus in buses %}
                    <div class="bus-card">
                        <div class="d-flex justify-content-between align-items-center">
                            <h5>{{ bus.bus_name }} <i class="fas fa-bus"></i></h5>
                            <span class="badge {{ 'bg-success' if bus.current_lat else 'bg-secondary' }}">
                                {{ '🟢 LIVE' if bus.current_lat else '⚪ Offline' }}
                            </span>
                        </div>
                        <div class="bus-info mt-2">
                            <p><i class="fas fa-clock"></i> Departure: {{ bus.departure_time.strftime('%H:%M') }}</p>
                            <p><i class="fas fa-chair"></i> Seats Left: {{ bus.total_seats - bus.booked_count }} | Total Seats: {{ bus.total_seats }}</p>
                        </div>

                        <div class="d-flex flex-wrap gap-2 mt-2">
                            <a href="/live-bus/{{ bus.id }}" class="btn btn-primary flex-fill">🗺️ Live GPS</a>
                            <a href="/select/{{ bus.id }}" class="btn btn-success flex-fill">🎫 Book Seat</a>
                        </div>

                    {% endfor %}
                {% else %}
                    <div class="alert alert-warning text-center">आज कोई बस नहीं है</div>
                {% endif %}
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

    return render_template_string(html, route=route, buses=buses_data)


# **** create counter ******
@app.route("/create-counter", methods=["GET", "POST"])
@admin_required
def create_counter():
    error = ""
    success = ""

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        if not username or not password:
            error = "सभी fields भरें"
        else:
            try:
                conn, cur = get_db()
                cur.execute("""
                    INSERT INTO admins (username, password, role)
                    VALUES (%s, %s, 'counter')
                    ON CONFLICT (username) DO NOTHING
                """, (username, password))
                conn.commit()
                success = f"Counter '{username}' सफलतापूर्वक बनाया गया ✅"
            except Exception as e:
                conn.rollback()
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


# ******* login ********
@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        try:
            conn, cur = get_db()
            # ✅ IMPORTANT
            cur.execute("""
                SELECT id, role FROM admins
                WHERE username=%s AND password=%s
            """, (username, password))

            user = cur.fetchone()

            if user:
                session.clear()  # ✅ clean old session
                session["user_logged_in"] = True
                session["user_id"] = user["id"]
                session["role"] = user["role"]  # admin / office / conductor
                return redirect("/dashboard")
            else:
                error = "गलत यूज़रनेम या पासवर्ड"

        except Exception as e:
            import traceback
            traceback.print_exc()
            print("LOGIN ERROR:", e)
            error = "सर्वर में समस्या"

    return render_template_string(
        BASE_HTML,
        content=render_template_string(LOGIN_HTML, error=error)
    )


# ******* counter ********
@app.route("/counter", methods=["GET", "POST"])
def counter():
    error = ""

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        try:
            conn, cur = get_db()
            # ✅ IMPORTANT
            cur.execute("SELECT * FROM admins")
            user = cur.fetchone()
            print(user)
            cur.execute("""
                SELECT id, role FROM admins
                WHERE username=%s AND password=%s
            """, (username, password))

            user = cur.fetchone()

            if user:
                session.clear()  # ✅ clean old session
                session["user_logged_in"] = True
                session["user_id"] = user["id"]
                session["role"] = user["role"]  # admin / office / conductor
                return redirect("/dashboard")
            else:
                error = "गलत यूज़रनेम या पासवर्ड"

        except Exception as e:
            import traceback
            traceback.print_exc()
            print("LOGIN ERROR:", e)
            error = "सर्वर में समस्या"

    return render_template_string(
        BASE_HTML,
        content=render_template_string(LOGIN_HTML, error=error)
    )


@app.route("/select/<int:sid>")
@safe_db
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
    conn, cur = get_db()

    # ===== Schedule details =====
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

    # ===== Route stations =====
    cur.execute("""
        SELECT station_name, station_order
        FROM route_stations
        WHERE route_id=%s
        ORDER BY station_order
    """, (schedule['route_id'],))
    stations = cur.fetchall()

    # ===== Already booked seats =====
    today = session.get("date", date.today().isoformat())
    cur.execute("""
        SELECT seat_number
        FROM seat_bookings
        WHERE schedule_id=%s AND travel_date=%s AND status='confirmed'
    """, (sid, today))
    booked = cur.fetchall()
    booked_seats = set(r['seat_number'] for r in booked)

    # ===== Seat buttons =====
    seat_buttons = ""
    for i in range(1, 41):  # Total 40 seats
        if i in booked_seats:
            seat_buttons += f'''
            <button id="seat-{i}" class="btn btn-danger seat" disabled>X{i}</button>
            '''
        else:
            seat_buttons += f'''
            <button id="seat-{i}" class="btn btn-success seat" onclick="bookSeat({i})">{i}</button>
            '''

    # ===== Bus default location =====
    user_role = session.get("role", "guest")
    counter_id = session.get("user_id") if user_role in ("counter", "conductor") else None
    bus_lat = schedule['current_lat'] if schedule['current_lat'] else 27.5
    bus_lon = schedule['current_lng'] if schedule['current_lng'] else 75.0
    counter_js = session.get("user_id") if session.get("role") == "counter" else "null"

    # ===== Map div =====
    map_div = """
    <div id="map" style="
        width:100%;
        max-width:900px;
        height:300px;
        border-radius:12px;
        overflow:hidden;
        box-shadow:0 4px 10px rgba(0,0,0,0.2);
    "></div>
    """
    # ===== Role color =====
    role_color = {
        "admin": "red",
        "counter": "green",
        "conductor": "blue",
        "user": "orange"
    }.get(user_role, "gray")
    # ===== Full HTML =====
    html_content = f"""
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>

    <div class="container" style="max-width:900px;margin:auto;">
        <h2>बस: {schedule['bus_name']} | Route: {schedule['route_name']}</h2>
        <h4>Departure: {schedule['departure_time'].strftime('%H:%M')}</h4>
        <h5>
        Role:
        <span style="color:{role_color};font-weight:bold;">
            {user_role.upper()}
        </span>
    </h5>
        <h5>Live Location</h5>
        {map_div}

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
    const COUNTER_ID = {counter_js};

    // ===== Leaflet Map Init =====
    const map = L.map('map').setView([BUS_LAT, BUS_LNG], 15); // zoom 15 = city/highway level

    L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/">CARTO</a>',
        subdomains: 'abcd',
        maxZoom: 19
    }}).addTo(map);

    let busMarker = L.marker([BUS_LAT, BUS_LNG]).addTo(map);

    // ===== Bus location update =====
    socket.on("bus_location", data => {{
        if(data.sid == SID){{
            let lat = parseFloat(data.lat);
            let lng = parseFloat(data.lng);
            busMarker.setLatLng([lat, lng]);
            map.flyTo([lat,lng], map.getZoom());;
        }}
    }});

    // ===== Seat update realtime =====
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
     setInterval(()=>{{
    fetch("/heartbeat");
    }}, 30000);

    // ===== Seat Booking =====
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
            return;
            }}

         payment_mode = prompt("Payment mode: cash / online", "cash");
        if(payment_mode !== "cash" && payment_mode !== "online"){{
            alert("Only cash or online allowed");
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


    </script>
    """

    return render_template_string(BASE_HTML, content=html_content)


@app.route("/heartbeat")
def heartbeat():
    return "ok"


@app.route("/book", methods=["POST"])
@safe_db
def book():
    data = request.get_json()

    conn, cur = get_db()

    try:
        cur.execute("""
            SELECT id FROM seat_bookings
            WHERE schedule_id=%s 
            AND seat_number=%s 
            AND travel_date=%s
            AND status='confirmed'
        """, (data['schedule_id'], data['seat_number'], data['date']))

        if cur.fetchone():
            return jsonify({"ok": False, "error": "Seat already booked"}), 409

        user_role = session.get("role", "user")

        if user_role == "counter":
            fare = int(data.get("fare", 0))
            payment_mode = data.get("payment_mode", "cash")
        else:
            fare = random.randint(250, 450)
            payment_mode = "cash"

        cur.execute("""
        INSERT INTO seat_bookings
        (
         schedule_id, seat_number, passenger_name, mobile,
         from_station, to_station, travel_date,
         fare, status, payment_mode,
         booked_by_type, booked_by_id, counter_id
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,
        %s,%s,%s,%s,%s)
        """, (
            int(data['schedule_id']),
            int(data['seat_number']),
            data['passenger_name'],
            data['mobile'],
            session.get("from"),
            session.get("to"),
            data['date'],
            int(fare),
            'confirmed',  # status
            payment_mode,  # payment_mode
            user_role,  # booked_by_type
            int(session.get("user_id", 0)),
            int(data.get("counter_id") or 0)
        ))
        conn.commit()
        socketio.emit("seat_update", {
            "sid": data['schedule_id'],  # schedule_id को sid की जगह
            "seat": data['seat_number'],  # data['seat'] नहीं, data['seat_number']
            "date": data['date']
        })
        return jsonify({"ok": True, "fare": fare})

    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)})


@app.route("/driver/<int:bus_id>")
def driver(bus_id):
    """Driver GPS page with background GPS tracking - COMPLETELY FIXED"""

    try:
        # Get bus info from database
        conn, cur = get_db()
        cur.execute("""
            SELECT s.id, s.bus_name, s.current_lat, s.current_lng, r.route_name
            FROM schedules s
            LEFT JOIN routes r ON s.route_id = r.id
            WHERE s.id = %s
        """, (bus_id,))
        bus = cur.fetchone()

        if not bus:
            # Default values if bus not found
            bus = {
                'id': bus_id,
                'bus_name': f'Bus {bus_id}',
                'current_lat': 27.5,
                'current_lng': 75.0,
                'route_name': 'Unknown Route'
            }
        else:
            # Ensure lat/lng are not None
            bus['current_lat'] = bus['current_lat'] or 27.5
            bus['current_lng'] = bus['current_lng'] or 75.0

        bus_lat = float(bus['current_lat'])
        bus_lng = float(bus['current_lng'])

    except Exception as e:
        # Fallback values if database fails
        print(f"Driver page DB error: {e}")
        bus_lat = 27.5
        bus_lng = 75.0
        bus = {
            'id': bus_id,
            'bus_name': f'Bus {bus_id}',
            'route_name': 'Default Route'
        }

    # HTML CONTENT - USING SIMPLE STRING CONCATENATION TO AVOID TEMPLATE ERRORS
    html_content = '''<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🚗 ड्राइवर GPS - ''' + bus['bus_name'] + '''</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <style>
        body {
            background: linear-gradient(135deg, #1a2980, #26d0ce);
            color: white;
            padding: 20px;
            min-height: 100vh;
        }
        #map {
            height: 400px;
            width: 100%;
            border-radius: 15px;
            margin: 20px 0;
            border: 3px solid white;
            box-shadow: 0 10px 30px rgba(0,0,0,0.3);
        }
        .status-card {
            background: rgba(255,255,255,0.15);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            padding: 20px;
            margin-bottom: 20px;
            border: 1px solid rgba(255,255,255,0.2);
        }
        .gps-btn {
            font-size: 1.2rem;
            padding: 15px;
            border-radius: 10px;
            font-weight: bold;
            margin: 5px 0;
        }
        #logBox {
            background: rgba(0,0,0,0.3);
            border-radius: 10px;
            padding: 10px;
            height: 150px;
            overflow-y: auto;
            font-family: monospace;
            font-size: 12px;
            margin-top: 10px;
        }
        .bus-icon {
            background: #ff5722;
            color: white;
            padding: 12px;
            border-radius: 50%;
            font-size: 20px;
            border: 4px solid white;
            box-shadow: 0 0 15px rgba(0,0,0,0.5);
        }
    </style>
</head>
<body>

<div class="container">
    <h1 class="text-center mb-3">🚗 ड्राइवर GPS ट्रैकिंग</h1>
    <h3 class="text-center mb-4">''' + bus['bus_name'] + ''' - ''' + bus['route_name'] + '''</h3>

    <div class="status-card">
        <div class="row">
            <div class="col-md-6">
                <button id="startBtn" class="btn btn-success gps-btn w-100" onclick="startGPS()">
                    🚀 GPS शुरू करें
                </button>
            </div>
            <div class="col-md-6">
                <button id="stopBtn" class="btn btn-danger gps-btn w-100" onclick="stopGPS()" disabled>
                    🛑 GPS बंद करें
                </button>
            </div>
        </div>
    </div>

    <div id="map"></div>

    <div class="row">
        <div class="col-md-8">
            <div class="status-card">
                <h4>📍 लाइव लोकेशन</h4>
                <div class="row">
                    <div class="col-md-3">
                        <strong>अक्षांश:</strong><br>
                        <span id="lat" class="h5">''' + str(bus_lat) + '''</span>
                    </div>
                    <div class="col-md-3">
                        <strong>देशांतर:</strong><br>
                        <span id="lng" class="h5">''' + str(bus_lng) + '''</span>
                    </div>
                    <div class="col-md-3">
                        <strong>गति:</strong><br>
                        <span id="speed" class="h5">0 km/h</span>
                    </div>
                    <div class="col-md-3">
                        <strong>अपडेट:</strong><br>
                        <span id="lastUpdate" class="h5">-</span>
                    </div>
                </div>
                <p class="mt-3">
                    <strong>स्थिति:</strong> 
                    <span id="statusText" class="badge bg-secondary fs-6">बंद</span>
                </p>
            </div>
        </div>

        <div class="col-md-4">
            <div class="status-card">
                <h4>📋 GPS लॉग</h4>
                <div id="logBox">
                    <!-- Logs will appear here -->
                </div>
                <div class="mt-2">
                    <button class="btn btn-sm btn-light me-2" onclick="clearLogs()">लॉग साफ़ करें</button>
                </div>
            </div>
        </div>
    </div>

    <div class="text-center mt-4">
        <a href="/dashboard" class="btn btn-light me-3">📊 डैशबोर्ड</a>
        <a href="/" class="btn btn-secondary">🏠 होम</a>
    </div>
</div>

<!-- Scripts -->
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>

<script>
// Global variables
const socket = io();
const busId = ''' + str(bus_id) + ''';
let watchId = null;
let map = null;
let marker = null;
let lastLat = ''' + str(bus_lat) + ''';
let lastLng = ''' + str(bus_lng) + ''';

// Initialize map
function initMap() {
    try {
        map = L.map('map').setView([''' + str(bus_lat) + ''', ''' + str(bus_lng) + '''], 15);

        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '© OpenStreetMap',
            maxZoom: 19
        }).addTo(map);

        const busIcon = L.divIcon({
            html: '<div class="bus-icon">🚌</div>',
            className: 'custom-bus-icon',
            iconSize: [60, 60]
        });

        marker = L.marker([''' + str(bus_lat) + ''', ''' + str(bus_lng) + '''], {icon: busIcon})
            .addTo(map)
            .bindPopup('<b>''' + bus['bus_name'] + '''</b><br>ड्राइवर GPS');

        addLog('✅ मैप तैयार है');
    } catch (err) {
        addLog('❌ मैप error: ' + err.message);
    }
}

// Start GPS tracking
function startGPS() {
    if (!navigator.geolocation) {
        alert('इस ब्राउज़र में GPS सपोर्ट नहीं है');
        addLog('❌ GPS सपोर्ट नहीं');
        return;
    }

    const options = {
        enableHighAccuracy: true,
        maximumAge: 0,
        timeout: 10000
    };

    watchId = navigator.geolocation.watchPosition(
        // Success callback
        function(position) {
            const lat = position.coords.latitude;
            const lng = position.coords.longitude;
            const speed = (position.coords.speed || 0) * 3.6;

            // Update UI
            document.getElementById('lat').textContent = lat.toFixed(6);
            document.getElementById('lng').textContent = lng.toFixed(6);
            document.getElementById('speed').textContent = speed.toFixed(1) + ' km/h';
            document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();

            // Update map
            if (map && marker) {
                marker.setLatLng([lat, lng]);
                map.setView([lat, lng], map.getZoom());
            }

            // Send to server
            sendToServer(lat, lng, speed);

            // Save last location
            lastLat = lat;
            lastLng = lng;

            // Update status
            document.getElementById('startBtn').disabled = true;
            document.getElementById('stopBtn').disabled = false;
            document.getElementById('statusText').className = 'badge bg-success fs-6';
            document.getElementById('statusText').textContent = 'चालू';

            addLog('📍 लोकेशन: ' + lat.toFixed(6) + ', ' + lng.toFixed(6) + ' | गति: ' + speed.toFixed(1) + ' km/h');
        },

        // Error callback
        function(error) {
            let errorMsg = 'GPS त्रुटि: ';
            switch(error.code) {
                case error.PERMISSION_DENIED:
                    errorMsg += 'Permission denied';
                    break;
                case error.POSITION_UNAVAILABLE:
                    errorMsg += 'Position unavailable';
                    break;
                case error.TIMEOUT:
                    errorMsg += 'Timeout';
                    break;
                default:
                    errorMsg += 'Unknown error';
            }

            addLog('❌ ' + errorMsg);
            document.getElementById('statusText').className = 'badge bg-danger fs-6';
            document.getElementById('statusText').textContent = 'त्रुटि';
        },

        options
    );

    addLog('✅ GPS ट्रैकिंग शुरू');
}

// Send location to server
function sendToServer(lat, lng, speed) {
    try {
        socket.emit('driver_gps', {
            sid: busId,
            lat: lat,
            lng: lng,
            speed: speed,
            timestamp: new Date().toISOString()
        });
    } catch (err) {
        addLog('❌ सर्वर error: ' + err.message);
    }
}

// Stop GPS
function stopGPS() {
    if (watchId) {
        navigator.geolocation.clearWatch(watchId);
        watchId = null;
    }

    document.getElementById('startBtn').disabled = false;
    document.getElementById('stopBtn').disabled = true;
    document.getElementById('statusText').className = 'badge bg-secondary fs-6';
    document.getElementById('statusText').textContent = 'बंद';

    addLog('🛑 GPS बंद कर दिया');
}

// Log functions
function addLog(message) {
    const logBox = document.getElementById('logBox');
    const timestamp = new Date().toLocaleTimeString();
    const logEntry = '<div>[' + timestamp + '] ' + message + '</div>';

    logBox.innerHTML = logEntry + logBox.innerHTML;

    // Keep only last 15 logs
    const logs = logBox.getElementsByTagName('div');
    if (logs.length > 15) {
        logBox.removeChild(logs[logs.length - 1]);
    }
}

function clearLogs() {
    document.getElementById('logBox').innerHTML = '';
    addLog('लॉग साफ़ किए गए');
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    initMap();
    addLog('पेज लोड हो गया | बस ID: ' + busId);
});

// Listen for server updates
socket.on('bus_location', function(data) {
    if (data.sid == busId) {
        console.log('Server update received:', data);
    }
});
</script>
</body>
</html>'''

    return html_content


@app.route("/live-bus/<int:bus_id>")
def live_bus(bus_id):
    """Live bus tracking page - WITHOUT in_memory_data"""

    try:
        # Get bus info directly from database
        conn, cur = get_db()
        cur.execute("""
            SELECT s.id, s.bus_name, s.current_lat, s.current_lng, 
                   r.route_name, s.departure_time, r.distance_km
            FROM schedules s
            LEFT JOIN routes r ON s.route_id = r.id
            WHERE s.id = %s
        """, (bus_id,))
        bus = cur.fetchone()

        if not bus:
            # Create default bus data if not found in DB
            bus = {
                'id': bus_id,
                'bus_name': f'Bus {bus_id}',
                'current_lat': 27.5,
                'current_lng': 75.0,
                'route_name': 'Unknown Route',
                'departure_time': '08:00',
                'distance_km': 0
            }
        else:
            # Ensure lat/lng are not None
            bus['current_lat'] = bus['current_lat'] or 27.5
            bus['current_lng'] = bus['current_lng'] or 75.0
            bus['distance_km'] = bus['distance_km'] or 0

        bus_lat = float(bus['current_lat'])
        bus_lng = float(bus['current_lng'])

    except Exception as e:
        # Fallback if database fails
        print(f"Live bus page DB error: {e}")
        bus_lat = 27.5
        bus_lng = 75.0
        bus = {
            'id': bus_id,
            'bus_name': f'Bus {bus_id}',
            'route_name': 'Default Route',
            'departure_time': '08:00',
            'distance_km': 0
        }

    # HTML CONTENT using simple string concatenation
    html_content = '''<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🚌 लाइव ट्रैकिंग - ''' + bus['bus_name'] + '''</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <style>
        body {
            background: linear-gradient(135deg, #0f2027, #203a43, #2c5364);
            color: white;
            padding: 20px;
            min-height: 100vh;
        }
        #map {
            height: 400px;
            width: 100%;
            border-radius: 15px;
            margin: 20px 0;
            border: 3px solid #00ff88;
            box-shadow: 0 10px 30px rgba(0,255,136,0.3);
        }
        .info-card {
            background: rgba(255,255,255,0.1);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            padding: 15px;
            margin-bottom: 15px;
            border: 1px solid rgba(0,255,136,0.3);
        }
        .live-badge {
            background: #ff0000;
            color: white;
            padding: 5px 15px;
            border-radius: 20px;
            font-weight: bold;
            animation: pulse 1.5s infinite;
        }
        @keyframes pulse {
            0% { opacity: 1; }
            50% { opacity: 0.5; }
            100% { opacity: 1; }
        }
    </style>
</head>
<body>

<div class="container">
    <div class="d-flex justify-content-between align-items-center mb-4">
        <h1 class="mb-0">🚌 लाइव बस ट्रैकिंग</h1>
        <span class="live-badge">LIVE</span>
    </div>

    <div class="info-card">
        <h3>''' + bus['bus_name'] + '''</h3>
        <p class="mb-1"><strong>रूट:</strong> ''' + bus['route_name'] + '''</p>
        <p class="mb-1"><strong>प्रस्थान समय:</strong> ''' + str(bus['departure_time']) + '''</p>
        <p class="mb-0"><strong>दूरी:</strong> ''' + str(bus['distance_km']) + ''' km</p>
    </div>

    <div id="map"></div>

    <div class="row">
        <div class="col-md-6">
            <div class="info-card">
                <h4>📍 वर्तमान स्थान</h4>
                <div class="row">
                    <div class="col-6">
                        <p><strong>अक्षांश:</strong><br>
                        <span id="busLat" class="h5">''' + str(bus_lat) + '''</span></p>
                    </div>
                    <div class="col-6">
                        <p><strong>देशांतर:</strong><br>
                        <span id="busLng" class="h5">''' + str(bus_lng) + '''</span></p>
                    </div>
                </div>
                <div class="row">
                    <div class="col-6">
                        <p><strong>गति:</strong><br>
                        <span id="busSpeed" class="h5">0 km/h</span></p>
                    </div>
                    <div class="col-6">
                        <p><strong>अंतिम अपडेट:</strong><br>
                        <span id="busUpdate" class="h6">-</span></p>
                    </div>
                </div>
            </div>
        </div>

        <div class="col-md-6">
            <div class="info-card">
                <h4>📊 कंट्रोल्स</h4>
                <div class="d-grid gap-2">
                    <a href="/driver/''' + str(bus_id) + '''" class="btn btn-success btn-lg">🚗 ड्राइवर मोड</a>
                    <a href="/seats/''' + str(bus_id) + '''" class="btn btn-primary btn-lg">🎫 सीट बुक करें</a>
                    <a href="/" class="btn btn-secondary">🏠 होम</a>
                </div>
            </div>
        </div>
    </div>
</div>

<!-- Scripts -->
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>

<script>
const socket = io();
const busId = ''' + str(bus_id) + ''';
let map = null;
let marker = null;

// Initialize map
function initMap() {
    try {
        map = L.map('map').setView([''' + str(bus_lat) + ''', ''' + str(bus_lng) + '''], 13);

        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '© OpenStreetMap',
            maxZoom: 19
        }).addTo(map);

        // Custom bus icon
        const busIcon = L.divIcon({
            html: '<div style="background: linear-gradient(135deg, #ff0000, #ff8800); color: white; padding: 15px; border-radius: 50%; font-size: 24px; border: 4px solid white; box-shadow: 0 0 25px rgba(255,0,0,0.7);">🚌</div>',
            className: 'live-bus-icon',
            iconSize: [70, 70]
        });

        marker = L.marker([''' + str(bus_lat) + ''', ''' + str(bus_lng) + '''], {icon: busIcon})
            .addTo(map)
            .bindPopup('<b>''' + bus['bus_name'] + '''</b><br>लाइव ट्रैकिंग<br>गति: 0 km/h')
            .openPopup();

        console.log('✅ मैप तैयार | बस ID: ' + busId);
    } catch (err) {
        console.error('❌ मैप error:', err);
    }
}

// Listen for live GPS updates from driver
socket.on('bus_location', function(data) {
    if (data.sid == busId) {
        const lat = parseFloat(data.lat);
        const lng = parseFloat(data.lng);
        const speed = parseFloat(data.speed) || 0;

        // Update UI
        document.getElementById('busLat').textContent = lat.toFixed(6);
        document.getElementById('busLng').textContent = lng.toFixed(6);
        document.getElementById('busSpeed').textContent = speed.toFixed(1) + ' km/h';
        document.getElementById('busUpdate').textContent = new Date().toLocaleTimeString();

        // Update map
        if (map && marker) {
            marker.setLatLng([lat, lng]);
            map.panTo([lat, lng]);
            marker.setPopupContent('<b>''' + bus['bus_name'] + '''</b><br>गति: ' + speed.toFixed(1) + ' km/h<br>' + new Date().toLocaleTimeString());
        }

        console.log('📍 अपडेट: ' + lat.toFixed(4) + ', ' + lng.toFixed(4) + ' | गति: ' + speed.toFixed(1) + ' km/h');
    }
});

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    initMap();

    // Auto-refresh connection
    setInterval(function() {
        socket.emit('heartbeat', { bus_id: busId });
    }, 30000);
});
</script>
</body>
</html>'''

    return html_content


@app.route("/create-payment", methods=["POST"])
def create_payment():
    if not RAZORPAY_ENABLED:
        return jsonify({
            "ok": False,
            "error": "Payment gateway not configured"
        }), 400

    data = request.get_json()

    order = razor_client.order.create({
        "amount": int(data['fare']) * 100,
        "currency": "INR",
        "receipt": f"seat_{data['sid']}_{data['seat']}",
        "payment_capture": 1
    })

    return jsonify({
        "ok": True,
        "order_id": order['id'],
        "key": os.getenv("RAZORPAY_KEY_ID")
    })


@app.route("/verify-payment", methods=["POST"])
@safe_db
def verify():
    data = request.get_json()

    conn, cur = get_db()

    # ✅ If Razorpay enabled → verify
    if RAZORPAY_ENABLED:
        try:
            razor_client.utility.verify_payment_signature({
                'razorpay_order_id': data['order_id'],
                'razorpay_payment_id': data['payment_id'],
                'razorpay_signature': data['signature']
            })
        except:
            return jsonify({"ok": False, "error": "Invalid payment"}), 400

    # ✅ Common confirm logic
    cur.execute("""
        UPDATE seat_bookings
        SET status='confirmed'
        WHERE schedule_id=%s AND seat_number=%s
    """, (data['sid'], data['seat']))

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
        return "Please select both From and To stations", 400

    fs = fs_input.lower()
    ts = ts_input.lower()

    conn, cur = get_db()

    # Step 1: Find routes containing both stations
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

    # Step 2: Check correct order
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

    # 🔥 DIRECT REDIRECT
    return redirect(f"/buses/{route['id']}")


if __name__ == "__main__":
    print("🚀 Bus Booking App Starting... (Live Updates 100% Working)")
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=True)