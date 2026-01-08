CREATE TABLE routes (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL
);

-- =========================
-- SCHEDULES (Bus)
-- =========================
CREATE TABLE schedules (
    id INT AUTO_INCREMENT PRIMARY KEY,
    route_id INT NOT NULL,
    bus_name VARCHAR(100),
    departure_time VARCHAR(20),
    seating_rate DOUBLE DEFAULT 0,
    single_sleeper_rate DOUBLE DEFAULT 0,
    double_sleeper_rate DOUBLE DEFAULT 0,
    current_lat DOUBLE DEFAULT NULL,
    current_lng DOUBLE DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (route_id) REFERENCES routes(id)
        ON DELETE CASCADE
);

-- =========================
-- ROUTE STATIONS
-- =========================
CREATE TABLE route_stations (
    id INT AUTO_INCREMENT PRIMARY KEY,
    route_id INT NOT NULL,
    station_name VARCHAR(100) NOT NULL,
    station_order INT NOT NULL,
    lat DOUBLE DEFAULT NULL,
    lng DOUBLE DEFAULT NULL,
    FOREIGN KEY (route_id) REFERENCES routes(id)
        ON DELETE CASCADE
);

-- =========================
-- SEATS
-- =========================
CREATE TABLE seats (
    id INT AUTO_INCREMENT PRIMARY KEY,
    schedule_id INT NOT NULL,
    seat_no VARCHAR(10),
    seat_type ENUM(
        'SEATING',
        'SLEEPER_SINGLE',
        'SLEEPER_DOUBLE'
    ) NOT NULL,
    FOREIGN KEY (schedule_id) REFERENCES schedules(id)
        ON DELETE CASCADE
);

-- =========================
-- SEAT BOOKINGS
-- =========================
CREATE TABLE seat_bookings (
    id INT AUTO_INCREMENT PRIMARY KEY,
    seat_id INT NOT NULL,
    schedule_id INT NOT NULL,
    passenger_name VARCHAR(100),
    mobile VARCHAR(20),
    from_station VARCHAR(100),
    to_station VARCHAR(100),
    booking_date DATE,
    fare DOUBLE,
    booked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (seat_id) REFERENCES seats(id)
        ON DELETE CASCADE,
    FOREIGN KEY (schedule_id) REFERENCES schedules(id)
        ON DELETE CASCADE
);