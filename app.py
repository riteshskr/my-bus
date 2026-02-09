"""
🚍 Bus Booking System with Supabase PostgreSQL
Deploy on Render.com
"""

import os
import json
import time
import random
from datetime import date, datetime
from functools import wraps

from flask import Flask, request, jsonify, render_template_string, redirect, session
from flask_socketio import SocketIO, emit
from flask_compress import Compress
import psycopg2
from psycopg2.extras import RealDictCursor
import razorpay

# ================= CONFIGURATION =================
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "supabase-bus-secret-key-2024")

Compress(app)

# SocketIO Configuration
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    logger=False,
    engineio_logger=False
)

# ================= DATABASE CONNECTION =================
def get_db_connection():
    """Connect to Supabase PostgreSQL"""
    database_url = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
    
    if not database_url:
        raise ValueError("❌ SUPABASE_DB_URL environment variable is missing!")
    
    # Ensure postgresql:// protocol
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://")
    
    try:
        conn = psycopg2.connect(
            database_url,
            cursor_factory=RealDictCursor
        )
        return conn
    except Exception as e:
        print(f"❌ Database connection error: {e}")
        raise

# ================= DATABASE INITIALIZATION =================
def init_database():
    """Initialize database tables with sample data"""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Create tables
        tables = [
            # Admins table
            """
            CREATE TABLE IF NOT EXISTS admins (
                id SERIAL PRIMARY KEY,
                username VARCHAR(100) UNIQUE NOT NULL,
                password VARCHAR(100) NOT NULL,
                role VARCHAR(20) DEFAULT 'admin',
                counter_no INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW()
            );
            """,
            
            # Routes table
            """
            CREATE TABLE IF NOT EXISTS routes (
                id SERIAL PRIMARY KEY,
                route_name VARCHAR(200) UNIQUE NOT NULL,
                distance_km INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            );
            """,
            
            # Schedules table
            """
            CREATE TABLE IF NOT EXISTS schedules (
                id SERIAL PRIMARY KEY,
                route_id INTEGER REFERENCES routes(id),
                bus_name VARCHAR(100) NOT NULL,
                departure_time TIME NOT NULL,
                current_lat DECIMAL(10, 6) DEFAULT 28.6139,
                current_lng DECIMAL(10, 6) DEFAULT 77.2090,
                total_seats INTEGER DEFAULT 40,
                created_at TIMESTAMP DEFAULT NOW()
            );
            """,
            
            # Seat bookings table
            """
            CREATE TABLE IF NOT EXISTS seat_bookings (
                id SERIAL PRIMARY KEY,
                schedule_id INTEGER REFERENCES schedules(id) ON DELETE CASCADE,
                seat_number INTEGER NOT NULL,
                passenger_name VARCHAR(100) NOT NULL,
                mobile VARCHAR(15) NOT NULL,
                from_station VARCHAR(100),
                to_station VARCHAR(100),
                travel_date DATE NOT NULL,
                status VARCHAR(20) DEFAULT 'confirmed',
                fare INTEGER DEFAULT 300,
                payment_mode VARCHAR(20) DEFAULT 'cash',
                booked_by_type VARCHAR(20) DEFAULT 'user',
                booked_by_id INTEGER,
                counter_id INTEGER,
                order_id VARCHAR(100),
                payment_id VARCHAR(100),
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(schedule_id, seat_number, travel_date)
            );
            """,
            
            # Route stations table
            """
            CREATE TABLE IF NOT EXISTS route_stations (
                id SERIAL PRIMARY KEY,
                route_id INTEGER REFERENCES routes(id),
                station_name VARCHAR(100) NOT NULL,
                station_order INTEGER NOT NULL,
                lat DECIMAL(10, 6) DEFAULT 28.6139,
                lng DECIMAL(10, 6) DEFAULT 77.2090,
                created_at TIMESTAMP DEFAULT NOW()
            );
            """
        ]
        
        # Execute table creation
        for table_sql in tables:
            cur.execute(table_sql)
        
        # Check if tables are empty and insert sample data
        cur.execute("SELECT COUNT(*) as count FROM admins")
        if cur.fetchone()['count'] == 0:
            # Default admin
            cur.execute("""
                INSERT INTO admins (username, password, role, counter_no)
                VALUES ('admin', 'admin123', 'admin', 1)
                ON CONFLICT (username) DO NOTHING
            """)
            
            # Sample routes
            sample_routes = [
                ('Delhi → Jaipur', 280),
                ('Delhi → Mumbai', 1400),
                ('Bangalore → Chennai', 350),
                ('Kolkata → Patna', 550),
                ('Hyderabad → Bangalore', 570),
                ('Pune → Mumbai', 150)
            ]
            
            for route_name, distance in sample_routes:
                cur.execute("""
                    INSERT INTO routes (route_name, distance_km)
                    VALUES (%s, %s)
                    ON CONFLICT (route_name) DO NOTHING
                """, (route_name, distance))
            
            # Get route IDs
            cur.execute("SELECT id, route_name FROM routes")
            routes = {r['route_name']: r['id'] for r in cur.fetchall()}
            
            # Sample schedules
            sample_schedules = [
                (routes['Delhi → Jaipur'], 'Volvo AC Sleeper', '08:00'),
                (routes['Delhi → Jaipur'], 'Semi Sleeper AC', '10:30'),
                (routes['Delhi → Mumbai'], 'Luxury AC Coach', '20:00'),
                (routes['Bangalore → Chennai'], 'Express AC', '07:00'),
                (routes['Kolkata → Patna'], 'Super Deluxe', '09:15')
            ]
            
            for route_id, bus_name, departure in sample_schedules:
                cur.execute("""
                    INSERT INTO schedules (route_id, bus_name, departure_time)
                    VALUES (%s, %s, %s::time)
                    ON CONFLICT DO NOTHING
                """, (route_id, bus_name, departure))
            
            # Sample stations
            station_data = [
                (routes['Delhi → Jaipur'], 'Delhi', 1, 28.6139, 77.2090),
                (routes['Delhi → Jaipur'], 'Jaipur', 2, 26.9124, 75.7873),
                (routes['Delhi → Mumbai'], 'Delhi', 1, 28.6139, 77.2090),
                (routes['Delhi → Mumbai'], 'Mumbai', 2, 19.0760, 72.8777),
                (routes['Bangalore → Chennai'], 'Bangalore', 1, 12.9716, 77.5946),
                (routes['Bangalore → Chennai'], 'Chennai', 2, 13.0827, 80.2707),
                (routes['Kolkata → Patna'], 'Kolkata', 1, 22.5726, 88.3639),
                (routes['Kolkata → Patna'], 'Patna', 2, 25.5941, 85.1376)
            ]
            
            for route_id, station, order_no, lat, lng in station_data:
                cur.execute("""
                    INSERT INTO route_stations (route_id, station_name, station_order, lat, lng)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                """, (route_id, station, order_no, lat, lng))
        
        conn.commit()
        print("✅ Database initialized successfully!")
        
    except Exception as e:
        print(f"❌ Error initializing database: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

# Initialize database on startup
init_database()

# ================= DATABASE HELPER =================
def db_operation(func):
    """Decorator for database operations"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            result = func(cur, *args, **kwargs)
            conn.commit()
            return result
        except Exception as e:
            print(f"Database error in {func.__name__}: {e}")
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                cur.close()
                conn.close()
    return wrapper

# ================= PAYMENT CONFIG =================
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")

if RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET:
    razor_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
    RAZORPAY_ENABLED = True
else:
    razor_client = None
    RAZORPAY_ENABLED = False

# ================= SOCKET.IO EVENTS =================
@socketio.on("connect")
def handle_connect():
    """Handle client connection"""
    print(f"✅ Client connected: {request.sid}")

@socketio.on("driver_gps")
def handle_gps(data):
    """Handle GPS updates from drivers"""
    try:
        bus_id = data.get('bus_id') or data.get('sid')
        lat = float(data.get('lat', 28.6139))
        lng = float(data.get('lng', 77.2090))
        speed = float(data.get('speed', 0))
        
        print(f"📍 Bus {bus_id} GPS: {lat}, {lng}")
        
        # Update in database
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            UPDATE schedules 
            SET current_lat = %s, current_lng = %s
            WHERE id = %s
        """, (lat, lng, bus_id))
        conn.commit()
        cur.close()
        conn.close()
        
        # Broadcast to all clients
        socketio.emit("bus_location", {
            "bus_id": bus_id,
            "lat": lat,
            "lng": lng,
            "speed": speed,
            "timestamp": time.time()
        })
        
    except Exception as e:
        print(f"❌ GPS error: {e}")

@socketio.on("seat_booked")
def handle_seat_booked(data):
    """Notify all clients about seat booking"""
    socketio.emit("seat_update", data)

# ================= HTML TEMPLATES =================
BASE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🚍 Smart Bus Booking</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        }
        
        body {
            background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
            min-height: 100vh;
        }
        
        .navbar {
            background: white;
            box-shadow: 0 2px 15px rgba(0,0,0,0.1);
            padding: 15px 0;
        }
        
        .logo {
            font-size: 1.8rem;
            font-weight: 700;
            color: #4a6bff;
            text-decoration: none;
        }
        
        .logo i {
            margin-right: 10px;
        }
        
        .hero {
            background: linear-gradient(rgba(74, 107, 255, 0.9), rgba(74, 107, 255, 0.7)),
                        url('https://images.unsplash.com/photo-1544620347-c4fd4a3d5957?ixlib=rb-4.0.3&auto=format&fit=crop&w=1920&q=80');
            background-size: cover;
            background-position: center;
            color: white;
            padding: 100px 20px;
            text-align: center;
            border-radius: 0 0 30px 30px;
            margin-bottom: 40px;
        }
        
        .hero h1 {
            font-size: 3.5rem;
            font-weight: 800;
            margin-bottom: 20px;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
        }
        
        .hero p {
            font-size: 1.2rem;
            max-width: 600px;
            margin: 0 auto 30px;
            opacity: 0.9;
        }
        
        .search-box {
            background: white;
            padding: 30px;
            border-radius: 20px;
            box-shadow: 0 20px 40px rgba(0,0,0,0.15);
            max-width: 800px;
            margin: -50px auto 50px;
            position: relative;
            z-index: 10;
        }
        
        .search-box h3 {
            color: #333;
            margin-bottom: 25px;
            font-weight: 600;
        }
        
        .form-control, .form-select {
            padding: 12px 15px;
            border: 2px solid #e0e0e0;
            border-radius: 10px;
            font-size: 16px;
            transition: all 0.3s;
        }
        
        .form-control:focus, .form-select:focus {
            border-color: #4a6bff;
            box-shadow: 0 0 0 0.25rem rgba(74, 107, 255, 0.25);
        }
        
        .btn-primary {
            background: #4a6bff;
            border: none;
            padding: 12px 30px;
            border-radius: 10px;
            font-weight: 600;
            font-size: 16px;
            transition: all 0.3s;
        }
        
        .btn-primary:hover {
            background: #3a5bef;
            transform: translateY(-2px);
            box-shadow: 0 10px 20px rgba(74, 107, 255, 0.3);
        }
        
        .card {
            border: none;
            border-radius: 15px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.08);
            transition: transform 0.3s;
            margin-bottom: 25px;
        }
        
        .card:hover {
            transform: translateY(-5px);
        }
        
        .card-title {
            color: #333;
            font-weight: 600;
        }
        
        .badge {
            padding: 8px 15px;
            border-radius: 20px;
            font-weight: 500;
        }
        
        .seat {
            width: 50px;
            height: 50px;
            margin: 5px;
            border: 2px solid #ddd;
            border-radius: 10px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }
        
        .seat.available {
            background: #d4edda;
            color: #155724;
            border-color: #c3e6cb;
        }
        
        .seat.available:hover {
            background: #c3e6cb;
            transform: scale(1.05);
        }
        
        .seat.booked {
            background: #f8d7da;
            color: #721c24;
            border-color: #f5c6cb;
            cursor: not-allowed;
        }
        
        .seat.selected {
            background: #4a6bff;
            color: white;
            border-color: #4a6bff;
        }
        
        .map-container {
            width: 100%;
            height: 400px;
            border-radius: 15px;
            overflow: hidden;
            box-shadow: 0 10px 30px rgba(0,0,0,0.1);
        }
        
        footer {
            background: #333;
            color: white;
            padding: 40px 0;
            margin-top: 60px;
        }
        
        @media (max-width: 768px) {
            .hero h1 {
                font-size: 2.5rem;
            }
            
            .search-box {
                padding: 20px;
                margin: -30px 20px 30px;
            }
        }
    </style>
</head>
<body>
    <!-- Navigation -->
    <nav class="navbar navbar-expand-lg">
        <div class="container">
            <a class="logo" href="/">
                <i class="fas fa-bus"></i> SmartBus
            </a>
            <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav">
                <span class="navbar-toggler-icon"></span>
            </button>
            <div class="collapse navbar-collapse" id="navbarNav">
                <ul class="navbar-nav ms-auto">
                    <li class="nav-item">
                        <a class="nav-link" href="/">Home</a>
                    </li>
                    <li class="nav-item">
                        <a class="nav-link" href="/buses">Buses</a>
                    </li>
                    <li class="nav-item">
                        <a class="nav-link" href="/login">Admin</a>
                    </li>
                    <li class="nav-item">
                        <a class="nav-link" href="/counter">Counter</a>
                    </li>
                    <li class="nav-item">
                        <a class="nav-link" href="/dashboard">
                            <i class="fas fa-user-circle"></i> Dashboard
                        </a>
                    </li>
                </ul>
            </div>
        </div>
    </nav>

    <!-- Main Content -->
    {% block content %}{% endblock %}

    <!-- Footer -->
    <footer>
        <div class="container text-center">
            <h4>🚍 Smart Bus Booking System</h4>
            <p>Powered by Supabase PostgreSQL & Flask</p>
            <p class="mt-3">
                <a href="#" class="text-white me-3"><i class="fab fa-twitter"></i></a>
                <a href="#" class="text-white me-3"><i class="fab fa-facebook"></i></a>
                <a href="#" class="text-white"><i class="fab fa-instagram"></i></a>
            </p>
            <p class="mt-3 text-white-50">© 2024 SmartBus. All rights reserved.</p>
        </div>
    </footer>

    <!-- Scripts -->
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
    
    <!-- Socket.IO Connection -->
    <script>
        const socket = io();
        
        socket.on('connect', () => {
            console.log('Connected to server');
        });
        
        socket.on('bus_location', (data) => {
            console.log('Bus location update:', data);
            // Handle bus location updates
        });
        
        socket.on('seat_update', (data) => {
            console.log('Seat update:', data);
            // Handle seat updates
        });
    </script>
    
    {% block scripts %}{% endblock %}
</body>
</html>
"""

LOGIN_HTML = """
{% extends "base.html" %}

{% block content %}
<div class="container">
    <div class="row justify-content-center">
        <div class="col-md-6 col-lg-5">
            <div class="card mt-5">
                <div class="card-body p-5">
                    <h3 class="card-title text-center mb-4">
                        <i class="fas fa-sign-in-alt me-2"></i>Login
                    </h3>
                    
                    {% if error %}
                    <div class="alert alert-danger">{{ error }}</div>
                    {% endif %}
                    
                    <form method="POST" action="/login">
                        <div class="mb-3">
                            <label class="form-label">Username</label>
                            <div class="input-group">
                                <span class="input-group-text">
                                    <i class="fas fa-user"></i>
                                </span>
                                <input type="text" name="username" class="form-control" 
                                       placeholder="Enter username" required>
                            </div>
                        </div>
                        
                        <div class="mb-4">
                            <label class="form-label">Password</label>
                            <div class="input-group">
                                <span class="input-group-text">
                                    <i class="fas fa-lock"></i>
                                </span>
                                <input type="password" name="password" class="form-control" 
                                       placeholder="Enter password" required>
                            </div>
                        </div>
                        
                        <div class="d-grid gap-2">
                            <button type="submit" class="btn btn-primary btn-lg">
                                <i class="fas fa-sign-in-alt me-2"></i>Login
                            </button>
                        </div>
                    </form>
                    
                    <div class="text-center mt-4">
                        <p class="mb-0">
                            <a href="/" class="text-decoration-none">
                                <i class="fas fa-arrow-left me-1"></i>Back to Home
                            </a>
                        </p>
                    </div>
                </div>
            </div>
        </div>
    </div>
</div>
{% endblock %}
"""

# ================= ROUTES =================
@app.route("/")
def home():
    """Home page"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get popular routes
        cur.execute("""
            SELECT r.*, COUNT(s.id) as bus_count
            FROM routes r
            LEFT JOIN schedules s ON r.id = s.route_id
            GROUP BY r.id
            ORDER BY bus_count DESC
            LIMIT 6
        """)
        popular_routes = cur.fetchall()
        
        # Get unique stations
        cur.execute("SELECT DISTINCT station_name FROM route_stations ORDER BY station_name")
        stations = [r['station_name'] for r in cur.fetchall()]
        
        cur.close()
        conn.close()
        
        # Render home page
        home_content = """
        <div class="hero">
            <div class="container">
                <h1>Book Bus Tickets Online</h1>
                <p>Safe, comfortable, and affordable bus travel across India</p>
            </div>
        </div>
        
        <div class="container">
            <!-- Search Box -->
            <div class="search-box">
                <h3 class="text-center mb-4">
                    <i class="fas fa-search me-2"></i>Find Your Bus
                </h3>
                <form action="/search" method="POST">
                    <div class="row g-3">
                        <div class="col-md-4">
                            <label class="form-label">From</label>
                            <input type="text" name="from" class="form-control" 
                                   placeholder="Departure city" list="stations-from" required>
                            <datalist id="stations-from">
                                {% for station in stations %}
                                <option value="{{ station }}">
                                {% endfor %}
                            </datalist>
                        </div>
                        <div class="col-md-4">
                            <label class="form-label">To</label>
                            <input type="text" name="to" class="form-control" 
                                   placeholder="Destination city" list="stations-to" required>
                            <datalist id="stations-to">
                                {% for station in stations %}
                                <option value="{{ station }}">
                                {% endfor %}
                            </datalist>
                        </div>
                        <div class="col-md-3">
                            <label class="form-label">Date</label>
                            <input type="date" name="date" class="form-control" 
                                   value="{{ today }}" min="{{ today }}" required>
                        </div>
                        <div class="col-md-1 d-flex align-items-end">
                            <button type="submit" class="btn btn-primary w-100">
                                <i class="fas fa-search"></i>
                            </button>
                        </div>
                    </div>
                </form>
            </div>
            
            <!-- Popular Routes -->
            <h3 class="text-center mb-4">Popular Bus Routes</h3>
            <div class="row">
                {% for route in routes %}
                <div class="col-md-4">
                    <div class="card h-100">
                        <div class="card-body">
                            <h5 class="card-title">
                                <i class="fas fa-route me-2"></i>{{ route.route_name }}
                            </h5>
                            <p class="card-text">
                                <i class="fas fa-road me-1"></i> {{ route.distance_km }} km
                                <br>
                                <i class="fas fa-bus me-1"></i> {{ route.bus_count }} buses
                            </p>
                            <a href="/buses/{{ route.id }}" class="btn btn-outline-primary">
                                View Buses <i class="fas fa-arrow-right ms-1"></i>
                            </a>
                        </div>
                    </div>
                </div>
                {% endfor %}
            </div>
            
            <!-- Features -->
            <div class="row mt-5">
                <div class="col-md-4 text-center">
                    <div class="p-4">
                        <i class="fas fa-shield-alt fa-3x text-primary mb-3"></i>
                        <h5>Safe Travel</h5>
                        <p>Sanitized buses with trained staff</p>
                    </div>
                </div>
                <div class="col-md-4 text-center">
                    <div class="p-4">
                        <i class="fas fa-bolt fa-3x text-primary mb-3"></i>
                        <h5>Live Tracking</h5>
                        <p>Real-time bus location tracking</p>
                    </div>
                </div>
                <div class="col-md-4 text-center">
                    <div class="p-4">
                        <i class="fas fa-headset fa-3x text-primary mb-3"></i>
                        <h5>24/7 Support</h5>
                        <p>Customer support always available</p>
                    </div>
                </div>
            </div>
        </div>
        """
        
        today = date.today().isoformat()
        return render_template_string(
            BASE_HTML.replace("{% block content %}{% endblock %}", home_content),
            routes=popular_routes,
            stations=stations,
            today=today
        )
        
    except Exception as e:
        return f"<div class='container mt-5'><div class='alert alert-danger'>Error: {str(e)}</div></div>"

@app.route("/login", methods=["GET", "POST"])
def login():
    """Admin login"""
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                SELECT id, username, role FROM admins 
                WHERE username = %s AND password = %s
            """, (username, password))
            
            user = cur.fetchone()
            cur.close()
            conn.close()
            
            if user:
                session.clear()
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                session["role"] = user["role"]
                session["logged_in"] = True
                return redirect("/dashboard")
            else:
                return render_template_string(LOGIN_HTML, error="Invalid username or password")
                
        except Exception as e:
            return render_template_string(LOGIN_HTML, error=f"Server error: {str(e)}")
    
    return render_template_string(LOGIN_HTML)

@app.route("/counter", methods=["GET", "POST"])
def counter_login():
    """Counter login"""
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                SELECT id, username, role, counter_no FROM admins 
                WHERE username = %s AND password = %s AND role IN ('counter', 'admin')
            """, (username, password))
            
            user = cur.fetchone()
            cur.close()
            conn.close()
            
            if user:
                session.clear()
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                session["role"] = user["role"]
                session["counter_no"] = user.get("counter_no", 0)
                session["logged_in"] = True
                return redirect("/counter/dashboard")
            else:
                return render_template_string(LOGIN_HTML, error="Invalid credentials")
                
        except Exception as e:
            return render_template_string(LOGIN_HTML, error=f"Server error: {str(e)}")
    
    return render_template_string(LOGIN_HTML)

@app.route("/dashboard")
def dashboard():
    """User dashboard"""
    if not session.get("logged_in"):
        return redirect("/login")
    
    user_role = session.get("role", "user")
    
    dashboard_content = f"""
    <div class="container mt-5">
        <div class="row">
            <div class="col-md-3">
                <div class="card">
                    <div class="card-body text-center">
                        <h5 class="card-title">Welcome</h5>
                        <h3>{session.get('username', 'User')}</h3>
                        <p class="text-muted">Role: {user_role}</p>
                    </div>
                </div>
                
                <div class="card mt-3">
                    <div class="card-body">
                        <h6 class="card-title">Quick Actions</h6>
                        <ul class="list-group list-group-flush">
                            <li class="list-group-item">
                                <a href="/" class="text-decoration-none">
                                    <i class="fas fa-home me-2"></i>Home
                                </a>
                            </li>
                            <li class="list-group-item">
                                <a href="/buses" class="text-decoration-none">
                                    <i class="fas fa-bus me-2"></i>View Buses
                                </a>
                            </li>
                            <li class="list-group-item">
                                <a href="/bookings" class="text-decoration-none">
                                    <i class="fas fa-ticket-alt me-2"></i>My Bookings
                                </a>
                            </li>
                            <li class="list-group-item">
                                <a href="/logout" class="text-decoration-none text-danger">
                                    <i class="fas fa-sign-out-alt me-2"></i>Logout
                                </a>
                            </li>
                        </ul>
                    </div>
                </div>
            </div>
            
            <div class="col-md-9">
                <div class="card">
                    <div class="card-body">
                        <h4 class="card-title">
                            <i class="fas fa-tachometer-alt me-2"></i>Dashboard
                        </h4>
                        
                        <div class="row mt-4">
                            <div class="col-md-4">
                                <div class="card bg-primary text-white">
                                    <div class="card-body text-center">
                                        <h1 id="totalBuses">0</h1>
                                        <p>Active Buses</p>
                                    </div>
                                </div>
                            </div>
                            <div class="col-md-4">
                                <div class="card bg-success text-white">
                                    <div class="card-body text-center">
                                        <h1 id="totalBookings">0</h1>
                                        <p>Today's Bookings</p>
                                    </div>
                                </div>
                            </div>
                            <div class="col-md-4">
                                <div class="card bg-info text-white">
                                    <div class="card-body text-center">
                                        <h1 id="availableSeats">0</h1>
                                        <p>Available Seats</p>
                                    </div>
                                </div>
                            </div>
                        </div>
                        
                        {% if role == 'admin' %}
                        <div class="mt-4">
                            <h5>Admin Actions</h5>
                            <div class="d-flex flex-wrap gap-2">
                                <a href="/admin/routes" class="btn btn-outline-primary">
                                    <i class="fas fa-route me-1"></i>Manage Routes
                                </a>
                                <a href="/admin/schedules" class="btn btn-outline-primary">
                                    <i class="fas fa-calendar-alt me-1"></i>Manage Schedules
                                </a>
                                <a href="/admin/users" class="btn btn-outline-primary">
                                    <i class="fas fa-users me-1"></i>Manage Users
                                </a>
                            </div>
                        </div>
                        {% endif %}
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <script>
        // Fetch dashboard stats
        async function loadDashboardStats() {{
            try {{
                const response = await fetch('/api/dashboard-stats');
                const data = await response.json();
                
                if (data.success) {{
                    document.getElementById('totalBuses').textContent = data.total_buses || 0;
                    document.getElementById('totalBookings').textContent = data.today_bookings || 0;
                    document.getElementById('availableSeats').textContent = data.available_seats || 0;
                }}
            }} catch (error) {{
                console.error('Error loading stats:', error);
            }}
        }}
        
        // Load stats on page load
        document.addEventListener('DOMContentLoaded', loadDashboardStats);
    </script>
    """
    
    return render_template_string(
        BASE_HTML.replace("{% block content %}{% endblock %}", dashboard_content),
        role=user_role
    )

@app.route("/counter/dashboard")
def counter_dashboard():
    """Counter dashboard"""
    if not session.get("logged_in") or session.get("role") not in ["counter", "admin"]:
        return redirect("/counter")
    
    return f"""
    <div class="container mt-5">
        <div class="card">
            <div class="card-body">
                <h3 class="card-title">
                    <i class="fas fa-store me-2"></i>Counter Dashboard
                </h3>
                <p>Counter Number: {session.get('counter_no', 'N/A')}</p>
                
                <div class="mt-4">
                    <a href="/counter/book" class="btn btn-primary me-2">
                        <i class="fas fa-ticket-alt me-1"></i>Book Ticket
                    </a>
                    <a href="/counter/view" class="btn btn-outline-primary me-2">
                        <i class="fas fa-list me-1"></i>View Bookings
                    </a>
                    <a href="/logout" class="btn btn-outline-danger">
                        <i class="fas fa-sign-out-alt me-1"></i>Logout
                    </a>
                </div>
            </div>
        </div>
    </div>
    """

@app.route("/buses")
def list_buses():
    """List all buses"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT s.*, r.route_name, r.distance_km,
                   COUNT(CASE WHEN b.status = 'confirmed' THEN 1 END) as booked_seats
            FROM schedules s
            JOIN routes r ON s.route_id = r.id
            LEFT JOIN seat_bookings b ON s.id = b.schedule_id 
                AND b.travel_date = CURRENT_DATE
            GROUP BY s.id, r.id
            ORDER BY s.departure_time
        """)
        
        buses = cur.fetchall()
        cur.close()
        conn.close()
        
        buses_html = ""
        for bus in buses:
            available_seats = bus['total_seats'] - (bus['booked_seats'] or 0)
            
            buses_html += f"""
            <div class="col-md-6">
                <div class="card">
                    <div class="card-body">
                        <h5 class="card-title">
                            <i class="fas fa-bus me-2"></i>{bus['bus_name']}
                        </h5>
                        <p class="card-text">
                            <strong>Route:</strong> {bus['route_name']}<br>
                            <strong>Departure:</strong> {bus['departure_time']}<br>
                            <strong>Distance:</strong> {bus['distance_km']} km<br>
                            <strong>Available Seats:</strong> {available_seats}/{bus['total_seats']}
                        </p>
                        <div class="d-flex justify-content-between">
                            <a href="/bus/{bus['id']}" class="btn btn-primary">
                                <i class="fas fa-eye me-1"></i>View Details
                            </a>
                            <a href="/book/{bus['id']}" class="btn btn-success">
                                <i class="fas fa-ticket-alt me-1"></i>Book Now
                            </a>
                        </div>
                    </div>
                </div>
            </div>
            """
        
        content = f"""
        <div class="container mt-5">
            <h2 class="mb-4">
                <i class="fas fa-bus me-2"></i>Available Buses
            </h2>
            <div class="row">
                {buses_html}
            </div>
        </div>
        """
        
        return render_template_string(
            BASE_HTML.replace("{% block content %}{% endblock %}", content)
        )
        
    except Exception as e:
        return f"<div class='container mt-5'><div class='alert alert-danger'>Error: {str(e)}</div></div>"

@app.route("/search", methods=["POST"])
def search_buses():
    """Search buses"""
    from_station = request.form.get("from", "").strip()
    to_station = request.form.get("to", "").strip()
    travel_date = request.form.get("date", date.today().isoformat())
    
    # Store in session
    session["search_from"] = from_station
    session["search_to"] = to_station
    session["search_date"] = travel_date
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Find routes with both stations
        cur.execute("""
            SELECT DISTINCT r.*
            FROM routes r
            JOIN route_stations rs1 ON r.id = rs1.route_id
            JOIN route_stations rs2 ON r.id = rs2.route_id
            WHERE LOWER(rs1.station_name) = LOWER(%s)
              AND LOWER(rs2.station_name) = LOWER(%s)
              AND rs1.station_order < rs2.station_order
        """, (from_station, to_station))
        
        routes = cur.fetchall()
        
        if not routes:
            return """
            <div class="container mt-5">
                <div class="alert alert-warning">
                    <h4>No buses found for this route</h4>
                    <p>Try searching for different stations.</p>
                    <a href="/" class="btn btn-primary">Back to Search</a>
                </div>
            </div>
            """
        
        # Get buses for the first matching route
        route_id = routes[0]['id']
        
        cur.execute("""
            SELECT s.*, r.route_name, r.distance_km,
                   COUNT(CASE WHEN b.status = 'confirmed' AND b.travel_date = %s THEN 1 END) as booked_seats
            FROM schedules s
            JOIN routes r ON s.route_id = r.id
            LEFT JOIN seat_bookings b ON s.id = b.schedule_id
            WHERE s.route_id = %s
            GROUP BY s.id, r.id
            ORDER BY s.departure_time
        """, (travel_date, route_id))
        
        buses = cur.fetchall()
        cur.close()
        conn.close()
        
        buses_html = ""
        for bus in buses:
            available_seats = bus['total_seats'] - (bus['booked_seats'] or 0)
            
            buses_html += f"""
            <div class="col-md-6">
                <div class="card">
                    <div class="card-body">
                        <h5 class="card-title">
                            <i class="fas fa-bus me-2"></i>{bus['bus_name']}
                        </h5>
                        <p class="card-text">
                            <strong>Route:</strong> {bus['route_name']}<br>
                            <strong>Departure:</strong> {bus['departure_time']}<br>
                            <strong>Date:</strong> {travel_date}<br>
                            <strong>Available Seats:</strong> {available_seats}/{bus['total_seats']}
                        </p>
                        <div class="d-flex justify-content-between">
                            <a href="/bus/{bus['id']}?date={travel_date}" class="btn btn-primary">
                                <i class="fas fa-eye me-1"></i>View Details
                            </a>
                            <a href="/book/{bus['id']}?date={travel_date}" class="btn btn-success">
                                <i class="fas fa-ticket-alt me-1"></i>Book Now
                            </a>
                        </div>
                    </div>
                </div>
            </div>
            """
        
        content = f"""
        <div class="container mt-5">
            <h2 class="mb-4">
                <i class="fas fa-search me-2"></i>Search Results
            </h2>
            <p>
                <strong>From:</strong> {from_station} | 
                <strong>To:</strong> {to_station} | 
                <strong>Date:</strong> {travel_date}
            </p>
            
            <div class="row">
                {buses_html if buses_html else '''
                <div class="col-12">
                    <div class="alert alert-info">
                        No buses available for the selected date. Please try a different date.
                    </div>
                </div>
                '''}
            </div>
            
            <div class="mt-4">
                <a href="/" class="btn btn-outline-primary">
                    <i class="fas fa-arrow-left me-1"></i>Back to Search
                </a>
            </div>
        </div>
        """
        
        return render_template_string(
            BASE_HTML.replace("{% block content %}{% endblock %}", content)
        )
        
    except Exception as e:
        return f"<div class='container mt-5'><div class='alert alert-danger'>Error: {str(e)}</div></div>"

@app.route("/bus/<int:bus_id>")
def bus_details(bus_id):
    """Bus details page"""
    travel_date = request.args.get("date", date.today().isoformat())
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get bus details
        cur.execute("""
            SELECT s.*, r.route_name, r.distance_km,
                   rs1.station_name as from_station,
                   rs2.station_name as to_station
            FROM schedules s
            JOIN routes r ON s.route_id = r.id
            LEFT JOIN route_stations rs1 ON r.id = rs1.route_id AND rs1.station_order = 1
            LEFT JOIN route_stations rs2 ON r.id = rs2.route_id AND rs2.station_order = 2
            WHERE s.id = %s
        """, (bus_id,))
        
        bus = cur.fetchone()
        
        if not bus:
            return "Bus not found", 404
        
        # Get booked seats
        cur.execute("""
            SELECT seat_number 
            FROM seat_bookings 
            WHERE schedule_id = %s 
              AND travel_date = %s 
              AND status = 'confirmed'
        """, (bus_id, travel_date))
        
        booked_seats = {row['seat_number'] for row in cur.fetchall()}
        
        # Generate seat layout
        seats_html = ""
        for seat in range(1, 41):
            if seat in booked_seats:
                seats_html += f'<div class="seat booked">{seat}</div>'
            else:
                seats_html += f'<div class="seat available" onclick="selectSeat({seat})">{seat}</div>'
        
        cur.close()
        conn.close()
        
        content = f"""
        <div class="container mt-5">
            <h2 class="mb-4">
                <i class="fas fa-bus me-2"></i>{bus['bus_name']}
            </h2>
            
            <div class="row">
                <div class="col-md-6">
                    <div class="card">
                        <div class="card-body">
                            <h5 class="card-title">Bus Details</h5>
                            <p><strong>Route:</strong> {bus['route_name']}</p>
                            <p><strong>From:</strong> {bus['from_station'] or 'N/A'}</p>
                            <p><strong>To:</strong> {bus['to_station'] or 'N/A'}</p>
                            <p><strong>Distance:</strong> {bus['distance_km']} km</p>
                            <p><strong>Departure:</strong> {bus['departure_time']}</p>
                            <p><strong>Travel Date:</strong> {travel_date}</p>
                            <p><strong>Total Seats:</strong> {bus['total_seats']}</p>
                        </div>
                    </div>
                    
                    <div class="card mt-3">
                        <div class="card-body">
                            <h5 class="card-title">Live Location</h5>
                            <div id="map" class="map-container"></div>
                            <p class="mt-2">
                                <small>Lat: {bus['current_lat']}, Lng: {bus['current_lng']}</small>
                            </p>
                        </div>
                    </div>
                </div>
                
                <div class="col-md-6">
                    <div class="card">
                        <div class="card-body">
                            <h5 class="card-title">Select Seat</h5>
                            <p>Available seats are shown in green. Click to select.</p>
                            
                            <div class="mb-3">
                                <div style="display: flex; flex-wrap: wrap;">
                                    {seats_html}
                                </div>
                            </div>
                            
                            <div id="selectedSeatInfo" class="alert alert-info" style="display: none;">
                                <h6>Selected Seat: <span id="selectedSeatNumber">-</span></h6>
                                <button id="bookButton" class="btn btn-success" onclick="bookTicket()">
                                    <i class="fas fa-ticket-alt me-1"></i>Book This Seat
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <script>
            let selectedSeat = null;
            
            function selectSeat(seatNumber) {{
                selectedSeat = seatNumber;
                document.getElementById('selectedSeatNumber').textContent = seatNumber;
                document.getElementById('selectedSeatInfo').style.display = 'block';
                
                // Update seat colors
                document.querySelectorAll('.seat').forEach(seat => {{
                    seat.classList.remove('selected');
                }});
                event.target.classList.add('selected');
            }}
            
            function bookTicket() {{
                if (!selectedSeat) {{
                    alert('Please select a seat first');
                    return;
                }}
                
                const passengerName = prompt('Enter passenger name:');
                if (!passengerName) return;
                
                const mobile = prompt('Enter mobile number:');
                if (!mobile) return;
                
                // Submit booking
                fetch('/api/book', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        bus_id: {bus_id},
                        seat_number: selectedSeat,
                        passenger_name: passengerName,
                        mobile: mobile,
                        travel_date: '{travel_date}',
                        fare: 300
                    }})
                }})
                .then(response => response.json())
                .then(data => {{
                    if (data.success) {{
                        alert('Booking successful! Booking ID: ' + data.booking_id);
                        window.location.reload();
                    }} else {{
                        alert('Error: ' + data.error);
                    }}
                }})
                .catch(error => {{
                    alert('Error: ' + error);
                }});
            }}
            
            // Initialize map
            function initMap() {{
                const map = L.map('map').setView([{bus['current_lat']}, {bus['current_lng']}], 13);
                
                L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
                    attribution: '© OpenStreetMap'
                }}).addTo(map);
                
                L.marker([{bus['current_lat']}, {bus['current_lng']}])
                    .addTo(map)
                    .bindPopup('{bus['bus_name']}<br>Current Location')
                    .openPopup();
            }}
            
            // Load Leaflet CSS and JS
            const link = document.createElement('link');
            link.rel = 'stylesheet';
            link.href = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';
            document.head.appendChild(link);
            
            const script = document.createElement('script');
            script.src = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';
            script.onload = initMap;
            document.head.appendChild(script);
        </script>
        """
        
        return render_template_string(
            BASE_HTML.replace("{% block content %}{% endblock %}", content)
        )
        
    except Exception as e:
        return f"<div class='container mt-5'><div class='alert alert-danger'>Error: {str(e)}</div></div>"

# ================= API ROUTES =================
@app.route("/api/dashboard-stats")
def dashboard_stats():
    """Get dashboard statistics"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Total active buses
        cur.execute("SELECT COUNT(*) as count FROM schedules")
        total_buses = cur.fetchone()['count']
        
        # Today's bookings
        cur.execute("""
            SELECT COUNT(*) as count 
            FROM seat_bookings 
            WHERE DATE(created_at) = CURRENT_DATE
        """)
        today_bookings = cur.fetchone()['count']
        
        # Available seats
        cur.execute("""
            SELECT SUM(s.total_seats) - COUNT(b.id) as available
            FROM schedules s
            LEFT JOIN seat_bookings b ON s.id = b.schedule_id 
                AND b.travel_date = CURRENT_DATE 
                AND b.status = 'confirmed'
        """)
        available_seats = cur.fetchone()['available'] or 0
        
        cur.close()
        conn.close()
        
        return jsonify({
            "success": True,
            "total_buses": total_buses,
            "today_bookings": today_bookings,
            "available_seats": available_seats
        })
        
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/api/book", methods=["POST"])
def api_book_seat():
    """API to book a seat"""
    try:
        data = request.json
        bus_id = data.get("bus_id")
        seat_number = data.get("seat_number")
        passenger_name = data.get("passenger_name")
        mobile = data.get("mobile")
        travel_date = data.get("travel_date")
        fare = data.get("fare", 300)
        
        # Validate
        if not all([bus_id, seat_number, passenger_name, mobile, travel_date]):
            return jsonify({"success": False, "error": "Missing required fields"})
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Check if seat is already booked
        cur.execute("""
            SELECT id FROM seat_bookings 
            WHERE schedule_id = %s 
              AND seat_number = %s 
              AND travel_date = %s 
              AND status = 'confirmed'
        """, (bus_id, seat_number, travel_date))
        
        if cur.fetchone():
            cur.close()
            conn.close()
            return jsonify({"success": False, "error": "Seat already booked"})
        
        # Get bus details for stations
        cur.execute("""
            SELECT r.id as route_id
            FROM schedules s
            JOIN routes r ON s.route_id = r.id
            WHERE s.id = %s
        """, (bus_id,))
        
        bus = cur.fetchone()
        
        # Get stations
        cur.execute("""
            SELECT station_name 
            FROM route_stations 
            WHERE route_id = %s 
            ORDER BY station_order
        """, (bus['route_id'],))
        
        stations = cur.fetchall()
        from_station = stations[0]['station_name'] if len(stations) > 0 else ""
        to_station = stations[-1]['station_name'] if len(stations) > 1 else ""
        
        # Insert booking
        cur.execute("""
            INSERT INTO seat_bookings 
            (schedule_id, seat_number, passenger_name, mobile, 
             from_station, to_station, travel_date, fare, status,
             booked_by_id, booked_by_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'confirmed',
                    %s, %s)
            RETURNING id
        """, (
            bus_id, seat_number, passenger_name, mobile,
            from_station, to_station, travel_date, fare,
            session.get("user_id", 0),
            session.get("role", "user")
        ))
        
        booking_id = cur.fetchone()['id']
        conn.commit()
        
        # Notify via Socket.IO
        socketio.emit("seat_update", {
            "bus_id": bus_id,
            "seat_number": seat_number,
            "travel_date": travel_date,
            "action": "booked"
        })
        
        cur.close()
        conn.close()
        
        return jsonify({
            "success": True,
            "booking_id": booking_id,
            "message": "Seat booked successfully"
        })
        
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/api/bookings")
def api_get_bookings():
    """Get user's bookings"""
    user_id = session.get("user_id")
    
    if not user_id:
        return jsonify({"success": False, "error": "Not logged in"})
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT b.*, s.bus_name, r.route_name,
                   s.departure_time
            FROM seat_bookings b
            JOIN schedules s ON b.schedule_id = s.id
            JOIN routes r ON s.route_id = r.id
            WHERE b.booked_by_id = %s
            ORDER BY b.created_at DESC
            LIMIT 20
        """, (user_id,))
        
        bookings = cur.fetchall()
        cur.close()
        conn.close()
        
        return jsonify({
            "success": True,
            "bookings": bookings
        })
        
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/logout")
def logout():
    """Logout user"""
    session.clear()
    return redirect("/")

@app.route("/health")
def health_check():
    """Health check endpoint"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.close()
        
        return jsonify({
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "database": "connected"
        })
    except Exception as e:
        return jsonify({
            "status": "unhealthy",
            "error": str(e)
        }), 500

# ================= ERROR HANDLERS =================
@app.errorhandler(404)
def not_found(error):
    return """
    <div class="container mt-5 text-center">
        <h1>404 - Page Not Found</h1>
        <p>The page you are looking for does not exist.</p>
        <a href="/" class="btn btn-primary">Go to Homepage</a>
    </div>
    """, 404

@app.errorhandler(500)
def server_error(error):
    return """
    <div class="container mt-5 text-center">
        <h1>500 - Server Error</h1>
        <p>Something went wrong on our server. Please try again later.</p>
        <a href="/" class="btn btn-primary">Go to Homepage</a>
    </div>
    """, 500

# ================= MAIN =================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"""
    🚀 Smart Bus Booking System Starting...
    📍 Port: {port}
    🗄️  Database: Supabase PostgreSQL
    🌐 WebSocket: Enabled
    🔧 Debug: {'Enabled' if os.environ.get('FLASK_DEBUG') else 'Disabled'}
    """)
    
    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        debug=os.environ.get("FLASK_DEBUG") == "1",
        allow_unsafe_werkzeug=True
    )