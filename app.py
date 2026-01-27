from dotenv import load_dotenv
load_dotenv()

import os, random, json
from datetime import date
from functools import wraps
from flask import Flask, request, jsonify, render_template, redirect, g, session
from flask_socketio import SocketIO, emit
from flask_compress import Compress
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
import razorpay
import atexit
import eventlet

# ================= CONFIG =================
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key")
Compress(app)

# SocketIO with eventlet (Render optimized)
socketio = SocketIO(
    app, cors_allowed_origins="*", async_mode="eventlet",
    logger=True, engineio_logger=True, ping_timeout=60
)
eventlet.monkey_patch()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise Exception("DATABASE_URL env var missing!")

pool = ConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=5, timeout=20)

# Razorpay
RAZORPAY_ENABLED = bool(os.getenv("RAZORPAY_KEY_ID") and os.getenv("RAZORPAY_KEY_SECRET"))
razor_client = razorpay.Client(
    auth=(os.getenv("RAZORPAY_KEY_ID"), os.getenv("RAZORPAY_KEY_SECRET"))
) if RAZORPAY_ENABLED else None

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
    @wraps(f)
    def wrap(*a, **k):
        if "admin" not in session:
            return redirect("/admin/login")
        return f(*a, **k)
    return wrap

@atexit.register
def shutdown_pool():
    pool.close()

# ================= DB INIT =================
def init_db():
    conn = pool.getconn()
    cur = conn.cursor()
    try:
        # Admin table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            id SERIAL PRIMARY KEY,
            username VARCHAR(50) UNIQUE,
            password VARCHAR(100),
            role VARCHAR(20) DEFAULT 'admin'
        )""")
        cur.execute("SELECT COUNT(*) FROM admins")
        if cur.fetchone()[0] == 0:
            cur.execute("""
                INSERT INTO admins (username,password) 
                VALUES ('admin','1234') ON CONFLICT DO NOTHING
            """)

        # Routes, schedules, seat_bookings, route_stations
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
        print("✅ DB Init Complete!")
    except Exception as e:
        conn.rollback()
        print("❌ DB Init Error:", e)
    finally:
        cur.close()
        pool.putconn(conn)

init_db()

# ================= ROUTES =================
@app.route("/")
@safe_db
def home():
    conn, cur = get_db()
    cur.execute("SELECT id, route_name, distance_km FROM routes ORDER BY id")
    routes = cur.fetchall()

    cur.execute("""
        SELECT s.id, s.bus_name, r.route_name,
               s.current_lat, s.current_lng
        FROM schedules s JOIN routes r ON s.route_id = r.id
        ORDER BY s.id LIMIT 4
    """)
    live_buses = cur.fetchall()

    today = date.today().isoformat()   # ← yahin convert
    return render_template(
        "mybus.html",
        routes=routes,
        live_buses=live_buses,
        today=today
    )

# ================= SOCKET EVENTS =================
@socketio.on("connect")
def handle_connect():
    print(f"✅ Client connected: {request.sid}")

@socketio.on("driver_gps")
def handle_gps(data):
    sid = data.get('sid')
    lat = float(data.get('lat', 27.2))
    lng = float(data.get('lng', 75.0))
    try:
        conn, cur = get_db()
        cur.execute(
            "UPDATE schedules SET current_lat=%s, current_lng=%s WHERE id=%s",
            (lat, lng, sid)
        )
        conn.commit()
    except:
        pass
    emit("bus_location", {"sid": sid, "lat": lat, "lng": lng}, broadcast=True)
# ********* routes search *******
@app.route("/search", methods=["POST"])
def search_routes():
    data = request.json
    from_station = data["from"]
    to_station = data["to"]

    query = """
    SELECT r.*
    FROM routes r
    WHERE r.id IN (
        SELECT s1.route_id
        FROM route_stations s1
        JOIN route_stations s2 
          ON s1.route_id = s2.route_id
        WHERE s1.station_name = %s
          AND s2.station_name = %s
          AND s1.station_order < s2.station_order
    )
    """

    conn, cur = get_db()
    cur.execute(query, (from_station, to_station))
    routes = cur.fetchall()

    return jsonify(routes)
# ================= RUN =================
if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000)),
        debug=False,
        server_options={"async_mode": "eventlet"}
    )
