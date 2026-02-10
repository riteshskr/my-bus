from dotenv import load_dotenv
import json

load_dotenv()
import setuptools
import os, random
from datetime import date, datetime
from functools import wraps
from flask import Flask, request, jsonify, render_template_string, redirect, g, session
from flask_socketio import SocketIO, emit
from flask_compress import Compress
from supabase import create_client, Client
import atexit
import razorpay

# ===== SUPABASE CONFIG =====
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise Exception("SUPABASE_URL और SUPABASE_KEY environment variables ज़रूरी हैं!")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
print("✅ Supabase क्लाइंट तैयार")

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
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key-12345")
Compress(app)

# ✅ SocketIO Configuration
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                    logger=True, engineio_logger=True, ping_timeout=60)

# ================= DB HELPER FUNCTIONS =================
def supabase_query(table, operation="select", data=None, filters=None):
    """Supabase queries के लिए helper function"""
    try:
        if operation == "select":
            query = supabase.table(table).select("*")
            if filters:
                for key, value in filters.items():
                    if isinstance(value, list):
                        query = query.in_(key, value)
                    else:
                        query = query.eq(key, value)
            response = query.execute()
            return response.data
        
        elif operation == "insert":
            response = supabase.table(table).insert(data).execute()
            return response.data
        
        elif operation == "update":
            query = supabase.table(table).update(data)
            if filters:
                for key, value in filters.items():
                    query = query.eq(key, value)
            response = query.execute()
            return response.data
        
        elif operation == "delete":
            query = supabase.table(table).delete()
            if filters:
                for key, value in filters.items():
                    query = query.eq(key, value)
            response = query.execute()
            return response.data
        
    except Exception as e:
        print(f"Supabase query error: {e}")
        return None

# ================= DECORATORS =================
def admin_required(f):
    @wraps(f)
    def wrap(*a, **k):
        if not session.get("user_logged_in"):
            return redirect("/login")
        if session.get("role") != "admin":
            return "Access Denied", 403
        return f(*a, **k)
    return wrap

def counter_required(f):
    @wraps(f)
    def wrap(*a, **k):
        if not session.get("user_logged_in"):
            return redirect("/counter")
        if session.get("role") not in ["admin", "counter"]:
            return "Access Denied", 403
        return f(*a, **k)
    return wrap

# ================= DB INIT =================
def init_db():
    """Supabase में टेबल्स और डिफ़ॉल्ट डेटा चेक करें"""
    try:
        # डिफ़ॉल्ट एडमिन चेक करें
        admin_check = supabase_query("admins", filters={"username": "admin"})
        
        if not admin_check or len(admin_check) == 0:
            # डिफ़ॉल्ट एडमिन बनाएँ
            supabase_query("admins", "insert", {
                "username": "admin",
                "password": "admin123",
                "role": "admin",
                "counter_no": 1
            })
            print("✅ डिफ़ॉल्ट एडमिन बनाया गया")
        
        # डिफ़ॉल्ट रूट्स चेक करें
        routes_check = supabase_query("routes")
        
        if not routes_check or len(routes_check) == 0:
            # डिफ़ॉल्ट डेटा इन्सर्ट करें
            default_routes = [
                {"id": 1, "route_name": "बीकानेर → जयपुर", "distance_km": 336},
                {"id": 2, "route_name": "बीकानेर → जोधपुर", "distance_km": 252},
                {"id": 3, "route_name": "जयपुर → जोधपुर", "distance_km": 330}
            ]
            
            for route in default_routes:
                supabase_query("routes", "insert", route)
            
            # डिफ़ॉल्ट schedules
            default_schedules = [
                {"id": 1, "route_id": 1, "bus_name": "Volvo AC Sleeper", "departure_time": "08:00:00"},
                {"id": 2, "route_id": 1, "bus_name": "Semi Sleeper AC", "departure_time": "10:30:00"},
                {"id": 3, "route_id": 2, "bus_name": "Volvo AC Seater", "departure_time": "09:00:00"},
                {"id": 4, "route_id": 3, "bus_name": "Deluxe AC", "departure_time": "07:30:00"}
            ]
            
            for schedule in default_schedules:
                supabase_query("schedules", "insert", schedule)
            
            # डिफ़ॉल्ट stations
            default_stations = [
                {"route_id": 1, "station_name": "बीकानेर", "station_order": 1},
                {"route_id": 1, "station_name": "जयपुर", "station_order": 2},
                {"route_id": 2, "station_name": "बीकानेर", "station_order": 1},
                {"route_id": 2, "station_name": "जोधपुर", "station_order": 2},
                {"route_id": 3, "station_name": "जयपुर", "station_order": 1},
                {"route_id": 3, "station_name": "जोधपुर", "station_order": 2}
            ]
            
            for station in default_stations:
                supabase_query("route_stations", "insert", station)
            
            print("✅ डिफ़ॉल्ट डेटा इन्सर्ट किया गया")
        
        print("✅ DB Init Complete!")
        
    except Exception as e:
        print(f"❌ DB INIT ERROR: {e}")
        import traceback
        traceback.print_exc()

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

    try:
        # Supabase में update करें
        supabase_query("schedules", "update", 
                      {"current_lat": lat, "current_lng": lng}, 
                      {"id": sid})
    except Exception as e:
        print(f"GPS update error: {e}")

    # Real-time emit
    socketio.emit("bus_location", {
        "sid": sid,
        "lat": lat,
        "lng": lng,
        "speed": speed,
        "timestamp": datetime.now().isoformat()
    })

# ================= HTML TEMPLATES =================
BASE_HTML = """
<!DOCTYPE html>
<html lang="hi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>My Bus AI - Smart Bus Booking</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">

<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:'Poppins',sans-serif;}
body{background:#f5f7fb;color:#222;}

/* Navbar */
.navbar{
  position:fixed;
  top:0;left:0;width:100%;
  background:white;
  display:flex;
  justify-content:space-between;
  align-items:center;
  padding:15px 8%;
  box-shadow:0 5px 20px rgba(0,0,0,.1);
  z-index:1000;
}
.logo{font-size:1.5rem;font-weight:700;color:#ff512f;}
.navbar a{margin-left:20px;text-decoration:none;color:#333;font-weight:500;}

/* Hero */
.hero{
  height:100vh;
  background:
    linear-gradient(rgba(0,0,0,.6),rgba(0,0,0,.8)),
    url("https://images.unsplash.com/photo-1544620347-c4fd4a3d5957");
  background-size:cover;
  background-position:center;
  display:flex;
  align-items:center;
  justify-content:center;
  text-align:center;
  color:white;
  padding-top:70px;
}

/* Search Box */
.search-box{
  background:white;
  padding:30px;
  border-radius:20px;
  display:flex;
  gap:15px;
  box-shadow:0 20px 40px rgba(0,0,0,.3);
  max-width:800px;
  margin:0 auto;
}
.search-box input, .search-box select{
  padding:15px;
  border:2px solid #ddd;
  border-radius:10px;
  outline:none;
  flex:1;
  min-width:180px;
}
.search-box button{
  padding:15px 40px;
  border:none;
  border-radius:10px;
  background:linear-gradient(45deg,#ff512f,#dd2476);
  color:white;
  font-weight:600;
  cursor:pointer;
  transition:transform 0.3s;
}
.search-box button:hover{transform:scale(1.05);}

/* Cards */
.card{
  background:white;
  border-radius:15px;
  box-shadow:0 10px 25px rgba(0,0,0,.1);
  padding:20px;
  margin-bottom:20px;
  transition:transform 0.3s;
}
.card:hover{transform:translateY(-10px);}

/* Mobile Fixes */
@media(max-width:768px){
  .navbar{flex-direction:column;gap:10px;padding:10px 20px;}
  .search-box{flex-direction:column;width:90%;padding:20px;}
  .hero h1{font-size:1.6rem;}
}

/* Status Colors */
.status-live{color:green;}
.status-offline{color:gray;}
.status-booked{color:red;}
.status-available{color:green;}

/* Seat Grid */
.seat-grid{
  display:grid;
  grid-template-columns:repeat(10,1fr);
  gap:10px;
  margin:20px 0;
}
.seat{
  padding:12px;
  border:none;
  border-radius:8px;
  text-align:center;
  cursor:pointer;
  font-weight:bold;
  transition:all 0.3s;
}
.seat-available{background:#28a745;color:white;}
.seat-booked{background:#dc3545;color:white;cursor:not-allowed;}
.seat-selected{background:#007bff;color:white;}
</style>
</head>
<body>

<div class="navbar">
  <div class="logo">🚌 My Bus AI</div>
  <div>
    {% if session.get('user_logged_in') %}
      <a href="/dashboard">Dashboard</a>
      <a href="/logout">Logout ({{ session.get('role', 'user') }})</a>
    {% else %}
      <a href="/login">Admin Login</a>
      <a href="/counter">Counter Login</a>
    {% endif %}
    <a href="/">Home</a>
  </div>
</div>

{% if not content %}
<section class="hero">
  <div style="width:100%;padding:20px;">
    <h1 style="font-size:3rem;margin-bottom:20px;">भारत का स्मार्ट बस प्लेटफॉर्म</h1>
    <p style="font-size:1.2rem;margin-bottom:30px;">बुक करें | ट्रैक करें | फेस बोर्डिंग | लाइव सीट्स</p>

    <form class="search-box" action="/search" method="POST">
      <select name="from" class="form-select" required>
        <option value="" selected disabled>From (स्टेशन)</option>
        {% for station in stations %}
        <option value="{{ station }}">{{ station }}</option>
        {% endfor %}
      </select>
      
      <select name="to" class="form-select" required>
        <option value="" selected disabled>To (स्टेशन)</option>
        {% for station in stations %}
        <option value="{{ station }}">{{ station }}</option>
        {% endfor %}
      </select>
      
      <input type="date" name="date" class="form-control" value="{{ today }}" required>
      <button type="submit">Search Buses 🔍</button>
    </form>
  </div>
</section>

<div class="container mt-5">
  <h2 class="text-center mb-4">Popular Routes</h2>
  <div class="row">
    {% for route in routes %}
    <div class="col-md-4 mb-3">
      <div class="card">
        <div class="card-body">
          <h5 class="card-title">{{ route.route_name }}</h5>
          <p class="card-text">{{ route.distance_km }} km</p>
          <a href="/buses/{{ route.id }}" class="btn btn-primary">View Buses</a>
        </div>
      </div>
    </div>
    {% endfor %}
  </div>
</div>
{% endif %}

{% if content %}
<div style="padding:100px 10%;">
    {{ content|safe }}
</div>
{% endif %}

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

LOGIN_HTML = """
<div class="row justify-content-center mt-5">
  <div class="col-md-4">
    <div class="card shadow-lg border-0 rounded-4">
      <div class="card-body p-4">
        <h3 class="text-center mb-4">
          {% if is_counter %}Counter Login{% else %}Admin Login{% endif %}
        </h3>

        <form method="POST" autocomplete="on">
          <input type="text" style="display:none">
          <input type="password" style="display:none">

          <div class="mb-3">
            <label class="form-label">Username</label>
            <input type="text" name="username" class="form-control" placeholder="Enter username" required>
          </div>

          <div class="mb-3">
            <label class="form-label">Password</label>
            <input type="password" name="password" class="form-control" placeholder="Enter password" required>
          </div>

          {% if is_counter %}
          <input type="hidden" name="counter_login" value="true">
          {% endif %}

          <button class="btn btn-success w-100">Login</button>
        </form>

        {% if error %}
          <div class="alert alert-danger mt-3 text-center">{{ error }}</div>
        {% endif %}

        <div class="text-center mt-3">
          {% if is_counter %}
            <a href="/login">Admin Login</a>
          {% else %}
            <a href="/counter">Counter Login</a>
          {% endif %}
        </div>
      </div>
    </div>
  </div>
</div>
"""

# ================= ROUTES =================
@app.route("/")
def home():
    if "role" not in session:
        session.clear()
        session["role"] = "guest"

    # सभी stations fetch करें
    stations_data = supabase_query("route_stations")
    stations = list(set([s["station_name"] for s in stations_data])) if stations_data else []

    # सभी routes fetch करें
    routes = supabase_query("routes") or []

    today = date.today().isoformat()

    return render_template_string(
        BASE_HTML, 
        stations=stations, 
        routes=routes,
        today=today,
        content=None
    )

@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        # Supabase से user fetch करें
        users = supabase_query("admins", filters={
            "username": username,
            "password": password
        })

        if users and len(users) > 0:
            user = users[0]
            session.clear()
            session["user_logged_in"] = True
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            session["counter_no"] = user.get("counter_no", 0)
            
            return redirect("/dashboard")
        else:
            error = "Invalid username or password"

    return render_template_string(
        BASE_HTML,
        content=render_template_string(LOGIN_HTML, error=error, is_counter=False)
    )

@app.route("/counter", methods=["GET", "POST"])
def counter_login():
    error = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        users = supabase_query("admins", filters={
            "username": username,
            "password": password,
            "role": "counter"
        })

        if users and len(users) > 0:
            user = users[0]
            session.clear()
            session["user_logged_in"] = True
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            session["counter_no"] = user.get("counter_no", 0)
            
            return redirect("/dashboard")
        else:
            error = "Invalid counter credentials"

    return render_template_string(
        BASE_HTML,
        content=render_template_string(LOGIN_HTML, error=error, is_counter=True)
    )

@app.route("/dashboard")
def dashboard():
    if not session.get("user_logged_in"):
        return redirect("/login")

    role = session.get("role", "user")
    username = session.get("username", "User")

    # Statistics fetch करें
    total_bookings = len(supabase_query("seat_bookings") or [])
    total_buses = len(supabase_query("schedules") or [])
    total_routes = len(supabase_query("routes") or [])

    # Recent bookings
    recent_bookings = supabase_query("seat_bookings", 
                                   filters={"status": "confirmed"})
    if recent_bookings:
        recent_bookings = recent_bookings[:5]

    admin_links = ""
    if role == "admin":
        admin_links = """
        <div class="row mt-4">
            <div class="col-md-3 mb-2">
                <a href="/routes" class="btn btn-info w-100">🛣️ Manage Routes</a>
            </div>
            <div class="col-md-3 mb-2">
                <a href="/schedules" class="btn btn-warning w-100">🚌 Manage Schedules</a>
            </div>
            <div class="col-md-3 mb-2">
                <a href="/bookings" class="btn btn-success w-100">🎫 View Bookings</a>
            </div>
            <div class="col-md-3 mb-2">
                <a href="/create-counter" class="btn btn-dark w-100">➕ Create Counter</a>
            </div>
        </div>
        """

    counter_links = ""
    if role == "counter":
        counter_links = """
        <div class="row mt-4">
            <div class="col-md-6 mb-2">
                <a href="/counter-bookings" class="btn btn-primary w-100">📋 My Bookings</a>
            </div>
            <div class="col-md-6 mb-2">
                <a href="/counter-buses" class="btn btn-info w-100">🚍 Available Buses</a>
            </div>
        </div>
        """

    content = f"""
    <div class="container">
        <div class="text-center mb-5">
            <h2>Welcome, {username}! 🎉</h2>
            <h4>Role: <span class="badge bg-primary">{role.upper()}</span></h4>
            {% if session.get('counter_no') %}
            <h5>Counter Number: <span class="badge bg-secondary">#{session.get('counter_no')}</span></h5>
            {% endif %}
        </div>

        <!-- Statistics Cards -->
        <div class="row mb-5">
            <div class="col-md-4">
                <div class="card text-center">
                    <div class="card-body">
                        <h1>{total_bookings}</h1>
                        <p>Total Bookings</p>
                    </div>
                </div>
            </div>
            <div class="col-md-4">
                <div class="card text-center">
                    <div class="card-body">
                        <h1>{total_buses}</h1>
                        <p>Active Buses</p>
                    </div>
                </div>
            </div>
            <div class="col-md-4">
                <div class="card text-center">
                    <div class="card-body">
                        <h1>{total_routes}</h1>
                        <p>Routes</p>
                    </div>
                </div>
            </div>
        </div>

        <!-- Action Links -->
        {admin_links}
        {counter_links}

        <!-- Recent Bookings -->
        <div class="card mt-4">
            <div class="card-header">
                <h5>Recent Bookings</h5>
            </div>
            <div class="card-body">
                {render_recent_bookings(recent_bookings)}
            </div>
        </div>

        <!-- Navigation -->
        <div class="text-center mt-4">
            <a href="/" class="btn btn-outline-primary">🏠 Home</a>
            <a href="/logout" class="btn btn-outline-danger ms-2">🚪 Logout</a>
        </div>
    </div>
    """

    return render_template_string(BASE_HTML, content=content)

def render_recent_bookings(bookings):
    if not bookings:
        return "<p>No recent bookings</p>"
    
    html = """
    <div class="table-responsive">
        <table class="table table-striped">
            <thead>
                <tr>
                    <th>Passenger</th>
                    <th>Bus</th>
                    <th>Seat</th>
                    <th>Date</th>
                    <th>Fare</th>
                </tr>
            </thead>
            <tbody>
    """
    
    for booking in bookings:
        # Bus details fetch करें
        bus_data = supabase_query("schedules", filters={"id": booking["schedule_id"]})
        bus_name = bus_data[0]["bus_name"] if bus_data else "N/A"
        
        html += f"""
                <tr>
                    <td>{booking.get('passenger_name', 'N/A')}</td>
                    <td>{bus_name}</td>
                    <td>{booking.get('seat_number', 'N/A')}</td>
                    <td>{booking.get('travel_date', 'N/A')}</td>
                    <td>₹{booking.get('fare', '0')}</td>
                </tr>
        """
    
    html += """
            </tbody>
        </table>
    </div>
    """
    return html

@app.route("/buses/<int:rid>")
def buses(rid):
    # Route details
    route_data = supabase_query("routes", filters={"id": rid})
    if not route_data:
        return "Route not found", 404
    route = route_data[0]

    # Route stations
    stations_data = supabase_query("route_stations", filters={"route_id": rid})
    stations = " → ".join([s["station_name"] for s in sorted(stations_data, key=lambda x: x["station_order"])]) if stations_data else ""

    # Buses for this route
    buses_data = supabase_query("schedules", filters={"route_id": rid})
    
    if not buses_data:
        return render_template_string(
            BASE_HTML,
            content=f"""
            <div class="alert alert-warning text-center">
                <h3>No buses available for {route['route_name']}</h3>
                <a href="/" class="btn btn-primary mt-3">Back to Home</a>
            </div>
            """
        )

    # Booked seats count for each bus
    for bus in buses_data:
        bookings = supabase_query("seat_bookings", filters={
            "schedule_id": bus["id"],
            "travel_date": date.today().isoformat(),
            "status": "confirmed"
        })
        bus["booked_count"] = len(bookings) if bookings else 0
        bus["available_seats"] = bus.get("total_seats", 40) - bus["booked_count"]

    content = f"""
    <div class="container">
        <div class="text-center mb-5">
            <h1>🚌 {route['route_name']}</h1>
            <h5>📍 {stations} | 🛣️ {route['distance_km']} km</h5>
        </div>

        <div class="row">
            {% for bus in buses %}
            <div class="col-md-6 mb-4">
                <div class="card h-100">
                    <div class="card-body">
                        <div class="d-flex justify-content-between align-items-start">
                            <h5 class="card-title">{bus['bus_name']}</h5>
                            <span class="badge {% if bus['current_lat'] %}bg-success{% else %}bg-secondary{% endif %}">
                                {% if bus['current_lat'] %}🟢 LIVE{% else %}⚪ OFFLINE{% endif %}
                            </span>
                        </div>
                        
                        <div class="bus-info mt-3">
                            <p><i class="fas fa-clock"></i> <strong>Departure:</strong> {bus['departure_time']}</p>
                            <p><i class="fas fa-chair"></i> <strong>Available Seats:</strong> {bus['available_seats']} / {bus.get('total_seats', 40)}</p>
                            {% if bus['current_lat'] %}
                            <p><i class="fas fa-map-marker-alt"></i> <strong>Live Location:</strong> Active</p>
                            {% endif %}
                        </div>

                        <div class="d-grid gap-2 d-md-flex justify-content-md-start mt-4">
                            {% if bus['current_lat'] %}
                            <a href="/live-bus/{bus['id']}" class="btn btn-primary me-md-2">
                                <i class="fas fa-map"></i> Live GPS
                            </a>
                            {% endif %}
                            <a href="/seats/{bus['id']}" class="btn btn-success">
                                <i class="fas fa-ticket-alt"></i> Book Seat
                            </a>
                        </div>
                    </div>
                </div>
            </div>
            {% endfor %}
        </div>

        <div class="text-center mt-4">
            <a href="/" class="btn btn-outline-secondary">Back to Home</a>
        </div>
    </div>
    """

    return render_template_string(
        BASE_HTML,
        content=render_template_string(content, buses=buses_data, route=route)
    )

@app.route("/seats/<int:sid>")
def seat_page(sid):
    if not session.get("from") or not session.get("to") or not session.get("date"):
        return redirect("/")

    # Bus details
    bus_data = supabase_query("schedules", filters={"id": sid})
    if not bus_data:
        return "Bus not found", 404
    bus = bus_data[0]

    # Route details
    route_data = supabase_query("routes", filters={"id": bus["route_id"]})
    route = route_data[0] if route_data else {"route_name": "Unknown Route"}

    # Booked seats for today
    bookings = supabase_query("seat_bookings", filters={
        "schedule_id": sid,
        "travel_date": session.get("date"),
        "status": "confirmed"
    })
    booked_seats = set([b["seat_number"] for b in bookings]) if bookings else set()

    # Seat grid बनाएँ
    seat_html = ""
    for seat in range(1, 41):  # 40 seats
        if seat in booked_seats:
            seat_html += f'<button class="seat seat-booked" disabled>S{seat}</button>'
        else:
            seat_html += f'<button class="seat seat-available" onclick="selectSeat({seat})">S{seat}</button>'

    user_role = session.get("role", "user")
    counter_id = session.get("user_id") if user_role == "counter" else None

    content = f"""
    <div class="container">
        <div class="card">
            <div class="card-header bg-primary text-white">
                <h4 class="mb-0">{bus['bus_name']} - {route['route_name']}</h4>
                <p class="mb-0">Departure: {bus['departure_time']} | Date: {session.get('date')}</p>
                <p class="mb-0">From: {session.get('from')} → To: {session.get('to')}</p>
                <p class="mb-0">Role: <span class="badge bg-info">{user_role.upper()}</span></p>
            </div>
            
            <div class="card-body">
                <h5>Select Your Seat:</h5>
                <div class="seat-grid">
                    {seat_html}
                </div>
                
                <div class="mt-4">
                    <div class="d-flex gap-2 mb-2">
                        <div class="seat seat-available" style="width:30px;"></div>
                        <span>Available</span>
                    </div>
                    <div class="d-flex gap-2">
                        <div class="seat seat-booked" style="width:30px;"></div>
                        <span>Booked</span>
                    </div>
                </div>
                
                <div class="mt-4" id="bookingForm" style="display:none;">
                    <h5>Passenger Details</h5>
                    <form id="passengerForm">
                        <div class="row">
                            <div class="col-md-6 mb-3">
                                <label>Passenger Name</label>
                                <input type="text" id="passengerName" class="form-control" required>
                            </div>
                            <div class="col-md-6 mb-3">
                                <label>Mobile Number</label>
                                <input type="tel" id="mobileNumber" class="form-control" required>
                            </div>
                        </div>
                        {% if user_role == 'counter' %}
                        <div class="row">
                            <div class="col-md-6 mb-3">
                                <label>Fare (₹)</label>
                                <input type="number" id="fareAmount" class="form-control" required>
                            </div>
                            <div class="col-md-6 mb-3">
                                <label>Payment Mode</label>
                                <select id="paymentMode" class="form-control">
                                    <option value="cash">Cash</option>
                                    <option value="online">Online</option>
                                </select>
                            </div>
                        </div>
                        {% endif %}
                        <input type="hidden" id="selectedSeat">
                        <button type="button" class="btn btn-success" onclick="confirmBooking()">Confirm Booking</button>
                    </form>
                </div>
            </div>
        </div>
    </div>

    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <script>
    const socket = io(window.location.origin);
    let selectedSeat = null;
    const counterId = {counter_id if counter_id else 'null'};
    const userRole = "{user_role}";
    
    function selectSeat(seat) {{
        if(selectedSeat) {{
            document.getElementById('seat-' + selectedSeat)?.classList.remove('seat-selected');
            document.getElementById('seat-' + selectedSeat)?.classList.add('seat-available');
        }}
        
        selectedSeat = seat;
        document.getElementById('seat-' + seat)?.classList.remove('seat-available');
        document.getElementById('seat-' + seat)?.classList.add('seat-selected');
        
        document.getElementById('selectedSeat').value = seat;
        document.getElementById('bookingForm').style.display = 'block';
    }}
    
    // Dynamic seat IDs create करें
    document.addEventListener('DOMContentLoaded', function() {{
        const seats = document.querySelectorAll('.seat');
        seats.forEach((seat, index) => {{
            if(seat.classList.contains('seat-available')) {{
                const seatNum = index + 1;
                seat.id = 'seat-' + seatNum;
            }}
        }});
    }});
    
    function confirmBooking() {{
        const name = document.getElementById('passengerName').value;
        const mobile = document.getElementById('mobileNumber').value;
        const seat = selectedSeat;
        
        if(!name || !mobile || !seat) {{
            alert('Please fill all details');
            return;
        }}
        
        let fare = 350;
        let paymentMode = 'cash';
        
        if(userRole === 'counter') {{
            fare = document.getElementById('fareAmount').value;
            paymentMode = document.getElementById('paymentMode').value;
            
            if(!fare || fare < 1) {{
                alert('Please enter valid fare');
                return;
            }}
        }}
        
        const bookingData = {{
            schedule_id: {sid},
            seat_number: seat,
            passenger_name: name,
            mobile: mobile,
            date: "{session.get('date')}",
            from: "{session.get('from')}",
            to: "{session.get('to')}",
            fare: parseInt(fare),
            payment_mode: paymentMode,
            counter_id: counterId === 'null' ? null : counterId
        }};
        
        fetch('/book', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(bookingData)
        }})
        .then(response => response.json())
        .then(data => {{
            if(data.ok) {{
                alert('Booking confirmed! Fare: ₹' + data.fare);
                // Seat को booked में बदलें
                const seatBtn = document.getElementById('seat-' + seat);
                seatBtn.classList.remove('seat-selected');
                seatBtn.classList.add('seat-booked');
                seatBtn.disabled = true;
                seatBtn.innerHTML = 'S' + seat + ' ✓';
                
                // Socket से broadcast करें
                socket.emit('seat_booked', {{
                    schedule_id: {sid},
                    seat_number: seat,
                    date: "{session.get('date')}"
                }});
                
                // Form reset करें
                document.getElementById('bookingForm').style.display = 'none';
                selectedSeat = null;
            }} else {{
                alert('Error: ' + (data.error || 'Booking failed'));
            }}
        }})
        .catch(error => {{
            console.error('Error:', error);
            alert('Network error. Please try again.');
        }});
    }}
    
    // Real-time seat updates
    socket.on('seat_update', function(data) {{
        if(data.schedule_id == {sid} && data.date == "{session.get('date')}") {{
            const seatBtn = document.getElementById('seat-' + data.seat_number);
            if(seatBtn) {{
                seatBtn.classList.remove('seat-available');
                seatBtn.classList.add('seat-booked');
                seatBtn.disabled = true;
                seatBtn.innerHTML = 'S' + data.seat_number + ' ✓';
            }}
        }}
    }});
    </script>
    """
    
    return render_template_string(BASE_HTML, content=content)

@app.route("/book", methods=["POST"])
def book():
    try:
        data = request.get_json()
        
        # Check if seat already booked
        existing = supabase_query("seat_bookings", filters={
            "schedule_id": data["schedule_id"],
            "seat_number": data["seat_number"],
            "travel_date": data["date"],
            "status": "confirmed"
        })
        
        if existing and len(existing) > 0:
            return jsonify({"ok": False, "error": "Seat already booked"}), 409
        
        # Insert booking
        booking_data = {
            "schedule_id": data["schedule_id"],
            "seat_number": data["seat_number"],
            "passenger_name": data["passenger_name"],
            "mobile": data["mobile"],
            "from_station": data.get("from", session.get("from")),
            "to_station": data.get("to", session.get("to")),
            "travel_date": data["date"],
            "fare": data["fare"],
            "status": "confirmed",
            "payment_mode": data.get("payment_mode", "cash"),
            "booked_by_type": session.get("role", "user"),
            "booked_by_id": session.get("user_id", 0),
            "counter_id": data.get("counter_id"),
            "created_at": datetime.now().isoformat()
        }
        
        result = supabase_query("seat_bookings", "insert", booking_data)
        
        if result:
            # Real-time update broadcast
            socketio.emit("seat_update", {
                "schedule_id": data["schedule_id"],
                "seat_number": data["seat_number"],
                "date": data["date"]
            })
            
            return jsonify({
                "ok": True, 
                "fare": data["fare"],
                "booking_id": result[0]["id"] if result else None
            })
        else:
            return jsonify({"ok": False, "error": "Failed to save booking"}), 500
            
    except Exception as e:
        print(f"Booking error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/live-bus/<int:sid>")
def live_bus(sid):
    # Bus details
    bus_data = supabase_query("schedules", filters={"id": sid})
    if not bus_data:
        return "Bus not found", 404
    bus = bus_data[0]
    
    # Route details
    route_data = supabase_query("routes", filters={"id": bus["route_id"]})
    route = route_data[0] if route_data else {"route_name": "Unknown"}
    
    # Route stations for map
    stations_data = supabase_query("route_stations", filters={"route_id": bus["route_id"]})
    
    stations_json = json.dumps(stations_data) if stations_data else "[]"
    
    content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Live Bus Tracking - {bus['bus_name']}</title>
        <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
        <style>
            body {{ margin: 0; padding: 0; font-family: Arial, sans-serif; }}
            #map {{ height: 100vh; width: 100%; }}
            .info-panel {{
                position: absolute;
                top: 20px;
                left: 20px;
                background: white;
                padding: 20px;
                border-radius: 10px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.2);
                z-index: 1000;
                max-width: 300px;
            }}
            .bus-marker {{
                background: #ff4444;
                border-radius: 50%;
                width: 20px;
                height: 20px;
                border: 3px solid white;
                box-shadow: 0 0 10px rgba(0,0,0,0.3);
                animation: pulse 2s infinite;
            }}
            @keyframes pulse {{
                0% {{ transform: scale(1); opacity: 1; }}
                50% {{ transform: scale(1.2); opacity: 0.8; }}
                100% {{ transform: scale(1); opacity: 1; }}
            }}
        </style>
    </head>
    <body>
        <div class="info-panel">
            <h3>🚌 {bus['bus_name']}</h3>
            <p><strong>Route:</strong> {route['route_name']}</p>
            <p><strong>Departure:</strong> {bus['departure_time']}</p>
            <p id="live-status"><strong>Status:</strong> <span class="text-success">LIVE</span></p>
            <p id="coordinates"><strong>Coordinates:</strong> Loading...</p>
            <p id="speed"><strong>Speed:</strong> 0 km/h</p>
            <a href="/seats/{sid}" class="btn btn-primary">Book Seat</a>
        </div>
        
        <div id="map"></div>
        
        <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
        <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
        <script>
            const map = L.map('map').setView([{bus.get('current_lat', 27.2)}, {bus.get('current_lng', 75.2)}], 13);
            
            L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
                attribution: '© OpenStreetMap contributors'
            }}).addTo(map);
            
            // Route stations
            const stations = {stations_json};
            const routePoints = [];
            
            stations.forEach(station => {{
                const lat = parseFloat(station.lat || 27.2);
                const lng = parseFloat(station.lng || 75.2);
                if(!isNaN(lat) && !isNaN(lng)) {{
                    routePoints.push([lat, lng]);
                    L.marker([lat, lng])
                        .addTo(map)
                        .bindPopup(`<b>📍 ${{station.station_name}}</b>`);
                }}
            }});
            
            // Route line
            if(routePoints.length > 1) {{
                L.polyline(routePoints, {{
                    color: 'blue',
                    weight: 4,
                    opacity: 0.7
                }}).addTo(map);
            }}
            
            // Bus marker
            const busIcon = L.divIcon({{
                html: '<div class="bus-marker"></div>',
                className: 'bus-icon',
                iconSize: [26, 26]
            }});
            
            let busMarker = L.marker([
                {bus.get('current_lat', 27.2)}, 
                {bus.get('current_lng', 75.2)}
            ], {{icon: busIcon}}).addTo(map);
            
            // Socket connection
            const socket = io(window.location.origin);
            
            socket.on('bus_location', data => {{
                if(data.sid == {sid}) {{
                    const lat = parseFloat(data.lat);
                    const lng = parseFloat(data.lng);
                    
                    busMarker.setLatLng([lat, lng]);
                    map.panTo([lat, lng]);
                    
                    // Update info panel
                    document.getElementById('coordinates').innerHTML = 
                        `<strong>Coordinates:</strong> ${{lat.toFixed(6)}}, ${{lng.toFixed(6)}}`;
                    document.getElementById('speed').innerHTML = 
                        `<strong>Speed:</strong> ${{data.speed || 0}} km/h`;
                }}
            }});
        </script>
    </body>
    </html>
    """
    
    return content

@app.route("/driver/<int:sid>")
def driver_page(sid):
    return f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Driver GPS - Bus {sid}</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            margin: 0;
            padding: 20px;
            background: #f0f0f0;
        }}
        .container {{
            max-width: 600px;
            margin: 0 auto;
            background: white;
            padding: 30px;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        h2 {{
            color: #333;
            text-align: center;
        }}
        .btn {{
            padding: 15px 30px;
            font-size: 18px;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            font-weight: bold;
            margin: 10px;
            width: 100%;
        }}
        .btn-start {{
            background: #28a745;
            color: white;
        }}
        .btn-stop {{
            background: #dc3545;
            color: white;
        }}
        #status {{
            background: #f8f9fa;
            padding: 20px;
            border-radius: 5px;
            margin-top: 20px;
            font-family: monospace;
            min-height: 100px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h2>🚗 Driver GPS - Bus {sid}</h2>
        
        <button id="startBtn" class="btn btn-start" onclick="startGPS()">
            🚀 Start GPS Tracking
        </button>
        
        <button id="stopBtn" class="btn btn-stop" onclick="stopGPS()" disabled>
            🛑 Stop GPS Tracking
        </button>
        
        <div id="status">
            GPS is not active. Click "Start GPS Tracking" to begin.
        </div>
    </div>
    
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <script>
        const socket = io(window.location.origin);
        let watchId = null;
        
        function startGPS() {{
            if (!navigator.geolocation) {{
                document.getElementById('status').innerHTML = 
                    '❌ GPS not supported by this browser';
                return;
            }}
            
            document.getElementById('startBtn').disabled = true;
            document.getElementById('stopBtn').disabled = false;
            
            watchId = navigator.geolocation.watchPosition(
                (position) => {{
                    const lat = position.coords.latitude;
                    const lng = position.coords.longitude;
                    const speed = position.coords.speed || 0;
                    
                    // Send to server
                    socket.emit('driver_gps', {{
                        sid: {sid},
                        lat: lat,
                        lng: lng,
                        speed: speed * 3.6, // Convert m/s to km/h
                        timestamp: new Date().toISOString()
                    }});
                    
                    // Update status
                    document.getElementById('status').innerHTML = 
                        `✅ LIVE GPS<br>
                         Latitude: ${{lat.toFixed(6)}}<br>
                         Longitude: ${{lng.toFixed(6)}}<br>
                         Speed: ${{(speed * 3.6).toFixed(1)}} km/h<br>
                         Time: ${{new Date().toLocaleTimeString()}}`;
                }},
                (error) => {{
                    document.getElementById('status').innerHTML = 
                        `❌ GPS Error: ${{error.message}}`;
                    document.getElementById('startBtn').disabled = false;
                    document.getElementById('stopBtn').disabled = true;
                }},
                {{
                    enableHighAccuracy: true,
                    timeout: 10000,
                    maximumAge: 0
                }}
            );
        }}
        
        function stopGPS() {{
            if (watchId !== null) {{
                navigator.geolocation.clearWatch(watchId);
                watchId = null;
            }}
            
            document.getElementById('startBtn').disabled = false;
            document.getElementById('stopBtn').disabled = true;
            
            document.getElementById('status').innerHTML = 
                '🛑 GPS tracking stopped';
        }}
    </script>
</body>
</html>
"""

@app.route("/create-counter", methods=["GET", "POST"])
@admin_required
def create_counter():
    error = ""
    success = ""
    
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        counter_no = request.form.get("counter_no", "").strip()
        
        if not username or not password or not counter_no:
            error = "All fields are required"
        else:
            try:
                # Check if username exists
                existing = supabase_query("admins", filters={"username": username})
                if existing and len(existing) > 0:
                    error = "Username already exists"
                else:
                    supabase_query("admins", "insert", {
                        "username": username,
                        "password": password,
                        "role": "counter",
                        "counter_no": int(counter_no)
                    })
                    success = f"Counter '{username}' created successfully!"
            except Exception as e:
                error = str(e)
    
    content = f"""
    <div class="container">
        <div class="row justify-content-center">
            <div class="col-md-6">
                <div class="card">
                    <div class="card-header">
                        <h4 class="mb-0">➕ Create New Counter</h4>
                    </div>
                    <div class="card-body">
                        <form method="POST">
                            <div class="mb-3">
                                <label class="form-label">Username</label>
                                <input type="text" name="username" class="form-control" required>
                            </div>
                            <div class="mb-3">
                                <label class="form-label">Password</label>
                                <input type="password" name="password" class="form-control" required>
                            </div>
                            <div class="mb-3">
                                <label class="form-label">Counter Number</label>
                                <input type="number" name="counter_no" class="form-control" required>
                            </div>
                            <button type="submit" class="btn btn-success w-100">Create Counter</button>
                        </form>
                        
                        {f'<div class="alert alert-success mt-3">{success}</div>' if success else ''}
                        {f'<div class="alert alert-danger mt-3">{error}</div>' if error else ''}
                        
                        <div class="mt-3">
                            <h5>Existing Counters:</h5>
                            {render_counters_list()}
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    """
    
    return render_template_string(BASE_HTML, content=content)

def render_counters_list():
    counters = supabase_query("admins", filters={"role": "counter"})
    if not counters:
        return "<p>No counters found</p>"
    
    html = '<div class="list-group">'
    for counter in counters:
        html += f"""
        <div class="list-group-item">
            <div class="d-flex justify-content-between">
                <div>
                    <strong>{counter['username']}</strong>
                    <div>Counter #{counter.get('counter_no', 'N/A')}</div>
                </div>
                <div>
                    <span class="badge bg-info">Active</span>
                </div>
            </div>
        </div>
        """
    html += '</div>'
    return html

@app.route("/search", methods=["POST"])
def search():
    from_station = request.form.get("from", "").strip()
    to_station = request.form.get("to", "").strip()
    travel_date = request.form.get("date", date.today().isoformat())
    
    if not from_station or not to_station:
        return "Please select both From and To stations", 400
    
    # Store in session
    session["from"] = from_station
    session["to"] = to_station
    session["date"] = travel_date
    
    # Find routes containing both stations
    from_routes = supabase_query("route_stations", filters={"station_name": from_station})
    to_routes = supabase_query("route_stations", filters={"station_name": to_station})
    
    if not from_routes or not to_routes:
        return render_template_string(
            BASE_HTML,
            content=f"""
            <div class="alert alert-warning text-center">
                <h3>No routes found for {from_station} → {to_station}</h3>
                <a href="/" class="btn btn-primary">Back to Home</a>
            </div>
            """
        )
    
    from_route_ids = set([r["route_id"] for r in from_routes])
    to_route_ids = set([r["route_id"] for r in to_routes])
    
    common_routes = from_route_ids.intersection(to_route_ids)
    
    if not common_routes:
        return render_template_string(
            BASE_HTML,
            content=f"""
            <div class="alert alert-warning text-center">
                <h3>No direct routes for {from_station} → {to_station}</h3>
                <a href="/" class="btn btn-primary">Back to Home</a>
            </div>
            """
        )
    
    # Check station order
    valid_routes = []
    for route_id in common_routes:
        from_station_data = supabase_query("route_stations", filters={
            "route_id": route_id,
            "station_name": from_station
        })
        to_station_data = supabase_query("route_stations", filters={
            "route_id": route_id,
            "station_name": to_station
        })
        
        if from_station_data and to_station_data:
            from_order = from_station_data[0]["station_order"]
            to_order = to_station_data[0]["station_order"]
            
            if from_order < to_order:
                valid_routes.append(route_id)
    
    if not valid_routes:
        return render_template_string(
            BASE_HTML,
            content=f"""
            <div class="alert alert-warning text-center">
                <h3>No valid route found (check station order)</h3>
                <a href="/" class="btn btn-primary">Back to Home</a>
            </div>
            """
        )
    
    # Redirect to first valid route
    return redirect(f"/buses/{valid_routes[0]}")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# ================= ADMIN ROUTES =================
@app.route("/routes")
@admin_required
def manage_routes():
    routes = supabase_query("routes")
    
    content = """
    <div class="container">
        <h3>🛣️ Manage Routes</h3>
        <a href="/dashboard" class="btn btn-secondary mb-3">← Back to Dashboard</a>
        
        <div class="card">
            <div class="card-body">
                <table class="table">
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Route Name</th>
                            <th>Distance (km)</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody>
    """
    
    for route in routes:
        content += f"""
                        <tr>
                            <td>{route['id']}</td>
                            <td>{route['route_name']}</td>
                            <td>{route['distance_km']}</td>
                            <td>
                                <a href="/route-stations/{route['id']}" class="btn btn-sm btn-info">Stations</a>
                            </td>
                        </tr>
        """
    
    content += """
                    </tbody>
                </table>
            </div>
        </div>
    </div>
    """
    
    return render_template_string(BASE_HTML, content=content)

@app.route("/bookings")
@admin_required
def view_bookings():
    bookings = supabase_query("seat_bookings")
    
    # Get bus names
    buses = supabase_query("schedules")
    bus_dict = {bus["id"]: bus["bus_name"] for bus in buses}
    
    content = """
    <div class="container">
        <h3>🎫 All Bookings</h3>
        <a href="/dashboard" class="btn btn-secondary mb-3">← Back to Dashboard</a>
        
        <div class="card">
            <div class="card-body">
                <table class="table">
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Passenger</th>
                            <th>Bus</th>
                            <th>Seat</th>
                            <th>Date</th>
                            <th>Fare</th>
                            <th>Status</th>
                        </tr>
                    </thead>
                    <tbody>
    """
    
    for booking in bookings:
        bus_name = bus_dict.get(booking["schedule_id"], "Unknown")
        
        content += f"""
                        <tr>
                            <td>{booking['id']}</td>
                            <td>{booking['passenger_name']}</td>
                            <td>{bus_name}</td>
                            <td>{booking['seat_number']}</td>
                            <td>{booking['travel_date']}</td>
                            <td>₹{booking['fare']}</td>
                            <td><span class="badge bg-success">{booking['status']}</span></td>
                        </tr>
        """
    
    content += """
                    </tbody>
                </table>
            </div>
        </div>
    </div>
    """
    
    return render_template_string(BASE_HTML, content=content)

# ================= COUNTER ROUTES =================
@app.route("/counter-bookings")
@counter_required
def counter_bookings():
    counter_id = session.get("user_id")
    bookings = supabase_query("seat_bookings", filters={"counter_id": counter_id})
    
    content = f"""
    <div class="container">
        <h3>📋 My Counter Bookings</h3>
        <p>Counter: #{session.get('counter_no', 'N/A')}</p>
        <a href="/dashboard" class="btn btn-secondary mb-3">← Back to Dashboard</a>
        
        <div class="card">
            <div class="card-body">
                <table class="table">
                    <thead>
                        <tr>
                            <th>Passenger</th>
                            <th>Seat</th>
                            <th>Date</th>
                            <th>Fare</th>
                            <th>Payment</th>
                            <th>Time</th>
                        </tr>
                    </thead>
                    <tbody>
    """
    
    for booking in bookings:
        content += f"""
                        <tr>
                            <td>{booking['passenger_name']}</td>
                            <td>{booking['seat_number']}</td>
                            <td>{booking['travel_date']}</td>
                            <td>₹{booking['fare']}</td>
                            <td><span class="badge bg-info">{booking['payment_mode']}</span></td>
                            <td>{booking['created_at'][:19] if booking.get('created_at') else 'N/A'}</td>
                        </tr>
        """
    
    content += """
                    </tbody>
                </table>
            </div>
        </div>
    </div>
    """
    
    return render_template_string(BASE_HTML, content=content)

# ================= INITIALIZATION =================
if __name__ == "__main__":
    print("🚀 Starting My Bus AI Application...")
    
    # Initialize database
    init_db()
    
    # Start the application
    port = int(os.environ.get("PORT", 10000))
    print(f"🌐 Server running on port {port}")
    print(f"📊 Supabase Connected: {SUPABASE_URL}")
    
    socketio.run(app, host="0.0.0.0", port=port, debug=True)