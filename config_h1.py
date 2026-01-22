"""
Configuration file for H1 Temperature Monitor Dashboard
Contains motor mappings, constants, and configuration settings
"""

# Motor ID to name mapping for H1 robot (20 motors)
# Based on official Unitree H1 documentation joint order
# Reference: https://docs.quadruped.de/projects/h1/html/h1_overview.html
MOTOR_NAMES = {
    0: "Right Hip Roll",
    1: "Right Hip Pitch",
    2: "Right Knee",
    3: "Left Hip Roll",
    4: "Left Hip Pitch",
    5: "Left Knee",
    6: "Torso",
    7: "Left Hip Yaw",
    8: "Right Hip Yaw",  # Inferred - should be near zero when standing
    9: "Reserved",      # Unknown/unused
    10: "Left Ankle",
    11: "Right Ankle",
    12: "Right Shoulder Pitch",
    13: "Right Shoulder Roll",
    14: "Right Shoulder Yaw",
    15: "Right Elbow",
    16: "Left Shoulder Pitch",
    17: "Left Shoulder Roll",
    18: "Left Shoulder Yaw",
    19: "Left Elbow",
}

# Motor ID to mesh link name mapping (based on URDF structure)
# Maps each motor to their corresponding visual mesh link
# Motor order matches the official H1 joint order from documentation
MOTOR_TO_MESH = {
    0: "right_hip_roll_link",
    1: "right_hip_pitch_link",
    2: "right_knee_link",
    3: "left_hip_roll_link",
    4: "left_hip_pitch_link",
    5: "left_knee_link",
    6: "torso_link",
    7: "left_hip_yaw_link",
    8: "right_hip_yaw_link",
    # 9: Reserved/unused
    10: "left_ankle_link",
    11: "right_ankle_link",
    12: "right_shoulder_pitch_link",
    13: "right_shoulder_roll_link",
    14: "right_shoulder_yaw_link",
    15: "right_elbow_link",
    16: "left_shoulder_pitch_link",
    17: "left_shoulder_roll_link",
    18: "left_shoulder_yaw_link",
    19: "left_elbow_link",
}

# Temperature thresholds (in Celsius)
TEMP_MIN = 30
TEMP_MAX = 120
TEMP_WARM_THRESHOLD = 45
TEMP_HOT_THRESHOLD = 60

# URDF configuration
#URDF_FILENAME = "h1.urdf"
#URDF_FILENAME = "h1_hand.urdf"  # Uses .dae files (not supported)
URDF_FILENAME = "h1_hand_stl.urdf"  # Uses .STL files (supported)
URDF_PATH = "assets/h1"

# Network configuration
DEFAULT_PORT = 8082
DEFAULT_HOST = "0.0.0.0"
