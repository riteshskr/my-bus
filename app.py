import os, random
from datetime import date
from functools import wraps
from flask import Flask, request, jsonify, render_template_string, redirect
from flask_socketio import SocketIO
from flask_compress import Compress
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
import atexit

# ================= APP =================
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key")
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
    timeout=20,
    max_idle=10,
    kwargs={
        "sslmode": "require",
        "connect_timeout": 10,
        "keepalives": 1,
        "keepalives_idle": 60,  # ↑ बढ़ाओ
        "keepalives_interval": 30,
        "keepalives_count": 3
    }
)

print("✅ Connection pool ready")

# ================= CLEANUP =================
@atexit.register
def shutdown_pool():
    print("🛑 Shutting down DB pool...")
    pool.close()

# ================= DB INIT =================
def init_db():
    conn = None
    try:
        conn = pool.getconn()
        cur = conn.cursor()

        # Tables
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

        # Insert default routes if empty
        cur.execute("SELECT COUNT(*) FROM routes")
        if cur.fetchone()[0] == 0:
            # Routes
            routes_data = [
                (1, 'बीकानेर → जयपुर', 336),
                (2, 'बीकानेर → जोधपुर', 252),
                (3, 'जयपुर → जोधपुर', 330)
            ]
            for rid, name, dist in routes_data:
                cur.execute("""
                    INSERT INTO routes (id, route_name, distance_km)
                    VALUES (%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING
                """, (rid, name, dist))

            # Schedules
            schedules_data = [
                (1, 1, 'Volvo AC Sleeper', '08:00'),
                (2, 1, 'Semi Sleeper AC', '10:30'),
                (3, 2, 'Volvo AC Seater', '09:00'),
                (4, 3, 'Deluxe AC', '07:30')
            ]
            for sid, rid, bus, dep in schedules_data:
                cur.execute("""
                    INSERT INTO schedules (id, route_id, bus_name, departure_time)
                    VALUES (%s,%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING
                """, (sid, rid, bus, dep))

            # Route stations
            route_stations_data = [
                (1,'बीकानेर',1), (1,'जयपुर',2),
                (2,'बीकानेर',1), (2,'जोधपुर',2),
                (3,'जयपुर',1), (3,'जोधपुर',2)
            ]
            for rid, station, order in route_stations_data:
                cur.execute("""
                    INSERT INTO route_stations (route_id, station_name, station_order)
                    VALUES (%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING
                """, (rid, station, order))

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
    if conn.closed:
        pool.putconn(conn)
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
BASE_HTML = """<html>
<head><meta name="viewport" content="width=device-width, initial-scale=1">
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
window.currentSid = null;  // ← ये add करें
window.currentDate = null; // ← ये add करें
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
// 🔥 LIVE SEAT UPDATES
socket.on("seat_update", data => {
    if(window.currentSid == data.sid && window.currentDate == data.date){
        // Seat को red करो
        let seatBtn = document.querySelector(`button[onclick*="bookSeat(${data.seat}")`);
        if(seatBtn){
            seatBtn.className = 'btn btn-danger seat';
            seatBtn.disabled = true;
            seatBtn.textContent = data.seat;
        }
    }
});
function bookSeat(seatId, fs, ts, d, sid){
    let name = prompt("Enter Name:"), mobile = prompt("Enter Mobile:");
    if(!name || !mobile) return;
    fetch("/book",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({
        sid: sid, seat: seatId, name: name, mobile: mobile, from: fs, to: ts, date: d
    })})
    .then(r=>r.json())
    .then(r=>{alert(r.msg);if(r.ok)location.reload();});
     if(r.ok){
            // ✅ ये 3 तरीके try करें (कोई भी 1 काम करेगा)
            window.currentSid = sid;      // ← IMPORTANT
            window.currentDate = d;        // ← IMPORTANT  
            location.reload();             // Page refresh
        }
}
</script>
</body>
</html>
"""

# ================= ROUTES =================

@app.route("/")
@safe_db
def home():
    conn, cur = get_db()
    cur.execute("SELECT id, route_name, distance_km FROM routes ORDER BY id")
    routes = cur.fetchall() or []
    close_db(conn)

    if not routes:
        content = '<div class="alert alert-warning text-center">No routes available</div>'
    else:
        content = '<div class="text-center mb-4"><h4>Available Routes</h4></div>'
        for r in routes:
            content += f'''
            <div class="card bg-info mb-3">
                <div class="card-body">
                    <h6>{r["route_name"]} — {r["distance_km"]} km</h6>
                    <a href="/buses/{r["id"]}" class="btn btn-success w-100">Book Seats</a>
                </div>
            </div>
            '''
    return render_template_string(BASE_HTML, content=content)

@app.route("/buses/<int:rid>")
@safe_db
def buses(rid):
    conn, cur = get_db()
    cur.execute("SELECT id, bus_name, departure_time FROM schedules WHERE route_id=%s ORDER BY departure_time",(rid,))
    buses_data = cur.fetchall() or []
    close_db(conn)

    html = '<div class="alert alert-info text-center">No Buses for this route</div>'
    if buses_data:
        html = '<div class="text-center mb-4"><h4>Available Buses</h4></div>'
        for bus in buses_data:
            html += f'''
            <div class="card bg-info mb-3">
                <div class="card-body">
                    <h6>{bus["bus_name"]}</h6>
                    <p>{bus["departure_time"]}</p>
                    <a href="/select/{bus["id"]}" class="btn btn-success w-100">Book Seats</a>
                </div>
            </div>
            '''
    return render_template_string(BASE_HTML, content=html)

@app.route("/select/<int:sid>", methods=["GET","POST"])
@safe_db
def select(sid):
    conn, cur = get_db()
    cur.execute("SELECT route_id FROM schedules WHERE id=%s",(sid,))
    row = cur.fetchone()
    route_id = row["route_id"] if row else 1

    cur.execute("SELECT station_name FROM route_stations WHERE route_id=%s ORDER BY station_order",(route_id,))
    stations = [r["station_name"] for r in cur.fetchall()]
    close_db(conn)

    opts = "".join(f"<option>{s}</option>" for s in stations)
    today = date.today().isoformat()

    if request.method=="POST":
        fs = request.form["from"]
        ts = request.form["to"]
        d = request.form["date"]
        return redirect(f"/seats/{sid}?fs={fs}&ts={ts}&d={d}")

    form = f"""
    <div class="card mx-auto" style="max-width:500px">
        <div class="card-body">
            <form method="POST">
                <label>From:</label>
                <select name="from" required>{opts}</select>
                <label>To:</label>
                <select name="to" required>{opts}</select>
                <label>Date:</label>
                <input type="date" name="date" value="{today}" min="{today}" required>
                <button class="btn btn-success w-100 mt-3">View Seats</button>
            </form>
        </div>
    </div>
    """
    return render_template_string(BASE_HTML, content=form)

@app.route("/seats/<int:sid>")
@safe_db
def seats(sid):
    fs = request.args.get("fs","बीकानेर")
    ts = request.args.get("ts","जयपुर")
    d = request.args.get("d", date.today().isoformat())

    conn, cur = get_db()
    cur.execute("""
        SELECT seat_number 
        FROM seat_bookings 
        WHERE schedule_id=%s AND travel_date=%s AND status='confirmed'
    """,(sid,d))
    booked = [r["seat_number"] for r in cur.fetchall()]
    close_db(conn)

    seat_buttons = ""
    for i in range(1,41):
        if i in booked:
            seat_buttons += f'<button class="btn btn-danger seat" disabled>{i}</button>'
        else:
            seat_buttons += f'<button class="btn btn-success seat" onclick="bookSeat({i},\'{fs}\',\'{ts}\',\'{d}\',{sid})">{i}</button>'

    html = f"""
    <div class="text-center">
        <h4>{fs} → {ts} | {d}</h4>
        <div id="map"></div>
        <div class="bus-row mt-3">{seat_buttons}</div>
    </div>
    <script>
    window.map = L.map('map').setView([26.9124, 75.7873], 7);
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{ maxZoom: 18 }}).addTo(map);
    window.busMarker = L.marker([26.9124,75.7873], {{
        icon: L.divIcon({{className:'custom-div-icon',html:'🚌',iconSize:[40,40]}})
    }}).addTo(map).bindPopup("Live Bus Location");
    </script>
    """
    return render_template_string(BASE_HTML, content=html)

@app.route("/driver/<int:sid>")
@safe_db
def driver(sid):
    return f"""
    <html>
    <head><title>Driver GPS</title></head>
    <body style="text-align:center;font-family:sans-serif">
        <h2>🚗 Driver Live GPS (Bus {sid})</h2>
        <p>Phone में ये page खोलो और नीचे वाला बटन दबाओ</p>
        <button onclick="start()" style="padding:15px;font-size:18px;">Start Sending Location</button>
        <p id="status"></p>
        <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
        <script>
            var socket = io();
            function start(){{
                if(!navigator.geolocation){{
                    alert("GPS not supported");
                    return;
                }}
                document.getElementById("status").innerText = "📡 Sending GPS...";
                navigator.geolocation.watchPosition(
                    function(pos){{
                        socket.emit("driver_gps", {{
                            sid: {sid},
                            lat: pos.coords.latitude,
                            lng: pos.coords.longitude
                        }});
                    }},
                    function(err){{
                        alert("GPS Error: " + err.message);
                    }},
                    {{ enableHighAccuracy: true }}
                );
            }}
        </script>
    </body>
    </html>
    """

@app.route("/book", methods=["POST"])
@safe_db
def book():
    data = request.get_json() or {}
    conn, cur = get_db()
    fare = random.randint(250,450)
    cur.execute("""
        INSERT INTO seat_bookings (schedule_id, seat_number, passenger_name, mobile, from_station, to_station, travel_date, fare)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
    """, (data.get("sid"), data.get("seat"), data.get("name"), data.get("mobile"), data.get("from"), data.get("to"), data.get("date"), fare))
    conn.commit()
    close_db(conn)
    socketio.emit("seat_update", {
        "sid": data.get("sid"),
        "seat": data.get("seat"),
        "date": data.get("date")
    }, broadcast=True)

    return jsonify({"ok": True, "msg": f"✅ Seat {data.get('seat')} booked! Fare ₹{fare}"})

# ================= RUN =================
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT",10000)))
