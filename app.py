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

# ================= SEATS + MAP =================
@app.route("/seats/<int:sid>")
def seats(sid):
    fs = request.args.get("fs")
    ts = request.args.get("ts")
    d = request.args.get("d")
    if not fs or not ts or not d:
        return "Missing fs/ts/d", 400

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Seats
    cur.execute("SELECT id, seat_no FROM seats WHERE schedule_id=%s", (sid,))
    seats = cur.fetchall()

    # Booked seats
    cur.execute("SELECT seat_id, from_station, to_station FROM seat_bookings WHERE schedule_id=%s AND booking_date=%s",
                (sid, d))
    bookings = cur.fetchall()

    seat_map = {}
    for seat_id, b_from, b_to in bookings:
        if seat_id not in seat_map: seat_map[seat_id] = []
        cur.execute(
            "SELECT station_order FROM route_stations WHERE route_id=(SELECT route_id FROM schedules WHERE id=%s) AND station_name=%s",
            (sid, b_from))
        b_from_order = cur.fetchone()[0]
        cur.execute(
            "SELECT station_order FROM route_stations WHERE route_id=(SELECT route_id FROM schedules WHERE id=%s) AND station_name=%s",
            (sid, b_to))
        b_to_order = cur.fetchone()[0]
        seat_map[seat_id].append((b_from_order, b_to_order))

    # Selected from/to order
    cur.execute(
        "SELECT station_order FROM route_stations WHERE route_id=(SELECT route_id FROM schedules WHERE id=%s) AND station_name=%s",
        (sid, fs))
    sel_from_order = cur.fetchone()[0]
    cur.execute(
        "SELECT station_order FROM route_stations WHERE route_id=(SELECT route_id FROM schedules WHERE id=%s) AND station_name=%s",
        (sid, ts))
    sel_to_order = cur.fetchone()[0]

    # Seats HTML
    html = f"""
<div id="map"></div>
<h6 class='text-center'>{fs} → {ts} | {d}</h6>
<div class='bus-row'>
"""
    for seat_id, seat_no in seats:
        status = "green"
        if seat_id in seat_map:
            for b_from_order, b_to_order in seat_map[seat_id]:
                if not (sel_to_order <= b_from_order or sel_from_order >= b_to_order):
                    status = "red"
                    break
        color = {"green": "btn-success", "red": "btn-danger"}[status]
        disabled = "disabled" if status == "red" else ""
        html += f"<button class='btn {color} seat' {disabled} onclick=\"bookSeat({seat_id},'{fs}','{ts}','{d}')\">{seat_no}</button>"
    html += "</div>"

    # ================= Map + Live Bus (Real Station Names) =================
    html += """
<script>
const AVG_SPEED = 40; // km/h
window.map = L.map('map');
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom:18}).addTo(window.map);

const busIcon = L.icon({
    iconUrl: "https://cdn-icons-png.flaticon.com/512/108/108009.png",  // ✅ CDN
    iconSize:[40,40], iconAnchor:[20,20], popupAnchor:[0,-20]
});

let routeLatLngs = [];
let routeNames = [];
let busMarker = null;

// ===== Load Route Points with real station names =====
fetch("/route_points/""" + str(sid) + """")
.then(r=>r.json())
.then(p=>{
    routeLatLngs = p.map(x=>[+x.lat,+x.lng]);
    routeNames = p.map(x=>x.station_name);  // <-- real names from DB
    let line = L.polyline(routeLatLngs, {color:"blue",weight:5}).addTo(map);
    map.fitBounds(line.getBounds());
});

// ===== Haversine Distance =====
function haversine(lat1,lng1,lat2,lng2){
    const R = 6371;
    const dLat = (lat2-lat1)*Math.PI/180;
    const dLng = (lng2-lng1)*Math.PI/180;
    const a = Math.sin(dLat/2)**2 + Math.cos(lat1*Math.PI/180)*Math.cos(lat2*Math.PI/180)*Math.sin(dLng/2)**2;
    return R*2*Math.atan2(Math.sqrt(a),Math.sqrt(1-a));
}

// ===== Update Bus with Next Station =====
function updateBus(lat,lng){
    if(routeLatLngs.length==0) return;

    let minDist = Infinity;
    let idx = 0;
    for(let i=0;i<routeLatLngs.length;i++){
        let d = Math.pow(routeLatLngs[i][0]-lat,2)+Math.pow(routeLatLngs[i][1]-lng,2);
        if(d<minDist){ minDist=d; idx=i; }
    }

    let nextIndex = idx+1;
    let nextStop = routeNames[nextIndex] || "Last Stop";

    let distToNext = 0;
    if(nextIndex<routeLatLngs.length){
        distToNext = haversine(lat,lng,routeLatLngs[nextIndex][0],routeLatLngs[nextIndex][1]);
    }

    let ETA = Math.round(distToNext/AVG_SPEED*60);

    if(!busMarker){
        busMarker = L.marker([lat,lng], {icon:busIcon})
                      .addTo(map)
                      .bindPopup("🚌 Next: "+nextStop+"<br>📍 Distance: "+distToNext.toFixed(1)+" km<br>⏱ ETA: "+ETA+" min")
                      .openPopup();
    } else {
        busMarker.setLatLng([lat,lng]);
        busMarker.setPopupContent("🚌 Next: "+nextStop+"<br>📍 Distance: "+distToNext.toFixed(1)+" km<br>⏱ ETA: "+ETA+" min");
        busMarker.openPopup();
    }
}

// ===== Live Polling =====
setInterval(()=>{
    fetch("/bus_location/""" + str(sid) + """")
    .then(r=>r.json())
    .then(d=>{
        if(d.lat && d.lng){
            updateBus(+d.lat,+d.lng);
        }
    });
},2000);
</script>
"""
    conn.close()
    return render_template_string(BASE_HTML, content=html)

# ================= BOOK API =================
@app.route("/book", methods=["POST"])
def book():
    data = request.json
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    sid = data["seat"]

    # Schedule ID
    cur.execute("SELECT schedule_id FROM seats WHERE id=%s", (sid,))
    schedule_id = cur.fetchone()["schedule_id"]

    # Station orders
    cur.execute("SELECT station_order FROM route_stations WHERE route_id=(SELECT route_id FROM schedules WHERE id=%s) AND station_name=%s", (schedule_id,data["from"]))
    sel_from_order = cur.fetchone()["station_order"]
    cur.execute("SELECT station_order FROM route_stations WHERE route_id=(SELECT route_id FROM schedules WHERE id=%s) AND station_name=%s", (schedule_id,data["to"]))
    sel_to_order = cur.fetchone()["station_order"]

    # Overlap check
    cur.execute("SELECT from_station,to_station FROM seat_bookings WHERE seat_id=%s AND schedule_id=%s AND booking_date=%s",(sid,schedule_id,data["date"]))
    bookings = cur.fetchall()
    for b_from,b_to in bookings:
        cur.execute("SELECT station_order FROM route_stations WHERE route_id=(SELECT route_id FROM schedules WHERE id=%s) AND station_name=%s",(schedule_id,b_from))
        b_from_order = cur.fetchone()["station_order"]
        cur.execute("SELECT station_order FROM route_stations WHERE route_id=(SELECT route_id FROM schedules WHERE id=%s) AND station_name=%s",(schedule_id,b_to))
        b_to_order = cur.fetchone()["station_order"]
        # अगर नई booking का overlap है तो block
        if sel_from_order < b_to_order and sel_to_order > b_from_order:
            conn.close()
            return jsonify({"ok":False,"msg":"❌ यह seat इस segment में पहले से बुक है!"})

    # ✅ Fixed Fare - distance column हटाया
    distance_km = abs(sel_to_order - sel_from_order) * 60  # 60km average
    fare = round(distance_km * 2.5, 2)

    # Booking insert
    cur.execute("""INSERT INTO seat_bookings (seat_id,schedule_id,passenger_name,mobile,from_station,to_station,booking_date,fare) 
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (sid,schedule_id,data["name"],data["mobile"],data["from"],data["to"],data["date"],fare))
    conn.commit()
    conn.close()
    socketio.emit("seat_booked",{"seat":sid})
    return jsonify({"ok":True,"msg":f"✅ बुकिंग सफल! किराया: ₹{fare}"})

# ================= DRIVER GPS =================
@app.route("/driver/<int:bus_id>")
def driver(bus_id):
    return render_template_string("""
<!DOCTYPE html>
<html>
<head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Driver GPS</title></head>
<body style="text-align:center;font-family:Arial">
<h3>Driver Live Location</h3>
<p id="s">Waiting GPS...</p>
<script>
navigator.geolocation.watchPosition(
 p=>{
  fetch("/update_location",{
   method:"POST",
   headers:{"Content-Type":"application/json"},
   body:JSON.stringify({"bus_id":""" + str(bus_id) + ""","lat":p.coords.latitude,"lng":p.coords.longitude})
  });
  document.getElementById("s").innerText = "Lat:"+p.coords.latitude+" Lng:"+p.coords.longitude;
 },
 e=>alert(e.message),
 {enableHighAccuracy:true}
);
</script>
</body>
</html>
""")

# ================= UPDATE LOCATION =================
@app.route("/update_location", methods=["POST"])
def update_location():
    d = request.json
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("UPDATE schedules SET current_lat=%s,current_lng=%s WHERE id=%s", (d["lat"],d["lng"],d["bus_id"]))
    conn.commit()
    conn.close()
    socketio.emit("bus_location",d)
    return jsonify(ok=True)

# ================= ROUTE POINTS =================
@app.route("/route_points/<int:sid>")
def route_points(sid):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT rs.lat, rs.lng, rs.station_name FROM route_stations rs JOIN schedules s ON s.route_id=rs.route_id WHERE s.id=%s ORDER BY rs.station_order",(sid,))
    points = cur.fetchall()
    conn.close()
    return jsonify(points)

# ================= LAST LOCATION =================
@app.route("/bus_location/<int:bus_id>")
def bus_location(bus_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT current_lat,current_lng FROM schedules WHERE id=%s", (bus_id,))
    row = cur.fetchone()
    conn.close()
    if row and row[0]:
        return jsonify(lat=row[0], lng=row[1])
    return jsonify(lat=None, lng=None)
# ================= sample data =================
def add_sample_data():
    conn = get_db_connection()
    cur = conn.cursor()

    # Route
    cur.execute("INSERT INTO routes (name) VALUES ('जयपुर-दिल्ली') ON CONFLICT (name) DO NOTHING")
    cur.execute("SELECT id FROM routes WHERE name='जयपुर-दिल्ली'")
    route_id = cur.fetchone()[0]

    # Stations
    stations = [('जयपुर', 1), ('सीकर', 2), ('दिल्ली', 3)]
    for name, order in stations:
        cur.execute(
            "INSERT INTO route_stations (route_id,station_name,station_order) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
            (route_id, name, order))

    # Bus
    cur.execute(
        "INSERT INTO schedules (route_id,bus_name,departure_time) VALUES (%s,'वॉल्वो AC','08:00 AM') ON CONFLICT DO NOTHING",
        (route_id,))
    cur.execute("SELECT id FROM schedules WHERE bus_name='वॉल्वो AC'")
    schedule_id = cur.fetchone()[0]

    # Seats (अगर नहीं हैं तो)
    cur.execute("SELECT COUNT(*) FROM seats WHERE schedule_id=%s", (schedule_id,))
    if cur.fetchone()[0] == 0:
        for i in range(1, 41):
            cur.execute("INSERT INTO seats (schedule_id,seat_no) VALUES (%s,%s)", (schedule_id, f"S{i}"))

    conn.commit()
    conn.close()
    print("✅ Sample Data Added!")


# ================= RUN =================
if __name__=="__main__":
    init_db()
    add_sample_data()  # ← यह LINE ADD करें! ❌ Missing
    socketio.run(app, host="0.0.0.0", port=int(os.getenv("PORT",5000)), debug=True, allow_unsafe_werkzeug=True)