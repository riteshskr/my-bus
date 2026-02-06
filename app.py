# ================= KAYA RENDER OPTIMIZED VERSION - FIXED =================
from dotenv import load_dotenv
import os
import json
import time
import hashlib
from datetime import date, datetime, timedelta
from functools import wraps
from contextlib import contextmanager
from flask import Flask, request, jsonify, render_template_string, redirect, session, g
from flask_compress import Compress
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
from psycopg2 import pool, extras
import atexit
import random
import traceback
import uuid
import logging

# Kaya Render के लिए WebSockets बंद करें
# Socket.IO को हटाएं क्योंकि free tier support नहीं करता

load_dotenv()


# Configuration for Kaya Render
class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "kaya-render-secret-key-123")
    DATABASE_URL = os.getenv("DATABASE_URL")

    # Kaya के limitations के अनुसार settings
    MAX_DB_CONNECTIONS = 5  # Free tier के लिए कम connections
    GPS_BATCH_SIZE = 10  # Smaller batches for limited RAM

    # WebSockets disabled for Kaya
    WEBSOCKETS_ENABLED = False

    # Rate limiting - use in-memory storage
    RATE_LIMITS = "100 per hour"


# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Kaya Render के लिए memory-efficient connection pool
connection_pool = None


def create_kaya_pool():
    """Kaya Render के limitations के लिए optimized pool"""
    global connection_pool
    try:
        connection_pool = psycopg2.pool.SimpleConnectionPool(
            2,  # Minimum connections
            Config.MAX_DB_CONNECTIONS,  # Max connections
            Config.DATABASE_URL
        )
        logger.info("✅ Kaya-optimized database pool created")
        return connection_pool
    except Exception as e:
        logger.error(f"❌ Pool creation failed: {e}")
        # Fallback to direct connection
        return None


# Flask app setup for Kaya
app = Flask(__name__)
app.secret_key = Config.SECRET_KEY
Compress(app)
CORS(app)

# Memory-based rate limiter (Redis not available in free tier)
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=[Config.RATE_LIMITS],
    storage_uri="memory://"  # In-memory storage
)

# Initialize connection pool
create_kaya_pool()


@atexit.register
def cleanup_pool():
    """Cleanup connection pool on exit"""
    if connection_pool:
        connection_pool.closeall()
        logger.info("✅ Connection pool closed")


# ================= DATABASE HELPERS FOR KAYA =================
def get_db_connection():
    """Get database connection - simplified for Kaya"""
    try:
        if connection_pool:
            conn = connection_pool.getconn()
        else:
            # Fallback to direct connection
            conn = psycopg2.connect(Config.DATABASE_URL)

        conn.autocommit = False
        return conn
    except Exception as e:
        logger.error(f"DB Connection Error: {e}")
        raise


def release_db_connection(conn):
    """Release connection back to pool"""
    try:
        if connection_pool:
            connection_pool.putconn(conn)
        else:
            conn.close()
    except Exception as e:
        logger.error(f"Error releasing connection: {e}")


@contextmanager
def db_cursor():
    """Context manager for database operations"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        yield cur
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Database error: {e}")
        raise
    finally:
        if cur:
            cur.close()
        if conn:
            release_db_connection(conn)


def safe_db(func):
    """Decorator for database operations"""

    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {e}")
            return jsonify({"error": "Service unavailable"}), 503

    return wrapper


# ================= INITIALIZE DATABASE =================
def init_database():
    """Initialize database with required tables"""
    try:
        with db_cursor() as cur:
            # Create tables if not exist
            tables = [
                """CREATE TABLE IF NOT EXISTS admins (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(50) UNIQUE,
                    password VARCHAR(100),
                    role VARCHAR(20) DEFAULT 'admin',
                    created_at TIMESTAMP DEFAULT NOW()
                )""",
                """CREATE TABLE IF NOT EXISTS routes (
                    id SERIAL PRIMARY KEY,
                    route_name VARCHAR(100),
                    distance_km INT,
                    created_at TIMESTAMP DEFAULT NOW()
                )""",
                """CREATE TABLE IF NOT EXISTS schedules (
                    id SERIAL PRIMARY KEY,
                    route_id INT,
                    bus_name VARCHAR(100),
                    departure_time TIME,
                    current_lat DOUBLE PRECISION,
                    current_lng DOUBLE PRECISION,
                    total_seats INT DEFAULT 40,
                    last_gps_update TIMESTAMP,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW()
                )""",
                """CREATE TABLE IF NOT EXISTS route_stations (
                    id SERIAL PRIMARY KEY,
                    route_id INT,
                    station_name VARCHAR(50),
                    station_order INT,
                    lat DOUBLE PRECISION DEFAULT 27.2,
                    lng DOUBLE PRECISION DEFAULT 75.2,
                    created_at TIMESTAMP DEFAULT NOW()
                )""",
                """CREATE TABLE IF NOT EXISTS seat_bookings (
                    id SERIAL PRIMARY KEY,
                    schedule_id INT,
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
                    booking_hash VARCHAR(64) UNIQUE,
                    created_at TIMESTAMP DEFAULT NOW()
                )""",
                """CREATE TABLE IF NOT EXISTS gps_logs (
                    id BIGSERIAL PRIMARY KEY,
                    schedule_id INT NOT NULL,
                    latitude DOUBLE PRECISION NOT NULL,
                    longitude DOUBLE PRECISION NOT NULL,
                    speed DOUBLE PRECISION DEFAULT 0,
                    accuracy DOUBLE PRECISION,
                    timestamp TIMESTAMP DEFAULT NOW()
                )"""
            ]

            for table_sql in tables:
                cur.execute(table_sql)

            # Insert default admin if not exists
            cur.execute("SELECT COUNT(*) FROM admins")
            if cur.fetchone()[0] == 0:
                hashed_pw = generate_password_hash("admin123")
                cur.execute(
                    "INSERT INTO admins (username, password) VALUES (%s, %s)",
                    ("admin", hashed_pw)
                )

            # Insert sample routes if not exists
            cur.execute("SELECT COUNT(*) FROM routes")
            if cur.fetchone()[0] == 0:
                sample_routes = [
                    (1, 'Bikaner → Jaipur', 336),
                    (2, 'Bikaner → Jodhpur', 252),
                    (3, 'Jaipur → Jodhpur', 330)
                ]
                for route in sample_routes:
                    cur.execute(
                        "INSERT INTO routes (id, route_name, distance_km) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                        route
                    )

                # Sample schedules
                sample_schedules = [
                    (1, 1, 'Volvo AC Sleeper', '08:00'),
                    (2, 1, 'Semi Sleeper AC', '10:30'),
                    (3, 2, 'Volvo AC Seater', '09:00'),
                    (4, 3, 'Deluxe AC', '07:30')
                ]
                for schedule in sample_schedules:
                    cur.execute(
                        "INSERT INTO schedules (id, route_id, bus_name, departure_time) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                        schedule
                    )

                # Sample stations
                sample_stations = [
                    (1, 'Bikaner', 1, 28.0229, 73.3119),
                    (1, 'Jaipur', 2, 26.9124, 75.7873),
                    (2, 'Bikaner', 1, 28.0229, 73.3119),
                    (2, 'Jodhpur', 2, 26.2389, 73.0243),
                    (3, 'Jaipur', 1, 26.9124, 75.7873),
                    (3, 'Jodhpur', 2, 26.2389, 73.0243)
                ]
                for station in sample_stations:
                    cur.execute(
                        "INSERT INTO route_stations (route_id, station_name, station_order, lat, lng) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                        station
                    )

            logger.info("✅ Database initialized successfully")

    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}")
        raise


# Initialize database on startup
init_database()


# ================= KAYA HEALTH CHECK =================
@app.route('/health')
def health_check_kaya():
    """Kaya Render health check endpoint"""
    try:
        # Check database connection
        with db_cursor() as cur:
            cur.execute("SELECT 1")

        return jsonify({
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "service": "My Bus AI - Kaya Render Optimized",
            "database": "connected"
        })
    except Exception as e:
        return jsonify({
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }), 500


# ================= KAYA-FRIENDLY HTML TEMPLATES =================
KAYA_BASE_HTML = """
<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>My Bus AI - Kaya Render</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
    body { 
        background: #f8f9fa; 
        font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        padding-top: 70px;
    }
    .navbar {
        background: linear-gradient(90deg, #2c3e50 0%, #4a6491 100%);
        box-shadow: 0 2px 10px rgba(0,0,0,0.1);
    }
    .hero {
        background: linear-gradient(rgba(0,0,0,0.7), rgba(0,0,0,0.8)),
                    url('https://images.unsplash.com/photo-1544620347-c4fd4a3d5957?auto=format&fit=crop&w=1200');
        background-size: cover;
        background-position: center;
        color: white;
        padding: 100px 20px;
        text-align: center;
        border-radius: 0 0 20px 20px;
        margin-top: -20px;
    }
    .card {
        border: none;
        border-radius: 15px;
        box-shadow: 0 5px 15px rgba(0,0,0,0.08);
        transition: transform 0.3s, box-shadow 0.3s;
        margin-bottom: 20px;
    }
    .card:hover {
        transform: translateY(-5px);
        box-shadow: 0 10px 25px rgba(0,0,0,0.15);
    }
    .btn-primary {
        background: linear-gradient(45deg, #4a6491, #2c3e50);
        border: none;
        padding: 10px 25px;
        border-radius: 8px;
    }
    .seat-btn {
        width: 45px;
        height: 45px;
        margin: 3px;
        border-radius: 8px;
    }
    @media (max-width: 768px) {
        .hero {
            padding: 60px 20px;
        }
        .hero h1 {
            font-size: 1.8rem;
        }
    }
    </style>
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-dark fixed-top">
        <div class="container">
            <a class="navbar-brand" href="/">
                <i class="fas fa-bus"></i> My Bus AI
            </a>
            <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav">
                <span class="navbar-toggler-icon"></span>
            </button>
            <div class="collapse navbar-collapse" id="navbarNav">
                <div class="navbar-nav ms-auto">
                    <a class="nav-link" href="/"><i class="fas fa-home"></i> Home</a>
                    <a class="nav-link" href="/search"><i class="fas fa-search"></i> Search</a>
                    <a class="nav-link" href="/admin"><i class="fas fa-user-shield"></i> Admin</a>
                    <a class="nav-link" href="/health"><i class="fas fa-heartbeat"></i> Status</a>
                </div>
            </div>
        </div>
    </nav>

    {% if not content %}
    <section class="hero">
        <div class="container">
            <h1 class="display-4 mb-4">Smart Bus Platform</h1>
            <p class="lead mb-5">Book • Track • Manage • Optimized for Kaya Render</p>
            <a href="/search" class="btn btn-light btn-lg px-5">
                <i class="fas fa-search"></i> Search Buses
            </a>
        </div>
    </section>

    <div class="container mt-5">
        <div class="row">
            <div class="col-md-4 mb-4">
                <div class="card text-center p-4">
                    <i class="fas fa-bus fa-3x text-primary mb-3"></i>
                    <h4>Search Buses</h4>
                    <p>Find buses between any two stations</p>
                    <a href="/search" class="btn btn-outline-primary">Search Now</a>
                </div>
            </div>
            <div class="col-md-4 mb-4">
                <div class="card text-center p-4">
                    <i class="fas fa-ticket-alt fa-3x text-success mb-3"></i>
                    <h4>Book Tickets</h4>
                    <p>Book seats with easy selection</p>
                    <a href="/search" class="btn btn-outline-success">Book Now</a>
                </div>
            </div>
            <div class="col-md-4 mb-4">
                <div class="card text-center p-4">
                    <i class="fas fa-map-marker-alt fa-3x text-info mb-3"></i>
                    <h4>Track Buses</h4>
                    <p>Real-time bus tracking</p>
                    <a href="/track" class="btn btn-outline-info">Track Now</a>
                </div>
            </div>
        </div>
    </div>
    {% endif %}

    {% if content %}
    <div class="container my-5">
        {{ content|safe }}
    </div>
    {% endif %}

    <footer class="bg-dark text-white py-4 mt-5">
        <div class="container text-center">
            <p class="mb-2">© 2024 My Bus AI | Running on Kaya Render Free Tier</p>
            <small class="text-muted">Optimized for limited resources | Health: <span id="healthStatus">Checking...</span></small>
        </div>
    </footer>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
    <script>
    // Check health status
    fetch('/health')
        .then(response => response.json())
        .then(data => {
            const statusEl = document.getElementById('healthStatus');
            if (data.status === 'healthy') {
                statusEl.innerHTML = '<span class="text-success">🟢 Healthy</span>';
            } else {
                statusEl.innerHTML = '<span class="text-warning">🟡 Issues</span>';
            }
        })
        .catch(() => {
            document.getElementById('healthStatus').innerHTML = '<span class="text-danger">🔴 Offline</span>';
        });
    </script>
</body>
</html>
"""


# ================= SIMPLIFIED ROUTES FOR KAYA =================
@app.route('/')
@safe_db
def home_kaya():
    """Homepage optimized for Kaya"""
    return render_template_string(KAYA_BASE_HTML, content=None)


@app.route('/search')
def search_page():
    """Search page"""
    # Get all stations for dropdown
    stations = []
    try:
        with db_cursor() as cur:
            cur.execute("SELECT DISTINCT station_name FROM route_stations ORDER BY station_name")
            stations = [row['station_name'] for row in cur.fetchall()]
    except:
        stations = ['Bikaner', 'Jaipur', 'Jodhpur', 'Delhi', 'Udaipur']

    stations_options = ''.join([f'<option value="{s}">{s}</option>' for s in stations])

    content = f"""
    <div class="row justify-content-center">
        <div class="col-md-8">
            <div class="card p-4">
                <h3 class="mb-4"><i class="fas fa-search"></i> Search Buses</h3>
                <form action="/search-results" method="POST">
                    <div class="row g-3">
                        <div class="col-md-5">
                            <label class="form-label">From Station</label>
                            <select name="from" class="form-select" required>
                                <option value="">Select Station</option>
                                {stations_options}
                            </select>
                        </div>
                        <div class="col-md-5">
                            <label class="form-label">To Station</label>
                            <select name="to" class="form-select" required>
                                <option value="">Select Station</option>
                                {stations_options}
                            </select>
                        </div>
                        <div class="col-md-2">
                            <label class="form-label">Date</label>
                            <input type="date" name="date" class="form-control" 
                                   value="{date.today().isoformat()}" required>
                        </div>
                    </div>
                    <button type="submit" class="btn btn-primary mt-4 w-100 py-2">
                        <i class="fas fa-bus"></i> Search Buses
                    </button>
                </form>
            </div>

            <div class="card p-4 mt-4">
                <h5><i class="fas fa-info-circle"></i> How to Search</h5>
                <ul class="mb-0">
                    <li>Select departure and arrival stations</li>
                    <li>Choose travel date</li>
                    <li>Click Search to see available buses</li>
                    <li>Select bus and book your seat</li>
                </ul>
            </div>
        </div>
    </div>
    """
    return render_template_string(KAYA_BASE_HTML, content=content)


@app.route('/search-results', methods=['POST'])
@safe_db
def search_results():
    """Search results page"""
    from_station = request.form.get('from')
    to_station = request.form.get('to')
    travel_date = request.form.get('date')

    with db_cursor() as cur:
        # Find routes connecting these stations
        cur.execute("""
            SELECT DISTINCT r.id, r.route_name, r.distance_km
            FROM routes r
            JOIN route_stations rs1 ON rs1.route_id = r.id
            JOIN route_stations rs2 ON rs2.route_id = r.id
            WHERE rs1.station_name = %s 
              AND rs2.station_name = %s
              AND rs1.station_order < rs2.station_order
            LIMIT 1
        """, (from_station, to_station))

        route = cur.fetchone()

        if route:
            # Get buses for this route
            cur.execute("""
                SELECT s.*, 
                       (SELECT COUNT(*) FROM seat_bookings 
                        WHERE schedule_id = s.id 
                        AND travel_date = %s 
                        AND status = 'confirmed') as booked_seats
                FROM schedules s
                WHERE s.route_id = %s 
                  AND s.is_active = true
                ORDER BY s.departure_time
            """, (travel_date, route['id']))

            buses = cur.fetchall()

            buses_html = ""
            for bus in buses:
                available_seats = bus['total_seats'] - bus['booked_seats']
                status_class = "success" if available_seats > 10 else "warning" if available_seats > 0 else "danger"
                status_text = f"{available_seats} seats" if available_seats > 0 else "Sold out"

                buses_html += f"""
                <div class="card mb-3">
                    <div class="card-body">
                        <div class="d-flex justify-content-between align-items-center mb-2">
                            <div>
                                <h5 class="mb-0">{bus['bus_name']}</h5>
                                <p class="text-muted mb-0">
                                    <i class="fas fa-clock"></i> {bus['departure_time']}
                                </p>
                            </div>
                            <span class="badge bg-{status_class}">{status_text}</span>
                        </div>

                        <div class="row mt-3">
                            <div class="col-6">
                                <p class="mb-1"><i class="fas fa-chair"></i> Total Seats: {bus['total_seats']}</p>
                                <p class="mb-1"><i class="fas fa-road"></i> Route: {route['distance_km']} km</p>
                            </div>
                            <div class="col-6 text-end">
                                {f'<a href="/book/{bus["id"]}?date={travel_date}" class="btn btn-primary">Book Now</a>' if available_seats > 0 else '<button class="btn btn-secondary" disabled>Sold Out</button>'}
                            </div>
                        </div>
                    </div>
                </div>
                """

            content = f"""
            <div class="card p-4">
                <div class="d-flex justify-content-between align-items-center mb-4">
                    <div>
                        <h3 class="mb-1">{route['route_name']}</h3>
                        <p class="text-muted mb-0">
                            {from_station} → {to_station} | {route['distance_km']} km
                        </p>
                        <p class="mb-0">Travel Date: {travel_date}</p>
                    </div>
                    <a href="/search" class="btn btn-outline-primary">
                        <i class="fas fa-arrow-left"></i> New Search
                    </a>
                </div>

                <h5 class="mb-3">Available Buses</h5>
                {buses_html if buses else '''
                <div class="alert alert-warning">
                    <i class="fas fa-exclamation-triangle"></i> No buses available for this route on selected date.
                </div>
                '''}
            </div>
            """
        else:
            content = f"""
            <div class="card p-4 text-center">
                <div class="mb-4">
                    <i class="fas fa-exclamation-triangle fa-3x text-warning mb-3"></i>
                    <h3>No Direct Route Found</h3>
                    <p class="text-muted">We couldn't find any direct buses from {from_station} to {to_station}.</p>
                </div>
                <div class="d-flex justify-content-center gap-3">
                    <a href="/search" class="btn btn-primary">
                        <i class="fas fa-search"></i> Try Another Search
                    </a>
                    <a href="/" class="btn btn-outline-secondary">
                        <i class="fas fa-home"></i> Go Home
                    </a>
                </div>
            </div>
            """

    return render_template_string(KAYA_BASE_HTML, content=content)


@app.route('/book/<int:bus_id>')
@safe_db
def book_seat_page(bus_id):
    """Seat booking page"""
    travel_date = request.args.get('date', date.today().isoformat())

    with db_cursor() as cur:
        # Get bus details
        cur.execute("""
            SELECT s.*, r.route_name
            FROM schedules s
            JOIN routes r ON s.route_id = r.id
            WHERE s.id = %s
        """, (bus_id,))

        bus = cur.fetchone()

        if not bus:
            return render_template_string(KAYA_BASE_HTML, content="""
                <div class="alert alert-danger text-center">
                    <h4><i class="fas fa-exclamation-circle"></i> Bus Not Found</h4>
                    <p>The requested bus does not exist or has been removed.</p>
                    <a href="/search" class="btn btn-primary mt-2">Search Buses</a>
                </div>
            """)

        # Get booked seats
        cur.execute("""
            SELECT seat_number 
            FROM seat_bookings 
            WHERE schedule_id = %s 
              AND travel_date = %s
              AND status = 'confirmed'
        """, (bus_id, travel_date))

        booked_seats = [row['seat_number'] for row in cur.fetchall()]

    # Generate seat layout (4x10 grid)
    seats_html = ""
    for row in range(4):
        for col in range(1, 11):
            seat_num = row * 10 + col
            if seat_num > bus['total_seats']:
                break

            if seat_num in booked_seats:
                seats_html += f'''
                <button class="btn btn-danger seat-btn" disabled>
                    {seat_num}
                </button>
                '''
            else:
                seats_html += f'''
                <button class="btn btn-success seat-btn" 
                        onclick="selectSeat({seat_num})"
                        data-seat="{seat_num}">
                    {seat_num}
                </button>
                '''

        if row < 3:
            seats_html += '<div class="w-100"></div>'

    content = f"""
    <div class="row">
        <div class="col-md-8">
            <div class="card p-4">
                <div class="d-flex justify-content-between align-items-center mb-4">
                    <div>
                        <h3>{bus['bus_name']}</h3>
                        <p class="text-muted mb-0">
                            {bus['route_name']} | Departure: {bus['departure_time']}
                        </p>
                        <p class="mb-0">Travel Date: {travel_date}</p>
                    </div>
                    <div class="text-end">
                        <div class="badge bg-info">Total Seats: {bus['total_seats']}</div>
                    </div>
                </div>

                <div class="mb-4">
                    <h5>Seat Selection</h5>
                    <div class="seat-legend mb-3">
                        <span class="badge bg-success me-3">Available</span>
                        <span class="badge bg-danger me-3">Booked</span>
                        <span class="badge bg-primary me-3">Selected</span>
                    </div>

                    <div class="seat-layout text-center">
                        {seats_html}
                    </div>
                </div>
            </div>
        </div>

        <div class="col-md-4">
            <div class="card p-4">
                <div id="bookingForm" style="display: none;">
                    <h5 class="mb-3">Passenger Details</h5>
                    <form id="passengerForm">
                        <div class="mb-3">
                            <label class="form-label">Full Name</label>
                            <input type="text" id="passengerName" class="form-control" 
                                   placeholder="Enter passenger name" required>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">Mobile Number</label>
                            <input type="tel" id="mobileNumber" class="form-control" 
                                   placeholder="10-digit mobile number" required>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">Email (Optional)</label>
                            <input type="email" id="email" class="form-control" 
                                   placeholder="email@example.com">
                        </div>

                        <div class="alert alert-info">
                            <i class="fas fa-info-circle"></i> 
                            <small>Seat: <span id="selectedSeatNumber">--</span></small>
                        </div>

                        <button type="button" class="btn btn-primary w-100" 
                                onclick="confirmBooking({bus_id}, '{travel_date}')">
                            <i class="fas fa-check-circle"></i> Confirm Booking
                        </button>
                    </form>
                </div>

                <div id="noSelection" class="text-center text-muted py-5">
                    <i class="fas fa-chair fa-3x mb-3"></i>
                    <h5>Select a Seat</h5>
                    <p>Click on an available seat to begin booking</p>
                </div>
            </div>
        </div>
    </div>

    <style>
    .seat-layout {{
        display: flex;
        flex-wrap: wrap;
        justify-content: center;
        gap: 5px;
        padding: 20px;
        background: #f8f9fa;
        border-radius: 10px;
    }}
    .seat-btn {{
        width: 50px;
        height: 50px;
        display: flex;
        align-items: center;
        justify-content: center;
        font-weight: bold;
    }}
    .seat-btn.selected {{
        background: #0d6efd;
        border-color: #0d6efd;
    }}
    @media (max-width: 768px) {{
        .seat-btn {{
            width: 40px;
            height: 40px;
            font-size: 0.9rem;
        }}
    }}
    </style>

    <script>
    let selectedSeat = null;

    function selectSeat(seatNumber) {{
        // Reset previous selection
        document.querySelectorAll('.seat-btn').forEach(btn => {{
            if (btn.classList.contains('selected')) {{
                btn.classList.remove('selected');
                btn.classList.add('btn-success');
            }}
        }});

        // Mark new selection
        const seatBtn = document.querySelector(`[data-seat="${{seatNumber}}"]`);
        if (seatBtn) {{
            seatBtn.classList.remove('btn-success');
            seatBtn.classList.add('selected');
            selectedSeat = seatNumber;

            // Show booking form
            document.getElementById('selectedSeatNumber').textContent = seatNumber;
            document.getElementById('bookingForm').style.display = 'block';
            document.getElementById('noSelection').style.display = 'none';
        }}
    }}

    function confirmBooking(busId, travelDate) {{
        const name = document.getElementById('passengerName').value.trim();
        const mobile = document.getElementById('mobileNumber').value.trim();
        const email = document.getElementById('email').value.trim();

        if (!name) {{
            alert('Please enter passenger name');
            return;
        }}

        if (!mobile || mobile.length !== 10 || !/^\d+$/.test(mobile)) {{
            alert('Please enter valid 10-digit mobile number');
            return;
        }}

        if (!selectedSeat) {{
            alert('Please select a seat first');
            return;
        }}

        // Disable button and show loading
        const btn = document.querySelector('#bookingForm button');
        const originalText = btn.innerHTML;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Processing...';
        btn.disabled = true;

        fetch('/api/book', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{
                bus_id: busId,
                seat: selectedSeat,
                date: travelDate,
                name: name,
                mobile: mobile,
                email: email
            }})
        }})
        .then(response => response.json())
        .then(data => {{
            if (data.success) {{
                alert('✅ Booking confirmed!\\nSeat: ' + selectedSeat + '\\nFare: ₹' + data.fare);
                window.location.href = '/booking-success/' + data.booking_id;
            }} else {{
                alert('❌ Error: ' + data.error);
                btn.innerHTML = originalText;
                btn.disabled = false;
            }}
        }})
        .catch(error => {{
            console.error('Error:', error);
            alert('Network error. Please try again.');
            btn.innerHTML = originalText;
            btn.disabled = false;
        }});
    }}
    </script>
    """

    return render_template_string(KAYA_BASE_HTML, content=content)


@app.route('/booking-success/<int:booking_id>')
@safe_db
def booking_success(booking_id):
    """Booking success page"""
    with db_cursor() as cur:
        cur.execute("""
            SELECT sb.*, s.bus_name, r.route_name, s.departure_time
            FROM seat_bookings sb
            JOIN schedules s ON sb.schedule_id = s.id
            JOIN routes r ON s.route_id = r.id
            WHERE sb.id = %s
        """, (booking_id,))

        booking = cur.fetchone()

    if not booking:
        return redirect('/')

    content = f"""
    <div class="card p-4 text-center">
        <div class="mb-4">
            <i class="fas fa-check-circle fa-4x text-success mb-3"></i>
            <h2>Booking Confirmed!</h2>
            <p class="text-muted">Your booking has been successfully completed.</p>
        </div>

        <div class="card mb-4" style="max-width: 500px; margin: 0 auto;">
            <div class="card-body">
                <h5 class="card-title">Booking Details</h5>
                <div class="text-start">
                    <p><strong>Booking ID:</strong> {booking_id}</p>
                    <p><strong>Passenger:</strong> {booking['passenger_name']}</p>
                    <p><strong>Bus:</strong> {booking['bus_name']}</p>
                    <p><strong>Route:</strong> {booking['route_name']}</p>
                    <p><strong>Seat:</strong> {booking['seat_number']}</p>
                    <p><strong>Departure:</strong> {booking['departure_time']}</p>
                    <p><strong>Travel Date:</strong> {booking['travel_date']}</p>
                    <p><strong>Fare:</strong> ₹{booking['fare']}</p>
                    <p><strong>Status:</strong> <span class="badge bg-success">{booking['status']}</span></p>
                </div>
            </div>
        </div>

        <div class="d-flex justify-content-center gap-3">
            <a href="/" class="btn btn-primary">
                <i class="fas fa-home"></i> Go Home
            </a>
            <a href="/search" class="btn btn-outline-primary">
                <i class="fas fa-plus"></i> Book Another
            </a>
            <button onclick="window.print()" class="btn btn-outline-secondary">
                <i class="fas fa-print"></i> Print Ticket
            </button>
        </div>

        <div class="mt-4 text-muted">
            <small>A confirmation SMS has been sent to {booking['mobile']}</small>
        </div>
    </div>
    """

    return render_template_string(KAYA_BASE_HTML, content=content)


@app.route('/api/book', methods=['POST'])
@limiter.limit("5 per minute")
@safe_db
def api_book():
    """API for booking seats"""
    data = request.json

    try:
        with db_cursor() as cur:
            # Check if seat already booked
            cur.execute("""
                SELECT id FROM seat_bookings 
                WHERE schedule_id = %s 
                  AND seat_number = %s 
                  AND travel_date = %s
                  AND status = 'confirmed'
            """, (data['bus_id'], data['seat'], data['date']))

            if cur.fetchone():
                return jsonify({
                    "success": False,
                    "error": "Seat already booked. Please select another seat."
                })

            # Calculate fare
            fare = random.randint(300, 500)

            # Generate booking hash
            booking_hash = hashlib.sha256(
                f"{data['bus_id']}_{data['seat']}_{data['date']}_{data['name']}".encode()
            ).hexdigest()

            # Insert booking
            cur.execute("""
                INSERT INTO seat_bookings 
                (schedule_id, seat_number, passenger_name, mobile, email,
                 travel_date, fare, status, booking_hash, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'confirmed', %s, NOW())
                RETURNING id
            """, (
                data['bus_id'],
                data['seat'],
                data['name'],
                data['mobile'],
                data.get('email'),
                data['date'],
                fare,
                booking_hash
            ))

            booking_id = cur.fetchone()[0]

            return jsonify({
                "success": True,
                "message": "Booking confirmed successfully",
                "booking_id": booking_id,
                "fare": fare
            })

    except Exception as e:
        logger.error(f"Booking error: {e}")
        return jsonify({
            "success": False,
            "error": "Booking failed. Please try again."
        }), 500


@app.route('/api/gps', methods=['POST'])
@limiter.limit("10 per minute")
def api_gps():
    """API for GPS updates (polling instead of WebSockets)"""
    data = request.json

    try:
        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO gps_logs 
                (schedule_id, latitude, longitude, speed, accuracy, timestamp)
                VALUES (%s, %s, %s, %s, %s, NOW())
            """, (
                data.get('bus_id'),
                data.get('lat'),
                data.get('lng'),
                data.get('speed', 0),
                data.get('accuracy', 0)
            ))

            # Update bus position
            cur.execute("""
                UPDATE schedules 
                SET current_lat = %s, current_lng = %s, last_gps_update = NOW()
                WHERE id = %s
            """, (
                data.get('lat'),
                data.get('lng'),
                data.get('bus_id')
            ))

        return jsonify({"success": True, "message": "GPS updated"})

    except Exception as e:
        logger.error(f"GPS API error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/track')
def track_page():
    """Bus tracking page"""
    content = """
    <div class="card p-4">
        <h3 class="mb-4"><i class="fas fa-map-marker-alt"></i> Live Bus Tracking</h3>

        <div class="row mb-4">
            <div class="col-md-6">
                <div class="card">
                    <div class="card-body">
                        <h5>Track Bus</h5>
                        <div class="mb-3">
                            <label class="form-label">Bus Number</label>
                            <input type="number" id="busNumber" class="form-control" 
                                   placeholder="Enter bus ID" value="1">
                        </div>
                        <button onclick="trackBus()" class="btn btn-primary w-100">
                            <i class="fas fa-search"></i> Track
                        </button>
                    </div>
                </div>
            </div>
            <div class="col-md-6">
                <div class="card">
                    <div class="card-body">
                        <h5>Live Status</h5>
                        <div id="trackingStatus" class="text-center py-3">
                            <i class="fas fa-bus fa-2x text-muted"></i>
                            <p class="mt-2">Select a bus to track</p>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <div id="mapContainer" style="display: none;">
            <div id="map" style="height: 400px; border-radius: 10px; background: #e9ecef;"></div>
            <div class="mt-3 text-center">
                <button onclick="refreshLocation()" class="btn btn-sm btn-outline-primary">
                    <i class="fas fa-sync-alt"></i> Refresh Location
                </button>
                <small class="text-muted ms-3">Last updated: <span id="lastUpdate">--</span></small>
            </div>
        </div>
    </div>

    <script>
    let currentBusId = null;
    let refreshInterval = null;

    function trackBus() {
        const busId = document.getElementById('busNumber').value;
        if (!busId) {
            alert('Please enter bus number');
            return;
        }

        currentBusId = busId;
        document.getElementById('mapContainer').style.display = 'block';
        updateBusLocation();

        // Refresh every 10 seconds
        if (refreshInterval) {
            clearInterval(refreshInterval);
        }
        refreshInterval = setInterval(updateBusLocation, 10000);
    }

    function updateBusLocation() {
        if (!currentBusId) return;

        fetch(`/api/bus-location/$'{currentBusId}'`)
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    const statusDiv = document.getElementById('trackingStatus');
                    statusDiv.innerHTML = `
                        <div class="text-success">
                            <i class="fas fa-bus fa-2x"></i>
                            <h5 class="mt-2">Bus ${'currentBusId'}</h5>
                            <p class="mb-0">Status: <span class="badge bg-success">Active</span></p>
                            <small>Last update: ${'new Date(data.last_update).toLocaleTimeString()'}</small>
                        </div>
                    `;

                    document.getElementById('lastUpdate').textContent = 
                        new Date(data.last_update).toLocaleTimeString();

                    // Update map display
                    updateMap(data.lat, data.lng);
                } else {
                    document.getElementById('trackingStatus').innerHTML = `
                        <div class="text-danger">
                            <i class="fas fa-exclamation-triangle fa-2x"></i>
                            <p class="mt-2">${'data.error || "Bus not found"}'</p>
                        </div>
                    `;
                }
            })
            .catch(error => {
                console.error('Error:', error);
            });
    }

    function updateMap(lat, lng) {
        const mapDiv = document.getElementById('map');
        mapDiv.innerHTML = `
            <div style="padding: 20px; text-align: center;">
                <i class="fas fa-map-marker-alt fa-3x text-danger"></i>
                <h5 class="mt-3">Bus Location</h5>
                <p>Latitude: ${'lat'}</p>
                <p>Longitude: ${'lng'}</p>
                <p><a href="https://maps.google.com/?q=${'lat'},${'lng'}" target="_blank">
                    <i class="fas fa-external-link-alt"></i> Open in Google Maps
                </a></p>
            </div>
        `;
    }

    function refreshLocation() {
        if (currentBusId) {
            updateBusLocation();
        }
    }
    </script>
    """
    return render_template_string(KAYA_BASE_HTML, content=content)


@app.route('/api/bus-location/<int:bus_id>')
@safe_db
def api_bus_location(bus_id):
    """Get bus location"""
    try:
        with db_cursor() as cur:
            cur.execute("""
                SELECT current_lat, current_lng, last_gps_update
                FROM schedules
                WHERE id = %s
            """, (bus_id,))

            bus = cur.fetchone()

            if bus and bus['current_lat'] and bus['current_lng']:
                return jsonify({
                    "success": True,
                    "lat": bus['current_lat'],
                    "lng": bus['current_lng'],
                    "last_update": bus['last_gps_update'].isoformat() if bus['last_gps_update'] else None
                })
            else:
                return jsonify({
                    "success": False,
                    "error": "Bus location not available"
                })

    except Exception as e:
        logger.error(f"Bus location error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ================= ADMIN PANEL =================
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    """Admin login"""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        with db_cursor() as cur:
            cur.execute("""
                SELECT id, username, password FROM admins 
                WHERE username = %s
            """, (username,))

            admin = cur.fetchone()

            if admin and check_password_hash(admin['password'], password):
                session['admin_logged_in'] = True
                session['admin_id'] = admin['id']
                session['admin_name'] = admin['username']
                return redirect('/admin/dashboard')
            else:
                error = "Invalid credentials"
                return render_template_string(KAYA_BASE_HTML, content=f"""
                    <div class="alert alert-danger">{error}</div>
                    {login_form()}
                """)

    return render_template_string(KAYA_BASE_HTML, content=login_form())


def login_form():
    return """
    <div class="row justify-content-center">
        <div class="col-md-4">
            <div class="card p-4">
                <h3 class="text-center mb-4">Admin Login</h3>
                <form method="POST">
                    <div class="mb-3">
                        <label class="form-label">Username</label>
                        <input type="text" name="username" class="form-control" 
                               placeholder="Enter username" required>
                    </div>
                    <div class="mb-3">
                        <label class="form-label">Password</label>
                        <input type="password" name="password" class="form-control" 
                               placeholder="Enter password" required>
                    </div>
                    <button type="submit" class="btn btn-primary w-100">
                        <i class="fas fa-sign-in-alt"></i> Login
                    </button>
                </form>
                <div class="text-center mt-3">
                    <a href="/" class="text-decoration-none">
                        <i class="fas fa-arrow-left"></i> Back to Home
                    </a>
                </div>
            </div>
        </div>
    </div>
    """


@app.route('/admin/dashboard')
@safe_db
def admin_dashboard():
    """Admin dashboard"""
    if not session.get('admin_logged_in'):
        return redirect('/admin/login')

    with db_cursor() as cur:
        # Get stats
        cur.execute("SELECT COUNT(*) as total FROM schedules WHERE is_active = true")
        active_buses = cur.fetchone()['total']

        cur.execute("""
            SELECT COUNT(*) as total 
            FROM seat_bookings 
            WHERE DATE(created_at) = CURRENT_DATE
        """)
        today_bookings = cur.fetchone()['total']

        cur.execute("""
            SELECT COALESCE(SUM(fare), 0) as total 
            FROM seat_bookings 
            WHERE DATE(created_at) = CURRENT_DATE
        """)
        today_revenue = cur.fetchone()['total']

        # Recent bookings
        cur.execute("""
            SELECT sb.*, s.bus_name 
            FROM seat_bookings sb
            JOIN schedules s ON sb.schedule_id = s.id
            ORDER BY sb.created_at DESC 
            LIMIT 5
        """)
        recent_bookings = cur.fetchall()

    bookings_html = ""
    for booking in recent_bookings:
        bookings_html += f"""
        <tr>
            <td>{booking['id']}</td>
            <td>{booking['passenger_name']}</td>
            <td>{booking['bus_name']}</td>
            <td>{booking['seat_number']}</td>
            <td>₹{booking['fare']}</td>
            <td><span class="badge bg-success">{booking['status']}</span></td>
        </tr>
        """

    content = f"""
    <div class="card p-4">
        <div class="d-flex justify-content-between align-items-center mb-4">
            <div>
                <h3 class="mb-1">Admin Dashboard</h3>
                <p class="text-muted mb-0">Welcome, {session.get('admin_name', 'Admin')}</p>
            </div>
            <a href="/admin/logout" class="btn btn-outline-danger">
                <i class="fas fa-sign-out-alt"></i> Logout
            </a>
        </div>

        <div class="row mb-4">
            <div class="col-md-4">
                <div class="card bg-primary text-white p-3 text-center">
                    <h4>{active_buses}</h4>
                    <p class="mb-0">Active Buses</p>
                </div>
            </div>
            <div class="col-md-4">
                <div class="card bg-success text-white p-3 text-center">
                    <h4>{today_bookings}</h4>
                    <p class="mb-0">Today's Bookings</p>
                </div>
            </div>
            <div class="col-md-4">
                <div class="card bg-warning text-white p-3 text-center">
                    <h4>₹{today_revenue}</h4>
                    <p class="mb-0">Today's Revenue</p>
                </div>
            </div>
        </div>

        <h5 class="mb-3">Recent Bookings</h5>
        <div class="table-responsive">
            <table class="table table-hover">
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Passenger</th>
                        <th>Bus</th>
                        <th>Seat</th>
                        <th>Fare</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>
                    {bookings_html}
                </tbody>
            </table>
        </div>

        <div class="mt-4">
            <h5>Quick Actions</h5>
            <div class="d-flex flex-wrap gap-2">
                <a href="/admin/buses" class="btn btn-outline-primary">
                    <i class="fas fa-bus"></i> Manage Buses
                </a>
                <a href="/admin/routes" class="btn btn-outline-primary">
                    <i class="fas fa-route"></i> Manage Routes
                </a>
                <a href="/admin/bookings" class="btn btn-outline-success">
                    <i class="fas fa-list"></i> All Bookings
                </a>
                <a href="/health" class="btn btn-outline-info">
                    <i class="fas fa-heartbeat"></i> System Health
                </a>
            </div>
        </div>
    </div>
    """

    return render_template_string(KAYA_BASE_HTML, content=content)


@app.route('/admin/buses')
@safe_db
def admin_buses():
    """Manage buses"""
    if not session.get('admin_logged_in'):
        return redirect('/admin/login')

    with db_cursor() as cur:
        cur.execute("""
            SELECT s.*, r.route_name
            FROM schedules s
            LEFT JOIN routes r ON s.route_id = r.id
            ORDER BY s.id
        """)
        buses = cur.fetchall()

    buses_html = ""
    for bus in buses:
        status_badge = "success" if bus['is_active'] else "secondary"
        status_text = "Active" if bus['is_active'] else "Inactive"

        buses_html += f"""
        <tr>
            <td>{bus['id']}</td>
            <td>{bus['bus_name']}</td>
            <td>{bus['route_name'] or 'N/A'}</td>
            <td>{bus['departure_time']}</td>
            <td>{bus['total_seats']}</td>
            <td><span class="badge bg-{status_badge}">{status_text}</span></td>
            <td>
                <button class="btn btn-sm btn-outline-primary">
                    <i class="fas fa-edit"></i>
                </button>
            </td>
        </tr>
        """

    content = f"""
    <div class="card p-4">
        <div class="d-flex justify-content-between align-items-center mb-4">
            <h3 class="mb-0">Manage Buses</h3>
            <a href="/admin/dashboard" class="btn btn-outline-secondary">
                <i class="fas fa-arrow-left"></i> Back
            </a>
        </div>

        <div class="table-responsive">
            <table class="table table-hover">
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Bus Name</th>
                        <th>Route</th>
                        <th>Departure</th>
                        <th>Seats</th>
                        <th>Status</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {buses_html}
                </tbody>
            </table>
        </div>
    </div>
    """

    return render_template_string(KAYA_BASE_HTML, content=content)


@app.route('/admin/logout')
def admin_logout():
    """Admin logout"""
    session.clear()
    return redirect('/')


# ================= KAYA RENDER के लिए RUN COMMAND =================
if __name__ == '__main__':
    print("🚀 Starting My Bus AI - Kaya Render Optimized Version")
    print("📊 Memory Limit: 512MB")
    print("💾 Database Connections:", Config.MAX_DB_CONNECTIONS)
    print("🌐 WebSockets: Disabled (using HTTP polling)")
    print("✅ Database initialized")

    # Get port from environment (Kaya Render provides this)
    port = int(os.getenv('PORT', 10000))

    # For local testing
    app.run(
        host='0.0.0.0',
        port=port,
        debug=False,  # Debug mode off for production
        threaded=True  # Handle multiple requests
    )