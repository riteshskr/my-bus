from geopy.geocoders import Nominatim
import time
import psycopg2
import os
print("🚀 Lat/Lng Finder शुरू!")
DATABASE_URL = "postgresql://busdb1_yl2r_user:49Tv97dLOzE8yd0WlYyns49KnyB646py@dpg-d5g7u19r0fns739mbng0-a.oregon-postgres.render.com/busdb1_yl2r"
#DATABASE_URL = os.getenv("DATABASE_URL")
print("DATABASE_URL =", os.getenv("DATABASE_URL"))
conn = psycopg2.connect(DATABASE_URL, sslmode='require')
cur = conn.cursor()

geolocator = Nominatim(user_agent="busapp_jaipur")

cur.execute("""
SELECT id, station_name 
FROM route_stations
""")

stations = cur.fetchall()

print(f"👉 {len(stations)} stations को geocode करना है")

for st in stations:
    id = st[0]
    name = st[1]

    print(f"\n🔍 ढूंढ रहे: {name}")
    time.sleep(1)

    location = geolocator.geocode(name + ", Rajasthan, India")

    if location:
        lat, lng = location.latitude, location.longitude

        cur.execute("""
            UPDATE route_stations
            SET lat = %s, lng = %s
            WHERE id = %s
        """, (lat, lng, id))

        conn.commit()

        print(f"✅ Saved: {lat:.4f}, {lng:.4f}")

    else:
        print("❌ नहीं मिला")

cur.close()
conn.close()

print("\n🏁 COMPLETE!")