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
                   SET current_lat=%s, current_lng=%s
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
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🚌 Bus Booking India - Live GPS + Real-time Seats</title>

    <!-- Bootstrap 5.3 CSS -->
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">

    <!-- Leaflet Maps CSS (Live GPS के लिए) -->
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>

    <!-- Font Awesome Icons -->
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">

    <style>
        /* Bus Booking Theme */
        body {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 50%, #f093fb 100%);
            min-height: 100vh;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        }

        .main-container {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(20px);
            border-radius: 25px;
            box-shadow: 0 25px 50px rgba(0,0,0,0.2);
            margin: 20px auto;
            padding: 30px;
        }

        /* Route Cards */
        .route-card {
            transition: all 0.4s ease;
            border: none;
            border-radius: 20px;
            overflow: hidden;
            height: 100%;
        }

        .route-card:hover {
            transform: translateY(-10px) scale(1.02);
            box-shadow: 0 30px 60px rgba(0,0,0,0.3) !important;
        }

        /* Bus Cards */
        .bus-card {
            border-radius: 20px;
            border: none;
            transition: all 0.3s;
            cursor: pointer;
        }

        .bus-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 20px 40px rgba(0,0,0,0.2) !important;
        }

        /* Seat Layout */
        .seat {
            width: 55px !important;
            height: 55px !important;
            margin: 4px;
            font-weight: bold;
            border-radius: 12px !important;
            font-size: 14px;
            transition: all 0.3s ease;
            border: 3px solid transparent;
        }

        .seat:hover:not(:disabled) {
            transform: scale(1.1);
            box-shadow: 0 8px 25px rgba(0,0,0,0.3);
        }

        .bus-row {
            display: flex;
            flex-wrap: wrap;
            justify-content: center;
            gap: 8px;
            max-width: 900px;
            margin: 0 auto;
        }

        /* Live GPS Map */
        #map, .mini-map {
            height: 400px;
            width: 100%;
            border-radius: 20px;
            box-shadow: 0 15px 35px rgba(0,0,0,0.2);
            margin-bottom: 20px;
        }

        .live-bus {
            animation: pulse 2s infinite;
            width: 30px;
            height: 30px;
            background: #ff4444;
            border-radius: 50%;
            border: 4px solid #fff;
            box-shadow: 0 0 20px #ff4444;
        }

        @keyframes pulse {
            0%, 100% { transform: scale(1); }
            50% { transform: scale(1.3); }
        }

        /* Booking Header */
        .booking-header {
            background: linear-gradient(135deg, #667eea, #764ba2);
            border-radius: 25px;
            color: white;
            padding: 30px;
            margin-bottom: 30px;
            box-shadow: 0 20px 40px rgba(0,0,0,0.2);
        }

        /* Navigation Buttons */
        .nav-buttons {
            background: rgba(255,255,255,0.2);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 20px;
            margin-top: 40px;
        }

        .btn-custom {
            border-radius: 25px;
            padding: 12px 30px;
            font-weight: 600;
            border: none;
            transition: all 0.3s;
            text-transform: uppercase;
            letter-spacing: 1px;
        }

        .btn-custom:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 25px rgba(0,0,0,0.2);
        }

        /* Responsive */
        @media (max-width: 768px) {
            .seat { width: 45px !important; height: 45px !important; font-size: 12px; }
            .main-container { margin: 10px; padding: 20px; }
        }

        /* Loading Animation */
        .loading {
            display: inline-block;
            width: 20px;
            height: 20px;
            border: 3px solid rgba(255,255,255,.3);
            border-radius: 50%;
            border-top-color: #fff;
            animation: spin 1s ease-in-out infinite;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }
    </style>
</head>

<body>
    <div class="container-fluid py-4">
        <div class="main-container">
            <!-- Header -->
            <div class="text-center mb-5">
                <h1 class="display-4 fw-bold mb-3">
                    <i class="fas fa-bus-alt me-3"></i>
                    Bus Booking India
                </h1>
                <p class="lead mb-0">
                    <i class="fas fa-map-marker-alt me-2 text-success"></i>
                    Live GPS Tracking + Real-time Seat Booking
                </p>
            </div>

            <!-- Main Content -->
            {{content|safe}}

            <!-- Navigation -->
            <div class="nav-buttons text-center">
                <a href="/" class="btn btn-light btn-lg btn-custom me-3">
                    <i class="fas fa-home me-2"></i>🏠 Home
                </a>
                <a href="/driver/1" class="btn btn-success btn-lg btn-custom" target="_blank">
                    <i class="fas fa-map-marker-alt me-2"></i>📱 Driver GPS
                </a>
                <a href="/live-bus/1" class="btn btn-primary btn-lg btn-custom">
                    <i class="fas fa-route me-2"></i>🗺️ Live Track
                </a>
            </div>
        </div>
    </div>

    <!-- Bootstrap JS -->
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>

    <!-- Socket.IO CDN (Real-time Updates) -->
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>

    <!-- Leaflet Maps JS (GPS Tracking) -->
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>

    <!-- Route Selection Flow -->
    <script>
        // Route card click handler
        function selectRoute(routeId) {
            window.location.href = `/buses/${routeId}`;
        }

        // Bus card click handler
        function selectBus(busId) {
            window.location.href = `/select/${busId}`;
        }

        // Smooth hover effects
        document.addEventListener('DOMContentLoaded', function() {
            document.querySelectorAll('.route-card, .bus-card').forEach(card => {
                card.style.transition = 'all 0.4s ease';
                card.addEventListener('mouseenter', () => {
                    card.style.transform = 'translateY(-8px) scale(1.02)';
                });
                card.addEventListener('mouseleave', () => {
                    card.style.transform = 'translateY(0) scale(1)';
                });
            });
        });

        // Socket connection status
        if (typeof io !== 'undefined') {
            const socket = io({
                transports: ['websocket', 'polling'],
                timeout: 10000
            });

            socket.on('connect', () => {
                console.log('✅ Socket Connected:', socket.id);
            });

            socket.on('connect_error', (err) => {
                console.log('❌ Socket Error:', err.message);
            });
        }
    </script>
</body>
</html>"""


# ================= ROUTES =================
@app.route("/")
@safe_db
def home():
    conn, cur = get_db()

    # सभी Routes (बड़े cards)
    cur.execute("SELECT id, route_name, distance_km FROM routes ORDER BY id")
    routes = cur.fetchall()

    # Hero Section
    hero_section = '''
    <div class="text-center p-5 bg-gradient-primary text-white rounded-4 shadow-lg mx-auto mb-5" style="max-width:800px;">
        <h1 class="display-4 fw-bold mb-4">🚌 Bus Booking India</h1>
        <p class="lead mb-5">Live GPS Tracking + Real-time Seat Booking</p>
        <h4 class="mb-4">📍 सबसे पहले अपना Route चुनें:</h4>
    </div>
    '''

    # 🔥 Route Selection Cards (बड़ा + Clear)
    routes_section = '<div class="row g-4 mb-5">'
    for r in routes:
        routes_section += f'''
        <div class="col-md-6 col-lg-4">
            <div class="card h-100 bg-info text-white shadow-lg border-0 hover-scale" style="border-radius:20px;cursor:pointer;">
                <div class="card-body p-5 text-center" onclick="selectRoute({r['id']})">
                    <h3 class="fw-bold mb-3">{r['route_name']}</h3>
                    <div class="display-4 text-warning mb-4">🛣️ {r['distance_km']} km</div>
                    <div class="h5 mb-3">⚡ Live GPS Tracking</div>
                    <button class="btn btn-success btn-lg px-5">
                        🚀 Buses देखें → Bus {r['id']}
                    </button>
                </div>
            </div>
        </div>'''
    routes_section += '</div>'

    # Live GPS Status (नीचे छोटा)
    cur.execute("""
        SELECT s.id, s.bus_name, r.route_name, 
               s.current_lat as lat, s.current_lng as lng
        FROM schedules s JOIN routes r ON s.route_id = r.id
        ORDER BY s.id LIMIT 4
    """)
    live_buses = cur.fetchall()

    live_section = '<h3 class="text-center mb-4">🟢 Live Running Buses</h3><div class="row g-4">'
    for bus in live_buses:
        status = "🟢 LIVE GPS" if bus.get('lat') else "⚪ Ready"
        coords = f'{float(bus["lat"]):.4f}, {float(bus["lng"]):.4f}' if bus.get('lat') else '---'
        live_section += f'''
        <div class="col-md-6 col-lg-3">
            <div class="card border-0 shadow">
                <div class="card-body text-center p-3">
                    <h6 class="fw-bold">{bus['bus_name']}</h6>
                    <small class="text-muted">{bus['route_name']}</small><br>
                    <span class="badge {"bg-success" if bus.get("lat") else "bg-secondary"}">{status}</span>
                    <div class="mt-2"><small>📍 {coords}</small></div>
                </div>
            </div>
        </div>'''
    live_section += '</div>'

    content = hero_section + routes_section + live_section
    return render_template_string(BASE_HTML, content=content)


@app.route("/buses/<int:rid>")
@safe_db
def buses(rid):
    conn, cur = get_db()

    # Route name + stations
    cur.execute("""
        SELECT r.route_name, r.distance_km, 
               string_agg(rs.station_name, ' → ' ORDER BY rs.station_order) as stations
        FROM routes r 
        LEFT JOIN route_stations rs ON r.id = rs.route_id 
        WHERE r.id = %s 
        GROUP BY r.id, r.route_name, r.distance_km
    """, (rid,))
    route = cur.fetchone()

    if not route:
        return "❌ Route नहीं मिला", 404

    # सभी schedules with LIVE GPS status
    cur.execute("""
        SELECT s.id, s.bus_name, s.departure_time, s.total_seats,
               s.current_lat, s.current_lng,
               COALESCE(bk.count, 0) as booked_count
        FROM schedules s 
        LEFT JOIN (
            SELECT schedule_id, COUNT(*) as count 
            FROM seat_bookings 
            WHERE travel_date = CURRENT_DATE AND status='confirmed'
            GROUP BY schedule_id
        ) bk ON s.id = bk.schedule_id
        WHERE s.route_id = %s 
        ORDER BY s.departure_time
    """, (rid,))
    buses_data = cur.fetchall()

    # Header
    html = f'''
    <div class="text-center mb-5 booking-header">
        <h2 class="display-4 fw-bold mb-3">
            🚌 <span class="text-warning">{route['route_name']}</span>
        </h2>
        <div class="h4 mb-4 text-white-50">
            📍 {route['stations']} | 🛣️ {route['distance_km']} km
        </div>
        <p class="lead mb-0">⏰ सभी बसों का समय + Live GPS ट्रैकिंग</p>
    </div>
    '''

    if not buses_data:
        html += '<div class="alert alert-warning text-center"><h4>⚠️ आज इस रूट पर कोई बस नहीं</h4></div>'
    else:
        html += '<h3 class="text-center mb-5">🚌 आज उपलब्ध बसें</h3>'

        # Schedule cards - बड़ा + attractive design
        for bus in buses_data:
            dep_time = bus['departure_time'].strftime('%H:%M')
            gps_status = "🟢 LIVE GPS" if bus.get('current_lat') else "⚪ Ready"
            gps_coords = f"{bus['current_lat']:.4f}, {bus['current_lng']:.4f}" if bus.get('current_lat') else ""
            seats_left = bus['total_seats'] - bus['booked_count']

            html += f'''
            <div class="row mb-5">
                <div class="col-lg-8 mx-auto">
                    <div class="card bus-card h-100 shadow-lg border-0" style="border-radius:25px;">
                        <div class="card-body p-5 text-center position-relative overflow-hidden">
                            <!-- GPS Badge -->
                            <div class="position-absolute top-0 end-0 m-3">
                                <span class="badge fs-6 px-3 py-2 {"bg-success text-white" if bus.get('current_lat') else "bg-secondary"}">
                                    {gps_status}
                                </span>
                            </div>

                            <!-- Bus Info -->
                            <div class="mb-4">
                                <h3 class="fw-bold mb-3 display-6">{bus['bus_name']}</h3>
                                <div class="h2 text-primary mb-4">
                                    <i class="fas fa-clock me-2"></i>{dep_time}
                                </div>
                                <div class="row text-center">
                                    <div class="col-md-4">
                                        <div class="h5 mb-1 text-success">🎫</div>
                                        <div>सीटें बाकी</div>
                                        <div class="h4 fw-bold text-success">{seats_left}</div>
                                    </div>
                                    <div class="col-md-4">
                                        <div class="h5 mb-1 text-info">💺</div>
                                        <div>कुल</div>
                                        <div class="h4 fw-bold text-info">{bus['total_seats']}</div>
                                    </div>
                                    <div class="col-md-4">
                                        <div class="h5 mb-1 text-warning">📱</div>
                                        <div>GPS</div>
                                        <div class="h6 {"text-success fw-bold" if bus.get('current_lat') else "text-muted"}">
                                            {gps_status}
                                        </div>
                                    </div>
                                </div>
                            </div>

                            {gps_coords and f'''
                            <div class="alert alert-warning mt-4">
                                📍 LIVE लोकेशन: <strong>{gps_coords}</strong>
                            </div>''' or ""}

                            <!-- Action Buttons -->
                            <div class="d-grid gap-3 d-md-flex mt-4">
                                <a href="/live-bus/{bus['id']}" class="btn btn-primary btn-lg flex-fill btn-custom">
                                    <i class="fas fa-map-marker-alt me-2"></i>Live GPS
                                </a>
                                <a href="/select/{bus['id']}" class="btn btn-success btn-lg flex-fill btn-custom">
                                    <i class="fas fa-chair me-2"></i>सीट बुक करें
                                </a>
                            </div>
                        </div>
                    </div>
                </div>
            </div>'''

    # Back button
    html += '''
    <div class="text-center mt-5">
        <a href="/" class="btn btn-outline-light btn-lg btn-custom">
            <i class="fas fa-arrow-left me-2"></i>← सभी Routes देखें
        </a>
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

    # Seat buttons
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

    # Get current bus location (if any)
    cur.execute("SELECT current_lat, current_lng FROM schedules WHERE id=%s", (sid,))
    bus_loc = cur.fetchone()
    lat = float(bus_loc['current_lat'] or 27.2)
    lng = float(bus_loc['current_lng'] or 75.0)

    # Script for seat booking + map + live location
    script = f'''
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>

    <style>
    .bus-row {{ display: flex; flex-wrap: wrap; justify-content: center; gap: 8px; }}
    .seat {{ width: 55px !important; height: 55px !important; font-weight: bold; border-radius: 8px !important; }}
    .bus-row > div {{ flex: 0 0 auto; }}
    .live-bus{{width:30px;height:30px;background:#ff4444;border-radius:50%;border:3px solid #fff;box-shadow:0 0 15px #ff4444;animation:pulse 2s infinite;}}
    @keyframes pulse{{0%,100%{{transform:scale(1);}}50%{{transform:scale(1.3);}}}}
    </style>

    <script>
    // Global config
    window.sid = {sid};
    window.fs = "{fs.replace("'", "\\'")}";
    window.ts = "{ts.replace("'", "\\'")}";
    window.date = "{d}";

    const socket = io({{
        transports: ["websocket", "polling"],
        reconnection: true,
        timeout: 20000,
        reconnectionAttempts: 5
    }});

    function bookSeat(seatId, btn){{
        btn.disabled = true;
        btn.innerHTML = "⏳";
        btn.className = "btn btn-warning seat";

        let name = prompt("👤 यात्री का नाम:");
        if(!name || !name.trim()){{
            resetSeat(btn, seatId); return;
        }}
        let mobile = prompt("📱 मोबाइल (9876543210):");
        if(!mobile || !/^[6-9][0-9]{{9}}$/.test(mobile)){{
            alert("❌ 10 अंक मोबाइल (6-9 से start)"); resetSeat(btn, seatId); return;
        }}

        fetch("/book", {{
            method:"POST",
            headers:{{"Content-Type":"application/json"}},
            body:JSON.stringify({{
                sid:window.sid, seat:seatId, name:name.trim(),
                mobile:mobile, from:window.fs, to:window.ts, date:window.date
            }})
        }})
        .then(r=>r.json())
        .then(data=>{{
            if(data.ok){{
                btn.innerHTML="✅";
                btn.className="btn btn-success seat";
                socket.emit("seat_update", {{sid:window.sid, seat:seatId, date:window.date}});
                alert(`🎉 बुकिंग सफल! नाम: ${{name.trim()}} सीट: ${{seatId}} किराया: ₹${{data.fare}}`);
                setTimeout(()=>location.reload(),2000);
            }} else {{
                alert("❌ बुकिंग असफल: "+data.error); resetSeat(btn, seatId);
            }}
        }})
        .catch(e=>{{alert("❌ सर्वर एरर!"); resetSeat(btn, seatId);}});
    }}

    function resetSeat(btn, seatId){{
        btn.disabled=false;
        btn.innerHTML=seatId;
        btn.className="btn btn-success seat";
        btn.style.cursor="pointer";
    }}

    // LIVE seat update
    socket.on("seat_update", function(data){{
        if(window.sid==data.sid && window.date==data.date){{
            const seatBtn=document.querySelector(`[data-seat="${{data.seat}}"]`);
            if(seatBtn && !seatBtn.disabled && seatBtn.innerHTML!="✅"){{
                seatBtn.className="btn btn-danger seat";
                seatBtn.disabled=true;
                seatBtn.innerHTML="X";
                const count=document.getElementById("availableCount");
                if(count) count.textContent=parseInt(count.textContent)-1;
            }}
        }}
    }});

    // ===== Leaflet Map =====
    const map = L.map('seat-map').setView([{lat}, {lng}], 13);
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
        attribution:'© OpenStreetMap'
    }}).addTo(map);

    let busMarker = L.marker([{lat},{lng}], {{
        icon:L.divIcon({{html:'<div class="live-bus"></div>', className:'bus-marker', iconSize:[30,30]}})
    }}).addTo(map);

    socket.on("bus_location", function(data){{
        if(data.sid==window.sid){{
            const lat=parseFloat(data.lat);
            const lng=parseFloat(data.lng);
            busMarker.setLatLng([lat,lng]);
            map.setView([lat,lng],13, {{animate:true}});
        }}
    }});
    </script>
    '''

    html = f'''
       <!-- MAP -->
        <div id="seat-map" class="rounded-4 mb-3" style="height:240px; max-width:900px; margin:auto;"></div>

        <!-- Seats -->
        <div class="bus-row" style="max-width:800px; margin:0 auto;">
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

    # ===== Fetch Bus + Route info from DB =====
    cur.execute("""
        SELECT s.id, s.bus_name, s.departure_time,
               r.id as route_id, r.route_name, r.distance_km,
               s.current_lat as lat, s.current_lng as lng
        FROM schedules s 
        JOIN routes r ON s.route_id = r.id 
        WHERE s.id = %s
    """, (sid,))
    bus = cur.fetchone()
    if not bus:
        return "Bus not found", 404

    lat = float(bus.get('lat', 27.2))
    lng = float(bus.get('lng', 74.2))

    # ===== Fetch Route Stations =====
    cur.execute("""
        SELECT lat, lng, station_name
        FROM route_stations
        WHERE route_id=%s
        ORDER BY station_order
    """, (bus['route_id'],))
    stations = cur.fetchall()
    stations_json = json.dumps(stations)

    # ===== HTML + JS =====
    content = f'''
    <div class="text-center">
        <h2>🚌 {bus['bus_name']}</h2>
        <h4>{bus['route_name']} ({bus['distance_km']} km)</h4>
        <div>{ "🟢 LIVE GPS" if bus.get('lat') else "📡 Waiting for GPS..." }</div>
    </div>
    <div id="map"></div>

    <script>
    // ===== Leaflet Map =====
    const map = L.map('map').setView([{lat}, {lng}], {13 if bus.get('lat') else 10});
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
        attribution: '© OpenStreetMap'
    }}).addTo(map);

    // ===== Stations & Polyline =====
    const stations = {stations_json};
    let routePoints = [];

    stations.forEach(st => {{
        const lat = parseFloat(st.lat);
        const lng = parseFloat(st.lng);
        if(!isNaN(lat) && !isNaN(lng)){{
            routePoints.push([lat,lng]);
            L.marker([lat,lng]).addTo(map).bindPopup("📍 " + st.station_name);
        }}
    }});

    let routeLine = null;
    if(routePoints.length > 1){{
        routeLine = L.polyline(routePoints, {{
            color: 'blue',
            weight: 6,
            opacity: 0.8
        }}).addTo(map);
        map.fitBounds(routePoints);
    }}

    // ===== Bus Icon =====
    const busIcon = L.divIcon({{
        html: '<i class="fa fa-bus"></i>',
        className: 'bus-icon',
        iconSize: [30,30]
    }});
    let busMarker = L.marker(routePoints[0] || [{lat},{lng}], {{icon: busIcon}}).addTo(map);

    // ===== SocketIO Live Update =====
    const socket = io({{transports:["websocket","polling"]}});
    const sid = {sid};

    socket.on('connect', () => {{
        console.log('✅ Socket Connected');
    }});

    socket.on('bus_location', data => {{
        if(data.sid == sid){{
            const lat = parseFloat(data.lat);
            const lng = parseFloat(data.lng);
            busMarker.setLatLng([lat,lng]);
            map.panTo([lat,lng], {{animate:true}});
        }}
    }});
    </script>
    '''

    return render_template_string(BASE_HTML, content=content)




if __name__ == "__main__":
    print("🚀 Bus Booking App Starting... (Live Updates 100% Working)")
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=True)
