from dotenv import load_dotenv
import json
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
import mysql.connector
import atexit
import razorpay
#import psycopg2
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

pool = ConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=10, timeout=20)
print("✅ Connection pool ready")


@atexit.register
def shutdown_pool():
    pool.close()


# ================= DB CONTEXT =================
def get_db():
    if 'db_conn' not in g:
        g.db_conn = pool.getconn()
    if 'db_cur' not in g:
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
        finally:
            close_db()  # चाहे error आये या न आये

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

    # Save to DB
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

    emit("bus_location", {
        "sid": sid, "lat": lat, "lng": lng, "speed": speed,
        "timestamp": data.get('timestamp', '')
    }, broadcast=True)


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
    <a href="/login">User Login</a>
    <a href="/admin">Admin</a>
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
#@safe_db
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
                        <a href="/live-bus/{{ bus.id }}" class="btn btn-primary float-end mt-2">🗺️ Live GPS</a>
                        <a href="/select/{{ bus.id }}" class="btn btn-success float-end mt-2">🎫 Book Seat</a>
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
        counter_no = request.form.get("counter_no")

        if not username or not password or not counter_no:
            error = "सभी fields भरें"
        else:
            try:
                conn, cur = get_db()
                cur.execute("""
                    INSERT INTO admins (username, password, role, counter_no)
                    VALUES (%s, %s, 'counter', %s)
                    ON CONFLICT (username) DO NOTHING
                """, (username, password, counter_no))
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
                <div class="mb-3">
                    <label class="form-label">Counter Number</label>
                    <input type="number" name="counter_no" class="form-control" required>
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


@app.route("/admin")
def admin():
    return render_template_string(
        BASE_HTML,
        content="<h2 class='text-center mt-5'>Welcome Admin 🎉</h2>"
    )


@app.route("/select/<int:sid>")
@safe_db
def select(sid):
    fs = session.get("from")
    ts = session.get("to")
    d  = session.get("date")

    if not fs or not ts or not d:
        return redirect("/")

    return redirect(f"/seats/{sid}?fs={fs}&ts={ts}&d={d}")


@app.route("/seats/<int:sid>")
@safe_db
def seat_page(sid):
    conn, cur = get_db()

    # Schedule details, route info, bus info
    cur.execute("""
        SELECT s.id, s.bus_name, s.departure_time, r.route_name, r.id as route_id, s.current_lat, s.current_lng
        FROM schedules s
        JOIN routes r ON s.route_id = r.id
        WHERE s.id = %s
    """, (sid,))
    schedule = cur.fetchone()

    if not schedule:
        return "Schedule not found", 404

    # Route stations
    cur.execute("""
        SELECT station_name, station_order
        FROM route_stations WHERE route_id=%s
        ORDER BY station_order
    """, (schedule['route_id'],))
    stations = cur.fetchall()
    stations_map = {r['station_name']: r['station_order'] for r in stations}

    # Booked seats for today
    today = date.today().isoformat()
    cur.execute("""
        SELECT seat_number, from_station, to_station
        FROM seat_bookings
        WHERE schedule_id=%s AND travel_date=%s AND status='confirmed'
    """, (sid, today))
    booked = cur.fetchall()

    booked_seats = set()
    for r in booked:
        booked_seats.add(r['seat_number'])

    total_seats = 40
    seat_buttons = ""

    # Generate seat buttons with color coding
    for i in range(1, 41):
        if i in booked:
            seat_buttons += f'''
            <button id="seat-{i}" class="btn btn-danger seat" disabled>X{i}</button>
            '''
        else:
            seat_buttons += f'''
            <button id="seat-{i}" class="btn btn-success seat" onclick="bookSeat({i},{sid})">
                {i}
            </button>
            '''

    # Google Maps iframe for live bus location
    bus_lat = schedule['current_lat'] or 0
    bus_lon = schedule['current_lng'] or 0
    counter_js = "null"
    if session.get("role") == "counter":
        counter_js = session.get("user_id")

    map_iframe = f"""
    <iframe
        width="100%"
        height="300"
        frameborder="0" style="border:0"
        src="https://www.google.com/maps?q={bus_lat},{bus_lon}&hl=es;z=14&output=embed"
        allowfullscreen>
    </iframe>
    """


    # Complete HTML
    html_content = f"""
    <div class="container" style="max-width:900px;margin:auto;">
        <h2 style="margin-top:20px;">Bus: {schedule['bus_name']} | Route: {schedule['route_name']}</h2>
        <h4>Departure: {schedule['departure_time'].strftime('%H:%M')}</h4>

        <!-- Live Bus Location -->
        <div style="margin-top:30px;">
            <h5>Bus Live Location</h5>
            {map_iframe}
        </div>
        
        <!-- Seat Selection -->
        <div style="margin-top:40px;">
            <h5>Select Your Seat</h5>
            <div style="display:flex; flex-wrap:wrap; gap:10px; margin-top:15px;">
                {seat_buttons}
            </div>
        </div>
    </div>

    <script>
     <!-- SOCKET -->
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>

    <script>
    const socket = io({{transports:["websocket","polling"]}});
    const SID = {sid};

    socket.on("connect", () => {{
        console.log("Socket connected");
    }});

    /// 🔥 INSTANT Seat Update (Real-time)
    socket.on('seat_update', function(data) {{
        console.log('🔴 SEAT UPDATE RECEIVED:', data);

        // Match current page
        if(window.currentSid != data.sid || window.currentDate != data.date) {{
            console.log('⏭️ Different bus/date, ignoring');
            return;
        }}

        let seatBtn = document.getElementById('seat-' + data.seat);
        if(seatBtn) {{
            seatBtn.classList.remove('btn-success', 'btn-outline-success');
            seatBtn.classList.add('btn-danger');
            seatBtn.disabled = true;
            seatBtn.innerHTML = '<i class="fas fa-user-check"></i> X' + data.seat;
            console.log('✅ Seat ' + data.seat + ' turned RED instantly!');

            // Visual feedback
            seatBtn.style.transform = 'scale(1.1)';
            setTimeout(() => seatBtn.style.transform = 'scale(1)', 200);
        }} else {{
            console.log('❌ Seat button not found:', data.seat);
        }}
    }});

    // 🪑 Book Seat Function (Improved)
    function bookSeat(seatId, fromStation, toStation, travelDate, scheduleId) {{
        console.log('🎫 Booking seat:', seatId);

        let name = prompt('👤 Passenger Name:');
        if(!name || name.trim() === '') {{
            alert('❌ नाम भरें!');
            return;
        }}

        let mobile = prompt('📱 Mobile Number:');
        if(!mobile || !/^[6-9]\\d{{9}}$/.test(mobile)) {{
            alert('❌ Valid mobile number दें!');
            return;
        }}

        // Show loading
        let seatBtn = document.getElementById('seat-' + seatId);
        let originalText = seatBtn.innerHTML;
        seatBtn.innerHTML = '⏳ Booking...';
        seatBtn.disabled = true;
        fetch('/book', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{
                sid: scheduleId,
                seat: seatId,
                name: name.trim(),
                mobile: mobile,
                from: fromStation,
                to: toStation,
                date: travelDate
            }})
        }})
        .then(response => {{
            console.log('📡 Response status:', response.status);
            return response.json();
        }})
        .then(result => {{
            console.log('📋 Booking result:', result);
            if(result.ok) {{
                alert('🎉 ' + result.msg);
                // Socket update भी आएगा automatically
            }} else {{
                alert('❌ ' + result.msg);
                // Re-enable button
                seatBtn.innerHTML = originalText;
                seatBtn.disabled = false;
            }}
        }})
        .catch(error => {{
            console.error('❌ Fetch error:', error);
            alert('❌ Network error! फिर कोशिश करें।');
            seatBtn.innerHTML = originalText;
            seatBtn.disabled = false;
        }});
    }}
    </script>
    """
    

    return render_template_string(BASE_HTML, content=html_content)


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

        fare = random.randint(250, 450)

        cur.execute("""
        INSERT INTO seat_bookings
        (
            schedule_id,
            seat_number,
            passenger_name,
            mobile,
            from_station,
            to_station,
            travel_date,
            fare,
            status,
            payment_mode,
            booked_by_type,
            booked_by_id,
            counter_id
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'confirmed','cash','user',1,%s)
        """, (
            data['schedule_id'],
            data['seat_number'],
            data['passenger_name'],
            data['mobile'],
            session.get("from"),
            session.get("to"),
            data['date'],
            fare,
            data.get('counter_id')
        ))

        conn.commit()
        socketio.emit("seat_update", {
            "sid": data['schedule_id'],  # schedule_id को sid की जगह
            "seat": data['seat_number']  # data['seat'] नहीं, data['seat_number']
        })
        return jsonify({"ok": True, "fare": fare})

    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)})


@app.route("/driver/<int:sid>")
def driver(sid):
    return f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Bus {sid} GPS</title>

    <style>
        body {{
            background-color: #f0f0f0;
            padding: 40px;
            text-align: center;
            font-family: sans-serif;
            margin: 0;
        }}
        h2 {{
            color: #333;
        }}
        .btn-gps {{
            padding: 15px 30px;
            font-size: 18px;
            border: none;
            border-radius: 10px;
            background-color: #28a745;
            color: white;
            cursor: pointer;
            font-weight: bold;
        }}
        .btn-stop {{
            padding: 15px 30px;
            font-size: 18px;
            border: none;
            border-radius: 10px;
            background-color: #dc3545;
            color: white;
            cursor: pointer;
            font-weight: bold;
            margin-left: 10px;
        }}
        #status {{
            font-size: 18px;
            margin-top: 25px;
            color: #333;
            font-family: monospace;
            padding: 15px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
    </style>
</head>

<body>

    <h2>🚗 Driver GPS – Bus {sid}</h2>

    <button id="startBtn" class="btn-gps" onclick="startGPS()">🚀 GPS शुरू करें</button>
    <button id="stopBtn" class="btn-stop" onclick="stopGPS()" disabled>🛑 GPS बंद करें</button>

    <div id="status">GPS बंद है</div>

    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <script>
        const socket = io({{ transports: ["websocket", "polling"] }});
        let watchId = null;

        function startGPS() {{
            const startBtn = document.getElementById("startBtn");
            const stopBtn = document.getElementById("stopBtn");
            const status = document.getElementById("status");

            // ✅ GPS support check
            if (!navigator.geolocation) {{
                status.innerHTML = "❌ इस ब्राउज़र में GPS सपोर्ट नहीं है";
                return;
            }}

            startBtn.disabled = true;
            stopBtn.disabled = false;
            startBtn.innerHTML = "⏳ GPS चालू हो रहा है...";
            status.innerHTML = "📡 GPS खोज रहे हैं...";

            watchId = navigator.geolocation.watchPosition(
                function (pos) {{
                    const lat = pos.coords.latitude.toFixed(6);
                    const lng = pos.coords.longitude.toFixed(6);

                    const data = {{
                        sid: {sid},
                        lat: lat,
                        lng: lng
                    }};

                    socket.emit("driver_gps", data);

                    status.innerHTML = "✅ LIVE GPS<br>Latitude: " + lat + "<br>Longitude: " + lng;
                    startBtn.innerHTML = "🚗 Live GPS चल रहा है";
                }},
                function (err) {{
                    status.innerHTML = "❌ GPS Error: " + err.message;
                    startBtn.disabled = false;
                    stopBtn.disabled = true;
                    startBtn.innerHTML = "🔄 GPS फिर शुरू करें";
                }},
                {{
                    enableHighAccuracy: true,
                    timeout: 10000,
                    maximumAge: 5000
                }}
            );
        }}

        function stopGPS() {{
            const startBtn = document.getElementById("startBtn");
            const stopBtn = document.getElementById("stopBtn");
            const status = document.getElementById("status");

            if (watchId !== null) {{
                navigator.geolocation.clearWatch(watchId);
                watchId = null;
            }}

            socket.emit("driver_gps_stop", {{ sid: {sid} }});

            startBtn.disabled = false;
            stopBtn.disabled = true;
            startBtn.innerHTML = "🚀 GPS शुरू करें";
            status.innerHTML = "🛑 GPS बंद कर दिया गया";
        }}
    </script>

</body>
</html>
"""


@app.route("/live-bus/<int:sid>")
@safe_db
def live_bus(sid):
    conn, cur = get_db()

    # Bus + Route info
    cur.execute("""
        SELECT s.id, s.bus_name, s.departure_time,
               r.id as route_id, r.route_name, r.distance_km,
               s.current_lat as lat, s.current_lng as lng
        FROM schedules s 
        JOIN routes r ON s.route_id = r.id 
        WHERE s.id = %s
    """, (sid,))
    bus = cur.fetchone()

    if not bus:
        return "Bus not found", 404

    lat = float(bus.get('lat', 27.2))
    lng = float(bus.get('lng', 74.2))

    # Route Stations for Polyline
    cur.execute("""
        SELECT lat, lng, station_name
        FROM route_stations
        WHERE route_id=%s
        ORDER BY station_order
    """, (bus['route_id'],))
    stations = cur.fetchall()

    import json
    stations_json = json.dumps(stations)  # ✅ Python side JSON

    content = f'''
    <style>
    #map{{height:70vh;width:100%;border-radius:20px;box-shadow:0 20px 40px rgba(0,0,0,0.3);}}
    .live-bus{{animation:pulse 2s infinite;width:30px;height:30px;background:#ff4444;border-radius:50%;border:3px solid #fff;box-shadow:0 0 20px #ff4444;}}
    @keyframes pulse{{0%,100%{{transform:scale(1);}}50%{{transform:scale(1.2);}}}}
    .stats-card{{background:rgba(255,255,255,0.95);backdrop-filter:blur(20px);padding:15px;}}
    </style>

    <div class="text-center mb-5">
        <h2 class="display-5 fw-bold mb-2">🚌 {bus['bus_name']}</h2>
        <h5 class="text-muted mb-1">{bus['route_name']} ({bus['distance_km']}km)</h5>
        <div class="h6 {'text-success' if bus.get('lat') else 'text-warning'} mb-3">
            {"🟢 LIVE GPS" if bus.get('lat') else "📡 Waiting for GPS..."}
        </div>
    </div>

    <div class="row g-4">
        <div class="col-lg-12">
            <div id="map" class="rounded-4"></div>
        </div>
    </div>

    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>

    <script>
    const map = L.map('map').setView([{lat}, {lng}], {13 if bus.get('lat') else 10});
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
        attribution: '© OpenStreetMap'
    }}).addTo(map);

    // ===== ROUTE POLYLINE =====
    const stations = {stations_json};
    let routePoints = [];

    stations.forEach(st => {{
        const lat = parseFloat(st.lat);
        const lng = parseFloat(st.lng);
        if(!isNaN(lat) && !isNaN(lng)){{
            routePoints.push([lat,lng]);
            // Station markers
            L.marker([lat,lng]).addTo(map).bindPopup("📍 " + st.station_name);
        }}
    }});

    let routeLine = null;
    if(routePoints.length > 1){{
        routeLine = L.polyline(routePoints, {{
            color: 'Blue',   // thick red polyline
            weight: 8,
            opacity: 0.9
        }}).addTo(map);
        map.fitBounds(routeLine.getBounds());
    }}

    // ===== BUS ICON =====
    const busIcon = L.divIcon({{
        html: '<i class="fa fa-bus" style="font-size:28px;color:green;"></i>',
        className: 'bus-icon',
        iconSize: [60,60]
    }});
    let busMarker = L.marker(routePoints[0] || [{lat},{lng}], {{icon: busIcon}}).addTo(map);

    // ===== SOCKET LIVE UPDATE =====
    const sid = {sid};
    const socket = io({{transports:["websocket","polling"]}});

    socket.on('connect', () => {{
        console.log('✅ Socket Connected');
    }});

    socket.on('bus_location', data => {{
        if(data.sid == sid){{
            const lat = parseFloat(data.lat);
            const lng = parseFloat(data.lng);
            busMarker.setLatLng([lat,lng]);
            if(routeLine) map.panTo([lat,lng], {{animate:true}});
        }}
    }});
    </script>
    '''

    return render_template_string(BASE_HTML, content=content)


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
