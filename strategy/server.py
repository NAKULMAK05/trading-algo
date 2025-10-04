from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import subprocess
import threading
import signal
import os

# Create Flask app
app = Flask(__name__, static_folder='static')
CORS(app)

# Global variable for running strategy process
process = None


# ==============================
# Serve Frontend
# ==============================
@app.route('/')
def serve_home():
    return send_from_directory(app.static_folder, 'index.html')


# ==============================
# Utility: log streaming thread
# ==============================
def stream_logs(stdout):
    """Continuously read survivor.py output and write to survivor_output.log"""
    with open("survivor_output.log", "a") as f:
        for line in stdout:
            f.write(line)
            f.flush()


# ==============================
# Start Strategy Endpoint
# ==============================
@app.route('/start', methods=['POST'])
def start_strategy():
    global process  # <-- Declare global variable

    try:
        # Prevent duplicate runs
        if process and process.poll() is None:
            return jsonify({"status": "error", "message": "Strategy already running."}), 400

        # Parse JSON from frontend
        data = request.get_json(silent=True)
        if data is None:
            return jsonify({"status": "error", "message": "Invalid or missing JSON body"}), 400

        symbol_initials = data.get('symbol_initials', 'NIFTY25OCT')
        pe_gap = data.get('pe_gap', 25)
        ce_gap = data.get('ce_gap', 25)
        request_token = data.get('request_token', '')
        min_price = data.get('min_price_to_sell', 15)

        # Build survivor.py command
        cmd = [
            "python", "survivor.py",
            "--symbol-initials", str(symbol_initials),
            "--pe-gap", str(pe_gap),
            "--ce-gap", str(ce_gap),
            "--min-price-to-sell", str(min_price)
        ]

        # Prepare environment variables
        env = os.environ.copy()
        if request_token:
            env["REQUEST_TOKEN"] = request_token

        # Launch survivor.py
        process = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )

        # Start a background thread to capture logs
        threading.Thread(target=stream_logs, args=(process.stdout,), daemon=True).start()

        return jsonify({
            "status": "started",
            "message": f"Survivor strategy started successfully (PID {process.pid})"
        }), 200

    except Exception as e:
        app.logger.exception("Failed to start strategy")
        return jsonify({"status": "error", "message": str(e)}), 500


# ==============================
# Stop Strategy Endpoint (with fallback)
# ==============================
@app.route('/stop', methods=['POST'])
def stop_strategy():
    global process
    stopped = False
    try:
        if process and process.poll() is None:
            app.logger.info(f"Stopping strategy process PID={process.pid}")
            try:
                process.terminate()  # polite shutdown
                process.wait(timeout=5)  # wait 5 seconds
                stopped = True
            except Exception:
                app.logger.warning("Graceful termination failed, killing process...")
                try:
                    process.kill()  # force kill
                    process.wait(timeout=3)
                    stopped = True
                except Exception as e:
                    app.logger.error(f"Failed to kill process: {e}")
                    stopped = False

            process = None  # clear reference

        else:
            # Process already stopped
            process = None
            stopped = True

        # Fallback message
        msg = "✅ Strategy stopped successfully." if stopped else "❌ Failed to stop strategy."
        return jsonify({"status": "stopped" if stopped else "error", "message": msg, "stopped": stopped}), 200

    except Exception as e:
        app.logger.exception("Error stopping strategy")
        process = None
        return jsonify({"status": "error", "message": str(e), "stopped": False}), 500




# ==============================
# Status Endpoint
# ==============================
@app.route('/status', methods=['GET'])
def status():
    global process
    running = process is not None and process.poll() is None
    return jsonify({"running": running}), 200


# ==============================
# Logs Endpoint
# ==============================
@app.route('/logs', methods=['GET'])
def get_logs():
    if os.path.exists("survivor_output.log"):
        with open("survivor_output.log", "r") as f:
            return jsonify({"logs": f.read()}), 200
    else:
        return jsonify({"logs": "No logs yet"}), 200



# ==============================
# Run Server
# ==============================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)
