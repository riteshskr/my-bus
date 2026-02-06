import pymysql
import psycopg2
import os

print("🚀 BusDB Migration: MySQL → Render PostgreSQL")
print("=" * 60)

# 1. PC MySQL Connection (H:\reetesh\MYBUS\busdb1)
print("🔗 Connecting PC MySQL (busdb1)...")
mysql_conn = pymysql.connect(
    host='localhost',
    user='root',
    password='*#06041974',  # अपना password डालें
    database='busdb1'  # या 'busdb1' जहाँ tables हैं
)
mysql_cur = mysql_conn.cursor()

# 2. Render PostgreSQL Connection (busdb1_yl2r_user)
print("🔗 Connecting Render PostgreSQL (busdb1_yl2r_user)...")
pg_conn = psycopg2.connect(
    host='dpg-d5g7u19r0fns739mbng0-a.oregon-postgres.render.com',  # अपना full hostname
    database='busdb1_yl2r',
    user='busdb1_yl2r_user',  # Render dashboard से
    password='49Tv97dLOzE8yd0WlYyns49KnyB646py'  # Render dashboard से
)
pg_cur = pg_conn.cursor()

# 3. सभी Tables Copy
tables = ['faces', 'face_logs', 'seat_bookings', ]

for table in tables:
    print(f"\n🔄 Migrating '{table}' table...")

    # MySQL से data लें
    pg_cur.execute(f"SELECT * FROM {table}")
    data = pg_cur.fetchall()

    if data:
        # Row structure check करें
        col_count = len(data[0])
        placeholders = ','.join(['%s'] * col_count)

        # PostgreSQL में insert (Safe - conflict ignore)
        query = f"INSERT INTO {table} VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING"
        mysql_cur.executemany(query, data)
        mysql_cur.commit()

        print(f"✅ {table}: {len(data)} rows migrated!")
    else:
        print(f"ℹ️  {table}: No data found")

# 4. Verification
print("\n📊 Verification...")
mysql_cur.execute("SELECT COUNT(*) as total FROM faces")
routes_count = mysql_cur.fetchone()[0]
print(f"✅ Render DB: {routes_count} faces loaded!")

print("\n🎉 MIGRATION COMPLETE!")
print("🔍 Check: your-app.onrender.com/admin")
print("🔍 Test: your-app.onrender.com/test-db")

mysql_conn.close()
pg_conn.close()