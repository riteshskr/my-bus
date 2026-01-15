import os, random
from datetime import date
from flask import Flask, request, render_template_string
from flask_socketio import SocketIO, emit
import psycopg2
from psycopg2 import pool

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-key')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Database config
db_config = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "database": os.environ.get("DB_NAME", "busdb1"),
    "user": os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "port": os.environ.get("DB_PORT", "5432")
}
db_pool = None


def get_db_connection():
    global db_pool
    if db_pool is None:
        db_pool = psycopg2.pool.ThreadedConnectionPool(1, 20, **db_config)
    return db_pool.getconn()


def close_db(conn):
    if db_pool:
        db_pool.putconn(conn)


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Tables
        cur.execute("""
        CREATE TABLE IF NOT EXISTS routes (
            id SERIAL PRIMARY KEY, name VARCHAR(100), distance_km INTEGER
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS route_stations (
            id SERIAL PRIMARY KEY, route_id INTEGER, station_name VARCHAR(100), 
            station_order INTEGER, lat DECIMAL, lng DECIMAL
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id SERIAL PRIMARY KEY, route_id INTEGER, bus_name VARCHAR(100), 
            departure_time TIME, total_seats INTEGER DEFAULT 40, available_seats INTEGER DEFAULT 40
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS seat_bookings (
            id SERIAL PRIMARY KEY, schedule_id INTEGER, seat_number INTEGER, 
            passenger_name VARCHAR(100), phone VARCHAR(15), booking_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        # Sample Data
        cur.execute("INSERT INTO routes VALUES (1, 'बीकानेर → जयपुर', 350) ON CONFLICT DO NOTHING")
        cur.execute("""
        INSERT INTO route_stations VALUES 
        (1, 1, 'बीकानेर', 1, 28.0194, 77.0290),
        (2, 1, 'सिकार', 2, 27.1751, 74.8551),
        (3, 1, 'जयपुर', 3, 26.9124, 75.7873) ON CONFLICT DO NOTHING
        """)
        cur.execute("INSERT INTO schedules VALUES (1, 1, 'RSRTC Volvo Bus 1', '08:00', 40, 40) ON CONFLICT DO NOTHING")
        conn.commit()
        print("✅ Database ready!")
    except Exception as e:
        print(f"❌ DB Error: {e}")
    finally:
        cur.close()
        close_db(conn)


@app.route("/")
def index():
    html = """
<!DOCTYPE html>
<html>
<head>
    <title>🚌 Bus Booking + Live Tracking</title>
    <meta name="viewport" content="width=device-width">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); font-family: 'Segoe UI', sans-serif; }
        .hero { background: rgba(255,255,255,0.95); backdrop-filter: blur(20px); border-radius: 25px; box-shadow: 0 25px 50px rgba(0,0,0,0.2); }
        .btn-live { background: linear-gradient(45deg, #4CAF50, #45a049); border: none; color: white; border-radius: 50px; padding: 15px 30px; font-weight: bold; }
    </style>
</head>
<body class="min-vh-100 d-flex align-items-center p-4">
    <div class="container">
        <div class="hero p-5 text-center mb-5">
            <h1 class="display-3 fw-bold mb-4">🚌 Bus Booking System</h1>
            <p class="lead mb-4">Live GPS Tracking + Seat Booking</p>
            <a href="/live-tracking" class="btn btn-live btn-lg mb-4">🚀 सभी बसों की Live Location</a>
        </div>
        <div class="row g-4">
            <div class="col-md-6">
                <div class="card h-100 shadow-lg">
                    <div class="card-body p-4">
                        <h4 class="card-title text-primary">बीकानेर → जयपुर</h4>
                        <a href="/buses/1" class="btn btn-warning w-100 fw-bold mt-3">View Buses →</a>
                    </div>
                </div>
            </div>
        </div>
    </div>
</body>
</html>
    """
    return render_template_string(html)


@app.route("/live-tracking")
def live_tracking():
    html_template = """
<!DOCTYPE html>
<html>
<head>
    <title>🚌 Multiple Bus Live Tracking</title>
    <meta name="viewport" content="width=device-width">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        body{{background:linear-gradient(135deg,#1e3c72 0%,#2a5298 100%);font-family:'Segoe UI',sans-serif;}}
        #map{{height:75vh;width:100%;border-radius:20px;box-shadow:0 20px 40px rgba(0,0,0,0.3);}}
        .live-dot{{width:16px;height:16px;background:#4CAF50;border-radius:50%;box-shadow:0 0 20px #4CAF50;
        animation:pulse 1.5s infinite;margin-right:10px;}}
        @keyframes pulse{{0%,100%{{transform:scale(1);opacity:1;}}50%{{transform:scale(1.3);opacity:0.7;}}}}
        .bus-card{{background:rgba(255,255,255,0.95);backdrop-filter:blur(15px);border-radius:15px;padding:20px;}}
    </style>
</head>
<body class="p-4">
    <div class="container-fluid">
        <div class="text-center mb-5">
            <h1 class="text-white display-4 fw-bold">🚌 Live Bus Tracking</h1>
            <p class="text-white-50 lead">Real-time Location Tracking (Free!)</p>
        </div>
        <div class="row g-4">
            <div class="col-lg-8 col-md-12">
                <div id="map"></div>
            </div>
            <div class="col-lg-4 col-md-12">
                <div class="bus-card sticky-top" style="top:20px;">
                    <h4>Active Buses <span class="badge bg-success">5</span></h4>
                    <div id="bus-list"></div>
                    <div id="live-stats" class="mt-4 p-3 bg-light rounded"></div>
                </div>
            </div>
        </div>
    </div>
    <script>
        const socket=io();let map,busMarkers={};
        map=L.map('map').setView([27.0,75.0],7);
        L.tileLayer('https://{{a-c}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{
            attribution:'© OpenStreetMap'
        }).addTo(map);

        // Load Bus 1 route
        fetch('/route_coords/1').then(r=>r.json()).then(data=>{
            if(data.coords.length>1){
                L.polyline(data.coords,{{color:'#FF6B35',weight:8,opacity:0.8}}).addTo(map);
            }
        });

        socket.on('bus_location',data=>{
            if(busMarkers[data.sid]){
                busMarkers[data.sid].setLatLng([data.lat,data.lng]);
            }else{
                busMarkers[data.sid]=L.marker([data.lat,data.lng],{
                    icon:L.divIcon({
                        html:'<div class="live-dot" style="background:#4CAF50;box-shadow:0 0 20px #4CAF50"></div>',
                        iconSize:[24,24],className:'custom-div-icon'
                    })
                }).addTo(map).bindPopup(`Bus ${{data.sid}}<br>Speed: ${{data.speed||0}} km/h`);
            }
            map.panTo([data.lat,data.lng]);
            document.getElementById('live-stats').innerHTML=`
                <div class="fw-bold text-success mb-2">📍 Live Update</div>
                <div>🚌 Bus ${{data.sid}}</div>
                <div>📍 ${{data.lat.toFixed(5)}}, ${{data.lng.toFixed(5)}}</div>
                <div>🚀 ${{data.speed||0}} km/h</div>
            `;
        });

        document.getElementById('bus-list').innerHTML=`
            <div class="d-flex align-items-center p-3 mb-2 bg-white rounded shadow-sm">
                <div class="live-dot"></div>
                <div><div class="fw-bold">RSRTC Volvo Bus 1</div><small>ID: 1</small></div>
            </div>
        `;
    </script>
</body>
</html>
    """
    return render_template_string(html_template)


@app.route("/route_coords/<int:sid>")
def route_coords(sid):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT lat, lng FROM route_stations WHERE route_id=1 ORDER BY station_order")
    coords = [[float(r[0]), float(r[1])] for r in cur.fetchall()]
    cur.close();
    close_db(conn)
    return {'coords': coords}


@app.route("/driver/<int:sid>")
def driver(sid):
    return f'''
<!DOCTYPE html>
<html>
<head>
    <title>Driver GPS - Bus {sid}</title>
    <meta name="viewport" content="width=device-width">
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <style>
        body{{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);min-height:100vh;display:flex;align-items:center;justify-content:center;font-family:'Segoe UI',sans-serif;}}
        .driver-box{{background:rgba(255,255,255,0.95);backdrop-filter:blur(20px);border-radius:25px;padding:40px;text-align:center;box-shadow:0 25px 50px rgba(0,0,0,0.2);max-width:450px;width:90%;}}
        .gps-btn{{background:linear-gradient(45deg,#4CAF50,#45a049);border:none;border-radius:50px;color:white;padding:20px 50px;font-size:1.3em;font-weight:bold;cursor:pointer;transition:all 0.3s;}}
        .gps-btn:hover:not(:disabled){{transform:scale(1.05);}}
        #status{{font-size:1.5em;font-weight:bold;margin:25px 0;min-height:50px;}}
    </style>
</head>
<body>
    <div class="driver-box">
        <h1>🚗 Bus <span style="color:#FF6B35;">{sid}</span></h1>
        <h3>Live GPS Tracking</h3>
        <button id="startBtn" class="gps-btn">🚀 GPS शुरू करें</button>
        <div id="status">GPS बंद है</div>
        <div id="coords" style="font-size:1.1em;margin-top:10px;"></div>
    </div>
    <script>
        const socket = io();
        let watchId = null;
        const btn = document.getElementById('startBtn');
        const status = document.getElementById('status');
        const coords = document.getElementById('coords');

        btn.addEventListener('click', function() {{
            if (watchId) {{
                navigator.geolocation.clearWatch(watchId);
                watchId = null;
                btn.innerHTML = '🚀 GPS फिर शुरू करें';
                status.innerHTML = 'GPS बंद';
                return;
            }}

            watchId = navigator.geolocation.watchPosition(
                function(pos) {{
                    const lat = pos.coords.latitude;
                    const lng = pos.coords.longitude;
                    const speed = pos.coords.speed ? (pos.coords.speed * 3.6).toFixed(1) : 0;

                    socket.emit('driver_gps', {{
                        sid: {sid},
                        lat: lat,
                        lng: lng,
                        speed: speed
                    }});

                    status.innerHTML = `📍 ${{lat.toFixed(6)}}, ${{lng.toFixed(6)}}`;
                    coords.innerHTML = `🚀 Speed: ${{speed}} km/h | 📏 ${{pos.coords.accuracy.toFixed(0)}}m`;
                    btn.innerHTML = '⏹️ GPS बंद करें';
                    btn.style.transform = 'scale(1.05)';
                }},
                function(err) {{
                    status.innerHTML = '❌ GPS Error: ' + err.message;
                }},
                {{
                    enableHighAccuracy: true,
                    timeout: 10000,
                    maximumAge: 30000
                }}
            );
        }});
    </script>
</body>
</html>
    '''


@socketio.on('driver_gps')
def handle_gps(data):
    print(f"📍 Bus {{data['sid']}}: {{data['lat']:.6f}}, {{data['lng']:.6f}} Speed: {{data.get('speed',0)}} km/h")
    emit('bus_location', data, broadcast=True)

if __name__ == "__main__":
     init_db()
     port = int(os.environ.get("PORT", 5000))
     socketio.run(app, host="0.0.0.0", port=port, debug=True)
