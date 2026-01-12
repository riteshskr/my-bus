import os
import random
from datetime import date
from functools import wraps
import psycopg_pool
from flask import Flask, render_template_string, request, redirect, jsonify
from flask_socketio import SocketIO
from psycopg import connect, rows
from flask_compress import Compress
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
# ================= APP =================
app = Flask(__name__)
app.secret_key = "super-secret-key"
Compress(app)
app.jinja_env.auto_reload = False

@app.after_request
def after_request(response):
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response
socketio = SocketIO(app, cors_allowed_origins="*", async_mode=None)

# ================= DB CONFIG =================

"""DB_CONFIG = {
    "host": os.getenv("DB_HOST","dpg-d5g7u19r0fns739mbng0-a.oregon-postgres.render.com"),
    "dbname": os.getenv("DB_NAME","busdb1_yl2r"),
    "user": os.getenv("DB_USER","busdb1_yl2r_user"),
    "password": os.getenv("DB_PASSWORD","49Tv97dLOzE8yd0WlYyns49KnyB646py"),
    "port": int(os.getenv("DB_PORT", 5432)),
    "sslmode": "require"
}"""
DATABASE_URL = os.getenv("DATABASE_URL")
pool = None

if DATABASE_URL:
    try:
        from psycopg_pool import ConnectionPool
        pool = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=4,  # ↑ बढ़ाएं
            max_size=10,  # ↑ मुख्य समस्या यहाँ!
            max_waiting=10,  # ↑ बढ़ाएं
            timeout=20.0,  # ↑ समय बढ़ाएं
            max_idle=600,
            reconnect_timeout=300,
            open=True
        )
        print("✅ ConnectionPool ready!")
    except Exception as e:
        print(f"❌ Pool error: {e}")
        pool = None
else:
    print("❌ DATABASE_URL missing!")


# अब पुराना get_db function replace करें:
def get_db():
    if not pool:
        raise Exception("No database pool available")
    conn = pool.getconn()
    cur = conn.cursor(row_factory=dict_row)
    return conn, cur

def close_db(conn):
    if conn:
        pool.putconn(conn)


# ================= INIT DB =================

def init_db():
    if not pool:
        print("⚠️ Skipping DB init - no pool")
        return
    try:
        conn, cur = get_db()
        # गलत if-block हटाएं
        cur.execute("CREATE TABLE IF NOT EXISTS routes ...")  # बाकी कोड
        conn.commit()
        close_db(conn)
    except Exception as e:
        print(f"Init DB error: {e}")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS routes (
            id SERIAL PRIMARY KEY,
            route_name VARCHAR(100),
            distance_km INT
        )
        """)
        # Schedules
        cur.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id SERIAL PRIMARY KEY,
            route_id INT,
            bus_name VARCHAR(100),
            departure_time TIME,
            current_lat double precision,
            current_lng double precision,
            created_at timestamp DEFAULT NOW(),
            updated_at timestamp DEFAULT NOW(),
            seating_rate double precision,
            single_sleeper_rate double precision,
            double_sleeper_rate double precision,
            total_seats INT DEFAULT 40
        )
        """)
        # Seat Bookings
        cur.execute("""
        CREATE TABLE IF NOT EXISTS seat_bookings (
            id SERIAL PRIMARY KEY,
            schedule_id INT,
            seat_number INT,
            passenger_name VARCHAR(100),
            mobile VARCHAR(15),
            from_station VARCHAR(50),
            to_station VARCHAR(50),
            travel_date DATE,
            status VARCHAR(20) DEFAULT 'confirmed',
            fare INT,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """)
        # Route Stations
        cur.execute("""
        CREATE TABLE IF NOT EXISTS route_stations (
            id SERIAL PRIMARY KEY,
            route_id INT,
            station_name VARCHAR(50),
            station_order INT
        )
        """)
        conn.commit()

        # ✅ Add sample route + buses if missing
        cur.execute("SELECT COUNT(*) AS count FROM schedules WHERE route_id=1")
        if cur.fetchone()['count'] == 0:
            # Route
            cur.execute("INSERT INTO routes (id, route_name, distance_km) VALUES (1,'Jaipur-Delhi',280) ON CONFLICT DO NOTHING")
            # Buses
            buses = [
                (1,'Volvo AC Sleeper','08:00:00'),
                (2,'Semi Sleeper AC','10:30:00'),
                (3,'Volvo AC Seater','14:00:00')
            ]
            for bus_id, bus_name, t in buses:
                cur.execute("""
                INSERT INTO schedules (id, route_id, bus_name, departure_time) 
                VALUES (%s,1,%s,%s) ON CONFLICT DO NOTHING
                """,(bus_id,bus_name,t))
            # Stations
            stations = [('Jaipur',1),('Ajmer',2),('Pushkar',3),('Kishangarh',4),('Delhi',5)]
            for st, order in stations:
                cur.execute("INSERT INTO route_stations (route_id, station_name, station_order) VALUES (1,%s,%s) ON CONFLICT DO NOTHING",(st,order))
            conn.commit()
        close_db(conn)
    except Exception as e:
        print(f"Init DB error: {e}")

# ================= SAFE DB DECORATOR =================
def safe_db(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            if pool.status == psycopg_pool.Status.CLOSED:
                pool.open()
            return func(*args, **kwargs)
        except Exception as e:
            return render_template_string(BASE_HTML,
                content=f'<div class="alert alert-danger">❌ DB Error: {str(e)}</div>'
            )
    return wrapper
#======== gps =========
@socketio.on("driver_gps")
def handle_gps(data):
    # data = { sid, lat, lng }
    print("📡 GPS:", data)

    # सभी passengers को भेजो
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
@safe_db
def home():
    if pool:  # ✅ Pool ready check
        init_db()
    return render_template_string(BASE_HTML, content="""
        <div class="alert alert-success text-center">✅ System Active - Render DB Connected!</div>
        <a href="/buses/1" class="btn btn-success btn-lg">Book Jaipur → Delhi</a>
        """)

@app.route("/buses/<int:rid>")
@safe_db
def buses(rid):
    conn, cur = get_db()
    cur.execute("SELECT id,bus_name,departure_time FROM schedules WHERE route_id=%s ORDER BY departure_time",(rid,))
    buses_data = cur.fetchall()
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
    fs = request.args.get("fs","Jaipur")
    ts = request.args.get("ts","Delhi")
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

        <!-- 🗺 LIVE MAP -->
        <div id="map"></div>

        <!-- 🪑 SEATS -->
        <div class="bus-row mt-3">
            {seat_buttons}
        </div>
    </div>

    <script>
    // Default Jaipur Location
   window.map = L.map('map').setView([26.9124, 75.7873], 7);

    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
        maxZoom: 18
    }}).addTo(map);

    // 🚌 Bus Marker
    window.busMarker = L.marker([26.9124,75.7873], {{
        icon: L.divIcon({{
            className:'custom-div-icon',
            html:'🚌',
            iconSize:[40,40]
        }})
    }}).addTo(map).bindPopup("Live Bus Location");
    </script>
    """

    return render_template_string(BASE_HTML, content=html)

#========= driver=========
@app.route("/driver/<int:sid>")
@safe_db
def driver(sid):
    return f"""
    <html>
    <head><title>Driver GPS</title></head>
    <body style="text-align:center;font-family:sans-serif">
        <h2>🚗 Driver Live GPS (Bus {sid})</h2>
        <p>Phone में ये page खोलो और नीचे वाला बटन दबाओ</p>

        <button onclick="start()" style="padding:15px;font-size:18px;">
            Start Sending Location
        </button>

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
                    {{
                        enableHighAccuracy: true
                    }}
                );
            }}
        </script>
    </body>
    </html>
    """


#=======seat book ==========
@app.route("/book", methods=["POST"])
@safe_db
def book():
    data = request.get_json() or {}
    seat = data.get("seat")
    name = data.get("name")
    mobile = data.get("mobile")
    from_st = data.get("from_station")
    to_st = data.get("to_station")
    travel_date = data.get("date")
    fare = random.randint(250,450)
    conn, cur = get_db()
    cur.execute("""
        INSERT INTO seat_bookings (schedule_id, seat_number, passenger_name, mobile, from_station, to_station, travel_date, fare)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
    """, (data.get("sid"), seat, name, mobile, from_st, to_st, travel_date, fare))
    conn.commit()
    close_db(conn)
    return jsonify(ok=True,msg=f"✅ Seat {seat} Booked! Fare ₹{fare}")

# ================= RUN =================
if __name__=="__main__":
    print("🚀 Bus App Starting on Render...")
    # socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT",5000)))  # ❌ DISABLED
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)), debug=False)  # ✅ Gunicorn के लिए
