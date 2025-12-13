# server_socket.py
import os
import datetime
import secrets
import hashlib
import sqlite3
from flask import Flask, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit, join_room, leave_room
import eventlet

eventlet.monkey_patch()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "quantara_central.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config['SECRET_KEY'] = secrets.token_hex(16)
socketio = SocketIO(app, cors_allowed_origins="*")  # allow from your flutter app

def get_db():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_server():
    conn = get_db()
    # users table
    conn.execute("""
      CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY,
        password_hash TEXT,
        salt TEXT,
        last_seen TEXT,
        online INTEGER DEFAULT 0,
        profile_url TEXT
      )
    """)
    # messages table
    conn.execute("""
      CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender TEXT,
        receiver TEXT,
        ciphertext TEXT,
        timestamp TEXT,
        seen INTEGER DEFAULT 0,
        attachment_url TEXT
      )
    """)
    # stories table (simple)
    conn.execute("""
      CREATE TABLE IF NOT EXISTS stories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner TEXT,
        media_url TEXT,
        created_at TEXT,
        expires_at TEXT
      )
    """)
    conn.commit()
    conn.close()

init_server()

# --- HTTP endpoints (login/register/file upload/serve) ---
def hash_pwd(password, salt=None):
    if not salt: salt = secrets.token_hex(16)
    hashed = hashlib.sha3_512((password + salt).encode()).hexdigest()
    return hashed, salt

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    u, p = data.get('username'), data.get('password')
    if not u or not p: return jsonify({"error":"Missing data"}), 400
    conn = get_db()
    try:
        h, s = hash_pwd(p)
        conn.execute("INSERT INTO users (username, password_hash, salt, last_seen) VALUES (?,?,?,?)",
                     (u, h, s, datetime.datetime.now().isoformat()))
        conn.commit()
        return jsonify({"status":"success"})
    except sqlite3.IntegrityError:
        return jsonify({"error":"exists"}), 409
    finally:
        conn.close()

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    u, p = data.get('username'), data.get('password')
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE username=?", (u,)).fetchone()
    if user:
        check_hash, _ = hash_pwd(p, user['salt'])
        if check_hash == user['password_hash']:
            conn.execute("UPDATE users SET last_seen=?, online=1 WHERE username=?",
                         (datetime.datetime.now().isoformat(), u))
            conn.commit()
            conn.close()
            return jsonify({"status":"success", "username": u, "profile_url": user["profile_url"]})
    conn.close()
    return jsonify({"error":"invalid"}), 401

@app.route('/users', methods=['GET'])
def list_users():
    conn = get_db()
    rows = conn.execute("SELECT username, online, profile_url FROM users").fetchall()
    conn.close()
    return jsonify([{"username":r[0], "online": r[1], "profile_url": r[2]} for r in rows])

# file upload (profile, attachments, stories)
@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({"error":"no file"}), 400
    f = request.files['file']
    owner = request.form.get('owner', 'anon')
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    filename = f"{owner}_{ts}_{f.filename}"
    path = os.path.join(UPLOAD_DIR, filename)
    f.save(path)
    url = f"/uploads/{filename}"
    # if story flag: insert into stories
    if request.form.get('type') == 'story':
        conn = get_db()
        now = datetime.datetime.now()
        expires = (now + datetime.timedelta(hours=24)).isoformat()
        conn.execute("INSERT INTO stories (owner, media_url, created_at, expires_at) VALUES (?,?,?,?)",
                     (owner, url, now.isoformat(), expires))
        conn.commit()
        conn.close()
        # emit story posted to user's followers / everyone
        socketio.emit('story_posted', {"owner": owner, "media_url": url})
    return jsonify({"url": url})

@app.route('/uploads/<path:filename>')
def serve_upload(filename):
    return send_from_directory(UPLOAD_DIR, filename)

# message history (HTTP fallback)
@app.route('/get_messages', methods=['POST'])
def get_msgs():
    data = request.json
    u, contact = data.get('username'), data.get('contact')
    conn = get_db()
    rows = conn.execute("""
        SELECT sender, ciphertext, timestamp, seen, attachment_url FROM messages 
        WHERE (sender=? AND receiver=?) OR (sender=? AND receiver=?)
        ORDER BY id ASC
    """, (u, contact, contact, u)).fetchall()
    conn.close()
    msgs = [{"sender": r[0], "text": r[1], "ts": r[2], "seen": r[3], "attachment": r[4]} for r in rows]
    return jsonify({"messages": msgs})

# --- Socket.IO handlers ---
@socketio.on('connect')
def handle_connect():
    # client should send 'join' with username after connect
    print('client connected', request.sid)

@socketio.on('join')
def handle_join(data):
    # data: {"username": "alice"}
    username = data.get('username')
    if not username: return
    join_room(username)  # user room
    conn = get_db()
    conn.execute("UPDATE users SET online=1 WHERE username=?", (username,))
    conn.commit()
    conn.close()
    # broadcast presence
    emit('presence_update', {"username": username, "online": 1}, broadcast=True)
    print(f"{username} joined")

@socketio.on('leave')
def handle_leave(data):
    username = data.get('username')
    if username:
        leave_room(username)
        conn = get_db()
        conn.execute("UPDATE users SET online=0, last_seen=? WHERE username=?", (datetime.datetime.now().isoformat(), username))
        conn.commit()
        conn.close()
        emit('presence_update', {"username": username, "online": 0}, broadcast=True)

@socketio.on('typing')
def handle_typing(data):
    # data: {"from":"alice","to":"bob","typing":true}
    to = data.get('to')
    emit('typing', data, room=to)

@socketio.on('message')
def handle_message(data):
    # data: {"from":"alice","to":"bob","text":"hi", "attachment": null}
    sender = data.get('from')
    receiver = data.get('to')
    text = data.get('text', '')
    attachment = data.get('attachment')  # e.g. "/uploads/..."
    ts = datetime.datetime.now().isoformat()
    conn = get_db()
    conn.execute("INSERT INTO messages (sender, receiver, ciphertext, timestamp, attachment_url) VALUES (?,?,?,?,?)",
                 (sender, receiver, text, ts, attachment))
    conn.commit()
    conn.close()
    # emit to receiver and sender so both update UI
    payload = {"sender": sender, "text": text, "ts": ts, "attachment": attachment}
    emit('message', payload, room=receiver)
    emit('message', payload, room=sender)

@socketio.on('seen')
def handle_seen(data):
    # data: {"reader":"bob","from":"alice", "message_ids":[1,2,3]}
    reader = data.get('reader')
    from_user = data.get('from')
    ids = data.get('message_ids', [])
    if ids:
        conn = get_db()
        # mark specific ids as seen
        query = "UPDATE messages SET seen=1 WHERE id IN ({})".format(','.join('?'*len(ids)))
        conn.execute(query, ids)
        conn.commit()
        conn.close()
    # notify original sender
    emit('seen', {"reader": reader, "from": from_user, "message_ids": ids}, room=from_user)

@socketio.on('disconnect')
def on_disconnect():
    print('client disconnected', request.sid)
    # can't reliably set offline here without knowing username; client should emit 'leave' before disconnect

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000)
