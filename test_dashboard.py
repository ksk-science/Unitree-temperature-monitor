#!/usr/bin/env python3
"""
Test script for the 3D dashboard with simulated motor data.
This allows testing the visualization without a real robot connection.
"""

import signal
import sys
import time
import random
import os
import secrets
import threading
from threading import Thread
from flask import Flask, render_template, jsonify, send_from_directory
from flask_socketio import SocketIO

from config_g1 import MOTOR_NAMES, MOTOR_TO_MESH, URDF_FILENAME, URDF_PATH, DEFAULT_PORT, DEFAULT_HOST

from visual import init_visual, cleanup_processes

app = Flask(__name__)
# Use environment variable for secret key, fallback to random key for security
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', secrets.token_hex(16))
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

init_visual(app)

shutdown_requested = False
cleanup_done = False
cleanup_lock = threading.Lock()

def force_exit_after_delay(delay=10):
    """Принудительная очистка и выход через delay секунд."""
    time.sleep(delay)
    with cleanup_lock:
        global cleanup_done
        if not cleanup_done:
            print(f"\n⏱️ Таймаут {delay} сек истёк, выполняю принудительную очистку...")
            cleanup_processes()
            cleanup_done = True
            print("💀 Принудительное завершение.")
            os._exit(1)

def signal_handler(sig, frame):
    global shutdown_requested
    if not shutdown_requested:
        shutdown_requested = True
        print("\n⏳ Получен сигнал завершения, завершаю работу...")
        
        # Запасной таймер на случай зависания (уменьшен до 5 секунд)
        killer = threading.Thread(target=force_exit_after_delay, args=(5,), daemon=True)
        killer.start()
        
        # Завершаем основной поток, вызывая SystemExit -> выполнится finally
        sys.exit(0)
    else:
        print("\n💥 Принудительное завершение (повторный Ctrl+C)")
        with cleanup_lock:
            global cleanup_done
            if not cleanup_done:
                cleanup_processes()
                cleanup_done = True
        os._exit(1)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Motor mappings imported from config.py
motor_data = {'temperatures': [], 'timestamp': 0}

def generate_simulated_data():
    """Generate simulated motor temperature data."""
    temps = []
    for i in range(29):  # G1 has 29 motors (29 DOF)
        # Simulate temperatures between 35°C and 115°C
        base_temp = 40 + random.uniform(0, 60)
        temp1 = int(base_temp + random.uniform(-5, 5))
        temp2 = int(base_temp + random.uniform(0, 10))  # Winding usually hotter
        
        temps.append({
            'motor_id': i,
            'motor_name': MOTOR_NAMES.get(i, f'Motor {i}'),
            'mesh_name': MOTOR_TO_MESH.get(i, ''),
            'temp1': temp1,
            'temp2': temp2,
            'avg': (temp1 + temp2) / 2.0
        })
    
    return {'temperatures': temps, 'timestamp': time.time()}

def update_loop():
    """Continuously update and broadcast simulated data."""
    while True:
        global motor_data
        motor_data = generate_simulated_data()
        socketio.emit('motor_update', motor_data)
        time.sleep(1)  # Update every second

@app.route('/')
def index():
    return render_template('index_g1.html')

@app.route('/api/motors')
def get_motors():
    return jsonify(motor_data)

@app.route('/api/motor_mapping')
def get_motor_mapping():
    return jsonify({'motor_names': MOTOR_NAMES, 'motor_to_mesh': MOTOR_TO_MESH})

@app.route('/assets/<path:filename>')
def serve_assets(filename):
    import os
    assets_path = os.path.join(os.path.dirname(__file__), 'assets')
    return send_from_directory(assets_path, filename)

@app.route('/api/urdf')
def get_urdf():
    """Serve the URDF file for parsing."""
    urdf_path = os.path.join(os.path.dirname(__file__), URDF_PATH, URDF_FILENAME)
    try:
        with open(urdf_path, 'r') as f:
            return f.read(), 200, {'Content-Type': 'application/xml'}
    except Exception as e:
        return jsonify({'error': str(e)}), 404


@app.route('/assets/js/<path:filename>')
def serve_js(filename):
    """Serve JavaScript files for offline support."""
    js_path = os.path.join(os.path.dirname(__file__), 'assets', 'js')
    return send_from_directory(js_path, filename)


if __name__ == "__main__":
    print("\n" + "="*60)
    print("G1 3D Temperature Dashboard - TEST MODE")
    print("="*60)
    print("Running with SIMULATED data (no robot connection needed)")
    print(f"Dashboard available at: http://localhost:8081")
    print("Press Ctrl+C to exit")
    print("="*60 + "\n")
    
    # Start update thread
    update_thread = Thread(target=update_loop, daemon=True)
    update_thread.start()
    
    # Generate initial data
    motor_data = generate_simulated_data()
    
    # Run Flask app
    try:
        socketio.run(app, host=DEFAULT_HOST, port=DEFAULT_PORT, debug=False)
    finally:
        with cleanup_lock:
            if not cleanup_done:
                print("🛑 Сервер остановлен, выполняю очистку...")
                cleanup_processes()
                cleanup_done = True
            else:
                print("🛑 Сервер остановлен, очистка уже выполнена.")
        print("✅ Выход.")