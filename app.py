from dotenv import load_dotenv
import json

load_dotenv()
import os, random
from datetime import date, datetime
from functools import wraps
from flask import Flask, request, jsonify, render_template_string, redirect, g, session
from flask_socketio import SocketIO, emit
from flask_compress import Compress
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
import atexit
import razorpay

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
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key-change-this-123")
Compress(app)

# SocketIO Configuration
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                    ping_timeout=60)

# ================= DATABASE =================
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    # Use in-memory mode if no DATABASE_URL
    print("⚠️ Warning: DATABASE_URL not found. Using in-memory mode.")
    DATABASE_CONNECTED = False
else:
    try:
        pool = ConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=5, timeout=10)
        DATABASE_CONNECTED = True
        print("✅ Database connection pool ready")
    except Exception as e:
        print(f"❌ Database connection failed: {e}")
        print("⚠️ Falling back to in-memory mode")
        DATABASE_CONNECTED = False

# In-memory data storage for fallback
in_memory_data = {
    'buses': {
        1: {'id': 1, 'bus_name': 'Volvo AC Sleeper', 'route_name': 'बीकानेर → जयपुर', 'departure_time': '08:00',
            'current_lat': 27.5, 'current_lng': 75.0, 'total_seats': 40, 'booked_seats': []},
        2: {'id': 2, 'bus_name': 'Semi Sleeper AC', 'route_name': 'बीकानेर → जोधपुर', 'departure_time': '10:30',
            'current_lat': 27.6, 'current_lng': 75.1, 'total_seats': 40, 'booked_seats': []},
        3: {'id': 3, 'bus_name': 'Deluxe AC', 'route_name': 'जयपुर → जोधपुर', 'departure_time': '07:30',
            'current_lat': 27.4, 'current_lng': 74.9, 'total_seats': 40, 'booked_seats': []}
    },
    'users': [
        {'id': 1, 'username': 'admin', 'password': 'admin123', 'role': 'admin'},
        {'id': 2, 'username': 'counter1', 'password': 'counter123', 'role': 'counter'},
        {'id': 3, 'username': 'driver1', 'password': 'driver123', 'role': 'driver'}
    ],
    'bookings': [],
    'gps_locations': {}
}


@atexit.register
def shutdown_pool():
    if DATABASE_CONNECTED:
        pool.close()


# ================= SIMPLE TEMPLATES =================
BASE_HTML = """
<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>बस बुकिंग सिस्टम</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: #f8f9fa;
            min-height: 100vh;
            color: #333;
        }
        .navbar {
            background: white;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        .hero {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 100px 20px;
            text-align: center;
            margin-top: 60px;
        }
        .search-box {
            background: white;
            padding: 30px;
            border-radius: 15px;
            max-width: 800px;
            margin: 30px auto;
            box-shadow: 0 10px 30px rgba(0,0,0,0.1);
        }
        .card {
            border-radius: 12px;
            border: none;
            box-shadow: 0 5px 15px rgba(0,0,0,0.08);
            margin-bottom: 20px;
        }
        .btn-primary {
            background: #0d6efd;
            border: none;
            padding: 10px 20px;
            border-radius: 8px;
        }
        .main-content {
            padding-top: 80px;
            padding-bottom: 50px;
        }
        #map {
            height: 400px;
            width: 100%;
            border-radius: 10px;
            z-index: 1;
        }
        .map-container {
            position: relative;
            margin: 20px 0;
        }
    </style>
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-light fixed-top">
        <div class="container">
            <a class="navbar-brand fw-bold" href="/">
                🚌 बस बुकिंग
            </a>
            <div class="navbar-nav ms-auto">
                {% if session.get('user_logged_in') %}
                    <a class="nav-link" href="/dashboard">डैशबोर्ड</a>
                    <a class="nav-link" href="/logout">लॉगआउट</a>
                {% else %}
                    <a class="nav-link" href="/login">लॉगिन</a>
                    <a class="nav-link" href="/counter">काउंटर</a>
                {% endif %}
                <a class="nav-link" href="/driver/1">ड्राइवर GPS</a>
            </div>
        </div>
    </nav>

    {% if not content %}
    <div class="hero">
        <div class="container">
            <h1 class="display-4 fw-bold mb-3">स्मार्ट बस बुकिंग</h1>
            <p class="lead mb-4">आसान, तेज और विश्वसनीय बस टिकट बुकिंग</p>

            <div class="search-box">
                <h3 class="mb-4">बस खोजें</h3>
                <form action="/search" method="POST" class="row g-3">
                    <div class="col-md-4">
                        <input type="text" name="from" class="form-control form-control-lg" placeholder="कहाँ से?" required>
                    </div>
                    <div class="col-md-4">
                        <input type="text" name="to" class="form-control form-control-lg" placeholder="कहाँ तक?" required>
                    </div>
                    <div class="col-md-3">
                        <input type="date" name="date" class="form-control form-control-lg" value="{{ date.today().isoformat() }}" required>
                    </div>
                    <div class="col-md-1">
                        <button type="submit" class="btn btn-primary btn-lg w-100">🔍</button>
                    </div>
                </form>
            </div>
        </div>
    </div>
    {% endif %}

    {% if content %}
    <div class="container main-content">
        {{ content|safe }}
    </div>
    {% endif %}

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

LOGIN_HTML = """
<div class="row justify-content-center">
    <div class="col-md-4">
        <div class="card">
            <div class="card-body p-4">
                <h3 class="text-center mb-4">लॉगिन</h3>
                <form method="POST">
                    <div class="mb-3">
                        <input type="text" name="username" class="form-control" placeholder="यूज़रनेम" required>
                    </div>
                    <div class="mb-3">
                        <input type="password" name="password" class="form-control" placeholder="पासवर्ड" required>
                    </div>
                    <button type="submit" class="btn btn-primary w-100">लॉगिन</button>
                </form>

                {% if error %}
                <div class="alert alert-danger mt-3 text-center">{{ error }}</div>
                {% endif %}

                <div class="mt-3 text-center">
                    <small class="text-muted">
                        डेमो यूज़र:<br>
                        admin / admin123<br>
                        counter1 / counter123<br>
                        driver1 / driver123
                    </small>
                </div>
            </div>
        </div>
    </div>
</div>
"""


# ================= ALL ROUTES =================

@app.route("/")
def home():
    """Home page"""
    return render_template_string(BASE_HTML, content=None)


@app.route("/login", methods=["GET", "POST"])
def login():
    """Login page"""
    error = None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if not username or not password:
            error = "कृपया यूज़रनेम और पासवर्ड दर्ज करें"
        else:
            # Check in-memory users
            user = next((u for u in in_memory_data['users']
                         if u['username'] == username and u['password'] == password), None)

            if user:
                session.clear()
                session["user_logged_in"] = True
                session["user_id"] = user.get('id', 1)
                session["username"] = user.get('username', username)
                session["role"] = user.get('role', 'user')
                return redirect("/dashboard")
            else:
                error = "गलत यूज़रनेम या पासवर्ड"

    return render_template_string(BASE_HTML, content=render_template_string(LOGIN_HTML, error=error))


@app.route("/counter", methods=["GET", "POST"])
def counter():
    """Counter login"""
    return login()  # Same as login page


@app.route("/dashboard")
def dashboard():
    """Dashboard page"""
    if not session.get("user_logged_in"):
        return redirect("/login")

    username = session.get("username", "User")
    role = session.get("role", "user")

    # Get stats
    total_buses = len(in_memory_data['buses'])
    total_bookings = len(in_memory_data['bookings'])

    admin_links = ""
    if role == "admin":
        admin_links = """
        <div class="mt-4">
            <a href="/create-counter" class="btn btn-info me-2">➕ Create Counter</a>
            <a href="/routes" class="btn btn-warning me-2">🛣️ Routes</a>
            <a href="/schedules" class="btn btn-success me-2">🚌 Schedules</a>
        </div>
        """

    content = f"""
    <div class="text-center">
        <h2>नमस्ते {username}! 🎉</h2>
        <h4>Role: <b>{role.upper()}</b></h4>

        <div class="row mt-4">
            <div class="col-md-3">
                <div class="card bg-primary text-white">
                    <div class="card-body">
                        <h5>कुल बसें</h5>
                        <h2>{total_buses}</h2>
                    </div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card bg-success text-white">
                    <div class="card-body">
                        <h5>कुल बुकिंग</h5>
                        <h2>{total_bookings}</h2>
                    </div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card bg-warning text-white">
                    <div class="card-body">
                        <h5>उपलब्ध सीटें</h5>
                        <h2>120</h2>
                    </div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card bg-info text-white">
                    <div class="card-body">
                        <h5>लाइव बसें</h5>
                        <h2>{total_buses}</h2>
                    </div>
                </div>
            </div>
        </div>

        <div class="mt-4">
            <a href="/" class="btn btn-primary">🏠 होम</a>
            <a href="/logout" class="btn btn-danger ms-2">🚪 लॉगआउट</a>
        </div>

        {admin_links}

        <div class="row mt-5">
            <div class="col-md-4">
                <div class="card">
                    <div class="card-body">
                        <h5>बस खोजें</h5>
                        <p>अपनी यात्रा के लिए बस ढूंढें</p>
                        <a href="/" class="btn btn-outline-primary">खोज शुरू करें</a>
                    </div>
                </div>
            </div>
            <div class="col-md-4">
                <div class="card">
                    <div class="card-body">
                        <h5>ड्राइवर GPS</h5>
                        <p>बस की लाइव लोकेशन ट्रैक करें</p>
                        <a href="/driver/1" class="btn btn-outline-success">ट्रैक करें</a>
                    </div>
                </div>
            </div>
            <div class="col-md-4">
                <div class="card">
                    <div class="card-body">
                        <h5>मेरी बुकिंग</h5>
                        <p>आपकी बुक की गई सीटें</p>
                        <a href="#" class="btn btn-outline-warning">देखें</a>
                    </div>
                </div>
            </div>
        </div>
    </div>
    """

    return render_template_string(BASE_HTML, content=content)


@app.route("/logout")
def logout():
    """Logout user"""
    session.clear()
    return redirect("/")


@app.route("/search", methods=["POST"])
def search():
    """Search buses"""
    from_station = request.form.get("from", "").strip()
    to_station = request.form.get("to", "").strip()
    travel_date = request.form.get("date", date.today().isoformat())

    # Store in session
    session["from"] = from_station
    session["to"] = to_station
    session["date"] = travel_date

    if not from_station or not to_station:
        error_html = """
        <div class="alert alert-danger text-center">
            <h4>त्रुटि</h4>
            <p>कृपया सभी फील्ड भरें</p>
            <a href="/" class="btn btn-primary mt-2">वापस जाएं</a>
        </div>
        """
        return render_template_string(BASE_HTML, content=error_html)

    # For demo, show all buses
    buses = list(in_memory_data['buses'].values())

    if not buses:
        no_buses_html = f"""
        <div class="alert alert-warning text-center">
            <h4>कोई बस नहीं मिली</h4>
            <p>{from_station} से {to_station} के लिए कोई बस उपलब्ध नहीं है</p>
            <a href="/" class="btn btn-primary mt-2">नई खोज</a>
        </div>
        """
        return render_template_string(BASE_HTML, content=no_buses_html)

    buses_html = "<h3 class='mb-4'>उपलब्ध बसें</h3>"

    for bus in buses:
        available_seats = bus['total_seats'] - len(bus['booked_seats'])
        has_gps = bus['current_lat'] is not None and bus['current_lng'] is not None

        buses_html += f"""
        <div class="card mb-3">
            <div class="card-body">
                <div class="row align-items-center">
                    <div class="col-md-8">
                        <h5>{bus['bus_name']}</h5>
                        <p class="mb-1"><strong>रूट:</strong> {bus['route_name']}</p>
                        <p class="mb-1"><strong>समय:</strong> {bus['departure_time']}</p>
                        <p class="mb-1"><strong>उपलब्ध सीटें:</strong> {available_seats}/{bus['total_seats']}</p>
                        <span class="badge {'bg-success' if has_gps else 'bg-secondary'}">
                            {'🟢 LIVE' if has_gps else '⚪ Offline'}
                        </span>
                    </div>
                    <div class="col-md-4 text-end">
                        <a href="/seats/{bus['id']}" class="btn btn-success btn-lg mb-2">सीट चुनें</a><br>
                        <a href="/driver/{bus['id']}" class="btn btn-primary">लाइव ट्रैकिंग</a>
                    </div>
                </div>
            </div>
        </div>
        """

    content = f"""
    <div class="container">
        <h2>खोज परिणाम</h2>
        <p class="text-muted mb-4">From: {from_station} → To: {to_station} | Date: {travel_date}</p>

        {buses_html}

        <div class="mt-4">
            <a href="/" class="btn btn-secondary">नई खोज</a>
        </div>
    </div>
    """

    return render_template_string(BASE_HTML, content=content)


@app.route("/buses/<int:bus_id>")
def buses(bus_id):
    """Show bus details"""
    bus = in_memory_data['buses'].get(bus_id)

    if not bus:
        return render_template_string(BASE_HTML, content="""
            <div class="alert alert-danger text-center">
                <h4>बस नहीं मिली</h4>
                <p>कृपया वैध बस ID दर्ज करें</p>
                <a href="/" class="btn btn-primary mt-2">वापस जाएं</a>
            </div>
        """)

    available_seats = bus['total_seats'] - len(bus['booked_seats'])

    content = f"""
    <div class="container">
        <h2>{bus['bus_name']}</h2>
        <p class="text-muted mb-4">{bus['route_name']}</p>

        <div class="row">
            <div class="col-md-6">
                <div class="card">
                    <div class="card-body">
                        <h5>बस विवरण</h5>
                        <p><strong>समय:</strong> {bus['departure_time']}</p>
                        <p><strong>कुल सीटें:</strong> {bus['total_seats']}</p>
                        <p><strong>उपलब्ध सीटें:</strong> {available_seats}</p>
                        <p><strong>स्थिति:</strong> {'🟢 चल रही है' if bus['current_lat'] else '⚪ बंद'}</p>
                    </div>
                </div>
            </div>
            <div class="col-md-6">
                <div class="card">
                    <div class="card-body">
                        <h5>कार्रवाई</h5>
                        <div class="d-grid gap-2">
                            <a href="/seats/{bus_id}" class="btn btn-success btn-lg">सीट चुनें</a>
                            <a href="/driver/{bus_id}" class="btn btn-primary">लाइव ट्रैकिंग</a>
                            <a href="/" class="btn btn-secondary">वापस जाएं</a>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    """

    return render_template_string(BASE_HTML, content=content)


@app.route("/seats/<int:bus_id>")
def seat_page(bus_id):
    """Seat selection page with map"""
    bus = in_memory_data['buses'].get(bus_id)

    if not bus:
        return render_template_string(BASE_HTML, content="""
            <div class="alert alert-danger text-center">
                <h4>बस नहीं मिली</h4>
                <a href="/" class="btn btn-primary mt-2">वापस जाएं</a>
            </div>
        """)

    # Generate seat layout
    seat_buttons = ""
    booked_seats = bus.get('booked_seats', [])

    for seat in range(1, 41):
        if seat in booked_seats:
            seat_buttons += f'<button class="btn btn-danger m-1" style="width: 60px;" disabled>X{seat}</button>'
        else:
            seat_buttons += f'<button class="btn btn-success m-1 seat-btn" style="width: 60px;" onclick="selectSeat({seat})">{seat}</button>'

    bus_lat = bus.get('current_lat', 27.5)
    bus_lng = bus.get('current_lng', 75.0)

    content = f"""
    <div class="container">
        <h2>{bus['bus_name']}</h2>
        <p class="text-muted">रूट: {bus['route_name']} | समय: {bus['departure_time']}</p>

        <div class="card mb-4">
            <div class="card-body">
                <h5>लाइव लोकेशन</h5>
                <div class="map-container">
                    <div id="map"></div>
                </div>
                <p class="mt-2"><small>बस की लाइव लोकेशन दिखाई जा रही है</small></p>
            </div>
        </div>

        <div class="alert alert-info">
            <h5>सीट चुनें</h5>
            <p>खाली सीट (हरी) पर क्लिक करके बुक करें</p>
        </div>

        <div class="mb-4">
            {seat_buttons}
        </div>

        <div id="bookingForm" style="display: none;" class="card p-4">
            <h5>यात्री विवरण</h5>
            <form id="passengerForm">
                <input type="hidden" id="selectedSeat">
                <div class="mb-3">
                    <label class="form-label">यात्री का नाम</label>
                    <input type="text" class="form-control" id="passengerName" required>
                </div>
                <div class="mb-3">
                    <label class="form-label">मोबाइल नंबर</label>
                    <input type="tel" class="form-control" id="mobileNumber" required>
                </div>
                <button type="button" class="btn btn-primary" onclick="bookSeat()">बुक करें</button>
            </form>
        </div>

        <div id="bookingResult" class="mt-3"></div>

        <div class="mt-4">
            <a href="/buses/{bus_id}" class="btn btn-secondary">वापस जाएं</a>
        </div>
    </div>

    <!-- Leaflet CSS & JS -->
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>

    <script>
    // Initialize map
    function initMap() {{
        const busLat = {bus_lat};
        const busLng = {bus_lng};

        // Create map instance
        const map = L.map('map').setView([busLat, busLng], 15);

        // Add tile layer
        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
            attribution: '© OpenStreetMap contributors',
            maxZoom: 19
        }}).addTo(map);

        // Add bus marker
        const busIcon = L.divIcon({{
            html: '<div style="background: #0d6efd; color: white; padding: 8px 12px; border-radius: 50%; font-weight: bold; border: 3px solid white; box-shadow: 0 0 10px rgba(0,0,0,0.3);">🚌</div>',
            className: 'bus-icon',
            iconSize: [50, 50]
        }});

        let busMarker = L.marker([busLat, busLng], {{icon: busIcon}})
            .addTo(map)
            .bindPopup('<b>{bus['bus_name']}</b><br>लाइव लोकेशन');

        // Store map and marker globally for updates
        window.busMap = map;
        window.busMarker = busMarker;

        console.log('Map initialized at:', busLat, busLng);
    }}

    // Socket for real-time updates
    const socket = io();
    const busId = {bus_id};

    socket.on('connect', () => {{
        console.log('Connected to server for bus tracking');
    }});

    socket.on('bus_location', (data) => {{
        if(data.sid == busId) {{
            console.log('Received location update:', data);

            // Update map if available
            if(window.busMarker) {{
                const lat = parseFloat(data.lat);
                const lng = parseFloat(data.lng);
                window.busMarker.setLatLng([lat, lng]);
                window.busMap.setView([lat, lng], window.busMap.getZoom());

                // Update popup
                window.busMarker.bindPopup('<b>{bus['bus_name']}</b><br>लाइव लोकेशन<br>गति: ' + data.speed + ' km/h').openPopup();
            }}
        }}
    }});

    // Initialize map when page loads
    document.addEventListener('DOMContentLoaded', initMap);

    // Seat selection functions
    let selectedSeat = null;

    function selectSeat(seat) {{
        selectedSeat = seat;
        document.getElementById('selectedSeat').value = seat;
        document.getElementById('bookingForm').style.display = 'block';
        document.getElementById('bookingResult').innerHTML = '';

        // Highlight selected seat
        document.querySelectorAll('.seat-btn').forEach(btn => {{
            btn.classList.remove('btn-primary');
            if(btn.textContent == seat) {{
                btn.classList.add('btn-primary');
            }}
        }});
    }}

    function bookSeat() {{
        const name = document.getElementById('passengerName').value;
        const mobile = document.getElementById('mobileNumber').value;

        if(!name || !mobile) {{
            alert('कृपया सभी विवरण भरें');
            return;
        }}

        fetch('/book', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{
                bus_id: {bus_id},
                seat_number: selectedSeat,
                passenger_name: name,
                mobile: mobile,
                date: new Date().toISOString().split('T')[0]
            }})
        }})
        .then(r => r.json())
        .then(res => {{
            if(res.ok) {{
                document.getElementById('bookingResult').innerHTML = 
                    '<div class="alert alert-success">सीट सफलतापूर्वक बुक हो गई! किराया: ₹' + res.fare + '</div>';
                // Disable the booked seat
                document.querySelectorAll('.seat-btn').forEach(btn => {{
                    if(btn.textContent == selectedSeat) {{
                        btn.disabled = true;
                        btn.classList.remove('btn-success', 'btn-primary');
                        btn.classList.add('btn-danger');
                        btn.innerHTML = 'X' + selectedSeat;
                    }}
                }});
                document.getElementById('bookingForm').style.display = 'none';

                // Emit seat update via socket
                socket.emit('seat_update', {{
                    sid: busId,
                    seat: selectedSeat,
                    date: new Date().toISOString().split('T')[0]
                }});
            }} else {{
                document.getElementById('bookingResult').innerHTML = 
                    '<div class="alert alert-danger">त्रुटि: ' + res.error + '</div>';
            }}
        }});
    }}
    </script>
    """

    return render_template_string(BASE_HTML, content=content)


@app.route("/driver/<int:bus_id>")
def driver(bus_id):
    """Driver GPS page with working map"""
    bus = in_memory_data['buses'].get(bus_id)

    if not bus:
        bus = {'bus_name': f'Bus {bus_id}', 'current_lat': 27.5, 'current_lng': 75.0}

    bus_lat = bus.get('current_lat', 27.5)
    bus_lng = bus.get('current_lng', 75.0)

    content = f"""
    <div class="container">
        <h2>🚗 Driver GPS – {bus['bus_name']}</h2>

        <div class="alert alert-info mb-4">
            <h5>निर्देश:</h5>
            <ul>
                <li>"GPS शुरू करें" बटन पर क्लिक करें</li>
                <li>ब्राउज़र को location access allow करें</li>
                <li>GPS background में भी काम करता रहेगा</li>
                <li>App को background में भी open रखें</li>
            </ul>
        </div>

        <div class="card mb-4">
            <div class="card-body">
                <h5>लाइव मैप</h5>
                <div id="map" style="height: 400px;"></div>
            </div>
        </div>

        <div class="row mb-4">
            <div class="col-md-6">
                <button id="startBtn" class="btn btn-success btn-lg w-100" onclick="startGPS()">
                    🚀 GPS शुरू करें
                </button>
            </div>
            <div class="col-md-6">
                <button id="stopBtn" class="btn btn-danger btn-lg w-100" onclick="stopGPS()" disabled>
                    🛑 GPS बंद करें
                </button>
            </div>
        </div>

        <div id="status" class="card p-3 mb-3">
            <h5>स्थिति: <span id="statusText">बंद</span></h5>
        </div>

        <div id="locationInfo" class="card p-3">
            <h5>लोकेशन विवरण</h5>
            <div class="row">
                <div class="col-md-6">
                    <p><strong>अक्षांश:</strong> <span id="lat">{bus_lat}</span></p>
                    <p><strong>देशांतर:</strong> <span id="lng">{bus_lng}</span></p>
                </div>
                <div class="col-md-6">
                    <p><strong>गति:</strong> <span id="speed">0 km/h</span></p>
                    <p><strong>अंतिम अपडेट:</strong> <span id="lastUpdate">-</span></p>
                </div>
            </div>
        </div>

        <div class="mt-4">
            <a href="/dashboard" class="btn btn-secondary">डैशबोर्ड</a>
            <a href="/" class="btn btn-primary ms-2">होम</a>
        </div>
    </div>

    <!-- Leaflet CSS & JS -->
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>

    <style>
    #map {{
        width: 100%;
        height: 400px;
        border-radius: 10px;
    }}
    .leaflet-container {{
        font-family: inherit;
    }}
    </style>

    <script>
    const socket = io();
    const busId = {bus_id};

    let watchId = null;
    let map = null;
    let marker = null;

    // Initialize map
    function initMap() {{
        const busLat = {bus_lat};
        const busLng = {bus_lng};

        // Create map
        map = L.map('map').setView([busLat, busLng], 15);

        // Add tile layer
        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
            attribution: '© OpenStreetMap contributors',
            maxZoom: 19
        }}).addTo(map);

        // Add initial marker
        const busIcon = L.divIcon({{
            html: '<div style="background: #198754; color: white; padding: 10px 15px; border-radius: 50%; font-weight: bold; border: 3px solid white; box-shadow: 0 0 15px rgba(0,0,0,0.4);">🚌</div>',
            className: 'bus-icon',
            iconSize: [60, 60]
        }});

        marker = L.marker([busLat, busLng], {{icon: busIcon}})
            .addTo(map)
            .bindPopup('<b>{bus['bus_name']}</b><br>ड्राइवर GPS');

        console.log('Driver map initialized');
    }}

    // Start GPS tracking
    function startGPS() {{
        if (!navigator.geolocation) {{
            alert('इस ब्राउज़र में GPS सपोर्ट नहीं है');
            return;
        }}

        const options = {{
            enableHighAccuracy: true,
            maximumAge: 0,
            timeout: 10000
        }};

        watchId = navigator.geolocation.watchPosition(
            (position) => {{
                const lat = position.coords.latitude;
                const lng = position.coords.longitude;
                const speed = position.coords.speed || 0;

                // Update UI
                document.getElementById('statusText').textContent = 'चालू';
                document.getElementById('status').className = 'card p-3 mb-3 bg-success text-white';
                document.getElementById('lat').textContent = lat.toFixed(6);
                document.getElementById('lng').textContent = lng.toFixed(6);
                document.getElementById('speed').textContent = (speed * 3.6).toFixed(1) + ' km/h';
                document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();

                // Update map
                if (map && marker) {{
                    marker.setLatLng([lat, lng]);
                    map.setView([lat, lng], map.getZoom());
                    marker.bindPopup('<b>{bus['bus_name']}</b><br>लाइव GPS<br>गति: ' + (speed * 3.6).toFixed(1) + ' km/h').openPopup();
                }}

                // Send to server
                socket.emit('driver_gps', {{
                    sid: busId,
                    lat: lat,
                    lng: lng,
                    speed: speed * 3.6,
                    timestamp: new Date().toISOString()
                }});

                // Update buttons
                document.getElementById('startBtn').disabled = true;
                document.getElementById('stopBtn').disabled = false;
            }},
            (error) => {{
                document.getElementById('statusText').textContent = 'त्रुटि: ' + error.message;
                document.getElementById('status').className = 'card p-3 mb-3 bg-danger text-white';
                console.error('GPS Error:', error);
            }},
            options
        );
    }}

    // Stop GPS tracking
    function stopGPS() {{
        if (watchId) {{
            navigator.geolocation.clearWatch(watchId);
            watchId = null;
        }}

        document.getElementById('statusText').textContent = 'बंद';
        document.getElementById('status').className = 'card p-3 mb-3 bg-secondary text-white';
        document.getElementById('startBtn').disabled = false;
        document.getElementById('stopBtn').disabled = true;
    }}

    // Initialize map when page loads
    document.addEventListener('DOMContentLoaded', initMap);

    // Listen for location updates from other drivers
    socket.on('bus_location', (data) => {{
        if(data.sid == busId) {{
            // Update UI if not actively tracking
            if(!watchId) {{
                document.getElementById('lat').textContent = parseFloat(data.lat).toFixed(6);
                document.getElementById('lng').textContent = parseFloat(data.lng).toFixed(6);
                document.getElementById('speed').textContent = data.speed + ' km/h';
                document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();

                // Update map
                if (map && marker) {{
                    marker.setLatLng([data.lat, data.lng]);
                    marker.bindPopup('<b>{bus['bus_name']}</b><br>लाइव अपडेट<br>गति: ' + data.speed + ' km/h');
                }}
            }}
        }}
    }});

    // App state detection for background GPS
    document.addEventListener('visibilitychange', () => {{
        const appState = document.hidden ? 'background' : 'foreground';
        console.log('App state changed to:', appState);
    }});
    </script>
    """

    return render_template_string(BASE_HTML, content=content)


@app.route("/live-bus/<int:bus_id>")
def live_bus(bus_id):
    """Live bus tracking with map"""
    bus = in_memory_data['buses'].get(bus_id)
    if not bus:
        bus = {'bus_name': f'Bus {bus_id}', 'current_lat': 27.5, 'current_lng': 75.0}

    bus_lat = bus.get('current_lat', 27.5)
    bus_lng = bus.get('current_lng', 75.0)

    content = f"""
    <div class="container">
        <h2>🚌 Live Tracking - {bus['bus_name']}</h2>

        <div class="card mb-4">
            <div class="card-body">
                <h5>लाइव लोकेशन मैप</h5>
                <div id="map" style="height: 500px; width: 100%;"></div>
            </div>
        </div>

        <div class="row">
            <div class="col-md-6">
                <div class="card">
                    <div class="card-body">
                        <h5>बस विवरण</h5>
                        <p><strong>बस नाम:</strong> {bus['bus_name']}</p>
                        <p><strong>अक्षांश:</strong> <span id="busLat">{bus_lat}</span></p>
                        <p><strong>देशांतर:</strong> <span id="busLng">{bus_lng}</span></p>
                        <p><strong>गति:</strong> <span id="busSpeed">0 km/h</span></p>
                        <p><strong>अंतिम अपडेट:</strong> <span id="busUpdate">-</span></p>
                    </div>
                </div>
            </div>

            <div class="col-md-6">
                <div class="card">
                    <div class="card-body">
                        <h5>कंट्रोल्स</h5>
                        <div class="d-grid gap-2">
                            <a href="/driver/{bus_id}" class="btn btn-success">ड्राइवर मोड</a>
                            <a href="/seats/{bus_id}" class="btn btn-primary">सीट बुक करें</a>
                            <a href="/" class="btn btn-secondary">होम</a>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Leaflet CSS & JS -->
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>

    <script>
    const socket = io();
    const busId = {bus_id};

    let map = null;
    let marker = null;
    let routeLine = null;

    // Initialize map
    function initMap() {{
        const busLat = {bus_lat};
        const busLng = {bus_lng};

        // Create map
        map = L.map('map').setView([busLat, busLng], 13);

        // Add tile layer
        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
            attribution: '© OpenStreetMap contributors',
            maxZoom: 19
        }}).addTo(map);

        // Add bus marker with custom icon
        const busIcon = L.divIcon({{
            html: '<div style="background: #dc3545; color: white; padding: 12px 18px; border-radius: 50%; font-size: 20px; border: 4px solid white; box-shadow: 0 0 20px rgba(0,0,0,0.5);">🚌</div>',
            className: 'bus-icon',
            iconSize: [70, 70]
        }});

        marker = L.marker([busLat, busLng], {{icon: busIcon}})
            .addTo(map)
            .bindPopup('<b>{bus['bus_name']}</b><br>लाइव ट्रैकिंग')
            .openPopup();

        // Add sample route line (for demo)
        const routePoints = [
            [27.5, 75.0],
            [27.6, 75.1],
            [27.7, 75.2],
            [27.8, 75.3]
        ];

        routeLine = L.polyline(routePoints, {{
            color: '#0d6efd',
            weight: 4,
            opacity: 0.7,
            dashArray: '10, 10'
        }}).addTo(map);

        console.log('Live tracking map initialized');
    }}

    // Listen for location updates
    socket.on('bus_location', (data) => {{
        if(data.sid == busId) {{
            const lat = parseFloat(data.lat);
            const lng = parseFloat(data.lng);

            // Update UI
            document.getElementById('busLat').textContent = lat.toFixed(6);
            document.getElementById('busLng').textContent = lng.toFixed(6);
            document.getElementById('busSpeed').textContent = data.speed + ' km/h';
            document.getElementById('busUpdate').textContent = new Date().toLocaleTimeString();

            // Update map
            if (map && marker) {{
                marker.setLatLng([lat, lng]);
                map.panTo([lat, lng]);
                marker.bindPopup('<b>{bus['bus_name']}</b><br>लाइव ट्रैकिंग<br>गति: ' + data.speed + ' km/h').openPopup();

                // Add to route line
                if(routeLine) {{
                    const currentLatLngs = routeLine.getLatLngs();
                    currentLatLngs.push([lat, lng]);
                    routeLine.setLatLngs(currentLatLngs);
                }}
            }}
        }}
    }});

    // Initialize map when page loads
    document.addEventListener('DOMContentLoaded', initMap);

    // Auto-refresh location every 10 seconds
    setInterval(() => {{
        if(map && marker) {{
            // Just to keep map active
            console.log('Live tracking active');
        }}
    }}, 10000);
    </script>
    """

    return render_template_string(BASE_HTML, content=content)


@app.route("/book", methods=["POST"])
def book():
    """Book a seat"""
    try:
        data = request.get_json()
        bus_id = data.get('bus_id')
        seat_number = data.get('seat_number')
        passenger_name = data.get('passenger_name')
        mobile = data.get('mobile')

        # Check if seat is available
        bus = in_memory_data['buses'].get(bus_id)
        if not bus:
            return jsonify({"ok": False, "error": "Bus not found"})

        if seat_number in bus.get('booked_seats', []):
            return jsonify({"ok": False, "error": "Seat already booked"})

        # Generate random fare
        fare = random.randint(200, 500)

        # Book the seat
        if 'booked_seats' not in bus:
            bus['booked_seats'] = []
        bus['booked_seats'].append(seat_number)

        # Add to bookings
        in_memory_data['bookings'].append({
            'bus_id': bus_id,
            'seat_number': seat_number,
            'passenger_name': passenger_name,
            'mobile': mobile,
            'date': data.get('date'),
            'fare': fare,
            'booking_time': datetime.now().isoformat()
        })

        # Emit socket event
        socketio.emit("seat_update", {
            "sid": bus_id,
            "seat": seat_number,
            "date": data.get('date')
        })

        return jsonify({"ok": True, "fare": fare})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


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

    print(f"📍 GPS Update: Bus-{sid} @ [{lat:.5f},{lng:.5f}] {speed}km/h")

    # Update bus location
    if sid in in_memory_data['buses']:
        in_memory_data['buses'][sid]['current_lat'] = lat
        in_memory_data['buses'][sid]['current_lng'] = lng
        in_memory_data['gps_locations'][sid] = {
            'lat': lat,
            'lng': lng,
            'speed': speed,
            'timestamp': datetime.now().isoformat()
        }

    # Emit to all clients
    socketio.emit("bus_location", {
        "sid": sid,
        "lat": lat,
        "lng": lng,
        "speed": speed,
        "timestamp": data.get('timestamp', datetime.now().isoformat())
    })


# ================= ERROR HANDLERS =================
@app.errorhandler(404)
def page_not_found(e):
    return render_template_string(BASE_HTML, content="""
        <div class="text-center mt-5">
            <h1>404</h1>
            <p class="lead">पेज नहीं मिला</p>
            <a href="/" class="btn btn-primary">होमपेज पर जाएं</a>
        </div>
    """), 404


@app.errorhandler(500)
def internal_server_error(e):
    return render_template_string(BASE_HTML, content="""
        <div class="alert alert-danger">
            <h4>सर्वर त्रुटि</h4>
            <p>कृपया बाद में पुनः प्रयास करें</p>
            <a href="/" class="btn btn-primary mt-2">होमपेज</a>
        </div>
    """), 500


# ================= MAIN =================
if __name__ == "__main__":
    print("🚀 Bus Booking System Starting...")
    print("🌐 Server will run on: http://localhost:10000")
    print("📍 GPS Background Tracking: Enabled")
    print("🗺️ Maps: Working with Leaflet")
    print("\nAvailable Routes with Maps:")
    print("  /seats/<id>    - Seat selection with bus location map")
    print("  /driver/<id>   - Driver GPS with live tracking map")
    print("  /live-bus/<id> - Live bus tracking with route map")

    socketio.run(app,
                 host="0.0.0.0",
                 port=int(os.environ.get("PORT", 10000)),
                 debug=True,
                 allow_unsafe_werkzeug=True)