import os
import time
import subprocess
import threading
import signal
import queue
import secrets
import tempfile
import re
from flask import Blueprint, render_template, Response, request, make_response, session
import numpy as np
import cv2
from collections import defaultdict
from datetime import datetime, timedelta


print("Checking dependencies for the method of cutting off invisible parts...")
required_utils = ['wmctrl', 'xdotool', 'import', 'xwininfo', 'xprop', 'scrot']
missing_utils = []

for util in required_utils:
    try:
        if util == 'import':
            subprocess.run(["convert", "--version"], capture_output=True)
        elif util == 'xprop':
            subprocess.run(["xprop", "-version"], capture_output=True)
        else:
            subprocess.run([util, "--version"], capture_output=True)
        print(f" {util}: available")
    except:
        missing_utils.append(util)
        print(f" {util}: not found")

if missing_utils:
    print(f"\n Install the missing utilities:")
    print(f"sudo apt install wmctrl x11-apps imagemagick x11-utils xdotool scrot")
    print("Continued in 3 seconds...")
    time.sleep(3)

visual_bp = Blueprint('visual', __name__, url_prefix='/visual')


app_processes = []
client_queues_individual = defaultdict(lambda: defaultdict(lambda: queue.Queue(maxsize=10)))
client_queues_tiled = defaultdict(lambda: queue.Queue(maxsize=10))
client_last_activity = {}
client_session_map = {}
clients_lock = threading.Lock()
CLEANUP_INTERVAL = 5
CLIENT_TIMEOUT = 10
max_windows = 10

print("Starting a virtual display...")
original_display = os.environ.get('DISPLAY', ':0')

xvfb_process = subprocess.Popen(
    ["Xvfb", ":99", "-screen", "0", "1920x1080x24"],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE
)
time.sleep(2)
os.environ['DISPLAY'] = ':99'
print(f"Virtual display started: {os.environ['DISPLAY']}")
subprocess.run(["xhost", "+"], capture_output=True)

apps = [
    "gedit -s",
    "gnome-system-monitor",
]


def force_window_redraw(window_id, app_name=None, display=':99'):
    try:
        try:
            prop_result = subprocess.run(
                f"DISPLAY={display} xprop -id {window_id} WM_TRANSIENT_FOR",
                shell=True, capture_output=True, text=True, timeout=2
            )
            if prop_result.returncode == 0 and 'WM_TRANSIENT_FOR' in prop_result.stdout:
                for line in prop_result.stdout.split('\n'):
                    if 'WM_TRANSIENT_FOR' in line:
                        parts = line.split('=')
                        if len(parts) > 1:
                            parent_id = parts[1].strip().split()[0]
                            if parent_id.startswith('0x'):
                                print(f"  Subwindow {window_id}, redraw the parent {parent_id}")
                                force_window_redraw(parent_id, app_name, display)
                                return
        except:
            pass
        
      
        subprocess.run(
            f"DISPLAY={display} xdotool windowfocus {window_id}",
            shell=True, capture_output=True, timeout=2
        )
        time.sleep(0.05)
        
        
        subprocess.run(
            f"DISPLAY={display} xrefresh -id {window_id}",
            shell=True, capture_output=True, timeout=2
        )
        time.sleep(0.05)
        
        
        try:
            geom_result = subprocess.run(
                f"DISPLAY={display} xwininfo -id {window_id} | grep -E 'Width:|Height:'",
                shell=True, capture_output=True, text=True, timeout=2
            )
            if geom_result.returncode == 0:
                lines = geom_result.stdout.strip().split('\n')
                width = height = 0
                for line in lines:
                    if 'Width:' in line:
                        width = int(line.split(':')[1].strip())
                    elif 'Height:' in line:
                        height = int(line.split(':')[1].strip())
                
                if width > 0 and height > 0:
                    subprocess.run(
                        f"DISPLAY={display} wmctrl -i -r {window_id} -e 0,0,0,{width-1},{height-1}",
                        shell=True, capture_output=True, timeout=2
                    )
                    time.sleep(0.05)
                    subprocess.run(
                        f"DISPLAY={display} wmctrl -i -r {window_id} -e 0,0,0,{width},{height}",
                        shell=True, capture_output=True, timeout=2
                    )
                    time.sleep(0.05)
        except:
            pass
        
    except Exception as e:
        print(f"Error while redrawing the window {window_id}: {e}")





def place_window_on_half(window_id, half='left', display=':99'):
    """
    Places the window strictly on the specified half of the screen.
    
    Args:
        window_id: ID window
        half: 'left' of 'right'
        display: DISPLAY for X-server
    """
    try:
        
        root_result = subprocess.run(
            f"DISPLAY={display} xwininfo -root",
            shell=True, capture_output=True, text=True, timeout=2
        )
        
        screen_width = 1920
        screen_height = 1080
        
        if root_result.returncode == 0:
            for line in root_result.stdout.split('\n'):
                if 'Width:' in line:
                    screen_width = int(line.split(':')[1].strip())
                elif 'Height:' in line:
                    screen_height = int(line.split(':')[1].strip())
        
        print(f"   Screen size: {screen_width}x{screen_height}")
        
       
        half_width = screen_width // 2
        window_height = int(screen_height * 0.9)
        window_y = (screen_height - window_height) // 2
        
        window_name = f"{half} side"
        if half == 'left':
            window_x = 0
        else: window_x = half_width

        
        try:
            geom_result = subprocess.run(
                f"DISPLAY={display} xwininfo -id {window_id}",
                shell=True, capture_output=True, text=True, timeout=2
            )
            
            if geom_result.returncode == 0:
                current_width = screen_width // 2
                current_height = screen_height
                for line in geom_result.stdout.split('\n'):
                    if 'Width:' in line:
                        current_width = int(line.split(':')[1].strip())
                    elif 'Height:' in line:
                        current_height = int(line.split(':')[1].strip())
                
                print(f"  Current window size: {current_width}x{current_height}")
        except:
            pass
        
        
        cmd = f"DISPLAY={display} wmctrl -i -r {window_id} -e 0,{window_x},{window_y},{half_width},{window_height}"
        print(f"  Setting the geometry: {cmd}")
        
        result = subprocess.run(
            cmd,
            shell=True, capture_output=True, text=True, timeout=3
        )
        
        if result.returncode == 0:
            print(f"  Window {window_id} posted on {window_name}")
            
            try:
                subprocess.run(
                    f"DISPLAY={display} wmctrl -i -r {window_id} -b add,maximized_vert",
                    shell=True, capture_output=True, timeout=2
                )
            except:
                pass
            
            return True
        else:
            print(f"  Window placement error: {result.stderr}")
            return False
            
    except Exception as e:
        print(f"   Error in place_window_on_half: {e}")
        import traceback
        traceback.print_exc()
        return False


def place_all_windows_on_halves():
    """Places all found windows on half of the screen."""
    print("Placing windows on half of the screen...")
    
    windows = get_main_window_info()
    
    if not windows:
        print("  No windows found, trying again in 3 seconds...")
        time.sleep(3)
        windows = get_main_window_info()
    
    print(f"  Windows found: {len(windows)}")
    
    
    placed_windows = 0
    
    for window in windows:
        window_id = window.get('id')
        app_name = window.get('app', 'unknown').lower()
        
        if not window_id:
            continue
        
        
        # half = 'right' if next((i for i, w in enumerate(windows) if w.get('app', 'unknown').lower() == app_name), -1) % 2 == 0 else 'left'  
        
        half = 'left' if placed_windows % 2 == 0 else 'right'
        
        print(f"  🪟 Window {window_id} ({app_name}) -> {half} half")
        
        
        if place_window_on_half(window_id, half):
            placed_windows += 1
        
        time.sleep(0.5)
    
    print(f"Posted windows: {placed_windows}/{len(windows)}")
    return placed_windows


def capture_child_window_safe(window_id, app_name=None, display=':99'):
    """
    Safe capture of child windows by redrawing the parent.
    """
    try:
        parent_id = None
        try:
            prop_result = subprocess.run(
                f"DISPLAY={display} xprop -id {window_id} WM_TRANSIENT_FOR",
                shell=True, capture_output=True, text=True, timeout=2
            )
            if prop_result.returncode == 0 and 'WM_TRANSIENT_FOR' in prop_result.stdout:
                for line in prop_result.stdout.split('\n'):
                    if 'WM_TRANSIENT_FOR' in line:
                        parts = line.split('=')
                        if len(parts) > 1:
                            parent_id = parts[1].strip().split()[0]
        except:
            pass
        
        if parent_id:
            force_window_redraw(parent_id, app_name, display)
            # time.sleep(0.1)
        
        temp_file = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
        temp_path = temp_file.name
        temp_file.close()
        
        result = subprocess.run(
            f"DISPLAY={display} xwd -id {window_id} | convert xwd:- {temp_path}",
            shell=True, capture_output=True, timeout=5
        )
        
        if result.returncode == 0:
            img = cv2.imread(temp_path)
        else:
            result = subprocess.run(
                f"DISPLAY={display} import -window {window_id} -quality 100 {temp_path}",
                shell=True, capture_output=True, timeout=5
            )
            img = cv2.imread(temp_path) if result.returncode == 0 else None
        
        try:
            os.unlink(temp_path)
        except:
            pass
        
        return img
        
    except Exception as e:
        print(f"Child window capture error {window_id}: {e}")
        return None


def capture_clean_window(window_id, app_name=None, display=':99', is_child=False):
    """
    Captures a clean image of the window.
    Uses additional processing for child windows.
    """
    
    try:
        if not window_id.startswith('0x'):
            try:
                window_id = f"0x{int(window_id):08x}"
            except ValueError:
                print(f"Incorrect window ID: {window_id}")
                return None
        
        if is_child:
            return capture_child_window_safe(window_id, app_name, display)
        
        
        try:
            focused_result = subprocess.run(
                f"DISPLAY={display} xdotool getwindowfocus",
                shell=True, capture_output=True, text=True, timeout=2
            )
            original_focused = focused_result.stdout.strip()
        except:
            original_focused = None
        
        
        try:
            subprocess.run(
                f"DISPLAY={display} wmctrl -i -r {window_id} -b add,above",
                shell=True, capture_output=True, timeout=2
            )
        except Exception as e:
            print(f"Failed to pop up window {window_id}: {e}")

        try:
            root_result = subprocess.run(
                f"DISPLAY={display} xwininfo -root | grep 'Window id:'",
                shell=True, capture_output=True, text=True, timeout=2
            )
            if root_result.returncode == 0:
                root_line = root_result.stdout
                root_match = re.search(r'0x[0-9a-f]+', root_line)
                if root_match:
                    root_id = root_match.group(0)
                    subprocess.run(
                        f"DISPLAY={display} xdotool windowfocus {root_id}",
                        shell=True, capture_output=True, timeout=2
                    )
        except Exception as e:
            print(f"Failed to remove focus: {e}")
        
       # time.sleep(0.2)
        
        img = None
        temp_file = None
        try:
            temp_file = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
            temp_path = temp_file.name
            temp_file.close()
            
            capture_methods = [
                # Method 1: import
                lambda: subprocess.run(
                    f"DISPLAY={display} import -window {window_id} -frame -quality 100 {temp_path}",
                    shell=True, capture_output=True, timeout=5
                ),
                # Method 2: scrot
                lambda: subprocess.run(
                    f"DISPLAY={display} scrot --focused --border --silent {temp_path}",
                    shell=True, capture_output=True, timeout=5
                ),
                # Method 3: xwd + convert
                lambda: subprocess.run(
                    f"DISPLAY={display} xwd -id {window_id} | convert xwd:- {temp_path}",
                    shell=True, capture_output=True, timeout=5
                )
            ]
            
            for i, method in enumerate(capture_methods):
                try:
                    result = method()
                    if result.returncode == 0:
                        img = cv2.imread(temp_path)
                        if img is not None and img.shape[0] > 10 and img.shape[1] > 10:
                            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                            if cv2.mean(gray)[0] > 10:
                                break
                            else:
                                img = None
                except:
                    continue
            
            try:
                os.unlink(temp_path)
            except:
                pass
                
        except Exception as e:
            print(f"Image capture error: {e}")
            if temp_file:
                try:
                    os.unlink(temp_file.name)
                except:
                    pass
        
        try:
            subprocess.run(
                f"DISPLAY={display} wmctrl -i -r {window_id} -b remove,above",
                shell=True, capture_output=True, timeout=2
            )
        except:
            pass
        
        if original_focused:
            try:
                subprocess.run(
                    f"DISPLAY={display} xdotool windowfocus {original_focused}",
                    shell=True, capture_output=True, timeout=2
                )
            except:
                pass
        
        return img
        
    except Exception as e:
        print(f"Critical error in capture_clean_window: {e}")
        import traceback
        traceback.print_exc()
        return None

def get_main_window_info(display=':99'):
    """Gets info about the main app windows"""
    windows = []
    
    try:
        result = subprocess.run(
            f"DISPLAY={display} wmctrl -l -p -x",
            shell=True, capture_output=True, text=True, timeout=5
        )
        
        if not result.stdout:
            return windows
        
        lines = result.stdout.strip().split('\n')
        
        for line in lines:
            
            parts = line.split()
            if len(parts) < 5:
                continue
            
            window_id = parts[0]
            pid = parts[2] if len(parts) > 2 else '0'
            
            try:
                geom_result = subprocess.run(
                    f"DISPLAY={display} xwininfo -id {window_id}",
                    shell=True, capture_output=True, text=True, timeout=2
                )
                
                if geom_result.returncode == 0:
                    geom_lines = geom_result.stdout.split('\n')
                    width = height = x = y = 0
                    
                    for geom_line in geom_lines:
                        if 'Width:' in geom_line:
                            width = int(geom_line.split(':')[1].strip())
                        elif 'Height:' in geom_line:
                            height = int(geom_line.split(':')[1].strip())
                        elif 'Absolute upper-left X:' in geom_line:
                            x = int(geom_line.split(':')[1].strip())
                        elif 'Absolute upper-left Y:' in geom_line:
                            y = int(geom_line.split(':')[1].strip())
                    
                    if width >= 100 and height >= 100:
                        windows.append({
                            'id': window_id,
                            'app': parts[3],
                            'width': width,
                            'height': height,
                            'x': x,
                            'y': y,
                            'pid': pid,
                            'title': ' '.join(parts[4:]) if len(parts) > 4 else '',
                            'is_main': True
                        })
            except Exception as e:
                print(f"    Error getting window geometry {window_id}: {e}")
                continue
    
    except Exception as e:
        print(f"Error getting information about windows: {e}")
        import traceback
        traceback.print_exc()
    
    return windows



def launch_applications():
    print("Running apps on a virtual display...")
    
    try:
        print("  Start the window manager (openbox)...")
        env = os.environ.copy()
        env['DISPLAY'] = ':99'
        
        wm_process = subprocess.Popen(
            "openbox --sm-disable",
            shell=True,
            env=env,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        app_processes.append(wm_process)
        time.sleep(2)
    except:
        print("  Unable to start the window manager, continuing without it")
    
    for i, app_cmd in enumerate(apps):
        try:
            print(f"  Start: {app_cmd}")
            env = os.environ.copy()
            env['DISPLAY'] = ':99'
            
            
            process = subprocess.Popen(
                app_cmd,
                shell=True,
                env=env,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            
            app_processes.append(process)
            time.sleep(5)
            
            if process.poll() is None:
                print(f"  The application has been started. (PID: {process.pid})")
            else:
                print(f"  The application has finished, code: {process.poll()}")
                
        except Exception as e:
            print(f"  Error: {e}")
    
    print("\nPlacing windows on half of the screen...")
    time.sleep(2)
    
    placed = place_all_windows_on_halves()
    
    if placed == 0:
        print(" Unable to automatically arrange windows, retrying in 3 seconds...")
        time.sleep(3)
        place_all_windows_on_halves()
    
    
    print("\nChecking users on a virtual display...")
    try:
        result = subprocess.run(
            f"DISPLAY=:99 wmctrl -l",
            shell=True, capture_output=True, text=True, timeout=5
        )
        print("  Windows on a virtual display:")
        for line in result.stdout.strip().split('\n'):
            if line:
                print(f"    {line}")
    except Exception as e:
        print(f"  Window validation error: {e}")
    
    print("Application launch complete")


def cleanup_processes():
    print("Completion of processes...")
    
    for process in app_processes:
        try:
            if process.poll() is None:
                print(f"  Application completion PID: {process.pid}")
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
        except Exception as e:
            print(f"  Application completion error: {e}")
    
    try:
        if xvfb_process and xvfb_process.poll() is None:
            print("  Completion Xvfb...")
            xvfb_process.terminate()
            try:
                xvfb_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                xvfb_process.kill()
    except Exception as e:
        print(f"  Completion error Xvfb: {e}")
    
    os.environ['DISPLAY'] = original_display
    print("All processes are complete")



def capture_app_windows():
    try:
        if not hasattr(capture_app_windows, '_windows_placed'):
            print("First window capture, checking placement...")
            place_all_windows_on_halves()
            capture_app_windows._windows_placed = True
        
        windows = []
        
        window_infos = get_main_window_info()
        
        for win_info in window_infos:
            window_id = win_info.get('id')
            app_name = win_info.get('app', 'Unknown')
            width = win_info.get('width', 0)
            height = win_info.get('height', 0)
            
            if not window_id:
                print("  Skip window without ID")
                continue
            
            img = capture_clean_window(window_id, app_name=app_name)
            
            if img is not None and img.shape[0] > 10 and img.shape[1] > 10:
                h, w = img.shape[:2]
                
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                mean_brightness = cv2.mean(gray)[0]
                
                if mean_brightness < 5:
                    print(f"  The image is too dark, skip it.")
                    continue
                
                _, thresh = cv2.threshold(gray, 15, 255, cv2.THRESH_BINARY)
                contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                
                if contours:
                    cnt = max(contours, key=cv2.contourArea)
                    x_cnt, y_cnt, w_cnt, h_cnt = cv2.boundingRect(cnt)
                    
                    if w_cnt > 50 and h_cnt > 50:
                        margin = 5
                        x_start = max(0, x_cnt - margin)
                        y_start = max(0, y_cnt - margin)
                        x_end = min(img.shape[1], x_cnt + w_cnt + margin)
                        y_end = min(img.shape[0], y_cnt + h_cnt + margin)
                        
                        if x_end > x_start and y_end > y_start:
                            img = img[y_start:y_end, x_start:x_end]
                
                windows.append({
                    'id': window_id,
                    'name': f"{app_name}: Main window",
                    'image': img,
                    'width': img.shape[1],
                    'height': img.shape[0],
                    'app': app_name
                })

                if len(windows) >= max_windows:
                    break

        if not windows:
            print("No application windows found, creating placeholders")
            for i, appl in enumerate(apps[:2]):
                app_name = appl.split()[0] if ' ' in appl else appl
                placeholder = np.zeros((400, 600, 3), dtype=np.uint8)
                cv2.putText(placeholder, f"App: {app_name}", (30, 100),
                          cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
                cv2.putText(placeholder, "Wait window...", (30, 140),
                          cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 200, 255), 1)
                cv2.putText(placeholder, "Trying redraw...", (30, 180),
                          cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 200, 200), 1)
                
                windows.append({
                    'id': f'placeholder_{i}',
                    'name': f'{app_name} (placegholder)',
                    'image': placeholder,
                    'width': 600,
                    'height': 400,
                    'app': app_name
                })
        
        return windows
        
    except Exception as e:
        print(f"Error in capture_app_windows: {e}")
        import traceback
        traceback.print_exc()
        return []


def create_tiled_view(windows, max_width=1920, tile_width=400, tile_height=300):
    if not windows:
        empty = np.zeros((tile_height, tile_width, 3), dtype=np.uint8)
        cv2.putText(empty, "No application windows", (50, 150),
                  cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
        return empty
    
    total_windows = len(windows)
    cols = min(3, max(1, max_width // tile_width))
    rows = (total_windows + cols - 1) // cols
    
    tiled_image = np.zeros((rows * tile_height, cols * tile_width, 3), dtype=np.uint8)
    
    for i, window in enumerate(windows):
        row = i // cols
        col = i % cols
        
        img = window['image']
        h, w = img.shape[:2]
        
        scale = min(tile_width / w, tile_height / h) * 0.9
        new_w = int(w * scale)
        new_h = int(h * scale)
        
        if new_w != w or new_h != h:
            img = cv2.resize(img, (new_w, new_h))
        
        x_offset = col * tile_width + (tile_width - new_w) // 2
        y_offset = row * tile_height + (tile_height - new_h) // 2
        
        tiled_image[y_offset:y_offset+new_h, x_offset:x_offset+new_w] = img
    
    return tiled_image

def cleanup_inactive_clients():
    with clients_lock:
        current_time = time.time()
        to_remove = []
        
        for client_id, last_active in list(client_last_activity.items()):
            if current_time - last_active > CLIENT_TIMEOUT:
                to_remove.append(client_id)
        
        for client_id in to_remove:
            if client_id in client_queues_tiled:
                while not client_queues_tiled[client_id].empty():
                    try:
                        client_queues_tiled[client_id].get_nowait()
                    except queue.Empty:
                        break
                del client_queues_tiled[client_id]
            
            if client_id in client_queues_individual:
                for window_idx in list(client_queues_individual[client_id].keys()):
                    while not client_queues_individual[client_id][window_idx].empty():
                        try:
                            client_queues_individual[client_id][window_idx].get_nowait()
                        except queue.Empty:
                            break
                del client_queues_individual[client_id]
            
            if client_id in client_last_activity:
                del client_last_activity[client_id]
            
            session_ids_to_remove = []
            for session_id, cid in list(client_session_map.items()):
                if cid == client_id:
                    session_ids_to_remove.append(session_id)
            
            for session_id in session_ids_to_remove:
                del client_session_map[session_id]
            
            print(f"Inactive user removed {client_id}")
        
        return len(to_remove)

def broadcast_frames():
    print("Launching application window streaming...")
    
    last_cleanup = time.time()
    
    while True:
        try:
            windows = capture_app_windows()
            tiled_image = create_tiled_view(windows)
            
            with clients_lock:
                current_time = time.time()
                active_client_ids = list(client_last_activity.keys())
                
                for client_id in active_client_ids:
                    if current_time - client_last_activity.get(client_id, 0) > CLIENT_TIMEOUT:
                        continue
                    
                    try:
                        if client_queues_tiled[client_id].full():
                            try:
                                client_queues_tiled[client_id].get_nowait()
                            except queue.Empty:
                                pass
                        
                        _, buffer_tiled = cv2.imencode('.jpg', tiled_image, [cv2.IMWRITE_JPEG_QUALITY, 95])
                        client_queues_tiled[client_id].put_nowait(buffer_tiled.tobytes())


                        for i, window in enumerate(windows):
                            if i >= max_windows:
                                break
                            
                            if client_queues_individual[client_id][i].full():
                                try:
                                    client_queues_individual[client_id][i].get_nowait()
                                except queue.Empty:
                                    pass
                            
                            _, buffer_window = cv2.imencode('.jpg', window['image'], [cv2.IMWRITE_JPEG_QUALITY, 95])
                            client_queues_individual[client_id][i].put_nowait(buffer_window.tobytes())
                            
                    except (queue.Full, Exception) as e:
                        pass
            
            if time.time() - last_cleanup > CLEANUP_INTERVAL:
                removed = cleanup_inactive_clients()
                if removed > 0:
                    print(f"Cleaning: {removed} inactive users removed")
                last_cleanup = time.time()
        
        except Exception as e:
            print(f"Error in broadcast_frames: {e}")
        
        # time.sleep(0.016)

def get_or_create_client_id():
    with clients_lock:
        if 'session_id' not in session:
            session['session_id'] = secrets.token_hex(16)
            session.permanent = True
        
        session_id = session['session_id']
        
        if session_id in client_session_map:
            client_id = client_session_map[session_id]
        else:
            import random
            client_id = random.randint(1000, 9999)
            client_session_map[session_id] = client_id
            session['client_id'] = client_id
            print(f"🆕 A new user {client_id} has been created for the session {session_id[:8]}...")
        
        client_last_activity[client_id] = time.time()
        return client_id

def generate_tiled_for_client(client_id):
    while True:
        try:
            frame_bytes = client_queues_tiled[client_id].get(timeout=1.0)
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + 
                   frame_bytes + b'\r\n')
        except queue.Empty:
            continue
        except Exception as e:
            print(f"Error in generate_tiled_for_client for user {client_id}: {e}")
            break

def generate_window_for_client(client_id, window_idx):
    while True:
        try:
            if window_idx in client_queues_individual[client_id]:
                frame_bytes = client_queues_individual[client_id][window_idx].get(timeout=1.0)
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + 
                       frame_bytes + b'\r\n')
            # else:
                # time.sleep(0.1)
        except queue.Empty:
            continue
        except Exception as e:
            print(f"Error in generate_window_for_client for user {client_id}, window {window_idx}: {e}")
            break


# ============= FLASK ROUTES =============

@visual_bp.route('/')
def index():
    client_id = get_or_create_client_id()
    session_id = session.get('session_id', 'No session')
    
    try:
        windows_data = capture_app_windows()
        windows_count = len(windows_data)
    except Exception as e:
        windows_data = []
        windows_count = 0
    
    
    with clients_lock:
        current_time = time.time()
        active_clients = 0
        for cid, last_active in client_last_activity.items():
            if current_time - last_active <= CLIENT_TIMEOUT:
                active_clients += 1
    
    
    return render_template(
        'visual.html',  
        client_id=client_id,
        session_id=session_id,
        windows_count=windows_count,
        active_clients=active_clients
    )


@visual_bp.route('/screenshot_tiled')
def screenshot_tiled():
    client_id = get_or_create_client_id()
    try:
        frame_bytes = client_queues_tiled[client_id].get()
        response = make_response(frame_bytes)
        response.headers.set('Content-Type', 'image/jpeg')
        response.headers.set('Content-Disposition', 
                           f'attachment; filename=screenshot_tiled_{int(time.time())}.jpg')
        return response
    except:
        return "No data available", 404

@visual_bp.route('/screenshot_window/<int:window_idx>')
def screenshot_window(window_idx):
    client_id = get_or_create_client_id()
    try:
        if window_idx in client_queues_individual[client_id]:
            frame_bytes = client_queues_individual[client_id][window_idx].get()
            response = make_response(frame_bytes)
            response.headers.set('Content-Type', 'image/jpeg')
            response.headers.set('Content-Disposition', 
                               f'attachment; filename=screenshot_window_{window_idx}_{int(time.time())}.jpg')
            return response
        else:
            return "Window not found", 404
    except:
        return "No data available", 404

@visual_bp.route('/video_feed_tiled')
def video_feed_tiled():
    client_id = get_or_create_client_id()
    return Response(
        generate_tiled_for_client(client_id),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )

@visual_bp.route('/video_feed_window/<int:window_idx>')
def video_feed_window(window_idx):
    client_id = get_or_create_client_id()
    return Response(
        generate_window_for_client(client_id, window_idx),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )

@visual_bp.route('/client_stats')
def client_stats():
    client_id = get_or_create_client_id() 
    with clients_lock:
        current_time = time.time()
        active_clients = 0
        
        for cid, last_active in client_last_activity.items():
            if current_time - last_active <= CLIENT_TIMEOUT:
                active_clients += 1
        
        return {
            'active_clients': active_clients,
            'total_sessions': len(client_session_map),
            'server_time': datetime.now().isoformat()
        }

@visual_bp.route('/windows_count')
def windows_count():
    windows = capture_app_windows()
    client_id = get_or_create_client_id() 
    return {'count': len(windows)}

@visual_bp.route('/force_redraw_all')
def force_redraw_all():
    try:
        window_infos = get_main_window_info()
        all_windows = []
        
        for win_info in window_infos:
            all_windows.append(win_info['id'])
            try:
                tree_result = subprocess.run(
                    f"DISPLAY=:99 xwininfo -id {win_info['id']} -children",
                    shell=True, capture_output=True, text=True, timeout=2
                )
                if tree_result.returncode == 0:
                    for line in tree_result.stdout.split('\n'):
                        child_match = re.search(r'(0x[0-9a-f]+)', line)
                        if child_match and child_match.group(1) != win_info['id']:
                            all_windows.append(child_match.group(1))
            except:
                pass

        for window_id in all_windows:
            force_window_redraw(window_id)
        
        return f"Forced redrawing performed for {len(all_windows)} windows"
    except Exception as e:
        return f"Error: {str(e)}"

@visual_bp.route('/debug')
def debug():
    client_id = get_or_create_client_id() 
    with clients_lock:
        current_time = time.time()
        clients_info = []
        
        for client_id, last_active in sorted(client_last_activity.items()):
            age = current_time - last_active
            active = age <= CLIENT_TIMEOUT
            
            sessions = []
            for session_id, cid in client_session_map.items():
                if cid == client_id:
                    sessions.append(session_id[:8] + '...')
            
            clients_info.append({
                'id': client_id,
                'last_active': datetime.fromtimestamp(last_active).strftime('%H:%M:%S'),
                'age_seconds': round(age, 1),
                'active': active,
                'sessions': sessions
            })
        
        return {
            'clients': clients_info,
            'total_clients': len(client_last_activity),
            'active_clients': sum(1 for c in clients_info if c['active']),
            'cleanup_timeout': CLIENT_TIMEOUT
        }

@visual_bp.route('/place_windows')
def place_windows():
    placed = place_all_windows_on_halves()
    return f"Placed {placed} windows on half of the screen. <a href='/'>Back</a>"


@visual_bp.route('/place_window/<window_id>/<half>')
def place_window(window_id, half):
    if half not in ['left', 'right']:
        return "Invalid parameter half. Use ‘left’ or ‘right’."
    
    success = place_window_on_half(window_id, half)
    if success:
        return f"Window {window_id} is placed on {half} half. <a href='/'>Back</a>"
    else:
        return f"Failed to place window {window_id}"

def start_threads():
    launch_applications()
    broadcast_thread = threading.Thread(target=broadcast_frames, daemon=True)
    broadcast_thread.start()
    print("All threads are running.")

def signal_handler(signum, frame):
    print("\nFinishing up...")
    cleanup_processes()
    os._exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# if __name__ == '__main__':
def init_visual(app):
    app.register_blueprint(visual_bp)
    print("=" * 60)
    print(" Multi-user window broadcasting")
    print("=" * 60)
    
    extra_utils = ['xdotool', 'import', 'scrot', 'xwd', 'xrefresh', 'openbox']
    for util in extra_utils:
        try:
            if util == 'import':
                subprocess.run(["convert", "--version"], capture_output=True)
            else:
                subprocess.run([util, "--version"], capture_output=True)
            print(f"{util}: available")
        except:
            print(f" {util}: not found, but let's try to continue...")
    
    try:
        subprocess.run(["openbox", "--version"], capture_output=True)
    except:
        print(" Openbox is not installed. It is recommended to install it for better window management..")
        print("   sudo apt install openbox")
    
    start_threads()
    
    print(f"\n Settings:")
    print(f"   User timeout: {CLIENT_TIMEOUT} seconds")
    print(f"   Cleanup interval: {CLEANUP_INTERVAL} seconds")
    
    print("\n Server is running:")
    print("   http://localhost:5000")
    print("   http://<your-ip>:5000")
    print("\n Additionally:")
    print("   /debug - debug information")
    print("   /client_stats - user stats")
    print("=" * 60)
