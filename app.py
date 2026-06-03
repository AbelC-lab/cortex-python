import os
import secrets
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_socketio import SocketIO, emit, join_room as socket_join_room
from werkzeug.utils import secure_filename


app = Flask(__name__)
app.config["SECRET_KEY"] = "cortex-secret-key"
app.config["UPLOAD_FOLDER"] = os.path.join("static", "uploads")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

AGENT_USERNAME = os.environ.get("AGENT_USERNAME", "Agent test")
AGENT_PASSWORD = os.environ.get("AGENT_PASSWORD", "test")

socketio = SocketIO(app, cors_allowed_origins="*")
DEFAULT_SESSION_ID = "lobby"
AGENT_DASHBOARD_ROOM = "agent_dashboard"
active_sessions = {DEFAULT_SESSION_ID}
closed_sessions = set()
session_records = {}
sid_sessions = {}
pending_join_requests = {}
approved_customer_sids = {}
approved_customer_by_room = {}
approved_customer_tokens = {}
authenticated_agent_sids = set()
chat_transcripts = {}


def require_agent_login(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if session.get("agent_logged_in"):
            return view(*args, **kwargs)

        return redirect(url_for("agent_login"))

    return wrapped_view


@app.route("/")
def index():
    return render_template(
        "chat.html",
        session_id=DEFAULT_SESSION_ID,
        chat_closed=DEFAULT_SESSION_ID in closed_sessions,
        requires_approval=False,
        is_agent=bool(session.get("agent_logged_in")),
    )


@app.route("/agent")
@require_agent_login
def agent():
    return render_template(
        "agent.html",
        agent_username=session.get("agent_username")
    )


@app.route("/agent/login", methods=["GET", "POST"])
def agent_login():
    error = None
    username = ""

    if request.method == "GET" and session.get("agent_logged_in"):
        return redirect(url_for("agent"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if (
            secrets.compare_digest(username, AGENT_USERNAME)
            and secrets.compare_digest(password, AGENT_PASSWORD)
        ):
            session.clear()
            session.permanent = True
            session["agent_logged_in"] = True
            session["agent_username"] = username
            return redirect(url_for("agent"))
        else:
            error = "Invalid username or password"

    return render_template(
        "agent_login.html",
        error=error,
        username=username
    )


@app.route("/agent/logout")
def agent_logout():
    session.clear()
    return redirect(url_for("agent_login"))


@app.route("/create-session", methods=["POST"])
@require_agent_login
def create_session():
    session_id = secrets.token_urlsafe(24)
    active_sessions.add(session_id)
    ensure_session_record(session_id, agent_name=session.get("agent_username", ""))
    emit_session_updated(session_id)
    return jsonify({
        "session_id": session_id,
        "link": url_for("chat_session", session_id=session_id)
    })


@app.route("/chat/<session_id>")
def chat_session(session_id):
    if session_id not in active_sessions:
        abort(404)

    return render_template(
        "chat.html",
        session_id=session_id,
        chat_closed=session_id in closed_sessions,
        requires_approval=not session.get("agent_logged_in"),
        is_agent=bool(session.get("agent_logged_in")),
    )


@app.route("/download-transcript/<session_id>")
@require_agent_login
def download_transcript(session_id):
    if session_id not in active_sessions:
        abort(404)

    transcript = build_transcript(session_id)
    return Response(
        transcript,
        mimetype="text/plain",
        headers={
            "Content-Disposition": f"attachment; filename=transcript_{session_id}.txt"
        },
    )


@app.route("/api/sessions")
@require_agent_login
def api_sessions():
    return jsonify({
        "sessions": [
            serialize_session(session_id)
            for session_id in active_sessions
            if session_id != DEFAULT_SESSION_ID
        ]
    })


@app.route("/api/session/<session_id>/messages")
@require_agent_login
def api_session_messages(session_id):
    if session_id not in active_sessions:
        abort(404)

    return jsonify({
        "session_id": session_id,
        "messages": [
            serialize_message(message, session_id)
            for message in chat_transcripts.get(session_id, [])
        ]
    })


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("image")

    if not file:
        return jsonify({"error": "No file uploaded"}), 400

    filename = f"{int(time.time())}_{secure_filename(file.filename)}"
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(filepath)

    return jsonify({
        "url": f"/static/uploads/{filename}"
    })


@socketio.on("agent_ready")
def handle_agent_ready():
    if not authenticate_agent_socket():
        emit("agent_error", {"error": "Agent login required"})
        return

    emit("dashboard_sessions", {
        "sessions": [
            serialize_session(session_id)
            for session_id in active_sessions
            if session_id != DEFAULT_SESSION_ID
        ]
    })
    emit("pending_join_requests", {
        "requests": [
            request_payload(room, join_request)
            for room, requests in pending_join_requests.items()
            for join_request in requests
        ]
    })


@socketio.on("agent_authenticate")
def handle_agent_authenticate(data):
    username = data.get("username") if isinstance(data, dict) else None
    if not authenticate_agent_socket(username):
        emit("agent_error", {"error": "Agent login required"})
        return

    emit("agent_authenticated", {
        "username": session.get("agent_username", "")
    })


@socketio.on("agent_join_dashboard")
def handle_agent_join_dashboard(data=None):
    if not authenticate_agent_socket():
        emit("agent_error", {"error": "Agent login required"})
        return

    emit("dashboard_sessions", {
        "sessions": [
            serialize_session(session_id)
            for session_id in active_sessions
            if session_id != DEFAULT_SESSION_ID
        ]
    })


@socketio.on("agent_join_session")
def handle_agent_join_session(data):
    if not is_authenticated_agent_socket():
        emit("agent_error", {"error": "Agent login required"})
        return

    room = get_room_from_payload(data)
    if not room or room == DEFAULT_SESSION_ID:
        emit("agent_error", {"error": "Invalid session"})
        return

    socket_join_room(room)
    sid_sessions[request.sid] = room
    session_record = ensure_session_record(room)
    session_record["unread_count"] = 0
    touch_session(room)
    emit_session_updated(room)
    emit("session_messages", {
        "session_id": room,
        "messages": [
            serialize_message(message, room)
            for message in chat_transcripts.get(room, [])
        ]
    })


@socketio.on("request_join")
def handle_request_join(data):
    room = get_room_from_payload(data)
    if not room or room == DEFAULT_SESSION_ID:
        emit("join_rejected", {
            "room": room,
            "text": "This support session is not available."
        })
        return

    if room in closed_sessions:
        emit("join_rejected", {
            "room": room,
            "text": "This chat has been closed."
        })
        return

    if room in approved_customer_by_room:
        emit("join_rejected", {
            "room": room,
            "text": "This support session already has a customer."
        })
        return

    name = clean_customer_field(data.get("name") if isinstance(data, dict) else None, "Guest")
    email = clean_customer_field(data.get("email") if isinstance(data, dict) else None, "")
    if not email:
        emit("join_rejected", {
            "room": room,
            "text": "Email is required to request access."
        })
        return

    remove_pending_request(request.sid)
    join_request = {
        "sid": request.sid,
        "name": name,
        "email": email,
    }
    pending_join_requests.setdefault(room, []).append(join_request)
    session_record = ensure_session_record(room)
    session_record.update({
        "customer_name": name,
        "customer_email": email,
        "status": "waiting",
        "customer_socket_id": request.sid,
        "last_message": "Waiting for approval",
        "updated_at": utc_now_iso(),
    })

    emit("join_pending", {
        "room": room,
        "text": "Waiting for agent approval..."
    })
    emit("join_request", request_payload(room, join_request), to=AGENT_DASHBOARD_ROOM)
    emit_session_updated(room)


@socketio.on("approve_join")
def handle_approve_join(data):
    if not is_authenticated_agent_socket():
        emit("agent_error", {"error": "Agent login required"})
        return

    room, customer_sid, join_request = find_pending_request(data)
    if not room or not customer_sid or not join_request:
        emit("agent_error", {"error": "Join request not found"})
        return

    if room in closed_sessions:
        remove_pending_request(customer_sid)
        emit("join_rejected", {
            "room": room,
            "text": "This chat has been closed."
        }, to=customer_sid)
        emit("join_request_removed", {
            "room": room,
            "customerSocketId": customer_sid
        }, to=AGENT_DASHBOARD_ROOM)
        return

    if room in approved_customer_by_room:
        remove_pending_request(customer_sid)
        emit("join_rejected", {
            "room": room,
            "text": "This support session already has a customer."
        }, to=customer_sid)
        emit("join_request_removed", {
            "room": room,
            "customerSocketId": customer_sid
        }, to=AGENT_DASHBOARD_ROOM)
        return

    remove_pending_request(customer_sid)
    socket_join_room(room, sid=customer_sid)
    customer_token = secrets.token_urlsafe(32)
    sid_sessions[customer_sid] = room
    approved_customer_sids[customer_sid] = room
    approved_customer_by_room[room] = customer_sid
    approved_customer_tokens[room] = {
        "token": customer_token,
        "sid": customer_sid,
        "name": join_request["name"],
        "email": join_request["email"],
    }
    session_record = ensure_session_record(room)
    session_record.update({
        "customer_name": join_request["name"],
        "customer_email": join_request["email"],
        "status": "active",
        "agent_name": session.get("agent_username", ""),
        "joined_at": utc_now_iso(),
        "customer_socket_id": customer_sid,
        "unread_count": 0,
    })

    emit("join_approved", {
        "room": room,
        "customerToken": customer_token,
    }, to=customer_sid)
    emit("join_request_removed", {
        "room": room,
        "customerSocketId": customer_sid
    }, to=AGENT_DASHBOARD_ROOM)
    system_message = make_system_message(
        room,
        "Customer approved and joined the chat.",
    )
    append_transcript_message(room, system_message)
    emit("chat_message", system_message, to=room)
    emit_session_updated(room)


@socketio.on("reject_join")
def handle_reject_join(data):
    if not is_authenticated_agent_socket():
        emit("agent_error", {"error": "Agent login required"})
        return

    room, customer_sid, join_request = find_pending_request(data)
    if not room or not customer_sid or not join_request:
        emit("agent_error", {"error": "Join request not found"})
        return

    remove_pending_request(customer_sid)
    append_transcript_message(
        room,
        make_system_message(room, f"Customer rejected: {join_request['name']}."),
    )
    session_record = ensure_session_record(room)
    session_record.update({
        "customer_name": join_request["name"],
        "customer_email": join_request["email"],
        "status": "closed",
        "agent_name": session.get("agent_username", ""),
        "customer_socket_id": None,
        "last_message": f"Rejected {join_request['name']}",
        "updated_at": utc_now_iso(),
    })
    closed_sessions.add(room)
    emit("join_rejected", {
        "room": room,
        "text": "Your request was not approved."
    }, to=customer_sid)
    emit("join_request_removed", {
        "room": room,
        "customerSocketId": customer_sid
    }, to=AGENT_DASHBOARD_ROOM)
    emit_session_updated(room)


@socketio.on("rejoin_session")
def handle_rejoin_session(data):
    room = get_room_from_payload(data)
    token = data.get("token") if isinstance(data, dict) else None

    if not room or room == DEFAULT_SESSION_ID:
        emit("rejoin_denied", {
            "room": room,
            "clearToken": True,
            "text": "This support session is not available."
        })
        return

    if room in closed_sessions:
        emit("rejoin_denied", {
            "room": room,
            "clearToken": True,
            "text": "This chat has been closed."
        })
        return

    if not validate_customer_token(room, token):
        emit("rejoin_denied", {
            "room": room,
            "clearToken": True,
            "text": "Your saved approval has expired. Please request access again."
        })
        return

    remove_pending_request(request.sid)
    socket_join_room(room)
    sid_sessions[request.sid] = room
    approved_customer_sids[request.sid] = room
    approved_customer_by_room[room] = request.sid
    approved_customer_tokens[room]["sid"] = request.sid
    session_record = ensure_session_record(room)
    session_record.update({
        "status": "active",
        "customer_socket_id": request.sid,
    })

    emit("rejoin_approved", {"room": room})
    system_message = make_system_message(room, "Customer rejoined the chat.")
    append_transcript_message(room, system_message)
    emit("chat_message", system_message, to=room)
    emit_session_updated(room)


@socketio.on("chat_message")
def handle_chat_message(msg):
    room = get_message_room(msg)
    if not room or room in closed_sessions:
        return

    append_transcript_message(room, msg, increment_unread=not is_authenticated_agent_socket())
    emit("chat_message", msg, to=room)
    emit_session_updated(room)


@socketio.on("join_room")
def handle_join_room(data):
    room = get_room_from_payload(data)
    if not room:
        emit("session_error", {"error": "Invalid session"})
        return

    if not can_access_room(request.sid, room):
        emit("session_pending", {
            "room": room,
            "text": "Agent approval is required before joining this chat."
        })
        return

    socket_join_room(room)
    sid_sessions[request.sid] = room

    if room in closed_sessions:
        emit("session_closed", {
            "room": room,
            "closed_by": "Support",
            "text": "Chat closed by Support."
        })


@socketio.on("typing")
def handle_typing(data):
    room = get_message_room(data)
    if not room or room in closed_sessions:
        return

    emit("typing", data, to=room, include_self=False)


@socketio.on("close_session")
def handle_close_session(data):
    room = get_message_room(data)
    if not room:
        emit("session_error", {"error": "Invalid session"})
        return

    user = "User"
    if isinstance(data, dict) and isinstance(data.get("user"), str) and data["user"].strip():
        user = data["user"].strip()

    closed_sessions.add(room)
    append_transcript_message(
        room,
        make_system_message(room, f"Chat closed by {user}."),
    )
    session_record = ensure_session_record(room)
    session_record.update({
        "status": "closed",
        "customer_socket_id": approved_customer_by_room.get(room),
        "updated_at": utc_now_iso(),
    })
    emit("session_closed", {
        "room": room,
        "closed_by": user,
        "text": f"Chat closed by {user}."
    }, to=room)
    emit_session_updated(room)


@socketio.on("disconnect")
def handle_disconnect():
    remove_pending_request(request.sid)
    approved_room = approved_customer_sids.pop(request.sid, None)
    if approved_room and approved_customer_by_room.get(approved_room) == request.sid:
        approved_customer_by_room[approved_room] = None
    approved_customer = approved_customer_tokens.get(approved_room)
    if approved_customer and approved_customer.get("sid") == request.sid:
        approved_customer["sid"] = None
    authenticated_agent_sids.discard(request.sid)
    sid_sessions.pop(request.sid, None)


def get_room_from_payload(data):
    if not isinstance(data, dict):
        return None

    session_id = data.get("session_id") or data.get("room")
    if session_id in active_sessions:
        return session_id

    return None


def get_message_room(data):
    room = get_room_from_payload(data)
    if room and can_access_room(request.sid, room):
        return room

    room = sid_sessions.get(request.sid)
    if room and can_access_room(request.sid, room):
        return room

    return None


def can_access_room(sid, room):
    if room not in active_sessions:
        return False

    if room == DEFAULT_SESSION_ID:
        return True

    if is_authenticated_agent_socket():
        return True

    return approved_customer_sids.get(sid) == room


def make_system_message(room, text):
    return {
        "session_id": room,
        "type": "system",
        "user": "System",
        "text": text,
        "timestamp": utc_now_iso(),
    }


def append_transcript_message(room, message, increment_unread=False):
    if room not in active_sessions or not isinstance(message, dict):
        return

    message_type = message.get("type")
    if message_type not in {"text", "image", "system"}:
        return

    entry = {
        "type": message_type,
        "user": clean_customer_field(message.get("user"), "User"),
        "timestamp": message.get("timestamp") or datetime.utcnow().isoformat() + "Z",
    }

    if message_type in {"text", "system"}:
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        entry["text"] = text.strip()
    elif message_type == "image":
        url = message.get("url")
        if not isinstance(url, str) or not url.strip():
            return
        entry["url"] = url.strip()

    chat_transcripts.setdefault(room, []).append(entry)
    update_session_from_message(room, entry, increment_unread=increment_unread)


def ensure_session_record(session_id, agent_name=""):
    if session_id in session_records:
        return session_records[session_id]

    now = utc_now_iso()
    session_records[session_id] = {
        "session_id": session_id,
        "customer_name": "",
        "customer_email": "",
        "status": "waiting",
        "agent_name": agent_name,
        "created_at": now,
        "updated_at": now,
        "joined_at": None,
        "last_message": "Session link created",
        "unread_count": 0,
        "customer_socket_id": None,
    }
    return session_records[session_id]


def update_session_from_message(room, message, increment_unread=False):
    session_record = ensure_session_record(room)
    session_record["last_message"] = message_preview(message)
    session_record["updated_at"] = message.get("timestamp") or utc_now_iso()
    if room in closed_sessions:
        session_record["status"] = "closed"
    elif session_record.get("status") != "waiting":
        session_record["status"] = "active"
    if increment_unread and message.get("type") != "system":
        session_record["unread_count"] = session_record.get("unread_count", 0) + 1


def touch_session(room):
    ensure_session_record(room)["updated_at"] = utc_now_iso()


def emit_session_updated(room):
    if room == DEFAULT_SESSION_ID or room not in active_sessions:
        return

    socketio.emit(
        "session_updated",
        {"session": serialize_session(room)},
        to=AGENT_DASHBOARD_ROOM,
    )


def serialize_session(session_id):
    session_record = ensure_session_record(session_id)
    return {
        "session_id": session_record["session_id"],
        "customer_name": session_record.get("customer_name") or "Waiting for customer",
        "customer_email": session_record.get("customer_email", ""),
        "status": "closed" if session_id in closed_sessions else session_record.get("status", "waiting"),
        "agent_name": session_record.get("agent_name", ""),
        "created_at": session_record.get("created_at"),
        "updated_at": session_record.get("updated_at"),
        "joined_at": session_record.get("joined_at"),
        "last_message": session_record.get("last_message", ""),
        "unread_count": session_record.get("unread_count", 0),
        "customer_socket_id": session_record.get("customer_socket_id"),
        "link": url_for("chat_session", session_id=session_id),
    }


def serialize_message(message, session_id):
    return {
        "session_id": session_id,
        "type": message.get("type", "text"),
        "user": message.get("user", "User"),
        "text": message.get("text", ""),
        "url": message.get("url", ""),
        "timestamp": message.get("timestamp") or utc_now_iso(),
    }


def message_preview(message):
    if message.get("type") == "image":
        return "Image uploaded"

    return (message.get("text") or "").strip()[:140]


def utc_now_iso():
    return datetime.utcnow().isoformat() + "Z"


def build_transcript(room):
    saved_at = datetime.now().strftime("%Y-%m-%d %I:%M %p")
    lines = [
        "Cortex Chat Transcript",
        f"Session: {room}",
        f"Saved: {saved_at}",
        "",
        "--------------------------------",
        "",
    ]

    for entry in chat_transcripts.get(room, []):
        timestamp = format_transcript_timestamp(entry.get("timestamp"))
        user = entry.get("user") or "User"
        lines.append(f"[{timestamp}] {user}:")

        if entry.get("type") == "image":
            lines.append(f"[Image uploaded: {entry.get('url', '')}]")
        else:
            lines.append(entry.get("text", ""))

        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def format_transcript_timestamp(value):
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.strftime("%I:%M %p").lstrip("0")
        except ValueError:
            return value

    return datetime.utcnow().strftime("%I:%M %p").lstrip("0")


def validate_customer_token(room, token):
    if not isinstance(token, str) or not token:
        return False

    approved_customer = approved_customer_tokens.get(room)
    if not approved_customer:
        return False

    return secrets.compare_digest(token, approved_customer["token"])


def authenticate_agent_socket(username=None):
    if not session.get("agent_logged_in"):
        return False

    session_username = session.get("agent_username", "")
    if username and not secrets.compare_digest(str(username), str(session_username)):
        return False

    authenticated_agent_sids.add(request.sid)
    socket_join_room(AGENT_DASHBOARD_ROOM)
    return True


def is_authenticated_agent_socket():
    return request.sid in authenticated_agent_sids or bool(session.get("agent_logged_in"))


def clean_customer_field(value, fallback):
    if isinstance(value, str) and value.strip():
        return value.strip()[:120]

    return fallback


def request_payload(room, join_request):
    return {
        "room": room,
        "customerSocketId": join_request["sid"],
        "name": join_request["name"],
        "email": join_request["email"],
    }


def remove_pending_request(sid):
    for room in list(pending_join_requests):
        pending_join_requests[room] = [
            join_request
            for join_request in pending_join_requests[room]
            if join_request["sid"] != sid
        ]
        if not pending_join_requests[room]:
            pending_join_requests.pop(room, None)


def find_pending_request(data):
    if not isinstance(data, dict):
        return None, None, None

    room = data.get("room") or data.get("session_id")
    customer_sid = data.get("customerSocketId")
    if room not in active_sessions or not isinstance(customer_sid, str):
        return None, None, None

    for join_request in pending_join_requests.get(room, []):
        if join_request["sid"] == customer_sid:
            return room, customer_sid, join_request

    return room, customer_sid, None


if __name__ == "__main__":
    socketio.run(
        app,
        host="127.0.0.1",
        port=3000,
        debug=True,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
    )
