from dotenv import load_dotenv
import os
import json
from datetime import date
from functools import wraps
from contextlib import contextmanager
from flask import Flask, request, jsonify, render_template_string, redirect, session
from flask_socketio import SocketIO, emit
from flask_compress import Compress
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
import atexit
import razorpay
import random
import traceback

load_dotenv()

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

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                    logger=True, engineio_logger=True, ping_timeout=60)

# PostgreSQL connection pool setup
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise Exception("DATABASE_URL environment variable is missing!")

pool = ConnectionPool(conninfo=DATABASE_URL, min_size=2, max_size=10, timeout=30)


@atexit.register
def shutdown_pool():
    try:
        pool.close()
        print("✅ Connection pool closed")
    except Exception as e:
        print(f"⚠️ Error closing pool: {e}")


# ================= DB Helper Functions =================
@contextmanager
def get_db():
    """Thread-safe database connection context manager"""
    conn = None
    cur = None
    try:
        conn = pool.getconn()
        cur = conn.cursor(row_factory=dict_row)
        yield conn, cur
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except:
                pass
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
                print(f"⚠️ Error returning connection to pool: {e}")


def safe_db(func):
    """Decorator for database operations with automatic cleanup"""

    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            print(f"❌ Database error in {func.__name__}: {str(e)}")
            traceback.print_exc()
            return jsonify({"error": "Database error occurred"}), 500

    return wrapper


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

        # टेबल्स बनाना
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
            )
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
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS routes (
                id SERIAL PRIMARY KEY, 
                route_name VARCHAR(100) UNIQUE, 
                distance_km INT
            )
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
            )
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
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS route_stations (
                id SERIAL PRIMARY KEY, 
                route_id INT REFERENCES routes(id), 
                station_name VARCHAR(50), 
                station_order INT,
                lat DOUBLE PRECISION DEFAULT 27.2,
                lng DOUBLE PRECISION DEFAULT 75.2
            )
        """)

        conn.commit()

        # डिफ़ॉल्ट डेटा इनसेट करना
        cur.execute("SELECT COUNT(*) FROM admins")
        count = cur.fetchone()[0]
        if count == 0:
            cur.execute("""
                INSERT INTO admins (username, password, role, counter_no)
                VALUES('admin', 'admin123', 'admin', 1)
                ON CONFLICT DO NOTHING;
            """)

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
                    INSERT INTO schedules (id, route_id, bus_name, departure_time, total_seats)
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
                    INSERT INTO route_stations (route_id,station_name,station_order)
                    VALUES (%s,%s,%s) ON CONFLICT DO NOTHING
                """, st)

            conn.commit()
        print("✅ DB Init Complete!")

    except Exception as e:
        print(f"❌ DB Init Error: {e}")
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

    conn = None
    try:
        conn = pool.getconn()
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("""
            UPDATE schedules SET current_lat=%s, current_lng=%s WHERE id=%s
        """, (lat, lng, sid))
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"❌ GPS Update Error: {e}")
        if conn:
            try:
                conn.rollback()
            except:
                pass
    finally:
        if conn:
            try:
                pool.putconn(conn)
            except:
                pass

    socketio.emit("bus_location", {
        "sid": sid,
        "lat": lat,
        "lng": lng,
        "speed": speed,
        "timestamp": data.get('timestamp', '')
    })


# ================= HTML Templates =================
BASE_HTML = """
<!DOCTYPE html>
<html lang="hi">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>माई बस एआई - अपनी यात्रा बुक करें</title>
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
    <p>बुक करें | ट्रैक करें | फेस बोर्डिंग | लाइव सीटें</p>
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

# ===== login html =======
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
            return "मार्ग नहीं मिला", 404

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

    html = """
    <!DOCTYPE html>
    <html lang="hi">
    <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
    <title>🚌 {{ route.route_name }} - प्रीमियम बुकिंग</title>
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
        <h1>🚌 {{ route.route_name }} - प्रीमियम बुकिंग</h1>
        <p>📍 {{ route.stations }} | 🛣️ {{ route.distance_km }} किमी</p>
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
                                {{ '🟢 LIVE' if bus.current_lat else '⚪ ऑफलाइन' }}
                            </span>
                        </div>
                        <div class="bus-info mt-2">
                            <p><i class="fas fa-clock"></i> प्रस्थान: {{ bus.departure_time.strftime('%H:%M') }}</p>
                            <p><i class="fas fa-chair"></i> बची सीटें: {{ bus.total_seats - bus.booked_count }} | कुल सीटें: {{ bus.total_seats }}</p>
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

    return render_template_string(html, route=route, buses=buses_data)


# **** create counter ******
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


# ******* login ********
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


# ******* counter login (same as above) *****
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
        # Schedule details
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

        # Route stations
        cur.execute("""
            SELECT station_name, station_order
            FROM route_stations
            WHERE route_id=%s
            ORDER BY station_order
        """, (schedule['route_id'],))
        stations = cur.fetchall()

        # Booked seats
        today = session.get("date", date.today().isoformat())
        cur.execute("""
            SELECT seat_number
            FROM seat_bookings
            WHERE schedule_id=%s AND travel_date=%s AND status='confirmed'
        """, (sid, today))
        booked = cur.fetchall()
        booked_seats = set(r['seat_number'] for r in booked)

    # Seat buttons
    seat_buttons = ""
    for i in range(1, 41):
        if i in booked_seats:
            seat_buttons += f'<button id="seat-{i}" class="btn btn-danger seat" disabled>X{i}</button>'
        else:
            seat_buttons += f'<button id="seat-{i}" class="btn btn-success seat" onclick="bookSeat({i})">{i}</button>'

    # Map location
    user_role = session.get("role", "guest")
    counter_id = session.get("user_id") if user_role in ("counter", "conductor") else None
    bus_lat = schedule['current_lat'] if schedule['current_lat'] else 27.5
    bus_lon = schedule['current_lng'] if schedule['current_lng'] else 75.0
    counter_js = session.get("user_id") if session.get("role") == "counter" else "null"

    # HTML content
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
const socket = io(window.location.origin);
const SID = {sid};
const TODAY = "{today}";
const BUS_LAT = {bus_lat};
const BUS_LNG = {bus_lon};
const COUNTER_ID = {counter_js};

// Map init
const map = L.map('map').setView([BUS_LAT, BUS_LNG], 10);
L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/">CARTO</a>',
    subdomains: 'abcd',
    maxZoom: 19
}}).addTo(map);
let busMarker = L.marker([BUS_LAT, BUS_LNG]).addTo(map);
let routeLine = null;

// Live location update
socket.on('bus_location', data => {{
    if(data.sid == SID){{
        const lat = parseFloat(data.lat);
        const lng = parseFloat(data.lng);
        busMarker.setLatLng([lat,lng]);
        map.panTo([lat,lng], {{animate:true}});
    }}
}});

// Seat booking
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

    with get_db() as (conn, cur):
        # Check seat availability
        cur.execute("""
            SELECT id FROM seat_bookings
            WHERE schedule_id=%s AND seat_number=%s AND travel_date=%s AND status='confirmed'
        """, (data['schedule_id'], data['seat_number'], data['date']))
        if cur.fetchone():
            return jsonify({"ok": False, "error": "सीट पहले से बुक है"}), 409

        user_role = session.get("role", "user")
        if user_role == "counter":
            fare = int(data.get("fare", 0))
            payment_mode = data.get("payment_mode", "cash")
        else:
            fare = random.randint(250, 450)
            payment_mode = "cash"

        # Insert booking
        cur.execute("""
        INSERT INTO seat_bookings (
            schedule_id, seat_number, passenger_name, mobile,
            from_station, to_station, travel_date,
            fare, status, payment_mode,
            booked_by_type, booked_by_id, counter_id
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
            int(data.get("counter_id") or 0)
        ))
        conn.commit()

    # Emit seat update (outside DB context)
    socketio.emit("seat_update", {
        "sid": data['schedule_id'],
        "seat": data['seat_number'],
        "date": data['date']
    })

    return jsonify({"ok": True, "fare": fare})


@app.route("/driver/<int:sid>")
def driver(sid):
    return f"""
<!DOCTYPE html>
<html lang="hi">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>बस {sid} जीपीएस</title>
<style>
body{{ background-color:#f0f0f0; padding:40px; text-align:center; font-family:sans-serif; margin:0; }}
h2{{ color:#333; }}
.btn-gps{{ padding:15px 30px; font-size:18px; border:none; border-radius:10px; background-color:#28a745; color:white; cursor:pointer; font-weight:bold; }}
.btn-stop{{ padding:15px 30px; font-size:18px; border:none; border-radius:10px; background-color:#dc3545; color:white; cursor:pointer; font-weight:bold; margin-left:10px; }}
#status{{ font-size:18px; margin-top:25px; color:#333; font-family:monospace; padding:15px; background:white; border-radius:8px; box-shadow:0 2px 10px rgba(0,0,0,0.1); }}
</style>
</head>
<body>
<h2>🚗 ड्राइवर जीपीएस – बस {sid}</h2>
<button id="startBtn" class="btn-gps" onclick="startGPS()">🚀 जीपीएस शुरू करें</button>
<button id="stopBtn" class="btn-stop" onclick="stopGPS()" disabled>🛑 जीपीएस बंद करें</button>
<div id="status">जीपीएस बंद है</div>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<script>
const socket = io(window.location.origin);
let watchId = null;

function startGPS() {{
    const startBtn = document.getElementById("startBtn");
    const stopBtn = document.getElementById("stopBtn");
    const status = document.getElementById("status");
    if (!navigator.geolocation) {{
        status.innerHTML = "❌ इस ब्राउज़र में जीपीएस सपोर्ट नहीं है";
        return;
    }}
    startBtn.disabled = true;
    stopBtn.disabled = false;
    startBtn.innerHTML = "⏳ जीपीएस चालू हो रहा है...";
    status.innerHTML = "📡 जीपीएस खोज रहे हैं...";
    watchId = navigator.geolocation.watchPosition(
        function (pos) {{
            const lat = pos.coords.latitude.toFixed(6);
            const lng = pos.coords.longitude.toFixed(6);
            const data = {{ sid: {sid}, lat: lat, lng: lng }};
            socket.emit("driver_gps", data);
            status.innerHTML = "✅ LIVE जीपीएस<br>अक्षांश: " + lat + "<br>देशांतर: " + lng;
            startBtn.innerHTML = "🚗 लाइव जीपीएस चल रहा है";
        }},
        function (err) {{
            status.innerHTML = "❌ जीपीएस त्रुटि: " + err.message;
            startBtn.disabled = false;
            stopBtn.disabled = true;
            startBtn.innerHTML = "🔄 जीपीएस फिर शुरू करें";
        }},
        {{ enableHighAccuracy: true, timeout: 10000, maximumAge: 5000 }}
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
    startBtn.innerHTML = "🚀 जीपीएस शुरू करें";
    status.innerHTML = "🛑 जीपीएस बंद कर दिया गया";
}}
</script>
</body>
</html>
"""


@app.route("/live-bus/<int:sid>")
@safe_db
def live_bus(sid):
    with get_db() as (conn, cur):
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
            return "बस नहीं मिली", 404

        lat = float(bus.get('lat', 27.2))
        lng = float(bus.get('lng', 74.2))

        # Route Stations for polyline
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
    .live-bus{{animation:pulse 2s infinite;width:30px;height:30px;background:#ff4444;border-radius:50%;border:3px solid #fff;box-shadow:0 0 20px #ff4444;}}
    @keyframes pulse{{0%,100%{{transform:scale(1);}}50%{{transform:scale(1.2);}}}}
    .stats-card{{background:rgba(255,255,255,0.95);backdrop-filter:blur(20px);padding:15px;}}
    </style>
    <div class="text-center mb-5">
        <h2 class="display-5 fw-bold mb-2">🚌 {bus['bus_name']}</h2>
        <h5 class="text-muted mb-1">{bus['route_name']} ({bus['distance_km']}किमी)</h5>
        <div class="h6 {'text-success' if bus.get('lat') else 'text-warning'} mb-3">
            {"🟢 लाइव जीपीएस" if bus.get('lat') else "📡 जीपीएस का इंतज़ार..."}
        </div>
    </div>
    <div class="row g-4">
        <div class="col-lg-12">
            <div id="map" class="rounded-4"></div>
        </div>
    </div>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <script>
    const map = L.map('map').setView([{lat}, {lng}], 13);
    L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/">CARTO</a>',
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

    let routeLine = null;
    if(routePoints.length > 1){{
        routeLine = L.polyline(routePoints, {{
            color: 'blue',
            weight: 8,
            opacity: 0.9
        }}).addTo(map);
        map.fitBounds(routeLine.getBounds());
    }}

    const busIcon = L.divIcon({{
        html: '<div class="live-bus"></div>',
        className: 'bus-icon',
        iconSize: [30,30]
    }});

    let busMarker = L.marker(routePoints[0] || [{lat},{lng}], {{icon: busIcon}}).addTo(map);
    const sid = {sid};
    const socket = io(window.location.origin);

    socket.on('connect', () => {{
        console.log('✅ सॉकेट कनेक्टेड');
    }});

    socket.on('bus_location', data => {{
        if(data.sid == sid){{
            const lat = parseFloat(data.lat);
            const lng = parseFloat(data.lng);
            busMarker.setLatLng([lat,lng]);
            map.panTo([lat,lng], {{animate:true}});
        }}
    }});
    </script>
    '''
    return render_template_string(BASE_HTML, content=content)


@app.route("/create-payment", methods=["POST"])
def create_payment():
    RAZORPAY_ENABLED = os.getenv("RAZORPAY_ENABLED", "false").lower() == "true"
    if not RAZORPAY_ENABLED:
        return jsonify({"ok": False, "error": "भुगतान गेटवे कॉन्फ़िगर नहीं है"}), 400

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

    RAZORPAY_ENABLED = os.getenv("RAZORPAY_ENABLED", "false").lower() == "true"

    if RAZORPAY_ENABLED:
        try:
            razor_client.utility.verify_payment_signature({
                'razorpay_order_id': data['order_id'],
                'razorpay_payment_id': data['payment_id'],
                'razorpay_signature': data['signature']
            })
        except:
            return jsonify({"ok": False, "error": "अमान्य भुगतान"}), 400

    with get_db() as (conn, cur):
        # Confirm booking
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
        return "कृपया दोनों स्टेशन चुनें", 400

    fs = fs_input.lower()
    ts = ts_input.lower()

    with get_db() as (conn, cur):
        # Find routes containing both stations
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
            content=f"<h3 class='text-center mt-5 text-danger'>🚫 {fs_input} → {ts_input} के लिए कोई वैध मार्ग नहीं</h3>"
        )

    return redirect(f"/buses/{route['id']}")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


# ================= RUN SERVER =================
if __name__ == "__main__":
    print("🚀 बस बुकिंग ऐप शुरू हो रहा है... (लाइव अपडेट 100% काम कर रहे हैं)")
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=True)