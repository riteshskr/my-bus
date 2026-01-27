import cv2, requests

BUS_ID = 1
URL = "https://your-app.onrender.com/face_entry"

cap = cv2.VideoCapture(0)

def get_location():
    import requests
    j = requests.get("https://ipinfo.io/json").json()
    lat, lng = j["loc"].split(",")
    return lat, lng

while True:
    ret, frame = cap.read()
    cv2.imwrite("temp.jpg", frame)

    lat, lng = get_location()

    files = {"image": open("temp.jpg","rb")}
    data = {"bus_id": BUS_ID, "lat": lat, "lng": lng}

    r = requests.post(URL, files=files, data=data)
    print(r.json())