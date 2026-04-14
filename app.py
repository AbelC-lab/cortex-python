import os
import time

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename


app = Flask(__name__)
app.config["SECRET_KEY"] = "cortex-secret-key"
app.config["UPLOAD_FOLDER"] = os.path.join("static", "uploads")

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")


@app.route("/")
def index():
    return render_template("index.html")


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


@socketio.on("chat_message")
def handle_chat_message(msg):
    emit("chat_message", msg, broadcast=True)


@socketio.on("typing")
def handle_typing(data):
    emit("typing", data, broadcast=True, include_self=False)


if __name__ == "__main__":
    socketio.run(app, host="127.0.0.1", port=3000, debug=False, allow_unsafe_werkzeug=True)
