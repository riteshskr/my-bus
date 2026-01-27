from geopy.geocoders import Nominatim
import time

print("🚀 Lat/Lng Finder शुरू!")

geolocator = Nominatim(user_agent="busapp_jaipur")

# Rajasthan Bus Stations Test
stations = [
    "Bikaner Bus Stand",
    "Sikar Bus Stand",
    "Churu",
    "Nokha",
    "Jodhpur"
]

results = []
for station in stations:
    print(f"\n🔍 ढूंढ रहे: {station}")
    time.sleep(1)  # Rate limit

    location = geolocator.geocode(station + ", Rajasthan")

    if location:
        lat, lng = location.latitude, location.longitude
        results.append((station, lat, lng))
        print(f"✅ मिला: {lat:.4f}, {lng:.4f}")
    else:
        print("❌ नहीं मिला")

print("\n🎉 सभी Coordinates:")
for station, lat, lng in results:
    print(f"{station}: {lat}, {lng}")