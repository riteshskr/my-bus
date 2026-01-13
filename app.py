import os, random
from datetime import date
from functools import wraps
from flask import Flask, request, jsonify, render_template_string
from flask_socketio import SocketIO
from flask_compress import Compress
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
import atexit

# ================= APP =================
app = Flask(__name__)
app.secret_key = "super-secret-key"
Compress(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode=None)

# ================= DATABASE =================
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise Exception("DATABASE_URL environment variable is missing!")

pool = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=1,
    max_size=3,
    timeout=30,
    max_idle=120
)

print("✅ Connection pool ready")


# ================= CLEANUP ON EXIT =================
@atexit.register
def shutdown_pool():
    print("🛑 Shutting down DB pool...")
    pool.close()


# ================= DB INIT FUNCTION =================
def init_db():
    conn = None
    try:
        conn = pool.getconn()
        cur = conn.cursor()

        # Create tables
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
            current_lat DOUBLE PRECISION DEFAULT 0.0, 
            current_lng DOUBLE PRECISION DEFAULT 0.0,
            created_at TIMESTAMP DEFAULT NOW(), 
            seating_rate DOUBLE PRECISION,
            total_seats INT DEFAULT 40
        )""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS seat_bookings (
            id SERIAL PRIMARY KEY, 
            schedule_id INT REFERENCES schedules(id), 
            seat_number INT,
            passenger_name VARCHAR(100), 
            mobile VARCHAR(15), 
            from_station VARCHAR(50),
            to_station VARCHAR(50), 
            travel_date DATE, 
            status VARCHAR(20) DEFAULT 'confirmed',
            fare INT, 
            created_at TIMESTAMP DEFAULT NOW()
        )""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS route_stations (
            id SERIAL PRIMARY KEY, 
            route_id INT REFERENCES routes(id), 
            station_name VARCHAR(50), 
            station_order INT
        )""")

        conn.commit()

        # Insert default data if not exists
        cur.execute("SELECT COUNT(*) FROM routes WHERE id=1")
        if cur.fetchone()[0] == 0:
            cur.execute("""
            INSERT INTO routes (id, route_name, distance_km)
            VALUES (1,'Jaipur-Delhi',280)
            ON CONFLICT (id) DO NOTHING
            """)
            cur.execute("""
            INSERT INTO schedules (id, route_id, bus_name, departure_time)
            VALUES 
            (1,1,'Volvo AC Sleeper','08:00'),
            (2,1,'Semi Sleeper AC','10:30')
            ON CONFLICT (id) DO NOTHING
            """)
            cur.execute("""
            INSERT INTO route_stations (route_id,station_name,station_order)
            VALUES 
            (1,'Jaipur',1),
            (1,'Delhi',2)
            ON CONFLICT (id) DO NOTHING
            """)
            conn.commit()

        print("✅ DB Init Complete!")

    except Exception as e:
        print("❌ DB init failed:", e)
    finally:
        if conn:
            pool.putconn(conn)


init_db()


# ================= HELPERS =================
def get_db():
    conn = pool.getconn()
    cur = conn.cursor(row_factory=dict_row)
    return conn, cur


def close_db(conn):
    pool.putconn(conn)


def safe_db(func):
    @wraps(func)
    def wrapper(*a, **kw):
        try:
            return func(*a, **kw)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    return wrapper


# ================= SOCKET =================
@socketio.on("driver_gps")
def gps(data):
    socketio.emit("bus_location", data)

# ================= HTML =================
BASE_HTML = """
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bus Booking India</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
.seat{width:45px;height:45px;margin:3px;border-radius:5px;font-weight:bold}
.bus-row{display:flex;flex-wrap:wrap;justify-content:center;gap:5px}
#map{height:400px;margin:20px 0;border-radius:10px}
body{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);min-height:100vh}
.card{border-radius:15px;box-shadow:0 10px 30px rgba(0,0,0,0.3)}
</style>
</head>
<body class="text-white">
<div class="container py-5">
<h2 class="text-center mb-4">🚌 Bus Booking + Live GPS</h2>
{{content|safe}}
<div class="text-center mt-4">
<a href="/" class="btn btn-light btn-lg px-4 me-2">🏠 Home</a>
<a href="/driver/1" class="btn btn-success btn-lg px-4" target="_blank">🚗 Driver GPS</a>
</div>
</div>
<script>
var socket = io();
socket.on("bus_location", d => {
    if(window.map && d.lat){
        if(!window.busMarker){
            window.busMarker = L.marker([d.lat,d.lng],{
                icon:L.divIcon({className:'custom-div-icon',html:'🚌',iconSize:[40,40]})
            }).addTo(window.map).bindPopup("Live Bus");
        }else{
            window.busMarker.setLatLng([d.lat,d.lng]);
        }
    }
});
function bookSeat(seatId, fs, ts, d, sid){
    let name = prompt("Enter Name:"), mobile = prompt("Enter Mobile:");
    if(!name || !mobile) return;

    fetch("/book", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
            sid: sid,       // schedule id add किया
            seat: seatId,
            name: name,
            mobile: mobile,
            from: fs,
            to: ts,
            date: d
        })
    })
    .then(r => r.json())
    .then(r => {
        alert(r.msg);
        if(r.ok) location.reload();
    });
}
</script>
</body>
</html>
"""
# ================= ROUTES =================
@app.route("/")
def home():
    if pool:  # ✅ Pool ready check
        init_db()
    return render_template_string(BASE_HTML, content="""
        <div class="alert alert-success text-center">✅ System Active - Render DB Connected!</div>
        <a href="/buses/1" class="btn btn-success btn-lg">Book Jaipur → Delhi</a>
        """)
    return "✅ Bus Booking App Running"


@app.route("/book", methods=["POST"])
@safe_db
def book():
    data = request.json
    conn, cur = get_db()

    # Check if seat already booked
    cur.execute("""
    SELECT COUNT(*) FROM seat_bookings 
    WHERE schedule_id=%s AND seat_number=%s AND travel_date=%s
    """, (data["sid"], data["seat"], data["date"]))

    if cur.fetchone()[0] > 0:
        close_db(conn)
        return jsonify({"ok": False, "error": "Seat already booked"})

    fare = random.randint(200, 500)

    cur.execute("""
    INSERT INTO seat_bookings (schedule_id,seat_number,passenger_name,mobile,
    from_station,to_station,travel_date,fare)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
    """, (data["sid"], data["seat"], data["name"], data["mobile"], data["from_station"],
          data["to_station"], data["date"], fare))

    conn.commit()
    close_db(conn)
    return jsonify({"ok": True, "fare": fare})


# ================= RUN =================
if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )
