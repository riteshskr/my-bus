from asyncio import transports
from dotenv import load_dotenv
import json
import bcrypt
import traceback
load_dotenv()
from openpyxl import Workbook
from flask import send_file
import io
import setuptools
from flask_cors import CORS
import requests
import os, random
from datetime import date, datetime
from functools import wraps
from flask import Flask, request, jsonify, render_template_string, redirect, g, session, render_template
from flask_socketio import SocketIO, emit
from flask_compress import Compress
from supabase import create_client, Client
import atexit
from dateutil import parser
import pandas as pd
from flask import Response
import razorpay
import os
IS_RENDER = os.environ.get("RENDER") == "true"
notifier = None 
if not IS_RENDER:
      from whatsapp_notifier import get_whatsapp_notifier
      notifier = get_whatsapp_notifier(headless=False)


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

LOCATIONIQ_KEY = os.getenv("LOCATIONIQ_KEY")

# ================= APP =================
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key-12345")
Compress(app)

# ✅ SocketIO Configuration
socketio = SocketIO(app, cors_allowed_origins="*", ping_timeout=60)

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
            hashed = bcrypt.hashpw("admin123".encode(), bcrypt.gensalt()).decode()
            supabase_query("admins", "insert", {
                "username": "admin",
                "password": hashed,
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
      <a href="/logout">Logout ({{ session.get('role', 'guest') }})</a>
    {% else %}
      <a href="/login">Admin Login</a>
      <a href="/counter_login">Counter Login</a>
    {% endif %}
    <a href="/">Home</a>
  </div>
</div>

{% if not content %}
<section class="hero">
  <div style="width:100%;padding:20px;">
    <h1 style="font-size:3rem;margin-bottom:20px;">My Bus AI Booking</h1>
     <form class="search-box" action="/search" method="POST">
      <select name="from" class="form-select" required>
        <option value="" selected disabled>From (Station</option>
        {% for station in stations %}
        <option value="{{ station }}">{{ station }}</option>
        {% endfor %}
      </select>

      <select name="to" class="form-select" required>
        <option value="" selected disabled>To (Station)</option>
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
            <a href="/counter_login">Counter Login</a>
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


# ================= get-distance ================= 
@app.route("/get-distance")
def get_distance():
    from_lat = request.args.get("from_lat")
    from_lng = request.args.get("from_lng")
    to_lat = request.args.get("to_lat")
    to_lng = request.args.get("to_lng")

    url = f"https://us1.locationiq.com/v1/directions/driving/{from_lng},{from_lat};{to_lng},{to_lat}?key={LOCATIONIQ_KEY}&overview=false"

    response = requests.get(url)
    data = response.json()

    if "routes" in data:
        distance_m = data["routes"][0]["distance"]
        distance_km = round(distance_m / 1000, 2)
        return jsonify({"distance": distance_km})
    else:
        return jsonify({"error": "Distance not found"})


#=================  login ================= 
@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        # Supabase से user fetch करें
        users = supabase_query("admins", filters={
            "username": username,
            "role": "admin"

        })

        if users and len(users) > 0:
            user = users[0]
            db_pass = user["password"]
            if bcrypt.checkpw(password.encode(), db_pass.encode()):
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

#=================  counter_login ================= 
@app.route("/counter_login", methods=["GET", "POST"])
def counter_login():
    error = ""

    # Step 1: सभी counter users fetch करें (dropdown के लिए)
    try:
        users_res = supabase.table("admins") \
            .select("username") \
            .eq("role", "counter") \
            .execute()
        usernames = [u["username"] for u in users_res.data] if users_res.data else []
    except Exception as e:
        print("Supabase Error fetching users:", e)
        usernames = []

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if not username or not password:
            error = "Username और Password दोनों चाहिए"
        else:
            try:
                # 🔐 सिर्फ username + role से user निकालो
                res = supabase.table("admins") \
                    .select("*") \
                    .eq("username", username) \
                    .eq("role", "counter") \
                    .execute()

                if res.data and len(res.data) > 0:
                    user = res.data[0]
                    db_pass = user["password"]   # hashed password from DB

                    # 🔑 यहीं bcrypt check होगा
                    if bcrypt.checkpw(password.encode(), db_pass.encode()):
                        session.clear()
                        session["user_logged_in"] = True
                        session["user_id"] = user["id"]
                        session["username"] = user["username"]
                        session["role"] = user["role"]
                        session["counter_no"] = user.get("counter_no", 0)

                        return redirect("/")  # Counter Dashboard
                    else:
                        error = "Invalid password"
                else:
                    error = "User not found"

            except Exception as e:
                print("Supabase Error:", e)
                error = "Server Error, try again"

    # ---------- HTML ----------
    login_html = f"""
    <div class="row justify-content-center mt-5">
      <div class="col-md-4">
        <div class="card shadow-lg border-0 rounded-4">
          <div class="card-body p-4">
            <h3 class="text-center mb-4">Counter Login</h3>

            <form method="POST" autocomplete="off">
              <div class="mb-3">
                <label class="form-label">Username</label>
                <select name="username" class="form-control" required>
                  <option value="">Select Username</option>
                  {''.join([f'<option value="{u}">{u}</option>' for u in usernames])}
                </select>
              </div>

              <div class="mb-3">
                <label class="form-label">Password</label>
                <input type="password" name="password"
                       class="form-control"
                       placeholder="Enter password" required>
              </div>

              <button class="btn btn-success w-100">Login</button>
            </form>

            {f'<div class="alert alert-danger mt-3">{error}</div>' if error else ''}
          </div>
        </div>
      </div>
    </div>
    """

    return render_template_string(BASE_HTML, content=login_html)

#=================  deshboard ================= 

@app.route("/dashboard")
def dashboard():
    if not session.get("user_logged_in"):
        return redirect("/login")

    role = session.get("role", "user")
    username = session.get("username", "User")

    total_bookings = len(supabase_query("seat_bookings") or [])
    total_buses = len(supabase_query("schedules") or [])
    total_routes = len(supabase_query("routes") or [])

    recent_bookings = supabase_query("seat_bookings", filters={"status": "confirmed"}) or []
    recent_bookings = recent_bookings[:5]

    admin_links = ""
    if role == "admin":
        admin_links = """
        <a href="/routes" class="btn btn-info shadow-sm">Manage Routes</a>
        <a href="/bookings" class="btn btn-success shadow-sm ms-2">View Bookings</a>
        <a href="/create-counter" class="btn btn-dark shadow-sm ms-2">Create Counter</a>
        <a href="/buses" class="btn btn-warning shadow-sm ms-2">New Bus</a>
        <a href="/backup" class="btn btn-secondary shadow-sm ms-2">Backup</a>
        """

    counter_links = ""
    if role == "counter":
        counter_links = """
        <a href="/counter-bookings" class="btn btn-primary">My Bookings</a>
        """

    counter_html = ""
    if session.get("counter_no"):
        counter_html = f"<h5>Counter Number: #{session.get('counter_no')}</h5>"

    content = f"""
    <div class="container">
        <h2>Welcome {username}</h2>
        <h4>Role: {role.upper()}</h4>
        {counter_html}

        <div class="row mt-4">
            <div class="col">Bookings: {total_bookings}</div>
            <div class="col">Buses: {total_buses}</div>
            <div class="col">Routes: {total_routes}</div>
        </div>

        <div class="mt-4">
            {admin_links}
            {counter_links}
        </div>
    </div>
    """

    return render_template_string(BASE_HTML, content=content)

#================= View Bookings  =================
@app.route("/bookings", methods=["GET"])
def view_bookings():

    if not session.get("user_logged_in"):
        return redirect("/login")

    route_id = request.args.get("route_id")
    bus_id = request.args.get("bus_id")
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")

    bookings = supabase_query("seat_bookings") or []
    routes = supabase_query("routes") or []
    schedules = supabase_query("schedules") or []

    # Fast lookup maps (important)
    route_map = {str(r["id"]): r.get("route_name", "") for r in routes}
    schedule_map = {str(s["id"]): s for s in schedules}

    # Route wise buses
    if route_id:
        available_buses = [
            s for s in schedules
            if str(s.get("route_id")) == str(route_id)
        ]
    else:
        available_buses = schedules

    filtered = []

    for b in bookings:

        schedule = schedule_map.get(str(b.get("schedule_id")))
        if not schedule:
            continue

        # Route filter
        if route_id and str(schedule.get("route_id")) != str(route_id):
            continue

        # Bus filter
        if bus_id and str(b.get("schedule_id")) != str(bus_id):
            continue

        travel_date = b.get("travel_date")
        if not travel_date:
            continue

        try:
            date_obj = parser.parse(travel_date)
            b["formatted_date"] = date_obj.strftime("%d-%m-%Y")
        except:
            continue

        # Date filter (date only compare)
        if date_from and date_obj.date() < parser.parse(date_from).date():
            continue

        if date_to and date_obj.date() > parser.parse(date_to).date():
            continue

        filtered.append(b)

    total_collection = sum(float(b.get("fare") or 0) for b in filtered)

    # Excel Export
    if request.args.get("export") == "excel":
        wb = Workbook()
        ws = wb.active
        ws.title = "Bookings"
        ws.append(["ID", "Passenger", "Route", "Bus", "Seat", "Date", "Fare","payment_mode",])

        for b in filtered:
            schedule = schedule_map.get(str(b.get("schedule_id")), {})
            ws.append([
                b.get("id"),
                b.get("passenger_name"),
                route_map.get(str(schedule.get("route_id")), ""),
                schedule.get("bus_name", ""),
                b.get("seat_number"),
                b.get("formatted_date"),
                b.get("fare"),
                b.get("payment_mode")
            ])

        file_stream = io.BytesIO()
        wb.save(file_stream)
        file_stream.seek(0)

        return send_file(
            file_stream,
            as_attachment=True,
            download_name="bookings.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    # HTML
    content = f"""
    <div class="container mt-4">
        <h2>View Bookings</h2>

        <form method="get" class="row g-3">
            <div class="col-md-3">
                <label>Route</label>
                <select name="route_id" class="form-control" onchange="this.form.submit()">
                    <option value="">All</option>
                    {''.join([f'<option value="{r["id"]}" {"selected" if str(route_id)==str(r["id"]) else ""}>{r["route_name"]}</option>' for r in routes])}
                </select>
            </div>

            <div class="col-md-3">
                <label>Bus</label>
                <select name="bus_id" class="form-control">
                    <option value="">All</option>
                    {''.join([f'<option value="{s["id"]}" {"selected" if str(bus_id)==str(s["id"]) else ""}>{s.get("bus_name","")}</option>' for s in available_buses])}
                </select>
            </div>

            <div class="col-md-2">
                <label>From</label>
                <input type="date" name="date_from" value="{date_from or ''}" class="form-control">
            </div>

            <div class="col-md-2">
                <label>To</label>
                <input type="date" name="date_to" value="{date_to or ''}" class="form-control">
            </div>

            <div class="col-md-2">
                <label>&nbsp;</label>
                <button class="btn btn-primary w-100">Filter</button>
            </div>
        </form>

        <h4 class="mt-3">Total Collection: ₹ {format(total_collection, ',.2f')}</h4>

        <table class="table table-bordered mt-3">
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Passenger</th>
                    <th>Route</th>
                    <th>Bus</th>
                    <th>Seat</th>
                    <th>Date</th>
                    <th>Fare</th>
                    <th>payment mode</th>
                </tr>
            </thead>
            <tbody>
                {''.join([
                    f"<tr>"
                    f"<td>{b.get('id')}</td>"
                    f"<td>{b.get('passenger_name')}</td>"
                    f"<td>{route_map.get(str(schedule_map.get(str(b.get('schedule_id')),{}).get('route_id')), '')}</td>"
                    f"<td>{schedule_map.get(str(b.get('schedule_id')),{}).get('bus_name','')}</td>"
                    f"<td>{b.get('seat_number')}</td>"
                    f"<td>{b.get('formatted_date')}</td>"
                    f"<td>{b.get('fare')}</td>"
                    f"<td>{b.get('payment_mode')}</td>"
                    f"</tr>"
                    for b in filtered
                ])}
            </tbody>
        </table>
    </div>
    """

    return render_template_string(BASE_HTML, content=content)


# ================== Manage Stations AJAX ==================
@app.route("/route/<int:route_id>/stations", methods=["GET"])
@admin_required
def manage_stations_page(route_id):
    # GET → route stations fetch करना
    stations = supabase_query("route_stations", filters={"route_id": route_id}) or []
    stations = sorted(stations, key=lambda x: x["station_order"])

    content = """
    <h2>Stations for Route ID: {{ route_id }}</h2>
    <table class="table table-bordered mt-3" id="stationsTable">
        <thead>
            <tr>
                <th>Route ID</th>
                <th>Station Name</th>
                <th>Station Order</th>
            </tr>
        </thead>
        <tbody>
            {% for s in stations %}
            <tr>
                <td>{{ s.route_id }}</td>
                <td>{{ s.station_name }}</td>
                <td>{{ s.station_order }}</td>
            </tr>
            {% endfor %}
        </tbody>
    </table>

    <h4 class="mt-4">Add New Station</h4>
    <form id="addStationForm" class="row g-3 mt-2">
        <div class="col-md-6">
            <input type="text" name="station_name" class="form-control" placeholder="Station Name" required>
        </div>
        <div class="col-md-3">
            <input type="number" name="station_order" class="form-control" placeholder="Station Order" required>
        </div>
        <div class="col-md-3">
            <button class="btn btn-success w-100" type="submit">Add Station</button>
        </div>
    </form>

    <a href="/routes" class="btn btn-secondary mt-4">Back to Routes</a>

    <script>
    const form = document.getElementById('addStationForm');
    form.addEventListener('submit', async function(e) {
        e.preventDefault();
        const formData = new FormData(form);
        const data = {
            station_name: formData.get('station_name'),
            station_order: formData.get('station_order')
        };

        const res = await fetch('/api/route/{{ route_id }}/add_station', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });

        const result = await res.json();
        if(result.ok){
            // Table में नया row add करें
            const table = document.getElementById('stationsTable').getElementsByTagName('tbody')[0];
            const newRow = table.insertRow();
            newRow.innerHTML = `
                <td>{{ route_id }}</td>
                <td>${data.station_name}</td>
                <td>${data.station_order}</td>
            `;
            form.reset();
        } else {
            alert("Error: " + result.error);
        }
    });
    </script>
    """
    return render_template_string(BASE_HTML, content=render_template_string(content, route_id=route_id, stations=stations))


# ================== API Add Station ==================
@app.route("/api/route/<int:route_id>/add_station", methods=["POST"])
@admin_required
def api_add_station(route_id):
    try:
        data = request.get_json()
        station_name = data.get("station_name", "").strip()
        station_order = int(data.get("station_order", 0))

        if not station_name or station_order <= 0:
            return jsonify({"ok": False, "error": "Invalid data"})

        supabase_query("route_stations", "insert", {
            "route_id": route_id,
            "station_name": station_name,
            "station_order": station_order
        })
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/buses/<int:rid>")
def buses(rid):
    route = supabase_query("routes", filters={"id": rid})[0]
    buses = supabase_query("schedules", filters={"route_id": rid}) or []

    bus_html = ""
    for bus in buses:
        bus_html += f"""
        <div class="card mb-3">
            <div class="card-body">
                <h5>{bus['bus_name']}</h5>
                <p>Departure: {bus['departure_time']}</p>
                <a href="/seats/{bus['id']}" class="btn btn-success">Book</a>
            </div>
        </div>
        """

    content = f"""
    <div class="container">
        <h2>{route['route_name']}</h2>
        {bus_html}
        <a href="/" class="btn btn-secondary">Home</a>
    </div>
    """

    return render_template_string(BASE_HTML, content=content)


@app.route("/seats/<int:sid>")
def seat_page(sid):
    try:
        bus_data = supabase_query("schedules", filters={"id": sid})
        if not bus_data:
            return "Schedule not found", 404

        bus = bus_data[0]
        route_id = bus["route_id"]

        today = session.get("date")

        route_rows = supabase_query("route_stations", filters={
            "route_id": route_id
        }) or []

        route_rows = sorted(route_rows, key=lambda x: x["station_order"])
        route = [r["station_name"] for r in route_rows]

        bookings = supabase_query("seat_bookings", filters={
            "schedule_id": sid,
            "travel_date": today,
            "status": "confirmed"
        }) or []

        from_station = session.get("from_station")
        to_station = session.get("to_station")

        if not from_station or not to_station:
            return "<h3>पहले route select करो</h3>"

        def is_overlap(b):
            nf = route.index(from_station)
            nt = route.index(to_station)
            ef = route.index(b["from_station"])
            et = route.index(b["to_station"])
            return nf < et and nt > ef

        blocked = []
        for b in bookings:
            if is_overlap(b):
                blocked.append({"seat_number": b["seat_number"]})

        return render_template(
            "seat.html",
            schedule=bus,
            booked_seats=blocked,
            sid=sid,
            travel_date=today,
            bus_name=bus['bus_name'],
            departure_time=bus['departure_time']
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"Server Error: {e}", 500

@app.route("/api/bus/<int:sid>")
def bus_from_table(sid):
    data = supabase_query("schedules", filters={"id": sid})

    if not data or len(data) == 0:
        return jsonify({"error": "Bus not found"}), 404

    row = data[0]

    return jsonify({
        "lat": row.get("current_lat", 0),
        "lng": row.get("current_lng", 0),
        "speed": row.get("speed", 0)
    })

@app.route("/book", methods=["POST"])
def book():
    try:
        data = request.get_json()

        schedule_id = int(data["schedule_id"])
        seat_number = int(data["seat_number"])
        travel_date = data.get("date") or session.get("date")

        if not travel_date:
            return jsonify({"ok": False, "error": "travel_date missing"}), 400

        # Already booked check
        existing = supabase.table("seat_bookings") \
            .select("id") \
            .eq("schedule_id", schedule_id) \
            .eq("seat_number", seat_number) \
            .eq("travel_date", travel_date) \
            .execute()

        if existing.data:
            return jsonify({"ok": False, "error": "Seat already booked"})

        role = session.get("role")
        if role not in ["counter", "admin", "guest"]:
            role = "guest"

        fare = int(data.get("fare", 0))
        payment_mode = data.get("payment_mode", "cash")

        booking_data = {
            "schedule_id": schedule_id,
            "seat_number": seat_number,
            "passenger_name": data["passenger_name"],
            "mobile": data["mobile"],
            "from_station": session.get("from_station", "NA"),
            "to_station": session.get("to_station", "NA"),
            "travel_date": travel_date,
            "fare": fare,
            "status": "confirmed",
            "payment_mode": payment_mode,
            "booked_by_type": role,
            "booked_by_id": session.get("user_id", 0),
            "counter_id": session.get("user_id", 0)
        }

        res = supabase.table("seat_bookings").insert(booking_data).execute()

        if not res.data:
            return jsonify({"ok": False, "error": "Insert failed"})

        # 🔥 realtime broadcast
     socketio.emit(
            "seat_update",
            {
                "sid": schedule_id,
                "seat": seat_number,
                "date": travel_date
            },
            broadcast=True
        )

        booking_id = res.data[0]["id"]
        if notifier:
             notifier.send_booking_confirmation_by_id(booking_id)
        return jsonify({"ok": True, "message": "Seat booked"})

    except Exception as e:
        traceback.print_exc()
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

    # MapTiler API Key - अपनी key यहाँ use करें
    MAPTILER_KEY = os.getenv("MAPTILER_KEY")

    content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Live Bus Tracking - {bus['bus_name']}</title>
        <!-- MapTiler SDK CSS -->
        <link rel="stylesheet" href="https://cdn.maptiler.com/maptiler-sdk-js/v2.2.4/maptiler-sdk.css" />
        <!-- CSP Policy for CORS -->
        <meta http-equiv="Content-Security-Policy" content="default-src * self blob: data: gap:; style-src * self 'unsafe-inline' blob: data:; script-src * self 'unsafe-inline' 'unsafe-eval' blob: data:; object-src * self blob: data:; img-src * self 'unsafe-inline' blob: data:; connect-src self * https://api.maptiler.com wss:; frame-src * self blob: data:;">
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

        <!-- MapTiler SDK JS -->
        <script src="https://cdn.maptiler.com/maptiler-sdk-js/v2.2.4/maptiler-sdk.umd.min.js"></script>
        <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
        <script>
            // MapTiler Configuration
            maptilersdk.config.apiKey = '{MAPTILER_KEY}';
            maptilersdk.config.crossOrigin = 'anonymous';
            maptilersdk.config.region = 'global';

            // Initialize map
            const map = new maptilersdk.Map({{
                container: 'map',
                style: maptilersdk.MapStyle.STREETS,
                center: [{bus.get('current_lng', 75.2)}, {bus.get('current_lat', 27.2)}],
                zoom: 13,
                navigationControl: true
            }});

            // Route stations
            const stations = {stations_json};
            const routePoints = [];

            map.on('load', function() {{
                console.log('✅ Map loaded');

                stations.forEach(station => {{
                    const lat = parseFloat(station.lat || 27.2);
                    const lng = parseFloat(station.lng || 75.2);
                    
                    if(!isNaN(lat) && !isNaN(lng)) {{
                        routePoints.push([lng, lat]);  // MapTiler uses [lng, lat]
                        
                        // Add marker for station
                        new maptilersdk.Marker({{ color: '#28a745' }})
                            .setLngLat([lng, lat])
                            .setPopup(new maptilersdk.Popup().setHTML(`<b>📍 ${{station.station_name}}</b>`))
                            .addTo(map);
                    }}
                }});

                // Draw route line
                if(routePoints.length > 1) {{
                    map.addLayer({{
                        id: 'route-line',
                        type: 'line',
                        source: {{
                            type: 'geojson',
                            data: {{
                                type: 'Feature',
                                geometry: {{
                                    type: 'LineString',
                                    coordinates: routePoints
                                }}
                            }}
                        }},
                        paint: {{
                            'line-color': '#007bff',
                            'line-width': 4,
                            'line-opacity': 0.7,
                            'line-dasharray': [3, 2]
                        }}
                    }});
                }}
            }});

            // Bus marker
            const busMarkerElement = document.createElement('div');
            busMarkerElement.className = 'bus-marker';
            
            const busMarker = new maptilersdk.Marker({{ element: busMarkerElement }})
                .setLngLat([{bus.get('current_lng', 75.2)}, {bus.get('current_lat', 27.2)}])
                .setPopup(new maptilersdk.Popup().setHTML('<b>🚌 बस यहाँ है</b>'))
                .addTo(map);

            // Socket connection
            const socket = io(window.location.origin);

            socket.on('bus_location', data => {{
                if(data.sid == {sid}) {{
                    const lat = parseFloat(data.lat);
                    const lng = parseFloat(data.lng);
                    const speed = parseFloat(data.speed) || 0;

                    busMarker.setLngLat([lng, lat]);
                    map.panTo([lng, lat]);

                    document.getElementById('coordinates').innerHTML = 
                        `<strong>Coordinates:</strong> ${{lat.toFixed(6)}}, ${{lng.toFixed(6)}}`;
                    document.getElementById('speed').innerHTML = 
                        `<strong>Speed:</strong> ${{speed.toFixed(1)}} km/h`;
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
body {{ font-family: Arial; background:#f0f0f0; padding:20px; }}
.container {{ max-width:600px;margin:auto;background:white;padding:30px;border-radius:12px; }}
.btn {{ width:100%; padding:16px; font-size:18px; border:none; border-radius:8px; margin-top:10px; }}
.start {{ background:#28a745;color:white; }}
.stop {{ background:#dc3545;color:white; }}
#status {{ margin-top:20px;padding:15px;background:#f8f9fa;border-radius:8px;font-family:monospace; }}
</style>
</head>
<body>

<div class="container">
<h2>🚌 Driver GPS - Bus {sid}</h2>

<button id="startBtn" class="btn start" onclick="startGPS()">Start GPS</button>
<button id="stopBtn" class="btn stop" onclick="stopGPS()" disabled>Stop GPS</button>

<div id="status">GPS not started</div>
</div>

<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<script>
const socket = io("/", {{ transports:["websocket"] }});
let watchId = null;

function startGPS() {{
 if(!navigator.geolocation) {{
   statusBox("GPS not supported");
   return;
 }}

 startBtn.disabled=true;
 stopBtn.disabled=false;

 watchId = navigator.geolocation.watchPosition(
   pos => {{
     const lat = pos.coords.latitude;
     const lng = pos.coords.longitude;
     const speed = pos.coords.speed || 0;

     socket.emit("driver_gps", {{
       sid:{sid},
       lat:lat,
       lng:lng,
       speed:speed*3.6
     }});

     statusBox(
       "LIVE GPS\\n" +
       "Lat: "+lat.toFixed(6)+"\\n" +
       "Lng: "+lng.toFixed(6)+"\\n" +
       "Speed: "+(speed*3.6).toFixed(1)+" km/h"
     );
   }},
   err => {{
     statusBox("Error: "+err.message);
     stopGPS();
   }},
   {{ enableHighAccuracy:true, timeout:10000, maximumAge:0 }}
 );
}}

function stopGPS() {{
 if(watchId) navigator.geolocation.clearWatch(watchId);
 watchId=null;
 startBtn.disabled=false;
 stopBtn.disabled=true;
 statusBox("GPS stopped");
}}

function statusBox(t) {{
 document.getElementById("status").innerText=t;
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
                    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
                    supabase_query("admins", "insert", {
                        "username": username,
                        "password": hashed,
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
    session["from_station"] = from_station
    session["to_station"] = to_station
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
@app.route("/routes", methods=["GET"])
@admin_required
def manage_routes():
    routes = supabase_query("routes") or []

    content = """
    <h3>🛣️ Manage Routes</h3>

    <!-- Add Route Card -->
    <div class="card mb-3">
        <div class="card-body">
            <h5>Add New Route</h5>
            <form id="addRouteForm" class="row g-3">
                <div class="col-md-6">
                    <input type="text" name="route_name" class="form-control" placeholder="Route Name" required>
                </div>
                <div class="col-md-3">
                    <input type="number" name="distance_km" class="form-control" placeholder="Distance (km)" required>
                </div>
                <div class="col-md-3">
                    <button type="submit" class="btn btn-success w-100">Add Route</button>
                </div>
            </form>
        </div>
    </div>

    <!-- Routes Table -->
    <div class="card">
        <div class="card-body">
            <table class="table table-bordered">
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
                <a href="/route/{route['id']}/stations" class="btn btn-sm btn-info">Stations</a>

                <button class="btn btn-sm btn-warning"
                    onclick="openEditModal({route['id']}, '{route['route_name']}', {route['distance_km']})">
                    Edit
                </button>

         <button class="btn btn-sm btn-danger"
    	onclick="deleteRoute({route['id']}, '{route['route_name']}')">
    	Delete
            </button>
            </td>
        </tr>
        """

    content += """
                </tbody>
            </table>
        </div>
    </div>

    <!-- Edit Modal -->
    <div class="modal fade" id="editRouteModal" tabindex="-1">
      <div class="modal-dialog">
        <div class="modal-content">
          <div class="modal-header">
            <h5 class="modal-title">Edit Route</h5>
            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
          </div>
          <div class="modal-body">
            <input type="hidden" id="edit_route_id">
            <div class="mb-3">
                <label>Route Name</label>
                <input type="text" id="edit_route_name" class="form-control">
            </div>
            <div class="mb-3">
                <label>Distance (km)</label>
                <input type="number" id="edit_distance_km" class="form-control">
            </div>
          </div>
          <div class="modal-footer">
            <button class="btn btn-primary" onclick="updateRoute()">Update</button>
          </div>
        </div>
      </div>
    </div>

    <script>

    // ADD ROUTE
    const addRouteForm = document.getElementById('addRouteForm');
    addRouteForm.addEventListener('submit', async function(e){
        e.preventDefault();

        const formData = new FormData(addRouteForm);
        const data = {
            route_name: formData.get('route_name'),
            distance_km: formData.get('distance_km')
        };

        const res = await fetch('/api/add_route', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify(data)
        });

        const result = await res.json();
        if(result.ok){
            location.reload();
        } else {
            alert(result.error);
        }
    });

    // OPEN EDIT MODAL
    function openEditModal(id, name, distance){
        document.getElementById("edit_route_id").value = id;
        document.getElementById("edit_route_name").value = name;
        document.getElementById("edit_distance_km").value = distance;

        var modal = new bootstrap.Modal(document.getElementById('editRouteModal'));
        modal.show();
    }

    // UPDATE ROUTE
    async function updateRoute(){
        const id = document.getElementById("edit_route_id").value;
        const route_name = document.getElementById("edit_route_name").value;
        const distance_km = document.getElementById("edit_distance_km").value;

        const res = await fetch('/api/update_route', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({id, route_name, distance_km})
        });

        const result = await res.json();
        if(result.ok){
            location.reload();
        } else {
            alert(result.error);
        }
    }

    // DELETE ROUTE
    async function deleteRoute(id, name){

    if(!confirm("Delete Route: " + name + " ? This will also delete all stations.")) return;

    const res = await fetch('/api/delete_route', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
            id: id,
            route_name: name
        })
    });

    const result = await res.json();

    if(result.ok){
        alert(result.message);
        location.reload();
    } else {
        alert(result.error);
    }
}

    </script>
    """

    return render_template_string(BASE_HTML, content=content)


# ==============================
# API ADD ROUTE
# ==============================

@app.route("/api/add_route", methods=["POST"])
@admin_required
def api_add_route():
    data = request.get_json()
    print("API CALLED")

    route_name = data.get("route_name", "").strip()
    distance_km = data.get("distance_km")

    if not route_name:
        return jsonify({"ok": False, "error": "Route name required"})

    try:
        distance_km = float(distance_km)
    except:
        return jsonify({"ok": False, "error": "Distance must be number"})

    if distance_km <= 0:
        return jsonify({"ok": False, "error": "Distance must be greater than 0"})

    existing = supabase_query("routes", filters={"route_name": route_name})
    if existing:
        return jsonify({"ok": False, "error": "Route already exists"})

    supabase_query("routes", "insert", {
        "route_name": route_name,
        "distance_km": distance_km
    })

    return jsonify({"ok": True})


# ==============================
# API UPDATE ROUTE
# ==============================

@app.route("/api/update_route", methods=["POST"])
@admin_required
def api_update_route():
    data = request.get_json()

    route_id = data.get("id")
    route_name = data.get("route_name", "").strip()
    distance_km = data.get("distance_km")

    if not route_id or not route_name:
        return jsonify({"ok": False, "error": "Invalid data"})

    try:
        distance_km = float(distance_km)
    except:
        return jsonify({"ok": False, "error": "Distance must be number"})

    supabase_query("routes", "update",
        {"route_name": route_name, "distance_km": distance_km},
        filters={"id": route_id}
    )

    return jsonify({"ok": True})


# ==============================
# API DELETE ROUTE
# ==============================

@app.route("/api/delete_route", methods=["POST"])
@admin_required
def api_delete_route():
    try:
        data = request.get_json()

        route_id = int(data.get("id"))
        route_name = data.get("route_name")

        if not route_id:
            return jsonify({"ok": False, "error": "Invalid route id"})

        # STEP 1: Delete stations first
        supabase_query(
            "route_stations",
            "delete",
            filters={"route_id": route_id}
        )

        # STEP 2: Delete route
        supabase_query(
            "routes",
            "delete",
            filters={"id": route_id}
        )

        return jsonify({
            "ok": True,
            "message": f"Route '{route_name}' (ID: {route_id}) and all its stations deleted successfully"
        })

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


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

    init_db()

    port = int(os.environ.get("PORT", 10000))
    print(f"🌐 Server running on port {port}")
    print(f"📊 Supabase Connected: {SUPABASE_URL}")

    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        debug=True   # ✅ ADD THIS
    )