import os
import secrets
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Flask,
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
sid_sessions = {}
pending_join_requests = {}
approved_customer_sids = {}
approved_customer_by_room = {}
authenticated_agent_sids = set()


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
    )


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

    emit("join_pending", {
        "room": room,
        "text": "Waiting for agent approval..."
    })
    emit("join_request", request_payload(room, join_request), to=AGENT_DASHBOARD_ROOM)


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
    sid_sessions[customer_sid] = room
    approved_customer_sids[customer_sid] = room
    approved_customer_by_room[room] = customer_sid

    emit("join_approved", {"room": room}, to=customer_sid)
    emit("join_request_removed", {
        "room": room,
        "customerSocketId": customer_sid
    }, to=AGENT_DASHBOARD_ROOM)
    emit("chat_message", {
        "session_id": room,
        "type": "system",
        "user": "System",
        "text": "Customer approved and joined the chat.",
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }, to=room)


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
    emit("join_rejected", {
        "room": room,
        "text": "Your request was not approved."
    }, to=customer_sid)
    emit("join_request_removed", {
        "room": room,
        "customerSocketId": customer_sid
    }, to=AGENT_DASHBOARD_ROOM)


@socketio.on("chat_message")
def handle_chat_message(msg):
    room = get_message_room(msg)
    if not room or room in closed_sessions:
        return

    emit("chat_message", msg, to=room)


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
    emit("session_closed", {
        "room": room,
        "closed_by": user,
        "text": f"Chat closed by {user}."
    }, to=room)


@socketio.on("disconnect")
def handle_disconnect():
    remove_pending_request(request.sid)
    approved_room = approved_customer_sids.pop(request.sid, None)
    if approved_room and approved_customer_by_room.get(approved_room) == request.sid:
        approved_customer_by_room.pop(approved_room, None)
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
