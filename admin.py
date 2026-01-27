from dotenv import load_dotenv
load_dotenv()
import setuptools
import os, random
from datetime import date
from functools import wraps
from flask import Flask, request, jsonify, render_template_string, redirect, g, session, render_template
from flask_socketio import SocketIO, emit
from flask_compress import Compress
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
import atexit
import razorpay
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
    def wrap(*a, **k):
        if "admin" not in session:
            return redirect("/admin/login")
        return f(*a, **k)

    wrap.__name__ = f.__name__
    return wrap


# ================= DB INIT =================
def init_db():
    try:
        conn = pool.getconn()
        cur = conn.cursor()

        # ===== TABLES =====
        cur.execute("""
                CREATE TABLE IF NOT EXISTS camera_logs (
                id SERIAL PRIMARY KEY,
    bus_id INT,
    station TEXT,
    boarded INT,
    dropped INT,
    time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);""")

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

# ========= /admin/add-bus =========
@app.route("/admin/add-bus", methods=["GET","POST"])
@admin_required
def admin_add_bus():
    conn, cur = get_db()

    # सभी routes dropdown के लिए
    cur.execute("SELECT id, route_name FROM routes ORDER BY id")
    routes = cur.fetchall()

    if request.method == "POST":
        route_id = request.form["route_id"]
        bus_name = request.form["bus_name"]
        time = request.form["departure_time"]
        seats = request.form["total_seats"]

        cur.execute("""
            INSERT INTO schedules
            (route_id, bus_name, departure_time, total_seats)
            VALUES (%s, %s, %s::time, %s)
        """, (route_id, bus_name, time, seats))

        conn.commit()

        return """
        <h3>✅ नई बस सफलतापूर्वक जोड़ दी गई!</h3>
        <a href='/admin'>Admin Dashboard</a>
        """

    options = "".join(
        f"<option value='{r['id']}'>{r['route_name']}</option>"
        for r in routes
    )

    return f"""
    <h3>🚌 नई Bus जोड़ें</h3>

    <form method="post">

    Route:
    <select name="route_id">{options}</select><br><br>

    Bus Name:
    <input name="bus_name" placeholder="जैसे: Volvo AC"><br><br>

    Departure Time:
    <input name="departure_time" placeholder="08:30"><br><br>

    Total Seats:
    <input name="total_seats" value="40"><br><br>

    <button>Add Bus</button>

    </form>
    """
#======= /admin/login ========

@app.route("/admin/login", methods=["GET","POST"])
def admin_login():
    conn, cur = get_db()

    error = ""

    if request.method == "POST":
        u = request.form["username"]
        p = request.form["password"]

        cur.execute(
            "SELECT * FROM admins WHERE username=%s AND password=%s",
            (u, p)
        )
        admin = cur.fetchone()

        if admin:
            session["admin"] = admin["username"]
            return redirect("/admin")
        else:
            error = "❌ गलत Username या Password"

    html = f"""
    <div class="row justify-content-center">
        <div class="col-md-5">
            <div class="card shadow-lg border-0 rounded-4 p-4">
                <div class="text-center mb-4">
                    <h2 class="fw-bold">🔐 Admin Login</h2>
                    <p class="text-muted">Bus Booking Control Panel</p>
                </div>

                {'<div class="alert alert-danger text-center">'+error+'</div>' if error else ''}

                <form method="post">
                    <div class="mb-3">
                        <label class="form-label">Username</label>
                        <input name="username" class="form-control form-control-lg" required>
                    </div>

                    <div class="mb-3">
                        <label class="form-label">Password</label>
                        <input name="password" type="password" class="form-control form-control-lg" required>
                    </div>

                    <div class="d-grid">
                        <button class="btn btn-primary btn-lg">
                            🚀 Login
                        </button>
                    </div>
                </form>
            </div>
        </div>
    </div>
    """

    return render_template_string(BASE_HTML, content=html)
#========= admin=======
@app.route("/admin")
@admin_required
def admin_home():
    conn, cur = get_db()

    # ===== STATS =====
    cur.execute("SELECT COUNT(*) AS total FROM seat_bookings")
    total = cur.fetchone()["total"]

    cur.execute("SELECT COALESCE(SUM(fare),0) AS earn FROM seat_bookings")
    earn = cur.fetchone()["earn"]

    cur.execute("""
        SELECT COUNT(*) AS today
        FROM seat_bookings
        WHERE travel_date = CURRENT_DATE
    """)
    today = cur.fetchone()["today"]

    cur.execute("""
        SELECT passenger_name, seat_number, travel_date,
               fare, booked_by_type
        FROM seat_bookings
        ORDER BY id DESC LIMIT 8
    """)
    recent = cur.fetchall()

    cards = f"""
    <div class="row g-4 mb-5">
        <div class="col-md-4">
            <div class="card shadow text-center border-0 rounded-4 p-4">
                <h6>Total Bookings</h6>
                <h2 class="fw-bold text-primary">{total}</h2>
            </div>
        </div>

        <div class="col-md-4">
            <div class="card shadow text-center border-0 rounded-4 p-4">
                <h6>Total Earning</h6>
                <h2 class="fw-bold text-success">₹ {earn}</h2>
            </div>
        </div>

        <div class="col-md-4">
            <div class="card shadow text-center border-0 rounded-4 p-4">
                <h6>Today Bookings</h6>
                <h2 class="fw-bold text-warning">{today}</h2>
            </div>
        </div>
    </div>
    """

    actions = """
    <div class="d-flex justify-content-center gap-3 mb-5 flex-wrap">
        <a href="/admin/add-bus" class="btn btn-success btn-lg">➕ Add Bus</a>
        <a href="/admin/book" class="btn btn-primary btn-lg">🧾 Counter Booking</a>
        <a href="/admin/bookings" class="btn btn-info btn-lg">📋 All Bookings</a>
        <a href="/admin/logout" class="btn btn-danger btn-lg">🚪 Logout</a>
    </div>
    """

    table = """
    <h4 class="mb-3">🕒 Recent Bookings</h4>
    <div class="table-responsive">
    <table class="table table-striped table-hover shadow rounded-4">
        <thead class="table-dark">
            <tr>
                <th>Name</th>
                <th>Seat</th>
                <th>Date</th>
                <th>Fare</th>
                <th>Type</th>
            </tr>
        </thead>
        <tbody>
    """

    for r in recent:
        table += f"""
        <tr>
            <td>{r['passenger_name']}</td>
            <td>{r['seat_number']}</td>
            <td>{r['travel_date']}</td>
            <td>₹ {r['fare']}</td>
            <td>{r['booked_by_type']}</td>
        </tr>
        """

    table += "</tbody></table></div>"

    content = f"""
    <div class="text-center mb-5">
        <h2 class="fw-bold">🚌 Admin Dashboard</h2>
        <p class="text-muted">Bus Booking Management</p>
    </div>

    {cards}
    {actions}
    {table}
    """

    return render_template_string(BASE_HTML, content=content)

    #========== /admin/bookings =========
@app.route("/admin/bookings")
@admin_required
def all_bookings():
    conn, cur = get_db()

    cur.execute("""
    SELECT id, schedule_id, seat_number,
           passenger_name, mobile,
           from_station, to_station,
           travel_date, fare, status,
           booked_by_type
    FROM seat_bookings
    ORDER BY id DESC
    """)

    rows = cur.fetchall()

    html = """
    <h3>All Bookings</h3>
    <table border="1" cellpadding="5">
    <tr>
      <th>ID</th>
      <th>Name</th>
      <th>Seat</th>
      <th>Date</th>
      <th>Fare</th>
      <th>Type</th>
    </tr>
    """

    for r in rows:
        html += f"""
        <tr>
          <td>{r.get('id','')}</td>
          <td>{r.get('passenger_name','')}</td>
          <td>{r.get('seat_number','')}</td>
          <td>{r.get('travel_date','')}</td>
          <td>{r.get('fare','')}</td>
          <td>{r.get('booked_by_type','')}</td>
        </tr>
        """

    html += "</table><br><a href='/admin'>Back</a>"

    return html
#========/admin/book======
@app.route("/admin/book", methods=["GET","POST"])
@admin_required
def admin_book():
    if request.method=="POST":

        data = request.form

        payload = {
            "sid": data["sid"],
            "seat": data["seat"],
            "name": data["name"],
            "mobile": data["mobile"],
            "date": data["date"],
            "from": data["from"],
            "to": data["to"],
            "payment_mode": "cash",
            "booked_by_type": "counter",
            "booked_by_id": 1
        }

        with app.test_client() as c:
            c.post("/book", json=payload)

        return "✅ Counter se booking ho gayi"

    return """
    <h3>Counter Booking</h3>

    <form method="post">
    Bus ID: <input name="sid"><br>
    Seat: <input name="seat"><br>
    Name: <input name="name"><br>
    Mobile: <input name="mobile"><br>
    Date: <input name="date"><br>
    From: <input name="from"><br>
    To: <input name="to"><br>

    <button>Book</button>
    </form>
    """

if __name__ == "__main__":
    print("🚀 Bus Booking App Starting... (Live Updates 100% Working)")
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=True)
