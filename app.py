from dotenv import load_dotenv
import json

load_dotenv()
import os, random
from datetime import date, datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify, render_template_string, redirect, g, session
from flask_socketio import SocketIO, emit
from flask_compress import Compress
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
import atexit
import razorpay
import threading
import time

# ===== INITIALIZATION =====
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key-change-this")
Compress(app)

# SocketIO Configuration
socketio = SocketIO(app,
                    cors_allowed_origins="*",
                    async_mode="threading",
                    ping_timeout=60,
                    ping_interval=25)

# ===== DATABASE SETUP =====
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise Exception("DATABASE_URL environment variable is missing!")

pool = ConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=10, timeout=20)
print("✅ Connection pool ready")

# ===== GLOBAL VARIABLES =====
gps_backup_store = {}
gps_last_update = {}

# ===== TEMPLATES =====
BASE_HTML = """
<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>बस बुकिंग सिस्टम</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        }
        .navbar {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            box-shadow: 0 4px 20px rgba(0,0,0,0.1);
        }
        .main-container {
            padding-top: 80px;
            padding-bottom: 40px;
        }
        .card {
            border-radius: 20px;
            border: none;
            box-shadow: 0 10px 30px rgba(0,0,0,0.1);
            transition: transform 0.3s;
        }
        .card:hover {
            transform: translateY(-5px);
        }
        .btn-primary {
            background: linear-gradient(45deg, #667eea, #764ba2);
            border: none;
            border-radius: 10px;
            padding: 12px 30px;
            font-weight: 600;
        }
        .footer {
            background: rgba(0,0,0,0.8);
            color: white;
            padding: 20px 0;
            margin-top: 40px;
        }
    </style>
</head>
<body>
    <!-- Navbar -->
    <nav class="navbar navbar-expand-lg navbar-light fixed-top">
        <div class="container">
            <a class="navbar-brand fw-bold" href="/">
                🚌 बस बुकिंग सिस्टम
            </a>
            <div class="navbar-nav ms-auto">
                {% if session.get('user_logged_in') %}
                    <a class="nav-link" href="/dashboard">डैशबोर्ड</a>
                    <a class="nav-link" href="/logout">लॉगआउट</a>
                {% else %}
                    <a class="nav-link" href="/login">लॉगिन</a>
                    <a class="nav-link" href="/counter">काउंटर</a>
                {% endif %}
            </div>
        </div>
    </nav>

    <!-- Main Content -->
    <div class="container main-container">
        {% if content %}
            {{ content|safe }}
        {% else %}
        <!-- Hero Section -->
        <div class="row align-items-center" style="min-height: 70vh;">
            <div class="col-md-6 text-white">
                <h1 class="display-4 fw-bold mb-4">स्मार्ट बस बुकिंग</h1>
                <p class="lead mb-4">आसान, तेज और विश्वसनीय बस टिकट बुकिंग</p>
                <div class="d-flex gap-3">
                    <a href="#search" class="btn btn-primary btn-lg">बस खोजें</a>
                    <a href="/driver/1" class="btn btn-outline-light btn-lg">ड्राइवर GPS</a>
                </div>
            </div>
            <div class="col-md-6">
                <img src="https://cdn.pixabay.com/photo/2016/11/22/19/17/buildings-1850129_1280.jpg" 
                     class="img-fluid rounded-3 shadow-lg" alt="Bus">
            </div>
        </div>

        <!-- Search Form -->
        <div id="search" class="card mt-5 p-4">
            <h3 class="text-center mb-4">बस खोजें</h3>
            <form action="/search" method="POST" class="row g-3">
                <div class="col-md-3">
                    <input type="text" name="from" class="form-control form-control-lg" 
                           placeholder="कहाँ से?" required>
                </div>
                <div class="col-md-3">
                    <input type="text" name="to" class="form-control form-control-lg" 
                           placeholder="कहाँ तक?" required>
                </div>
                <div class="col-md-3">
                    <input type="date" name="date" class="form-control form-control-lg" 
                           value="{{ date.today().isoformat() }}" required>
                </div>
                <div class="col-md-3">
                    <button type="submit" class="btn btn-primary btn-lg w-100">
                        🔍 बस खोजें
                    </button>
                </div>
            </form>
        </div>
        {% endif %}
    </div>

    <!-- Footer -->
    <div class="footer">
        <div class="container text-center">
            <p>© 2024 बस बुकिंग सिस्टम. सभी अधिकार सुरक्षित।</p>
        </div>
    </div>

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
                        <label class="form-label">यूज़रनेम</label>
                        <input type="text" name="username" class="form-control" required>
                    </div>
                    <div class="mb-3">
                        <label class="form-label">पासवर्ड</label>
                        <input type="password" name="password" class="form-control" required>
                    </div>
                    <button type="submit" class="btn btn-primary w-100">लॉगिन</button>
                </form>
                {% if error %}
                <div class="alert alert-danger mt-3">
                    {{ error }}
                </div>
                {% endif %}
            </div>
        </div>
    </div>
</div>
"""


# ===== DATABASE HELPER FUNCTIONS =====
def get_db():
    """Get database connection"""
    if 'db' not in g:
        g.db = pool.getconn()
        g.db_cursor = g.db.cursor(row_factory=dict_row)
    return g.db, g.db_cursor


@app.teardown_appcontext
def close_db(e=None):
    """Close database connection"""
    db = g.pop('db', None)
    cursor = g.pop('db_cursor', None)

    if cursor is not None:
        cursor.close()
    if db is not None:
        pool.putconn(db)


def safe_db(func):
    """Decorator for safe database operations"""

    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            print(f"Database error: {e}")
            raise
        finally:
            close_db()

    return wrapper


def admin_required(f):
    """Decorator for admin authorization"""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('user_logged_in'):
            return redirect('/login')
        if session.get('role') != 'admin':
            return "Access Denied", 403
        return f(*args, **kwargs)

    return decorated_function


# ===== INITIALIZE DATABASE =====
@safe_db
def init_db():
    """Initialize database tables"""
    conn, cur = get_db()

    # Create tables if not exists
    tables = [
        """
        CREATE TABLE IF NOT EXISTS admins (
            id SERIAL PRIMARY KEY,
            username VARCHAR(50) UNIQUE NOT NULL,
            password VARCHAR(100) NOT NULL,
            role VARCHAR(20) DEFAULT 'admin',
            counter_no INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS routes (
            id SERIAL PRIMARY KEY,
            route_name VARCHAR(100) UNIQUE NOT NULL,
            distance_km INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS schedules (
            id SERIAL PRIMARY KEY,
            route_id INTEGER REFERENCES routes(id),
            bus_name VARCHAR(100) NOT NULL,
            departure_time TIME NOT NULL,
            current_lat DOUBLE PRECISION,
            current_lng DOUBLE PRECISION,
            last_gps_update TIMESTAMP,
            total_seats INTEGER DEFAULT 40,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS seat_bookings (
            id SERIAL PRIMARY KEY,
            schedule_id INTEGER REFERENCES schedules(id),
            seat_number INTEGER NOT NULL,
            passenger_name VARCHAR(100) NOT NULL,
            mobile VARCHAR(15) NOT NULL,
            from_station VARCHAR(50),
            to_station VARCHAR(50),
            travel_date DATE NOT NULL,
            status VARCHAR(20) DEFAULT 'confirmed',
            fare INTEGER NOT NULL,
            payment_mode VARCHAR(10) DEFAULT 'cash',
            booked_by_type VARCHAR(10) DEFAULT 'user',
            booked_by_id INTEGER,
            counter_id INTEGER,
            order_id VARCHAR(100),
            payment_id VARCHAR(100),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS route_stations (
            id SERIAL PRIMARY KEY,
            route_id INTEGER REFERENCES routes(id),
            station_name VARCHAR(50) NOT NULL,
            station_order INTEGER NOT NULL,
            lat DOUBLE PRECISION DEFAULT 27.2,
            lng DOUBLE PRECISION DEFAULT 75.2,
            UNIQUE(route_id, station_order)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS gps_backup (
            id SERIAL PRIMARY KEY,
            bus_id INTEGER NOT NULL,
            lat DOUBLE PRECISION NOT NULL,
            lng DOUBLE PRECISION NOT NULL,
            speed DOUBLE PRECISION DEFAULT 0,
            accuracy DOUBLE PRECISION DEFAULT 50,
            source VARCHAR(50),
            app_state VARCHAR(20) DEFAULT 'foreground',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    ]

    for table_sql in tables:
        cur.execute(table_sql)

    # Insert default admin if not exists
    cur.execute("SELECT COUNT(*) as count FROM admins WHERE username = 'admin'")
    if cur.fetchone()['count'] == 0:
        cur.execute(
            "INSERT INTO admins (username, password, role) VALUES (%s, %s, %s)",
            ('admin', 'admin123', 'admin')
        )

    # Insert sample routes if not exists
    cur.execute("SELECT COUNT(*) as count FROM routes")
    if cur.fetchone()['count'] == 0:
        sample_routes = [
            ('बीकानेर → जयपुर', 336),
            ('बीकानेर → जोधपुर', 252),
            ('जयपुर → जोधपुर', 330)
        ]
        for route in sample_routes:
            cur.execute(
                "INSERT INTO routes (route_name, distance_km) VALUES (%s, %s)",
                route
            )

    conn.commit()
    print("✅ Database initialized successfully")


# ===== ROUTES =====

@app.route('/')
def home():
    """Home page"""
    return render_template_string(BASE_HTML, content=None)


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page"""
    error = None

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        try:
            conn, cur = get_db()
            cur.execute(
                "SELECT id, role FROM admins WHERE username = %s AND password = %s",
                (username, password)
            )
            user = cur.fetchone()

            if user:
                session.clear()
                session['user_logged_in'] = True
                session['user_id'] = user['id']
                session['role'] = user['role']
                return redirect('/dashboard')
            else:
                error = "गलत यूज़रनेम या पासवर्ड"
        except Exception as e:
            error = f"सर्वर त्रुटि: {str(e)}"

    return render_template_string(BASE_HTML, content=render_template_string(LOGIN_HTML, error=error))


@app.route('/dashboard')
def dashboard():
    """Dashboard page"""
    if not session.get('user_logged_in'):
        return redirect('/login')

    role = session.get('role', 'user')

    # Get stats for dashboard
    try:
        conn, cur = get_db()

        # Total bookings
        cur.execute("SELECT COUNT(*) as total FROM seat_bookings")
        total_bookings = cur.fetchone()['total']

        # Today's bookings
        cur.execute("SELECT COUNT(*) as today FROM seat_bookings WHERE DATE(created_at) = CURRENT_DATE")
        today_bookings = cur.fetchone()['today']

        # Total routes
        cur.execute("SELECT COUNT(*) as routes FROM routes")
        total_routes = cur.fetchone()['routes']

        # Active buses
        cur.execute("SELECT COUNT(*) as buses FROM schedules")
        total_buses = cur.fetchone()['buses']

    except Exception as e:
        total_bookings = today_bookings = total_routes = total_buses = 0
        print(f"Dashboard stats error: {e}")

    dashboard_html = f"""
    <div class="row">
        <div class="col-12">
            <h2>डैशबोर्ड</h2>
            <p class="text-muted">रोल: <strong>{role.upper()}</strong></p>
        </div>
    </div>

    <div class="row mt-4">
        <div class="col-md-3">
            <div class="card text-white bg-primary">
                <div class="card-body">
                    <h5 class="card-title">कुल बुकिंग</h5>
                    <h2>{total_bookings}</h2>
                </div>
            </div>
        </div>
        <div class="col-md-3">
            <div class="card text-white bg-success">
                <div class="card-body">
                    <h5 class="card-title">आज की बुकिंग</h5>
                    <h2>{today_bookings}</h2>
                </div>
            </div>
        </div>
        <div class="col-md-3">
            <div class="card text-white bg-warning">
                <div class="card-body">
                    <h5 class="card-title">रूट्स</h5>
                    <h2>{total_routes}</h2>
                </div>
            </div>
        </div>
        <div class="col-md-3">
            <div class="card text-white bg-info">
                <div class="card-body">
                    <h5 class="card-title">बसें</h5>
                    <h2>{total_buses}</h2>
                </div>
            </div>
        </div>
    </div>

    <div class="row mt-4">
        <div class="col-12">
            <div class="card">
                <div class="card-body">
                    <h5 class="card-title">त्वरित कार्य</h5>
                    <div class="d-flex flex-wrap gap-2 mt-3">
                        <a href="/routes" class="btn btn-outline-primary">रूट्स प्रबंधित करें</a>
                        <a href="/schedules" class="btn btn-outline-success">शेड्यूल प्रबंधित करें</a>
                        <a href="/bookings" class="btn btn-outline-warning">बुकिंग्स देखें</a>
                        <a href="/create-counter" class="btn btn-outline-info">काउंटर बनाएं</a>
                        <a href="/driver/1" class="btn btn-outline-danger">ड्राइवर GPS</a>
                    </div>
                </div>
            </div>
        </div>
    </div>
    """

    return render_template_string(BASE_HTML, content=dashboard_html)


@app.route('/logout')
def logout():
    """Logout user"""
    session.clear()
    return redirect('/')


@app.route('/counter', methods=['GET', 'POST'])
def counter():
    """Counter login"""
    error = None

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        try:
            conn, cur = get_db()
            cur.execute(
                "SELECT id, role FROM admins WHERE username = %s AND password = %s",
                (username, password)
            )
            user = cur.fetchone()

            if user:
                session.clear()
                session['user_logged_in'] = True
                session['user_id'] = user['id']
                session['role'] = user['role']
                return redirect('/dashboard')
            else:
                error = "गलत यूज़रनेम या पासवर्ड"
        except Exception as e:
            error = f"सर्वर त्रुटि: {str(e)}"

    return render_template_string(BASE_HTML, content=render_template_string(LOGIN_HTML, error=error))


@app.route('/search', methods=['POST'])
def search():
    """Search buses"""
    from_station = request.form.get('from', '').strip()
    to_station = request.form.get('to', '').strip()
    travel_date = request.form.get('date', date.today().isoformat())

    # Store in session
    session['from'] = from_station
    session['to'] = to_station
    session['date'] = travel_date

    if not from_station or not to_station:
        return render_template_string(BASE_HTML, content="<div class='alert alert-danger'>कृपया सभी फील्ड भरें</div>")

    try:
        conn, cur = get_db()

        # Find routes containing both stations
        cur.execute("""
            SELECT r.* FROM routes r
            WHERE EXISTS (
                SELECT 1 FROM route_stations rs1 
                WHERE rs1.route_id = r.id AND LOWER(rs1.station_name) LIKE LOWER(%s)
            ) AND EXISTS (
                SELECT 1 FROM route_stations rs2 
                WHERE rs2.route_id = r.id AND LOWER(rs2.station_name) LIKE LOWER(%s)
            )
        """, (f'%{from_station}%', f'%{to_station}%'))

        routes = cur.fetchall()

        if not routes:
            return render_template_string(BASE_HTML, content="""
                <div class='alert alert-warning'>
                    <h4>कोई बस नहीं मिली</h4>
                    <p>{from_station} से {to_station} के लिए आज कोई बस उपलब्ध नहीं है।</p>
                </div>
            """.format(from_station=from_station, to_station=to_station))

        # Show routes
        routes_html = "<h3>उपलब्ध रूट्स</h3><div class='row'>"
        for route in routes:
            routes_html += f"""
            <div class='col-md-4 mb-3'>
                <div class='card h-100'>
                    <div class='card-body'>
                        <h5 class='card-title'>{route['route_name']}</h5>
                        <p class='card-text'>दूरी: {route['distance_km']} किमी</p>
                        <a href='/buses/{route['id']}' class='btn btn-primary'>बसें देखें</a>
                    </div>
                </div>
            </div>
            """
        routes_html += "</div>"

        return render_template_string(BASE_HTML, content=routes_html)

    except Exception as e:
        return render_template_string(BASE_HTML, content=f"<div class='alert alert-danger'>त्रुटि: {str(e)}</div>")


@app.route('/buses/<int:route_id>')
def buses(route_id):
    """Show buses for a route"""
    try:
        conn, cur = get_db()

        # Get route details
        cur.execute("SELECT * FROM routes WHERE id = %s", (route_id,))
        route = cur.fetchone()

        if not route:
            return render_template_string(BASE_HTML, content="<div class='alert alert-danger'>रूट नहीं मिला</div>")

        # Get stations for this route
        cur.execute("SELECT station_name FROM route_stations WHERE route_id = %s ORDER BY station_order", (route_id,))
        stations = cur.fetchall()
        station_list = " → ".join([s['station_name'] for s in stations])

        # Get buses/schedules for this route
        cur.execute("""
            SELECT s.*, 
                   (SELECT COUNT(*) FROM seat_bookings b 
                    WHERE b.schedule_id = s.id AND b.travel_date = %s) as booked_seats
            FROM schedules s
            WHERE s.route_id = %s
            ORDER BY s.departure_time
        """, (session.get('date', date.today().isoformat()), route_id))

        buses = cur.fetchall()

        buses_html = f"""
        <div class="card">
            <div class="card-body">
                <h3>{route['route_name']}</h3>
                <p class="text-muted">{station_list}</p>
                <p><strong>दूरी:</strong> {route['distance_km']} किमी</p>

                <div class="mt-4">
                    <h4>उपलब्ध बसें</h4>
                    {"<p>कोई बस उपलब्ध नहीं है</p>" if not buses else ""}
        """

        for bus in buses:
            available_seats = bus['total_seats'] - bus['booked_seats']
            buses_html += f"""
            <div class="card mb-3">
                <div class="card-body">
                    <div class="row">
                        <div class="col-md-6">
                            <h5>{bus['bus_name']}</h5>
                            <p><strong>समय:</strong> {bus['departure_time']}</p>
                            <p><strong>उपलब्ध सीटें:</strong> {available_seats}/{bus['total_seats']}</p>
                        </div>
                        <div class="col-md-6 text-end">
                            <a href="/seats/{bus['id']}" class="btn btn-primary btn-lg">सीट चुनें</a>
                            <a href="/driver/{bus['id']}" class="btn btn-outline-secondary mt-2">लाइव ट्रैकिंग</a>
                        </div>
                    </div>
                </div>
            </div>
            """

        buses_html += """
                </div>
            </div>
        </div>
        """

        return render_template_string(BASE_HTML, content=buses_html)

    except Exception as e:
        return render_template_string(BASE_HTML, content=f"<div class='alert alert-danger'>त्रुटि: {str(e)}</div>")


@app.route('/seats/<int:schedule_id>')
def seats(schedule_id):
    """Seat selection page"""
    try:
        conn, cur = get_db()

        # Get schedule details
        cur.execute("""
            SELECT s.*, r.route_name 
            FROM schedules s 
            JOIN routes r ON s.route_id = r.id 
            WHERE s.id = %s
        """, (schedule_id,))

        schedule = cur.fetchone()

        if not schedule:
            return render_template_string(BASE_HTML, content="<div class='alert alert-danger'>शेड्यूल नहीं मिला</div>")

        # Get booked seats for today
        travel_date = session.get('date', date.today().isoformat())
        cur.execute("""
            SELECT seat_number FROM seat_bookings 
            WHERE schedule_id = %s AND travel_date = %s AND status = 'confirmed'
        """, (schedule_id, travel_date))

        booked_seats = [row['seat_number'] for row in cur.fetchall()]

        # Generate seat layout
        seats_html = "<div class='row'><div class='col-12'><h4>सीट चुनें</h4></div></div>"
        seats_html += "<div class='row mt-3'>"

        for seat in range(1, 41):
            if seat in booked_seats:
                seat_class = "btn-danger disabled"
                seat_text = f"X{seat}"
            else:
                seat_class = "btn-success"
                seat_text = str(seat)

            seats_html += f"""
            <div class='col-3 col-md-2 mb-2'>
                <button class='btn {seat_class} w-100 seat-btn' 
                        data-seat='{seat}' 
                        {'disabled' if seat in booked_seats else ''}
                        onclick='selectSeat({seat})'>
                    {seat_text}
                </button>
            </div>
            """

            if seat % 4 == 0:
                seats_html += "<div class='w-100'></div>"

        seats_html += "</div>"

        # Full page content
        content = f"""
        <div class="card">
            <div class="card-body">
                <h3>{schedule['bus_name']}</h3>
                <p><strong>रूट:</strong> {schedule['route_name']}</p>
                <p><strong>समय:</strong> {schedule['departure_time']}</p>
                <p><strong>तारीख:</strong> {travel_date}</p>

                <hr>

                {seats_html}

                <div id="bookingForm" class="mt-4" style="display:none;">
                    <h5>यात्री विवरण</h5>
                    <form id="passengerForm" onsubmit="bookSeat(event)">
                        <input type="hidden" id="selectedSeat">
                        <div class="mb-3">
                            <label class="form-label">यात्री का नाम</label>
                            <input type="text" class="form-control" id="passengerName" required>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">मोबाइल नंबर</label>
                            <input type="tel" class="form-control" id="mobileNumber" required>
                        </div>
                        <button type="submit" class="btn btn-primary">बुक करें</button>
                    </form>
                </div>

                <div id="bookingResult" class="mt-3"></div>
            </div>
        </div>

        <script>
        let selectedSeat = null;

        function selectSeat(seat) {{
            selectedSeat = seat;
            document.getElementById('selectedSeat').value = seat;
            document.getElementById('bookingForm').style.display = 'block';
            document.getElementById('bookingResult').innerHTML = '';

            // Highlight selected seat
            document.querySelectorAll('.seat-btn').forEach(btn => {{
                btn.classList.remove('btn-primary');
                if(btn.dataset.seat == seat) {{
                    btn.classList.add('btn-primary');
                }}
            }});
        }}

        function bookSeat(event) {{
            event.preventDefault();

            const passengerName = document.getElementById('passengerName').value;
            const mobileNumber = document.getElementById('mobileNumber').value;

            fetch('/book', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{
                    schedule_id: {schedule_id},
                    seat_number: selectedSeat,
                    passenger_name: passengerName,
                    mobile: mobileNumber,
                    date: '{travel_date}'
                }})
            }})
            .then(response => response.json())
            .then(data => {{
                if(data.ok) {{
                    document.getElementById('bookingResult').innerHTML = 
                        '<div class="alert alert-success">सीट सफलतापूर्वक बुक हो गई!</div>';
                    // Disable the booked seat
                    document.querySelector(`button[data-seat="${{selectedSeat}}"]`).disabled = true;
                    document.querySelector(`button[data-seat="${{selectedSeat}}"]`).classList.remove('btn-success', 'btn-primary');
                    document.querySelector(`button[data-seat="${{selectedSeat}}"]`).classList.add('btn-danger');
                    document.querySelector(`button[data-seat="${{selectedSeat}}"]`).innerHTML = 'X' + selectedSeat;
                    // Hide form
                    document.getElementById('bookingForm').style.display = 'none';
                }} else {{
                    document.getElementById('bookingResult').innerHTML = 
                        '<div class="alert alert-danger">त्रुटि: ' + data.error + '</div>';
                }}
            }})
            .catch(error => {{
                document.getElementById('bookingResult').innerHTML = 
                    '<div class="alert alert-danger">नेटवर्क त्रुटि</div>';
            }});
        }}
        </script>
        """

        return render_template_string(BASE_HTML, content=content)

    except Exception as e:
        return render_template_string(BASE_HTML, content=f"<div class='alert alert-danger'>त्रुटि: {str(e)}</div>")


@app.route('/book', methods=['POST'])
def book():
    """Book a seat"""
    try:
        data = request.get_json()

        conn, cur = get_db()

        # Check if seat is already booked
        cur.execute("""
            SELECT id FROM seat_bookings 
            WHERE schedule_id = %s AND seat_number = %s AND travel_date = %s AND status = 'confirmed'
        """, (data['schedule_id'], data['seat_number'], data['date']))

        if cur.fetchone():
            return jsonify({'ok': False, 'error': 'यह सीट पहले से बुक है'})

        # Generate random fare
        fare = random.randint(200, 500)

        # Get user info from session
        user_id = session.get('user_id', 0)
        user_role = session.get('role', 'user')
        from_station = session.get('from', '')
        to_station = session.get('to', '')

        # Insert booking
        cur.execute("""
            INSERT INTO seat_bookings 
            (schedule_id, seat_number, passenger_name, mobile, from_station, to_station, 
             travel_date, fare, booked_by_id, booked_by_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            data['schedule_id'], data['seat_number'], data['passenger_name'], data['mobile'],
            from_station, to_station, data['date'], fare, user_id, user_role
        ))

        conn.commit()

        # Emit socket event for real-time update
        socketio.emit('seat_update', {
            'sid': data['schedule_id'],
            'seat': data['seat_number'],
            'date': data['date']
        })

        return jsonify({'ok': True, 'fare': fare})

    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


# ===== GPS RELATED ROUTES =====

@app.route('/driver/<int:bus_id>')
def driver_page(bus_id):
    """Driver GPS page"""
    driver_html = f"""
    <div class="card">
        <div class="card-body">
            <h3>🚌 बस {bus_id} - GPS ट्रैकिंग</h3>

            <div class="alert alert-info">
                <h5>निर्देश:</h5>
                <ul>
                    <li>GPS शुरू करने के लिए नीचे दिए बटन पर क्लिक करें</li>
                    <li>अपने ब्राउज़र में "Background Location" permission allow करें</li>
                    <li>GPS background में भी काम करता रहेगा</li>
                </ul>
            </div>

            <div class="row mt-4">
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

            <div id="status" class="mt-4 p-3 bg-light rounded">
                <h5>स्थिति: <span id="statusText">बंद</span></h5>
            </div>

            <div id="locationInfo" class="mt-4" style="display:none;">
                <h5>लोकेशन विवरण</h5>
                <div class="row">
                    <div class="col-md-6">
                        <p><strong>अक्षांश:</strong> <span id="lat">-</span></p>
                        <p><strong>देशांतर:</strong> <span id="lng">-</span></p>
                    </div>
                    <div class="col-md-6">
                        <p><strong>गति:</strong> <span id="speed">0 km/h</span></p>
                        <p><strong>अंतिम अपडेट:</strong> <span id="lastUpdate">-</span></p>
                    </div>
                </div>
            </div>

            <div class="mt-4">
                <button class="btn btn-outline-primary" onclick="requestBackgroundPermission()">
                    ⚙️ Background Permission सेटिंग
                </button>
            </div>
        </div>
    </div>

    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <script>
    const socket = io();
    const busId = {bus_id};

    let watchId = null;
    let lastLocation = null;
    let appState = document.hidden ? 'background' : 'foreground';

    socket.on('connect', () => {{
        console.log('Connected to server');
    }});

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
                document.getElementById('status').className = 'mt-4 p-3 bg-success text-white rounded';
                document.getElementById('locationInfo').style.display = 'block';
                document.getElementById('lat').textContent = lat.toFixed(6);
                document.getElementById('lng').textContent = lng.toFixed(6);
                document.getElementById('speed').textContent = (speed * 3.6).toFixed(1) + ' km/h';
                document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();

                // Send to server
                socket.emit('driver_gps', {{
                    sid: busId,
                    lat: lat,
                    lng: lng,
                    speed: speed * 3.6,
                    app_state: appState,
                    timestamp: new Date().toISOString()
                }});

                lastLocation = {{ lat, lng, speed }};

                // Update buttons
                document.getElementById('startBtn').disabled = true;
                document.getElementById('stopBtn').disabled = false;
            }},
            (error) => {{
                document.getElementById('statusText').textContent = 'त्रुटि: ' + error.message;
                document.getElementById('status').className = 'mt-4 p-3 bg-danger text-white rounded';
            }},
            options
        );
    }}

    function stopGPS() {{
        if (watchId) {{
            navigator.geolocation.clearWatch(watchId);
            watchId = null;
        }}

        document.getElementById('statusText').textContent = 'बंद';
        document.getElementById('status').className = 'mt-4 p-3 bg-light rounded';
        document.getElementById('startBtn').disabled = false;
        document.getElementById('stopBtn').disabled = true;
        document.getElementById('locationInfo').style.display = 'none';
    }}

    function requestBackgroundPermission() {{
        if ('permissions' in navigator) {{
            navigator.permissions.query({{name: 'geolocation'}})
                .then(permissionStatus => {{
                    console.log('Location permission:', permissionStatus.state);
                }});
        }}
        alert('कृपया ब्राउज़र सेटिंग में "Background Location" permission allow करें');
    }}

    // Detect app state changes
    document.addEventListener('visibilitychange', () => {{
        appState = document.hidden ? 'background' : 'foreground';
        console.log('App state changed to:', appState);
    }});

    // Keep connection alive
    setInterval(() => {{
        fetch('/heartbeat');
    }}, 30000);
    </script>
    """

    return render_template_string(BASE_HTML, content=driver_html)


# ===== SOCKET EVENTS =====

@socketio.on('connect')
def handle_connect():
    print(f"✅ Client connected: {request.sid}")


@socketio.on('driver_gps')
def handle_gps(data):
    """Handle GPS data from driver"""
    try:
        sid = data.get('sid')
        lat = float(data.get('lat', 27.5))
        lng = float(data.get('lng', 75.0))
        speed = float(data.get('speed', 0))
        app_state = data.get('app_state', 'foreground')

        print(f"📍 GPS Update - Bus {sid}: [{lat:.6f}, {lng:.6f}] {speed}km/h ({app_state})")

        # Update in database
        conn, cur = get_db()

        # Update schedule
        cur.execute("""
            UPDATE schedules 
            SET current_lat = %s, current_lng = %s, last_gps_update = NOW()
            WHERE id = %s
        """, (lat, lng, sid))

        # Store in GPS backup if in background
        if app_state == 'background':
            cur.execute("""
                INSERT INTO gps_backup (bus_id, lat, lng, speed, app_state, source)
                VALUES (%s, %s, %s, %s, %s, 'driver_app')
            """, (sid, lat, lng, speed, app_state))

        conn.commit()

        # Store in memory cache
        key = f"bus_{sid}"
        gps_backup_store[key] = {
            'lat': lat,
            'lng': lng,
            'speed': speed,
            'timestamp': time.time(),
            'app_state': app_state
        }
        gps_last_update[key] = time.time()

        # Broadcast to all clients
        emit('bus_location', {
            'sid': sid,
            'lat': lat,
            'lng': lng,
            'speed': speed,
            'timestamp': datetime.now().isoformat()
        }, broadcast=True)

    except Exception as e:
        print(f"GPS processing error: {e}")


@socketio.on('app_state_change')
def handle_app_state(data):
    """Handle app state changes"""
    sid = data.get('sid')
    state = data.get('state', 'foreground')
    print(f"📱 App state changed - Bus {sid}: {state}")


# ===== API ENDPOINTS =====

@app.route('/heartbeat')
def heartbeat():
    """Keep connection alive"""
    return jsonify({'status': 'alive', 'timestamp': datetime.now().isoformat()})


@app.route('/api/last-location/<int:bus_id>')
def last_location(bus_id):
    """Get last known location of bus"""
    try:
        # Check memory cache first
        key = f"bus_{bus_id}"
        if key in gps_backup_store:
            data = gps_backup_store[key]
            return jsonify({
                'ok': True,
                'lat': data['lat'],
                'lng': data['lng'],
                'speed': data['speed'],
                'timestamp': datetime.fromtimestamp(data['timestamp']).isoformat(),
                'source': 'cache'
            })

        # Check database
        conn, cur = get_db()
        cur.execute("""
            SELECT current_lat as lat, current_lng as lng, last_gps_update
            FROM schedules WHERE id = %s AND current_lat IS NOT NULL
        """, (bus_id,))

        schedule = cur.fetchone()
        if schedule:
            return jsonify({
                'ok': True,
                'lat': schedule['lat'],
                'lng': schedule['lng'],
                'timestamp': schedule['last_gps_update'].isoformat() if schedule['last_gps_update'] else None,
                'source': 'database'
            })

        return jsonify({'ok': False, 'message': 'No location data available'})

    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/gps-backup', methods=['POST'])
def gps_backup():
    """Backup GPS data from mobile app"""
    try:
        data = request.get_json()

        conn, cur = get_db()
        cur.execute("""
            INSERT INTO gps_backup (bus_id, lat, lng, speed, source, app_state)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            data.get('bus_id'),
            data.get('lat'),
            data.get('lng'),
            data.get('speed', 0),
            data.get('source', 'mobile_app'),
            data.get('app_state', 'background')
        ))

        conn.commit()
        return jsonify({'ok': True})

    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


# ===== ADMIN ROUTES =====

@app.route('/routes')
@admin_required
def manage_routes():
    """Manage routes"""
    try:
        conn, cur = get_db()
        cur.execute("SELECT * FROM routes ORDER BY id")
        routes = cur.fetchall()

        routes_html = "<h3>रूट्स प्रबंधन</h3>"
        routes_html += "<table class='table table-striped'><thead><tr><th>ID</th><th>रूट नाम</th><th>दूरी (किमी)</th><th>कार्रवाई</th></tr></thead><tbody>"

        for route in routes:
            routes_html += f"""
            <tr>
                <td>{route['id']}</td>
                <td>{route['route_name']}</td>
                <td>{route['distance_km']}</td>
                <td>
                    <button class='btn btn-sm btn-primary'>संपादित करें</button>
                    <button class='btn btn-sm btn-danger'>हटाएं</button>
                </td>
            </tr>
            """

        routes_html += "</tbody></table>"

        return render_template_string(BASE_HTML, content=routes_html)

    except Exception as e:
        return render_template_string(BASE_HTML, content=f"<div class='alert alert-danger'>त्रुटि: {str(e)}</div>")


@app.route('/create-counter', methods=['GET', 'POST'])
@admin_required
def create_counter():
    """Create counter account"""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if not username or not password:
            return render_template_string(BASE_HTML,
                                          content="<div class='alert alert-danger'>कृपया सभी फील्ड भरें</div>")

        try:
            conn, cur = get_db()
            cur.execute(
                "INSERT INTO admins (username, password, role) VALUES (%s, %s, 'counter')",
                (username, password)
            )
            conn.commit()

            return render_template_string(BASE_HTML, content=f"""
                <div class='alert alert-success'>
                    काउंटर '{username}' सफलतापूर्वक बनाया गया
                </div>
            """)

        except Exception as e:
            return render_template_string(BASE_HTML, content=f"<div class='alert alert-danger'>त्रुटि: {str(e)}</div>")

    # GET request - show form
    form_html = """
    <div class="row justify-content-center">
        <div class="col-md-6">
            <div class="card">
                <div class="card-body">
                    <h3>नया काउंटर बनाएं</h3>
                    <form method="POST">
                        <div class="mb-3">
                            <label class="form-label">यूज़रनेम</label>
                            <input type="text" name="username" class="form-control" required>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">पासवर्ड</label>
                            <input type="password" name="password" class="form-control" required>
                        </div>
                        <button type="submit" class="btn btn-primary w-100">काउंटर बनाएं</button>
                    </form>
                </div>
            </div>
        </div>
    </div>
    """

    return render_template_string(BASE_HTML, content=form_html)


# ===== ERROR HANDLERS =====

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
        </div>
    """), 500


# ===== MAIN APPLICATION =====

if __name__ == '__main__':
    # Initialize database
    with app.app_context():
        init_db()

    print("🚀 बस बुकिंग सिस्टम शुरू हो रहा है...")
    print("🌐 सर्वर चल रहा है: http://localhost:10000")
    print("📍 GPS बैकग्राउंड ट्रैकिंग सक्षम है")

    socketio.run(app,
                 host='0.0.0.0',
                 port=int(os.environ.get('PORT', 10000)),
                 debug=True,
                 allow_unsafe_werkzeug=True)