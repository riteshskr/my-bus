import cv2
from ultralytics import YOLO
from deep_sort_realtime.deepsort_tracker import DeepSort

# ===== YOLO model =====
model = YOLO("yolov8n.pt")  # fastest model

# ===== DeepSort tracker =====
tracker = DeepSort(max_age=60, n_init=3)

# ===== Webcam =====
cap = cv2.VideoCapture(0)
if not cap.isOpened():
    print("❌ Camera open nahi ho raha")
    exit()

# ===== Entry line =====
ENTRY_LINE_Y = 250  # isko upar-niche adjust kar sakte ho

passenger_count = 0
track_memory = {}   # track_id : last center_y
counted_ids = set() # already counted IDs

print("✅ Press Q to quit")

while True:
    ret, frame = cap.read()
    if not ret:
        print("Frame nahi mil raha")
        break

    frame = cv2.flip(frame, 1)
    height, width = frame.shape[:2]

    # ===== YOLO detection (person) =====
    results = model(frame, conf=0.4, classes=[0])
    detections = []

    for r in results:
        for box in r.boxes:
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            w = x2 - x1
            h = y2 - y1
            detections.append(([x1, y1, w, h], box.conf.item(), "person"))

    # ===== DeepSort tracking =====
    tracks = tracker.update_tracks(detections, frame=frame)

    # ===== Draw entry line =====
    cv2.line(frame, (0, ENTRY_LINE_Y), (width, ENTRY_LINE_Y), (0,0,255), 3)
    cv2.putText(frame, "ENTRY LINE", (10, ENTRY_LINE_Y-10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0,0,255), 2)

    # ===== Process tracks =====
    for track in tracks:
        if not track.is_confirmed():
            continue

        track_id = track.track_id
        l, t, r, b = map(int, track.to_ltrb())
        cy = (t + b)//2  # center Y

        last_cy = track_memory.get(track_id, cy)

        # ===== SIMPLE BULLETPROOF LOGIC =====
        if track_id not in counted_ids:
            if last_cy < ENTRY_LINE_Y and cy >= ENTRY_LINE_Y:
                passenger_count += 1
                counted_ids.add(track_id)
                print("Passenger:", passenger_count)

        track_memory[track_id] = cy

        # Draw box + center + ID
        cv2.rectangle(frame, (l,t), (r,b), (0,255,0), 2)
        cv2.circle(frame, ((l+r)//2, cy), 5, (255,0,0), -1)
        cv2.putText(frame, f"ID {track_id}",
                    (l, t-10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (255,255,0), 2)

    # ===== Show count =====
    cv2.putText(frame, f"Passengers: {passenger_count}",
                (20,50),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.3, (0,0,255), 3)

    cv2.imshow("YOLO + DeepSort Passenger Counter", frame)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()
print("🛑 Webcam बंद कर दिया गया")