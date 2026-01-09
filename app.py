from dotenv import load_dotenv
import os, time, requests, math
import psycopg2
import psycopg2.extras
from flask import Flask, render_template_string, request, redirect, url_for, jsonify
from flask_socketio import SocketIO
from datetime import date

load_dotenv()

# ================= APP =================
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY","dev-key")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ================= DB =================
def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        port=int(os.getenv("DB_PORT", 5432)),
        sslmode=os.getenv("DB_SSL", "disable")  # 👈 FIX
    )

# ================= INIT DB =================
def init_db():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # Routes
    cur.execute("""CREATE TABLE IF NOT EXISTS routes (id SERIAL PRIMARY KEY, name VARCHAR(100) NOT NULL)""")
    # Schedules
    cur.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id SERIAL PRIMARY KEY,
            route_id INT REFERENCES routes(id) ON DELETE CASCADE,
            bus_name VARCHAR(100),
            departure_time VARCHAR(20),
            seating_rate DOUBLE PRECISION DEFAULT 0,
            single_sleeper_rate DOUBLE PRECISION DEFAULT 0,
            double_sleeper_rate DOUBLE PRECISION DEFAULT 0,
            current_lat DOUBLE PRECISION DEFAULT NULL,
            current_lng DOUBLE PRECISION DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Route Stations
    cur.execute("""
        CREATE TABLE IF NOT EXISTS route_stations (
            id SERIAL PRIMARY KEY,
            route_id INT REFERENCES routes(id) ON DELETE CASCADE,
            station_name VARCHAR(100),
            station_order INT,
            lat DOUBLE PRECISION,
            lng DOUBLE PRECISION
        )
    """)
    # Seats
    cur.execute("""
        CREATE TABLE IF NOT EXISTS seats (
            id SERIAL PRIMARY KEY,
            schedule_id INT REFERENCES schedules(id) ON DELETE CASCADE,
            seat_no VARCHAR(10),
            seat_type VARCHAR(30)
        )
    """)
    # Seat Bookings
    cur.execute("""
        CREATE TABLE IF NOT EXISTS seat_bookings (
            id SERIAL PRIMARY KEY,
            seat_id INT REFERENCES seats(id) ON DELETE CASCADE,
            schedule_id INT REFERENCES schedules(id) ON DELETE CASCADE,
            passenger_name VARCHAR(100),
            mobile VARCHAR(20),
            from_station VARCHAR(100),
            to_station VARCHAR(100),
            booking_date DATE,
            fare DOUBLE PRECISION,
            booked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    print("✅ PostgreSQL DB initialized successfully!")

# ================= GEOCODE =================
def geocode_station(name):
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": name + ", India", "format": "json", "limit": 1}
    try:
        res = requests.get(url, params=params, headers={"User-Agent":"BusApp"})
        data = res.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except:
        pass
    return None, None

def fill_missing_latlng(route_id=None):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if route_id:
        cur.execute("SELECT id,station_name FROM route_stations WHERE route_id=%s AND (lat IS NULL OR lng IS NULL OR lat=0 OR lng=0)", (route_id,))
    else:
        cur.execute("SELECT id,station_name FROM route_stations WHERE lat IS NULL OR lng IS NULL OR lat=0 OR lng=0")
    stations = cur.fetchall()
    for st in stations:
        lat,lng = geocode_station(st['station_name'])
        if lat and lng:
            cur.execute("UPDATE route_stations SET lat=%s,lng=%s WHERE id=%s",(lat,lng,st['id']))
            print(f"Updated {st['station_name']} -> {lat},{lng}")
        time.sleep(1)
    conn.commit()
    conn.close()

# ================= BASE HTML =================
BASE_HTML = """
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bus Booking</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
<script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
<link rel="stylesheet" href="https://unpkg.com/leaflet/dist/leaflet.css">
<script src="https://unpkg.com/leaflet/dist/leaflet.js"></script>
<style>
.bus-row{display:grid;grid-template-columns: repeat(4, 38px);column-gap:4px;row-gap:2px;justify-content:center;}
.seat:nth-child(2){margin-right:26px;}
.seat{width:38px;height:40px;padding:0;margin:0;display:flex;flex-direction:column;align-items:center;justify-content:center;font-size:10px;}
.seat i{font-size:16px;line-height:1;}
#map{height:400px;margin-bottom:10px}
</style>
</head>
<body class="bg-dark text-white">
<div class="container py-3">
<h4 class="text-center">Bus Booking + Live Tracking</h4>
{{content|safe}}
<a href="/" class="btn btn-light w-100 mt-3">Home</a>
</div>
<script>
var socket = io({transports:["websocket","polling"]});
socket.on("bus_location", function(d){
    if(!window.map || !d.lat) return;
    if(!window.busMarker){window.busMarker = L.marker([+d.lat,+d.lng]).addTo(window.map).bindPopup("Live Bus");}
    else { window.busMarker.setLatLng([+d.lat,+d.lng]); }
});
function bookSeat(seatId,fs,ts,d){
    let name = prompt("Enter Name:"); let mobile = prompt("Enter Mobile:");
    if(!name||!mobile) return;
    fetch("/book",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({seat:seatId,name:name,mobile:mobile,from:fs,to:ts,date:d})}).then(r=>r.json()).then(r=>{alert(r.msg);if(r.ok) location.reload();});
}
</script>
</body>
</html>
"""

# ================= ROUTES =================
@app.route("/")
def index():
    return redirect(url_for("home"))

@app.route("/home")
def home():
    fill_missing_latlng()
    conn=get_db_connection()
    cur=conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id,name FROM routes")
    html = "".join(f"<a class='btn btn-success w-100 mb-2' href='/buses/{r['id']}'>{r['name']}</a>" for r in cur.fetchall())
    conn.close()
    return render_template_string(BASE_HTML, content=html)

@app.route("/buses/<int:rid>")
def buses(rid):
    fill_missing_latlng(rid)
    conn=get_db_connection()
    cur=conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id,bus_name,departure_time FROM schedules WHERE route_id=%s",(rid,))
    html = "".join(f"<a class='btn btn-info w-100 mb-2' href='/select/{r['id']}'>{r['bus_name']} ({r['departure_time']})</a>" for r in cur.fetchall())
    conn.close()
    return render_template_string(BASE_HTML, content=html)

@app.route("/select/<int:sid>", methods=["GET","POST"])
def select(sid):
    conn=get_db_connection()
    cur=conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT station_name FROM route_stations rs JOIN schedules s ON s.route_id=rs.route_id WHERE s.id=%s ORDER BY station_order""",(sid,))
    stations=[s['station_name'] for s in cur.fetchall()]
    conn.close()
    if request.method=="POST":
        return redirect(url_for("seats",sid=sid,fs=request.form["from"],ts=request.form["to"],d=request.form["date"]))
    opts="".join(f"<option>{s}</option>" for s in stations)
    return render_template_string(BASE_HTML, content=f"""<form method="post" class="bg-light text-dark p-3 rounded">
<select name="from" class="form-select mb-2">{opts}</select>
<select name="to" class="form-select mb-2">{opts}</select>
<input type="date" name="date" class="form-control mb-2" value="{date.today()}" required>
<button class="btn btn-success w-100">Show Seats</button>
</form>""")

# ================= RUN =================
if __name__=="__main__":
    init_db()
    socketio.run(app, host="0.0.0.0", port=int(os.getenv("PORT",5000)), debug=True, allow_unsafe_werkzeug=True)