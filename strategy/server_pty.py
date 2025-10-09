import os
import sys
import shlex
import threading
from pathlib import Path
from flask import Flask, request, send_from_directory, jsonify
from flask_socketio import SocketIO, emit
from flask_cors import CORS
import subprocess

IS_WINDOWS = sys.platform == "win32"

if not IS_WINDOWS:
    import pty
    import select
    import signal
    import fcntl
    import struct
    import termios

# ---------------- CONFIG ----------------
HOST = "0.0.0.0"
PORT = 8000
STATIC_DIR = Path(__file__).parent / "static"
ADMIN_TOKEN = "my-secret-token"  # e.g. "my-secret-token"
# ----------------------------------------

app = Flask(__name__, static_folder=str(STATIC_DIR))
socketio = SocketIO(app, async_mode="eventlet", cors_allowed_origins="*")
CORS(app)

@app.route('/')
def serve_home():
    return send_from_directory(app.static_folder, 'terminal_pty.html')

# Global state
pty_master_fd = None
pty_pid = None
pty_proc = None
pty_lock = threading.Lock()
reading_thread = None

def _check_token(headers):
    if ADMIN_TOKEN is None:
        return True
    token = headers.get("X-ADMIN-TOKEN") or request.args.get("token")
    return token == ADMIN_TOKEN

def _set_pty_size(master_fd, rows, cols):
    winsz = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsz)

def _spawn_pty(cmd_list, cwd=None, env=None, cols=120, rows=30):
    global pty_master_fd, pty_pid
    cwd = cwd or os.getcwd()
    env = env or os.environ.copy()
    pid, master_fd = pty.fork()
    if pid == 0:
        try:
            os.chdir(cwd)
        except Exception:
            pass
        os.execvpe(cmd_list[0], cmd_list, env)
    else:
        _set_pty_size(master_fd, rows, cols)
        pty_master_fd = master_fd
        pty_pid = pid
        return pid, master_fd

def _spawn_subprocess(cmd_list, cwd=None, env=None):
    global pty_proc
    cwd = cwd or os.getcwd()
    env = env or os.environ.copy()
    proc = subprocess.Popen(
        cmd_list,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE,
        text=True,
        encoding="utf-8"  # 👈 Force UTF-8 decoding
    )
    pty_proc = proc
    return proc

def _reader_loop(master_fd=None, proc=None):
    try:
        while True:
            if IS_WINDOWS and proc:
                data = proc.stdout.readline()
                if not data:
                    break
                socketio.emit("output", data)
            elif master_fd:
                r, _, _ = select.select([master_fd], [], [], 0.5)
                if master_fd in r:
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    socketio.emit("output", data.decode("utf-8", errors="replace"))
    finally:
        socketio.emit("process-exit", {"pid": pty_pid})
        _cleanup_pty()

def _cleanup_pty():
    global pty_master_fd, pty_pid, pty_proc, reading_thread
    with pty_lock:
        try:
            if pty_master_fd:
                try:
                    os.close(pty_master_fd)
                except Exception:
                    pass
            if pty_proc:
                try:
                    pty_proc.kill()
                except Exception:
                    pass
            pty_master_fd = None
            pty_pid = None
            pty_proc = None
        finally:
            reading_thread = None

@app.route("/run", methods=["POST"])
def run_cmd():
    if not _check_token(request.headers):
        return jsonify({"error": "unauthorized"}), 403

    payload = request.get_json(silent=True) or {}
    cmd = payload.get("cmd")
    if not cmd:
        return jsonify({"error": "cmd required"}), 400

    cols = int(payload.get("cols", 120))
    rows = int(payload.get("rows", 30))
    cwd = payload.get("cwd", None)
    env = payload.get("env", None)

    with pty_lock:
        global pty_master_fd, pty_pid, pty_proc, reading_thread
        if pty_pid or pty_proc:
            return jsonify({"error": "process already running"}), 400

        if isinstance(cmd, str):
            cmd_list = shlex.split(cmd)
        elif isinstance(cmd, list):
            cmd_list = cmd
        else:
            return jsonify({"error": "cmd must be string or list"}), 400

        if cmd_list and Path(cmd_list[0]).name.startswith("python"):
            if "-u" not in cmd_list:
                cmd_list.insert(1, "-u")

        try:
            if IS_WINDOWS:
                proc = _spawn_subprocess(cmd_list, cwd=cwd, env=(env or os.environ.copy()))
                reading_thread = threading.Thread(target=_reader_loop, kwargs={"proc": proc}, daemon=True)
                reading_thread.start()
                return jsonify({"status": "started", "pid": proc.pid}), 200
            else:
                pid, master_fd = _spawn_pty(cmd_list, cwd=cwd, env=(env or os.environ.copy()), cols=cols, rows=rows)
                reading_thread = threading.Thread(target=_reader_loop, args=(master_fd,), daemon=True)
                reading_thread.start()
                return jsonify({"status": "started", "pid": pid}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500

@app.route("/stop", methods=["POST"])
def stop_cmd():
    if not _check_token(request.headers):
        return jsonify({"error": "unauthorized"}), 403

    with pty_lock:
        global pty_pid, pty_proc
        if not pty_pid and not pty_proc:
            return jsonify({"status": "no process"}), 200
        try:
            if IS_WINDOWS and pty_proc:
                pty_proc.terminate()
            elif pty_pid:
                os.kill(pty_pid, signal.SIGTERM)
        except Exception:
            try:
                if IS_WINDOWS and pty_proc:
                    pty_proc.kill()
                elif pty_pid:
                    os.kill(pty_pid, signal.SIGKILL)
            except Exception as e:
                return jsonify({"error": str(e)}), 500
        return jsonify({"status": "stopped"}), 200

@app.route("/status", methods=["GET"])
def status():
    with pty_lock:
        return jsonify({
            "running": pty_pid is not None or pty_proc is not None,
            "pid": pty_pid or (pty_proc.pid if pty_proc else None)
        }), 200

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(str(STATIC_DIR), filename)

@socketio.on("connect")
def ws_connect():
    emit("welcome", {"msg": "connected"})

@socketio.on("client-input")
def on_client_input(data):
    text = data.get("input", "")
    if not text:
        return
    with pty_lock:
        try:
            if IS_WINDOWS and pty_proc:
                pty_proc.stdin.write(text + "\n")
                pty_proc.stdin.flush()
            elif pty_master_fd:
                os.write(pty_master_fd, text.encode("utf-8", errors="replace"))
            else:
                emit("error", {"msg": "no process"})
        except Exception as e:
            emit("error", {"msg": str(e)})

@socketio.on("resize")
def on_resize(data):
    if IS_WINDOWS:
        return  # No PTY resizing on Windows
    cols = int(data.get("cols", 120))
    rows = int(data.get("rows", 30))
    with pty_lock:
        if pty_master_fd:
            try:
                _set_pty_size(pty_master_fd, rows, cols)
            except Exception as e:
                emit("error", {"msg": str(e)})

@socketio.on("disconnect")
def ws_disconnect():
    pass

if __name__ == "__main__":
    if not STATIC_DIR.exists():
        STATIC_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Starting server on http://{HOST}:{PORT} (static served from {STATIC_DIR})")
    socketio.run(app, host=HOST, port=PORT)