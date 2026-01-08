INSERT INTO routes (name)
VALUES ('Sikar → Jaipur');

INSERT INTO schedules
(route_id, bus_name, departure_time, seating_rate, single_sleeper_rate, double_sleeper_rate)
VALUES
(1, 'Shree Bus', '08:00 AM', 2.5, 4.0, 6.0);

INSERT INTO route_stations (route_id, station_name, station_order)
VALUES
(1, 'Sikar', 1),
(1, 'Neem Ka Thana', 2),
(1, 'Jaipur', 3);

INSERT INTO seats (schedule_id, seat_no, seat_type)
VALUES
(1, 'A1', 'SEATING'),
(1, 'A2', 'SEATING'),
(1, 'B1', 'SLEEPER_SINGLE'),
(1, 'B2', 'SLEEPER_DOUBLE');