from flask import Flask, request, render_template
import psycopg2
from datetime import datetime
from dotenv import load_dotenv
import os
from dotenv import load_dotenv
load_dotenv()
import setuptools
import os, random
from datetime import date
from functools import wraps
from flask import Flask, request, jsonify, render_template_string, redirect, g, session, render_template
from flask_socketio import SocketIO, emit
from flask_compress import Compress
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
import atexit
load_dotenv()
app = Flask(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise Exception("DATABASE_URL environment variable is missing!")

pool = ConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=10, timeout=20)
print("✅ Connection pool ready")


@atexit.register
def shutdown_pool():
    pool.close()


# ================= DB CONTEXT =================
def get_db():
    if 'db_conn' not in g:
        g.db_conn = pool.getconn()
    return g.db_conn, g.db_conn.cursor(row_factory=dict_row)


@app.teardown_appcontext
def close_db(error=None):
    conn = g.pop('db_conn', None)
    if conn:
        pool.putconn(conn)
# ===== Database connection =====
conn = pool.getconn()
cur = conn.cursor()
# ===== Create table =====
cur.execute("""
CREATE TABLE IF NOT EXISTS camera_logs (
    id SERIAL PRIMARY KEY,
    bus_id INT,
    station TEXT,
    passengers INT,
    time TIMESTAMP
);
""")
conn.commit()

# ===== API to receive mobile/CCTV data =====
@app.route("/api/camera", methods=["POST"])
def camera_api():
    data = request.json
    bus_id = data.get("bus_id")
    station = data.get("station")
    passengers = data.get("passengers")
    time = datetime.fromisoformat(data.get("time"))

    cur.execute("""
    INSERT INTO camera_logs (bus_id, station, passengers, time)
    VALUES (%s,%s,%s,%s)
    """, (bus_id, station, passengers, time))
    conn.commit()
    return {"ok": True}

# ===== Render page =====
@app.route("/dashboard")
def dashboard():
    cur.execute("SELECT bus_id, station, passengers, time FROM camera_logs ORDER BY time DESC LIMIT 50")
    rows = cur.fetchall()
    return render_template("dashboard.html", data=rows)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)