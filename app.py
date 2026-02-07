from dotenv import load_dotenv
import json

load_dotenv()
import setuptools
import os, random
from datetime import date, datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify, render_template_string, redirect, g, session, send_file
from flask_socketio import SocketIO, emit
from flask_compress import Compress
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
import atexit
import razorpay
import threading
import time

# import psycopg2
razor_client = razorpay.Client(auth=(
    os.getenv("RAZORPAY_KEY_ID"),
    os.getenv("RAZORPAY_KEY_SECRET")
))
# ===== PAYMENT CONFIG =====
RAZORPAY_ENABLED = bool(
    os.getenv("RAZORPAY_KEY_ID") and
    os.getenv("RAZORPAY_KEY_SECRET")
)

if RAZORPAY_ENABLED:
    razor_client = razorpay.Client(auth=(
        os.getenv("RAZORPAY_KEY_ID"),
        os.getenv("RAZORPAY_KEY_SECRET")
    ))
else:
    razor_client = None

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

# ================= GPS BACKGROUND STORAGE =================
# Store GPS data temporarily when app is in background
gps_backup_store = {}
gps_last_update = {}


# Clean old GPS data every hour
def cleanup_gps_store():
    while True:
        time.sleep(3600)  # 1 hour
        try:
            current_time = time.time()
            keys_to_delete = []
            for key, data in gps_backup_store.items():
                if current_time - data.get('timestamp', 0) > 7200:  # 2 hours old
                    keys_to_delete.append(key)

            for key in keys_to_delete:
                del gps_backup_store[key]
                if key in gps_last_update:
                    del gps_last_update[key]

            print(f"🧹 Cleaned {len(keys_to_delete)} old GPS entries")
        except:
            pass


# Start cleanup thread
cleanup_thread = threading.Thread(target=cleanup_gps_store, daemon=True)
cleanup_thread.start()


@atexit.register
def shutdown_pool():
    pool.close()


# ================= DB CONTEXT =================
def get_db():
    try:
        if 'db_conn' not in g or g.db_conn.closed:
            g.db_conn = pool.getconn()
        if 'db_cur' not in g:
            g.db_cur = g.db_conn.cursor(row_factory=dict_row)
        return g.db_conn, g.db_cur
    except:
        pool.closeall()
        g.db_conn = pool.getconn()
        g.db_cur = g.db_conn.cursor(row_factory=dict_row)
        return g.db_conn, g.db_cur


@app.teardown_appcontext
def close_db(error=None):
    cur = g.pop('db_cur', None)
    conn = g.pop('db_conn', None)

    if cur:
        cur.close()
    if conn:
        pool.putconn(conn)


def safe_db(func):
    @wraps(func)
    def wrapper(*a, **kw):
        try:
            return func(*a, **kw)
        finally:
            close_db()  # चाहे error आये या न आये

    return wrapper


def admin_required(f):
    @wraps(f)
    def wrap(*a, **k):
        if not session.get("user_logged_in"):
            return redirect("/login")

        if session.get("role") != "admin":
            return "Access Denied", 403

        return f(*a, **k)

    return wrap


# ================= DB INIT =================
def init_db():
    try:
        conn = pool.getconn()
        cur = conn.cursor()

        # ===== TABLES =====
        # cur.execute("DROP TABLE IF EXISTS admin CASCADE;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS faces (
                id SERIAL PRIMARY KEY,
                bus_id INT NOT NULL,
                face_data BYTEA NOT NULL,
                face_image BYTEA NOT NULL
            );
            """)

        # face_logs table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS face_logs (
                id SERIAL PRIMARY KEY,
                face_id INT NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
                bus_id INT NOT NULL,
                entry_time TIMESTAMP NOT NULL,
                latitude DOUBLE PRECISION,
                longitude DOUBLE PRECISION
            );
            """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            id SERIAL PRIMARY KEY,
            username VARCHAR(50) UNIQUE,
            password VARCHAR(100),
            role VARCHAR(20) DEFAULT 'admin',
            counter_no INTEGER DEFAULT 0
        )
        """)
        cur.execute("SELECT COUNT(*) FROM admins ")
        count = cur.fetchone()[0]

        if count == 0:
            cur.execute("""
            INSERT INTO admins (username, password)
            VALUES ('admin', '1234')
            ON CONFLICT DO NOTHING
            """)

        # ===== GPS BACKUP TABLE =====
        cur.execute("""
        CREATE TABLE IF NOT EXISTS gps_backup (
            id SERIAL PRIMARY KEY,
            bus_id INT NOT NULL,
            lat DOUBLE PRECISION NOT NULL,
            lng DOUBLE PRECISION NOT NULL,
            speed DOUBLE PRECISION DEFAULT 0,
            accuracy DOUBLE PRECISION DEFAULT 50,
            battery INT DEFAULT 100,
            source VARCHAR(50),
            app_state VARCHAR(20) DEFAULT 'foreground',
            created_at TIMESTAMP DEFAULT NOW(),
            INDEX idx_bus_time (bus_id, created_at)
        )""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY,
            schedule_id INT,
            seat_number INT,
            order_id VARCHAR(100),
            payment_id VARCHAR(100),
            amount INT,
            status VARCHAR(20),
            created_at TIMESTAMP DEFAULT NOW()
        )""")

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
            last_gps_update TIMESTAMP DEFAULT NOW(),
            total_seats INT DEFAULT 40
        )""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS seat_bookings (
            id SERIAL PRIMARY KEY,
            schedule_id INT REFERENCES schedules(id) ON DELETE CASCADE,
            seat_number INT,
            passenger_name VARCHAR(100),
            mobile VARCHAR(15),
            from_station VARCHAR(50),
            to_station VARCHAR(50),
            travel_date DATE,
            status VARCHAR(20) DEFAULT 'confirmed',
            fare INT,
            payment_mode VARCHAR(10) DEFAULT 'cash',
            booked_by_type VARCHAR(10) DEFAULT 'user',
            booked_by_id INT,
            counter_id INT,
            order_id VARCHAR(100),
            payment_id VARCHAR(100),
            created_at TIMESTAMP DEFAULT NOW()
        )""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS route_stations (
            id SERIAL PRIMARY KEY, 
            route_id INT REFERENCES routes(id), 
            station_name VARCHAR(50), 
            station_order INT,
            lat DOUBLE PRECISION DEFAULT 27.2,
            lng DOUBLE PRECISION DEFAULT 75.2
        )""")

        conn.commit()

        # ===== DEFAULT DATA =====
        cur.execute("SELECT COUNT(*) FROM admins")
        count = cur.fetchone()[0]

        if count == 0:
            cur.execute(""" INSERT INTO  admins(username, password, role, counter_no)
            VALUES('admin', 'admin123', 'admin', 1);""")

        cur.execute("SELECT COUNT(*) FROM routes")
        count = cur.fetchone()[0]

        if count == 0:
            routes = [
                (1, 'बीकानेर → जयपुर', 336),
                (2, 'बीकानेर → जोधपुर', 252),
                (3, 'जयपुर → जोधपुर', 330)
            ]

            for r in routes:
                cur.execute(
                    "INSERT INTO routes VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    r
                )

            schedules = [
                (1, 1, 'Volvo AC Sleeper', '08:00'),
                (2, 1, 'Semi Sleeper AC', '10:30'),
                (3, 2, 'Volvo AC Seater', '09:00'),
                (4, 3, 'Deluxe AC', '07:30')
            ]

            for s in schedules:
                cur.execute("""
                    INSERT INTO schedules
                    (id, route_id, bus_name, departure_time, total_seats)
                    VALUES (%s,%s,%s,%s::time,40)
                    ON CONFLICT DO NOTHING
                """, s)

            stations = [
                (1, 'बीकानेर', 1),
                (1, 'जयपुर', 2),
                (2, 'बीकानेर', 1),
                (2, 'जोधपुर', 2),
                (3, 'जयपुर', 1),
                (3, 'जोधपुर', 2)
            ]

            for st in stations:
                cur.execute("""
                    INSERT INTO route_stations
                    (route_id,station_name,station_order)
                    VALUES (%s,%s,%s)
                    ON CONFLICT DO NOTHING
                """, st)

            conn.commit()

        cur.close()
        pool.putconn(conn)  # ✅ सबसे important line

        print("✅ DB Init Complete!")

    except Exception as e:
        import traceback
        print("❌ DB INIT REAL ERROR ↓")
        traceback.print_exc()

        try:
            conn.rollback()
            pool.putconn(conn, close=True)
        except:
            pass


print("✅ Connection pool ready")
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
    app_state = data.get('app_state', 'foreground')  # foreground/background

    print(f"📍 LIVE: Bus-{sid} @ [{lat:.5f},{lng:.5f}] {speed}km/h [{app_state}]")

    try:
        with app.app_context():
            conn, cur = get_db()

            # Update schedule table
            cur.execute("""
                UPDATE schedules 
                SET current_lat=%s, current_lng=%s, last_gps_update=NOW()
                WHERE id=%s
            """, (lat, lng, sid))

            # Store in GPS backup for background tracking
            if app_state == 'background':
                cur.execute("""
                    INSERT INTO gps_backup (bus_id, lat, lng, speed, source, app_state)
                    VALUES (%s, %s, %s, %s, 'mobile_app', %s)
                """, (sid, lat, lng, speed, app_state))

            conn.commit()

            # Store in memory cache for quick access
            key = f"bus_{sid}"
            gps_backup_store[key] = {
                'lat': lat,
                'lng': lng,
                'speed': speed,
                'timestamp': time.time(),
                'app_state': app_state
            }
            gps_last_update[key] = time.time()

    except Exception as e:
        print(f"GPS update error: {e}")
        pass

    # 🔥 Always emit to all clients
    socketio.emit("bus_location", {
        "sid": sid,
        "lat": lat,
        "lng": lng,
        "speed": speed,
        "app_state": app_state,
        "timestamp": data.get('timestamp', datetime.now().isoformat())
    })


# New event for app state changes
@socketio.on("app_state_change")
def handle_app_state(data):
    sid = data.get('sid')
    state = data.get('state', 'foreground')  # foreground/background
    print(f"📱 Bus-{sid} app state changed to: {state}")

    # Store last known state
    key = f"bus_{sid}_state"
    gps_backup_store[key] = {
        'state': state,
        'timestamp': time.time()
    }


# New event for background GPS sync
@socketio.on("gps_sync_background")
def sync_background_gps(data):
    """Sync multiple GPS points from background"""
    sid = data.get('sid')
    points = data.get('points', [])

    print(f"🔄 Syncing {len(points)} GPS points for Bus-{sid} from background")

    try:
        with app.app_context():
            conn, cur = get_db()
            for point in points:
                cur.execute("""
                    INSERT INTO gps_backup (bus_id, lat, lng, speed, source, app_state)
                    VALUES (%s, %s, %s, %s, 'mobile_background', 'background')
                """, (sid, point['lat'], point['lng'], point.get('speed', 0)))

            # Update latest location
            if points:
                latest = points[-1]
                cur.execute("""
                    UPDATE schedules 
                    SET current_lat=%s, current_lng=%s, last_gps_update=NOW()
                    WHERE id=%s
                """, (latest['lat'], latest['lng'], sid))

                # Emit latest location
                socketio.emit("bus_location", {
                    "sid": sid,
                    "lat": latest['lat'],
                    "lng": latest['lng'],
                    "speed": latest.get('speed', 0),
                    "app_state": "background",
                    "timestamp": latest.get('timestamp', datetime.now().isoformat())
                })

            conn.commit()

    except Exception as e:
        print(f"Background GPS sync error: {e}")


# ================= IMPROVED DRIVER PAGE =================
@app.route("/driver/<int:sid>")
def driver(sid):
    return f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Bus {sid} GPS - Enhanced</title>
    <style>
        body {{
            background-color: #1a1a2e;
            color: white;
            padding: 20px;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 0;
        }}
        .container {{
            max-width: 800px;
            margin: 0 auto;
        }}
        header {{
            text-align: center;
            padding: 20px;
            background: #16213e;
            border-radius: 15px;
            margin-bottom: 30px;
        }}
        h2 {{
            color: #4cc9f0;
            margin: 10px 0;
        }}
        .controls {{
            display: flex;
            gap: 15px;
            justify-content: center;
            flex-wrap: wrap;
            margin: 30px 0;
        }}
        .btn {{
            padding: 15px 30px;
            font-size: 16px;
            border: none;
            border-radius: 10px;
            cursor: pointer;
            font-weight: bold;
            transition: all 0.3s;
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .btn-start {{
            background: linear-gradient(45deg, #4CAF50, #8BC34A);
            color: white;
        }}
        .btn-stop {{
            background: linear-gradient(45deg, #f44336, #e91e63);
            color: white;
        }}
        .btn-secondary {{
            background: linear-gradient(45deg, #2196F3, #03A9F4);
            color: white;
        }}
        .status-card {{
            background: #0f3460;
            padding: 20px;
            border-radius: 15px;
            margin: 20px 0;
            font-family: monospace;
            font-size: 16px;
            line-height: 1.6;
        }}
        .info-grid {{
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 15px;
            margin-top: 20px;
        }}
        .info-item {{
            background: rgba(255,255,255,0.1);
            padding: 10px;
            border-radius: 8px;
        }}
        .connection-status {{
            display: inline-block;
            width: 12px;
            height: 12px;
            border-radius: 50%;
            margin-right: 10px;
        }}
        .connected {{ background: #4CAF50; box-shadow: 0 0 10px #4CAF50; }}
        .disconnected {{ background: #f44336; }}
        .instructions {{
            background: #1e3a5f;
            padding: 20px;
            border-radius: 15px;
            margin-top: 30px;
            border-left: 5px solid #4cc9f0;
        }}
        .instruction-list {{
            margin-left: 20px;
            line-height: 1.8;
        }}
        .heartbeat {{
            font-size: 12px;
            color: #888;
            text-align: center;
            margin-top: 20px;
        }}
        @media (max-width: 600px) {{
            .controls {{ flex-direction: column; }}
            .info-grid {{ grid-template-columns: 1fr; }}
        }}
    </style>
</head>

<body>
    <div class="container">
        <header>
            <h1>🚌 Driver GPS – Bus {sid}</h1>
            <p>Enhanced with Background GPS Support</p>
        </header>

        <div class="controls">
            <button id="startBtn" class="btn btn-start" onclick="startGPS()">
                <span>🚀</span> GPS शुरू करें
            </button>
            <button id="stopBtn" class="btn btn-stop" onclick="stopGPS()" disabled>
                <span>🛑</span> GPS बंद करें
            </button>
            <button class="btn btn-secondary" onclick="requestBackgroundPermission()">
                <span>⚙️</span> Background Permission
            </button>
        </div>

        <div class="status-card">
            <h3>📊 GPS Status</h3>
            <div id="status">GPS बंद है</div>

            <div class="info-grid" id="locationInfo">
                <div class="info-item"><strong>Latitude:</strong> <span id="lat">-</span></div>
                <div class="info-item"><strong>Longitude:</strong> <span id="lng">-</span></div>
                <div class="info-item"><strong>Speed:</strong> <span id="speed">0 km/h</span></div>
                <div class="info-item"><strong>Accuracy:</strong> <span id="accuracy">-</span></div>
                <div class="info-item"><strong>App State:</strong> <span id="appState">foreground</span></div>
                <div class="info-item"><strong>Last Update:</strong> <span id="lastUpdate">-</span></div>
            </div>

            <div style="margin-top: 15px;">
                <span class="connection-status" id="socketStatus" class="disconnected"></span>
                <span id="socketText">Disconnected</span>
            </div>
        </div>

        <div class="instructions">
            <h3>📱 Important Instructions for Continuous GPS:</h3>
            <ul class="instruction-list">
                <li><strong>Allow "Background Location"</strong> permission in browser settings</li>
                <li><strong>Disable battery optimization</strong> for this website</li>
                <li>Keep this tab <strong>open in background</strong></li>
                <li>Don't force close the browser</li>
                <li>For Android: Use Chrome and enable "Site Settings" → "Location" → "Allow in background"</li>
            </ul>
        </div>

        <div class="heartbeat" id="heartbeat">
            Heartbeat: ♥
        </div>
    </div>

    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <script>
        const socket = io(window.location.origin);
        const SID = {sid};

        let watchId = null;
        let lastLocation = null;
        let backgroundPoints = [];
        let appState = 'foreground';
        let heartbeatInterval = null;

        // Socket connection status
        socket.on('connect', () => {{
            updateSocketStatus(true);
        }});

        socket.on('disconnect', () => {{
            updateSocketStatus(false);
        }});

        function updateSocketStatus(connected) {{
            const statusElem = document.getElementById('socketStatus');
            const textElem = document.getElementById('socketText');

            if (connected) {{
                statusElem.className = 'connection-status connected';
                textElem.textContent = 'Connected to Server';
            }} else {{
                statusElem.className = 'connection-status disconnected';
                textElem.textContent = 'Disconnected';
            }}
        }}

        // App state detection
        document.addEventListener('visibilitychange', () => {{
            appState = document.hidden ? 'background' : 'foreground';
            document.getElementById('appState').textContent = appState;

            // Notify server about state change
            socket.emit('app_state_change', {{
                sid: SID,
                state: appState
            }});

            if (appState === 'background') {{
                console.log('📱 App went to background - GPS continues in background mode');
                // Reduce update frequency in background
                if (watchId) {{
                    navigator.geolocation.clearWatch(watchId);
                    startBackgroundGPS();
                }}
            }} else {{
                console.log('📱 App back to foreground - Switching to high accuracy GPS');
                // Restore high accuracy GPS
                if (watchId) {{
                    navigator.geolocation.clearWatch(watchId);
                    startForegroundGPS();
                }}
            }}
        }});

        // Request background permission
        function requestBackgroundPermission() {{
            if ('permissions' in navigator) {{
                navigator.permissions.query({{name: 'background-sync'}})
                    .then(permissionStatus => {{
                        console.log('Background sync permission:', permissionStatus.state);
                    }});
            }}

            if ('serviceWorker' in navigator) {{
                navigator.serviceWorker.register('/sw.js')
                    .then(registration => {{
                        console.log('Service Worker registered');
                    }});
            }}

            alert('Please enable background location in browser settings:\\n\\nChrome: Settings → Site Settings → Location → Allow in background');
        }}

        // Start GPS in foreground (high accuracy)
        function startForegroundGPS() {{
            const options = {{
                enableHighAccuracy: true,
                maximumAge: 0,
                timeout: 10000
            }};

            watchId = navigator.geolocation.watchPosition(
                handlePositionUpdate,
                handlePositionError,
                options
            );

            console.log('📍 Foreground GPS started (high accuracy)');
        }}

        // Start GPS in background (battery optimized)
        function startBackgroundGPS() {{
            const options = {{
                enableHighAccuracy: false,  // Save battery
                maximumAge: 30000,  // Accept older positions
                timeout: 15000
            }};

            watchId = navigator.geolocation.watchPosition(
                handlePositionUpdate,
                handlePositionError,
                options
            );

            console.log('📍 Background GPS started (battery optimized)');
        }}

        // Main GPS start function
        function startGPS() {{
            const startBtn = document.getElementById('startBtn');
            const stopBtn = document.getElementById('stopBtn');
            const status = document.getElementById('status');

            if (!navigator.geolocation) {{
                status.innerHTML = "❌ इस ब्राउज़र में GPS सपोर्ट नहीं है";
                return;
            }}

            startBtn.disabled = true;
            stopBtn.disabled = false;
            startBtn.innerHTML = '<span>⏳</span> GPS चालू हो रहा है...';
            status.innerHTML = "📡 GPS खोज रहे हैं...";

            // Start heartbeat
            startHeartbeat();

            // Start based on current state
            if (appState === 'foreground') {{
                startForegroundGPS();
            }} else {{
                startBackgroundGPS();
            }}

            // Store GPS points when in background
            setInterval(() => {{
                if (appState === 'background' && lastLocation) {{
                    backgroundPoints.push({{
                        lat: lastLocation.lat,
                        lng: lastLocation.lng,
                        speed: lastLocation.speed,
                        timestamp: new Date().toISOString()
                    }});

                    // Keep only last 50 points
                    if (backgroundPoints.length > 50) {{
                        backgroundPoints = backgroundPoints.slice(-50);
                    }}
                }}
            }}, 10000);  // Every 10 seconds in background

            // Sync background points when back to foreground
            setInterval(() => {{
                if (appState === 'foreground' && backgroundPoints.length > 0) {{
                    console.log(`🔄 Syncing ${{backgroundPoints.length}} background GPS points`);
                    socket.emit('gps_sync_background', {{
                        sid: SID,
                        points: backgroundPoints
                    }});
                    backgroundPoints = [];
                }}
            }}, 30000);  // Every 30 seconds
        }}

        function handlePositionUpdate(pos) {{
            const lat = pos.coords.latitude.toFixed(6);
            const lng = pos.coords.longitude.toFixed(6);
            const speed = pos.coords.speed || 0;
            const accuracy = pos.coords.accuracy.toFixed(1);

            lastLocation = {{ lat, lng, speed }};

            // Update UI
            document.getElementById('lat').textContent = lat;
            document.getElementById('lng').textContent = lng;
            document.getElementById('speed').textContent = (speed * 3.6).toFixed(1) + ' km/h';
            document.getElementById('accuracy').textContent = accuracy + ' m';
            document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();
            document.getElementById('appState').textContent = appState;

            document.getElementById('status').innerHTML = 
                `✅ <span style="color:#4CAF50">LIVE GPS</span> - ${{appState === 'background' ? 'Background Mode' : 'Foreground Mode'}}`;

            // Send to server
            const data = {{
                sid: SID,
                lat: parseFloat(lat),
                lng: parseFloat(lng),
                speed: (speed * 3.6).toFixed(1),
                app_state: appState,
                timestamp: new Date().toISOString()
            }};

            socket.emit("driver_gps", data);

            // Update button
            document.getElementById('startBtn').innerHTML = '<span>🚗</span> Live GPS चल रहा है';

            // Store in localStorage as backup
            localStorage.setItem(`last_gps_${{SID}}`, JSON.stringify({{
                ...data,
                stored_at: Date.now()
            }}));
        }}

        function handlePositionError(err) {{
            console.error('GPS Error:', err);
            document.getElementById('status').innerHTML = 
                `❌ GPS Error: ${{err.message}}`;
            document.getElementById('startBtn').disabled = false;
            document.getElementById('stopBtn').disabled = true;
            document.getElementById('startBtn').innerHTML = '<span>🔄</span> GPS फिर शुरू करें';
        }}

        function stopGPS() {{
            const startBtn = document.getElementById('startBtn');
            const stopBtn = document.getElementById('stopBtn');
            const status = document.getElementById('status');

            if (watchId !== null) {{
                navigator.geolocation.clearWatch(watchId);
                watchId = null;
            }}

            // Stop heartbeat
            stopHeartbeat();

            // Sync any remaining background points
            if (backgroundPoints.length > 0) {{
                socket.emit('gps_sync_background', {{
                    sid: SID,
                    points: backgroundPoints
                }});
                backgroundPoints = [];
            }}

            startBtn.disabled = false;
            stopBtn.disabled = true;
            startBtn.innerHTML = '<span>🚀</span> GPS शुरू करें';
            status.innerHTML = "🛑 GPS बंद कर दिया गया";
        }}

        function startHeartbeat() {{
            heartbeatInterval = setInterval(() => {{
                // Send heartbeat to keep connection alive
                fetch('/heartbeat');

                // Update heartbeat indicator
                const heartbeat = document.getElementById('heartbeat');
                heartbeat.textContent = 'Heartbeat: ' + (heartbeat.textContent.includes('♥') ? '♡' : '♥');

                // Check if we have location in background
                if (appState === 'background' && !lastLocation) {{
                    navigator.geolocation.getCurrentPosition(
                        (pos) => {{
                            // Just to keep GPS active
                            console.log('Background heartbeat check - GPS active');
                        }},
                        null,
                        {{ enableHighAccuracy: false }}
                    );
                }}
            }}, 10000);  // Every 10 seconds
        }}

        function stopHeartbeat() {{
            if (heartbeatInterval) {{
                clearInterval(heartbeatInterval);
                heartbeatInterval = null;
            }}
        }}

        // Initialize
        window.addEventListener('load', () => {{
            // Check for previous GPS data
            const storedGPS = localStorage.getItem(`last_gps_${{SID}}`);
            if (storedGPS) {{
                const data = JSON.parse(storedGPS);
                console.log('Loaded previous GPS data:', data);
            }}

            // Detect initial app state
            appState = document.hidden ? 'background' : 'foreground';
            document.getElementById('appState').textContent = appState;
        }});

        // Prevent sleep on mobile
        if ('wakeLock' in navigator) {{
            let wakeLock = null;
            async function requestWakeLock() {{
                try {{
                    wakeLock = await navigator.wakeLock.request('screen');
                    console.log('Wake Lock active');
                }} catch (err) {{
                    console.log('Wake Lock failed:', err);
                }}
            }}
            requestWakeLock();
        }}
    </script>
</body>
</html>
"""


# ================= SERVICE WORKER FOR BACKGROUND SYNC =================
@app.route('/sw.js')
def service_worker():
    return """
// Service Worker for Background GPS
const CACHE_NAME = 'gps-driver-v1';

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then(cache => cache.addAll([
                '/',
                '/driver/1'
            ]))
    );
});

self.addEventListener('fetch', (event) => {
    event.respondWith(
        caches.match(event.request)
            .then(response => response || fetch(event.request))
    );
});

// Background Sync
self.addEventListener('sync', (event) => {
    if (event.tag === 'gps-sync') {
        event.waitUntil(syncGPSData());
    }
});

async function syncGPSData() {
    // Get stored GPS data from IndexedDB or localStorage
    const storedData = await getStoredGPSData();

    for (const data of storedData) {
        try {
            await fetch('/api/gps-backup', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data)
            });
            // Remove after successful sync
            await removeStoredGPSData(data.id);
        } catch (err) {
            console.error('Sync failed:', err);
        }
    }
}

async function getStoredGPSData() {
    return new Promise((resolve) => {
        const request = indexedDB.open('GPS_DB', 1);
        request.onsuccess = (event) => {
            const db = event.target.result;
            const tx = db.transaction('locations', 'readonly');
            const store = tx.objectStore('locations');
            const allData = store.getAll();
            allData.onsuccess = () => resolve(allData.result || []);
        };
        request.onerror = () => resolve([]);
    });
}

async function removeStoredGPSData(id) {
    return new Promise((resolve) => {
        const request = indexedDB.open('GPS_DB', 1);
        request.onsuccess = (event) => {
            const db = event.target.result;
            const tx = db.transaction('locations', 'readwrite');
            const store = tx.objectStore('locations');
            store.delete(id);
            resolve();
        };
    });
}
""", 200, {'Content-Type': 'application/javascript'}


# ================= GPS BACKUP API =================
@app.route('/api/gps-backup', methods=['POST'])
@safe_db
def gps_backup_api():
    """API for background GPS sync"""
    try:
        data = request.get_json()

        conn, cur = get_db()
        cur.execute("""
            INSERT INTO gps_backup (bus_id, lat, lng, speed, source, app_state)
            VALUES (%s, %s, %s, %s, %s, 'background')
        """, (
            data.get('bus_id') or data.get('sid'),
            data['lat'],
            data['lng'],
            data.get('speed', 0),
            data.get('source', 'background_sync')
        ))

        # Update current location if this is the latest
        bus_id = data.get('bus_id') or data.get('sid')
        timestamp = data.get('timestamp') or data.get('stored_at')

        if timestamp:
            # Check if this is newer than current location
            cur.execute("""
                SELECT last_gps_update FROM schedules WHERE id = %s
            """, (bus_id,))
            schedule = cur.fetchone()

            if not schedule or not schedule['last_gps_update'] or \
                    (timestamp > schedule['last_gps_update'].timestamp() * 1000):
                cur.execute("""
                    UPDATE schedules 
                    SET current_lat = %s, current_lng = %s, last_gps_update = NOW()
                    WHERE id = %s
                """, (data['lat'], data['lng'], bus_id))

                # Emit to clients
                socketio.emit('bus_location', {
                    'sid': bus_id,
                    'lat': data['lat'],
                    'lng': data['lng'],
                    'speed': data.get('speed', 0),
                    'app_state': 'background',
                    'timestamp': data.get('timestamp', datetime.now().isoformat())
                })

        conn.commit()

        return jsonify({'ok': True, 'message': 'GPS backup saved'})

    except Exception as e:
        print(f"GPS backup error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


# ================= GET LAST KNOWN LOCATION =================
@app.route('/api/last-location/<int:bus_id>')
@safe_db
def get_last_location(bus_id):
    """Get last known location of a bus"""
    try:
        conn, cur = get_db()

        # First try from schedules
        cur.execute("""
            SELECT current_lat as lat, current_lng as lng, 
                   last_gps_update, bus_name
            FROM schedules 
            WHERE id = %s AND current_lat IS NOT NULL
        """, (bus_id,))
        schedule = cur.fetchone()

        if schedule:
            return jsonify({
                'ok': True,
                'lat': schedule['lat'],
                'lng': schedule['lng'],
                'last_update': schedule['last_gps_update'].isoformat() if schedule['last_gps_update'] else None,
                'bus_name': schedule['bus_name'],
                'source': 'realtime'
            })

        # Fallback to GPS backup table
        cur.execute("""
            SELECT lat, lng, created_at
            FROM gps_backup 
            WHERE bus_id = %s 
            ORDER BY created_at DESC 
            LIMIT 1
        """, (bus_id,))
        backup = cur.fetchone()

        if backup:
            return jsonify({
                'ok': True,
                'lat': backup['lat'],
                'lng': backup['lng'],
                'last_update': backup['created_at'].isoformat(),
                'source': 'backup'
            })

        # Fallback to memory cache
        key = f"bus_{bus_id}"
        if key in gps_backup_store:
            data = gps_backup_store[key]
            return jsonify({
                'ok': True,
                'lat': data['lat'],
                'lng': data['lng'],
                'last_update': datetime.fromtimestamp(data['timestamp']).isoformat(),
                'source': 'memory_cache'
            })

        return jsonify({'ok': False, 'message': 'No location data available'})

    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ================= HEARTBEAT ENDPOINT =================
@app.route('/heartbeat')
def heartbeat():
    """Keep connection alive"""
    return jsonify({'status': 'alive', 'timestamp': datetime.now().isoformat()})


# ================= WEB APP MANIFEST =================
@app.route('/manifest.json')
def manifest():
    return jsonify({
        "name": "Bus Driver GPS",
        "short_name": "DriverGPS",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#1a1a2e",
        "theme_color": "#4cc9f0",
        "icons": [
            {
                "src": "/static/icon-192.png",
                "sizes": "192x192",
                "type": "image/png"
            },
            {
                "src": "/static/icon-512.png",
                "sizes": "512x512",
                "type": "image/png"
            }
        ]
    })


# ================= REST OF YOUR ROUTES =================
# (Keep all your existing routes like /, /dashboard, /login, /counter, /seats, etc.)
# They remain the same as in your original code

# ... [YOUR EXISTING ROUTES HERE - KEEP THEM AS IS] ...

if __name__ == "__main__":
    print("🚀 Bus Booking App Starting... (Enhanced GPS Support)")
    print("📍 Background GPS is now enabled")
    print("📱 Mobile/Windows GPS will continue in background")
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=True)