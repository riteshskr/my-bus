import os
import time
import re
import math
import threading
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from webdriver_manager.chrome import ChromeDriverManager
from supabase import create_client, Client
from datetime import datetime

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

CHROME_USER_DATA = os.path.join(os.path.dirname(__file__), "chrome_data")


class WhatsAppNotifier:
    def __init__(self, headless=False):
        self.driver = None
        self.lock = threading.Lock()
        self.headless = headless
        self.start_driver()
        print("✅ WhatsApp Notifier Initialized!")

    def start_driver(self):
        """Chrome crash + Device Link FIXED - Complete stealth mode"""
        options = Options()
        profile_path = os.path.join(os.path.dirname(__file__), "chrome_profile")
        options.add_argument(f"--user-data-dir={profile_path}")
        options.add_argument("profile-directory=Default")
        # ========== CHROME CRASH FIX ==========
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-web-security")
        options.add_argument("--disable-features=VizDisplayCompositor")

        # ========== DEVICE LINK ERROR FIX (STEALTH MODE) ==========
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)

        # Real Chrome user agent (WhatsApp detection bypass)
        options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

        # Window settings
        options.add_argument("--start-maximized")
        options.add_argument("--disable-background-timer-throttling")
        options.add_argument("--disable-backgrounding-occluded-windows")
        options.add_argument("--disable-renderer-backgrounding")

        # Chrome service
        service = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=service, options=options)

        # ========== ULTIMATE STEALTH - CDP Script ==========
        self.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
            'source': '''
                // Remove webdriver property completely
                delete navigator.__proto__.webdriver;
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined,
                });

                // Spoof languages
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en'],
                });

                // Spoof platform
                Object.defineProperty(navigator, 'platform', {
                    get: () => 'Win32',
                });
            '''
        })

        # WhatsApp Web
        self.driver.get("https://web.whatsapp.com")
        print("🔄 WhatsApp Web Loading... QR Code Scan करें!")
        print("📱 PHONE STEPS:")
        print("1. WhatsApp → Settings → Linked Devices → Log out ALL")
        print("2. PHONE RESTART करें")
        print("3. QR CODE तुरंत SCAN करें")

        # Wait for WhatsApp side panel (linked device)
        WebDriverWait(self.driver, 60).until(
            EC.presence_of_element_located((By.XPATH, "//div[@id='pane-side']"))
        )
        print("✅ WhatsApp Ready! 🎉 Automation Active!")

    def is_valid_mobile(self, mobile):
        """Validate Indian mobile (10 digits starting 6-9)"""
        return re.match(r'^[6-9]\d{9}$', str(mobile)) is not None

    def send_whatsapp_message(self, mobile, message):

        with self.lock:
            try:
                if not self.is_valid_mobile(mobile):
                    print(f"❌ Invalid mobile: {mobile}")
                    return False

                mobile_full = "91" + str(mobile)
                print(f"📱 Sending to: {mobile_full}")

                # Direct chat open (reload कम होगा)
                self.driver.get(f"https://web.whatsapp.com/send?phone={mobile_full}")

                # message box wait
                input_box = WebDriverWait(self.driver, 30).until(
                    EC.presence_of_element_located(
                        (By.XPATH, "//div[@contenteditable='true'][@data-tab='10']")
                    )
                )

                input_box.click()
                input_box.send_keys(message)
                input_box.send_keys(Keys.ENTER)

                print(f"✅ Message sent to {mobile_full}")

                time.sleep(2)
                return True

            except Exception as e:
                print(f"❌ Send Error: {e}")
                return False

    def calculate_distance(self, lat1, lon1, lat2, lon2):
        """Haversine formula - accurate KM distance"""
        R = 6371  # Earth radius
        dLat = math.radians(float(lat2) - float(lat1))
        dLon = math.radians(float(lon2) - float(lon1))

        a = (math.sin(dLat / 2) ** 2 +
             math.cos(math.radians(float(lat1))) *
             math.cos(math.radians(float(lat2))) *
             math.sin(dLon / 2) ** 2)

        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c

    def send_booking_confirmation(self):
        """Send booking confirmations (whatsapp=null only)"""
        print(" Checking new bookings...")
        bookings = supabase.table("seat_bookings") \
            .select("*") \
            .is_("whatsapp", None) \
            .execute().data

        print(f" Found {len(bookings)} pending bookings")
        for booking in bookings:
            name = booking['passenger_name']
            seat = booking['seat_number']
            mobile = booking['mobile']
            bus = booking['bus_number']
            msg = f""" MYBUS बुकिंग कन्फर्म!
नमस्ते {name} जी!   सीट: {seat}  मोबाइल: {mobile}
स्टेटस: कन्फर्म Bus No.{bus} धन्यवाद! MYBUS AI"""

            if self.send_whatsapp_message(mobile, msg):
                # Mark as sent (DUPLICATE PROTECTION!)
                supabase.table("seat_bookings") \
                    .update({"whatsapp": "yes"}) \
                    .eq("id", booking["id"]) \
                    .execute()
                print(f"✅ Confirmation sent: {name} - {seat}")

    def send_30km_alerts(self):
        print("Checking alerts for distance ≤ 31KM...")
        schedules = supabase.table("schedules") \
            .select("id, current_lat, current_lng") \
            .execute().data
        print("Total schedules:", len(schedules))
        for schedule in schedules:
            print("Schedule:", schedule["id"])
            print("Bus location:", schedule["current_lat"], schedule["current_lng"])

            if not schedule.get("current_lat") or not schedule.get("current_lng"):
                print("❌ Bus location missing")
                continue

            bookings = supabase.table("seat_bookings") \
                .select("*") \
                .eq("schedule_id", schedule["id"]) \
                .is_("alert_30km_sent", None) \
                .execute().data
            print("Bookings found:", len(bookings))
            for booking in bookings:
                print("Passenger:", booking["passenger_name"])
                print("Passenger location:", booking.get("lat"), booking.get("lan"))
                if not booking.get("lat") or not booking.get("lan"):
                    print("❌ Passenger location missing")
                    continue

                dist = self.calculate_distance(
                    float(schedule["current_lat"]), float(schedule["current_lng"]),
                    float(booking["lat"]), float(booking["lan"])
                )

                print(f"DEBUG: schedule_id={schedule['id']}, booking_id={booking['id']}, distance={dist:.2f} KM")
                print(f"Distance = {dist} KM")
                if round(dist,1) > 10 and round(dist,1) <= 30:
                    msg = f""" नमस्ते {booking['passenger_name']} जी!
                    बस नंबर: {booking['bus_number']}
                    दूरी: {dist:.1f} KM बस पहुँच रही है!
                    MYBUS AI"""

                    if self.send_whatsapp_message(booking["mobile"], msg):
                        supabase.table("seat_bookings") \
                            .update({"alert_30km_sent": True}) \
                            .eq("id", booking["id"]) \
                            .execute()
                        print(f"✅ Alert sent: {booking['passenger_name']}")

    def send_10km_alerts(self):
        print("Checking alerts for distance ≤ 10KM...")
        schedules = supabase.table("schedules") \
            .select("id, current_lat, current_lng") \
            .execute().data
        print("Total schedules:", len(schedules))
        for schedule in schedules:
            print("Schedule:", schedule["id"])
            print("Bus location:", schedule["current_lat"], schedule["current_lng"])

            if not schedule.get("current_lat") or not schedule.get("current_lng"):
                print("❌ Bus location missing")
                continue

            bookings = supabase.table("seat_bookings") \
                .select("*") \
                .eq("schedule_id", schedule["id"]) \
                .is_("alert_10km_sent", None) \
                .execute().data
            print("Bookings found:", len(bookings))
            for booking in bookings:
                print("Passenger:", booking["passenger_name"])
                print("Passenger location:", booking.get("lat"), booking.get("lan"))
                if not booking.get("lat") or not booking.get("lan"):
                    print("❌ Passenger location missing")
                    continue

                dist = self.calculate_distance(
                    float(schedule["current_lat"]), float(schedule["current_lng"]),
                    float(booking["lat"]), float(booking["lan"])
                )

                print(f"DEBUG: schedule_id={schedule['id']}, booking_id={booking['id']}, distance={dist:.2f} KM")
                print(f"Distance = {dist} KM")
                if round(dist,1) > 1 and round(dist,1) <= 10:  # ✅ 10KM से कम या बराबर
                    msg = f""" नमस्ते {booking['passenger_name']} जी!
                               बस नंबर: {booking['bus_number']}
                                दूरी: {dist:.1f} KM बस पहुँच रही है!
                                MYBUS AI"""

                    if self.send_whatsapp_message(booking["mobile"], msg):
                        supabase.table("seat_bookings") \
                            .update({"alert_10km_sent": True}) \
                            .eq("id", booking["id"]) \
                            .execute()
                        print(f"✅ Alert sent: {booking['passenger_name']}")

    def run_all(self):
        """Complete automation cycle"""
        print("🚀 Running WhatsApp Automations...")
        start_time = time.time()
        try:
            self.send_booking_confirmation()
            self.send_30km_alerts()
            self.send_10km_alerts()
            self.check_bus_documents()
            print(f"✅ Automations complete! ({time.time() - start_time:.1f}s)")
        except Exception as e:
            print(f"❌ Automation Error: {e}")

    def close(self):
        """Clean shutdown"""
        if self.driver:
            self.driver.quit()
            print("🔄 WhatsApp driver closed")
        # for admit #

    def days_left(self, expiry_date):
        today = datetime.now().date()
        expiry = datetime.strptime(expiry_date, "%Y-%m-%d").date()
        return (expiry - today).days

    def check_bus_documents(self):
        from datetime import date

        today1 = date.today()
        print("Checking bus insurance & permit expiry...")

        schedules = supabase.table("schedules") \
            .select("id,bus_number,insurance_expiry,permit_expiry,insurance_alert_date,permit_alert_date") \
            .execute().data

        admin_mobile = "9875262306"

        for bus in schedules:

            bus_number = bus["bus_number"]

            insurance_days = self.days_left(bus["insurance_expiry"])
            permit_days = self.days_left(bus["permit_expiry"])

            if insurance_days <= 10:
                last_alert = bus.get("insurance_alert_date")

                if last_alert != str(today1):
                    msg = f"""BUS INSURANCE ALERT
    Bus Number: {bus_number}
    Insurance expiry in {insurance_days} days.
    Please renew immediately.
    MYBUS SYSTEM"""

                    self.send_whatsapp_message(admin_mobile, msg)

                    supabase.table("schedules") \
                        .update({"insurance_alert_date": str(today1)}) \
                        .eq("id", bus["id"]) \
                        .execute()

            if permit_days <= 10:
                last_alert = bus.get("permit_alert_date")

                if last_alert != str(today1):
                    msg = f"""BUS PERMIT ALERT
    Bus Number: {bus_number}
    Permit expiry in {permit_days} days.
    Please renew immediately.
    MYBUS SYSTEM"""

                    self.send_whatsapp_message(admin_mobile, msg)

                    supabase.table("schedules") \
                        .update({"permit_alert_date": str(today1)}) \
                        .eq("id", bus["id"]) \
                        .execute()
# Factory function (main app import करता है)
def get_whatsapp_notifier(headless=False):
    return WhatsAppNotifier(headless=headless)