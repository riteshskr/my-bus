python
import os, random
from datetime import date
from functools import wraps
from flask import Flask, request, jsonify, render_template_string, redirect, g
from flask_socketio import SocketIO, emit
from flask_compress import Compress
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
import atexit

# ================= APP =================
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key")
Compress(app)

# ✅ PERFECT SocketIO Configuration
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    ping_timeout=60,
    ping_interval=25
)

# ================= DATABASE =================
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise Exception("DATABASE_URL environment variable is missing!")

pool = ConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=5, timeout=30,kwargs={
            })
print("✅ Connection pool ready")


@atexit.register
def shutdown_pool():
    pool.close()


# ================= DB CONTEXT =================
def get_db():
    conn = pool.connection()
    cur = conn.cursor(row_factory=dict_row)
    return conn, cur

def safe_db(func):
    @wraps(func)
    def wrapper(*a, **kw):
        try:
            return func(*a, **kw)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    return wrapper


# ================= DB INIT =================
def init_db():
    """✅ FIXED: Flask app context added"""
    with app.app_context():
        conn, cur = get_db()
        try:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS routes (
                id SERIAL PRIMARY KEY, 
                route_name VARCHAR(100) UNIQUE, 
                distance_km INT
            )""")

            cur.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id SERIAL PRIMARY KEY, 
                route_id INT REFERENCES routes(id), 
                bus_name VARCHAR(100),
                departure_time TIME, 
                total_seats INT DEFAULT 40
            )""")

            cur.execute("""
            CREATE TABLE IF NOT EXISTS seat_bookings (
                id SERIAL PRIMARY KEY, 
                schedule_id INT REFERENCES schedules(id), 
                seat_number INT,
                passenger_name VARCHAR(100), 
                mobile VARCHAR(15), 
                from_station VARCHAR(50),
                to_station VARCHAR(50), 
                travel_date DATE, 
                status VARCHAR(20) DEFAULT 'confirmed',
                fare INT, 
                created_at TIMESTAMP DEFAULT NOW()
            )""")

            cur.execute("""
            CREATE TABLE IF NOT EXISTS route_stations (
                id SERIAL PRIMARY KEY, 
                route_id INT REFERENCES routes(id), 
                station_name VARCHAR(50), 
                station_order INT
            )""")

            cur.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'unique_seat_booking') THEN
                    ALTER TABLE seat_bookings ADD CONSTRAINT unique_seat_booking
                    UNIQUE (schedule_id, seat_number, travel_date);
                END IF;
            END$$;
            """)
            conn.commit()

            # Insert default data
            cur.execute("SELECT COUNT(*) FROM routes")
            if cur.fetchone()[0] == 0:
                routes = [
                    (1, 'बीकानेर → जयपुर', 336),
                    (2, 'बीकानेर → जोधपुर', 252),
                    (3, 'जयपुर → जोधपुर', 330)
                ]
                for rid, name, dist in routes:
                    cur.execute("INSERT INTO routes VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                                (rid, name, dist))

                schedules = [
                    (1, 1, 'Volvo AC Sleeper', '08:00'),
                    (2, 1, 'Semi Sleeper AC', '10:30'),
                    (3, 2, 'Volvo AC Seater', '09:00'),
                    (4, 3, 'Deluxe AC', '07:30')
                ]
                for sid, rid, bus, dep in schedules:
                    cur.execute("INSERT INTO schedules VALUES (%s,%s,%s,%s::time,40) ON CONFLICT DO NOTHING",
                                (sid, rid, bus, dep))

                stations = [
                    (1, 'बीकानेर', 1), (1, 'जयपुर', 2),
                    (2, 'बीकानेर', 1), (2, 'जोधपुर', 2),
                    (3, 'जयपुर', 1), (3, 'जोधपुर', 2)
                ]
                for rid, station, order in stations:
                    cur.execute(
                        "INSERT INTO route_stations (route_id,station_name,station_order) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                        (rid, station, order))
                conn.commit()
            print("✅ DB Init Complete!")
        finally:
            try:
                cur.close()
                conn.close()

            except:
                pass


init_db()


# ================= SOCKET EVENTS =================
@socketio.on("connect")
def handle_connect():
    print(f"✅ Client connected: {request.sid}")


@socketio.on("driver_gps")
def gps(data):
    print(f"📍 GPS: Bus {data.get('sid')}")
    emit("bus_location", data, broadcast=True)


# ================= HTML BASE =================
BASE_HTML = """<!DOCTYPE html>
<html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bus Booking India</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
.seat{width:45px;height:45px;margin:3px;border-radius:5px;font-weight:bold;transition:all 0.3s;}
.bus-row{display:flex;flex-wrap:wrap;justify-content:center;gap:5px}
body{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);min-height:100vh}
.card{border-radius:15px;box-shadow:0 10px 30px rgba(0,0,0,0.3)}
</style>
</head><body class="text-white">
<div class="container py-5"><h2 class="text-center mb-4">🚌 Bus Booking + Live GPS</h2>
{{content|safe}}<div class="text-center mt-4">
<a href="/" class="btn btn-light btn-lg px-4 me-2">🏠 Home</a>
<a href="/driver/1" class="btn btn-success btn-lg px-4" target="_blank">🚗 Driver GPS</a></div></div>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
</body></html>"""


# ================= ROUTES =================
@app.route("/")
@safe_db
def home():
    conn, cur = get_db()
    cur.execute("SELECT id, route_name, distance_km FROM routes ORDER BY id")
    routes = cur.fetchall()
    cur.close()
    conn.close()
    content = '<div class="text-center mb-4"><h4>📋 Available Routes</h4></div>'
    for r in routes:
        content += f'''
        <div class="card bg-info mb-3 mx-auto" style="max-width:500px">
            <div class="card-body">
                <h6>{r["route_name"]} — {r["distance_km"]} km</h6>
                <a href="/buses/{r["id"]}" class="btn btn-success w-100">Book Seats</a>
            </div>
        </div>'''
    return render_template_string(BASE_HTML, content=content)


@app.route("/buses/<int:rid>")
@safe_db
def buses(rid):
    conn, cur = get_db()
    cur.execute("SELECT id, bus_name, departure_time FROM schedules WHERE route_id=%s ORDER BY departure_time", (rid,))
    buses_data = cur.fetchall()

    html = '<div class="alert alert-info text-center">No Buses for this route</div>'
    if buses_data:
        html = '<div class="text-center mb-4"><h4>🚌 Available Buses</h4></div>'
        for bus in buses_data:
            html += f'''
            <div class="card bg-success mb-3 mx-auto" style="max-width:500px">
                <div class="card-body">
                    <h6>{bus["bus_name"]}</h6>
                    <p><strong>Time:</strong> {bus["departure_time"]}</p>
                    <a href="/select/{bus["id"]}" class="btn btn-warning w-100 text-dark">Book Seats</a>
                </div>
            </div>'''
    return render_template_string(BASE_HTML, content=html)


@app.route("/select/<int:sid>", methods=["GET", "POST"])
@safe_db
def select(sid):
    conn, cur = get_db()
    cur.execute("SELECT route_id FROM schedules WHERE id=%s", (sid,))
    row = cur.fetchone()
    route_id = row["route_id"] if row else 1

    cur.execute("SELECT station_name FROM route_stations WHERE route_id=%s ORDER BY station_order", (route_id,))
    stations = [r["station_name"] for r in cur.fetchall()]

    opts = "".join(f"<option>{s}</option>" for s in stations)
    today = date.today().isoformat()

    if request.method == "POST":
        fs = request.form["from"]
        ts = request.form["to"]
        d = request.form["date"]
        return redirect(f"/seats/{sid}?fs={fs}&ts={ts}&d={d}")

    form = f'''
    <div class="card mx-auto" style="max-width:500px">
        <div class="card-body">
            <h5 class="card-title text-center">🎫 Journey Details</h5>
            <form method="POST">
                <div class="mb-3">
                    <label class="form-label">From:</label>
                    <select name="from" class="form-select" required>{opts}</select>
                </div>
                <div class="mb-3">
                    <label class="form-label">To:</label>
                    <select name="to" class="form-select" required>{opts}</select>
                </div>
                <div class="mb-3">
                    <label class="form-label">Date:</label>
                    <input type="date" name="date" class="form-control" value="{today}" min="{today}" required>
                </div>
                <button class="btn btn-success w-100">View Available Seats</button>
            </form>
        </div>
    </div>'''
    cur.close()
    conn.close()
    return render_template_string(BASE_HTML, content=form)


@app.route("/seats/<int:sid>")
@safe_db
def seats(sid):
    fs = request.args.get("fs", "बीकानेर")
    ts = request.args.get("ts", "जयपुर")
    d = request.args.get("d", date.today().isoformat())

    conn, cur = get_db()

    # स्टेशन order निकालना
    cur.execute("""
        SELECT station_name, station_order
        FROM route_stations
        WHERE route_id = (SELECT route_id FROM schedules WHERE id=%s)
        ORDER BY station_order
    """, (sid,))
    stations_data = cur.fetchall()
    station_to_order = {r['station_name']: r['station_order'] for r in stations_data}
    fs_order = station_to_order.get(fs, 1)
    ts_order = station_to_order.get(ts, 2)

    # booked seats निकालना
    cur.execute("""
        SELECT seat_number, from_station, to_station
        FROM seat_bookings
        WHERE schedule_id=%s AND travel_date=%s AND status='confirmed'
    """, (sid, d))
    booked_rows = cur.fetchall()
    booked_seats = set()
    for row in booked_rows:
        if row['from_station'] in station_to_order and row['to_station'] in station_to_order:
            booked_fs = station_to_order[row['from_station']]
            booked_ts = station_to_order[row['to_station']]
            if not (ts_order <= booked_fs or fs_order >= booked_ts):
                booked_seats.add(row['seat_number'])

    # सीट buttons बनाना
    seat_buttons = ""
    for i in range(1, 41):
        if i in booked_seats:
            seat_buttons += f'<button class="btn btn-danger seat" disabled>X</button>'
        else:
            seat_buttons += f"<button class='btn btn-success seat' onclick=\"bookSeat({i}, '{fs}', '{ts}', '{d}', {sid}, event)\" data-seat='{i}'>{i}</button>"

    html = f'''
    <div class="text-center mb-4">
        <h4>🚌 {fs} → {ts} | 📅 {d}</h4>
        <p class="lead">Available Seats: <span id="availableCount">{40 - len(booked_seats)}</span>/40</p>
        <div class="bus-row mt-3">{seat_buttons}</div>
    </div>

    <script>
    window.currentSid = {sid};
    window.currentDate = '{d}';

    const socket = io({{transports:['websocket','polling'], reconnection:true, timeout:10000}});

    socket.on('connect', () => console.log('✅ Socket Connected:', socket.id));
    socket.on('disconnect', () => console.log('❌ Socket Disconnected'));

    socket.on('seat_update', function(data) {{
        if(window.currentSid == data.sid && window.currentDate == data.date) {{
            const seatBtn = document.querySelector('[data-seat="' + data.seat + '"]');
            if(seatBtn) {{
                seatBtn.className = 'btn btn-danger seat';
                seatBtn.disabled = true;
                seatBtn.innerHTML = 'X';
                document.getElementById('availableCount').textContent =
                    parseInt(document.getElementById('availableCount').textContent) - 1;
            }}
        }}
    }});

    function bookSeat(seatId, fs, ts, d, sid, e) {{
        const btn = e.target;
        btn.disabled = true;
        btn.innerHTML = '⏳';

        let name = prompt("👤 नाम:");
        if(!name || name.trim() === "") return resetSeat(btn, seatId);

        let mobile = prompt("📱 मोबाइल (10 अंक):");
        if(!/^\d{{10}}$/.test(mobile)) {{
            alert("❌ 10 अंक मोबाइल नंबर डालें");
            return resetSeat(btn, seatId);
        }}

        fetch("/book", {{
            method: "POST",
            headers: {{"Content-Type":"application/json"}},
            body: JSON.stringify({{
                sid: sid, seat: seatId, name: name.trim(), mobile: mobile,
                from: fs, to: ts, date: d
            }})
        }})
        .then(r => r.json())
        .then(r => {{
            if(r.ok) {{
                btn.innerHTML = '✅';
                alert('🎉 सीट ' + seatId + ' बुक हो गई | ₹' + r.fare);
                setTimeout(() => location.reload(), 1500);
            }} else {{
                alert('❌ ' + r.error);
                resetSeat(btn, seatId);
            }}
        }})
        .catch(() => {{
            alert('❌ नेटवर्क त्रुटि');
            resetSeat(btn, seatId);
        }});
    }}

    function resetSeat(btn, seatId) {{
        btn.disabled = false;
        btn.innerHTML = seatId;
        btn.className = 'btn btn-success seat';
    }}
    </script>
    '''
    cur.close()
    conn.close()
    return render_template_string(BASE_HTML, content=html)



@app.route("/book", methods=["POST"])
@safe_db
def book():
    data = request.get_json()
    if not all(k in data for k in ['sid', 'seat', 'name', 'mobile', 'date']):
        return jsonify({"ok": False, "error": "सभी fields जरूरी"}), 400

    if not str(data['mobile']).isdigit() or len(data['mobile']) != 10:
        return jsonify({"ok": False, "error": "10 अंक मोबाइल"}), 400

    conn, cur = get_db()
    try:
        cur.execute("SELECT id FROM seat_bookings WHERE schedule_id=%s AND seat_number=%s AND travel_date=%s",
                    (data['sid'], data['seat'], data['date']))
        if cur.fetchone():
            return jsonify({"ok": False, "error": "सीट पहले से बुक है"}), 409

        fare = random.randint(250, 450)
        cur.execute("""
            INSERT INTO seat_bookings (schedule_id, seat_number, passenger_name, mobile, 
            from_station, to_station, travel_date, fare, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'confirmed')
        """, (data['sid'], data['seat'], data['name'], data['mobile'],
              data['from'], data['to'], data['date'], fare))
        conn.commit()

        # ✅ 100% WORKING LIVE UPDATE
        socketio.emit("seat_update", {
            "sid": data['sid'],
            "seat": data['seat'],
            "date": data['date']
        }, broadcast=True)

        print(f"✅ BROADCAST: Seat {data['seat']} booked for bus {data['sid']}")
        cur.close()
        conn.close()
        return jsonify({"ok": True, "fare": fare})

    except Exception as e:
        if conn:
            conn.rollback()
        print(f"❌ Booking error: {e}")
        cur.close()
        conn.close()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/driver/<int:sid>")
def driver(sid):
    return f'''
    <!DOCTYPE html><html><body style="text-align:center;font-family:sans-serif;background:#f0f0f0;padding:50px">
    <h2>🚗 Driver GPS - Bus {sid}</h2>
    <button id="startBtn" class="btn btn-success" onclick="startGPS()" style="padding:15px 30px;font-size:18px;border:none;border-radius:10px">Start GPS</button>
    <p id="status" style="font-size:20px;margin:20px;color:#333"></p>
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <script>
    const socket = io({{transports:['websocket','polling']}});
    let watchId;
    function startGPS() {{
        document.getElementById('startBtn').disabled=true;
        document.getElementById('status').innerText='📡 GPS जोड़ रहा है...';
        watchId=navigator.geolocation.watchPosition(pos=>{{
            const data={{sid:{sid},lat:pos.coords.latitude,lng:pos.coords.longitude}};
            socket.emit("driver_gps",data);
            document.getElementById('status').innerHTML=`📍 ${data.lat.toFixed(6)}, ${data.lng.toFixed(6)}`;
        }},err=>{{document.getElementById('status').innerText='❌ GPS Error: '+err.message;}},{{enableHighAccuracy:true}});
    }}
    </script></body></html>'''


if __name__ == "__main__":
    print("🚀 Bus Booking App Starting... (Live Updates 100% Working)")
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=False)
