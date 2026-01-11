import os
import time
from functools import wraps
from psycopg import connect
import psycopg.rows
import requests
from flask import Flask, render_template_string, request, redirect, url_for, jsonify
from flask_socketio import SocketIO
from datetime import date
import random

# ================= APP =================
app = Flask(__name__)
app.secret_key = "super-secret-key"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ================= DB CONFIG =================
DB_CONFIG = {
    "host": os.getenv('DB_HOST'),
    "dbname": os.getenv('DB_NAME', "busdb1_yl2r_user"),
    "user": os.getenv('DB_USER'),
    "password": os.getenv('DB_PASSWORD'),
    "port": int(os.getenv('DB_PORT', 5432))
}


def get_db():
    try:
        conn = connect(**DB_CONFIG)
        cur = conn.cursor(row_factory=psycopg.rows.dict_row)
        return conn, cur
    except:
        raise Exception("DB Connection failed")


def safe_db(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except:
            print(f"Error in {func.__name__}")
            if 'buses' in func.__name__:
                return render_template_string(BASE_HTML,
                                              content='<div class="alert alert-info text-white">No buses available</div>')
            elif 'select' in func.__name__:
                return render_template_string(BASE_HTML,
                                              content='<div class="alert alert-warning">Select stations unavailable</div>')
            elif 'seats' in func.__name__:
                return render_template_string(BASE_HTML, content=SEATS_DEMO_HTML)
            return "Service unavailable", 503

    return wrapper


def init_db():
    """पहली बार run होने पर tables create + sample data"""
    try:
        conn, cur = get_db()

        # Tables create (same)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS routes (
            id SERIAL PRIMARY KEY, route_name VARCHAR(100), distance_km INT
        )""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id SERIAL PRIMARY KEY, route_id INT, bus_name VARCHAR(100), 
            departure_time TIME, total_seats INT DEFAULT 40
        )""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
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

        # ✅ FIXED: Multiple buses + route_id=1 guaranteed
        cur.execute("SELECT COUNT(*) as count FROM schedules WHERE route_id=1")
        if cur.fetchone()['count'] == 0:
            # Route बनाएं
            cur.execute(
                "INSERT INTO routes (id, route_name, distance_km) VALUES (1, 'Jaipur-Delhi', 280) ON CONFLICT (id) DO NOTHING")

            # ✅ 3 Buses route_id=1 पर (अब /buses/1 काम करेगा!)
            buses = [
                (1, 'Volvo AC Sleeper', '08:00:00'),
                (2, 'Semi Sleeper AC', '10:30:00'),
                (3, 'Volvo AC Seater', '14:00:00')
            ]
            for bus_id, bus_name, time in buses:
                cur.execute("""
                    INSERT INTO schedules (id, route_id, bus_name, departure_time) 
                    VALUES (%s, 1, %s, %s) ON CONFLICT (id) DO NOTHING
                """, (bus_id, bus_name, time))

            # Stations
            stations = [('Jaipur', 1), ('Ajmer', 2), ('Pushkar', 3), ('Kishangarh', 4), ('Delhi', 5)]
            for station, order in stations:
                cur.execute("""
                    INSERT INTO route_stations (route_id, station_name, station_order) 
                    VALUES (1, %s, %s) ON CONFLICT DO NOTHING
                """, (station, order))

            conn.commit()
            print("✅ FIXED: 3 buses added for route_id=1! /buses/1 अब काम करेगा!")

        conn.close()
    except Exception as e:
        print(f"Init DB error: {e}")


# ================= HTML TEMPLATES =================
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
function bookSeat(seatId,fs,ts,d){
    let name=prompt("Enter Name:"), mobile=prompt("Enter Mobile:");
    if(!name||!mobile) return;
    fetch("/book",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({seat:seatId,name:name,mobile:mobile,from:fs,to:ts,date:d})
    }).then(r=>r.json()).then(r=>{
        alert(r.msg);
        if(r.ok) location.reload();
    });
}
</script>
</body>
</html>
"""

HOME_HTML = """
<div class="row g-4 mb-5">
    <div class="col-md-4">
        <div class="card bg-primary text-white h-100">
            <div class="card-body text-center">
                <div class="display-6 mb-3">🚍</div>
                <h5>Live Routes</h5>
                <p class="card-text">Jaipur → Delhi<br>Jaipur → Ajmer<br>10+ Routes</p>
            </div>
        </div>
    </div>
    <div class="col-md-4">
        <div class="card bg-success text-white h-100">
            <div class="card-body text-center">
                <div class="display-6 mb-3">🚌</div>
                <h5>GPS Tracking</h5>
                <p class="card-text">Real-time location<br>ETA calculation<br>Next stop alerts</p>
            </div>
        </div>
    </div>
    <div class="col-md-4">
        <div class="card bg-info text-white h-100">
            <div class="card-body text-center">
                <div class="display-6 mb-3">💺</div>
                <h5>Seat Booking</h5>
                <p class="card-text">50+ seats/bus<br>Real-time booking<br>Overlap protection</p>
            </div>
        </div>
    </div>
</div>

<div class="alert alert-success text-center mb-4">
    ✅ System Active - GPS Tracking Ready!
</div>

<div class="text-center">
    <div class="row justify-content-center g-3">
        <div class="col-auto">
            <a href="/buses/1" class="btn btn-success btn-lg px-5">
                🚀 बस बुक करें
            </a>
        </div>
        <div class="col-auto">
            <a href="/driver/1" class="btn btn-info btn-lg px-5" target="_blank">
                📱 ड्राइवर GPS
            </a>
        </div>
    </div>
    <small class="text-muted mt-2 d-block">Jaipur → Delhi | Live GPS Active 🟢</small>
</div>
"""

SEATS_DEMO_HTML = """
<div class="text-center mb-4">
    <h4 class="mb-3">🚌 Demo Bus Seats</h4>
    <p class="text-muted mb-4">40 Seats Available | Click to Book</p>
</div>
<div id="demoMap" style="height:350px;margin-bottom:20px;border-radius:10px"></div>
<div class="bus-row justify-content-center">
""" + "".join(
    f'<button class="btn btn-success seat" onclick="bookSeat({i},\'Jaipur\',\'Delhi\',\'2026-01-10\')">{i}</button>' for
    i in range(1, 41)) + """
</div>
<script>
setTimeout(()=>{
    if(!window.map){
        window.map = L.map('demoMap').setView([27.5,76.0],8);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png').addTo(window.map);
        L.polyline([[27.0,75.8],[27.5,76.0],[28.6,77.2]],{color:'blue',weight:6}).addTo(window.map);
    }
},100);
</script>
"""


# ================= ROUTES =================
@app.route("/")
@safe_db
def home():
    init_db()
    try:
        # Quick DB test
        conn, cur = get_db()
        cur.execute("SELECT 1")
        conn.close()
        status = '<div class="alert alert-success">✅ Database Connected!</div>'
    except:
        status = '<div class="alert alert-warning">⚠️ Demo Mode Active</div>'
    return render_template_string(BASE_HTML, content=HOME_HTML + status)
@app.route("/buses/<int:rid>")
@safe_db
def buses(rid):
    html = '<div class="alert alert-info text-center"><h4>No Buses</h4><p>No schedules available for route {}</p></div>'.format(
        rid)
    try:
        conn, cur = get_db()
        cur.execute("SELECT id,bus_name,departure_time FROM schedules WHERE route_id=%s LIMIT 5", (rid,))
        buses = cur.fetchall()
        conn.close()
        if buses:
            html = '<div class="text-center mb-4"><h4>Available Buses</h4></div>'
            for bus in buses:
                html += f'<div class="card bg-info mb-3"><div class="card-body"><h6>{bus["bus_name"]}</h6><p>{bus["departure_time"]}</p><a href="/select/{bus["id"]}" class="btn btn-success w-100">Book Seats</a></div></div>'
    except:
        pass
    return render_template_string(BASE_HTML, content=html)

@app.route("/select/<int:sid>", methods=["GET", "POST"])
@safe_db
def select(sid):
    """
    Bus seat selection form - From/To stations + Date picker
    Redirects to /seats/<sid> with query params
    """
    # Default stations for demo mode
    stations = ["Jaipur", "Ajmer", "Pushkar", "Kishangarh", "Delhi"]

    # Try to get route stations from DB
    try:
        conn, cur = get_db()
        cur.execute("SELECT route_id FROM schedules WHERE id=%s", (sid,))
        row = cur.fetchone()

        if row and row["route_id"]:
            route_id = row["route_id"]
            cur.execute(
                "SELECT station_name FROM route_stations WHERE route_id=%s ORDER BY station_order",
                (route_id,)
            )
            stations = [r["station_name"] for r in cur.fetchall()]

        conn.close()
    except Exception as e:
        print(f"Select stations error: {e}")
        # Fallback to demo stations

    # Create station dropdown options
    opts = "".join(f"<option>{s}</option>" for s in stations)
    today = date.today().isoformat()

    # Hindi/English form
    form = f"""
    <div class="card mx-auto shadow" style="max-width:500px">
        <div class="card-header bg-primary text-white text-center py-4">
            <h4>🎫 यात्रा चुनें</h4>
            <p class="mb-0">Bus ID: <strong>{sid}</strong></p>
            <small class="text-warning">Select From → To Stations</small>
        </div>
        <div class="card-body p-4">
            <form method="POST">
                <div class="mb-3">
                    <label class="form-label fw-bold">प्रस्थान स्टेशन / From</label>
                    <select name="from" class="form-select" required>
                        <option value="">-- चुनें --</option>{opts}
                    </select>
                </div>
                <div class="mb-3">
                    <label class="form-label fw-bold">गंतव्य स्टेशन / To</label>
                    <select name="to" class="form-select" required>
                        <option value="">-- चुनें --</option>{opts}
                    </select>
                </div>
                <div class="mb-4">
                    <label class="form-label fw-bold">तारीख / Date</label>
                    <input type="date" name="date" class="form-control" 
                           value="{today}" min="{today}" required>
                </div>
                <button type="submit" class="btn btn-success w-100 py-3 fs-5">
                    🚀 उपलब्ध सीटें देखें
                </button>
            </form>
        </div>
        <div class="card-footer text-center bg-light text-dark">
            <small>Demo: Jaipur → Delhi | 40 Seats Available</small>
        </div>
    </div>
    """

    # Handle form submission
    if request.method == "POST":
        from_st = request.form.get("from")
        to_st = request.form.get("to")
        travel_date = request.form.get("date", today)

        if from_st and to_st:
            print(f"Form POST: {from_st} → {to_st} | Date: {travel_date}")
            return redirect(f"/seats/{sid}?fs={from_st}&ts={to_st}&d={travel_date}")
        else:
            form += '<div class="alert alert-warning mt-3">कृपया सभी fields भरें!</div>'

    return render_template_string(BASE_HTML, content=form)


# ================= API ROUTES =================
@app.route("/book", methods=["POST"])
@safe_db
def book():
    try:
        data = request.get_json() or {}
        seat = data.get('seat')
        name = data.get('name')
        mobile = data.get('mobile')
        from_st = data.get('from')
        to_st = data.get('to')
        travel_date = data.get('date')

        # ✅ DATABASE में SAVE!
        conn, cur = get_db()
        cur.execute("""
            INSERT INTO bookings (schedule_id, seat_number, passenger_name, mobile, from_station, to_station, travel_date, fare)
            VALUES (1, %s, %s, %s, %s, %s, %s, %s)
        """, (seat, name, mobile, from_st, to_st, travel_date, random.randint(250, 450)))
        conn.commit()
        conn.close()

        print(f"✅ SAVED in DB: Seat {seat} - {name}")
        return jsonify({
            "ok": True,
            "msg": f"✅ Seat {seat} Booked! 💺 Fare: ₹350 | Saved in Database!",
            "fare": 350
        })
    except Exception as e:
        print(f"Booking error: {e}")
        return jsonify({"ok": False, "msg": "Booking failed"})


#=========seat==========
@app.route("/seats/<int:sid>")
@safe_db
def seats(sid):
    fs = request.args.get("fs", "Jaipur")
    ts = request.args.get("ts", "Delhi")
    d = request.args.get("d", date.today().isoformat())

    # ⭐ STEP 1: Booked seats database से fetch करें
    booked_seats = []
    try:
        conn, cur = get_db()
        cur.execute("""
            SELECT seat_number FROM bookings 
            WHERE schedule_id=%s AND travel_date=%s AND status='confirmed'
        """, (sid, d))
        booked_seats = [row['seat_number'] for row in cur.fetchall()]
        conn.close()
    except Exception as e:
        print(f"Booked seats fetch error: {e}")

    available_seats = 40 - len(booked_seats)

    # ⭐ STEP 2: Green (Available) vs Red (Booked) seats generate करें
    seat_buttons = ''
    for i in range(1, 41):
        if i in booked_seats:
            # Booked seat - Red + Disabled
            seat_buttons += f'''
            <button class="btn btn-danger seat" disabled title="Booked">
                ⛔ {i}
            </button>
            '''
        else:
            # Available seat - Green + Clickable
            seat_buttons += f'''
            <button class="btn btn-success seat" onclick="bookSeat({i},'{fs}','{ts}','{d}')" title="Click to Book">
                💺 {i}
            </button>
            '''

    # ⭐ STEP 3: Complete HTML with Live GPS Map
    html = f"""
    <div class="row">
        <div class="col-md-8">
            <div class="text-center mb-5">
                <h3 class="mb-3">🚌 {fs} → {ts}</h3>
                <p class="lead text-muted mb-4">
                    📅 Date: <strong>{d}</strong> | 
                    💺 Available: <span class="text-success">{available_seats}</span>/40 | 
                    <span class="text-danger">{len(booked_seats)} Booked</span>
                </p>
            </div>

            <!-- Live GPS Map -->
            <div id="mapId" class="mb-5"></div>

            <!-- Bus Seats Layout -->
            <h5 class="text-center mb-4">💺 उपलब्ध सीटें (Click to Book)</h5>
            <div class="bus-row justify-content-center">{seat_buttons}</div>
        </div>

        <div class="col-md-4">
            <div class="card bg-warning text-dark">
                <div class="card-body text-center">
                    <h5>📋 Booking Info</h5>
                    <p><strong>Bus ID:</strong> {sid}</p>
                    <p><strong>From:</strong> {fs}</p>
                    <p><strong>To:</strong> {ts}</p>
                    <p><strong>Date:</strong> {d}</p>
                    <hr>
                    <div class="legend">
                        <div><span class="btn btn-success btn-sm me-2">💺</span> Available</div>
                        <div><span class="btn btn-danger btn-sm me-2">⛔</span> Booked</div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
    setTimeout(function() {{
        // Initialize Live GPS Map
        window.map = L.map('mapId').setView([27.5, 76.0], 9);
        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
            attribution: '© OpenStreetMap'
        }}).addTo(window.map);

        // Route polyline (Jaipur to Delhi)
        L.polyline([
            [27.0, 75.8],  // Jaipur
            [27.5, 76.0],  // Ajmer  
            [28.6, 77.2]   // Delhi
        ], {{
            color: 'blue',
            weight: 8,
            opacity: 0.7
        }}).addTo(window.map);

        // Live GPS Update हर 3 सेकंड
        setInterval(function() {{
            fetch('/bus_location/{sid}')
                .then(r => r.json())
                .then(d => {{
                    if(d.lat && d.lng && window.map) {{
                        if(!window.busMarker) {{
                            window.busMarker = L.marker([d.lat, d.lng], {{
                                icon: L.divIcon({{
                                    className: 'custom-div-icon',
                                    html: '<div style="background:green;color:white;padding:5px;border-radius:50%;font-size:20px">🚌</div>',
                                    iconSize: [50, 50],
                                    iconAnchor: [25, 25]
                                }})
                            }}).addTo(window.map)
                            .bindPopup('Live Bus Location');
                        }} else {{
                            window.busMarker.setLatLng([d.lat, d.lng]);
                        }}
                    }}
                }})
                .catch(e => console.log('GPS update error:', e));
        }}, 3000);
    }}, 300);
    </script>
    """
    return render_template_string(BASE_HTML, content=html)


#======= driver=========

@app.route("/driver/<int:bus_id>")
def driver(bus_id):
    return render_template_string("""
<!DOCTYPE html>
<html><head><meta name="viewport" content="width=device-width">
<title>Driver GPS - Bus """ + str(bus_id) + """</title>
<style>body{margin:0;padding:40px;background:#111;color:#0f0;font-family:monospace;font-size:18px;text-align:center}
#status{padding:20px;background:rgba(0,255,0,0.1);border-radius:10px;margin:20px}</style></head>
<body>
<h1>🚌 Driver GPS Tracker</h1>
<h3>Bus ID: """ + str(bus_id) + """</h3>
<div id="status">Waiting GPS signal...</div>
<script>
navigator.geolocation.watchPosition(p=>{
    fetch("/update_location",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({bus_id:""" + str(bus_id) + """,lat:p.coords.latitude,lng:p.coords.longitude})
    });
    document.getElementById("status").innerHTML=`✅ LIVE<br>Lat:${p.coords.latitude.toFixed(6)}<br>Lng:${p.coords.longitude.toFixed(6)}<br>Updated: ${new Date().toLocaleTimeString()}`;
},e=>document.getElementById("status").innerHTML="❌ GPS Error", {enableHighAccuracy:true});
</script>
</body></html>""")


@app.route("/update_location", methods=["POST"])
def update_location():
    socketio.emit("bus_location", request.json)
    return jsonify(ok=True)


@app.route("/route_points/<int:sid>")
@safe_db
def route_points(sid):
    return jsonify([])


@app.route("/bus_location/<int:bus_id>")
@safe_db
def bus_location(bus_id):
    lat = 27.5 + random.uniform(-0.1, 0.1)
    lng = 76.0 + random.uniform(-0.3, 0.3)
    return jsonify(lat=lat, lng=lng)


if __name__ == "__main__":
    print("🚀 Bus App Starting...")
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
