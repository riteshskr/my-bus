import os
import time
import requests
import mysql.connector
from flask import Flask, render_template_string, request, redirect, url_for, jsonify
from flask_socketio import SocketIO
from datetime import date

# ================= APP =================
app = Flask(__name__)
app.secret_key = "super-secret-key"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ================= DB CONFIG =================
DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "*#06041974",
    "database": "busdb1",
    "autocommit": True
}

def get_db():
    return mysql.connector.connect(**DB_CONFIG)

# ================= GEOCODE HELPER =================
def geocode_station(station_name):
    """Use OpenStreetMap Nominatim API to get lat/lng"""
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": station_name + ", India", "format": "json", "limit": 1}
    try:
        response = requests.get(url, params=params, headers={"User-Agent": "BusApp"})
        data = response.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        print("Error geocoding:", station_name, e)
    return None, None

def fill_missing_latlng(route_id=None):
    """Automatically fill missing lat/lng for stations"""
    conn = get_db()
    cur = conn.cursor()
    if route_id:
        cur.execute("SELECT id, station_name FROM route_stations WHERE route_id=%s AND (lat IS NULL OR lng IS NULL)", (route_id,))
    else:
        cur.execute("SELECT id, station_name FROM route_stations WHERE lat IS NULL OR lng IS NULL")
    stations = cur.fetchall()
    for sid, name in stations:
        lat, lng = geocode_station(name)
        if lat and lng:
            cur.execute("UPDATE route_stations SET lat=%s, lng=%s WHERE id=%s", (lat, lng, sid))
            print(f"Updated {name} -> lat:{lat}, lng:{lng}")
        else:
            print(f"Could not find coordinates for {name}")
        time.sleep(1)  # Nominatim rate limit
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
<link rel="stylesheet"
 href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
<script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
<link rel="stylesheet" href="https://unpkg.com/leaflet/dist/leaflet.css">
<script src="https://unpkg.com/leaflet/dist/leaflet.js"></script>
<style>

.bus-row{
    display:grid;
    grid-template-columns: repeat(4, 38px); /* ✅ सिर्फ 4 column */
    column-gap:4px;
    row-gap:2px;
    justify-content:center;
}
.bus-row .seat:nth-child(2){
    margin-right:26px;   /* 👈 gap size (20–40px try kar sakte ho) */
}
/* aisle look (gap only) */
.seat:nth-child(2){
    margin-right:26px;   /* 👈 बीच का रास्ता */
}

.seat{
    width:38px;
    height:40px;
    padding:0;
    margin:0;
    display:flex;
    flex-direction:column;
    align-items:center;
    justify-content:center;
    font-size:10px;
}

.seat i{
    font-size:16px;
    line-height:1;
}

.seat span{
    font-size:9px;
    line-height:1;
}
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
    if(!window.busMarker){
        window.busMarker = L.marker([parseFloat(d.lat),parseFloat(d.lng)]).addTo(window.map).bindPopup("Live Bus");
    } else {
        window.busMarker.setLatLng([parseFloat(d.lat),parseFloat(d.lng)]);
    }
});

function bookSeat(seatId,fs,ts,d){
    let name = prompt("Enter Name:");
    let mobile = prompt("Enter Mobile:");
    if(!name || !mobile) return;

    fetch("/book",{
        method:"POST",
        headers:{"Content-Type":"application/json"},
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

# ================= HOME =================
@app.route("/")
def home():
    fill_missing_latlng()  # Auto-fill all missing lat/lng
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id,name FROM routes")
    html = "".join(f"<a class='btn btn-success w-100 mb-2' href='/buses/{i}'>{n}</a>" for i,n in cur.fetchall())
    conn.close()
    return render_template_string(BASE_HTML, content=html)

# ================= BUSES =================
@app.route("/buses/<int:rid>")
def buses(rid):
    fill_missing_latlng(rid)  # Auto-fill for this route
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id,bus_name,departure_time FROM schedules WHERE route_id=%s", (rid,))
    html = "".join(f"<a class='btn btn-info w-100 mb-2' href='/select/{i}'>{n} ({t})</a>" for i,n,t in cur.fetchall())
    conn.close()
    return render_template_string(BASE_HTML, content=html)

# ================= FROM / TO =================
@app.route("/select/<int:sid>", methods=["GET","POST"])
def select(sid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT station_name FROM route_stations rs
        JOIN schedules s ON s.route_id=rs.route_id
        WHERE s.id=%s ORDER BY station_order
    """, (sid,))
    stations = [x[0] for x in cur.fetchall()]
    conn.close()

    if request.method=="POST":
        return redirect(url_for("seats", sid=sid,
                                fs=request.form["from"], ts=request.form["to"], d=request.form["date"]))
    opts = "".join(f"<option>{s}</option>" for s in stations)
    return render_template_string(BASE_HTML, content=f"""
<form method="post" class="bg-light text-dark p-3 rounded">
<select name="from" class="form-select mb-2">{opts}</select>
<select name="to" class="form-select mb-2">{opts}</select>
<input type="date" name="date" class="form-control mb-2" value="{date.today()}" required>
<button class="btn btn-success w-100">Show Seats</button>
</form>
""")

# ================= SEATS + MAP =================
@app.route("/seats/<int:sid>")
def seats(sid):
    fs = request.args.get("fs")
    ts = request.args.get("ts")
    d = request.args.get("d")
    if not fs or not ts or not d:
        return "Missing fs/ts/d", 400

    conn = get_db()
    cur = conn.cursor()

    # Seats
    cur.execute("SELECT id, seat_no, seat_type FROM seats WHERE schedule_id=%s", (sid,))
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

    count = 0

    for seat_id, seat_no, seat_type in seats:
        status = "green"
        if seat_id in seat_map:
            for b_from_order, b_to_order in seat_map[seat_id]:
                if not (sel_to_order <= b_from_order or sel_from_order >= b_to_order):
                    status = "red"
                    break

        color = "btn-danger" if status == "red" else "btn-success"
        disabled = "disabled" if status == "red" else ""

        # icon
        if seat_type == "SEATING":
            icon = "fa-chair"
        elif seat_type == "SLEEPER_SINGLE":
            icon = "fa-bed"
        elif seat_type == "SLEEPER_DOUBLE":
            icon = "fa-bed"
        else:
            icon = "fa-chair"

        if status == "red":
            icon = "fa-ban"

        html += f"""
        <button class='btn {color} seat' {disabled}
         onclick="bookSeat({seat_id},'{fs}','{ts}','{d}')">
         <i class="fa-solid {icon}"></i>
        </button>
        """

        count += 1
        if count % 4 == 0:
            html += "</div><div class='bus-row'>"
        # 👉 4 seat ke baad aisle reset (2+2 complete)


    # ================= Map + Live Bus (Real Station Names) =================
    html += """
<script>
const AVG_SPEED = 40; // km/h
window.map = L.map('map');
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom:18}).addTo(window.map);

const busIcon = L.icon({
    iconUrl: "/static/bus.png",
    iconSize:[40,40],
    iconAnchor:[20,20],
    popupAnchor:[0,-20]
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
    d = request.json
    conn = get_db()
    cur = conn.cursor()
    sid = d["seat"]

    # Get schedule_id
    cur.execute("SELECT schedule_id FROM seats WHERE id=%s", (sid,))
    res = cur.fetchone()
    if not res:
        conn.close()
        return jsonify(ok=False, msg="Seat not found")
    schedule_id = res[0]

    # Get seat type + rates
    cur.execute("""
        SELECT se.seat_type,
               sc.seating_rate,
               sc.single_sleeper_rate,
               sc.double_sleeper_rate
        FROM seats se
        JOIN schedules sc ON sc.id = se.schedule_id
        WHERE se.id=%s
    """, (sid,))
    seat_data = cur.fetchone()
    if not seat_data:
        conn.close()
        return jsonify(ok=False, msg="Seat info not found")
    seat_type, seating_rate, single_rate, double_rate = seat_data

    # ===== Get route points =====
    cur.execute("""
        SELECT lat, lng, station_name 
        FROM route_stations 
        WHERE route_id=(SELECT route_id FROM schedules WHERE id=%s)
        ORDER BY station_order
    """, (schedule_id,))
    route_points = cur.fetchall()  # [(lat,lng,station_name), ...]

    if not route_points or len(route_points) < 2:
        conn.close()
        return jsonify(ok=False, msg="Route not properly defined")

    # ===== Distance calculation function =====
    import math
    def calculate_distance(route_points, from_name, to_name):
        station_idx_map = {p[2].strip().lower(): i for i, p in enumerate(route_points)}
        from_idx = station_idx_map.get(from_name.strip().lower())
        to_idx = station_idx_map.get(to_name.strip().lower())
        if from_idx is None or to_idx is None or from_idx >= to_idx:
            return 0

        distance = 0
        for i in range(from_idx, to_idx):
            lat1, lon1 = route_points[i][0], route_points[i][1]
            lat2, lon2 = route_points[i+1][0], route_points[i+1][1]
            if None in (lat1, lon1, lat2, lon2):
                continue
            dlat = math.radians(lat2 - lat1)
            dlon = math.radians(lon2 - lon1)
            a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
            c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
            distance += 6371 * c
        return distance

    distance = calculate_distance(route_points, d["from"], d["to"])
    if distance == 0:
        conn.close()
        return jsonify(ok=False, msg="Invalid from/to station")

    # ===== Fare calculation =====
    if seat_type == "SEATING":
        rate = seating_rate
    elif seat_type == "SLEEPER_SINGLE":
        rate = single_rate
    elif seat_type == "SLEEPER_DOUBLE":
        rate = double_rate
    else:
        rate = seating_rate
    fare = round(distance * rate, 2)

    # ===== Check overlapping bookings =====
    station_idx_map = {p[2].strip().lower(): i for i, p in enumerate(route_points)}
    cur.execute("""
        SELECT from_station, to_station 
        FROM seat_bookings 
        WHERE seat_id=%s AND schedule_id=%s AND booking_date=%s
    """, (sid, schedule_id, d["date"]))
    for b_from, b_to in cur.fetchall():
        b_from_idx = station_idx_map.get(b_from.strip().lower())
        b_to_idx = station_idx_map.get(b_to.strip().lower())
        if b_from_idx is None or b_to_idx is None:
            continue
        if not (station_idx_map[d["to"].strip().lower()] <= b_from_idx or station_idx_map[d["from"].strip().lower()] >= b_to_idx):
            conn.close()
            return jsonify(ok=False, msg="Seat already booked for this segment")

    # ===== Insert booking =====
    cur.execute("""
        INSERT INTO seat_bookings 
        (seat_id, schedule_id, passenger_name, mobile, from_station, to_station, booking_date, fare)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
    """, (sid, schedule_id, d["name"], d["mobile"], d["from"], d["to"], d["date"], fare))
    conn.commit()
    conn.close()

    # Notify front-end
    socketio.emit("seat_booked", {"seat": sid})
    return jsonify(ok=True, msg=f"Seat Booked! Distance: {distance:.2f} km | Fare: ₹{fare}")
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
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE schedules SET current_lat=%s,current_lng=%s WHERE id=%s", (d["lat"],d["lng"],d["bus_id"]))
    conn.commit()
    conn.close()
    socketio.emit("bus_location",d)
    return jsonify(ok=True)

# ================= ROUTE POINTS =================
@app.route("/route_points/<int:sid>")
def route_points(sid):
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT rs.lat, rs.lng, rs.station_name FROM route_stations rs JOIN schedules s ON s.route_id=rs.route_id WHERE s.id=%s ORDER BY rs.station_order",(sid,))
    points = cur.fetchall()
    conn.close()
    return jsonify(points)

# ================= LAST LOCATION =================
@app.route("/bus_location/<int:bus_id>")
def bus_location(bus_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT current_lat,current_lng FROM schedules WHERE id=%s", (bus_id,))
    row = cur.fetchone()
    conn.close()
    if row and row[0]:
        return jsonify(lat=row[0], lng=row[1])
    return jsonify(lat=None, lng=None)

# ================= MAIN =================
if __name__ == "__main__":
    print("Server started...")
    socketio.run(app, host="0.0.0.0", port=5000, debug=True, allow_unsafe_werkzeug=True)