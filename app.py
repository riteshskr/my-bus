import eventlet
eventlet.monkey_patch()

import os, random
from datetime import date
from functools import wraps

from flask import Flask, request, jsonify, redirect
from flask_socketio import SocketIO, emit, join_room
from flask_compress import Compress

from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
import razorpay

# ============== APP SETUP ==============

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key")
Compress(app)

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="eventlet",
    ping_timeout=60
)

# ============== DB ==============

DATABASE_URL = os.getenv("DATABASE_URL")

pool = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=1,
    max_size=10,
    kwargs={"row_factory": dict_row}
)

def get_db():
    conn = pool.getconn()
    cur = conn.cursor()
    return conn, cur


# ============== RAZORPAY ==============

RAZORPAY_ENABLED = bool(
    os.getenv("RAZORPAY_KEY_ID") and
    os.getenv("RAZORPAY_KEY_SECRET")
)

if RAZORPAY_ENABLED:
    razor_client = razorpay.Client(auth=(
        os.getenv("RAZORPAY_KEY_ID"),
        os.getenv("RAZORPAY_KEY_SECRET")
    ))


# ==================================================
#                     HOME PAGE
# ==================================================

@app.route("/")
def home():

    conn, cur = get_db()

    cur.execute("""
        SELECT s.id, s.bus_name, r.name as route_name,
               s.current_lat as lat, s.current_lng as lng
        FROM schedules s
        JOIN routes r ON r.id = s.route_id
        ORDER BY s.id
    """)

    buses = cur.fetchall()

    live_section = ""

    for bus in buses:

        if bus.get("lat"):
            status = "🟢 Live"
            badge = "bg-success"
            coords = f"{bus['lat']}, {bus['lng']}"
        else:
            status = "⚪ Offline"
            badge = "bg-secondary"
            coords = "No GPS"

        live_section += f'''
        <div class="col-md-6 col-lg-3">
            <div class="card border-0 shadow">
                <div class="card-body text-center p-3">

                    <h6 class="fw-bold">{bus['bus_name']}</h6>
                    <small class="text-muted">{bus['route_name']}</small><br>

                    <span class="badge {badge}">{status}</span>

                    <div class="mt-2">
                        <small>📍 {coords}</small>
                    </div>

                    <a class="btn btn-sm btn-primary mt-2"
                       href="/seat/{bus['id']}">
                       View Seats
                    </a>

                </div>
            </div>
        </div>
        '''

    html = f"""
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">

<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">

<title>My Bus</title>

</head>

<body class="bg-light">

<div class="container py-4">

<h4 class="fw-bold mb-3">🚌 Live Buses</h4>

<div class="row g-3">
    {live_section}
</div>

</div>

</body>
</html>
"""

    return html


# ==================================================
#                DRIVER PAGE
# ==================================================

@app.route("/driver/<int:sid>")
def driver(sid):

    return f"""
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">

<title>Driver {sid}</title>

<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>

</head>

<body>

<h3>🚗 Driver – Bus {sid}</h3>

<div id="status">Waiting GPS...</div>

<script>

const socket = io();

if(navigator.geolocation){{

navigator.geolocation.watchPosition(pos => {{

    const data = {{
        sid: "{sid}",
        lat: pos.coords.latitude,
        lng: pos.coords.longitude,
        speed: pos.coords.speed || 0
    }};

    socket.emit("driver_gps", data);

    document.getElementById("status").innerHTML =
        "📍 " + data.lat + " , " + data.lng;

}}, err => {{
    document.getElementById("status").innerHTML =
        "GPS ERROR: " + err.message;
}},
{{
    enableHighAccuracy:true,
    maximumAge:0,
    timeout:5000
}}
);

}}

</script>

</body>
</html>
"""


# ==================================================
#                SEAT PAGE
# ==================================================

@app.route("/seat/<int:sid>")
def seat(sid):

    html = f"""
<!DOCTYPE html>
<html>
<head>

<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<link rel="stylesheet" href="https://unpkg.com/leaflet/dist/leaflet.css"/>

<script src="https://unpkg.com/leaflet/dist/leaflet.js"></script>

</head>

<body>

<h3>🚌 Bus {sid} Live</h3>

<div id="map" style="height:400px"></div>

<script>

var sid = "{sid}";

var map = L.map('map').setView([27.5,75.5], 8);

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png').addTo(map);

var marker = L.marker([27.5,75.5]).addTo(map);

const socket = io();

socket.emit("join_bus", sid);

socket.on("bus_location", data => {{

    console.log("BUS UPDATE", data);

    if(data.sid == sid){{

        marker.setLatLng([data.lat, data.lng]);

        map.panTo([data.lat, data.lng]);

    }}

}});

</script>

</body>
</html>
"""

    return html


# ==================================================
#                SOCKET LOGIC
# ==================================================

@socketio.on("join_bus")
def join_bus_event(sid):
    join_room(str(sid))
    print("👥 Joined room", sid)


@socketio.on("driver_gps")
def gps(data):

    print("📍 LIVE:", data)

    sid = str(data.get("sid"))

    lat = float(data.get("lat"))
    lng = float(data.get("lng"))
    speed = float(data.get("speed", 0))

    # DB SAVE
    try:
        conn, cur = get_db()

        cur.execute("""
            UPDATE schedules
            SET current_lat=%s,
                current_lng=%s
            WHERE id=%s
        """, (lat, lng, sid))

        conn.commit()

    except Exception as e:
        print("DB ERROR:", e)

    # BROADCAST
    emit("bus_location", {
        "sid": sid,
        "lat": lat,
        "lng": lng,
        "speed": speed
    }, room=sid)



# ==================================================

if __name__ == "__main__":

    socketio.run(app, host="0.0.0.0", port=5000)
