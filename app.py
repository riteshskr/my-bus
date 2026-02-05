from dotenv import load_dotenv
import os
import json
from datetime import date, datetime
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

        # सभी टेबल्स बनाना
        tables = [
            """
            CREATE TABLE IF NOT EXISTS faces (
                id SERIAL PRIMARY KEY,
                bus_id INT NOT NULL,
                face_data BYTEA NOT NULL,
                face_image BYTEA NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS face_logs (
                id SERIAL PRIMARY KEY,
                face_id INT NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
                bus_id INT NOT NULL,
                entry_time TIMESTAMP NOT NULL,
                latitude DOUBLE PRECISION,
                longitude DOUBLE PRECISION
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS admins (
                id SERIAL PRIMARY KEY,
                username VARCHAR(50) UNIQUE,
                password VARCHAR(100),
                role VARCHAR(20) DEFAULT 'admin',
                counter_no INTEGER DEFAULT 0
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
                distance_km INT
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
                last_gps_update TIMESTAMP
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
                lng DOUBLE PRECISION DEFAULT 75.2
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS gps_logs (
                id SERIAL PRIMARY KEY,
                schedule_id INT NOT NULL,
                latitude DOUBLE PRECISION NOT NULL,
                longitude DOUBLE PRECISION NOT NULL,
                speed DOUBLE PRECISION DEFAULT 0,
                accuracy DOUBLE PRECISION,
                timestamp TIMESTAMP DEFAULT NOW()
            )
            """
        ]

        for table_sql in tables:
            cur.execute(table_sql)

        conn.commit()

        # डिफ़ॉल्ट डेटा इनसेट करना
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

            stations = [(1, 'बीकानेर', 1), (1, 'जयपुर', 2), (2, 'बीकानेर', 1),
                        (2, 'जोधपुर', 2), (3, 'जयपुर', 1), (3, 'जोधपुर', 2)]
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
    accuracy = data.get('accuracy', 0)

    print(f"📍 LIVE: Bus-{sid} @ [{lat:.5f},{lng:.5f}] {speed}km/h")

    conn = None
    try:
        conn = pool.getconn()
        cur = conn.cursor(row_factory=dict_row)

        # मुख्य टेबल अपडेट
        cur.execute("""
            UPDATE schedules SET current_lat=%s, current_lng=%s, last_gps_update=NOW() 
            WHERE id=%s
        """, (lat, lng, sid))

        # GPS लॉग सेव करें
        cur.execute("""
            INSERT INTO gps_logs (schedule_id, latitude, longitude, speed, accuracy)
            VALUES (%s, %s, %s, %s, %s)
        """, (sid, lat, lng, speed, accuracy))

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
        "timestamp": datetime.now().isoformat()
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
const socket = io(window.location.origin);
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

    socketio.emit("seat_update", {
        "sid": data['schedule_id'],
        "seat": data['seat_number'],
        "date": data['date']
    })

    return jsonify({"ok": True, "fare": fare})


# ================= BACKGROUND GPS SOLUTION =================

@app.route("/driver/<int:sid>")
def driver(sid):
    """ड्राइवर GPS पेज - बैकग्राउंड में काम करने के लिए Service Worker के साथ"""
    return f"""
<!DOCTYPE html>
<html lang="hi">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no"/>
<meta name="theme-color" content="#28a745"/>
<title>बस {sid} - ड्राइवर GPS</title>
<link rel="manifest" href="/manifest.json?id={sid}">
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ 
    font-family: 'Segoe UI', sans-serif; 
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    min-height: 100vh; 
    color: white;
    overflow-x: hidden;
}}
.container {{
    max-width: 100%;
    padding: 20px;
    text-align: center;
}}
.status-card {{
    background: rgba(255,255,255,0.95);
    color: #333;
    border-radius: 20px;
    padding: 25px;
    margin: 20px 0;
    box-shadow: 0 10px 30px rgba(0,0,0,0.3);
}}
.gps-indicator {{
    width: 80px;
    height: 80px;
    border-radius: 50%;
    margin: 20px auto;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 30px;
    transition: all 0.3s;
}}
.gps-active {{
    background: #28a745;
    animation: pulse 2s infinite;
    box-shadow: 0 0 30px #28a745;
}}
.gps-inactive {{
    background: #dc3545;
}}
@keyframes pulse {{
    0%, 100% {{ transform: scale(1); }}
    50% {{ transform: scale(1.1); }}
}}
.btn {{
    width: 100%;
    padding: 18px;
    margin: 10px 0;
    border: none;
    border-radius: 15px;
    font-size: 18px;
    font-weight: bold;
    cursor: pointer;
    transition: all 0.3s;
    text-transform: uppercase;
}}
.btn-start {{
    background: linear-gradient(45deg, #28a745, #20c997);
    color: white;
    box-shadow: 0 5px 20px rgba(40, 167, 69, 0.4);
}}
.btn-stop {{
    background: linear-gradient(45deg, #dc3545, #f093fb);
    color: white;
    box-shadow: 0 5px 20px rgba(220, 53, 69, 0.4);
}}
.btn:disabled {{
    opacity: 0.6;
    cursor: not-allowed;
}}
.stats-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 15px;
    margin: 20px 0;
}}
.stat-box {{
    background: rgba(255,255,255,0.1);
    padding: 15px;
    border-radius: 12px;
    backdrop-filter: blur(10px);
}}
.stat-value {{
    font-size: 24px;
    font-weight: bold;
    color: #ffd700;
}}
.stat-label {{
    font-size: 12px;
    opacity: 0.9;
    margin-top: 5px;
}}
#log {{
    background: rgba(0,0,0,0.3);
    padding: 15px;
    border-radius: 10px;
    font-family: monospace;
    font-size: 12px;
    max-height: 150px;
    overflow-y: auto;
    text-align: left;
    margin-top: 15px;
}}
.wakelock-indicator {{
    position: fixed;
    top: 10px;
    right: 10px;
    background: rgba(0,0,0,0.7);
    padding: 8px 12px;
    border-radius: 20px;
    font-size: 12px;
    display: none;
}}
.wakelock-active {{
    display: block !important;
    background: #28a745 !important;
}}
</style>
</head>
<body>
<div class="wakelock-indicator" id="wakelockIndicator">
    🔒 स्क्रीन लॉक रोका गया
</div>

<div class="container">
    <h1>🚌 बस {sid}</h1>
    <p style="opacity: 0.9;">ड्राइवर GPS ट्रैकिंग</p>

    <div class="status-card">
        <div class="gps-indicator gps-inactive" id="gpsIndicator">📍</div>
        <h2 id="statusText">GPS बंद है</h2>
        <p id="subStatus" style="color: #666; margin-top: 10px;">स्टार्ट बटन दबाएं</p>

        <div class="stats-grid">
            <div class="stat-box">
                <div class="stat-value" id="lat">--</div>
                <div class="stat-label">अक्षांश (Latitude)</div>
            </div>
            <div class="stat-box">
                <div class="stat-value" id="lng">--</div>
                <div class="stat-label">देशांतर (Longitude)</div>
            </div>
            <div class="stat-box">
                <div class="stat-value" id="speed">0</div>
                <div class="stat-label">गति (km/h)</div>
            </div>
            <div class="stat-box">
                <div class="stat-value" id="accuracy">--</div>
                <div class="stat-label">सटीकता (m)</div>
            </div>
        </div>

        <div style="margin-top: 20px;">
            <div id="queueStatus" style="color: #666; font-size: 14px; margin-bottom: 10px;">
                ऑनलाइन मोड
            </div>
        </div>
    </div>

    <button id="startBtn" class="btn btn-start" onclick="startTracking()">
        🚀 GPS ट्रैकिंग शुरू करें
    </button>

    <button id="stopBtn" class="btn btn-stop" onclick="stopTracking()" disabled>
        🛑 ट्रैकिंग बंद करें
    </button>

    <div id="log"></div>
</div>

<script>
const BUS_ID = {sid};
const API_BASE = window.location.origin;

// ग्लोबल वेरिएबल्स
let watchId = null;
let isTracking = false;
let wakeLock = null;
let gpsQueue = [];
let lastSentTime = 0;
const SEND_INTERVAL = 5000; // हर 5 सेकंड में भेजें
const BACKUP_INTERVAL = 10000; // बैकअप हर 10 सेकंड

// Service Worker रजिस्टर करें
if ('serviceWorker' in navigator) {{
    navigator.serviceWorker.register('/sw.js')
        .then(reg => console.log('SW registered:', reg))
        .catch(err => console.log('SW registration failed:', err));
}}

// Wake Lock API - स्क्रीन को चालू रखें
async function requestWakeLock() {{
    try {{
        if ('wakeLock' in navigator) {{
            wakeLock = await navigator.wakeLock.request('screen');
            document.getElementById('wakelockIndicator').classList.add('wakelock-active');
            log('स्क्रीन लॉक सक्रिय');

            wakeLock.addEventListener('release', () => {{
                log('स्क्रीन लॉक रिलीज़ हो गया');
                document.getElementById('wakelockIndicator').classList.remove('wakelock-active');
            }});
        }}
    }} catch (err) {{
        log('Wake Lock त्रुटि: ' + err.message);
    }}
}}

function releaseWakeLock() {{
    if (wakeLock) {{
        wakeLock.release();
        wakeLock = null;
    }}
}}

// Visibility API - बैकग्राउंड में भी काम करें
document.addEventListener('visibilitychange', () => {{
    if (document.hidden && isTracking) {{
        log('बैकग्राउंड मोड सक्रिय');
        // बैकग्राउंड में भी GPS जारी रखें
        if (watchId === null) {{
            restartGPS();
        }}
    }} else {{
        log('फोरग्राउंड मोड');
    }}
}});

function restartGPS() {{
    if (watchId !== null) {{
        navigator.geolocation.clearWatch(watchId);
    }}
    watchId = navigator.geolocation.watchPosition(
        handlePosition,
        handleError,
        {{
            enableHighAccuracy: true,
            timeout: 20000,
            maximumAge: 0,
            distanceFilter: 10
        }}
    );
}}

function log(msg) {{
    const logDiv = document.getElementById('log');
    const time = new Date().toLocaleTimeString('hi-IN');
    logDiv.innerHTML = `[${{time}}] ${{msg}}<br>` + logDiv.innerHTML;
    console.log(msg);
}}

function updateUI(lat, lng, speed, accuracy) {{
    document.getElementById('lat').textContent = lat.toFixed(5);
    document.getElementById('lng').textContent = lng.toFixed(5);
    document.getElementById('speed').textContent = speed ? speed.toFixed(1) : '0';
    document.getElementById('accuracy').textContent = accuracy ? accuracy.toFixed(0) : '--';
}}

function setStatus(active, text, subtext) {{
    const indicator = document.getElementById('gpsIndicator');
    const statusText = document.getElementById('statusText');
    const subStatus = document.getElementById('subStatus');

    if (active) {{
        indicator.className = 'gps-indicator gps-active';
        statusText.textContent = text || 'LIVE ट्रैकिंग';
        statusText.style.color = '#28a745';
    }} else {{
        indicator.className = 'gps-indicator gps-inactive';
        statusText.textContent = text || 'GPS बंद है';
        statusText.style.color = '#dc3545';
    }}
    if (subtext) subStatus.textContent = subtext;
}}

// GPS पोजीशन हैंडलर
function handlePosition(position) {{
    const lat = position.coords.latitude;
    const lng = position.coords.longitude;
    const speed = position.coords.speed ? position.coords.speed * 3.6 : 0; // m/s to km/h
    const accuracy = position.coords.accuracy;
    const timestamp = new Date().toISOString();

    updateUI(lat, lng, speed, accuracy);

    const data = {{
        sid: BUS_ID,
        lat: lat,
        lng: lng,
        speed: speed,
        accuracy: accuracy,
        timestamp: timestamp
    }};

    // क्यू में डालें
    gpsQueue.push(data);

    // तुरंत भेजने की कोशिश
    sendGPSData();
}}

function handleError(error) {{
    let msg = '';
    switch(error.code) {{
        case error.PERMISSION_DENIED:
            msg = "GPS अनुमति अस्वीकृत";
            break;
        case error.POSITION_UNAVAILABLE:
            msg = "लोकेशन उपलब्ध नहीं";
            break;
        case error.TIMEOUT:
            msg = "GPS टाइमआउट";
            break;
        default:
            msg = "GPS त्रुटि: " + error.message;
    }}
    log(msg);
    setStatus(false, 'त्रुटि', msg);
}}

// GPS डेटा भेजें (ऑनलाइन/ऑफलाइन दोनों मोड में)
async function sendGPSData() {{
    if (gpsQueue.length === 0) return;

    const now = Date.now();
    if (now - lastSentTime < SEND_INTERVAL && navigator.onLine) return;

    const dataToSend = [...gpsQueue];
    gpsQueue = [];

    // Service Worker को मैसेज भेजें (बैकग्राउंड के लिए)
    if ('serviceWorker' in navigator && navigator.serviceWorker.controller) {{
        navigator.serviceWorker.controller.postMessage({{
            type: 'GPS_DATA',
            data: dataToSend,
            apiBase: API_BASE
        }});
    }}

    // मुख्य थ्रेड में भी भेजें (तुरंत अपडेट के लिए)
    if (navigator.onLine) {{
        try {{
            const response = await fetch(API_BASE + '/api/gps-batch', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{locations: dataToSend}})
            }});

            if (response.ok) {{
                lastSentTime = now;
                document.getElementById('queueStatus').textContent = 
                    `✅ ${{dataToSend.length}} लोकेशन सिंक हुए`;
                log('डेटा सिंक सफल');
            }} else {{
                throw new Error('Server error');
            }}
        }} catch (err) {{
            // फेल होने पर वापस क्यू में डालें
            gpsQueue.unshift(...dataToSend);
            document.getElementById('queueStatus').textContent = 
                `⏳ ${{gpsQueue.length}} लोकेशन क्यू में`;
            log('सिंक फेल, क्यू में सेव किया');
        }}
    }} else {{
        gpsQueue.unshift(...dataToSend);
        document.getElementById('queueStatus').textContent = 
            `📴 ऑफलाइन - ${{gpsQueue.length}} सेव`;
        log('ऑफलाइन मोड - डेटा सेव किया');
    }}
}}

// ट्रैकिंग शुरू करें
async function startTracking() {{
    if (!navigator.geolocation) {{
        alert('इस डिवाइस में GPS सपोर्ट नहीं है');
        return;
    }}

    isTracking = true;
    document.getElementById('startBtn').disabled = true;
    document.getElementById('stopBtn').disabled = false;

    // Wake Lock लें
    await requestWakeLock();

    // GPS शुरू करें
    restartGPS();

    // बैकग्राउंड सिंक इंटरवल
    window.syncInterval = setInterval(() => {{
        if (gpsQueue.length > 0) {{
            sendGPSData();
        }}
    }}, BACKUP_INTERVAL);

    setStatus(true, 'LIVE ट्रैकिंग', 'GPS सक्रिय');
    log('ट्रैकिंग शुरू हुई');

    // ब्राउज़र नोटिफिकेशन
    if ('Notification' in window && Notification.permission === 'granted') {{
        new Notification('GPS ट्रैकिंग शुरू', {{
            body: 'बस ' + BUS_ID + ' की ट्रैकिंग चालू है',
            icon: '🚌'
        }});
    }}
}}

// ट्रैकिंग बंद करें
function stopTracking() {{
    isTracking = false;

    if (watchId !== null) {{
        navigator.geolocation.clearWatch(watchId);
        watchId = null;
    }}

    if (window.syncInterval) {{
        clearInterval(window.syncInterval);
    }}

    releaseWakeLock();

    // बचा हुआ डेटा भेजें
    if (gpsQueue.length > 0) {{
        sendGPSData();
    }}

    document.getElementById('startBtn').disabled = false;
    document.getElementById('stopBtn').disabled = true;

    setStatus(false, 'GPS बंद', 'ट्रैकिंग रोक दी गई');
    log('ट्रैकिंग बंद हुई');
}}

// पेज लोड पर नोटिफिकेशन अनुमति लें
if ('Notification' in window && Notification.permission === 'default') {{
    Notification.requestPermission();
}}

// पेज अनलोड से पहले बचा हुआ डेटा भेजें
window.addEventListener('beforeunload', (e) => {{
    if (isTracking && gpsQueue.length > 0) {{
        sendGPSData();
    }}
}});

// ऑनलाइन/ऑफलाइन हैंडलर
window.addEventListener('online', () => {{
    log('इंटरनेट वापस आ गया');
    if (gpsQueue.length > 0) sendGPSData();
}});

window.addEventListener('offline', () => {{
    log('इंटरनेट गया');
}});

// Service Worker से मैसेज सुनें
navigator.serviceWorker?.addEventListener('message', (event) => {{
    if (event.data.type === 'SYNC_COMPLETE') {{
        log('बैकग्राउंड सिंक पूरा');
    }}
}});
</script>
</body>
</html>
"""


@app.route("/manifest.json")
def manifest():
    """PWA Manifest - होम स्क्रीन पर ऐप की तरह इंस्टॉल करें"""
    bus_id = request.args.get('id', '1')
    return jsonify({
        "name": f"बस ट्रैकर {bus_id}",
        "short_name": f"बस {bus_id}",
        "start_url": f"/driver/{bus_id}",
        "display": "standalone",
        "background_color": "#28a745",
        "theme_color": "#28a745",
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
    """Service Worker - बैकग्राउंड में GPS डेटा सिंक करें"""
    return """
self.addEventListener('install', (event) => {
    console.log('Service Worker installing...');
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    console.log('Service Worker activating...');
    event.waitUntil(clients.claim());
});

// GPS डेटा क्यू
let gpsQueue = [];
let isSyncing = false;

self.addEventListener('message', (event) => {
    if (event.data.type === 'GPS_DATA') {
        const data = event.data.data;
        const apiBase = event.data.apiBase;

        // क्यू में जोड़ें
        gpsQueue.push(...data);

        // तुरंत सिंक करें
        syncGPSData(apiBase);
    }
});

// बैकग्राउंड सिंक
async function syncGPSData(apiBase) {
    if (isSyncing || gpsQueue.length === 0) return;

    isSyncing = true;
    const dataToSend = [...gpsQueue];
    gpsQueue = [];

    try {
        const response = await fetch(apiBase + '/api/gps-batch', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({locations: dataToSend})
        });

        if (response.ok) {
            // क्लाइंट को बताएं
            const clients = await self.clients.matchAll();
            clients.forEach(client => {
                client.postMessage({
                    type: 'SYNC_COMPLETE',
                    count: dataToSend.length
                });
            });
        } else {
            throw new Error('Server error');
        }
    } catch (err) {
        // फेल होने पर वापस क्यू में डालें
        gpsQueue.unshift(...dataToSend);
        console.log('Sync failed, queued:', gpsQueue.length);
    } finally {
        isSyncing = false;

        // अगर और डेटा है तो फिर से कोशिश करें
        if (gpsQueue.length > 0) {
            setTimeout(() => syncGPSData(apiBase), 5000);
        }
    }
}

// Periodic Background Sync (अगर सपोर्टेड हो)
self.addEventListener('periodicsync', (event) => {
    if (event.tag === 'gps-sync') {
        event.waitUntil(syncGPSData(self.location.origin));
    }
});

// Fetch इंटरसेप्ट - ऑफलाइन सपोर्ट
self.addEventListener('fetch', (event) => {
    // GPS API कॉल्स को नेटवर्क-ओनली रखें
    if (event.request.url.includes('/api/gps')) {
        event.respondWith(fetch(event.request).catch(() => {
            return new Response(JSON.stringify({queued: true}), {
                headers: {'Content-Type': 'application/json'}
            });
        }));
    }
});
""", 200, {'Content-Type': 'application/javascript'}


@app.route("/api/gps-batch", methods=["POST"])
@safe_db
def gps_batch():
    """बैच में GPS डेटा रिसीव करें - बैकग्राउंड सिंक के लिए"""
    data = request.get_json()
    locations = data.get('locations', [])

    if not locations:
        return jsonify({"ok": False, "error": "No data"}), 400

    success_count = 0

    with get_db() as (conn, cur):
        for loc in locations:
            try:
                sid = loc.get('sid')
                lat = float(loc.get('lat', 0))
                lng = float(loc.get('lng', 0))
                speed = float(loc.get('speed', 0))
                accuracy = float(loc.get('accuracy', 0))

                # मुख्य टेबल अपडेट
                cur.execute("""
                    UPDATE schedules 
                    SET current_lat=%s, current_lng=%s, last_gps_update=NOW() 
                    WHERE id=%s
                """, (lat, lng, sid))

                # GPS लॉग सेव करें
                cur.execute("""
                    INSERT INTO gps_logs (schedule_id, latitude, longitude, speed, accuracy)
                    VALUES (%s, %s, %s, %s, %s)
                """, (sid, lat, lng, speed, accuracy))

                # SocketIO पर ब्रॉडकास्ट
                socketio.emit("bus_location", {
                    "sid": sid,
                    "lat": lat,
                    "lng": lng,
                    "speed": speed,
                    "timestamp": loc.get('timestamp', datetime.now().isoformat())
                })

                success_count += 1
            except Exception as e:
                print(f"Error processing GPS data: {e}")
                continue

        conn.commit()

    return jsonify({
        "ok": True,
        "processed": success_count,
        "total": len(locations)
    })


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

        # GPS अपडेट का समय
        last_update = bus.get('last_gps_update')
        is_live = False
        if last_update:
            from datetime import datetime
            time_diff = (datetime.now() - last_update).total_seconds()
            is_live = time_diff < 300  # 5 मिनट से कerma पुराना न हो

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
    .offline-indicator{{width:20px;height:20px;background:#dc3545;border-radius:50%;display:inline-block;margin-right:10px;}}
    @keyframes pulse{{0%,100%{{transform:scale(1);opacity:1;}}50%{{transform:scale(1.2);opacity:0.7;}}}}
    .stats-card{{background:rgba(255,255,255,0.95);backdrop-filter:blur(20px);padding:15px;border-radius:15px;margin-bottom:20px;}}
    .last-update{{font-size:12px;color:#666;}}
    </style>

    <div class="stats-card">
        <div class="d-flex justify-content-between align-items-center">
            <div>
                <h2 class="mb-1">🚌 {bus['bus_name']}</h2>
                <p class="text-muted mb-0">{bus['route_name']} ({bus['distance_km']}किमी)</p>
            </div>
            <div class="text-end">
                <span class="{'live-indicator' if is_live else 'offline-indicator'}"></span>
                <span class="fw-bold {'text-success' if is_live else 'text-danger'}">
                    {'🟢 LIVE' if is_live else '⚪ ऑफलाइन'}
                </span>
                <div class="last-update mt-1">
                    अंतिम अपडेट: {last_update.strftime('%H:%M:%S') if last_update else 'कभी नहीं'}
                </div>
            </div>
        </div>
    </div>

    <div id="map" class="rounded-4"></div>

    <div class="alert alert-info mt-3" id="connectionStatus">
        📡 कनेक्टिंग...
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

    if(routePoints.length > 1){{
        L.polyline(routePoints, {{
            color: 'blue',
            weight: 8,
            opacity: 0.9
        }}).addTo(map);
        map.fitBounds(L.polyline(routePoints).getBounds());
    }}

    const busIcon = L.divIcon({{
        html: '<div style="animation:pulse 2s infinite;width:30px;height:30px;background:#28a745;border-radius:50%;border:3px solid #fff;box-shadow:0 0 20px #28a745;display:flex;align-items:center;justify-content:center;">🚌</div>',
        className: 'bus-icon',
        iconSize: [30,30]
    }});

    let busMarker = L.marker([{lat},{lng}], {{icon: busIcon}}).addTo(map);
    const sid = {sid};
    const socket = io(window.location.origin);
    const statusDiv = document.getElementById('connectionStatus');

    socket.on('connect', () => {{
        statusDiv.className = 'alert alert-success mt-3';
        statusDiv.innerHTML = '✅ लाइव कनेक्टेड - रीयल-टाइम अपडेट';
    }});

    socket.on('disconnect', () => {{
        statusDiv.className = 'alert alert-warning mt-3';
        statusDiv.innerHTML = '⚠️ डिस्कनेक्टेड - पुनः कनेक्ट हो रहा है...';
    }});

    socket.on('bus_location', data => {{
        if(data.sid == sid){{
            const lat = parseFloat(data.lat);
            const lng = parseFloat(data.lng);
            busMarker.setLatLng([lat,lng]);
            map.panTo([lat,lng], {{animate:true}});

            // स्पीड अपडेट
            if(data.speed) {{
                statusDiv.innerHTML = `🚌 गति: ${{data.speed.toFixed(1)}} km/h | अपडेट: ${{new Date().toLocaleTimeString('hi-IN')}}`;
            }}
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
    print("🚀 बस बुकिंग ऐप शुरू हो रहा है... (बैकग्राउंड GPS सहित)")
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=True)