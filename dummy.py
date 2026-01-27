import psycopg2

# DB connection
pg_conn = psycopg2.connect(
    host='dpg-d5g7u19r0fns739mbng0-a.oregon-postgres.render.com',
    database='busdb1_yl2r',
    user='busdb1_yl2r_user',
    password='49Tv97dLOzE8yd0WlYyns49KnyB646py'
)

# Cursor create
pg_cur = pg_conn.cursor()

# Fetch all rows from schedules
pg_cur.execute("""DELETE FROM seat_bookings WHERE schedule_id IN (1,2,3,4);""")
pg_conn.commit()
pg_cur.execute("SELECT * FROM seat_bookings;")
rows = pg_cur.fetchall()

# Print rows
for row in rows:
    print(row)

# Close
pg_cur.close()
pg_conn.close()

