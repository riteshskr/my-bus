from dotenv import load_dotenv
load_dotenv()
import setuptools
import os, random
from datetime import date
from functools import wraps
from flask import Flask, request, jsonify, render_template_string, redirect, g,session
from flask_socketio import SocketIO, emit
from flask_compress import Compress
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
import atexit
import razorpay

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
    return g.db_conn, g.db_conn.cursor(row_factory=dict_row)


@app.teardown_appcontext
def close_db(error=None):
    conn = g.pop('db_conn', None)
    if conn:
        pool.putconn(conn)


def safe_db(func):
    @wraps(func)
    def wrapper(*a, **kw):
        try:
            return func(*a, **kw)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    return wrapper

def admin_required(f):
    def wrap(*a,**k):
        if "admin" not in session:
            return redirect("/admin/login")
        return f(*a,**k)
    wrap.__name__ = f.__name__
    return wrap

# ================= DB INIT =================
def init_db():
    try:
        conn = pool.getconn()
        cur = conn.cursor()

        # ===== TABLES =====
        cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            id SERIAL PRIMARY KEY,
            username VARCHAR(50) UNIQUE,
            password VARCHAR(100),
            role VARCHAR(20) DEFAULT 'admin'
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
        pool.putconn(conn)

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
<title>🚌 SmartBus India – Live Booking System</title>

<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">

<style>
body{
 background: linear-gradient(120deg,#ff3d00,#ff9800);
 min-height:100vh;
 font-family: 'Segoe UI',sans-serif;
}

/* Glass Container */
.main-container{
 background: rgba(255,255,255,0.97);
 border-radius:25px;
 box-shadow:0 25px 50px rgba(0,0,0,.25);
 margin:20px auto;
 padding:30px;
 max-width:1200px;
}

/* Navbar */
.topbar{
 display:flex;
 justify-content:space-between;
 align-items:center;
 margin-bottom:20px;
}
.logo{
 font-size:28px;
 font-weight:700;
 color:#ff3d00;
}
.topbar a{
 margin-left:20px;
 text-decoration:none;
 font-weight:600;
 color:#333;
}

/* Hero */
.hero{
 background:linear-gradient(120deg,#ff3d00,#ff9800);
 color:white;
 padding:30px;
 border-radius:20px;
 text-align:center;
 margin-bottom:25px;
 box-shadow:0 15px 30px rgba(0,0,0,.2);
}

/* Cards */
.route-card,.bus-card{
 border-radius:20px;
 border:none;
 transition:.3s;
 cursor:pointer;
}
.route-card:hover,.bus-card:hover{
 transform:translateY(-6px);
 box-shadow:0 20px 40px rgba(0,0,0,.2);
}

/* Seat */
.seat{
 width:50px;height:50px;
 margin:5px;
 border-radius:12px;
 font-weight:bold;
}

/* Map */
#map{
 height:350px;
 border-radius:20px;
 box-shadow:0 10px 30px rgba(0,0,0,.2);
}

/* Bottom nav */
.nav-buttons{
 text-align:center;
 margin-top:40px;
}
.btn-custom{
 border-radius:25px;
 padding:12px 28px;
 font-weight:600;
}
</style>
</head>

<body>
<div class="container-fluid py-4">
 <div class="main-container">

  <!-- Navbar -->
  <div class="topbar">
    <div class="logo">🚌 SmartBus</div>
    <div>
      <a href="/">Home</a>
      <a href="/bookings">My Bookings</a>
      <a href="/offers">Offers</a>
      <a href="/login">Login</a>
    </div>
  </div>

  <!-- Hero -->
  <div class="hero">
    <h2>Online Bus Booking System</h2>
    <p>Live GPS • Real-time Seats • Secure Payments</p>
  </div>

  <!-- Dynamic Page Content -->
  {{content|safe}}

  <!-- Bottom Navigation -->
  <div class="nav-buttons">
    <a href="/" class="btn btn-light btn-lg btn-custom me-3">
      🏠 Home
    </a>
    <a href="/driver/1" class="btn btn-success btn-lg btn-custom me-3">
      📱 Driver GPS
    </a>
    <a href="/live-bus/1" class="btn btn-primary btn-lg btn-custom">
      🗺️ Live Track
    </a>
  </div>

 </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
</body>
</html>
"""

# Home Page content in Hindi (inputs and buttons)
HOME_HTML = """
<div class="row g-3">
  <div class="col-md-4">
    <select class="form-select" id="from">
      <option selected disabled>कहाँ से</option>
      <option>दिल्ली</option>
      <option>मुंबई</option>
      <option>बेंगलुरु</option>
      <option>जयपुर</option>
    </select>
  </div>

  <div class="col-md-4">
    <select class="form-select" id="to">
      <option selected disabled>कहाँ तक</option>
      <option>जयपुर</option>
      <option>पुणे</option>
      <option>चेन्नई</option>
      <option>हैदराबाद</option>
    </select>
  </div>

  <div class="col-md-3">
    <input type="date" class="form-control" id="date">
  </div>

  <div class="col-md-1 d-grid">
    <button class="btn btn-danger" onclick="searchBus()">
      खोजें
    </button>
  </div>
</div>

<script>
function searchBus(){
  let f = document.getElementById("from").value;
  let t = document.getElementById("to").value;
  let d = document.getElementById("date").value;

  if(!f || !t || !d){
    alert("कृपया सभी फ़ील्ड भरें");
    return;
  }

  alert("बस खोजी जा रही है: " + f + " से " + t);
}

function selectRoute(rid){
    // Bus list page पर redirect
    window.location.href = "/buses/" + rid;
}

</script>
"""


# ================= ROUTES =================
@app.route("/")
@safe_db
def home():
    conn, cur = get_db()

    # सभी Routes (बड़े cards)
    cur.execute("SELECT id, route_name, distance_km FROM routes ORDER BY id")
    routes = cur.fetchall()

    # Hero Section
    hero_section = '''
    <div class="text-center p-5 bg-gradient-primary text-blue rounded-4 shadow-lg mx-auto mb-5" style="max-width:800px;">
        <h1 class="display-4 fw-bold mb-4">🚌 Bus Booking India</h1>
        <p class="lead mb-5">Live GPS Tracking + Real-time Seat Booking</p>
        <h4 class="mb-4">📍 सबसे पहले अपना Route चुनें:</h4>
    </div>
    '''

    # 🔥 Route Selection Cards (बड़ा + Clear)
    routes_section = '<div class="row g-4 mb-5">'
    for r in routes:
        routes_section += f'''
        <div class="col-md-6 col-lg-4">
            <div class="card h-100 bg-info text-white shadow-lg border-0 hover-scale" style="border-radius:20px;cursor:pointer;">
                <div class="card-body p-5 text-center" onclick="selectRoute({r['id']})">
                    <h3 class="fw-bold mb-3">{r['route_name']}</h3>
                    <div class="display-4 text-warning mb-4">🛣️ {r['distance_km']} km</div>
                    <div class="h5 mb-3">⚡ Live GPS Tracking</div>
                    <button class="btn btn-success btn-lg px-5">
                        🚀 Buses देखें → Bus {r['id']}
                    </button>
                </div>
            </div>
        </div>'''
    routes_section += '</div>'

    # Live GPS Status (नीचे छोटा)
    cur.execute("""
        SELECT s.id, s.bus_name, r.route_name, 
               s.current_lat as lat, s.current_lng as lng
        FROM schedules s JOIN routes r ON s.route_id = r.id
        ORDER BY s.id LIMIT 4
    """)
    live_buses = cur.fetchall()

    live_section = '<h3 class="text-center mb-4">🟢 Live Running Buses</h3><div class="row g-4">'
    for bus in live_buses:
        status = "🟢 LIVE GPS" if bus.get('lat') else "⚪ Ready"
        coords = f'{float(bus["lat"]):.4f}, {float(bus["lng"]):.4f}' if bus.get('lat') else '---'
        live_section += f'''
        <div class="col-md-6 col-lg-3">
            <div class="card border-0 shadow">
                <div class="card-body text-center p-3">
                    <h6 class="fw-bold">{bus['bus_name']}</h6>
                    <small class="text-muted">{bus['route_name']}</small><br>
                    <span class="badge {'bg-success' if bus.get('lat') else 'bg-secondary'}">{status}</span>
                    <div class="mt-2"><small>📍 {coords}</small></div>
                </div>
            </div>
        </div>'''
    live_section += '</div>'

    content = hero_section + routes_section + live_section
    return render_template_string(BASE_HTML, content=content)


@app.route("/buses/<int:rid>")
@safe_db
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

    html = f"""
    <div class="text-center mb-5 booking-header">
        <h2 class="display-4 fw-bold">🚌 {route['route_name']}</h2>
        <div class="h5 text-white-50">
            📍 {route['stations']} | 🛣️ {route['distance_km']} km
        </div>
        <p class="lead">आज की सभी बसें</p>
    </div>
    """

    if not buses_data:
        html += "<div class='alert alert-warning text-center'>आज कोई बस नहीं है</div>"
    else:
        for bus in buses_data:
            dep_time = bus['departure_time'].strftime('%H:%M')
            seats_left = bus['total_seats'] - bus['booked_count']
            gps_status = "🟢 LIVE" if bus.get('current_lat') else "⚪ Offline"
            badge = "bg-success" if bus.get('current_lat') else "bg-secondary"

            html += f"""
            <div class="row mb-4">
                <div class="col-lg-8 mx-auto">
                    <div class="card shadow-lg border-0 bus-card">
                        <div class="card-body p-4 text-center">

                            <span class="badge {badge} float-end">
                                {gps_status}
                            </span>

                            <h3 class="fw-bold">{bus['bus_name']}</h3>
                            <h4 class="text-primary">
                                ⏰ {dep_time}
                            </h4>

                            <div class="row mt-3">
                                <div class="col">
                                    <div class="fw-bold text-success">
                                        Seats Left
                                    </div>
                                    <div class="fs-4">
                                        {seats_left}
                                    </div>
                                </div>
                                <div class="col">
                                    <div class="fw-bold text-info">
                                        Total Seats
                                    </div>
                                    <div class="fs-4">
                                        {bus['total_seats']}
                                    </div>
                                </div>
                            </div>

                            <div class="d-grid gap-2 d-md-flex mt-4">
                                <a href="/live-bus/{bus['id']}" 
                                   class="btn btn-primary btn-lg flex-fill">
                                    🗺️ Live GPS
                                </a>
                                <a href="/select/{bus['id']}" 
                                   class="btn btn-success btn-lg flex-fill">
                                    🎫 Book Seat
                                </a>
                            </div>

                        </div>
                    </div>
                </div>
            </div>
            """

    html += """
    <div class="text-center mt-5">
        <a href="/" class="btn btn-outline-light btn-lg">
            ← Back to Routes
        </a>
    </div>
    """

    return render_template_string(BASE_HTML, content=html)


@app.route("/select/<int:sid>", methods=["GET", "POST"])
@safe_db
def select(sid):
    conn, cur = get_db()
    cur.execute("SELECT route_id FROM schedules WHERE id=%s", (sid,))
    row = cur.fetchone()
    route_id = row["route_id"] if row else 1

    cur.execute("SELECT station_name FROM route_stations WHERE route_id=%s ORDER BY station_order", (route_id,))
    stations = [r["station_name"] for r in cur.fetchall()]

    opts = "".join(f"<option>{s}</option>" for s in stations)
    today = date.today().isoformat()

    if request.method == "POST":
        fs = request.form["from"]
        ts = request.form["to"]
        d = request.form["date"]
        return redirect(f"/seats/{sid}?fs={fs}&ts={ts}&d={d}")

    form = f'''
    <div class="card mx-auto" style="max-width:500px">
        <div class="card-body">
            <h5 class="card-title text-center">🎫 Journey Details</h5>
            <form method="POST">
                <div class="mb-3">
                    <label class="form-label">From:</label>
                    <select name="from" class="form-select" required>{opts}</select>
                </div>
                <div class="mb-3">
                    <label class="form-label">To:</label>
                    <select name="to" class="form-select" required>{opts}</select>
                </div>
                <div class="mb-3">
                    <label class="form-label">Date:</label>
                    <input type="date" name="date" class="form-control" value="{today}" min="{today}" required>
                </div>
                <button class="btn btn-success w-100">View Available Seats</button>
            </form>
        </div>
    </div>'''
    return render_template_string(BASE_HTML, content=form)


@app.route("/seats/<int:sid>")
@safe_db
def seats(sid):
    fs = request.args.get("fs", "बीकानेर")
    ts = request.args.get("ts", "जयपुर")
    d = request.args.get("d", date.today().isoformat())

    conn, cur = get_db()

    # ===== STATION ORDER =====
    cur.execute("""
        SELECT station_name, station_order
        FROM route_stations
        WHERE route_id = (SELECT route_id FROM schedules WHERE id=%s)
        ORDER BY station_order
    """, (sid,))
    stations_data = cur.fetchall()

    station_to_order = {r['station_name']: r['station_order'] for r in stations_data}
    fs_order = station_to_order.get(fs, 1)
    ts_order = station_to_order.get(ts, 2)

    # ===== BOOKED SEATS =====
    cur.execute("""
        SELECT seat_number, from_station, to_station
        FROM seat_bookings
        WHERE schedule_id=%s
        AND travel_date=%s
        AND status='confirmed'
    """, (sid, d))

    booked_rows = cur.fetchall()
    booked_seats = set()

    for row in booked_rows:
        b_fs = station_to_order.get(row['from_station'], 0)
        b_ts = station_to_order.get(row['to_station'], 0)

        if not (ts_order <= b_fs or fs_order >= b_ts):
            booked_seats.add(row['seat_number'])

    # ===== SEAT BUTTONS =====
    seat_buttons = ""
    available_count = 40 - len(booked_seats)

    for i in range(1, 41):
        if i in booked_seats:
            seat_buttons += '<button class="btn btn-danger seat" disabled>X</button>'
        else:
            seat_buttons += f'<button class="btn btn-success seat" onclick="bookSeat({i}, this)">{i}</button>'

    # ===== BUS LOCATION =====
    cur.execute("SELECT current_lat, current_lng, route_id FROM schedules WHERE id=%s", (sid,))
    bus = cur.fetchone()

    lat = float(bus['current_lat'] or 27.2)
    lng = float(bus['current_lng'] or 75.0)

    # ===== ROUTE STATIONS FOR MAP =====
    cur.execute("""
        SELECT lat, lng, station_name
        FROM route_stations
        WHERE route_id=%s
        ORDER BY station_order
    """, (bus['route_id'],))

    stations = cur.fetchall()
    import json
    stations_json = json.dumps(stations, ensure_ascii=False)

    # ================= HTML =================
    html = f"""
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>

<style>
#seat-map{{height:260px;border-radius:20px;margin-bottom:20px;}}
.seat{{width:55px;height:55px;margin:4px;font-weight:bold;border-radius:12px;font-size:14px;}}
.btn-success{{background:#28a745 !important;}}
.btn-danger{{background:#dc3545 !important;}}

.bus-icon{{
   width:30px !important;
   height:30px !important;
   background:url('https://cdn-icons-png.flaticon.com/512/1048/1048313.png');
   background-size:contain;
   background-repeat:no-repeat;
   filter: drop-shadow(0 0 6px rgba(0,0,0,0.5));
}}
</style>

<div class="text-center mb-3">
  <h3>🚌 {fs} → {ts}</h3>
  <h5>📅 {d}</h5>
  Available: <span class="badge bg-success">{available_count}</span>
</div>

<div id="seat-map"></div>

<div class="text-center">
  {seat_buttons}
</div>

<script>
const sid = {sid};

// ===== MAP =====
const map = L.map('seat-map').setView([{lat}, {lng}], 9);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png').addTo(map);

const stations = {stations_json};
let routePoints = [];

stations.forEach(st => {{
    let la = parseFloat(st.lat);
    let ln = parseFloat(st.lng);
    if(!isNaN(la) && !isNaN(ln)){{
        routePoints.push([la, ln]);
        L.marker([la, ln]).addTo(map).bindPopup(st.station_name);
    }}
}});

if(routePoints.length >= 2){{
    let poly = L.polyline(routePoints,{{color:'blue',weight:6}}).addTo(map);
    map.fitBounds(poly.getBounds());
}}

// ===== BUS ICON =====
let busIcon = L.divIcon({{className:'bus-icon'}});
let busMarker = L.marker([{lat},{lng}],{{icon:busIcon}}).addTo(map);

// ===== SOCKET =====
const socket = io();

socket.on("bus_location", d => {{
   if(d.sid == sid){{
       busMarker.setLatLng([d.lat, d.lng]);
   }}
}});

socket.on("seat_update", d => {{
   if(d.sid == sid){{
       markSeatBooked(d.seat);
   }}
}});

// ===== HELPER =====
function markSeatBooked(seat){{
    const btns = document.querySelectorAll(".seat");
    const btn = btns[seat-1];
    if(btn){{
        btn.classList.remove("btn-success");
        btn.classList.add("btn-danger");
        btn.innerText = "X";
        btn.disabled = true;
    }}
}}

// ===== BOOK SEAT =====
async function bookSeat(seat, btn){{
    let name = prompt("Passenger Name");
    if(!name) return;

    let mobile = prompt("Mobile Number");
    if(!mobile) return;

    let payload = {{
        sid: sid,
        seat: seat,
        name: name,
        mobile: mobile,
        date: "{d}",
        from: "{fs}",
        to: "{ts}",
        payment_mode: "cash",
        booked_by_type: "user",
        booked_by_id: 1
    }};

    let res = await fetch("/book", {{
        method:"POST",
        headers:{{"Content-Type":"application/json"}},
        body: JSON.stringify(payload)
    }});

    let data = await res.json();

    if(data.ok){{
        markSeatBooked(seat);
        alert("Seat Booked Successfully ✅");
    }} 
    else {{
        alert(data.error);
    }}
}}
</script>
"""
    return render_template_string(BASE_HTML, content=html)

@app.route("/book", methods=["POST"])
@safe_db
def book():
    data = request.get_json()

    # ===== Required fields =====
    required = [
        'sid', 'seat', 'name', 'mobile', 'date',
        'from', 'to', 'payment_mode',
        'booked_by_type', 'booked_by_id'
    ]

    for field in required:
        if field not in data or str(data[field]).strip() == "":
            return jsonify({"ok": False, "error": f"Missing field: {field}"})

    conn, cur = get_db()

    try:
        # ===== Check if seat already booked =====
        cur.execute("""
            SELECT id FROM seat_bookings
            WHERE schedule_id=%s 
            AND seat_number=%s 
            AND travel_date=%s
            AND status='confirmed'
        """, (data['sid'], data['seat'], data['date']))

        if cur.fetchone():
            return jsonify({"ok": False, "error": "Seat already booked"}), 409

        # ===== Temporary Fare =====
        fare = random.randint(250, 450)

        # 👉 RAZORPAY IGNORE → ALWAYS CASH
        status = "confirmed"
        payment_mode = "cash"

        # ===== INSERT BOOKING =====
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
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            data['sid'],
            data['seat'],
            data['name'],
            data['mobile'],
            data['from'],
            data['to'],
            data['date'],
            fare,
            status,
            payment_mode,
            data['booked_by_type'],
            data['booked_by_id'],
            data.get('counter_id')  # optional
        ))

        conn.commit()

        # ===== LIVE UPDATE =====
        socketio.emit("seat_update", {
            "sid": data['sid'],
            "seat": data['seat']
        })

        return jsonify({
            "ok": True,
            "fare": fare,
            "message": "Seat booked successfully (CASH MODE)"
        })

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


if __name__ == "__main__":
    print("🚀 Bus Booking App Starting... (Live Updates 100% Working)")
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=True)
