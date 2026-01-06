import os
import mysql.connector
from flask import Flask, render_template_string, request, redirect, url_for, jsonify
from flask_socketio import SocketIO

# ================= APP =================
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "super-secret-key")

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading"
)

# ================= DB CONFIG =================
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),  # Cloud DB host from Render env
    "user": os.environ.get("DB_USER", "root"),
    "password": os.environ.get("DB_PASS", "*#06041974"),
    "database": os.environ.get("DB_NAME", "busdb1"),
    "autocommit": True
}


def get_db():
    return mysql.connector.connect(**DB_CONFIG)


# ================= INIT DB =================
def init_db():
    """Run only once to create tables and sample data"""
    conn = get_db()
    cur = conn.cursor(buffered=True)
    cur.execute("SET FOREIGN_KEY_CHECKS=0")

    # Drop old tables (first time only)
    for t in ["seat_bookings", "seats", "schedules", "route_stations", "routes"]:
        cur.execute(f"DROP TABLE IF EXISTS {t}")

    # Create tables
    cur.execute("""
        CREATE TABLE routes(
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(200)
        )
    """)
    cur.execute("""
        CREATE TABLE route_stations(
            id INT AUTO_INCREMENT PRIMARY KEY,
            route_id INT,
            station_name VARCHAR(50),
            station_order INT,
            arrival_time TIME,
            distance FLOAT
        )
    """)
    cur.execute("""
        CREATE TABLE schedules(
            id INT AUTO_INCREMENT PRIMARY KEY,
            bus_name VARCHAR(100),
            route_id INT,
            departure_time TIME,
            current_lat DOUBLE DEFAULT NULL,
            current_lng DOUBLE DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP

        )
    """)
    cur.execute("""
        CREATE TABLE seats(
            id INT AUTO_INCREMENT PRIMARY KEY,
            schedule_id INT,
            seat_no VARCHAR(10)
        )
    """)
    cur.execute("""
        CREATE TABLE seat_bookings(
            id INT AUTO_INCREMENT PRIMARY KEY,
            seat_id INT,
            schedule_id INT,
            passenger_name VARCHAR(100),
            mobile VARCHAR(15),
            from_station VARCHAR(50),
            to_station VARCHAR(50),
            from_time TIME,
            to_time TIME,
            fare FLOAT,
            booking_date DATE
        )
    """)

    # Sample data
    routes = [("बीकानेर → जयपुर",), ("बीकानेर → जोधपुर",), ("जयपुर → जोधपुर",)]
    cur.executemany("INSERT INTO routes (name) VALUES (%s)", routes)

    stations = [
        (1, "बीकानेर", 1, "08:00:00", 0),
        (1, "सिकर", 2, "10:00:00", 150),
        (1, "जयपुर", 3, "14:00:00", 350),
        (2, "बीकानेर", 1, "08:30:00", 0),
        (2, "जोधपुर", 2, "12:30:00", 350),
        (3, "जयपुर", 1, "09:00:00", 0),
        (3, "जोधपुर", 2, "13:00:00", 400)
    ]
    cur.executemany("""
        INSERT INTO route_stations (route_id,station_name,station_order,arrival_time,distance)
        VALUES (%s,%s,%s,%s,%s)
    """, stations)

    buses = [
        ("RSRTC Volvo", 1, "08:00:00"),
        ("RSRTC Deluxe", 1, "10:00:00"),
        ("RSRTC Express", 2, "09:00:00"),
        ("RSRTC SuperFast", 3, "09:30:00")
    ]
    cur.executemany(
        "INSERT INTO schedules (bus_name,route_id,departure_time) VALUES (%s,%s,%s)", buses
    )

    # Seats
    for sid in range(1, len(buses) + 1):
        for i in range(1, 41):
            cur.execute("INSERT INTO seats (schedule_id,seat_no) VALUES (%s,%s)", (sid, f"S{i}"))

    cur.execute("SET FOREIGN_KEY_CHECKS=1")
    conn.close()


# ================= HTML =================
BASE_HTML = """
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bus Booking</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
<style>
.seat{width:45px;height:45px;margin:3px}
.bus-row{display:flex;flex-wrap:wrap;justify-content:center}
</style>
</head>
<body class="bg-dark text-white">
<div class="container py-3">
<h4 class="text-center mb-3">🚌 Bus Booking</h4>
{{content|safe}}
<a href="/" class="btn btn-light w-100 mt-3">Home</a>
</div>

<script>
var socket = io();
socket.on("seat_booked", d => {
  let b = document.querySelector("[data-seat='"+d.seat+"']");
  if(b){ b.classList.replace("btn-warning","btn-danger"); b.disabled=true; }
});

function bookSeat(seat,fs,ts,d){
 let n=prompt("Name"); let m=prompt("Mobile");
 if(!n||!m) return;
 fetch("/book",{
   method:"POST",
   headers:{"Content-Type":"application/json"},
   body:JSON.stringify({seat:seat,name:n,mobile:m,from:fs,to:ts,date:d})
 }).then(r=>r.json()).then(r=>{
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
def home():
    conn = get_db();
    cur = conn.cursor(buffered=True)
    cur.execute("SELECT id,name FROM routes")
    html = "".join(f"<a class='btn btn-success w-100 mb-2' href='/buses/{i}'>{n}</a>" for i, n in cur.fetchall())
    conn.close()
    return render_template_string(BASE_HTML, content=html)


@app.route("/buses/<int:rid>")
def buses(rid):
    conn = get_db();
    cur = conn.cursor(buffered=True)
    cur.execute("SELECT id,bus_name,departure_time FROM schedules WHERE route_id=%s", (rid,))
    html = "".join(f"<a class='btn btn-info w-100 mb-2' href='/select/{i}'>{n} ({t})</a>" for i, n, t in cur.fetchall())
    conn.close()
    return render_template_string(BASE_HTML, content=html)


@app.route("/select/<int:sid>", methods=["GET", "POST"])
def select(sid):
    conn = get_db();
    cur = conn.cursor(buffered=True)
    cur.execute("""
        SELECT station_name FROM route_stations rs
        JOIN schedules s ON s.route_id=rs.route_id
        WHERE s.id=%s ORDER BY station_order
    """, (sid,))
    stations = [x[0] for x in cur.fetchall()]
    if request.method == "POST":
        return redirect(
            url_for("seats", sid=sid, fs=request.form["from"], ts=request.form["to"], d=request.form["date"]))
    opts = "".join(f"<option>{s}</option>" for s in stations)
    conn.close()
    return render_template_string(BASE_HTML, content=f"""
        <form method="post" class="bg-light text-dark p-3 rounded">
          <select name="from" class="form-select mb-2">{opts}</select>
          <select name="to" class="form-select mb-2">{opts}</select>
          <input type="date" name="date" class="form-control mb-2" required>
          <button class="btn btn-success w-100">Show Seats</button>
        </form>
    """)


# ================= Seat Display & Booking =================
@app.route("/seats/<int:sid>")
def seats(sid):
    fs, ts, d = request.args["fs"], request.args["ts"], request.args["d"]
    conn = get_db();
    cur = conn.cursor(buffered=True)

    # All seats
    cur.execute("SELECT id, seat_no FROM seats WHERE schedule_id=%s", (sid,))
    seats = cur.fetchall()

    # Booked seats with segments
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

    cur.execute(
        "SELECT station_order FROM route_stations WHERE route_id=(SELECT route_id FROM schedules WHERE id=%s) AND station_name=%s",
        (sid, fs))
    sel_from_order = cur.fetchone()[0]
    cur.execute(
        "SELECT station_order FROM route_stations WHERE route_id=(SELECT route_id FROM schedules WHERE id=%s) AND station_name=%s",
        (sid, ts))
    sel_to_order = cur.fetchone()[0]

    html = f"<h6 class='text-center'>Selected: {fs} → {ts} | Date: {d}</h6><div class='bus-row'>"
    for s_id, s_no in seats:
        status = "green"
        if s_id in seat_map:
            for b_from_order, b_to_order in seat_map[s_id]:
                if not (sel_to_order <= b_from_order or sel_from_order >= b_to_order):
                    status = "red"
                    break
                elif (sel_from_order < b_to_order and sel_to_order > b_from_order):
                    status = "yellow"
        color = {"green": "btn-success", "yellow": "btn-warning", "red": "btn-danger"}[status]
        disabled = "disabled" if status == "red" else ""
        html += f"<button class='btn {color} seat' {disabled} data-seat='{s_id}' onclick=\"bookSeat({s_id},'{fs}','{ts}','{d}')\">{s_no}</button>"
    html += "</div>"
    conn.close()
    return render_template_string(BASE_HTML, content=html)


@app.route("/book", methods=["POST"])
def book():
    d = request.json
    conn = get_db();
    cur = conn.cursor(buffered=True)

    cur.execute("SELECT schedule_id FROM seats WHERE id=%s", (d["seat"],))
    sid = cur.fetchone()[0]

    cur.execute(
        "SELECT station_order FROM route_stations WHERE route_id=(SELECT route_id FROM schedules WHERE id=%s) AND station_name=%s",
        (sid, d["from"]))
    from_order = cur.fetchone()[0]
    cur.execute(
        "SELECT station_order FROM route_stations WHERE route_id=(SELECT route_id FROM schedules WHERE id=%s) AND station_name=%s",
        (sid, d["to"]))
    to_order = cur.fetchone()[0]

    # Check overlap
    cur.execute(
        "SELECT seat_id, from_station, to_station FROM seat_bookings WHERE schedule_id=%s AND booking_date=%s AND seat_id=%s",
        (sid, d["date"], d["seat"]))
    bookings = cur.fetchall()
    for _, b_from, b_to in bookings:
        cur.execute(
            "SELECT station_order FROM route_stations WHERE route_id=(SELECT route_id FROM schedules WHERE id=%s) AND station_name=%s",
            (sid, b_from))
        b_from_order = cur.fetchone()[0]
        cur.execute(
            "SELECT station_order FROM route_stations WHERE route_id=(SELECT route_id FROM schedules WHERE id=%s) AND station_name=%s",
            (sid, b_to))
        b_to_order = cur.fetchone()[0]
        if not (to_order <= b_from_order or from_order >= b_to_order):
            conn.close()
            return jsonify(ok=False, msg="This seat is already booked for overlapping segment.")

    # Fare calculation
    cur.execute("""
        SELECT fs.arrival_time, ts.arrival_time, fs.distance, ts.distance
        FROM route_stations fs
        JOIN route_stations ts ON fs.route_id = ts.route_id
        WHERE fs.station_name=%s AND ts.station_name=%s AND fs.station_order < ts.station_order AND fs.route_id=(SELECT route_id FROM schedules WHERE id=%s)
    """, (d["from"], d["to"], sid))
    from_time, to_time, df, dt = cur.fetchone()
    fare = round((dt - df) * 2.5, 2)

    # Insert booking
    cur.execute("""
        INSERT INTO seat_bookings
        (seat_id, schedule_id, passenger_name, mobile, from_station, to_station, from_time, to_time, fare, booking_date)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (d["seat"], sid, d["name"], d["mobile"], d["from"], d["to"], from_time, to_time, fare, d["date"]))

    conn.commit();
    conn.close()
    socketio.emit("seat_booked", {"seat": d["seat"]})
    return jsonify(ok=True, msg="Seat Booked Successfully")


# ================= MAIN =================
if __name__ == "__main__":
    # init_db()  # Uncomment only first time
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=True, allow_unsafe_werkzeug=True)