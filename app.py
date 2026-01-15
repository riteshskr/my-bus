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
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                    logger=True, engineio_logger=True, ping_timeout=60)

# ================= DATABASE =================
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise Exception("DATABASE_URL environment variable is missing!")

pool = ConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=10, timeout=20)
print("✅ Connection pool ready")


@atexit.register
def shutdown_pool():
    pool.close()


# ================= DB CONTEXT =================
def get_db():
    if 'db_conn' not in g:
        g.db_conn = pool.getconn()
    return g.db_conn, g.db_conn.cursor(row_factory=dict_row)


@app.teardown_appcontext
def close_db(error=None):
    conn = g.pop('db_conn', None)
    if conn:
        pool.putconn(conn)


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
                current_lat DOUBLE PRECISION,
                current_lng DOUBLE PRECISION,
                
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
        except Exception as e:
            print(f"❌ DB init failed: {e}")
            conn.rollback()


init_db()


# ================= SOCKET EVENTS =================
@socketio.on("connect")
def handle_connect():
    print(f"✅ Client connected: {request.sid}")


@socketio.on("driver_gps")
def gps(data):
    sid = data.get('sid')
    lat = float(data.get('lat', 27.5))
    lng = float(data.get('lng', 75.0))
    speed = float(data.get('speed', 0))

    print(f"📍 LIVE: Bus-{sid} @ [{lat:.5f},{lng:.5f}] {speed}km/h")

    # Save to DB
    try:
        with app.app_context():
            conn, cur = get_db()
            cur.execute("""
                   UPDATE schedules 
                   SET current_lat=%s, current_lng=%s, last_updated=NOW()
                   WHERE id=%s
               """, (lat, lng, sid))
            conn.commit()
    except:
        pass

    emit("bus_location", {
        "sid": sid, "lat": lat, "lng": lng, "speed": speed,
        "timestamp": data.get('timestamp', '')
    }, broadcast=True)


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

    # सभी routes
    cur.execute("SELECT id, route_name, distance_km FROM routes ORDER BY id")
    routes = cur.fetchall()

    # ✅ LIVE BUS STATUS - schedules से current location
    cur.execute("""
        SELECT s.id, s.bus_name, r.route_name, 
               s.current_lat as lat, s.current_lng as lng,
               s.last_updated
        FROM schedules s JOIN routes r ON s.route_id = r.id
        ORDER BY s.last_updated DESC NULLS LAST LIMIT 6
    """)
    live_buses = cur.fetchall()

    # Hero Section
    hero_section = '''
    <div class="text-center mb-6 p-5 bg-gradient-primary text-white rounded-4 shadow-lg mx-auto" style="max-width:700px;">
        <h1 class="display-4 fw-bold mb-4">🚌 Bus Booking India</h1>
        <p class="lead mb-5">Live GPS Tracking + Real-time Seat Booking</p>
        <div class="d-flex flex-column flex-md-row gap-3 justify-content-center">
            <a href="/live-tracking" class="btn btn-light btn-lg px-5">🗺️ Live Tracking</a>
            <a href="/buses/1" class="btn btn-success btn-lg px-5">🎫 Book Seats</a>
        </div>
    </div>
    '''

    # Live Buses Section
    live_html = '<h3 class="text-center mb-4">🟢 Live Buses</h3><div class="row g-4 mb-5">'
    for bus in live_buses:
        status = "🟢 LIVE" if bus.get('lat') else "⚪ Ready"
        coords = f'{float(bus["lat"]):.4f}, {float(bus["lng"]):.4f}' if bus.get('lat') else '---'
        live_html += f'''
        <div class="col-md-6 col-lg-4">
            <a href="/live-bus/{bus['id']}" class="text-decoration-none">
                <div class="card h-100 border-0 shadow hover-shadow">
                    <div class="card-body text-center p-4">
                        <h5 class="fw-bold">{bus['bus_name']}</h5>
                        <div class="text-muted mb-2">{bus['route_name']}</div>
                        <div class="h6 mb-1">{status}</div>
                        <small class="text-success">📍 {coords}</small>
                        <div class="mt-3">
                            <span class="btn btn-sm btn-outline-success">Live Track →</span>
                        </div>
                    </div>
                </div>
            </a>
        </div>'''
    live_html += '</div>'

    # Routes Cards
    routes_section = '<h3 class="text-center mb-4">📋 Available Routes</h3>'
    for r in routes:
        routes_section += f'''
        <div class="card bg-info mb-4 mx-auto shadow-lg" style="max-width:550px;border-radius:20px;">
            <div class="card-body p-4 text-center">
                <h4 class="card-title mb-3 text-white fw-bold">{r["route_name"]}</h4>
                <div class="display-6 text-warning mb-3">🛣️ {r["distance_km"]} km</div>
                <a href="/buses/{r["id"]}" class="btn btn-success btn-lg px-5">Book Seats →</a>
            </div>
        </div>'''

    content = hero_section + live_html + routes_section
    return render_template_string(BASE_HTML, content=content)


@app.route("/buses/<int:rid>")
@safe_db
def buses(rid):
    conn, cur = get_db()

    # Route name
    cur.execute("SELECT route_name FROM routes WHERE id=%s", (rid,))
    route = cur.fetchone()
    route_name = route['route_name'] if route else "Unknown Route"

    # Live buses इस route के
    cur.execute("""
        SELECT s.id, s.bus_name, s.departure_time,
               s.current_lat, s.current_lng, s.last_updated
        FROM schedules s 
        WHERE s.route_id = %s 
        ORDER BY s.departure_time
    """, (rid,))
    buses_data = cur.fetchall()

    html = f'''
    <div class="text-center mb-5">
        <h3 class="display-5 fw-bold">🚌 {route_name}</h3>
        <p class="lead text-muted">Live GPS + Seat Booking</p>
    </div>
    '''

    if not buses_data:
        html += '<div class="alert alert-info text-center">No Buses Available</div>'
    else:
        for bus in buses_data:
            status = "🟢 LIVE" if bus.get('current_lat') else "⚪ Ready"
            coords = f'{float(bus["current_lat"]):.4f}, {float(bus["current_lng"]):.4f}' if bus.get(
                'current_lat') else ''

            html += f'''
            <div class="card bg-gradient-success text-white mb-4 mx-auto shadow-lg" style="max-width:550px;border-radius:20px;">
                <div class="card-body p-4">
                    <div class="d-flex justify-content-between align-items-start mb-3">
                        <div>
                            <h5 class="fw-bold mb-1">{bus["bus_name"]}</h5>
                            <p class="mb-1"><strong>⏰ Departure:</strong> {bus["departure_time"]}</p>
                        </div>
                        <span class="badge bg-light text-dark fs-6 px-3 py-2">{status}</span>
                    </div>

                    {coords and f'''
                    <div class="alert alert-warning alert-dismissible fade show" role="alert" style="font-size:0.9rem;">
                        📍 LIVE GPS: <strong>{coords}</strong>
                        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="alert"></button>
                    </div>''' or ""}

                    <div class="d-grid gap-2 d-md-flex justify-content-md-between">
                        <a href="/live-bus/{bus['id']}" class="btn btn-outline-light flex-fill me-md-2">
                            📍 Live Track
                        </a>
                        <a href="/select/{bus['id']}" class="btn btn-light flex-fill text-dark">
                            🎫 Book Seats
                        </a>
                    </div>
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
    return render_template_string(BASE_HTML, content=form)


@app.route("/seats/<int:sid>")
@safe_db
def seats(sid):
    fs = request.args.get("fs", "बीकानेर")
    ts = request.args.get("ts", "जयपुर")
    d = request.args.get("d", date.today().isoformat())

    conn, cur = get_db()

    # Stations mapping
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

    # Booked seats calculation
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

    # 🔥 FIXED SEAT BUTTONS - हर button में onclick direct!
    seat_buttons = ""
    available_count = 40 - len(booked_seats)

    for i in range(1, 41):
        if i in booked_seats:
            seat_buttons += f'<button class="btn btn-danger seat" disabled>X</button>'
        else:
            seat_buttons += f'''
            <button class="btn btn-success seat" 
                    data-seat="{i}" 
                    onclick="bookSeat({i}, this)"
                    style="cursor:pointer; width:50px; height:50px; margin:2px;">
                {i}
            </button>'''

    # ✅ PERFECT WORKING SCRIPT - Socket + Socket.IO CDN दोनों!
    script = f'''
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <script>
    // Global config
    window.sid = {sid};
    window.fs = "{fs.replace("'", "\\'")}";
    window.ts = "{ts.replace("'", "\\'")}";
    window.date = "{d}";

    // Socket connection
    const socket = io({{
        transports: ["websocket", "polling"],
        reconnection: true,
        timeout: 20000,
        reconnectionAttempts: 5
    }});

    console.log("🚀 Seat page loaded - Socket connected");

    // ⭐ MAIN BOOKING FUNCTION - हर onclick यहीं आएगा
    function bookSeat(seatId, btn) {{
        console.log("🚌 Booking seat:", seatId);

        // Visual feedback
        btn.disabled = true;
        btn.innerHTML = "⏳";
        btn.className = "btn btn-warning seat";

        // Name input
        let name = prompt("👤 यात्री का नाम:");
        if(!name || !name.trim()) {{
            resetSeat(btn, seatId);
            return;
        }}

        // Mobile validation
        let mobile = prompt("📱 मोबाइल (9876543210):");
        if(!mobile || !/^[6-9][0-9]{{9}}$/.test(mobile)) {{
            alert("❌ 10 अंक मोबाइल (6-9 से start)!\\nउदाहरण: 9876543210");
            resetSeat(btn, seatId);
            return;
        }}

        // Server booking
        fetch("/book", {{
            method: "POST",
            headers: {{"Content-Type": "application/json"}},
            body: JSON.stringify({{
                sid: window.sid,
                seat: seatId,
                name: name.trim(),
                mobile: mobile,
                from: window.fs,
                to: window.ts,
                date: window.date
            }})
        }})
        .then(response => response.json())
        .then(data => {{
            console.log("📋 Booking response:", data);
            if(data.ok) {{
                btn.innerHTML = "✅";
                btn.className = "btn btn-success seat";

                // Live broadcast
                socket.emit("seat_update", {{
                    sid: window.sid,
                    seat: seatId,
                    date: window.date
                }});

                alert(`🎉 बुकिंग सफल!\\nनाम: ${{name.trim()}}\\nसीट: ${{seatId}}\\nकिराया: ₹${{data.fare}}`);
                setTimeout(() => location.reload(), 2000);
            }} else {{
                alert("❌ बुकिंग असफल: " + data.error);
                resetSeat(btn, seatId);
            }}
        }})
        .catch(error => {{
            console.error("❌ Network error:", error);
            alert("❌ सर्वर एरर! फिर कोशिश करें।");
            resetSeat(btn, seatId);
        }});
    }}

    function resetSeat(btn, seatId) {{
        btn.disabled = false;
        btn.innerHTML = seatId;
        btn.className = "btn btn-success seat";
        btn.style.cursor = "pointer";
    }}

    // ⭐ LIVE UPDATES - दूसरे tab में instant red
    socket.on("seat_update", function(data) {{
        console.log("📡 Live update received:", data);
        if(window.sid == data.sid && window.date == data.date) {{
            const seatBtn = document.querySelector(`[data-seat="${{data.seat}}"]`);
            if(seatBtn && !seatBtn.disabled && seatBtn.innerHTML != "✅") {{
                seatBtn.className = "btn btn-danger seat";
                seatBtn.disabled = true;
                seatBtn.innerHTML = "X";

                // Count update
                const count = document.getElementById("availableCount");
                if(count) {{
                    count.textContent = parseInt(count.textContent) - 1;
                }}
            }}
        }}
    }});

    // Connection status
    socket.on("connect", () => console.log("✅ Socket connected:", socket.id));
    socket.on("disconnect", () => console.log("❌ Socket disconnected"));
    </script>
    '''

    html = f'''
    <style>
    .bus-row {{ display: flex; flex-wrap: wrap; justify-content: center; gap: 8px; }}
    .seat {{ width: 55px !important; height: 55px !important; font-weight: bold; border-radius: 8px !important; }}
    .bus-row > div {{ flex: 0 0 auto; }}
    </style>

    <div class="text-center mb-5">
        <div class="card bg-gradient-primary text-white mx-auto mb-4" style="max-width: 600px;">
            <div class="card-body py-4">
                <h3 class="mb-2">🚌 {fs} → {ts}</h3>
                <h5 class="mb-3">📅 {d}</h5>
                <div class="h4">सीटें उपलब्ध: <span id="availableCount" class="badge bg-success fs-3">{available_count}</span>/40</div>
            </div>
        </div>

        <div class="bus-row" style="max-width: 800px; margin: 0 auto;">
            {seat_buttons}
        </div>

        <div class="mt-4">
            <small class="text-muted">
                💚 हरी = उपलब्ध | 🔴 लाल = बुक | ⏳ बुक हो रही | ✅ बुक हो गई
            </small>
        </div>
    </div>

    {script}
    '''

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
        })

        print(f"✅ BROADCAST: Seat {data['seat']} booked for bus {data['sid']}")
        return jsonify({"ok": True, "fare": fare})

    except Exception as e:
        if conn:
            conn.rollback()
        print(f"❌ Booking error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500



@app.route("/driver/<int:sid>")
def driver(sid):
    return f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Bus {sid} GPS</title>

    <style>
        body {{
            background-color: #f0f0f0;
            padding: 40px;
            text-align: center;
            font-family: sans-serif;
            margin: 0;
        }}
        h2 {{
            color: #333;
        }}
        .btn-gps {{
            padding: 15px 30px;
            font-size: 18px;
            border: none;
            border-radius: 10px;
            background-color: #28a745;
            color: white;
            cursor: pointer;
            font-weight: bold;
        }}
        .btn-stop {{
            padding: 15px 30px;
            font-size: 18px;
            border: none;
            border-radius: 10px;
            background-color: #dc3545;
            color: white;
            cursor: pointer;
            font-weight: bold;
            margin-left: 10px;
        }}
        #status {{
            font-size: 18px;
            margin-top: 25px;
            color: #333;
            font-family: monospace;
            padding: 15px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
    </style>
</head>

<body>

    <h2>🚗 Driver GPS – Bus {sid}</h2>

    <button id="startBtn" class="btn-gps" onclick="startGPS()">🚀 GPS शुरू करें</button>
    <button id="stopBtn" class="btn-stop" onclick="stopGPS()" disabled>🛑 GPS बंद करें</button>

    <div id="status">GPS बंद है</div>

    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <script>
        const socket = io({{ transports: ["websocket", "polling"] }});
        let watchId = null;

        function startGPS() {{
            const startBtn = document.getElementById("startBtn");
            const stopBtn = document.getElementById("stopBtn");
            const status = document.getElementById("status");

            // ✅ GPS support check
            if (!navigator.geolocation) {{
                status.innerHTML = "❌ इस ब्राउज़र में GPS सपोर्ट नहीं है";
                return;
            }}

            startBtn.disabled = true;
            stopBtn.disabled = false;
            startBtn.innerHTML = "⏳ GPS चालू हो रहा है...";
            status.innerHTML = "📡 GPS खोज रहे हैं...";

            watchId = navigator.geolocation.watchPosition(
                function (pos) {{
                    const lat = pos.coords.latitude.toFixed(6);
                    const lng = pos.coords.longitude.toFixed(6);

                    const data = {{
                        sid: {sid},
                        lat: lat,
                        lng: lng
                    }};

                    socket.emit("driver_gps", data);

                    status.innerHTML = "✅ LIVE GPS<br>Latitude: " + lat + "<br>Longitude: " + lng;
                    startBtn.innerHTML = "🚗 Live GPS चल रहा है";
                }},
                function (err) {{
                    status.innerHTML = "❌ GPS Error: " + err.message;
                    startBtn.disabled = false;
                    stopBtn.disabled = true;
                    startBtn.innerHTML = "🔄 GPS फिर शुरू करें";
                }},
                {{
                    enableHighAccuracy: true,
                    timeout: 10000,
                    maximumAge: 5000
                }}
            );
        }}

        function stopGPS() {{
            const startBtn = document.getElementById("startBtn");
            const stopBtn = document.getElementById("stopBtn");
            const status = document.getElementById("status");

            if (watchId !== null) {{
                navigator.geolocation.clearWatch(watchId);
                watchId = null;
            }}

            socket.emit("driver_gps_stop", {{ sid: {sid} }});

            startBtn.disabled = false;
            stopBtn.disabled = true;
            startBtn.innerHTML = "🚀 GPS शुरू करें";
            status.innerHTML = "🛑 GPS बंद कर दिया गया";
        }}
    </script>

</body>
</html>
"""


@app.route("/live-bus/<int:sid>")
@safe_db
def live_bus(sid):
    conn, cur = get_db()
    cur.execute("""
        SELECT s.id, s.bus_name, s.departure_time,
               r.route_name, r.distance_km,
               s.current_lat as lat, s.current_lng as lng, s.last_updated
        FROM schedules s JOIN routes r ON s.route_id = r.id 
        WHERE s.id = %s
    """, (sid,))
    bus = cur.fetchone()

    if not bus:
        return "Bus not found", 404

    lat = float(bus.get('lat', 27.2))
    lng = float(bus.get('lng', 74.2))
    has_gps = bus.get('lat') is not None

    content = f'''
    <style>
    #map{{height:70vh;width:100%;border-radius:20px;box-shadow:0 20px 40px rgba(0,0,0,0.3);}}
    .live-bus{{animation:pulse 2s infinite;width:30px;height:30px;background:#ff4444;border-radius:50%;border:3px solid #fff;box-shadow:0 0 20px #ff4444;}}
    @keyframes pulse{{0%,100%{{transform:scale(1);}}50%{{transform:scale(1.2);}}}}
    .stats-card{{background:rgba(255,255,255,0.95);backdrop-filter:blur(20px);}}
    </style>

    <div class="text-center mb-5">
        <h2 class="display-5 fw-bold mb-2">🚌 {bus['bus_name']}</h2>
        <h5 class="text-muted mb-1">{bus['route_name']} ({bus['distance_km']}km)</h5>
        <div class="h6 {"text-success" if has_gps else "text-warning"} mb-3">
            {"🟢 LIVE GPS" if has_gps else "📡 Waiting for GPS..."}
        </div>
    </div>

    <div class="row g-4">
        <div class="col-lg-8">
            <div id="map" class="rounded-4"></div>
        </div>
        <div class="col-lg-4">
            <div id="live-stats" class="stats-card p-4 rounded-4 shadow-lg h-100">
                <h5 class="text-center mb-4">
                    {"📱 Phone से /driver/{sid} GPS चालू करें" if not has_gps else f"📍 {lat:.5f}, {lng:.5f}"}
                </h5>
                <div id="current-location" class="h4 {"text-primary" if has_gps else "text-muted"} mb-3">
                    {"Waiting..." if not has_gps else f"📍 {lat:.5f}, {lng:.5f}"}
                </div>
                <div class="mb-3">
                    <a href="/driver/{sid}" target="_blank" class="btn btn-success w-100 mb-2">
                        📱 Driver GPS (Phone)
                    </a>
                    <a href="/" class="btn btn-outline-secondary w-100">🏠 Back to Home</a>
                </div>
                <hr>
                <div id="connection-status" class="small text-muted">
                    Socket connecting...
                </div>
            </div>
        </div>
    </div>

    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <script>
    const sid = {sid};
    const map = L.map('map').setView([{lat}, {lng}], {13 if has_gps else 10});
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
        attribution: '© OpenStreetMap | Bus Live Tracking'
    }}).addTo(map);

    {"let marker = L.marker([{lat}, {lng}]).addTo(map).bindPopup('🚌 Live Location');" if has_gps else ""}

    const socket = io({{transports:["websocket","polling"]}});

    socket.on('connect', () => {{
        document.getElementById('connection-status').innerHTML = '✅ Socket Connected | GPS Updates Live!';
    }});

    socket.on('bus_location', data => {{
        if(data.sid == sid) {{
            const pos = [parseFloat(data.lat), parseFloat(data.lng)];
            document.getElementById('current-location').innerHTML = 
                `📍 ${{data.lat.toFixed(5)}}, ${{data.lng.toFixed(5)}}`;

            {"document.getElementById('live-stats').scrollIntoView({{behavior: \\\"smooth\\\"}});"}

            if(marker) marker.setLatLng(pos);
            else {{
                marker = L.marker(pos, {{
                    icon: L.divIcon({{
                        html: '<div class="live-bus"></div>',
                        iconSize: [40,40], className: 'bus-marker'
                    }})
                }}).addTo(map);
            }}
            map.panTo(pos, {{duration: 1.5}});
        }}
    }});
    </script>
    '''

    return render_template_string(BASE_HTML, content=content)


if __name__ == "__main__":
    print("🚀 Bus Booking App Starting... (Live Updates 100% Working)")
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=True)
