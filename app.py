"""
🚍 Bus Booking System
Render.com + Supabase PostgreSQL
Working Version
"""

import os
import time
from datetime import date, datetime
from flask import Flask, request, jsonify, render_template_string, redirect, session
from flask_socketio import SocketIO, emit
import psycopg2
from psycopg2.extras import RealDictCursor

# ================= INITIALIZE APP =================
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "bus-booking-secret-key-123456")

# SocketIO for real-time updates
socketio = SocketIO(app, cors_allowed_origins="*", logger=False, engineio_logger=False)

# ================= DATABASE FUNCTIONS =================
def get_db_connection():
    """Create database connection"""
    database_url = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
    
    if not database_url:
        print("⚠️ No database URL found")
        # Return None if no database URL (app will still work with basic features)
        return None
    
    # Fix for IPv6 issue - force postgresql:// protocol
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    
    try:
        conn = psycopg2.connect(
            database_url,
            cursor_factory=RealDictCursor,
            connect_timeout=5
        )
        return conn
    except Exception as e:
        print(f"❌ Database connection error: {e}")
        return None

def init_database():
    """Initialize database tables"""
    conn = get_db_connection()
    if not conn:
        print("⚠️ Skipping database initialization (no connection)")
        return
    
    try:
        cur = conn.cursor()
        
        # Create tables
        tables = [
            # Users table
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(50) UNIQUE NOT NULL,
                password VARCHAR(100) NOT NULL,
                full_name VARCHAR(100),
                email VARCHAR(100),
                phone VARCHAR(15),
                role VARCHAR(20) DEFAULT 'user',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """,
            
            # Routes table
            """
            CREATE TABLE IF NOT EXISTS routes (
                id SERIAL PRIMARY KEY,
                name VARCHAR(200) NOT NULL,
                from_city VARCHAR(100) NOT NULL,
                to_city VARCHAR(100) NOT NULL,
                distance_km INTEGER DEFAULT 100,
                duration_hours INTEGER DEFAULT 2,
                fare INTEGER DEFAULT 300
            );
            """,
            
            # Buses table
            """
            CREATE TABLE IF NOT EXISTS buses (
                id SERIAL PRIMARY KEY,
                bus_number VARCHAR(20) UNIQUE NOT NULL,
                bus_name VARCHAR(100) NOT NULL,
                bus_type VARCHAR(50) DEFAULT 'AC Sleeper',
                total_seats INTEGER DEFAULT 40,
                amenities TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """,
            
            # Schedules table
            """
            CREATE TABLE IF NOT EXISTS schedules (
                id SERIAL PRIMARY KEY,
                route_id INTEGER REFERENCES routes(id),
                bus_id INTEGER REFERENCES buses(id),
                departure_time TIME NOT NULL,
                arrival_time TIME NOT NULL,
                travel_date DATE NOT NULL,
                available_seats INTEGER DEFAULT 40,
                status VARCHAR(20) DEFAULT 'scheduled',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """,
            
            # Bookings table
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id SERIAL PRIMARY KEY,
                schedule_id INTEGER REFERENCES schedules(id),
                user_id INTEGER REFERENCES users(id),
                seat_numbers VARCHAR(50) NOT NULL,
                passenger_name VARCHAR(100) NOT NULL,
                passenger_age INTEGER,
                passenger_gender VARCHAR(10),
                contact_phone VARCHAR(15) NOT NULL,
                contact_email VARCHAR(100),
                total_fare INTEGER NOT NULL,
                payment_status VARCHAR(20) DEFAULT 'pending',
                booking_status VARCHAR(20) DEFAULT 'confirmed',
                booking_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                pnr_number VARCHAR(20) UNIQUE
            );
            """,
            
            # Payments table
            """
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                booking_id INTEGER REFERENCES bookings(id),
                amount INTEGER NOT NULL,
                payment_method VARCHAR(50),
                transaction_id VARCHAR(100),
                status VARCHAR(20) DEFAULT 'pending',
                payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        ]
        
        for table_sql in tables:
            try:
                cur.execute(table_sql)
            except Exception as e:
                print(f"⚠️ Table creation warning: {e}")
        
        # Insert default admin user
        cur.execute("SELECT COUNT(*) as count FROM users WHERE role = 'admin'")
        if cur.fetchone()['count'] == 0:
            cur.execute("""
                INSERT INTO users (username, password, full_name, email, role)
                VALUES ('admin', 'admin123', 'Administrator', 'admin@bus.com', 'admin')
                ON CONFLICT (username) DO NOTHING
            """)
        
        # Insert sample routes if empty
        cur.execute("SELECT COUNT(*) as count FROM routes")
        if cur.fetchone()['count'] == 0:
            sample_routes = [
                ('Delhi-Jaipur Express', 'Delhi', 'Jaipur', 280, 5, 450),
                ('Mumbai-Pune Express', 'Mumbai', 'Pune', 150, 3, 350),
                ('Bangalore-Chennai Express', 'Bangalore', 'Chennai', 350, 6, 550),
                ('Delhi-Mumbai Superfast', 'Delhi', 'Mumbai', 1400, 18, 1200),
                ('Kolkata-Patna Express', 'Kolkata', 'Patna', 550, 10, 650)
            ]
            for route in sample_routes:
                cur.execute("""
                    INSERT INTO routes (name, from_city, to_city, distance_km, duration_hours, fare)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                """, route)
        
        # Insert sample buses
        cur.execute("SELECT COUNT(*) as count FROM buses")
        if cur.fetchone()['count'] == 0:
            sample_buses = [
                ('DL01AB1234', 'Volvo AC Sleeper', 'AC Sleeper', 40, 'WiFi, Charging, Blanket, Water'),
                ('MH02CD5678', 'Mercedes Luxury', 'Luxury Coach', 32, 'WiFi, TV, Charging, Snacks, Blanket'),
                ('KA03EF9012', 'Scania Multi-Axle', 'AC Seater', 50, 'Charging, Water'),
                ('WB04GH3456', 'Tata Marcopolo', 'Non-AC Seater', 52, 'Water'),
                ('RJ05IJ7890', 'Volvo Multi-Axle', 'AC Sleeper', 36, 'WiFi, Charging, Blanket, Pillow, Water')
            ]
            for bus in sample_buses:
                cur.execute("""
                    INSERT INTO buses (bus_number, bus_name, bus_type, total_seats, amenities)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (bus_number) DO NOTHING
                """, bus)
        
        conn.commit()
        print("✅ Database initialized successfully!")
        
    except Exception as e:
        print(f"❌ Database initialization error: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()

# Initialize database on startup
try:
    init_database()
except Exception as e:
    print(f"⚠️ Database init warning: {e}")

# ================= SOCKET.IO EVENTS =================
@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    print(f"✅ Client connected: {request.sid}")
    emit('connected', {'message': 'Connected to server'})

@socketio.on('bus_location')
def handle_bus_location(data):
    """Update bus location"""
    bus_id = data.get('bus_id')
    lat = data.get('lat')
    lng = data.get('lng')
    
    if bus_id and lat and lng:
        print(f"📍 Bus {bus_id} location: {lat}, {lng}")
        # Broadcast to all clients
        emit('location_update', {'bus_id': bus_id, 'lat': lat, 'lng': lng}, broadcast=True)

@socketio.on('seat_booking')
def handle_seat_booking(data):
    """Handle seat booking updates"""
    schedule_id = data.get('schedule_id')
    seat_numbers = data.get('seat_numbers')
    
    if schedule_id and seat_numbers:
        print(f"🎫 Seats {seat_numbers} booked on schedule {schedule_id}")
        emit('seat_update', {
            'schedule_id': schedule_id,
            'seat_numbers': seat_numbers,
            'action': 'booked'
        }, broadcast=True)

# ================= HTML TEMPLATES =================
BASE_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🚍 SmartBus - {{ title }}</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        :root {
            --primary: #4a6bff;
            --secondary: #6c757d;
            --success: #28a745;
            --danger: #dc3545;
            --warning: #ffc107;
            --light: #f8f9fa;
            --dark: #343a40;
        }
        
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background-color: #f5f7fb;
            color: #333;
            line-height: 1.6;
        }
        
        .navbar {
            background: white;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            padding: 15px 0;
            position: sticky;
            top: 0;
            z-index: 1000;
        }
        
        .navbar-brand {
            font-size: 1.8rem;
            font-weight: 700;
            color: var(--primary);
            text-decoration: none;
        }
        
        .navbar-brand i {
            margin-right: 10px;
        }
        
        .hero {
            background: linear-gradient(rgba(74, 107, 255, 0.9), rgba(74, 107, 255, 0.8)),
                        url('https://images.unsplash.com/photo-1544620347-c4fd4a3d5957?ixlib=rb-4.0.3&auto=format&fit=crop&w=1920&q=80');
            background-size: cover;
            background-position: center;
            color: white;
            padding: 100px 20px;
            text-align: center;
            margin-bottom: 50px;
        }
        
        .hero h1 {
            font-size: 3.5rem;
            font-weight: 800;
            margin-bottom: 20px;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
        }
        
        .hero p {
            font-size: 1.3rem;
            max-width: 700px;
            margin: 0 auto 30px;
            opacity: 0.95;
        }
        
        .search-container {
            background: white;
            padding: 30px;
            border-radius: 15px;
            box-shadow: 0 15px 35px rgba(0,0,0,0.1);
            max-width: 900px;
            margin: -50px auto 50px;
            position: relative;
            z-index: 10;
        }
        
        .card {
            border: none;
            border-radius: 12px;
            box-shadow: 0 8px 20px rgba(0,0,0,0.08);
            transition: transform 0.3s, box-shadow 0.3s;
            margin-bottom: 25px;
            overflow: hidden;
        }
        
        .card:hover {
            transform: translateY(-5px);
            box-shadow: 0 15px 30px rgba(0,0,0,0.15);
        }
        
        .card-header {
            background: var(--primary);
            color: white;
            padding: 15px 20px;
            border-bottom: none;
        }
        
        .btn-primary {
            background: var(--primary);
            border: none;
            padding: 10px 25px;
            border-radius: 8px;
            font-weight: 600;
            transition: all 0.3s;
        }
        
        .btn-primary:hover {
            background: #3a5bef;
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(74, 107, 255, 0.3);
        }
        
        .btn-success {
            background: var(--success);
            border: none;
            padding: 10px 25px;
            border-radius: 8px;
            font-weight: 600;
        }
        
        .seat-map {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 10px;
            padding: 20px;
            background: #f8f9fa;
            border-radius: 10px;
        }
        
        .seat {
            width: 60px;
            height: 60px;
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 8px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            user-select: none;
        }
        
        .seat.available {
            background: #d4edda;
            color: #155724;
            border: 2px solid #c3e6cb;
        }
        
        .seat.available:hover {
            background: #c3e6cb;
            transform: scale(1.05);
        }
        
        .seat.booked {
            background: #f8d7da;
            color: #721c24;
            border: 2px solid #f5c6cb;
            cursor: not-allowed;
        }
        
        .seat.selected {
            background: var(--primary);
            color: white;
            border: 2px solid var(--primary);
        }
        
        .footer {
            background: var(--dark);
            color: white;
            padding: 40px 0;
            margin-top: 60px;
        }
        
        .stat-card {
            text-align: center;
            padding: 20px;
            border-radius: 10px;
            background: white;
            box-shadow: 0 5px 15px rgba(0,0,0,0.05);
        }
        
        .stat-card i {
            font-size: 2.5rem;
            margin-bottom: 15px;
            color: var(--primary);
        }
        
        .stat-card h3 {
            font-size: 2rem;
            font-weight: 700;
            margin-bottom: 10px;
            color: var(--dark);
        }
        
        @media (max-width: 768px) {
            .hero h1 {
                font-size: 2.2rem;
            }
            
            .hero p {
                font-size: 1.1rem;
            }
            
            .search-container {
                padding: 20px;
                margin: -30px 15px 30px;
            }
            
            .seat-map {
                grid-template-columns: repeat(3, 1fr);
            }
            
            .seat {
                width: 50px;
                height: 50px;
            }
        }
    </style>
</head>
<body>
    <!-- Navigation -->
    <nav class="navbar">
        <div class="container">
            <a class="navbar-brand" href="/">
                <i class="fas fa-bus"></i> SmartBus
            </a>
            <div class="d-flex align-items-center">
                <a href="/" class="text-decoration-none me-4">
                    <i class="fas fa-home me-1"></i> Home
                </a>
                <a href="/search" class="text-decoration-none me-4">
                    <i class="fas fa-search me-1"></i> Search
                </a>
                {% if session.user_id %}
                <a href="/dashboard" class="text-decoration-none me-4">
                    <i class="fas fa-user-circle me-1"></i> Dashboard
                </a>
                <a href="/logout" class="btn btn-outline-danger btn-sm">
                    <i class="fas fa-sign-out-alt me-1"></i> Logout
                </a>
                {% else %}
                <a href="/login" class="btn btn-primary btn-sm">
                    <i class="fas fa-sign-in-alt me-1"></i> Login
                </a>
                {% endif %}
            </div>
        </div>
    </nav>
    
    <!-- Main Content -->
    {% block content %}{% endblock %}
    
    <!-- Footer -->
    <footer class="footer">
        <div class="container">
            <div class="row">
                <div class="col-md-4">
                    <h4><i class="fas fa-bus"></i> SmartBus</h4>
                    <p>Your trusted partner for comfortable and safe bus travel.</p>
                </div>
                <div class="col-md-4">
                    <h5>Quick Links</h5>
                    <ul class="list-unstyled">
                        <li><a href="/" class="text-white text-decoration-none">Home</a></li>
                        <li><a href="/search" class="text-white text-decoration-none">Search Buses</a></li>
                        <li><a href="/about" class="text-white text-decoration-none">About Us</a></li>
                        <li><a href="/contact" class="text-white text-decoration-none">Contact</a></li>
                    </ul>
                </div>
                <div class="col-md-4">
                    <h5>Contact Info</h5>
                    <p><i class="fas fa-phone me-2"></i> +91 9876543210</p>
                    <p><i class="fas fa-envelope me-2"></i> info@smartbus.com</p>
                    <p><i class="fas fa-map-marker-alt me-2"></i> Delhi, India</p>
                </div>
            </div>
            <hr class="bg-light my-4">
            <div class="text-center">
                <p>&copy; 2024 SmartBus Booking System. All rights reserved.</p>
            </div>
        </div>
    </footer>
    
    <!-- Scripts -->
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
    <script>
        // Initialize Socket.IO
        const socket = io();
        
        socket.on('connect', () => {
            console.log('Connected to server');
        });
        
        socket.on('location_update', (data) => {
            console.log('Bus location updated:', data);
            // Update bus location on map if available
        });
        
        socket.on('seat_update', (data) => {
            console.log('Seat update:', data);
            // Update seat availability
            if (data.action === 'booked') {
                data.seat_numbers.split(',').forEach(seat => {
                    const seatEl = document.getElementById(`seat-${seat.trim()}`);
                    if (seatEl) {
                        seatEl.classList.remove('available', 'selected');
                        seatEl.classList.add('booked');
                        seatEl.onclick = null;
                    }
                });
            }
        });
        
        // Auto-refresh stats every 30 seconds
        setInterval(() => {
            if (window.location.pathname === '/dashboard') {
                fetch('/api/stats')
                    .then(r => r.json())
                    .then(data => {
                        if (data.success) {
                            if (document.getElementById('totalBuses')) {
                                document.getElementById('totalBuses').textContent = data.total_buses;
                            }
                            if (document.getElementById('totalBookings')) {
                                document.getElementById('totalBookings').textContent = data.today_bookings;
                            }
                            if (document.getElementById('totalRevenue')) {
                                document.getElementById('totalRevenue').textContent = '₹' + data.total_revenue;
                            }
                        }
                    });
            }
        }, 30000);
    </script>
    
    {% block scripts %}{% endblock %}
</body>
</html>
'''

# ================= ROUTES =================
@app.route('/')
def home():
    """Home page"""
    conn = get_db_connection()
    
    try:
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
        
        # Get unique cities
        cur.execute("""
            SELECT DISTINCT from_city as city FROM routes
            UNION
            SELECT DISTINCT to_city as city FROM routes
            ORDER BY city
        """)
        cities = [row['city'] for row in cur.fetchall()]
        
        # Get today's date
        today = date.today().isoformat()
        
        cur.close()
        
    except Exception as e:
        print(f"⚠️ Database error on home page: {e}")
        popular_routes = []
        cities = []
        today = date.today().isoformat()
    finally:
        if conn:
            conn.close()
    
    # Build cities options for datalist
    cities_options = ''
    for city in cities:
        cities_options += f'<option value="{city}">'
    
    # Build popular routes HTML
    routes_html = ''
    for route in popular_routes:
        routes_html += f'''
        <div class="col-md-4">
            <div class="card h-100">
                <div class="card-header">
                    <h5 class="mb-0">{route['from_city']} to {route['to_city']}</h5>
                </div>
                <div class="card-body">
                    <p><i class="fas fa-road me-2"></i> {route['distance_km']} km</p>
                    <p><i class="fas fa-clock me-2"></i> {route['duration_hours']} hours</p>
                    <p><i class="fas fa-rupee-sign me-2"></i> ₹{route['fare']}</p>
                    <p><i class="fas fa-bus me-2"></i> {route['bus_count']} buses available</p>
                    <a href="/search?from={route['from_city']}&to={route['to_city']}" class="btn btn-primary w-100">
                        <i class="fas fa-search me-1"></i> View Buses
                    </a>
                </div>
            </div>
        </div>
        '''
    
    if not routes_html:
        routes_html = '''
        <div class="col-12">
            <div class="alert alert-info">
                <h5>No routes available yet</h5>
                <p>Check back later or contact support.</p>
            </div>
        </div>
        '''
    
    content = f'''
    <!-- Hero Section -->
    <div class="hero">
        <div class="container">
            <h1>Book Bus Tickets Online</h1>
            <p>Safe, comfortable, and affordable bus travel across India</p>
            <a href="/search" class="btn btn-light btn-lg mt-3">
                <i class="fas fa-search me-2"></i> Search Buses
            </a>
        </div>
    </div>
    
    <!-- Search Box -->
    <div class="container">
        <div class="search-container">
            <h3 class="text-center mb-4">Find Your Bus</h3>
            <form action="/search" method="GET">
                <div class="row g-3">
                    <div class="col-md-4">
                        <label class="form-label">From City</label>
                        <input type="text" class="form-control" name="from" placeholder="Departure city" 
                               list="cities-list" required autocomplete="off">
                    </div>
                    <div class="col-md-4">
                        <label class="form-label">To City</label>
                        <input type="text" class="form-control" name="to" placeholder="Destination city" 
                               list="cities-list" required autocomplete="off">
                    </div>
                    <div class="col-md-3">
                        <label class="form-label">Travel Date</label>
                        <input type="date" class="form-control" name="date" value="{today}" 
                               min="{today}" required>
                    </div>
                    <div class="col-md-1 d-flex align-items-end">
                        <button type="submit" class="btn btn-primary w-100">
                            <i class="fas fa-search"></i>
                        </button>
                    </div>
                </div>
            </form>
            <datalist id="cities-list">
                {cities_options}
            </datalist>
        </div>
        
        <!-- Popular Routes -->
        <h2 class="text-center mb-4">Popular Bus Routes</h2>
        <div class="row">
            {routes_html}
        </div>
        
        <!-- Features -->
        <div class="row mt-5">
            <div class="col-md-4">
                <div class="stat-card">
                    <i class="fas fa-shield-alt"></i>
                    <h3>Safe Travel</h3>
                    <p>Sanitized buses with trained staff and emergency protocols</p>
                </div>
            </div>
            <div class="col-md-4">
                <div class="stat-card">
                    <i class="fas fa-bolt"></i>
                    <h3>Live Tracking</h3>
                    <p>Real-time GPS tracking of your bus</p>
                </div>
            </div>
            <div class="col-md-4">
                <div class="stat-card">
                    <i class="fas fa-headset"></i>
                    <h3>24/7 Support</h3>
                    <p>Customer support available round the clock</p>
                </div>
            </div>
        </div>
    </div>
    '''
    
    return render_template_string(BASE_TEMPLATE, title='Home - SmartBus', content=content)

@app.route('/login', methods=['GET', 'POST'])
def login():
    """User login"""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        conn = get_db_connection()
        if not conn:
            return render_template_string(BASE_TEMPLATE, title='Login',
                content='<div class="container mt-5"><div class="alert alert-danger">Database connection failed</div></div>')
        
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE username = %s AND password = %s", (username, password))
            user = cur.fetchone()
            cur.close()
            conn.close()
            
            if user:
                session['user_id'] = user['id']
                session['username'] = user['username']
                session['full_name'] = user['full_name']
                session['role'] = user['role']
                return redirect('/dashboard')
            else:
                content = '''
                <div class="container mt-5">
                    <div class="row justify-content-center">
                        <div class="col-md-5">
                            <div class="card">
                                <div class="card-header">
                                    <h4 class="mb-0">Login</h4>
                                </div>
                                <div class="card-body">
                                    <div class="alert alert-danger">Invalid username or password</div>
                                    <form method="POST">
                                        <div class="mb-3">
                                            <label class="form-label">Username</label>
                                            <input type="text" name="username" class="form-control" required>
                                        </div>
                                        <div class="mb-3">
                                            <label class="form-label">Password</label>
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
                return render_template_string(BASE_TEMPLATE, title='Login', content=content)
                
        except Exception as e:
            content = f'''
            <div class="container mt-5">
                <div class="alert alert-danger">Error: {str(e)}</div>
                <a href="/login" class="btn btn-primary">Try Again</a>
            </div>
            '''
            return render_template_string(BASE_TEMPLATE, title='Login', content=content)
    
    # GET request - show login form
    content = '''
    <div class="container mt-5">
        <div class="row justify-content-center">
            <div class="col-md-5">
                <div class="card">
                    <div class="card-header">
                        <h4 class="mb-0">Login</h4>
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
                            <button type="submit" class="btn btn-primary w-100">Login</button>
                        </form>
                        <div class="text-center mt-3">
                            <a href="/">Back to Home</a>
                        </div>
                        <hr class="my-4">
                        <div class="text-center">
                            <p class="mb-2">Demo Credentials:</p>
                            <p class="mb-1"><strong>Admin:</strong> admin / admin123</p>
                            <p><strong>User:</strong> user / user123</p>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    '''
    return render_template_string(BASE_TEMPLATE, title='Login', content=content)

@app.route('/register', methods=['GET', 'POST'])
def register():
    """User registration"""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        full_name = request.form.get('full_name')
        email = request.form.get('email')
        phone = request.form.get('phone')
        
        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'error': 'Database connection failed'})
        
        try:
            cur = conn.cursor()
            # Check if username already exists
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            if cur.fetchone():
                return jsonify({'success': False, 'error': 'Username already exists'})
            
            # Insert new user
            cur.execute("""
                INSERT INTO users (username, password, full_name, email, phone, role)
                VALUES (%s, %s, %s, %s, %s, 'user')
                RETURNING id
            """, (username, password, full_name, email, phone))
            
            user_id = cur.fetchone()['id']
            conn.commit()
            cur.close()
            conn.close()
            
            # Auto login after registration
            session['user_id'] = user_id
            session['username'] = username
            session['full_name'] = full_name
            session['role'] = 'user'
            
            return jsonify({'success': True, 'message': 'Registration successful', 'redirect': '/dashboard'})
            
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    
    # GET request - show registration form
    content = '''
    <div class="container mt-5">
        <div class="row justify-content-center">
            <div class="col-md-6">
                <div class="card">
                    <div class="card-header">
                        <h4 class="mb-0">Create New Account</h4>
                    </div>
                    <div class="card-body">
                        <form id="registerForm">
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Full Name</label>
                                    <input type="text" name="full_name" class="form-control" required>
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label class="form-label">Username</label>
                                    <input type="text" name="username" class="form-control" required>
                                </div>
                            </div>
                            <div class="mb-3">
                                <label class="form-label">Email Address</label>
                                <input type="email" name="email" class="form-control" required>
                            </div>
                            <div class="mb-3">
                                <label class="form-label">Phone Number</label>
                                <input type="tel" name="phone" class="form-control" required>
                            </div>
                            <div class="mb-3">
                                <label class="form-label">Password</label>
                                <input type="password" name="password" class="form-control" required>
                            </div>
                            <div class="mb-3">
                                <label class="form-label">Confirm Password</label>
                                <input type="password" name="confirm_password" class="form-control" required>
                            </div>
                            <button type="submit" class="btn btn-primary w-100">Register</button>
                        </form>
                        <div class="text-center mt-3">
                            <p>Already have an account? <a href="/login">Login here</a></p>
                            <a href="/">Back to Home</a>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <script>
        document.getElementById('registerForm').addEventListener('submit', function(e) {
            e.preventDefault();
            
            const formData = new FormData(this);
            const data = Object.fromEntries(formData);
            
            // Validate passwords match
            if (data.password !== data.confirm_password) {
                alert('Passwords do not match!');
                return;
            }
            
            // Remove confirm_password from data
            delete data.confirm_password;
            
            fetch('/register', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(data)
            })
            .then(r => r.json())
            .then(response => {
                if (response.success) {
                    alert(response.message);
                    window.location.href = response.redirect;
                } else {
                    alert('Error: ' + response.error);
                }
            })
            .catch(error => {
                alert('Registration failed: ' + error);
            });
        });
    </script>
    '''
    return render_template_string(BASE_TEMPLATE, title='Register', content=content)

@app.route('/dashboard')
def dashboard():
    """User dashboard"""
    if 'user_id' not in session:
        return redirect('/login')
    
    user_id = session['user_id']
    username = session.get('username', 'User')
    full_name = session.get('full_name', username)
    role = session.get('role', 'user')
    
    conn = get_db_connection()
    user_bookings = []
    stats = {}
    
    if conn:
        try:
            cur = conn.cursor()
            
            # Get user's bookings
            cur.execute("""
                SELECT b.*, s.departure_time, s.travel_date,
                       r.from_city, r.to_city, r.fare,
                       bs.bus_name, bs.bus_type
                FROM bookings b
                JOIN schedules s ON b.schedule_id = s.id
                JOIN routes r ON s.route_id = r.id
                JOIN buses bs ON s.bus_id = bs.id
                WHERE b.user_id = %s
                ORDER BY b.booking_date DESC
                LIMIT 5
            """, (user_id,))
            user_bookings = cur.fetchall()
            
            # Get dashboard stats
            cur.execute("SELECT COUNT(*) as total FROM buses")
            stats['total_buses'] = cur.fetchone()['total']
            
            cur.execute("SELECT COUNT(*) as total FROM bookings WHERE DATE(booking_date) = CURRENT_DATE")
            stats['today_bookings'] = cur.fetchone()['total']
            
            cur.execute("SELECT COALESCE(SUM(total_fare), 0) as total FROM bookings WHERE DATE(booking_date) = CURRENT_DATE")
            stats['total_revenue'] = cur.fetchone()['total']
            
            cur.close()
            
        except Exception as e:
            print(f"⚠️ Dashboard error: {e}")
        finally:
            conn.close()
    
    # Build bookings HTML
    bookings_html = ''
    if user_bookings:
        for booking in user_bookings:
            bookings_html += f'''
            <tr>
                <td>{booking['pnr_number'] or 'N/A'}</td>
                <td>{booking['from_city']} to {booking['to_city']}</td>
                <td>{booking['travel_date']} {booking['departure_time']}</td>
                <td>{booking['seat_numbers']}</td>
                <td>₹{booking['total_fare']}</td>
                <td><span class="badge bg-success">{booking['booking_status']}</span></td>
            </tr>
            '''
    else:
        bookings_html = '''
        <tr>
            <td colspan="6" class="text-center">No bookings yet</td>
        </tr>
        '''
    
    # Admin-specific content
    admin_content = ''
    if role == 'admin':
        admin_content = '''
        <div class="row mt-4">
            <div class="col-12">
                <div class="card">
                    <div class="card-header">
                        <h5 class="mb-0"><i class="fas fa-cog me-2"></i>Admin Actions</h5>
                    </div>
                    <div class="card-body">
                        <div class="d-flex flex-wrap gap-2">
                            <a href="/admin/buses" class="btn btn-outline-primary">
                                <i class="fas fa-bus me-1"></i>Manage Buses
                            </a>
                            <a href="/admin/routes" class="btn btn-outline-primary">
                                <i class="fas fa-route me-1"></i>Manage Routes
                            </a>
                            <a href="/admin/schedules" class="btn btn-outline-primary">
                                <i class="fas fa-calendar-alt me-1"></i>Manage Schedules
                            </a>
                            <a href="/admin/bookings" class="btn btn-outline-primary">
                                <i class="fas fa-ticket-alt me-1"></i>All Bookings
                            </a>
                            <a href="/admin/users" class="btn btn-outline-primary">
                                <i class="fas fa-users me-1"></i>Manage Users
                            </a>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        '''
    
    content = f'''
    <div class="container mt-4">
        <div class="row">
            <div class="col-md-3">
                <div class="card">
                    <div class="card-body text-center">
                        <div class="mb-3">
                            <i class="fas fa-user-circle fa-4x text-primary"></i>
                        </div>
                        <h4>{full_name}</h4>
                        <p class="text-muted">@{username}</p>
                        <p><span class="badge bg-primary">{role.upper()}</span></p>
                        <hr>
                        <div class="list-group list-group-flush">
                            <a href="/dashboard" class="list-group-item list-group-item-action active">
                                <i class="fas fa-tachometer-alt me-2"></i>Dashboard
                            </a>
                            <a href="/profile" class="list-group-item list-group-item-action">
                                <i class="fas fa-user me-2"></i>My Profile
                            </a>
                            <a href="/my-bookings" class="list-group-item list-group-item-action">
                                <i class="fas fa-ticket-alt me-2"></i>My Bookings
                            </a>
                            <a href="/search" class="list-group-item list-group-item-action">
                                <i class="fas fa-search me-2"></i>Book Tickets
                            </a>
                            <a href="/logout" class="list-group-item list-group-item-action text-danger">
                                <i class="fas fa-sign-out-alt me-2"></i>Logout
                            </a>
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="col-md-9">
                <div class="card">
                    <div class="card-header">
                        <h4 class="mb-0"><i class="fas fa-tachometer-alt me-2"></i>Dashboard Overview</h4>
                    </div>
                    <div class="card-body">
                        <!-- Stats Cards -->
                        <div class="row mb-4">
                            <div class="col-md-4">
                                <div class="card bg-primary text-white">
                                    <div class="card-body text-center">
                                        <h1 id="totalBuses">{stats.get('total_buses', 0)}</h1>
                                        <p>Total Buses</p>
                                    </div>
                                </div>
                            </div>
                            <div class="col-md-4">
                                <div class="card bg-success text-white">
                                    <div class="card-body text-center">
                                        <h1 id="totalBookings">{stats.get('today_bookings', 0)}</h1>
                                        <p>Today's Bookings</p>
                                    </div>
                                </div>
                            </div>
                            <div class="col-md-4">
                                <div class="card bg-info text-white">
                                    <div class="card-body text-center">
                                        <h1 id="totalRevenue">₹{stats.get('total_revenue', 0)}</h1>
                                        <p>Today's Revenue</p>
                                    </div>
                                </div>
                            </div>
                        </div>
                        
                        <!-- Recent Bookings -->
                        <h5 class="mb-3"><i class="fas fa-history me-2"></i>Recent Bookings</h5>
                        <div class="table-responsive">
                            <table class="table table-hover">
                                <thead>
                                    <tr>
                                        <th>PNR</th>
                                        <th>Route</th>
                                        <th>Departure</th>
                                        <th>Seats</th>
                                        <th>Fare</th>
                                        <th>Status</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {bookings_html}
                                </tbody>
                            </table>
                        </div>
                        
                        {admin_content}
                    </div>
                </div>
            </div>
        </div>
    </div>
    '''
    
    return render_template_string(BASE_TEMPLATE, title='Dashboard', content=content)

@app.route('/search')
def search_buses():
    """Search buses"""
    from_city = request.args.get('from', '')
    to_city = request.args.get('to', '')
    travel_date = request.args.get('date', date.today().isoformat())
    
    conn = get_db_connection()
    buses = []
    
    if conn:
        try:
            cur = conn.cursor()
            
            # Build query based on search parameters
            query = """
                SELECT s.*, r.from_city, r.to_city, r.distance_km, r.duration_hours, r.fare,
                       b.bus_name, b.bus_type, b.amenities, b.total_seats,
                       (SELECT COUNT(*) FROM bookings bk 
                        WHERE bk.schedule_id = s.id AND bk.booking_status = 'confirmed') as booked_seats
                FROM schedules s
                JOIN routes r ON s.route_id = r.id
                JOIN buses b ON s.bus_id = b.id
                WHERE s.travel_date = %s
                  AND s.status = 'scheduled'
            """
            params = [travel_date]
            
            if from_city:
                query += " AND LOWER(r.from_city) = LOWER(%s)"
                params.append(from_city)
            
            if to_city:
                query += " AND LOWER(r.to_city) = LOWER(%s)"
                params.append(to_city)
            
            query += " ORDER BY s.departure_time"
            
            cur.execute(query, params)
            buses = cur.fetchall()
            cur.close()
            
        except Exception as e:
            print(f"⚠️ Search error: {e}")
        finally:
            conn.close()
    
    # Build buses HTML
    buses_html = ''
    for bus in buses:
        available_seats = bus['total_seats'] - (bus['booked_seats'] or 0)
        departure_time = bus['departure_time']
        if isinstance(departure_time, str):
            departure_time = departure_time[:5]  # Format as HH:MM
        
        buses_html += f'''
        <div class="col-md-6">
            <div class="card h-100">
                <div class="card-header d-flex justify-content-between align-items-center">
                    <h5 class="mb-0">{bus['bus_name']}</h5>
                    <span class="badge bg-primary">{bus['bus_type']}</span>
                </div>
                <div class="card-body">
                    <div class="row">
                        <div class="col-8">
                            <h6>{bus['from_city']} → {bus['to_city']}</h6>
                            <p class="mb-1"><i class="fas fa-clock me-2"></i>{departure_time}</p>
                            <p class="mb-1"><i class="fas fa-road me-2"></i>{bus['distance_km']} km</p>
                            <p class="mb-1"><i class="fas fa-hourglass me-2"></i>{bus['duration_hours']} hours</p>
                            <p class="mb-1"><i class="fas fa-chair me-2"></i>{available_seats} seats available</p>
                            <p class="mb-0"><i class="fas fa-rupee-sign me-2"></i>₹{bus['fare']} per seat</p>
                        </div>
                        <div class="col-4 text-end">
                            <h3 class="text-primary">₹{bus['fare']}</h3>
                            <p class="text-muted small">per seat</p>
                            <a href="/bus/{bus['id']}" class="btn btn-primary mt-2">
                                <i class="fas fa-eye me-1"></i>View Details
                            </a>
                        </div>
                    </div>
                </div>
                <div class="card-footer bg-transparent">
                    <small class="text-muted">
                        <i class="fas fa-calendar me-1"></i>Travel Date: {bus['travel_date']}
                    </small>
                </div>
            </div>
        </div>
        '''
    
    if not buses_html:
        buses_html = '''
        <div class="col-12">
            <div class="alert alert-info">
                <h5>No buses found for your search</h5>
                <p>Try different dates or routes</p>
                <a href="/" class="btn btn-primary">Back to Search</a>
            </div>
        </div>
        '''
    
    content = f'''
    <div class="container mt-4">
        <div class="card">
            <div class="card-header">
                <h4 class="mb-0"><i class="fas fa-search me-2"></i>Search Results</h4>
            </div>
            <div class="card-body">
                <!-- Search Form -->
                <div class="row mb-4">
                    <div class="col-12">
                        <form action="/search" method="GET" class="row g-3">
                            <div class="col-md-4">
                                <input type="text" class="form-control" name="from" 
                                       value="{from_city}" placeholder="From City" required>
                            </div>
                            <div class="col-md-4">
                                <input type="text" class="form-control" name="to" 
                                       value="{to_city}" placeholder="To City" required>
                            </div>
                            <div class="col-md-3">
                                <input type="date" class="form-control" name="date" 
                                       value="{travel_date}" min="{date.today().isoformat()}" required>
                            </div>
                            <div class="col-md-1">
                                <button type="submit" class="btn btn-primary w-100">
                                    <i class="fas fa-search"></i>
                                </button>
                            </div>
                        </form>
                    </div>
                </div>
                
                <!-- Results Header -->
                <div class="d-flex justify-content-between align-items-center mb-4">
                    <h5>Found {len(buses)} buses for {from_city or 'Any'} to {to_city or 'Any'} on {travel_date}</h5>
                    <a href="/" class="btn btn-outline-primary">
                        <i class="fas fa-arrow-left me-1"></i>New Search
                    </a>
                </div>
                
                <!-- Buses List -->
                <div class="row">
                    {buses_html}
                </div>
            </div>
        </div>
    </div>
    '''
    
    return render_template_string(BASE_TEMPLATE, title='Search Results', content=content)

@app.route('/bus/<int:bus_id>')
def bus_details(bus_id):
    """Bus details page"""
    conn = get_db_connection()
    bus_details = None
    booked_seats = []
    
    if conn:
        try:
            cur = conn.cursor()
            
            # Get bus details
            cur.execute("""
                SELECT s.*, r.from_city, r.to_city, r.distance_km, r.duration_hours, r.fare,
                       b.bus_name, b.bus_type, b.amenities, b.total_seats,
                       b.bus_number
                FROM schedules s
                JOIN routes r ON s.route_id = r.id
                JOIN buses b ON s.bus_id = b.id
                WHERE s.id = %s
            """, (bus_id,))
            
            bus_details = cur.fetchone()
            
            if bus_details:
                # Get booked seats for this bus
                cur.execute("""
                    SELECT seat_numbers FROM bookings 
                    WHERE schedule_id = %s AND booking_status = 'confirmed'
                """, (bus_id,))
                
                booked_seats = []
                for row in cur.fetchall():
                    seats = row['seat_numbers'].split(',')
                    booked_seats.extend([int(s.strip()) for s in seats if s.strip().isdigit()])
            
            cur.close()
            
        except Exception as e:
            print(f"⚠️ Bus details error: {e}")
        finally:
            conn.close()
    
    if not bus_details:
        content = '''
        <div class="container mt-5">
            <div class="alert alert-danger">
                <h4>Bus not found</h4>
                <p>The requested bus does not exist or has been removed.</p>
                <a href="/search" class="btn btn-primary">Back to Search</a>
            </div>
        </div>
        '''
        return render_template_string(BASE_TEMPLATE, title='Bus Not Found', content=content)
    
    # Generate seat map
    total_seats = bus_details['total_seats']
    seats_per_row = 4
    rows = total_seats // seats_per_row
    if total_seats % seats_per_row > 0:
        rows += 1
    
    seat_html = ''
    seat_counter = 1
    
    for row in range(rows):
        seat_html += '<div class="d-flex justify-content-center mb-2">'
        for col in range(seats_per_row):
            if seat_counter > total_seats:
                break
            
            seat_class = 'booked' if seat_counter in booked_seats else 'available'
            seat_html += f'''
            <div class="seat {seat_class} me-2" 
                 id="seat-{seat_counter}"
                 onclick="selectSeat({seat_counter})">
                {seat_counter}
            </div>
            '''
            seat_counter += 1
        seat_html += '</div>'
    
    # Format amenities
    amenities = bus_details.get('amenities', '').split(',')
    amenities_html = ''
    for amenity in amenities:
        if amenity.strip():
            amenities_html += f'<span class="badge bg-secondary me-1 mb-1">{amenity.strip()}</span>'
    
    content = f'''
    <div class="container mt-4">
        <div class="card">
            <div class="card-header">
                <h4 class="mb-0">{bus_details['bus_name']} - {bus_details['from_city']} to {bus_details['to_city']}</h4>
            </div>
            <div class="card-body">
                <div class="row">
                    <div class="col-md-8">
                        <!-- Bus Details -->
                        <div class="row mb-4">
                            <div class="col-md-6">
                                <p><strong><i class="fas fa-bus me-2"></i>Bus Number:</strong> {bus_details['bus_number']}</p>
                                <p><strong><i class="fas fa-tag me-2"></i>Bus Type:</strong> {bus_details['bus_type']}</p>
                                <p><strong><i class="fas fa-road me-2"></i>Distance:</strong> {bus_details['distance_km']} km</p>
                                <p><strong><i class="fas fa-clock me-2"></i>Duration:</strong> {bus_details['duration_hours']} hours</p>
                            </div>
                            <div class="col-md-6">
                                <p><strong><i class="fas fa-calendar me-2"></i>Travel Date:</strong> {bus_details['travel_date']}</p>
                                <p><strong><i class="fas fa-clock me-2"></i>Departure:</strong> {bus_details['departure_time']}</p>
                                <p><strong><i class="fas fa-clock me-2"></i>Arrival:</strong> {bus_details['arrival_time']}</p>
                                <p><strong><i class="fas fa-rupee-sign me-2"></i>Fare per seat:</strong> ₹{bus_details['fare']}</p>
                            </div>
                        </div>
                        
                        <!-- Amenities -->
                        <h5><i class="fas fa-concierge-bell me-2"></i>Amenities</h5>
                        <div class="mb-4">
                            {amenities_html if amenities_html else '<p class="text-muted">No amenities listed</p>'}
                        </div>
                        
                        <!-- Seat Map -->
                        <h5><i class="fas fa-chair me-2"></i>Select Seats ({len(booked_seats)}/{total_seats} booked)</h5>
                        <div class="mb-4">
                            <div class="seat-map">
                                {seat_html}
                            </div>
                            <div class="d-flex mt-3">
                                <div class="d-flex align-items-center me-4">
                                    <div class="seat available me-2"></div>
                                    <span>Available</span>
                                </div>
                                <div class="d-flex align-items-center me-4">
                                    <div class="seat booked me-2"></div>
                                    <span>Booked</span>
                                </div>
                                <div class="d-flex align-items-center">
                                    <div class="seat selected me-2"></div>
                                    <span>Selected</span>
                                </div>
                            </div>
                        </div>
                    </div>
                    
                    <div class="col-md-4">
                        <!-- Booking Summary -->
                        <div class="card">
                            <div class="card-header">
                                <h5 class="mb-0">Booking Summary</h5>
                            </div>
                            <div class="card-body">
                                <div id="bookingSummary">
                                    <p class="text-muted">Select seats to proceed with booking</p>
                                </div>
                                <div id="selectedSeatsInfo" style="display: none;">
                                    <h6>Selected Seats: <span id="selectedSeatsList"></span></h6>
                                    <h6>Total Fare: ₹<span id="totalFare">0</span></h6>
                                    <hr>
                                    <div class="d-grid gap-2">
                                        <button onclick="proceedToBooking()" class="btn btn-success">
                                            <i class="fas fa-ticket-alt me-1"></i>Proceed to Booking
                                        </button>
                                        <button onclick="clearSelection()" class="btn btn-outline-danger">
                                            <i class="fas fa-times me-1"></i>Clear Selection
                                        </button>
                                    </div>
                                </div>
                            </div>
                        </div>
                        
                        <!-- Route Info -->
                        <div class="card mt-3">
                            <div class="card-header">
                                <h6 class="mb-0">Route Information</h6>
                            </div>
                            <div class="card-body">
                                <p><strong>From:</strong> {bus_details['from_city']}</p>
                                <p><strong>To:</strong> {bus_details['to_city']}</p>
                                <p><strong>Distance:</strong> {bus_details['distance_km']} km</p>
                                <p><strong>Approx. Travel Time:</strong> {bus_details['duration_hours']} hours</p>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <script>
        let selectedSeats = [];
        const farePerSeat = {bus_details['fare']};
        
        function selectSeat(seatNumber) {{
            const seatElement = document.getElementById('seat-' + seatNumber);
            
            // Check if seat is already booked
            if (seatElement.classList.contains('booked')) {{
                alert('This seat is already booked!');
                return;
            }}
            
            // Toggle seat selection
            if (seatElement.classList.contains('selected')) {{
                // Deselect seat
                seatElement.classList.remove('selected');
                seatElement.classList.add('available');
                selectedSeats = selectedSeats.filter(s => s !== seatNumber);
            }} else {{
                // Select seat (limit to 6 seats per booking)
                if (selectedSeats.length >= 6) {{
                    alert('Maximum 6 seats can be booked at once!');
                    return;
                }}
                seatElement.classList.remove('available');
                seatElement.classList.add('selected');
                selectedSeats.push(seatNumber);
            }}
            
            updateBookingSummary();
        }}
        
        function updateBookingSummary() {{
            const summaryDiv = document.getElementById('bookingSummary');
            const seatsInfoDiv = document.getElementById('selectedSeatsInfo');
            const seatsListSpan = document.getElementById('selectedSeatsList');
            const totalFareSpan = document.getElementById('totalFare');
            
            if (selectedSeats.length === 0) {{
                summaryDiv.style.display = 'block';
                seatsInfoDiv.style.display = 'none';
            }} else {{
                summaryDiv.style.display = 'none';
                seatsInfoDiv.style.display = 'block';
                
                // Update selected seats list
                seatsListSpan.textContent = selectedSeats.sort((a, b) => a - b).join(', ');
                
                // Calculate total fare
                const totalFare = selectedSeats.length * farePerSeat;
                totalFareSpan.textContent = totalFare;
            }}
        }}
        
        function clearSelection() {{
            selectedSeats.forEach(seatNumber => {{
                const seatElement = document.getElementById('seat-' + seatNumber);
                if (seatElement) {{
                    seatElement.classList.remove('selected');
                    seatElement.classList.add('available');
                }}
            }});
            selectedSeats = [];
            updateBookingSummary();
        }}
        
        function proceedToBooking() {{
            if (selectedSeats.length === 0) {{
                alert('Please select at least one seat!');
                return;
            }}
            
            {% if not session.user_id %}
                alert('Please login to book tickets!');
                window.location.href = '/login?redirect=' + encodeURIComponent(window.location.pathname);
                return;
            {% endif %}
            
            // Show booking form
            const passengerName = prompt('Enter passenger name:');
            if (!passengerName) return;
            
            const passengerPhone = prompt('Enter contact phone number:');
            if (!passengerPhone) return;
            
            const passengerEmail = prompt('Enter email address (optional):', '');
            
            // Submit booking
            fetch('/api/book', {{
                method: 'POST',
                headers: {{
                    'Content-Type': 'application/json'
                }},
                body: JSON.stringify({{
                    schedule_id: {bus_id},
                    seat_numbers: selectedSeats.join(','),
                    passenger_name: passengerName,
                    contact_phone: passengerPhone,
                    contact_email: passengerEmail || '',
                    total_fare: selectedSeats.length * farePerSeat
                }})
            }})
            .then(response => response.json())
            .then(data => {{
                if (data.success) {{
                    alert('Booking successful! Booking ID: ' + data.booking_id);
                    window.location.href = '/booking/' + data.booking_id;
                }} else {{
                    alert('Error: ' + data.error);
                }}
            }})
            .catch(error => {{
                alert('Booking failed: ' + error);
            }});
        }}
    </script>
    '''
    
    return render_template_string(BASE_TEMPLATE, title=f'Bus {bus_details["bus_name"]}', content=content)

# ================= API ENDPOINTS =================
@app.route('/api/stats')
def api_stats():
    """Get system statistics"""
    conn = get_db_connection()
    stats = {
        'success': False,
        'total_buses': 0,
        'today_bookings': 0,
        'total_revenue': 0
    }
    
    if conn:
        try:
            cur = conn.cursor()
            
            cur.execute("SELECT COUNT(*) as total FROM buses")
            stats['total_buses'] = cur.fetchone()['total']
            
            cur.execute("SELECT COUNT(*) as total FROM bookings WHERE DATE(booking_date) = CURRENT_DATE")
            stats['today_bookings'] = cur.fetchone()['total']
            
            cur.execute("SELECT COALESCE(SUM(total_fare), 0) as total FROM bookings WHERE DATE(booking_date) = CURRENT_DATE")
            stats['total_revenue'] = cur.fetchone()['total']
            
            stats['success'] = True
            cur.close()
            
        except Exception as e:
            print(f"⚠️ API stats error: {e}")
            stats['error'] = str(e)
        finally:
            conn.close()
    
    return jsonify(stats)

@app.route('/api/book', methods=['POST'])
def api_book():
    """Create a new booking"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Please login to book tickets'})
    
    data = request.json
    schedule_id = data.get('schedule_id')
    seat_numbers = data.get('seat_numbers')
    passenger_name = data.get('passenger_name')
    contact_phone = data.get('contact_phone')
    contact_email = data.get('contact_email', '')
    total_fare = data.get('total_fare')
    
    # Validate required fields
    if not all([schedule_id, seat_numbers, passenger_name, contact_phone, total_fare]):
        return jsonify({'success': False, 'error': 'Missing required fields'})
    
    conn = get_db_connection()
    if not conn:
        return jsonify({'success': False, 'error': 'Database connection failed'})
    
    try:
        cur = conn.cursor()
        
        # Generate PNR number
        import random
        import string
        pnr = ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
        
        # Create booking
        cur.execute("""
            INSERT INTO bookings 
            (schedule_id, user_id, seat_numbers, passenger_name, contact_phone, 
             contact_email, total_fare, pnr_number)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (schedule_id, session['user_id'], seat_numbers, passenger_name, 
              contact_phone, contact_email, total_fare, pnr))
        
        booking_id = cur.fetchone()['id']
        
        # Create payment record
        cur.execute("""
            INSERT INTO payments (booking_id, amount, payment_method, status)
            VALUES (%s, %s, 'cash', 'completed')
        """, (booking_id, total_fare))
        
        conn.commit()
        
        # Update available seats in schedule
        seats_count = len(seat_numbers.split(','))
        cur.execute("""
            UPDATE schedules 
            SET available_seats = available_seats - %s
            WHERE id = %s
        """, (seats_count, schedule_id))
        
        conn.commit()
        
        # Notify all clients about seat booking
        socketio.emit('seat_booking', {
            'schedule_id': schedule_id,
            'seat_numbers': seat_numbers
        })
        
        cur.close()
        
        return jsonify({
            'success': True,
            'booking_id': booking_id,
            'pnr': pnr,
            'message': 'Booking successful'
        })
        
    except Exception as e:
        conn.rollback()
        print(f"❌ Booking error: {e}")
        return jsonify({'success': False, 'error': str(e)})
    finally:
        conn.close()

@app.route('/api/buses')
def api_buses():
    """Get all buses with locations"""
    conn = get_db_connection()
    buses = []
    
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT b.*, s.departure_time, s.travel_date,
                       r.from_city, r.to_city
                FROM buses b
                LEFT JOIN schedules s ON b.id = s.bus_id
                LEFT JOIN routes r ON s.route_id = r.id
                WHERE s.travel_date >= CURRENT_DATE
                ORDER BY s.departure_time
            """)
            buses = cur.fetchall()
            cur.close()
        except Exception as e:
            print(f"⚠️ API buses error: {e}")
        finally:
            conn.close()
    
    return jsonify({'success': True, 'buses': buses})

@app.route('/api/bookings/<int:booking_id>')
def api_booking_details(booking_id):
    """Get booking details"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Unauthorized'})
    
    conn = get_db_connection()
    booking = None
    
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT b.*, s.departure_time, s.travel_date,
                       r.from_city, r.to_city, r.fare,
                       bs.bus_name, bs.bus_type,
                       p.transaction_id, p.payment_method, p.status as payment_status
                FROM bookings b
                JOIN schedules s ON b.schedule_id = s.id
                JOIN routes r ON s.route_id = r.id
                JOIN buses bs ON s.bus_id = bs.id
                LEFT JOIN payments p ON b.id = p.booking_id
                WHERE b.id = %s AND (b.user_id = %s OR %s = 'admin')
            """, (booking_id, session['user_id'], session.get('role', '')))
            
            booking = cur.fetchone()
            cur.close()
            
        except Exception as e:
            print(f"⚠️ API booking error: {e}")
        finally:
            conn.close()
    
    if booking:
        return jsonify({'success': True, 'booking': booking})
    else:
        return jsonify({'success': False, 'error': 'Booking not found'})

# ================= ADMIN ROUTES =================
@app.route('/admin/buses')
def admin_buses():
    """Admin: Manage buses"""
    if session.get('role') != 'admin':
        return redirect('/dashboard')
    
    conn = get_db_connection()
    buses = []
    
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM buses ORDER BY id")
            buses = cur.fetchall()
            cur.close()
        except Exception as e:
            print(f"⚠️ Admin buses error: {e}")
        finally:
            conn.close()
    
    # Build buses table
    buses_html = ''
    for bus in buses:
        buses_html += f'''
        <tr>
            <td>{bus['id']}</td>
            <td>{bus['bus_number']}</td>
            <td>{bus['bus_name']}</td>
            <td>{bus['bus_type']}</td>
            <td>{bus['total_seats']}</td>
            <td>
                <button class="btn btn-sm btn-primary">Edit</button>
                <button class="btn btn-sm btn-danger">Delete</button>
            </td>
        </tr>
        '''
    
    content = f'''
    <div class="container mt-4">
        <div class="card">
            <div class="card-header d-flex justify-content-between align-items-center">
                <h4 class="mb-0"><i class="fas fa-bus me-2"></i>Manage Buses</h4>
                <button class="btn btn-primary" data-bs-toggle="modal" data-bs-target="#addBusModal">
                    <i class="fas fa-plus me-1"></i>Add New Bus
                </button>
            </div>
            <div class="card-body">
                <div class="table-responsive">
                    <table class="table table-hover">
                        <thead>
                            <tr>
                                <th>ID</th>
                                <th>Bus Number</th>
                                <th>Bus Name</th>
                                <th>Type</th>
                                <th>Seats</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {buses_html if buses_html else '''
                            <tr>
                                <td colspan="6" class="text-center">No buses found</td>
                            </tr>
                            '''}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Add Bus Modal -->
    <div class="modal fade" id="addBusModal" tabindex="-1">
        <div class="modal-dialog">
            <div class="modal-content">
                <div class="modal-header">
                    <h5 class="modal-title">Add New Bus</h5>
                    <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <form id="addBusForm">
                        <div class="mb-3">
                            <label class="form-label">Bus Number</label>
                            <input type="text" name="bus_number" class="form-control" required>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">Bus Name</label>
                            <input type="text" name="bus_name" class="form-control" required>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">Bus Type</label>
                            <select name="bus_type" class="form-select" required>
                                <option value="AC Sleeper">AC Sleeper</option>
                                <option value="Non-AC Sleeper">Non-AC Sleeper</option>
                                <option value="AC Seater">AC Seater</option>
                                <option value="Non-AC Seater">Non-AC Seater</option>
                                <option value="Luxury Coach">Luxury Coach</option>
                            </select>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">Total Seats</label>
                            <input type="number" name="total_seats" class="form-control" value="40" required>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">Amenities (comma separated)</label>
                            <input type="text" name="amenities" class="form-control" 
                                   placeholder="WiFi, Charging, Blanket, Water">
                        </div>
                    </form>
                </div>
                <div class="modal-footer">
                    <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                    <button type="button" class="btn btn-primary" onclick="addBus()">Add Bus</button>
                </div>
            </div>
        </div>
    </div>
    
    <script>
        function addBus() {{
            const form = document.getElementById('addBusForm');
            const formData = new FormData(form);
            const data = Object.fromEntries(formData);
            
            fetch('/api/admin/buses', {{
                method: 'POST',
                headers: {{
                    'Content-Type': 'application/json'
                }},
                body: JSON.stringify(data)
            }})
            .then(r => r.json())
            .then(response => {{
                if (response.success) {{
                    alert('Bus added successfully!');
                    location.reload();
                }} else {{
                    alert('Error: ' + response.error);
                }}
            }});
        }}
    </script>
    '''
    
    return render_template_string(BASE_TEMPLATE, title='Admin - Buses', content=content)

@app.route('/api/admin/buses', methods=['POST'])
def api_add_bus():
    """API: Add new bus"""
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'})
    
    data = request.json
    bus_number = data.get('bus_number')
    bus_name = data.get('bus_name')
    bus_type = data.get('bus_type')
    total_seats = data.get('total_seats')
    amenities = data.get('amenities', '')
    
    conn = get_db_connection()
    if not conn:
        return jsonify({'success': False, 'error': 'Database connection failed'})
    
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO buses (bus_number, bus_name, bus_type, total_seats, amenities)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """, (bus_number, bus_name, bus_type, total_seats, amenities))
        
        bus_id = cur.fetchone()['id']
        conn.commit()
        cur.close()
        
        return jsonify({'success': True, 'bus_id': bus_id, 'message': 'Bus added successfully'})
        
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)})
    finally:
        conn.close()

# ================= UTILITY ROUTES =================
@app.route('/logout')
def logout():
    """Logout user"""
    session.clear()
    return redirect('/')

@app.route('/health')
def health():
    """Health check endpoint"""
    try:
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            cur.execute('SELECT 1')
            cur.close()
            conn.close()
            db_status = 'connected'
        else:
            db_status = 'disconnected'
        
        return jsonify({
            'status': 'healthy',
            'timestamp': datetime.now().isoformat(),
            'database': db_status,
            'service': 'bus-booking-system'
        })
    except Exception as e:
        return jsonify({
            'status': 'unhealthy',
            'error': str(e)
        }), 500

@app.errorhandler(404)
def page_not_found(e):
    """404 error handler"""
    content = '''
    <div class="container mt-5 text-center">
        <div class="card">
            <div class="card-body py-5">
                <h1 class="display-1 text-muted">404</h1>
                <h2 class="mb-4">Page Not Found</h2>
                <p class="lead mb-4">The page you are looking for does not exist.</p>
                <a href="/" class="btn btn-primary btn-lg">
                    <i class="fas fa-home me-2"></i>Go to Homepage
                </a>
            </div>
        </div>
    </div>
    '''
    return render_template_string(BASE_TEMPLATE, title='404 - Page Not Found', content=content), 404

@app.errorhandler(500)
def server_error(e):
    """500 error handler"""
    content = '''
    <div class="container mt-5 text-center">
        <div class="card">
            <div class="card-body py-5">
                <h1 class="display-1 text-danger">500</h1>
                <h2 class="mb-4">Server Error</h2>
                <p class="lead mb-4">Something went wrong on our server. Please try again later.</p>
                <a href="/" class="btn btn-primary btn-lg">
                    <i class="fas fa-home me-2"></i>Go to Homepage
                </a>
            </div>
        </div>
    </div>
    '''
    return render_template_string(BASE_TEMPLATE, title='500 - Server Error', content=content), 500

# ================= MAIN =================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    print(f"""
    🚀 Bus Booking System Starting...
    📍 Port: {port}
    🔧 Environment: {'Production' if os.environ.get('RENDER') else 'Development'}
    🗄️  Database: {'Connected' if get_db_connection() else 'Not connected'}
    🌐 WebSocket: Enabled
    """)
    
    socketio.run(
        app,
        host='0.0.0.0',
        port=port,
        debug=os.environ.get('FLASK_DEBUG') == '1',
        allow_unsafe_werkzeug=True
    )