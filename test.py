import os
import mysql.connector
from flask import Flask, render_template_string, request, redirect, url_for, jsonify
from flask_socketio import SocketIO
from datetime import date

# ================= APP =================
app = Flask(__name__)
app.secret_key = "super-secret-key"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ================= DB CONFIG =================
DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "*#06041974",
    "database": "busdb1",
    "autocommit": True
}


def get_db():
    return mysql.connector.connect(**DB_CONFIG)
conn = get_db()
cur = conn.cursor()
conn = get_db()
cur = conn.cursor()


cur.execute("""ALTER TABLE schedules
ADD COLUMN seating_rate DOUBLE DEFAULT 2.5,
ADD COLUMN single_sleeper_rate DOUBLE DEFAULT 4.0,
ADD COLUMN double_sleeper_rate DOUBLE DEFAULT 6.0;""")
'''
cur.execute("""CREATE TABLE drivers (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100),
    mobile VARCHAR(15))"""
);

'''

conn.commit()
conn.close()