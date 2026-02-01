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

<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600;700&display=swap" rel="stylesheet">

<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:'Poppins',sans-serif;}

body{background:#f5f7fb;color:#222;}

/* NAVBAR */
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
.navbar a:hover{color:#ff512f;}

/* HERO */
.hero{
  height:100vh;
  background:
    linear-gradient(rgba(0,0,0,.6),rgba(0,0,0,.8)),
    url("https://images.unsplash.com/photo-1544620347-c4fd4a3d5957?auto=format&fit=crop&w=1600&q=80");
  background-size:cover;
  background-position:center;
  display:flex;
  align-items:center;
  justify-content:center;
  text-align:center;
  color:white;
  padding-top:70px;
}

.hero h1{font-size:3.5rem;}
.hero p{font-size:1.3rem;margin:15px 0 30px;}

/* SEARCH */
.search-box{
  background:white;
  padding:20px;
  border-radius:15px;
  display:flex;
  gap:10px;
  box-shadow:0 20px 40px rgba(0,0,0,.3);
}
.search-box input{
  padding:12px;
  border:none;
  outline:none;
  border-radius:8px;
  width:180px;
  background:#f1f3f7;
}
.search-box button{
  padding:12px 30px;
  border:none;
  border-radius:10px;
  background:linear-gradient(45deg,#ff512f,#dd2476);
  color:white;
  font-weight:600;
  cursor:pointer;
}

/* STATS */
.stats{
  padding:60px 10%;
  display:grid;
  grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
  text-align:center;
  gap:30px;
}
.stat h2{color:#ff512f;font-size:2.2rem;}
.stat p{color:#666;}

/* FEATURES */
.features{
  padding:60px 10%;
  display:grid;
  grid-template-columns:repeat(auto-fit,minmax(250px,1fr));
  gap:30px;
}
.card{
  background:white;
  border-radius:20px;
  box-shadow:0 15px 30px rgba(0,0,0,.1);
  transition:.4s;
}
.card:hover{transform:translateY(-10px);}
.card img{
  width:100%;height:180px;object-fit:cover;
  border-radius:20px 20px 0 0;
}
.card .content{padding:20px;}

/* CTA */
.cta{
  background:linear-gradient(45deg,#ff512f,#dd2476);
  color:white;
  padding:60px 10%;
  text-align:center;
}
.cta h2{font-size:2.5rem;margin-bottom:15px;}
.cta button{
  padding:15px 40px;
  border:none;
  border-radius:30px;
  background:white;
  color:#ff512f;
  font-weight:600;
  cursor:pointer;
}

/* FOOTER */
footer{
  background:#111;
  color:#aaa;
  padding:40px 10%;
  display:grid;
  grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
}
footer h3{color:white;margin-bottom:10px;}
footer p{font-size:.9rem;}
.copy{text-align:center;background:#000;color:#777;padding:15px;}
</style>
</head>
<body>

<!-- NAVBAR -->
<div class="navbar">
  <div class="logo">🚌 My Bus AI</div>
  <div>
    <a href="/login">User Login</a>
    <a href="/admin">Admin</a>
    <a href="/counter">Counter</a>
  </div>
</div>

<!-- HERO -->
<section class="hero">
  <div>
    <h1>India’s Smart Bus Platform</h1>
    <p>Book | Track | Face Boarding | Live Seats</p>
    <form class="search-box" action="/search" method="POST">
      <input name="from" placeholder="From">
      <input name="to" placeholder="To">
      <datalist id="stations">
    {% for s in stations %}
      <option value="{{s}}">
    {% endfor %}
     </datalist>	
      <input type="date" name="date">
      <button type="submit">Search</button>
    </form>
  </div>
</section>

<!-- STATS -->
<section class="stats">
  <div class="stat"><h2>5,000+</h2><p>Buses</p></div>
  <div class="stat"><h2>2M+</h2><p>Passengers</p></div>
  <div class="stat"><h2>99%</h2><p>On-Time</p></div>
  <div class="stat"><h2>AI</h2><p>Face Boarding</p></div>
</section>

<!-- FEATURES -->
<section class="features">
  <div class="card">
    <img src="https://images.unsplash.com/photo-1509749837427-ac94a2553d0e">
    <div class="content">
      <h3>Luxury Buses</h3>
      <p>AC Sleeper, Volvo & Electric</p>
    </div>
  </div>
  <div class="card">
    <img src="https://images.unsplash.com/photo-1519582149095-fe7d19b07b63">
    <div class="content">
      <h3>Live GPS</h3>
      <p>Real-time tracking</p>
    </div>
  </div>
  <div class="card">
    <img src="https://images.unsplash.com/photo-1500530855697-b586d89ba3ee">
    <div class="content">
      <h3>AI Boarding</h3>
      <p>No ticket, just face scan</p>
    </div>
  </div>
</section>

<!-- CTA -->
<section class="cta">
  <h2>Ready for Smart Travel?</h2>
  <p>Join India’s first AI-powered bus system</p>
  <button onclick="location.href='/register'">Create Free Account</button>
</section>

<!-- FOOTER -->
<footer>
  <div>
    <h3>SmartBus AI</h3>
    <p>Future of travel in India</p>
  </div>
  <div>
    <h3>Company</h3>
    <p>About | Careers | Blog</p>
  </div>
  <div>
    <h3>Support</h3>
    <p>Help Center | Terms | Privacy</p>
  </div>
</footer>
<div class="copy">© 2026 SmartBus AI</div>

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

    return render_template_string(BASE_HTML, stations=stations, routes=routes)

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


@app.route("/select/<int:sid>", methods=["GET", "POST"])
@safe_db
def select(sid):
    # DB connection
    conn, cur = get_db()
    print("SID =", sid)
    print("SID TYPE =", type(sid))
    # ✅ Bus schedule fetch
    cur.execute("SELECT route_id FROM schedules WHERE id=%s", (sid,))
    row = cur.fetchone()
    print(row)
    if not row:
        return "Bus schedule not found", 404
    route_id = row["route_id"]

    # ✅ Route stations fetch with order
    cur.execute("""
        SELECT station_name, station_order
        FROM route_stations
        WHERE route_id=%s
        ORDER BY station_order
    """, (route_id,))
    stations_data = cur.fetchall()
    stations = [s["station_name"] for s in stations_data]

    today = date.today().isoformat()

    # ✅ Handle POST form submission
    if request.method == "POST":
        fs = request.form.get("from")
        ts = request.form.get("to")
        d = request.form.get("date")
        if not (fs and ts and d):
            return "Please select From, To, and Date", 400
        return redirect(f"/seats/{sid}?fs={fs}&ts={ts}&d={d}")

    # ✅ Dropdown HTML options
    options = "".join(f"<option>{s}</option>" for s in stations)
    stations_json = json.dumps(stations_data)  # Python list → JSON for JS

    # ✅ Form HTML
    form_html = f"""
    <div class="card mx-auto" style="max-width:500px; margin-top:40px;">
        <div class="card-body">
            <h4 class="card-title text-center mb-4">🎫 Journey Details</h4>
            <form method="POST">
                <div class="mb-3">
                    <label class="form-label">From:</label>
                    <select name="from" class="form-select" required onchange="updateTo(this.value)">
                        {options}
                    </select>
                </div>
                <div class="mb-3">
                    <label class="form-label">To:</label>
                    <select name="to" class="form-select" required>
                        {options}
                    </select>
                </div>
                <div class="mb-3">
                    <label class="form-label">Date:</label>
                    <input type="date" name="date" class="form-control" value="{today}" min="{today}" required>
                </div>
                <button class="btn btn-success w-100">View Available Seats</button>
            </form>
        </div>
    </div>

    <script>
    // Stations data from Python
    const stations = {stations_json};

    function updateTo(fromStation) {{
        const fromOrder = stations.find(s => s.station_name === fromStation).station_order;
        const toSelect = document.querySelector('select[name="to"]');
        toSelect.innerHTML = '';
        stations.forEach(s => {{
            if(s.station_order > fromOrder) {{
                let opt = document.createElement('option');
                opt.value = s.station_name;
                opt.innerText = s.station_name;
                toSelect.appendChild(opt);
            }}
        }});
    }}
    </script>
    """

    return render_template_string(BASE_HTML, content=form_html)


@app.route("/seats/<int:sid>")
@safe_db
def seats(sid):
    fs = request.args.get("fs", "बीकानेर")
    ts = request.args.get("ts", "जयपुर")
    d = request.args.get("d", date.today().isoformat())

    conn, cur = get_db()

    # ===== Station Order =====
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

    # ===== Booked Seats =====
    cur.execute("""
        SELECT seat_number, from_station, to_station
        FROM seat_bookings
        WHERE schedule_id=%s
          AND travel_date=%s
          AND status='confirmed'
    """, (sid, d))

    booked_seats = set()
    for r in cur.fetchall():
        bfs = station_to_order.get(r["from_station"], 0)
        bts = station_to_order.get(r["to_station"], 0)
        if not (ts_order <= bfs or fs_order >= bts):
            booked_seats.add(r["seat_number"])

    # ===== Seat Buttons =====
    seat_buttons = ""
    total_seats = 40
    available = total_seats - len(booked_seats)

    for i in range(1, total_seats + 1):
        if i in booked_seats:
            seat_buttons += '<button class="btn btn-danger seat" disabled>X</button>'
        else:
            seat_buttons += f'''
            <button class="btn btn-success seat"
                    onclick="bookSeat({i}, this)">
                {i}
            </button>'''

    # ===== Bus + Map =====
    cur.execute("""
        SELECT current_lat, current_lng, route_id
        FROM schedules WHERE id=%s
    """, (sid,))
    bus = cur.fetchone()

    lat = float(bus["current_lat"] or 27.2)
    lng = float(bus["current_lng"] or 75.0)

    cur.execute("""
        SELECT lat, lng, station_name
        FROM route_stations
        WHERE route_id=%s
        ORDER BY station_order
    """, (bus["route_id"],))
    import json
    stations_json = json.dumps(cur.fetchall(), ensure_ascii=False)

    role = session.get("role", "user")
    user_id = session.get("user_id", 0)
    counter_no = session.get("counter_no", None)

    # ================= HTML =================
    html = f"""
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>

<style>
#seat-map{{height:260px;border-radius:20px;margin-bottom:20px;}}
.seat{{width:52px;height:52px;margin:4px;font-weight:bold;border-radius:12px;}}
</style>

<div class="text-center mb-3">
    <h3>🚌 {fs} → {ts}</h3>
    <h5>📅 {d}</h5>
    <span class="badge bg-success">Available {available}</span>
</div>

<div id="seat-map"></div>

<div class="text-center mb-4">
    {seat_buttons}
</div>

<script>
const sid = {sid};
let bookingLock = false;

// ===== MAP =====
const map = L.map("seat-map").setView([{lat},{lng}], 9);
L.tileLayer("https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png").addTo(map);

const busIcon = L.divIcon({{
    html: '<i class="fa fa-bus" style="font-size:28px;color:green;"></i>',
    className: 'bus-icon',
    iconSize: [40,40]
}});
let busMarker = L.marker([{lat},{lng}], {{icon: busIcon}}).addTo(map);
// ===== STATIONS + ROUTE =====
const stations = {stations_json};
let routePts = [];
stations.forEach(s => {{
    let la = parseFloat(s.lat), ln = parseFloat(s.lng);
    if(!isNaN(la) && !isNaN(ln)) routePts.push([la, ln]);
}});
if(routePts.length>1){{
    let poly = L.polyline(routePts, {{color:'blue'}}).addTo(map);
    map.fitBounds(poly.getBounds());
}}


const socket = io();
socket.on("seat_update", d => {{
    if(d.sid == sid) markSeatBooked(d.seat);
}});

function markSeatBooked(seat){{
    let btn = document.querySelectorAll(".seat")[seat-1];
    if(btn){{
        btn.disabled = true;
        btn.classList.remove("btn-success");
        btn.classList.add("btn-danger");
        btn.innerText = "X";
    }}
}}

// ===== BOOK SEAT =====
async function bookSeat(seat, btn){{
    if(bookingLock) return;

    let name = prompt("Passenger Name");
    if(!name) return;

    let mobile = prompt("Mobile Number");
    if(!mobile) return;

    let payment = "online";  // default
    let fare = 0;          // default

    let role = "{role}";

    if(role !== "user"){{
        fare = prompt("Enter fare");
        payment = confirm("OK = CASH | Cancel = ONLINE") ? "cash" : "online";
    }}

    bookingLock = true;
    btn.disabled = true;

    let payload = {{
        sid: sid,
        seat: seat,
        name: name,
        mobile: mobile,
        date: "{d}",
        from: "{fs}",
        to: "{ts}",
        payment_mode: payment,
        fare: fare,   
        booked_by_type: role,
        booked_by_id: {user_id},
        counter_id: {counter_no if counter_no else 'null'}
    }};

    let res = await fetch("/book", {{
        method:"POST",
        headers:{{"Content-Type":"application/json"}},
        body: JSON.stringify(payload)
    }});

    let data = await res.json();

    if(data.ok){{
        markSeatBooked(seat);
        alert("Seat Booked ✅ ("+payment.toUpperCase()+")");
    }}else{{
        alert(data.error);
        btn.disabled = false;
    }}

    bookingLock = false;
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
        if field not in data:
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
        role = data['booked_by_type']

        if role == "user":
            payment_mode = "online"
            status = "confirmed"  # online payment ke baad confirm
        else:
            payment_mode = "cash"
            status = "confirmed"

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
@app.route("/search", methods=["POST"])
@safe_db
def search():
    from_station = request.form.get("from")
    to_station = request.form.get("to")
    travel_date = request.form.get("date")

    if not from_station or not to_station or not travel_date:
        return "Please fill all fields", 400

    # Find routes that include both stations
    conn, cur = get_db()
    cur.execute("""
        SELECT DISTINCT r.id, r.route_name
        FROM routes r
        JOIN route_stations rs1 ON r.id = rs1.route_id
        JOIN route_stations rs2 ON r.id = rs2.route_id
        WHERE rs1.station_name = %s AND rs2.station_name = %s
    """, (from_station, to_station))

    route_ids = [r['route_id'] for r in cur.fetchall()]

    if not route_ids:
        return f"कोई route नहीं मिला {from_station} → {to_station}", 404

    # ✅ Routes की पूरी जानकारी fetch करें
    cur.execute("""
            SELECT id, route_name, distance_km
            FROM routes
            WHERE id = ANY(%s)
        """, (route_ids,))

    routes = cur.fetchall()

    # HTML में दिखाएँ
    html = f"<h3>Routes from {from_station} → {to_station}:</h3>"
    for r in routes:
        html += f"""
            <div class='card mb-3 p-3'>
                <h5>🛣️ {r['route_name']} ({r['distance_km']} km)</h5>
                <a href='/buses/{r['id']}' class='btn btn-primary btn-sm'>View Buses</a>
            </div>
            """

    return render_template_string(BASE_HTML, content=html)

if __name__ == "__main__":
    print("🚀 Bus Booking App Starting... (Live Updates 100% Working)")
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=True)
