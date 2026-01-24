import sys
import time
import os
import secrets
import logging
import argparse
from threading import Lock
from flask import Flask, render_template, jsonify, send_from_directory
from flask_socketio import SocketIO

from unitree_sdk2py.core.channel import ChannelSubscriber, ChannelFactoryInitialize

# Robot type will be set at runtime
ROBOT_TYPE = None
MOTOR_NAMES = None
MOTOR_TO_MESH = None
URDF_FILENAME = None
URDF_PATH = None
DEFAULT_PORT = None
DEFAULT_HOST = None
LowState_ = None

app = Flask(__name__)
# Use environment variable for secret key, fallback to random key for security
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', secrets.token_hex(16))
app.config['TEMPLATES_AUTO_RELOAD'] = True
socketio = SocketIO(app, cors_allowed_origins="*", logger=False, engineio_logger=False)

# Configure logging to suppress routine werkzeug messages
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# Global variable to store latest motor data
motor_data = {
    'temperatures': [],
    'positions': [],
    'timestamp': 0
}
data_lock = Lock()


def load_robot_config(robot_type):
    """Load configuration based on robot type."""
    global ROBOT_TYPE, MOTOR_NAMES, MOTOR_TO_MESH, URDF_FILENAME, URDF_PATH, DEFAULT_PORT, DEFAULT_HOST, LowState_
    
    ROBOT_TYPE = robot_type.upper()
    
    if ROBOT_TYPE == 'G1':
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_ as G1_LowState
        from config_g1 import (
            MOTOR_NAMES as G1_MOTOR_NAMES,
            MOTOR_TO_MESH as G1_MOTOR_TO_MESH,
            URDF_FILENAME as G1_URDF_FILENAME,
            URDF_PATH as G1_URDF_PATH,
            DEFAULT_PORT as G1_DEFAULT_PORT,
            DEFAULT_HOST as G1_DEFAULT_HOST
        )
        LowState_ = G1_LowState
        MOTOR_NAMES = G1_MOTOR_NAMES
        MOTOR_TO_MESH = G1_MOTOR_TO_MESH
        URDF_FILENAME = G1_URDF_FILENAME
        URDF_PATH = G1_URDF_PATH
        DEFAULT_PORT = G1_DEFAULT_PORT
        DEFAULT_HOST = G1_DEFAULT_HOST
        
    elif ROBOT_TYPE == 'H1':
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_ as H1_LowState
        from config_h1 import (
            MOTOR_NAMES as H1_MOTOR_NAMES,
            MOTOR_TO_MESH as H1_MOTOR_TO_MESH,
            URDF_FILENAME as H1_URDF_FILENAME,
            URDF_PATH as H1_URDF_PATH,
            DEFAULT_PORT as H1_DEFAULT_PORT,
            DEFAULT_HOST as H1_DEFAULT_HOST
        )
        LowState_ = H1_LowState
        MOTOR_NAMES = H1_MOTOR_NAMES
        MOTOR_TO_MESH = H1_MOTOR_TO_MESH
        URDF_FILENAME = H1_URDF_FILENAME
        URDF_PATH = H1_URDF_PATH
        DEFAULT_PORT = H1_DEFAULT_PORT
        DEFAULT_HOST = H1_DEFAULT_HOST
        
    else:
        raise ValueError(f"Unknown robot type: {robot_type}. Must be 'g1' or 'h1'")


def low_state_callback(msg):
    """Callback function to process received LowState data and update dashboard."""
    global motor_data
    
    if hasattr(msg, 'motor_state') and len(msg.motor_state) > 0:
        temps = []
        positions = []
        for i, motor in enumerate(msg.motor_state):
            # G1: Only process motors 0-28 (29 actual motors with DOF)
            # H1: Process all motors we have mappings for
            if ROBOT_TYPE == 'G1' and i >= 29:
                continue
            if ROBOT_TYPE == 'H1' and i >= len(MOTOR_NAMES):
                continue
                
            motor_info = {
                'motor_id': i,
                'motor_name': MOTOR_NAMES.get(i, f'Motor {i}'),
                'mesh_name': MOTOR_TO_MESH.get(i, ''),
            }
            
            # Add temperature data - handle both G1 and H1 formats
            if hasattr(motor, 'temperature'):
                temp = motor.temperature
                try:
                    # Try array format (like G1 with [surface, winding])
                    if hasattr(temp, '__len__') and len(temp) >= 2:
                        motor_info['surface'] = int(temp[0])
                        motor_info['winding'] = int(temp[1])
                        motor_info['temp1'] = int(temp[0])
                        motor_info['temp2'] = int(temp[1])
                        motor_info['avg'] = (int(temp[0]) + int(temp[1])) / 2.0
                    elif hasattr(temp, '__len__') and len(temp) == 1:
                        # Single temperature value in array
                        motor_info['surface'] = int(temp[0])
                        motor_info['winding'] = int(temp[0])
                        motor_info['temp1'] = int(temp[0])
                        motor_info['temp2'] = int(temp[0])
                        motor_info['avg'] = int(temp[0])
                    else:
                        # Single temperature value (not array) - H1 format
                        motor_info['surface'] = int(temp)
                        motor_info['winding'] = int(temp)
                        motor_info['temp1'] = int(temp)
                        motor_info['temp2'] = int(temp)
                        motor_info['avg'] = int(temp)
                except (TypeError, AttributeError):
                    # Fallback: treat as single value
                    motor_info['surface'] = int(temp)
                    motor_info['winding'] = int(temp)
                    motor_info['temp1'] = int(temp)
                    motor_info['temp2'] = int(temp)
                    motor_info['avg'] = int(temp)

            # Add position data
            if hasattr(motor, 'q'):
                motor_info['position'] = float(motor.q)
                positions.append({
                    'motor_id': i,
                    'position': float(motor.q),
                    'link_name': MOTOR_TO_MESH.get(i, None),
                })
            
            # Add velocity and torque for future use
            if hasattr(motor, 'dq'):
                motor_info['velocity'] = float(motor.dq)
            if hasattr(motor, 'tau_est'):
                motor_info['torque'] = float(motor.tau_est)
            
            temps.append(motor_info)
        
        with data_lock:
            motor_data['temperatures'] = temps
            motor_data['positions'] = positions
            motor_data['timestamp'] = time.time()
        
        # Emit data to all connected clients
        socketio.emit('motor_update', motor_data)


def init_robot_subscriber(network_interface=None):
    """Initialize the robot data subscriber."""
    print("Initializing robot connection...")
    print(f"Network interface: {network_interface}")
    
    domain_id = 0
    
    try:
        if network_interface:
            print(f"Initializing with domain {domain_id} and interface {network_interface}")
            ChannelFactoryInitialize(domain_id, network_interface)
        else:
            print(f"Initializing with domain {domain_id}")
            ChannelFactoryInitialize(domain_id)
    except Exception as e:
        print(f"Warning during ChannelFactoryInitialize: {e}")
    
    print("Creating subscriber for topic: rt/lowstate")
    lowstate_subscriber = ChannelSubscriber("rt/lowstate", LowState_)
    
    print("Initializing subscriber callback...")
    lowstate_subscriber.Init(low_state_callback, 10)
    
    print("Robot subscriber initialized successfully!")
    print("Waiting for messages on rt/lowstate...")


@app.route('/')
def index():
    """Serve the main 3D dashboard page."""
    if ROBOT_TYPE == 'G1':
        return render_template('index_g1.html')
    else:  # H1
        return render_template('index_h1.html')


@app.route('/api/motors')
def get_motors():
    """API endpoint to get current motor data."""
    with data_lock:
        return jsonify(motor_data)


@app.route('/api/motor_mapping')
def get_motor_mapping():
    """API endpoint to get motor-to-mesh mapping."""
    return jsonify({
        'motor_names': MOTOR_NAMES,
        'motor_to_mesh': MOTOR_TO_MESH
    })


@app.route('/assets/<path:filename>')
def serve_assets(filename):
    """Serve STL files and other assets."""
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



def run_flask_app():
    """Run the Flask application."""
    socketio.run(app, host=DEFAULT_HOST, port=DEFAULT_PORT, debug=False)


def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='Unitree Robot 3D Motor Dashboard')
    parser.add_argument('--robot', '-r', type=str, choices=['g1', 'h1'], required=True,
                        help='Robot type: g1 or h1')
    parser.add_argument('--interface', '-i', type=str, default=None,
                        help='Network interface (e.g., en0, eth0, enp3s0)')
    args = parser.parse_args()
    
    # Load robot configuration
    try:
        load_robot_config(args.robot)
        print(f"Loaded configuration for {ROBOT_TYPE} robot")
    except Exception as e:
        print(f"Error loading robot configuration: {e}")
        sys.exit(1)
    
    print(f"Using network interface: {args.interface}")
    
    # Initialize robot subscriber
    try:
        init_robot_subscriber(args.interface)
    except Exception as e:
        print(f"Error initializing robot connection: {e}")
        print("Starting dashboard anyway (no live data will be available)")
    
    # Start Flask app
    print("\n" + "="*50)
    print(f"{ROBOT_TYPE} 3D Motor Temperature Dashboard")
    print("="*50)
    print(f"Dashboard available at: http://localhost:{DEFAULT_PORT}")
    print("Press Ctrl+C to exit")
    print("="*50 + "\n")
    
    try:
        run_flask_app()
    except KeyboardInterrupt:
        print("\nShutting down dashboard...")


if __name__ == "__main__":
    main()
