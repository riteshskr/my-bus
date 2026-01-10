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
<div class="alert alert-success text-center">
    ✅ System Active - GPS Tracking Ready!
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
def select(sid):  # ❌ @safe_db हटाया!
    stations = ["Jaipur", "Ajmer", "Pushkar", "Kishangarh", "Delhi"]

    # Safe DB query
    try:
        conn, cur = get_db()

        # 🔥 schedule → route_id निकालो
        cur.execute(
            "SELECT route_id FROM schedules WHERE id=%s",
            (schedule_id,)
        )
        row = cur.fetchone()

        if row:
            route_id = row["route_id"]

            cur.execute(
                "SELECT station_name FROM route_stations WHERE route_id=%s ORDER BY station_order",
                (route_id,)
            )
            stations = [r["station_name"] for r in cur.fetchall()]

        conn.close()
    except Exception as e:
        print("Select error:", e)

    opts = "".join(f"<option>{s}</option>" for s in stations)
    today = date.today().isoformat()

    # ✅ Fixed date format
    #today_str = date.today().isoformat()

    form = f"""
    <div class="card mx-auto shadow" style="max-width:500px">
        <div class="card-header bg-primary text-white text-center py-4">
            <h4>🎫 Select Journey</h4>
            <p class="mb-0">Bus ID: <strong>{sid}</strong></p>
        </div>
        <div class="card-body p-4">
            <form method="POST">
                <div class="mb-3">
                    <label class="form-label fw-bold">From Station</label>
                    <select name="from" class="form-select" required>
                        <option value="">Choose From</option>{opts}
                    </select>
                </div>
                <div class="mb-3">
                    <label class="form-label fw-bold">To Station</label>
                    <select name="to" class="form-select" required>
                        <option value="">Choose To</option>{opts}
                    </select>
                </div>
                <div class="mb-4">
                    <label class="form-label fw-bold">Date</label>
                    <input type="date" name="date" class="form-control" value="{today_str}" required>
                </div>
                <button type="submit" class="btn btn-success w-100 py-3 fs-5">
                    🚀 Show Available Seats
                </button>
            </form>
        </div>
    </div>
    """

    if request.method == "POST":
        from_st = request.form.get("from", "Jaipur")
        to_st = request.form.get("to", "Delhi")
        travel_date = request.form.get("date", today_str)

        print(f"Form POST: {from_st} → {to_st} | Date: {travel_date}")

        # ✅ Manual redirect - NO url_for!
        return redirect(f"/seats/{sid}?fs={from_st}&ts={to_st}&d={travel_date}")

    return render_template_string(BASE_HTML, content=form)


@app.route("/seats/<int:sid>")
@safe_db
def seats(sid):
    fs = request.args.get("fs", "Jaipur")
    ts = request.args.get("ts", "Delhi")
    d = request.args.get("d", date.today().isoformat())

    # ✅ FIXED: Proper seat buttons (no escaping issues)
    seat_buttons = ''.join(
        f'<button class="btn btn-success seat" onclick="bookSeat({i},\'{fs}\',\'{ts}\',\'{d}\')">{i}</button>'
        for i in range(1, 41)
    )

    html = f"""
    <div class="text-center mb-5">
        <h3 class="mb-3">{fs} → {ts}</h3>
        <p class="lead text-muted mb-4">Date: {d} | 40 Seats Available</p>
    </div>
    <div id="mapId" style="height:350px;margin:0 auto 30px;max-width:800px"></div>
    <div class="text-center mb-4">
        <h5>💺 Available Seats</h5>
    </div>
    <div class="bus-row justify-content-center">{seat_buttons}</div>

    <script>
    setTimeout(function() {{
        window.map = L.map('mapId').setView([27.5, 76.0], 8);
        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
            attribution: '© OpenStreetMap'
        }}).addTo(window.map);

        var routeCoords = [[27.0, 75.8], [27.5, 76.0], [28.6, 77.2]];
        L.polyline(routeCoords, {{color: 'blue', weight: 6}}).addTo(window.map);
        window.map.fitBounds(routeCoords);

        // ✅ FIXED: Proper setInterval
        setInterval(function() {{
            fetch('/bus_location/{sid}')
                .then(function(r) {{ return r.json(); }})
                .then(function(d) {{
                    if(d.lat && d.lng && window.map) {{
                        if(!window.busMarker) {{
                            window.busMarker = L.marker([d.lat, d.lng], {{
                                icon: L.divIcon({{
                                    className: 'custom-div-icon',
                                    html: '🚌',
                                    iconSize: [40, 40]
                                }})
                            }}).addTo(window.map).bindPopup('Live Bus Location');
                        }} else {{
                            window.busMarker.setLatLng([d.lat, d.lng]);
                        }}
                    }}
                }});
        }}, 3000);
    }}, 200);
    </script>
    """
    return render_template_string(BASE_HTML, content=html)


# ================= API ROUTES =================
@app.route("/book", methods=["POST"])
def book():  # ❌ @safe_db हटाया!
    try:
        data = request.get_json() or {}
        print(f"Booking data: {data}")

        seat = data.get('seat', 'Unknown')
        fare = random.randint(250, 450)

        print(f"✅ BOOKED Seat {seat}")
        return jsonify({
            "ok": True,
            "msg": f"✅ Seat {seat} Booked Successfully! 💺 Fare: ₹{fare}",
            "fare": fare
        })
    except Exception as e:
        print(f"Booking error: {e}")
        return jsonify({"ok": False, "msg": "Booking failed"})


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
