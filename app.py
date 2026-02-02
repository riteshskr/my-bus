from dotenv import load_dotenv
import os
import random
import atexit
import time
from datetime import date
from functools import wraps
from flask import Flask, request, jsonify, render_template_string, redirect, g, session
from flask_socketio import SocketIO, emit
from flask_compress import Compress
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
import razorpay

# ===== CONFIG & IMPORTS =====
load_dotenv()

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

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key-change-me")
Compress(app)

# SocketIO (Eventlet mode for production)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet",
                    logger=False, engineio_logger=False, ping_timeout=60)

# ================= DATABASE =================
DATABASE_URL = os.getenv("DATABASE_URL")
pool = None


@atexit.register
def shutdown_pool():
    if pool:
        pool.close()


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
    global pool
    if not DATABASE_URL:
        print("⚠️ DATABASE_URL missing.")
        return

    try:
        print("🔄 Connecting to DB...")
        # Delay for Render DB startup
        time.sleep(2)

        pool = ConnectionPool(conninfo=DATABASE_URL, min_size=2, max_size=10, timeout=20)
        print("✅ Pool Created")

        conn = pool.getconn()
        cur = conn.cursor()

        # Create Tables
        cur.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                id SERIAL PRIMARY KEY,
                username VARCHAR(50) UNIQUE,
                password VARCHAR(100),
                role VARCHAR(20) DEFAULT 'admin',
                counter_no INTEGER DEFAULT 0
            )
        """)
        cur.execute("SELECT COUNT(*) FROM admins")
        if cur.fetchone()[0] == 0:
            cur.execute("INSERT INTO admins (username, password, role) VALUES ('admin', 'admin123', 'admin')")

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
            CREATE TABLE IF NOT EXISTS route_stations (
                id SERIAL PRIMARY KEY, 
                route_id INT REFERENCES routes(id), 
                station_name VARCHAR(50), 
                station_order INT,
                lat DOUBLE PRECISION DEFAULT 27.2,
                lng DOUBLE PRECISION DEFAULT 75.2
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
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # Default Data
        cur.execute("SELECT COUNT(*) FROM routes")
        if cur.fetchone()[0] == 0:
            routes = [(1, 'बीकानेर → जयपुर', 336), (2, 'बीकानेर → जोधपुर', 252), (3, 'जयपुर → जोधपुर', 330)]
            for r in routes:
                cur.execute("INSERT INTO routes VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", r)

            schedules = [(1, 1, 'Volvo AC Sleeper', '08:00'), (2, 1, 'Semi Sleeper AC', '10:30'),
                         (3, 2, 'Volvo AC Seater', '09:00'), (4, 3, 'Deluxe AC', '07:30')]
            for s in schedules:
                cur.execute(
                    "INSERT INTO schedules (id, route_id, bus_name, departure_time, total_seats) VALUES (%s,%s,%s,%s::time,40) ON CONFLICT DO NOTHING",
                    s)

            stations = [(1, 'बीकानेर', 1), (1, 'जयपुर', 2), (2, 'बीकानेर', 1), (2, 'जोधपुर', 2), (3, 'जयपुर', 1),
                        (3, 'जोधपुर', 2)]
            for st in stations:
                cur.execute(
                    "INSERT INTO route_stations (route_id,station_name,station_order) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    st)

        conn.commit()
        cur.close()
        pool.putconn(conn)
        print("✅ DB Init Complete!")
    except Exception as e:
        print(f"❌ DB ERROR (Non-fatal): {e}")
        import traceback
        traceback.print_exc()
        try:
            pool.putconn(conn, close=True)
        except:
            pass


# ================= HTML TEMPLATES =================
BASE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BusConnect</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=Poppins:wght@500;600;700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
:root { --primary: #4F46E5; --gradient-main: linear-gradient(135deg, #4F46E5 0%, #7C3AED 100%); --bg-body: #F3F4F6; --white: #FFFFFF; --shadow-float: 0 25px 50px -12px rgba(0, 0, 0, 0.25); }
*{margin:0;padding:0;box-sizing:border-box;}
body{font-family:'Inter', sans-serif;background-color:var(--bg-body);color:#111827;overflow-x:hidden;}
h1, h2, h3 { font-family: 'Poppins', sans-serif; }
a { text-decoration: none; color: inherit; }
.btn { display: inline-flex; align-items: center; justify-content: center; padding: 14px 32px; border-radius: 50px; font-weight: 600; cursor: pointer; transition: all 0.3s; border: none; gap: 10px; font-size: 1rem; color: white; background: var(--gradient-main); }
.btn:hover { transform: translateY(-2px); box-shadow: 0 8px 25px rgba(79, 70, 229, 0.6); }
header { position: fixed; top: 0; width: 100%; background: rgba(255,255,255,0.95); backdrop-filter: blur(12px); z-index: 2000; border-bottom: 1px solid rgba(0,0,0,0.05); }
nav { display: flex; justify-content: space-between; align-items: center; height: 80px; max-width: 1200px; margin: 0 auto; padding: 0 24px; }
.logo { font-size: 1.8rem; font-weight: 800; color: var(--primary); display: flex; align-items: center; gap: 10px; }
.nav-links { display: flex; gap: 40px; }
.nav-links a { font-weight: 500; color: #111827; position: relative; }
.hero { position: relative; height: 700px; background: linear-gradient(135deg, rgba(17, 24, 39, 0.85) 0%, rgba(79, 70, 229, 0.6) 100%), url("https://picsum.photos/seed/bus/1920/1080") center/cover fixed; display: flex; align-items: center; justify-content: center; text-align: center; color: white; padding-top: 80px; }
.booking-widget { background: rgba(255, 255, 255, 0.95); backdrop-filter: blur(20px); border-radius: 24px; padding: 2.5rem; box-shadow: var(--shadow-float); width: 100%; max-width: 1100px; margin: -80px auto 0; position: relative; z-index: 1000; border: 1px solid rgba(255,255,255,0.5); }
.booking-form { display: grid; grid-template-columns: 1.4fr 1.4fr 1fr auto; gap: 20px; align-items: end; }
.input-group label { display: block; font-size: 0.85rem; font-weight: 600; color: #6B7280; margin-bottom: 8px; text-align: left;}
.input-control { width: 100%; padding: 16px; border: 2px solid #E5E7EB; border-radius: 12px; font-size: 1rem; outline: none; transition: all 0.3s; background: #F9FAFB; }
.input-control:focus { border-color: var(--primary); background: var(--white); box-shadow: 0 0 0 4px rgba(79, 70, 229, 0.1); }
.card { background: var(--white); border-radius: 20px; overflow: hidden; box-shadow: 0 10px 30px -10px rgba(0, 0, 0, 0.1); transition: all 0.4s; position: relative; display: flex; flex-direction: column; }
.card:hover { transform: translateY(-10px); box-shadow: 0 20px 40px rgba(0, 0, 0, 0.2); }
.card-img { height: 200px; width: 100%; object-fit: cover; transition: transform 0.6s ease; }
.card:hover .card-img { transform: scale(1.1); }
.card-img-wrapper { overflow: hidden; height: 220px; }
@media(max-width:768px){ nav { flex-direction: column; height: auto; padding: 15px; } .booking-form { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <nav>
    <a href="/" class="logo"><i class="fa-solid fa-bus-simple"></i> BusConnect</a>
    <div class="nav-links">
      <a href="/">Home</a>
      <a href="/login">Login</a>
      <a href="/admin">Admin</a>
    </div>
  </nav>
</header>
<main style="min-height: 100vh;">
    {{ content|safe }}
</main>
</body>
</html>
"""

HOME_HTML = """
    <section class="hero">
      <div style="max-width: 900px; padding: 20px; z-index: 2;">
        <h1 style="font-size: 3.5rem; line-height: 1.1; margin-bottom: 1.5rem; font-weight: 800; text-shadow: 0 2px 10px rgba(0,0,0,0.2);">Travel Comfortably,<br><span style="background: -webkit-linear-gradient(to right, #4F46E5, #EC4899); -webkit-background-clip: text; -webkit-text-fill-color: transparent;">Anytime, Anywhere</span></h1>
        <p style="font-size: 1.25rem; margin-bottom: 2.5rem; opacity: 0.9;">Discover routes with premium comfort and real-time tracking.</p>
      </div>
    </section>

    <div style="padding: 0 20px;">
        <div class="booking-widget">
            <form class="booking-form" action="/search" method="POST">
                <div class="input-group">
                    <label>From</label>
                    <select name="from" class="input-control" required>
                        <option value="" disabled selected>Select Station</option>
                        {% for s in stations %}<option value="{{ s }}">{{ s }}</option>{% endfor %}
                    </select>
                </div>
                <div class="input-group">
                    <label>To</label>
                    <select name="to" class="input-control" required>
                        <option value="" disabled selected>Select Station</option>
                        {% for s in stations %}<option value="{{ s }}">{{ s }}</option>{% endfor %}
                    </select>
                </div>
                <div class="input-group">
                    <label>Date</label>
                    <input type="date" name="date" class="input-control" required value="{{ today }}">
                </div>
                <button type="submit" class="btn">
                    Search <i class="fa-solid fa-arrow-right"></i>
                </button>
            </form>
        </div>

        <div style="max-width: 1200px; margin: 60px auto 40px;">
            <h2 style="text-align: center; margin-bottom: 30px; font-size: 2.5rem; font-weight: 700;">Popular Routes</h2>
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 30px;">
                {% for route in routes %}
                <a href="/buses/{{ route.id }}" style="text-decoration: none; color: inherit;">
                    <div class="card">
                        <div class="card-img-wrapper">
                            <img src="https://picsum.photos/seed/{{ route.id }}/400/300" alt="Route" class="card-img">
                        </div>
                        <div style="padding: 25px;">
                            <h3 style="font-size: 1.4rem; margin-bottom: 5px;">{{ route.route_name }}</h3>
                            <p style="color: #6B7280;"><i class="fa-solid fa-road"></i> {{ route.distance_km }} km</p>
                        </div>
                    </div>
                </a>
                {% endfor %}
            </div>
        </div>
    </div>
"""

LOGIN_HTML = """
<div style="display: flex; justify-content: center; align-items: center; min-height: 60vh;">
  <div style="background: rgba(255,255,255,0.95); backdrop-filter: blur(20px); padding: 40px; border-radius: 24px; box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25); width: 100%; max-width: 400px;">
    <h2 style="font-size: 2rem; margin-bottom: 20px; text-align:center;">Welcome Back</h2>
    {% if error %}<div style="color: #dc3545; text-align: center; margin-bottom: 15px; background: #f8d7da; padding: 10px; border-radius: 8px;">{{ error }}</div>{% endif %}
    <form method="POST" autocomplete="on">
        <div style="margin-bottom: 20px;">
            <label style="display: block; font-weight: 600; margin-bottom: 8px; color: #4B5563;">Username</label>
            <input type="text" name="username" class="input-control" required>
        </div>
        <div style="margin-bottom: 25px;">
            <label style="display: block; font-weight: 600; margin-bottom: 8px; color: #4B5563;">Password</label>
            <input type="password" name="password" class="input-control" required>
        </div>
        <button class="btn" style="width: 100%;">Login</button>
    </form>
  </div>
</div>
"""


# ================= ROUTES =================
@app.route("/")
@safe_db
def home():
    conn, cur = get_db()
    today = date.today().isoformat()
    cur.execute("SELECT id, route_name, distance_km FROM routes ORDER BY id")
    routes = cur.fetchall()
    cur.execute("SELECT DISTINCT station_name FROM route_stations ORDER BY station_name")
    stations = [r["station_name"] for r in cur.fetchall()]
    return render_template_string(HOME_HTML, stations=stations, routes=routes, today=today)


@app.route("/search", methods=["POST"])
@safe_db
def search():
    fs_input = request.form.get("from", "").strip()
    ts_input = request.form.get("to", "").strip()
    travel_date = request.form.get("date", date.today().isoformat())

    session["from"] = fs_input
    session["to"] = ts_input
    session["date"] = travel_date

    if not fs_input or not ts_input: return "Select stations", 400
    fs = fs_input.lower()
    ts = ts_input.lower()

    conn, cur = get_db()
    cur.execute(
        "SELECT DISTINCT route_id FROM route_stations WHERE LOWER(station_name) = %s OR LOWER(station_name) = %s",
        (fs, ts))
    candidate_routes = [r["route_id"] for r in cur.fetchall()]

    if not candidate_routes: return "No routes", 404

    cur.execute("""
        SELECT r.id FROM routes r
        JOIN route_stations rs_from ON rs_from.route_id = r.id
        JOIN route_stations rs_to   ON rs_to.route_id = r.id
        WHERE r.id = ANY(%s) AND LOWER(rs_from.station_name) = %s AND LOWER(rs_to.station_name) = %s AND rs_from.station_order < rs_to.station_order LIMIT 1
    """, (candidate_routes, fs, ts))
    route = cur.fetchone()
    if not route: return "No valid route", 404
    return redirect(f"/buses/{route['id']}?from={fs_input}&to={ts_input}&date={travel_date}")


@app.route("/buses/<int:rid>")
@safe_db
def buses(rid):
    if request.args.get('from'):
        session['from'] = request.args.get('from')
        session['to'] = request.args.get('to')
        session['date'] = request.args.get('date')

    fs = session.get("from")
    ts = session.get("to")
    travel_date = session.get("date", date.today().isoformat())

    conn, cur = get_db()
    cur.execute(
        "SELECT r.route_name, r.distance_km, string_agg(rs.station_name, ' → ' ORDER BY rs.station_order) as stations FROM routes r LEFT JOIN route_stations rs ON r.id = rs.route_id WHERE r.id = %s GROUP BY r.id, r.route_name, r.distance_km",
        (rid,))
    route = cur.fetchone()
    if not route: return "Route not found", 404

    cur.execute("""
            SELECT s.id, s.bus_name, s.departure_time, s.total_seats, s.current_lat, s.current_lng, COALESCE(bk.count, 0) as booked_count
            FROM schedules s LEFT JOIN (
                SELECT schedule_id, COUNT(*) as count FROM seat_bookings WHERE travel_date::date = %s AND status='confirmed' GROUP BY schedule_id
            ) bk ON s.id = bk.schedule_id WHERE s.route_id = %s ORDER BY s.departure_time
        """, (travel_date, rid))
    buses_data = cur.fetchall()

    cards_html = ""
    for bus in buses_data:
        booked = bus['booked_count']
        available = bus['total_seats'] - booked
        is_live = "🟢 LIVE" if bus['current_lat'] else "⚪ Offline"
        cards_html += f"""
        <div class="card">
            <div style="padding: 25px; display: flex; flex-direction: column; flex-grow: 1;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
                    <h3 style="font-size: 1.4rem;">{bus['bus_name']}</h3>
                    <span style="background: {'#10B981' if bus['current_lat'] else '#6B7280'}; color: white; padding: 5px 12px; border-radius: 20px; font-size: 0.8rem; font-weight: 600;">{is_live}</span>
                </div>
                <p style="color: #6B7280; margin-bottom: 10px;"><i class="fa-regular fa-clock"></i> Departure: {bus['departure_time'].strftime('%H:%M')}</p>
                <p style="color: #6B7280; margin-bottom: 20px;"><i class="fa-solid fa-chair"></i> Seats Left: <b>{available}</b> / {bus['total_seats']}</p>
                <div style="display: flex; gap: 10px; margin-top: auto;">
                    <a href="/live-bus/{bus['id']}" class="btn" style="flex: 1; justify-content: center; background-color:#dc3545;">🗺️ Live GPS</a>
                    <a href="/seats/{bus['id']}?from={fs}&to={ts}&date={travel_date}" class="btn" style="flex: 1; justify-content: center;">🎫 Book Seat</a>
                </div>
            </div>
        </div>
        """

    page_content = f"""
        <div style="padding: 100px 20px 40px; max-width: 1200px; margin: 0 auto;">
            <div style="text-align: center; margin-bottom: 40px;">
                <h1 style="font-size: 3rem;">{route['route_name']}</h1>
                <p style="font-size: 1.2rem; color: #6B7280;">📍 {route['stations']} | 🛣️ {route['distance_km']} km</p>
            </div>
            <div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 30px;">{cards_html}</div>
        </div>
    """
    return render_template_string(BASE_HTML, content=page_content)


@app.route("/select/<int:sid>")
def select(sid):
    fs = request.args.get("from", session.get("from"))
    ts = request.args.get("to", session.get("to"))
    d = request.args.get("date", session.get("date"))
    if not fs or not ts or not d: return redirect("/")
    session['from'] = fs
    session['to'] = ts
    session['date'] = d
    return redirect(f"/seats/{sid}?from={fs}&to={ts}&date={d}")


@app.route("/seats/<int:sid>")
@safe_db
def seats(sid):
    fs = request.args.get("from", session.get("from", ""))
    ts = request.args.get("to", session.get("to", ""))
    d = request.args.get("date", session.get("date", date.today().isoformat()))

    if request.args.get('date'):
        session['from'] = fs
        session['to'] = ts
        session['date'] = d

    fs = fs.strip()
    ts = ts.strip()
    if not fs or not ts: return "Missing route info", 400

    conn, cur = get_db()
    cur.execute(
        "SELECT station_name, station_order FROM route_stations WHERE route_id = (SELECT route_id FROM schedules WHERE id=%s) ORDER BY station_order",
        (sid,))
    stations_data = cur.fetchall()
    station_to_order = {r['station_name'].strip().lower(): r['station_order'] for r in stations_data}
    fs_order = station_to_order.get(fs.lower(), 1)
    ts_order = station_to_order.get(ts.lower(), 2)

    cur.execute(
        "SELECT seat_number, from_station, to_station FROM seat_bookings WHERE schedule_id=%s AND travel_date::date = %s AND status='confirmed'",
        (sid, d))
    booked_seats = set()

    for r in cur.fetchall():
        bfs = station_to_order.get(r["from_station"].strip().lower(), 0)
        bts = station_to_order.get(r["to_station"].strip().lower(), 0)
        if bfs == 0 or bts == 0: continue
        if not (ts_order <= bfs or fs_order >= bts): booked_seats.add(r["seat_number"])

    seat_buttons = ""
    for i in range(1, 41):
        if i in booked_seats:
            seat_buttons += f'<button style="width:50px; height:50px; background:#E5E7EB; color:#9CA3AF; cursor:not-allowed; border-radius:10px; border:none;" disabled>{i}</button>'
        else:
            seat_buttons += f'<button onclick="bookSeat({i}, this)" style="width:50px; height:50px; background:var(--primary); color:white; border-radius:10px; margin:5px; border:none; font-weight:bold; cursor:pointer;">{i}</button>'

    page_content = f"""
    <div style="padding: 100px 20px 40px;">
        <div class="text-center mb-5">
            <h2>🚌 {fs} → {ts}</h2>
            <h5>📅 {d}</h5>
        </div>
        <div style="padding: 100px 20px 40px;">
        <div class="text-center mb-5">
            <h2>🚌 {fs} → {ts}</h2>
            <h5>📅 {d}</h5>
        </div>
        <div style="background: white; padding: 30px; border-radius: 20px; box-shadow: 0 10px 25px rgba(0,0,0,0.05); display: flex; flex-wrap: wrap; justify-content: center; gap: 10px;">
            {seat_buttons}
        </div>
    </div>
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <script>
    let bookingLock = false;
    const sid = {sid};
    const date = "{d}";
    const fromStation = "{fs}";
    const toStation = "{ts}";
    
    // ✅ FIX: Python Variables को यहाँ सेर कर दिया
    // अब JavaScript में सिर्फ इन variables का उपयोग होगा
    const userId = {session.get('user_id', 0)};
    const counterId = {session.get('counter_no', 'null')};
    
    const socket = io();
    socket.on("seat_update", d => {{ if(d.sid == sid) {{ location.reload(); }}}});
    
    async function bookSeat(seat, btn){{
        if(bookingLock) return;
        let name = prompt("Passenger Name");
        if(!name) return;
        let mobile = prompt("Mobile Number");
        if(!mobile) return;
        let payment = "online";
        let fare = 0;
        let role = "{session.get('role', 'user')}";
        if(role !== "user"){{
            fare = prompt("Enter fare");
            payment = confirm("OK = CASH | Cancel = ONLINE") ? "cash" : "online";
        }}
        bookingLock = true;
        btn.disabled = true;
        btn.style.background = "#E5E7EB";
        btn.style.color = "#9CA3AF";
        
        // ✅ FIX: अब Payload को साफ़ करें
        let payload = {{
            sid: sid,
            seat: seat,
            name: name,
            mobile: mobile,
            date: date,
            from: fromStation,
            to: toStation,
            payment_mode: payment,
            fare: fare,
            booked_by_type: role,
            booked_by_id: userId,   // यहाँ Variable Use हो रहा है
            counter_id: counterId // यहाँ Variable Use हो रहा है
        }};
        
        let res = await fetch("/book", {{
            method:"POST",
            headers:{{"Content-Type":"application/json"}},
            body: JSON.stringify(payload)
        }});
        let data = await res.json();
        if(data.ok){{
            alert("Seat Booked ✅ ("+payment.toUpperCase()+")");
            window.location.reload();
        }}else{{
            alert("Error: " + (data.error || "Unknown"));
            btn.disabled = false;
            btn.style.background = "var(--primary)";
            btn.style.color = "white";
        }}
        bookingLock = false;
    }}
    </script>
    """
    return render_template_string(BASE_HTML, content=page_content)


@app.route("/book", methods=["POST"])
@safe_db
def book():
    data = request.get_json()
    required = ['sid', 'seat', 'name', 'mobile', 'date', 'from', 'to', 'payment_mode', 'booked_by_type', 'booked_by_id']
    for field in required:
        if field not in data: return jsonify({"ok": False, "error": f"Missing field: {field}"}), 400

    conn, cur = get_db()
    try:
        cur.execute(
            "SELECT station_name, station_order FROM route_stations WHERE route_id = (SELECT route_id FROM schedules WHERE id=%s)",
            (data['sid'],))
        station_map = {r['station_name'].strip().lower(): r['station_order'] for r in cur.fetchall()}
        fs_new = station_map.get(data['from'].strip().lower())
        ts_new = station_map.get(data['to'].strip().lower())
        if fs_new is None or ts_new is None: return jsonify({"ok": False, "error": "Invalid stations"}), 400

        cur.execute(
            "SELECT from_station, to_station FROM seat_bookings WHERE schedule_id=%s AND seat_number=%s AND travel_date::date = %s AND status='confirmed'",
            (data['sid'], data['seat'], data['date']))
        existing = cur.fetchall()
        for r in existing:
            fs_old = station_map.get(r['from_station'].strip().lower())
            ts_old = station_map.get(r['to_station'].strip().lower())
            if fs_old is None or ts_old is None: continue
            if not (ts_new <= fs_old or fs_new >= ts_old): return jsonify(
                {"ok": False, "error": "Seat already booked"}), 409

        fare = random.randint(250, 450) if data['payment_mode'] == 'online' else int(data.get('fare', 300))
        cur.execute(
            "INSERT INTO seat_bookings (schedule_id, seat_number, passenger_name, mobile, from_station, to_station, travel_date, fare, status, payment_mode, booked_by_type, booked_by_id, counter_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'confirmed',%s,%s,%s,%s)",
            (data['sid'], data['seat'], data['name'], data['mobile'], data['from'], data['to'], data['date'], fare,
             data['payment_mode'], data['booked_by_type'], data['booked_by_id'], data.get('counter_id')))
        conn.commit()
        socketio.emit("seat_update", {"sid": data['sid'], "seat": data['seat']})
        return jsonify({"ok": True, "fare": fare})
    except Exception as e:
        conn.rollback()
        import traceback;
        traceback.print_exc()
        return jsonify({"ok": False, "error": "Server error"}), 500


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        try:
            conn, cur = get_db()
            cur.execute("SELECT id, role FROM admins WHERE username=%s AND password=%s", (username, password))
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
            error = "Server error"
    return render_template_string(BASE_HTML, content=render_template_string(LOGIN_HTML, error=error))


@app.route("/dashboard")
def dashboard():
    if not session.get("user_logged_in"): return redirect("/login")
    role = session.get("role", "user")
    admin_links = ""
    if role.lower() == "admin":
        admin_links = """<div style="margin-top: 30px; display: flex; gap: 10px; flex-wrap: wrap; justify-content: center;"><a href="/create-counter" class="btn">➕ Create Counter</a></div>"""

    return render_template_string(BASE_HTML, content=f"""
        <div style="text-align: center; padding: 100px 20px;">
            <h2>Welcome 🎉</h2><h4>Role: <b>{role.upper()}</b></h4>
            <div style="margin-top: 20px;"><a href="/" class="btn" style="background:#10B981;">🏠 Home</a> <a href="/logout" class="btn" style="background:#dc3545; margin-left:10px;">🚪 Logout</a></div>
            {admin_links}
        </div>""")


@app.route("/live-bus/<int:sid>")
@safe_db
def live_bus(sid):
    conn, cur = get_db()
    cur.execute(
        "SELECT s.id, s.bus_name, r.id as route_id, r.route_name, s.current_lat, s.current_lng FROM schedules s JOIN routes r ON s.route_id = r.id WHERE s.id = %s",
        (sid,))
    bus = cur.fetchone()
    if not bus: return "Bus not found", 404

    lat = float(bus.get('current_lat', 27.2))
    lng = float(bus.get('current_lng', 75.0))

    # HTML with Fix for CSS padding and JS SVG injection
    page_content = f"""
    <div style="padding: 100px 20px 40px; max-width: 1200px; margin: 0 auto;">
        <div class="text-center mb-4"><h2>{bus['bus_name']} Live Tracking</h2><span style="background:#10B981; color:white; padding:5px 10px; border-radius:15px; font-size:0.8rem;">🟢 GPS Active</span></div>
        <div id="map" style="height: 600px; width: 100%; border-radius: 20px; box-shadow: 0 20px 40px rgba(0,0,0,0.2); z-index: 1; position: relative;"></div>
    </div>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <script>
    const map = L.map('map').setView([{lat}, {lng}], 13);
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{attribution: '© OpenStreetMap', maxZoom: 19}}).addTo(map);
    const svgContent = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" style="width:100%;height:100%;fill:#dc3545;"><path d="M144 48h224c13.3 0 24 10.7 24 24v48h-48v-32h-64v32h-64v-32h-48V72c0-13.3 10.7-24 24-24zM112 144h288c26.5 0 48 21.5 48 48v208c0 26.5-21.5 48-48 48H112c-26.5 0-48-21.5-48-48V192c0-26.5 21.5-48 48-48zm32 64c0-8.8 7.2-16 16-16s16 7.2 16 16v64c0 8.8-7.2 16-16 16s-16-7.2-16-16v-64zm192 0c0-8.8 7.2-16 16-16s16 7.2 16 16v64c0 8.8-7.2 16-16 16s-16-7.2-16-16v-64z"/></svg>';
    const busIcon = L.divIcon({{html: `<div style="width: 50px; height: 50px; display: flex; justify-content: center; align-items: center; transform: rotate(90deg);">${{svgContent}}</div>`, className: 'bus-icon-marker', iconSize: [50, 50], iconAnchor: [25, 25]}});
    let busMarker = L.marker([{lat}, {lng}], {{icon: busIcon}}).addTo(map);
    let trailPoints = [[{lat}, {lng}]];
    let trailLine = L.polyline(trailPoints, {{color: 'red', weight: 4, opacity: 0.7}}).addTo(map);
    let prevLat = {lat}, prevLng = {lng};
    const socket = io();
    socket.on('bus_location', data => {{
        if(data.sid == {sid}){{
            const lt = parseFloat(data.lat), ln = parseFloat(data.lng);
            busMarker.setLatLng([lt, ln]);
            map.panTo([lt, ln], {{animate: true}});
            trailPoints.push([lt, ln]);
            trailLine.setLatLngs(trailPoints);
            const dLat = lt - prevLat, dLng = ln - prevLng;
            const angle = Math.atan2(dLng, dLat) * 180 / Math.PI;
            const iconElement = busMarker.getElement().querySelector('div');
            if(iconElement) {{ iconElement.style.transform = `rotate(${{angle + 90}}deg)`; }}
            prevLat = lt; prevLng = ln;
        }}
    }});
    </script>
    """
    return render_template_string(BASE_HTML, content=page_content)


@app.route("/driver/<int:sid>")
def driver(sid):
    return render_template_string(BASE_HTML, content=f"""
    <div class="text-center" style="padding: 100px 20px;">
        <h2>Driver GPS - Bus {sid}</h2>
        <button class="btn" onclick="startGPS()">Start GPS</button>
        <button class="btn" style="background: #dc3545;" onclick="stopGPS()">Stop GPS</button>
        <div id="status" style="margin-top:20px;">GPS Off</div>
    </div>
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <script>
    const socket = io();
    let watchId = null;
    function startGPS(){{ if(!navigator.geolocation) return; watchId = navigator.geolocation.watchPosition(pos => {{ socket.emit("driver_gps", {{sid:{sid}, lat:pos.coords.latitude, lng:pos.coords.longitude}}); document.getElementById("status").innerText = "LIVE: " + pos.coords.latitude.toFixed(5); }}, err => alert("Error")); }}
    function stopGPS(){{ if(watchId) navigator.geolocation.clearWatch(watchId); watchId = null; document.getElementById("status").innerText = "Stopped"; }}
    </script>""")


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
            error = "Fill all fields"
        else:
            try:
                conn, cur = get_db()
                cur.execute(
                    "INSERT INTO admins (username, password, role, counter_no) VALUES (%s, %s, 'counter', %s) ON CONFLICT (username) DO NOTHING",
                    (username, password, counter_no))
                conn.commit()
                success = "Counter Created ✅"
            except:
                error = "Error"

    return render_template_string(BASE_HTML, content=f"""
    <div class="card" style="max-width:500px; margin:100px auto 20px;">
        <div style="padding: 25px;">
            <h4>➕ Create New Counter</h4>
            {f"<div style='color:#10B981'>{success}</div>" if success else ""}
            {f"<div style='color:#dc3545'>{error}</div>" if error else ""}
            <form method="POST" style="margin-top:15px;">
                <div style="margin-bottom:15px;"><label>Username</label><input type="text" name="username" class="input-control" required></div>
                <div style="margin-bottom:15px;"><label>Password</label><input type="password" name="password" class="input-control" required></div>
                <div style="margin-bottom:15px;"><label>Counter No</label><input type="number" name="counter_no" class="input-control" required></div>
                <button class="btn" style="width:100%;">Create</button>
            </form>
        </div>
    </div>""")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


if __name__ == "__main__":
    try:
        init_db()
    except:
        pass
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=False)