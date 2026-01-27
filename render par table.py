import psycopg2

pg_conn = psycopg2.connect(
    host="dpg-d5g7u19r0fns739mbng0-a.oregon-postgres.render.com",
    database="busdb1_yl2r",
    user="busdb1_yl2r_user",
    password="49Tv97dLOzE8yd0WlYyns49KnyB646py",
    port=5432,
    sslmode="require"
)

pg_cur = pg_conn.cursor()

print("🔥 Dropping old tables...")

pg_cur.execute("""
DROP TABLE IF EXISTS seat_bookings CASCADE;
DROP TABLE IF EXISTS schedules CASCADE;
DROP TABLE IF EXISTS route_stations CASCADE;
DROP TABLE IF EXISTS routes CASCADE;
DROP TABLE IF EXISTS seats CASCADE;
""")

print("✅ Old tables removed")

print("🛠 Creating fresh tables...")
pg_cur.execute("""
CREATE TABLE seats (
    id SERIAL PRIMARY KEY,
    schedule_id INT,
    seat_no VARCHAR(10),
    seat_type VARCHAR(20),
    status VARCHAR(15) DEFAULT 'free',
    name VARCHAR(50)
    
);""")
pg_cur.execute("""
CREATE TABLE routes (
    id SERIAL PRIMARY KEY,
    route_name VARCHAR(100) NOT NULL,
    distance_km INT
);""")
pg_cur.execute("""
CREATE TABLE schedules (
    id SERIAL PRIMARY KEY,
    bus_name VARCHAR(100),
    route_id INT REFERENCES routes(id) ON DELETE CASCADE,
    departure_time TIME,
    current_lat DOUBLE PRECISION,
    current_lng DOUBLE PRECISION,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    seating_rate DOUBLE PRECISION,
    single_sleeper_rate DOUBLE PRECISION,
    double_sleeper_rate DOUBLE PRECISION,
    total_seats INT DEFAULT 40
    );""")
pg_cur.execute("""
    CREATE TABLE route_stations (
    id SERIAL PRIMARY KEY,
    route_id int, 
    station_name VARCHAR(50),
    station_order INT,
    arrival_time time, 
    distance float ,
    lat DOUBLE PRECISION, 
    lng DOUBLE PRECISION
    );""")
pg_cur.execute("""
    CREATE TABLE seat_bookings (
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
);""")


print("✅ Tables created")

# 🔴 COMMIT is mandatory
pg_conn.commit()

pg_cur.close()
pg_conn.close()

print("🚀 Database reset successful")
