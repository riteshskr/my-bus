import os
import time
from psycopg import connect  # केवल psycopg3
import psycopg.rows  # Dict cursor के लिए
import requests
from flask import Flask, render_template_string, request, redirect, url_for, jsonify
from flask_socketio import SocketIO
from datetime import date

# ================= APP =================
app = Flask(__name__)
app.secret_key = "super-secret-key"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ================= DB CONFIG =================

DB_CONFIG = {
    "host": os.getenv('DB_HOST'),
    "dbname": os.getenv('DB_NAME', "busdb1_yl2r"),  # ✅ केवल "dbname" ही काम करेगा
    "user": os.getenv('DB_USER'),
    "password": os.getenv('DB_PASSWORD'),
    "port": int(os.getenv('DB_PORT', 5432))
}

def get_db():
    conn = connect(**DB_CONFIG)
    # RealDictCursor जैसा behavior
    cur = conn.cursor(row_factory=psycopg.rows.dict_row)
    return conn, cur




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
    conn, cur = get_db()
    if route_id:
        cur.execute("SELECT id, station_name FROM route_stations WHERE route_id=%s AND (lat IS NULL OR lng IS NULL)",
                    (route_id,))
    else:
        cur.execute("SELECT id, station_name FROM route_stations WHERE lat IS NULL OR lng IS NULL")
    stations = cur.fetchall()

    for station in stations:
        lat, lng = geocode_station(station['station_name'])
        if lat and lng:  # ✅ केवल तब UPDATE करें**
            cur.execute("UPDATE route_stations SET lat=%s, lng=%s WHERE id=%s",
                        (lat, lng, station['id']))
            print(f"Updated {station['station_name']} -> lat:{lat}, lng:{lng}")
        else:
            print(f"Could not find coordinates for {station['station_name']}")
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
<script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
<link rel="stylesheet" href="https://unpkg.com/leaflet/dist/leaflet.css">
<script src="https://unpkg.com/leaflet/dist/leaflet.js"></script>
<style>
.seat{width:45px;height:45px;margin:3px}
.bus-row{display:flex;flex-wrap:wrap;justify-content:center}
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
    conn,cur = get_db()
    #cur = conn.cursor()
    cur.execute("SELECT id,name FROM routes")
    routes = cur.fetchall()
    html = "".join(f"<a class='btn btn-success w-100 mb-2' href='/buses/{r['id']}'>{r['name']}</a>" for r in routes)
    conn.close()
    return render_template_string(BASE_HTML, content=html)

# ================= BUSES =================
@app.route("/buses/<int:rid>")
def buses(rid):
    fill_missing_latlng(rid)  # Auto-fill for this route
    conn,cur = get_db()
    #cur = conn.cursor()
    cur.execute("SELECT id,bus_name,departure_time FROM schedules WHERE route_id=%s", (rid,))
    schedules = cur.fetchall()
    html = "".join(
        f"<a class='btn btn-info w-100 mb-2' href='/select/{s['id']}'>{s['bus_name']} ({s['departure_time']})</a>" for s
        in schedules)
    conn.close()
    return render_template_string(BASE_HTML, content=html)

# ================= FROM / TO =================
@app.route("/select/<int:sid>", methods=["GET","POST"])
def select(sid):
    conn,cur = get_db()
    #cur = conn.cursor()
    cur.execute("""
        SELECT station_name FROM route_stations rs
        JOIN schedules s ON s.route_id=rs.route_id
        WHERE s.id=%s ORDER BY station_order
    """, (sid,))
    stations_data = cur.fetchall()
    stations = [s['station_name'] for s in stations_data]
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

    conn,cur = get_db()
    #cur = conn.cursor()

    # Seats
    cur.execute("SELECT id, seat_no FROM seats WHERE schedule_id=%s", (sid,))
    seats = cur.fetchall()

    # Booked seats
    cur.execute("SELECT seat_id, from_station, to_station FROM seat_bookings WHERE schedule_id=%s AND booking_date=%s",
                (sid, d))
    bookings = cur.fetchall()

    seat_map = {}
    for booking in bookings:
        seat_id = booking['seat_id']
        b_from = booking['from_station']
        b_to = booking['to_station']
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
    for seat in seats:
        seat_id = seat['id']
        seat_no = seat['seat_no']
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
    conn, cur = get_db()
    sid = d["seat"]

    # Schedule ID लें
    cur.execute("SELECT schedule_id FROM seats WHERE id=%s", (sid,))
    schedule_id = cur.fetchone()[0]

    # Selected from/to station orders
    cur.execute("""
        SELECT station_order FROM route_stations 
        WHERE route_id=(SELECT route_id FROM schedules WHERE id=%s) 
        AND station_name=%s
    """, (schedule_id, d["from"]))
    sel_from_order = cur.fetchone()[0]

    cur.execute("""
        SELECT station_order FROM route_stations 
        WHERE route_id=(SELECT route_id FROM schedules WHERE id=%s) 
        AND station_name=%s
    """, (schedule_id, d["to"]))
    sel_to_order = cur.fetchone()[0]

    # ✅ FIXED: Overlapping booking check (psycopg3 dict_row)
    cur.execute("""
        SELECT from_station, to_station FROM seat_bookings 
        WHERE seat_id=%s AND schedule_id=%s AND booking_date=%s
    """, (sid, schedule_id, d["date"]))
    bookings = cur.fetchall()  # Dict rows

    for booking in bookings:  # ✅ Dict access
        b_from = booking['from_station']
        b_to = booking['to_station']

        # Booking के station orders
        cur.execute("""
            SELECT station_order FROM route_stations 
            WHERE route_id=(SELECT route_id FROM schedules WHERE id=%s) 
            AND station_name=%s
        """, (schedule_id, b_from))
        b_from_order = cur.fetchone()[0]

        cur.execute("""
            SELECT station_order FROM route_stations 
            WHERE route_id=(SELECT route_id FROM schedules WHERE id=%s) 
            AND station_name=%s
        """, (schedule_id, b_to))
        b_to_order = cur.fetchone()[0]

        # Overlap check logic
        if not (sel_to_order <= b_from_order or sel_from_order >= b_to_order):
            conn.close()
            return jsonify(ok=False, msg="Seat already booked for this segment")

    # Fare calculation (distance based)
    cur.execute("""
        SELECT fs.distance, ts.distance 
        FROM route_stations fs 
        JOIN route_stations ts ON fs.route_id=ts.route_id 
        WHERE fs.station_name=%s AND ts.station_name=%s 
        AND fs.route_id=(SELECT route_id FROM schedules WHERE id=%s)
    """, (d["from"], d["to"], schedule_id))

    result = cur.fetchone()
    if result:
        df, dt = result
        fare = round((dt - df) * 2.5, 2)  # ₹2.5 per km
    else:
        fare = 100.0  # Default fare

    # ✅ Booking insert
    cur.execute("""
        INSERT INTO seat_bookings (
            seat_id, schedule_id, passenger_name, mobile, 
            from_station, to_station, booking_date, fare
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (sid, schedule_id, d["name"], d["mobile"],
          d["from"], d["to"], d["date"], fare))

    conn.commit()

    # Real-time seat update
    conn.close()
    socketio.emit("seat_booked", {"seat": sid})

    return jsonify(
        ok=True,
        msg=f"✅ Seat Booked! Fare: ₹{fare}",
        booking={
            "seat_id": sid,
            "name": d["name"],
            "mobile": d["mobile"],
            "from": d["from"],
            "to": d["to"],
            "date": d["date"],
            "fare": fare
        }
    )


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
    conn,cur = get_db()
    #cur = conn.cursor()
    cur.execute("UPDATE schedules SET current_lat=%s,current_lng=%s WHERE id=%s", (d["lat"],d["lng"],d["bus_id"]))
    conn.commit()
    conn.close()
    socketio.emit("bus_location",d)
    return jsonify(ok=True)

# ================= ROUTE POINTS =================
@app.route("/route_points/<int:sid>")
def route_points(sid):
    conn,cur = get_db()
    #cur = conn.cursor(dictionary=True)
    cur.execute("SELECT rs.lat, rs.lng, rs.station_name FROM route_stations rs JOIN schedules s ON s.route_id=rs.route_id WHERE s.id=%s ORDER BY rs.station_order",(sid,))
    points = cur.fetchall()
    conn.close()
    return jsonify(points)

# ================= LAST LOCATION =================
@app.route("/bus_location/<int:bus_id>")
def bus_location(bus_id):
    conn,cur = get_db()
    #cur = conn.cursor()
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