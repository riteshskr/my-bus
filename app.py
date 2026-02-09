"""
🚍 Bus Booking System with Supabase PostgreSQL
Deploy on Render.com - FIXED VERSION
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
        print("❌ SUPABASE_DB_URL environment variable is missing!")
        # Fallback for local testing
        return psycopg2.connect(
            host="localhost",
            database="postgres",
            user="postgres",
            password="password",
            cursor_factory=RealDictCursor
        )
    
    # Fix for Render's IPv6 issue - force PostgreSQL protocol
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://")
    
    print(f"🔗 Connecting to database...")
    
    try:
        conn = psycopg2.connect(
            database_url,
            cursor_factory=RealDictCursor,
            connect_timeout=10
        )
        print("✅ Database connected successfully!")
        return conn
    except Exception as e:
        print(f"❌ Database connection error: {e}")
        # Try alternative connection
        try:
            # Parse the URL and reconstruct
            from urllib.parse import urlparse
            parsed = urlparse(database_url)
            
            conn = psycopg2.connect(
                host=parsed.hostname,
                port=parsed.port or 5432,
                database=parsed.path[1:] if parsed.path else "postgres",
                user=parsed.username,
                password=parsed.password,
                cursor_factory=RealDictCursor,
                connect_timeout=10
            )
            print("✅ Database connected via parsed URL!")
            return conn
        except Exception as e2:
            print(f"❌ Alternative connection also failed: {e2}")
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
            try:
                cur.execute(table_sql)
            except Exception as e:
                print(f"⚠️ Table creation warning: {e}")
        
        # Check if tables are empty and insert sample data
        cur.execute("SELECT COUNT(*) as count FROM admins")
        admin_count = cur.fetchone()['count']
        
        if admin_count == 0:
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
                try:
                    cur.execute("""
                        INSERT INTO routes (route_name, distance_km)
                        VALUES (%s, %s)
                        ON CONFLICT (route_name) DO NOTHING
                    """, (route_name, distance))
                except:
                    pass
        
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
try:
    init_database()
except Exception as e:
    print(f"⚠️ Database init warning: {e}")

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
BASE_HTML = '''
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
            font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
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
        
        .hero {
            background: linear-gradient(rgba(74, 107, 255, 0.9), rgba(74, 107, 255, 0.7)),
                        url("https://images.unsplash.com/photo-1544620347-c4fd4a3d5957");
            background-size: cover;
            color: white;
            padding: 80px 20px;
            text-align: center;
            margin-bottom: 40px;
        }
        
        .search-box {
            background: white;
            padding: 25px;
            border-radius: 15px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.1);
            max-width: 800px;
            margin: 0 auto 40px;
        }
        
        .btn-primary {
            background: #4a6bff;
            border: none;
            padding: 10px 25px;
            border-radius: 8px;
        }
        
        .card {
            border-radius: 10px;
            box-shadow: 0 5px 15px rgba(0,0,0,0.05);
            margin-bottom: 20px;
        }
        
        .seat {
            width: 45px;
            height: 45px;
            margin: 5px;
            border-radius: 8px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-weight: 600;
            cursor: pointer;
        }
        
        .seat.available {
            background: #28a745;
            color: white;
        }
        
        .seat.booked {
            background: #dc3545;
            color: white;
            cursor: not-allowed;
        }
        
        footer {
            background: #333;
            color: white;
            padding: 30px 0;
            margin-top: 50px;
        }
    </style>
</head>
<body>
    <!-- Navigation -->
    <nav class="navbar">
        <div class="container">
            <a class="logo" href="/">
                <i class="fas fa-bus"></i> SmartBus
            </a>
            <div>
                <a href="/" class="btn btn-outline-primary btn-sm me-2">Home</a>
                <a href="/login" class="btn btn-outline-primary btn-sm me-2">Admin</a>
                <a href="/dashboard" class="btn btn-primary btn-sm">Dashboard</a>
            </div>
        </div>
    </nav>

    <!-- Main Content -->
    {{ content|safe }}

    <!-- Footer -->
    <footer>
        <div class="container text-center">
            <p>© 2024 SmartBus Booking System</p>
        </div>
    </footer>

    <!-- Scripts -->
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
    
    <script>
        const socket = io();
        socket.on("connect", () => {
            console.log("Connected to server");
        });
    </script>
    
    {{ scripts|safe }}
</body>
</html>
'''

LOGIN_HTML = '''
<div class="container">
    <div class="row justify-content-center">
        <div class="col-md-5">
            <div class="card mt-5">
                <div class="card-body p-4">
                    <h3 class="text-center mb-4">Login</h3>
                    
                    {% if error %}
                    <div class="alert alert-danger">{{ error }}</div>
                    {% endif %}
                    
                    <form method="POST">
                        <div class="mb-3">
                            <label>Username</label>
                            <input type="text" name="username" class="form-control" required>
                        </div>
                        
                        <div class="mb-3">
                            <label>Password</label>
                            <input type="password" name="password" class="form-control" required>
                        </div>
                        
                        <button type="submit" class="btn btn-primary w-100">Login</button>
                    </form>
                    
                    <div class="text-center mt-3">
                        <a href="/">Back to Home</a>
                    </div>
                </div>
            </div>
        </div>
    </div>
</div>
'''

# ================= ROUTES =================
@app.route("/")
def home():
    """Home page"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get routes
        cur.execute("SELECT id, route_name, distance_km FROM routes ORDER BY id LIMIT 6")
        routes = cur.fetchall()
        
        # Get stations
        cur.execute("SELECT DISTINCT station_name FROM route_stations ORDER BY station_name")
        stations = [r['station_name'] for r in cur.fetchall()]
        
        cur.close()
        conn.close()
        
        today = date.today().isoformat()
        
        # Build HTML content
        routes_html = ""
        for route in routes:
            routes_html += f'''
            <div class="col-md-4">
                <div class="card">
                    <div class="card-body">
                        <h5>{route["route_name"]}</h5>
                        <p>{route["distance_km"]} km</p>
                        <a href="/search?route={route["id"]}" class="btn btn-sm btn-primary">View Buses</a>
                    </div>
                </div>
            </div>
            '''
        
        stations_options = ""
        for station in stations:
            stations_options += f'<option value="{station}">'
        
        content = f'''
        <div class="hero">
            <div class="container">
                <h1>Book Bus Tickets Online</h1>
                <p>Safe and comfortable bus travel</p>
            </div>
        </div>
        
        <div class="container">
            <!-- Search Box -->
            <div class="search-box">
                <h3 class="text-center mb-4">Find Your Bus</h3>
                <form action="/search" method="POST">
                    <div class="row g-3">
                        <div class="col-md-4">
                            <input type="text" name="from" class="form-control" placeholder="From" list="stations" required>
                        </div>
                        <div class="col-md-4">
                            <input type="text" name="to" class="form-control" placeholder="To" list="stations" required>
                        </div>
                        <div class="col-md-3">
                            <input type="date" name="date" class="form-control" value="{today}" required>
                        </div>
                        <div class="col-md-1">
                            <button type="submit" class="btn btn-primary w-100">
                                <i class="fas fa-search"></i>
                            </button>
                        </div>
                    </div>
                </form>
                <datalist id="stations">{stations_options}</datalist>
            </div>
            
            <!-- Popular Routes -->
            <h3 class="text-center mb-4">Popular Routes</h3>
            <div class="row">
                {routes_html if routes_html else "<p class='text-center'>No routes available</p>"}
            </div>
            
            <!-- Info -->
            <div class="row mt-5 text-center">
                <div class="col-md-4">
                    <i class="fas fa-shield-alt fa-2x text-primary mb-2"></i>
                    <h5>Safe Travel</h5>
                </div>
                <div class="col-md-4">
                    <i class="fas fa-bolt fa-2x text-primary mb-2"></i>
                    <h5>Live Tracking</h5>
                </div>
                <div class="col-md-4">
                    <i class="fas fa-headset fa-2x text-primary mb-2"></i>
                    <h5>24/7 Support</h5>
                </div>
            </div>
        </div>
        '''
        
        return render_template_string(BASE_HTML, content=content)
        
    except Exception as e:
        error_content = f'''
        <div class="container mt-5">
            <div class="alert alert-danger">
                <h4>Error Loading Page</h4>
                <p>{str(e)}</p>
                <a href="/" class="btn btn-primary">Reload</a>
            </div>
        </div>
        '''
        return render_template_string(BASE_HTML, content=error_content)

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
                return render_template_string(BASE_HTML, content=render_template_string(LOGIN_HTML, error="Invalid username or password"))
                
        except Exception as e:
            return render_template_string(BASE_HTML, content=render_template_string(LOGIN_HTML, error=f"Server error: {str(e)}"))
    
    return render_template_string(BASE_HTML, content=render_template_string(LOGIN_HTML))

@app.route("/dashboard")
def dashboard():
    """User dashboard"""
    if not session.get("logged_in"):
        return redirect("/login")
    
    user_role = session.get("role", "user")
    username = session.get("username", "User")
    
    # Admin actions HTML
    admin_actions = ""
    if user_role == "admin":
        admin_actions = '''
        <div class="mt-4">
            <h5>Admin Actions</h5>
            <div class="d-flex flex-wrap gap-2">
                <a href="#" class="btn btn-outline-primary">Manage Routes</a>
                <a href="#" class="btn btn-outline-primary">Manage Schedules</a>
                <a href="#" class="btn btn-outline-primary">Manage Users</a>
            </div>
        </div>
        '''
    
    content = f'''
    <div class="container mt-5">
        <div class="row">
            <div class="col-md-3">
                <div class="card">
                    <div class="card-body text-center">
                        <h5>Welcome</h5>
                        <h3>{username}</h3>
                        <p>Role: {user_role}</p>
                    </div>
                </div>
                
                <div class="card mt-3">
                    <div class="card-body">
                        <h6>Quick Actions</h6>
                        <div class="list-group">
                            <a href="/" class="list-group-item list-group-item-action">Home</a>
                            <a href="/buses" class="list-group-item list-group-item-action">View Buses</a>
                            <a href="/logout" class="list-group-item list-group-item-action text-danger">Logout</a>
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="col-md-9">
                <div class="card">
                    <div class="card-body">
                        <h4>Dashboard</h4>
                        
                        <div class="row mt-4">
                            <div class="col-md-4">
                                <div class="card bg-primary text-white">
                                    <div class="card-body text-center">
                                        <h3 id="totalBuses">0</h3>
                                        <p>Active Buses</p>
                                    </div>
                                </div>
                            </div>
                            <div class="col-md-4">
                                <div class="card bg-success text-white">
                                    <div class="card-body text-center">
                                        <h3 id="totalBookings">0</h3>
                                        <p>Today's Bookings</p>
                                    </div>
                                </div>
                            </div>
                            <div class="col-md-4">
                                <div class="card bg-info text-white">
                                    <div class="card-body text-center">
                                        <h3 id="availableSeats">0</h3>
                                        <p>Available Seats</p>
                                    </div>
                                </div>
                            </div>
                        </div>
                        
                        {admin_actions}
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <script>
        // Load dashboard stats
        fetch('/api/stats')
            .then(r => r.json())
            .then(data => {{
                if (data.success) {{
                    document.getElementById('totalBuses').textContent = data.total_buses || 0;
                    document.getElementById('totalBookings').textContent = data.today_bookings || 0;
                    document.getElementById('availableSeats').textContent = data.available_seats || 0;
                }}
            }});
    </script>
    '''
    
    return render_template_string(BASE_HTML, content=content)

@app.route("/search", methods=["GET", "POST"])
def search_buses():
    """Search buses"""
    if request.method == "POST":
        from_station = request.form.get("from", "").strip()
        to_station = request.form.get("to", "").strip()
        travel_date = request.form.get("date", date.today().isoformat())
    else:
        from_station = request.args.get("from", "")
        to_station = request.args.get("to", "")
        travel_date = request.args.get("date", date.today().isoformat())
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Simple search - just get all buses
        cur.execute("""
            SELECT s.*, r.route_name, r.distance_km
            FROM schedules s
            JOIN routes r ON s.route_id = r.id
            ORDER BY s.departure_time
            LIMIT 10
        """)
        
        buses = cur.fetchall()
        cur.close()
        conn.close()
        
        buses_html = ""
        for bus in buses:
            buses_html += f'''
            <div class="col-md-6">
                <div class="card">
                    <div class="card-body">
                        <h5>{bus["bus_name"]}</h5>
                        <p>Route: {bus["route_name"]}</p>
                        <p>Departure: {bus["departure_time"]}</p>
                        <a href="/bus/{bus["id"]}" class="btn btn-primary">View Details</a>
                    </div>
                </div>
            </div>
            '''
        
        content = f'''
        <div class="container mt-5">
            <h2>Search Results</h2>
            <p>From: {from_station or "Any"} | To: {to_station or "Any"} | Date: {travel_date}</p>
            
            <div class="row mt-4">
                {buses_html if buses_html else "<p class='text-center'>No buses found</p>"}
            </div>
            
            <div class="mt-4">
                <a href="/" class="btn btn-outline-primary">Back to Search</a>
            </div>
        </div>
        '''
        
        return render_template_string(BASE_HTML, content=content)
        
    except Exception as e:
        error_content = f'''
        <div class="container mt-5">
            <div class="alert alert-danger">
                <h4>Search Error</h4>
                <p>{str(e)}</p>
                <a href="/" class="btn btn-primary">Back to Home</a>
            </div>
        </div>
        '''
        return render_template_string(BASE_HTML, content=error_content)

@app.route("/bus/<int:bus_id>")
def bus_details(bus_id):
    """Bus details page"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get bus details
        cur.execute("""
            SELECT s.*, r.route_name, r.distance_km
            FROM schedules s
            JOIN routes r ON s.route_id = r.id
            WHERE s.id = %s
        """, (bus_id,))
        
        bus = cur.fetchone()
        
        if not bus:
            return render_template_string(BASE_HTML, content="<div class='container mt-5'><div class='alert alert-warning'>Bus not found</div></div>")
        
        # Generate seat layout (simple version)
        seats_html = ""
        for seat in range(1, 41):
            seats_html += f'<div class="seat available" onclick="selectSeat({seat})">{seat}</div>'
        
        cur.close()
        conn.close()
        
        content = f'''
        <div class="container mt-5">
            <h2>{bus["bus_name"]}</h2>
            <p>Route: {bus["route_name"]} | Distance: {bus["distance_km"]} km</p>
            
            <div class="row">
                <div class="col-md-6">
                    <div class="card">
                        <div class="card-body">
                            <h5>Bus Details</h5>
                            <p>Departure: {bus["departure_time"]}</p>
                            <p>Total Seats: {bus["total_seats"]}</p>
                            <p>Location: {bus["current_lat"]}, {bus["current_lng"]}</p>
                        </div>
                    </div>
                </div>
                
                <div class="col-md-6">
                    <div class="card">
                        <div class="card-body">
                            <h5>Select Seat</h5>
                            <div style="display: flex; flex-wrap: wrap;">
                                {seats_html}
                            </div>
                            <div id="seatInfo" class="mt-3" style="display: none;">
                                <p>Selected Seat: <span id="selectedSeat">-</span></p>
                                <button onclick="bookTicket()" class="btn btn-success">Book Now</button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <script>
            let selectedSeat = null;
            
            function selectSeat(seat) {{
                selectedSeat = seat;
                document.getElementById("selectedSeat").textContent = seat;
                document.getElementById("seatInfo").style.display = "block";
            }}
            
            function bookTicket() {{
                if (!selectedSeat) {{
                    alert("Please select a seat first");
                    return;
                }}
                
                const name = prompt("Enter passenger name:");
                if (!name) return;
                
                const mobile = prompt("Enter mobile number:");
                if (!mobile) return;
                
                fetch("/api/book", {{
                    method: "POST",
                    headers: {{ "Content-Type": "application/json" }},
                    body: JSON.stringify({{
                        bus_id: {bus_id},
                        seat: selectedSeat,
                        name: name,
                        mobile: mobile
                    }})
                }})
                .then(r => r.json())
                .then(data => {{
                    if (data.success) {{
                        alert("Booking successful! ID: " + data.booking_id);
                    }} else {{
                        alert("Error: " + data.error);
                    }}
                }});
            }}
        </script>
        '''
        
        return render_template_string(BASE_HTML, content=content)
        
    except Exception as e:
        error_content = f'''
        <div class="container mt-5">
            <div class="alert alert-danger">
                <h4>Error</h4>
                <p>{str(e)}</p>
            </div>
        </div>
        '''
        return render_template_string(BASE_HTML, content=error_content)

# ================= API ROUTES =================
@app.route("/api/stats")
def api_stats():
    """Get dashboard statistics"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Total buses
        cur.execute("SELECT COUNT(*) as count FROM schedules")
        total_buses = cur.fetchone()['count']
        
        # Today's bookings
        cur.execute("SELECT COUNT(*) as count FROM seat_bookings WHERE DATE(created_at) = CURRENT_DATE")
        today_bookings = cur.fetchone()['count']
        
        # Available seats (simplified)
        cur.execute("SELECT SUM(total_seats) as total FROM schedules")
        total_seats = cur.fetchone()['total'] or 0
        available_seats = total_seats - today_bookings * 1  # Simplified calculation
        
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
def api_book():
    """Book a seat"""
    try:
        data = request.json
        bus_id = data.get("bus_id")
        seat = data.get("seat")
        name = data.get("name")
        mobile = data.get("mobile")
        
        if not all([bus_id, seat, name, mobile]):
            return jsonify({"success": False, "error": "Missing fields"})
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Simple booking
        cur.execute("""
            INSERT INTO seat_bookings 
            (schedule_id, seat_number, passenger_name, mobile, travel_date, fare)
            VALUES (%s, %s, %s, %s, CURRENT_DATE, 300)
            RETURNING id
        """, (bus_id, seat, name, mobile))
        
        booking_id = cur.fetchone()['id']
        conn.commit()
        
        # Notify via socket
        socketio.emit("seat_update", {
            "bus_id": bus_id,
            "seat": seat,
            "action": "booked"
        })
        
        cur.close()
        conn.close()
        
        return jsonify({
            "success": True,
            "booking_id": booking_id,
            "message": "Booking successful"
        })
        
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/logout")
def logout():
    """Logout user"""
    session.clear()
    return redirect("/")

@app.route("/health")
def health():
    """Health check"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.close()
        return jsonify({"status": "ok", "database": "connected"})
    except:
        return jsonify({"status": "error", "database": "disconnected"}), 500

# ================= MAIN =================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"""
    🚀 Bus Booking System Starting...
    📍 Port: {port}
    🔧 Ready for deployment
    """)
    
    # For Render.com
    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        debug=False,
        allow_unsafe_werkzeug=True
    )