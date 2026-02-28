import requests
from dotenv import load_dotenv
import os
load_dotenv()

url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_KEY")
headers = {"apikey": key, "Authorization": f"Bearer {key}"}

try:
    r = requests.get(f"{url}/rest/v1/", headers=headers, timeout=10)
    print("✅ Status:", r.status_code)
except Exception as e:
    print("❌ Error:", e)