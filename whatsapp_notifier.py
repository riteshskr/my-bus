# whatsapp_notifier.py
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import time
import os
import re
import threading
from datetime import datetime, date
import json
from supabase import create_client, Client
from dotenv import load_dotenv
import traceback

# Load environment variables
load_dotenv()
print("📤 WhatsApp function called")
# ========== SUPABASE CONFIG ==========
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("⚠️ WARNING: SUPABASE_URL or SUPABASE_KEY not set!")
    supabase = None
else:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("✅ Supabase connected in WhatsApp notifier")

# ========== CONFIGURATION ==========
CHROME_USER_DATA = os.path.join(os.path.dirname(__file__), "chrome_whatsapp_data")
SENT_LOG = os.path.join(os.path.dirname(__file__), "whatsapp_log.txt")
MESSAGE_FILE = os.path.join(os.path.dirname(__file__), "message.txt")

def clean_text(text):
    return text.encode("utf-16", "ignore").decode("utf-16")

# मैसेज टेम्पलेट्स
DEFAULT_TEMPLATES = {
    "booking_confirmation": """ नमस्ते {name} जी!
आपकी बस टिकट बुक हो गई है 
 तारीख: {date}  बस: {bus_name}  सीट: {seat_number}
 से: {from_station} से {to_station}  किराया: ₹{fare}
यात्रा शुभ हो!
My Bus AI टीम """,

    "bus_30km_alert": """ नमस्ते {name} जी!

आपकी बस ({bus_name}) अब {station} स्टेशन से सिर्फ {distance:.1f} किमी दूर है और 30 मिनट में पहुंचने वाली है।

कृपया स्टेशन पहुंचने की तैयारी रखें।

लाइव लोकेशन ट्रैक करें: {tracking_link}

धन्यवाद,
My Bus AI टीम """,

    "bus_departure": """ नमस्ते {name} जी!

आपकी बस ({bus_name}) अभी {from_station} से रवाना हो गई है।

अपनी यात्रा का आनंद लें!
My Bus AI टीम """,

    "payment_reminder": """ नमस्ते {name} जी!

आपकी बस बुकिंग {booking_id} के लिए भुगतान लंबित है।

कृपया जल्द से जल्द भुगतान करें।

धन्यवाद,
My Bus AI टीम"""
}


class WhatsAppNotifier:
    """WhatsApp नोटिफिकेशन भेजने का क्लास - Supabase से डेटा लेगा"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance
    
    def __init__(self, headless=False, auto_start=True):
        """Initialize WhatsApp notifier"""
        if self._initialized:
            return
            
        self.driver = None
        self.is_running = False
        self.headless = headless
        self.templates = self._load_templates()
        self._initialized = True
        print("✅ WhatsAppNotifier instance created")
        
        if auto_start:
            self.start()
    
    def _load_templates(self):
        """message.txt से टेम्पलेट लोड करें"""
        templates = DEFAULT_TEMPLATES.copy()
        
        try:
            if os.path.exists(MESSAGE_FILE):
                with open(MESSAGE_FILE, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        templates["booking_confirmation"] = content
                        print(f"✅ Message template loaded from {MESSAGE_FILE}")
        except Exception as e:
            print(f"⚠️ Error loading message file: {e}")
        
        return templates
    
    def start(self):
        """WhatsApp Web शुरू करें"""
        try:
            print("🔄 Starting WhatsApp Web...")
            
            options = Options()
            options.add_argument("--remote-debugging-port=9222")
            options.add_argument("--disable-software-rasterizer")
            options.add_argument("--disable-extensions")
            options.add_argument("--start-maximized")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-gpu")
            
            if self.headless:
                options.add_argument("--headless=new")
            
            if not os.path.exists(CHROME_USER_DATA):
                os.makedirs(CHROME_USER_DATA)
                print(f"📁 Created directory: {CHROME_USER_DATA}")
            
            options.add_argument(f"--user-data-dir={CHROME_USER_DATA}")
            
            service = Service(ChromeDriverManager().install())
            self.driver = webdriver.Chrome(service=service, options=options)
            
            self.driver.get("https://web.whatsapp.com")
            
            wait = WebDriverWait(self.driver, 60)
            print("⏳ Waiting for WhatsApp Web to load (scan QR code if needed)...")
            
            try:
                wait.until(EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true']")))
                print("✅ WhatsApp Web ready!")
            except:
                print("⚠️ Timeout - checking if already logged in...")
                time.sleep(5)
            
            self.is_running = True
            return True
            
        except Exception as e:
            print(f"❌ WhatsApp start error: {e}")
            return False
    
    def stop(self):
        """WhatsApp Web बंद करें"""
        if self.driver:
            try:
                self.driver.quit()
                print("✅ WhatsApp Web closed")
            except:
                pass
            finally:
                self.driver = None
                self.is_running = False
    
    def restart(self):
        """WhatsApp Web रीस्टार्ट करें"""
        self.stop()
        time.sleep(2)
        return self.start()
    
    def is_valid_mobile(self, mobile):
        """मोबाइल नंबर वैलिड है?"""
        if not mobile:
            return False
        mobile_str = str(mobile).strip()
        return re.match(r'^[6-9]\d{9}$', mobile_str) is not None
    
    def log_message(self, mobile, name, template_type, status, booking_id=None, error=""):
        """मैसेज लॉग करें"""
        try:
            with open(SENT_LOG, "a", encoding="utf-8") as f:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                booking_info = f" | Booking: {booking_id}" if booking_id else ""
                if error:
                    f.write(f"{timestamp} | {mobile} | {name}{booking_info} | {template_type} | {status} | Error: {error}\n")
                else:
                    f.write(f"{timestamp} | {mobile} | {name}{booking_info} | {template_type} | {status}\n")
        except Exception as e:
            print(f"Logging error: {e}")
    
    def send_whatsapp_message(self, mobile, message):
        """WhatsApp पर मैसेज भेजें (लो-लेवल फंक्शन)"""
        if not self.is_running or not self.driver:
            print("❌ WhatsApp not running")
            return False
        
        try:
            mobile_full = "+91" + str(mobile).strip()
            
            # सर्च बॉक्स ढूंढें
            try:
                search_box = WebDriverWait(self.driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//div[@contenteditable='true'][@data-tab='3']"))
                )
            except:
                search_box = WebDriverWait(self.driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//div[@contenteditable='true']"))
                )
            
            search_box.click()
            search_box.clear()
            search_box.send_keys(mobile_full)
            time.sleep(2)
            search_box.send_keys(Keys.ENTER)
            time.sleep(2)
            
            # मैसेज बॉक्स ढूंढें
            try:
                msg_box = WebDriverWait(self.driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//div[@contenteditable='true'][@data-tab='10']"))
                )
            except:
                msg_box = WebDriverWait(self.driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//div[@contenteditable='true'][@spellcheck='true']"))
                )
            
            msg_box.click()
            msg_box.send_keys(clean_text(message))
            time.sleep(1)
            msg_box.send_keys(Keys.ENTER)
            
            return True
            
        except Exception as e:
            print(f"❌ Error sending message: {e}")
            return False
    
    # ========== SUPABASE से डेटा लेकर मैसेज भेजने के फंक्शन ==========
    
    def get_booking_from_supabase(self, booking_id):
        """Supabase से बुकिंग डिटेल्स लें"""
        if not supabase:
            print("❌ Supabase not connected")
            return None
        
        try:
            result = supabase.table("seat_bookings") \
                .select("""
                    id,
                    passenger_name,
                    mobile,
                    from_station,
                    to_station,
                    travel_date,
                    fare,
                    seat_number,
                    schedule_id,
                    status,
                    schedules (
                        bus_name,
                        route_id
                    )
                """) \
                .eq("id", booking_id) \
                .execute()
            
            if result.data and len(result.data) > 0:
                return result.data[0]
            return None
            
        except Exception as e:
            print(f"❌ Error fetching booking: {e}")
            return None
    
    def get_today_bookings(self):
        """आज की सभी कन्फर्म बुकिंग्स लें"""
        if not supabase:
            print("❌ Supabase not connected")
            return []
        
        try:
            today = date.today().isoformat()
            
            result = supabase.table("seat_bookings") \
                .select("""
                    id,
                    passenger_name,
                    mobile,
                    from_station,
                    to_station,
                    travel_date,
                    fare,
                    seat_number,
                    schedule_id,
                    schedules (
                        bus_name,
                        route_id
                    )
                """) \
                .eq("travel_date", today) \
                .eq("status", "confirmed") \
                .execute()
            
            return result.data if result.data else []
            
        except Exception as e:
            print(f"❌ Error fetching today's bookings: {e}")
            return []
    
    def get_bookings_by_schedule(self, schedule_id, travel_date=None):
        """किसी特定 बस की बुकिंग्स लें"""
        if not supabase:
            return []
        
        try:
            if not travel_date:
                travel_date = date.today().isoformat()
            
            result = supabase.table("seat_bookings") \
                .select("""
                    id,
                    passenger_name,
                    mobile,
                    from_station,
                    to_station,
                    seat_number,
                    schedules (
                        bus_name,
                        route_id
                    )
                """) \
                .eq("schedule_id", schedule_id) \
                .eq("travel_date", travel_date) \
                .eq("status", "confirmed") \
                .execute()
            
            return result.data if result.data else []
            
        except Exception as e:
            print(f"❌ Error fetching schedule bookings: {e}")
            return []
    
    def get_station_location(self, route_id, station_name):
        """स्टेशन की लोकेशन लें"""
        if not supabase:
            return None, None
        
        try:
            result = supabase.table("route_stations") \
                .select("lat, lng") \
                .eq("route_id", route_id) \
                .eq("station_name", station_name) \
                .execute()
            
            if result.data and len(result.data) > 0:
                return result.data[0].get('lat'), result.data[0].get('lng')
            return None, None
            
        except Exception as e:
            print(f"❌ Error fetching station location: {e}")
            return None, None
    
    def get_bus_location(self, schedule_id):
        """बस की मौजूदा लोकेशन लें"""
        if not supabase:
            return None, None
        
        try:
            result = supabase.table("schedules") \
                .select("current_lat, current_lng") \
                .eq("id", schedule_id) \
                .execute()
            
            if result.data and len(result.data) > 0:
                return result.data[0].get('current_lat'), result.data[0].get('current_lng')
            return None, None
            
        except Exception as e:
            print(f"❌ Error fetching bus location: {e}")
            return None, None
    
    def send_booking_confirmation_by_id(self, booking_id):
        """बुकिंग ID से कन्फर्मेशन भेजें"""
        booking = self.get_booking_from_supabase(booking_id)
        
        if not booking:
            print(f"❌ Booking {booking_id} not found")
            return False
        
        mobile = booking.get('mobile')
        name = booking.get('passenger_name', 'यात्री')
        
        if not self.is_valid_mobile(mobile):
            self.log_message(mobile, name, "booking_confirmation", "FAILED", booking_id, "Invalid mobile")
            return False
        
        bus_name = booking.get('schedules', {}).get('bus_name', 'बस') if booking.get('schedules') else 'बस'
        
        message = self.templates["booking_confirmation"].format(
            name=name,
            date=booking.get('travel_date', 'N/A'),
            bus_name=bus_name,
            seat_number=booking.get('seat_number', 'N/A'),
            from_station=booking.get('from_station', 'N/A'),
            to_station=booking.get('to_station', 'N/A'),
            fare=booking.get('fare', '0')
        )
        
        result = self.send_whatsapp_message(mobile, message)
        
        if result:
            self.log_message(mobile, name, "booking_confirmation", "SENT", booking_id)
        else:
            self.log_message(mobile, name, "booking_confirmation", "FAILED", booking_id, "Send failed")
        
        return result
    
    def send_bulk_confirmations(self, bookings=None):
        """एक साथ कई बुकिंग्स के कन्फर्मेशन भेजें"""
        if bookings is None:
            bookings = self.get_today_bookings()
        
        if not bookings:
            print("📭 No bookings to send")
            return {"success": 0, "fail": 0, "total": 0}
        
        success = 0
        fail = 0
        
        for booking in bookings:
            try:
                booking_id = booking.get('id')
                mobile = booking.get('mobile')
                name = booking.get('passenger_name', 'यात्री')
                
                if not self.is_valid_mobile(mobile):
                    fail += 1
                    continue
                
                bus_name = booking.get('schedules', {}).get('bus_name', 'बस') if booking.get('schedules') else 'बस'
                
                message = self.templates["booking_confirmation"].format(
                    name=name,
                    date=booking.get('travel_date', 'N/A'),
                    bus_name=bus_name,
                    seat_number=booking.get('seat_number', 'N/A'),
                    from_station=booking.get('from_station', 'N/A'),
                    to_station=booking.get('to_station', 'N/A'),
                    fare=booking.get('fare', '0')
                )
                
                if self.send_whatsapp_message(mobile, message):
                    success += 1
                    self.log_message(mobile, name, "booking_confirmation", "SENT", booking_id)
                else:
                    fail += 1
                    self.log_message(mobile, name, "booking_confirmation", "FAILED", booking_id, "Send failed")
                
                time.sleep(2)  # थोड़ा रुकें
                
            except Exception as e:
                print(f"Error processing booking: {e}")
                fail += 1
        
        print(f"✅ Bulk send complete: {success} sent, {fail} failed")
        return {"success": success, "fail": fail, "total": len(bookings)}
    
    def check_and_send_bus_alerts(self, schedule_id):
        """बस की लोकेशन चेक करके 30km अलर्ट भेजें"""
        # बस की लोकेशन लें
        bus_lat, bus_lng = self.get_bus_location(schedule_id)
        
        if not bus_lat or not bus_lng:
            print(f"⚠️ Bus {schedule_id} location not available")
            return
        
        # बस की डिटेल्स लें
        if not supabase:
            return
        
        try:
            bus_result = supabase.table("schedules") \
                .select("bus_name, route_id") \
                .eq("id", schedule_id) \
                .execute()
            
            if not bus_result.data:
                return
            
            bus = bus_result.data[0]
            bus_name = bus.get('bus_name', 'बस')
            route_id = bus.get('route_id')
            
            # इस बस की आज की बुकिंग्स लें
            bookings = self.get_bookings_by_schedule(schedule_id)
            
            for booking in bookings:
                try:
                    from_station = booking.get('from_station')
                    
                    # स्टेशन की लोकेशन लें
                    station_lat, station_lng = self.get_station_location(route_id, from_station)
                    
                    if not station_lat or not station_lng:
                        continue
                    
                    # दूरी निकालें (यहाँ haversine formula लगाना होगा)
                    # मैं सिंपल distance मान ले रहा हूँ
                    distance = 25  # यहाँ असली distance निकालें
                    
                    if distance <= 30:
                        # चेक करें कि पहले भेजा तो नहीं
                        notif_check = supabase.table("bus_notifications") \
                            .select("id") \
                            .eq("schedule_id", schedule_id) \
                            .eq("booking_id", booking['id']) \
                            .execute()
                        
                        if notif_check.data:
                            continue
                        
                        mobile = booking.get('mobile')
                        name = booking.get('passenger_name', 'यात्री')
                        
                        if not self.is_valid_mobile(mobile):
                            continue
                        
                        tracking_link = f"https://your-app.com/live-bus/{schedule_id}"
                        
                        message = self.templates["bus_30km_alert"].format(
                            name=name,
                            bus_name=bus_name,
                            station=from_station,
                            distance=distance,
                            tracking_link=tracking_link
                        )
                        
                        if self.send_whatsapp_message(mobile, message):
                            # नोटिफिकेशन रिकॉर्ड सेव करें
                            supabase.table("bus_notifications").insert({
                                "schedule_id": schedule_id,
                                "booking_id": booking['id'],
                                "distance_km": distance
                            }).execute()
                            
                            self.log_message(mobile, name, "bus_30km_alert", "SENT", booking['id'])
                        
                        time.sleep(2)
                        
                except Exception as e:
                    print(f"Error processing booking alert: {e}")
                    
        except Exception as e:
            print(f"Error in check_and_send_bus_alerts: {e}")
    
    def send_custom_message(self, mobile, name, message_text, booking_id=None):
        """कस्टम मैसेज भेजें"""
        if not self.is_valid_mobile(mobile):
            return False
        
        result = self.send_whatsapp_message(mobile, message_text)
        
        if result:
            self.log_message(mobile, name, "custom", "SENT", booking_id)
        else:
            self.log_message(mobile, name, "custom", "FAILED", booking_id)
        
        return result
    
    def get_status(self):
        """WhatsApp नोटिफायर का स्टेटस लौटाएं"""
        return {
            "is_running": self.is_running,
            "headless": self.headless,
            "user_data_dir": CHROME_USER_DATA,
            "log_file": SENT_LOG,
            "templates_loaded": list(self.templates.keys()),
            "supabase_connected": supabase is not None
        }
    
    def get_recent_logs(self, lines=50):
        """हाल के लॉग पढ़ें"""
        if not os.path.exists(SENT_LOG):
            return []
        
        try:
            with open(SENT_LOG, "r", encoding="utf-8") as f:
                logs = f.readlines()
                return logs[-lines:]
        except Exception as e:
            print(f"Error reading logs: {e}")
            return []


# सिंगलटन इंस्टेंस
_whatsapp_notifier_instance = None

def init_whatsapp_notifier(headless=False, auto_start=True):
    """WhatsApp नोटिफायर इनिशियलाइज़ करें"""
    global _whatsapp_notifier_instance
    if _whatsapp_notifier_instance is None:
        _whatsapp_notifier_instance = WhatsAppNotifier(headless=headless, auto_start=auto_start)
    return _whatsapp_notifier_instance

def get_whatsapp_notifier(headless=False, auto_start=True):
    global _whatsapp_notifier_instance
    if _whatsapp_notifier_instance is None:
        _whatsapp_notifier_instance = init_whatsapp_notifier(
            headless=headless,
            auto_start=auto_start
        )
    return _whatsapp_notifier_instance

def close_whatsapp_notifier():
    """WhatsApp नोटिफायर बंद करें"""
    global _whatsapp_notifier_instance
    if _whatsapp_notifier_instance:
        _whatsapp_notifier_instance.stop()
        _whatsapp_notifier_instance = None
        print("✅ WhatsApp notifier closed")


# ========== टेस्ट फंक्शन ==========
if __name__ == "__main__":
    print("🧪 Testing WhatsApp Notifier with Supabase...")
    
    notifier = init_whatsapp_notifier(headless=False)
    
    try:
        print("\n📊 Status:", notifier.get_status())
        
        # आज की बुकिंग्स दिखाएं
        bookings = notifier.get_today_bookings()
        print(f"\n📋 Today's bookings: {len(bookings)}")
        
        if bookings:
            print("\nFirst booking:")
            print(json.dumps(bookings[0], indent=2, default=str))
            
            # टेस्ट मैसेज (पहली बुकिंग पर)
            print("\n📱 Sending test message...")
            booking_id = bookings[0]['id']
            result = notifier.send_booking_confirmation_by_id(booking_id)
            print(f"Result: {'✅ Success' if result else '❌ Failed'}")
        
        # 30 seconds wait
        print("\n⏳ Running for 30 seconds...")
        time.sleep(30)
        
    except KeyboardInterrupt:
        print("\n⚠️ Interrupted")
    finally:
        close_whatsapp_notifier()