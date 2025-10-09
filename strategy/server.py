# server.py — final streaming server (use in development)
from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
import subprocess
import threading
import os
import time
import sys
import yaml
import codecs

import os, sys, time, codecs
from dotenv import load_dotenv

load_dotenv()   # <-- add this near file top, before start_strategy uses env


app = Flask(__name__, static_folder='static')
CORS(app)


CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "configs\survivor.yml"))

# Read config
def read_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

# Write config
def write_config(data):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

@app.route("/config", methods=["GET"])
def get_config():
    try:
        config = read_config()
        return jsonify({"status": "success", "config": config.get("default", {})})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/config", methods=["POST"])
def update_config():
    try:
        updates = request.json
        config = read_config()
        default_config = config.get("default", {})

        # Update only existing keys
        for k, v in updates.items():
            if k in default_config:
                default_config[k] = v

        # Save back
        config["default"] = default_config
        write_config(config)
        return jsonify({"status": "success", "message": "Config updated"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

process = None
log_lock = threading.Lock()
LOGFILE = "survivor_output.log"


@app.route('/')
def serve_home():
    index_path = os.path.join(app.static_folder, "terminal.html")
    if os.path.exists(index_path):
        return send_from_directory(app.static_folder, 'terminal.html')
    return jsonify({"status": "error", "message": "Frontend not found"}), 404


def background_log_writer(stdout_pipe, proc):
    """Read raw bytes using os.read and write decoded text to a log file."""
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    fd = stdout_pipe.fileno()
    try:
        try:
            os.set_blocking(fd, False)
        except Exception:
            pass

        with open(LOGFILE, "a", encoding="utf-8") as f:
            while True:
                try:
                    chunk = os.read(fd, 8192)
                except BlockingIOError:
                    if proc.poll() is not None:
                        rest = decoder.decode(b'', final=True)
                        if rest:
                            with log_lock:
                                f.write(rest); f.flush()
                        break
                    time.sleep(0.02)
                    continue
                except OSError:
                    break

                if chunk:
                    text = decoder.decode(chunk)
                    with log_lock:
                        f.write(text); f.flush()
                else:
                    if proc.poll() is not None:
                        rest = decoder.decode(b'', final=True)
                        if rest:
                            with log_lock:
                                f.write(rest); f.flush()
                        break
                    time.sleep(0.02)
    except Exception:
        pass


@app.route('/start', methods=['POST'])
def start_strategy():
    global process
    try:
        if process and process.poll() is None:
            return jsonify({"status": "error", "message": "Strategy already running."}), 400

        SURVIVOR_PATH = os.path.join(os.path.dirname(__file__), "survivor.py")
        if not os.path.exists(SURVIVOR_PATH):
            return jsonify({"status": "error", "message": "survivor.py not found"}), 404

        # Use same interpreter, unbuffered. Force ALWAYS_CONFIRM in child env for dev.
        cmd = [sys.executable, "-u", SURVIVOR_PATH]
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["ALWAYS_CONFIRM"] = "1"   # <--- development: ensure prompt always appears (remove in production)

        proc = subprocess.Popen(
            cmd,
            cwd=os.path.dirname(SURVIVOR_PATH),
            stdout=subprocess.PIPE,    # binary
            stderr=subprocess.STDOUT,  # merged to stdout
            stdin=subprocess.PIPE,     # binary
            bufsize=0,
            env=os.environ.copy()
        )

        # truncate previous logfile for fresh run
        try:
            open(LOGFILE, "w", encoding="utf-8").close()
        except Exception:
            pass

        process = proc

        # Start background logger thread that writes to file
        threading.Thread(target=background_log_writer, args=(proc.stdout, proc), daemon=True).start()

        return jsonify({"status": "started", "message": f"✅ Survivor strategy started successfully (PID {proc.pid})"}), 200
    except Exception as e:
        app.logger.exception("Failed to start strategy")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/stop', methods=['POST'])
def stop_strategy():
    global process
    try:
        if not process:
            return jsonify({"status": "error", "message": "Not running"}), 400
        proc = process
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
        process = None
        return jsonify({"status": "stopped", "message": "✅ Strategy stopped successfully."}), 200
    except Exception as e:
        process = None
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/status', methods=['GET'])
def status():
    global process
    running = process is not None and process.poll() is None
    pid = process.pid if running else None
    return jsonify({"running": running, "pid": pid}), 200


@app.route('/logs', methods=['GET'])
def stream_realtime_logs():
    global process
    local_proc = process
    if not local_proc or local_proc.poll() is not None:
        return Response("Process not running.\n", mimetype='text/plain; charset=utf-8')

    def generate():
        fd = local_proc.stdout.fileno()
        try:
            os.set_blocking(fd, False)
        except Exception:
            pass

        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        text_buffer = ''
        last_data_time = None
        debounce_secs = 0.05

        while True:
            try:
                chunk = os.read(fd, 4096)
            except (BlockingIOError, OSError):
                chunk = b''

            if chunk:
                text_buffer += decoder.decode(chunk)
                while '\n' in text_buffer:
                    line, text_buffer = text_buffer.split('\n', 1)
                    yield f"data: {line}\n\n"  # SSE format
            else:
                if local_proc.poll() is not None:
                    rest = decoder.decode(b'', final=True)
                    if rest:
                        yield f"data: {rest}\n\n"
                    break
                time.sleep(0.02)

    return Response(generate(), mimetype='text/event-stream')



@app.route('/input', methods=['POST'])
def send_input():
    global process
    if not process or process.poll() is not None:
        return jsonify({"status": "error", "message": "Process not running"}), 400
    data = request.get_json()
    user_input = data.get("input", "")
    if user_input is None or user_input == "":
        return jsonify({"status": "error", "message": "No input provided"}), 400
    try:
        to_send = (user_input + "\n").encode("utf-8")
        process.stdin.write(to_send)
        process.stdin.flush()
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/lastlog', methods=['GET'])
def lastlog():
    """Return tail of the logfile for debugging"""
    nbytes = int(request.args.get('nbytes', '8000'))
    if not os.path.exists(LOGFILE):
        return jsonify({"status": "ok", "log": ""})
    try:
        with open(LOGFILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - nbytes)
            f.seek(start)
            tail = f.read().decode('utf-8', errors='replace')
        return jsonify({"status": "ok", "log": tail})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.errorhandler(404)
def not_found(e):
    return jsonify({"status": "error", "message": "Endpoint not found"}), 404


@app.errorhandler(500)
def internal_error(e):
    return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == '__main__':
    print("🚀 Flask server running at http://127.0.0.1:8000")
    app.run(host='0.0.0.0', port=8000, debug=True)
