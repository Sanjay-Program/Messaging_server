import os
import sqlite3
import datetime
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room

# ---------------- CONFIG ----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "quantara_central.db")

app = Flask(__name__)
app.config["SECRET_KEY"] = "quantara-secret"

# ✅ IMPORTANT: use threading (NO eventlet)
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading"
)

# ---------------- DATABASE ----------------
def get_db():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

# ---------------- SOCKET EVENTS ----------------
@socketio.on("connect")
def on_connect():
    print("Client connected")

@socketio.on("join")
def on_join(data):
    username = data.get("username")
    if not username:
        return
    join_room(username)
    print(f"{username} joined")
    emit("presence", {"user": username, "online": True}, broadcast=True)

@socketio.on("leave")
def on_leave(data):
    username = data.get("username")
    if username:
        leave_room(username)
        emit("presence", {"user": username, "online": False}, broadcast=True)

@socketio.on("typing")
def on_typing(data):
    emit("typing", data, room=data.get("to"))

@socketio.on("message")
def on_message(data):
    sender = data.get("from")
    receiver = data.get("to")
    text = data.get("text")

    ts = datetime.datetime.now().isoformat()

    conn = get_db()
    conn.execute(
        "INSERT INTO messages (sender, receiver, ciphertext, timestamp) VALUES (?,?,?,?)",
        (sender, receiver, text, ts),
    )
    conn.commit()
    conn.close()

    payload = {
        "sender": sender,
        "text": text,
        "timestamp": ts,
    }

    emit("message", payload, room=receiver)
    emit("message", payload, room=sender)

@socketio.on("disconnect")
def on_disconnect():
    print("Client disconnected")

# ---------------- RUN SERVER ----------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    socketio.run(app, host="0.0.0.0", port=port)
