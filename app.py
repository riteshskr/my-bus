import os
import random
from datetime import date
from functools import wraps
from flask import Flask, render_template_string, request, redirect, jsonify
from flask_socketio import SocketIO
from psycopg import connect, rows
from flask_compress import Compress
from psycopg.rows import dict_row
db_initialized = False
# ================= APP =================
app = Flask(__name__)
app.secret_key = "super-secret-key"
Compress(app)
app.jinja_env.auto_reload = False

@app.after_request
def after_request(response):
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

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

# अब पुराना get_db function replace करें:

def get_db():
    """No pooling - direct connection for Render Free Tier"""
    if not DATABASE_URL:
        raise Exception("DATABASE_URL missing")

    conn = connect(DATABASE_URL)
    cur = conn.cursor(row_factory=dict_row)
    return conn, cur


def close_db(conn):
    if conn:
        conn.close()


# ================= INIT DB =================

def init_db():
        # Manual pool open

    conn = None
    try:
        conn, cur = get_db()
        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_seat_bookings_lookup 
        ON seat_bookings(schedule_id, travel_date, seat_number)
        """)
        # Tables CREATE
        
        cur.execute("""
        CREATE TABLE IF NOT EXISTS routes (
            id SERIAL PRIMARY KEY, route_name VARCHAR(100), distance_km INT
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id SERIAL PRIMARY KEY, route_id INT, bus_name VARCHAR(100),
            departure_time TIME, current_lat double precision, current_lng double precision,
            created_at timestamp DEFAULT NOW(), seating_rate double precision,
            total_seats INT DEFAULT 40
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS seat_bookings (
            id SERIAL PRIMARY KEY, schedule_id INT, seat_number INT,
            passenger_name VARCHAR(100), mobile VARCHAR(15), from_station VARCHAR(50),
            to_station VARCHAR(50), travel_date DATE, status VARCHAR(20) DEFAULT 'confirmed',
            fare INT, created_at TIMESTAMP DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS route_stations (
            id SERIAL PRIMARY KEY, route_id INT, station_name VARCHAR(50), station_order INT
        )""")

        conn.commit()

        # Sample data
        cur.execute("SELECT COUNT(*) AS count FROM schedules WHERE route_id=1")
        if cur.fetchone()['count'] == 0:
            cur.execute(
                "INSERT INTO routes (id, route_name, distance_km) VALUES (1,'Jaipur-Delhi',280) ON CONFLICT DO NOTHING")
            cur.execute(
                "INSERT INTO schedules (id, route_id, bus_name, departure_time) VALUES (1,1,'Volvo AC Sleeper','08:00:00') ON CONFLICT DO NOTHING")
            cur.execute(
                "INSERT INTO schedules (id, route_id, bus_name, departure_time) VALUES (2,1,'Semi Sleeper AC','10:30:00') ON CONFLICT DO NOTHING")
            cur.execute(
                "INSERT INTO route_stations (route_id, station_name, station_order) VALUES (1,'Jaipur',1),(1,'Delhi',2) ON CONFLICT DO NOTHING")
            conn.commit()
        print("✅ DB Init Complete!")

    except Exception as e:
        print(f"❌ Init DB error: {e}")
    finally:
        if conn:
            close_db(conn)

# ================= SAFE DB DECORATOR =================
def safe_db(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            print("❌ DB Error:", e)
            return render_template_string(BASE_HTML,
                content=f'<div class="alert alert-danger text-center">❌ डेटाबेस त्रुटि: {str(e)}</div>'
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

/* 🚌 Live Bus GPS */
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

/* 🔴 Realtime Seat Sync */
socket.on("seat_update", function(data){
    if(window.currentSid != data.sid || window.currentDate != data.date){
        return;
    }

    let btn = document.getElementById("seat-" + data.seat);
    if(btn){
        btn.classList.remove("btn-success");
        btn.classList.add("btn-danger");
        btn.disabled = true;
        btn.innerText = data.seat;
    }
});

/* 🪑 Seat Booking */
function bookSeat(seatId, fs, ts, d, sid){
    let name = prompt("Enter Name:");
    let mobile = prompt("Enter Mobile:");

    if(!name || !mobile) return;

    fetch("/book", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
            sid: sid,
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
    global db_initialized
    if not db_initialized:
        init_db()
        db_initialized = True

    # सभी routes show करें
    conn, cur = get_db()
    cur.execute("SELECT id, route_name FROM routes ORDER BY id")
    routes = cur.fetchall()
    close_db(conn)

    routes_html = ""
    for r in routes:
        routes_html += f'''
        <a href="/buses/{r["id"]}" class="btn btn-success btn-lg mb-2 d-block">
            Book {r["route_name"]}
        </a>
        '''

    return render_template_string(BASE_HTML, content="""
        <div class="alert alert-success text-center">✅ सभी Routes Active!</div>
        """ + routes_html)

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

/ seats / < int: sid > Complete
Fixed
Code(Real - time
instant
red
seats):
python


@app.route("/seats/<int:sid>")
@safe_db
def seats(sid):
    fs = request.args.get("fs", "बीकानेर")
    ts = request.args.get("ts", "जयपुर")
    d = request.args.get("d", date.today().isoformat())

    conn, cur = get_db()

    # Booked seats
    cur.execute("""
        SELECT seat_number 
        FROM seat_bookings 
        WHERE schedule_id=%s AND travel_date=%s AND status='confirmed'
    """, (sid, d))
    booked = [r["seat_number"] for r in cur.fetchall()]

    # Route info
    cur.execute("SELECT route_id, bus_name FROM schedules WHERE id=%s", (sid,))
    route_info = cur.fetchone() or {}

    close_db(conn)

    seat_buttons = ""
    for i in range(1, 41):
        if i in booked:
            seat_buttons += f'''
            <button id="seat-{i}" class="btn btn-danger seat" disabled>X{i}</button>
            '''
        else:
            seat_buttons += f'''
            <button id="seat-{i}" class="btn btn-success seat" onclick="bookSeat({i},'{fs}','{ts}','{d}',{sid})">
                {i}
            </button>
            '''

    html = f"""
    <div class="row text-center">
        <div class="col-md-12">
            <h4 class="mb-4">🚌 {route_info.get('bus_name', 'Bus')} | {fs} → {ts}</h4>
            <p class="text-muted">📅 {d} | 💺 {40 - len(booked)} seats available</p>
        </div>
    </div>

    <!-- Live GPS Map -->
    <div class="card bg-dark mb-4">
        <div class="card-body">
            <div id="map" style="height:300px;border-radius:10px"></div>
        </div>
    </div>

    <!-- Seat Layout -->
    <div class="card">
        <div class="card-body p-3">
            <h6 class="text-center mb-3">🪑 Seat Selection</h6>
            <div class="bus-row justify-content-center">
                {seat_buttons}
            </div>
        </div>
    </div>

    <script>
    // 🔴 Current page context
    window.currentSid = {sid};
    window.currentDate = '{d}';
    console.log('🎯 Seats page loaded:', window.currentSid, window.currentDate);

    // 🗺️ Initialize Map
    window.map = L.map('map').setView([27.5, 75.0], 7);
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
        maxZoom: 18, attribution: '© OpenStreetMap'
    }}).addTo(window.map);

    window.busMarker = L.marker([27.39, 75.14], {{
        icon: L.divIcon({{
            className: 'custom-div-icon text-primary',
            html: '<div style=\\"font-size:30px\\">🚌</div>',
            iconSize: [40,40], iconAnchor: [20,20]
        }})
    }}).addTo(window.map).bindPopup('Live Bus Location');

    // 🚀 Real-time Socket Events
    socket.on('connect', function() {{
        console.log('✅ Socket Connected!');
    }});

    socket.on('bus_location', function(data) {{
        console.log('📡 GPS Update:', data);
        if(data.lat && data.lng && window.map) {{
            if(window.busMarker) {{
                window.busMarker.setLatLng([data.lat, data.lng]);
            }}
        }}
    }});

    // 🔥 INSTANT Seat Update (Real-time)
    socket.on('seat_update', function(data) {{
        console.log('🔴 SEAT UPDATE RECEIVED:', data);

        // Match current page
        if(window.currentSid != data.sid || window.currentDate != data.date) {{
            console.log('⏭️ Different bus/date, ignoring');
            return;
        }}

        let seatBtn = document.getElementById('seat-' + data.seat);
        if(seatBtn) {{
            seatBtn.classList.remove('btn-success', 'btn-outline-success');
            seatBtn.classList.add('btn-danger');
            seatBtn.disabled = true;
            seatBtn.innerHTML = '<i class="fas fa-user-check"></i> X' + data.seat;
            console.log('✅ Seat ' + data.seat + ' turned RED instantly!');

            // Visual feedback
            seatBtn.style.transform = 'scale(1.1)';
            setTimeout(() => seatBtn.style.transform = 'scale(1)', 200);
        }} else {{
            console.log('❌ Seat button not found:', data.seat);
        }}
    }});

    // 🪑 Book Seat Function (Improved)
    function bookSeat(seatId, fromStation, toStation, travelDate, scheduleId) {{
        console.log('🎫 Booking seat:', seatId);

        let name = prompt('👤 Passenger Name:');
        if(!name || name.trim() === '') {{
            alert('❌ नाम भरें!');
            return;
        }}

        let mobile = prompt('📱 Mobile Number:');
        if(!mobile || !/^[6-9]\\d{{9}}$/.test(mobile)) {{
            alert('❌ Valid mobile number दें!');
            return;
        }}

        // Show loading
        let seatBtn = document.getElementById('seat-' + seatId);
        let originalText = seatBtn.innerHTML;
        seatBtn.innerHTML = '⏳ Booking...';
        seatBtn.disabled = true;

        fetch('/book', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{
                sid: scheduleId,
                seat: seatId,
                name: name.trim(),
                mobile: mobile,
                from: fromStation,
                to: toStation,
                date: travelDate
            }})
        }})
        .then(response => {{
            console.log('📡 Response status:', response.status);
            return response.json();
        }})
        .then(result => {{
            console.log('📋 Booking result:', result);
            if(result.ok) {{
                alert('🎉 ' + result.msg);
                // Socket update भी आएगा automatically
            }} else {{
                alert('❌ ' + result.msg);
                // Re-enable button
                seatBtn.innerHTML = originalText;
                seatBtn.disabled = false;
            }}
        }})
        .catch(error => {{
            console.error('❌ Fetch error:', error);
            alert('❌ Network error! फिर कोशिश करें।');
            seatBtn.innerHTML = originalText;
            seatBtn.disabled = false;
        }});
    }}
    </script>
    """


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
def book():
    print("🔥 BOOK REQUEST RECEIVED!")  # Debug log

    try:
        data = request.get_json()
        print(f"📋 Data: {data}")

        if not data:
            return jsonify({"ok": False, "msg": "❌ No data received"})

        conn, cur = get_db()

        # ✅ SIMPLE CHECK - बस ये ही चाहिए
        cur.execute("""
            SELECT 1 FROM seat_bookings 
            WHERE schedule_id=%s AND travel_date=%s AND seat_number=%s 
            AND status='confirmed'
        """, (data["sid"], data["date"], data["seat"]))

        if cur.fetchone():
            close_db(conn)
            return jsonify({"ok": False, "msg": "❌ Seat पहले से बुक है!"})

        # ✅ FIXED INSERT - सारे fields match
        cur.execute("""
            INSERT INTO seat_bookings 
            (schedule_id, travel_date, seat_number, passenger_name, mobile, 
             from_station, to_station, status, fare, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'confirmed',500,NOW())
        """, (data["sid"], data["date"], data["seat"], data["name"],
              data["mobile"], data["from"], data["to"]))

        conn.commit()
        print(f"✅ Seat {data['seat']} BOOKED!")

        # Socket emit AFTER commit
        socketio.emit("seat_update", {
            "sid": data["sid"], "seat": data["seat"], "date": data["date"]
        })

        close_db(conn)
        return jsonify({"ok": True, "msg": f"✅ Seat {data['seat']} बुक हो गई!"})

    except Exception as e:
        print(f"❌ BOOK ERROR: {e}")
        if 'conn' in locals():
            close_db(conn)
        return jsonify({"ok": False, "msg": f"❌ त्रुटि: {str(e)}"})



# ================= RUN =================
if __name__ == "__main__":
    print("🚀 Bus App Starting on Render...")
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
    #app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)), debug=False)  # ✅ Gunicorn के लिए
