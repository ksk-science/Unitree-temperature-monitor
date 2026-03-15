#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import subprocess
import threading
import signal
import secrets
import tempfile
import shlex
import re
import traceback
from collections import defaultdict
from datetime import datetime, timedelta
from dotenv import load_dotenv

ENVIRONMENT = os.getenv("ENVIRONMENT", "dev")
env_file = f".env.{ENVIRONMENT}"
if os.path.exists(env_file):
    load_dotenv(env_file)
    print(f"Loaded environment: {ENVIRONMENT}")
else:
    print(f"Warning: {env_file} not found, using system environment variables only")

import numpy as np
import cv2
from flask import Blueprint, render_template, Response, request, make_response, session
from mss import mss

# ========== Конфигурация ==========
CAPTURE_INTERVAL = 0.033               # ~30 FPS
WINDOW_INFO_CACHE_TIME = 2.0            # кэш списка окон (сек)
WINDOW_GEOMETRY_CACHE_TIME = 1.0        # кэш геометрии окон (сек)
CLIENT_TIMEOUT = 11                     # неактивные клиенты удаляются через N сек
CLEANUP_INTERVAL = 5                    # интервал проверки неактивных клиентов
MAX_WINDOWS = 10                         # максимум отслеживаемых окон
JPEG_QUALITY = 85                        # качество JPEG

# ========== Глобальные переменные ==========
app_processes = []                       # процессы приложений
xvfb_process = None

# --- Новый механизм: последние кадры для каждого окна ---
last_frames = [None] * MAX_WINDOWS        # список байтов JPEG для каждого окна
last_frames_lock = threading.Lock()       # блокировка для доступа к last_frames

# --- Данные для отслеживания активности клиентов (только статистика) ---
client_last_activity = {}                 # client_id -> timestamp
client_session_map = {}                   # session_id -> client_id
clients_lock = threading.Lock()           # блокировка для работы с клиентами

# Кэш информации об окнах (не изображений!)
_window_info_cache = []
_window_info_cache_time = 0

# Кэш геометрии окон
_window_geometry_cache = {}
_window_geometry_cache_time = 0

# Поток трансляции
broadcast_thread = None
stop_broadcast = None

original_display = os.environ.get('DISPLAY', ':0')
os.environ['DISPLAY'] = ':99'

# Список приложений
apps_str = os.getenv("visual_path_array", "[]")
apps = []

try:
    apps = json.loads(apps_str)
except json.JSONDecodeError:
    print("Ошибка: переменная visual_path_array не содержит валидный JSON")

windows_head_names = [
    "Head Camera",
    "display_point_cloud_ROS1.rviz*-RViz"
]

# ========== Вспомогательные функции ==========

def check_dependencies():
    """Проверка наличия необходимых утилит (некритично, но полезно)"""
    required_utils = ['wmctrl', 'xdotool', 'xwininfo', 'xprop']
    missing_utils = []
    for util in required_utils:
        try:
            subprocess.run([util, "--version"], capture_output=True, timeout=1)
            print(f"  {util}: available")
        except:
            missing_utils.append(util)
            print(f"  {util}: not found")
    if missing_utils:
        print(f"\nУстановите недостающие утилиты:")
        print(f"sudo apt install wmctrl xdotool x11-utils")
        print("Продолжение через 3 секунды...")
        time.sleep(3)

def cleanup_inactive_clients():
    """Удаляет неактивных клиентов из структур данных (только статистика)."""
    with clients_lock:
        current_time = time.time()
        to_remove = []
        for client_id, last_active in list(client_last_activity.items()):
            if current_time - last_active > CLIENT_TIMEOUT:
                to_remove.append(client_id)
        
        for client_id in to_remove:
            if client_id in client_last_activity:
                del client_last_activity[client_id]
            
            # Удаляем из маппинга сессий
            session_ids_to_remove = []
            for session_id, cid in list(client_session_map.items()):
                if cid == client_id:
                    session_ids_to_remove.append(session_id)
            for session_id in session_ids_to_remove:
                del client_session_map[session_id]
            
            print(f"Неактивный пользователь {client_id} удалён")
        return len(to_remove)

def get_or_create_client_id():
    """Создаёт или возвращает существующий ID клиента на основе сессии Flask."""
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
            print(f"🆕 Новый пользователь {client_id} создан для сессии {session_id[:8]}...")
        
        client_last_activity[client_id] = time.time()
        return client_id

def get_window_geometry_cached(window_id, display=':99'):
    """
    Возвращает геометрию окна {x, y, width, height} с кэшированием.
    Если данные устарели (WINDOW_GEOMETRY_CACHE_TIME), выполняет xwininfo.
    """
    global _window_geometry_cache, _window_geometry_cache_time
    now = time.time()
    
    # Проверяем кэш
    if window_id in _window_geometry_cache:
        geom, timestamp = _window_geometry_cache[window_id]
        if now - timestamp < WINDOW_GEOMETRY_CACHE_TIME:
            return geom
    
    # Иначе запрашиваем свежие данные
    try:
        result = subprocess.run(
            f"DISPLAY={display} xwininfo -id {window_id}",
            shell=True, capture_output=True, text=True, timeout=2  # <- добавлен timeout
        )
        if result.returncode != 0:
            return None
        
        lines = result.stdout.split('\n')
        geom = {}
        for line in lines:
            if 'Absolute upper-left X:' in line:
                geom['x'] = int(line.split(':')[1].strip())
            elif 'Absolute upper-left Y:' in line:
                geom['y'] = int(line.split(':')[1].strip())
            elif 'Width:' in line:
                geom['width'] = int(line.split(':')[1].strip())
            elif 'Height:' in line:
                geom['height'] = int(line.split(':')[1].strip())
        
        if 'x' in geom and 'y' in geom and 'width' in geom and 'height' in geom:
            _window_geometry_cache[window_id] = (geom, now)
            return geom
        else:
            return None
    except subprocess.TimeoutExpired:
        print(f"Таймаут xwininfo для окна {window_id}")
        return None
    except Exception as e:
        print(f"Ошибка получения геометрии окна {window_id}: {e}")
        return None

def get_main_window_info_cached(display=':99'):
    """
    Возвращает список основных окон (кэшируется на WINDOW_INFO_CACHE_TIME секунд).
    """
    global _window_info_cache, _window_info_cache_time
    now = time.time()
    if now - _window_info_cache_time < WINDOW_INFO_CACHE_TIME and _window_info_cache:
        return _window_info_cache

    windows = []
    try:
        result = subprocess.run(
            f"DISPLAY={display} wmctrl -l -p -x",
            shell=True, capture_output=True, text=True, timeout=3
        )
        if not result.stdout:
            return windows

        lines = result.stdout.strip().split('\n')
        for line in lines:
            parts = line.split()
            if len(parts) < 5:
                continue
            window_id = parts[0]
            windows.append({
                'id': window_id,
                'app': parts[3],
                'pid': parts[2] if len(parts) > 2 else '0',
                'title': ' '.join(parts[4:]) if len(parts) > 4 else '',
                'is_main': True
            })
        _window_info_cache = windows
        _window_info_cache_time = now
    except Exception as e:
        print(f"Ошибка получения информации об окнах: {e}")
        _window_info_cache = []
    return windows

def place_window_on_half(window_id, half='left', display=':99'):
    """Размещает окно на левой или правой половине экрана (wmctrl)."""
    try:
        # Получаем размер экрана
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
        
        half_width = screen_width // 2
        window_height = int(screen_height * 0.9)
        window_y = (screen_height - window_height) // 2
        window_x = 0 if half == 'left' else half_width
        
        cmd = f"DISPLAY={display} wmctrl -i -r {window_id} -e 0,{window_x},{window_y},{half_width},{window_height}"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=3)
        if result.returncode == 0:
            # Добавляем вертикальное расширение
            subprocess.run(
                f"DISPLAY={display} wmctrl -i -r {window_id} -b add,maximized_vert",
                shell=True, capture_output=True, timeout=2
            )
            return True
        return False
    except Exception as e:
        print(f"Ошибка в place_window_on_half: {e}")
        return False

def place_all_windows_on_halves():
    """Размещает все найденные окна по половинкам экрана (по очереди)."""
    print("Размещение окон по половинам экрана...")
    windows = get_main_window_info_cached()
    if not windows:
        print("  Окна не найдены, повтор через 3 секунды...")
        time.sleep(3)
        windows = get_main_window_info_cached()
    
    print(f"  Найдено окон: {len(windows)}")
    placed_windows = 0
    for window in windows:
        window_id = window.get('id')
        if not window_id:
            continue
        half = 'left' if placed_windows % 2 == 0 else 'right'
        print(f"  🪟 Окно {window_id} ({window.get('app', 'unknown')}) -> {half} половина")
        if place_window_on_half(window_id, half):
            placed_windows += 1
    print(f"Размещено окон: {placed_windows}/{len(windows)}")
    return placed_windows

def capture_app_windows():
    """
    Захватывает окна приложений, используя MSS.
    Сначала делается снимок всего экрана, затем для каждого окна вырезается его область.
    Возвращает список словарей с полями: id, name, image (numpy BGR), width, height, app.
    """
    try:
        # Первоначальное размещение окон (выполняется один раз)
        if not hasattr(capture_app_windows, '_windows_placed'):
            place_all_windows_on_halves()
            capture_app_windows._windows_placed = True

        window_infos = get_main_window_info_cached()
        if not window_infos:
            return []

        # Захватываем весь экран через MSS
        with mss() as sct:
            # monitors[0] — объединённая область всех мониторов (для одного экрана подходит)
            full_img = sct.grab(sct.monitors[0])
            img_array = np.frombuffer(full_img.bgra, dtype=np.uint8).reshape(full_img.height, full_img.width, 4)
            full_np = img_array[:, :, :3].copy()  # отбрасываем альфа-канал
            screen_h, screen_w = full_np.shape[:2]

            windows_data = []
            for win_info in window_infos:
                window_id = win_info.get('id')
                if not window_id:
                    continue

                geom = get_window_geometry_cached(window_id)
                if geom is None:
                    continue

                x, y, w, h = geom['x'], geom['y'], geom['width'], geom['height']
                if w <= 0 or h <= 0:
                    continue

                # Корректировка области, если окно частично за пределами экрана
                x1 = max(0, x)
                y1 = max(0, y)
                x2 = min(screen_w, x + w)
                y2 = min(screen_h, y + h)
                if x2 <= x1 or y2 <= y1:
                    continue

                img = full_np[y1:y2, x1:x2].copy()

                windows_data.append({
                    'id': window_id,
                    'name': f"{win_info.get('app', 'Unknown')}: Main window",
                    'image': img,
                    'width': img.shape[1],
                    'height': img.shape[0],
                    'app': win_info.get('app', 'Unknown')
                })
                if len(windows_data) >= MAX_WINDOWS:
                    break

        # Если окон нет, создаём плейсхолдеры
        if not windows_data:
            for i, appl in enumerate(windows_head_names[:2]):
                app_name = appl.split()[0] if ' ' in appl else appl
                placeholder = np.zeros((400, 600, 3), dtype=np.uint8)
                cv2.putText(placeholder, f"App: {app_name}", (30, 100),
                          cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
                cv2.putText(placeholder, "Wait window...", (30, 140),
                          cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 200, 255), 1)
                windows_data.append({
                    'id': f'placeholder_{i}',
                    'name': f'{app_name} (placeholder)',
                    'image': placeholder,
                    'width': 600,
                    'height': 400,
                    'app': app_name
                })
        return windows_data

    except Exception as e:
        print(f"Ошибка в capture_app_windows: {e}")
        traceback.print_exc()
        return []

# ========== Поток трансляции (теперь только обновляет last_frames) ==========

def broadcast_frames(stop_event):
    """
    Основной поток: захватывает окна, кодирует в JPEG и сохраняет в глобальном массиве last_frames.
    """
    print("Запуск потока трансляции окон (broadcast)...")
    last_cleanup = time.time()
    last_capture_time = 0
    last_successful_capture = time.time()

    while not stop_event.is_set():
        try:
            now = time.time()
            if now - last_capture_time >= CAPTURE_INTERVAL:
                windows = capture_app_windows()
                # Кодируем в JPEG и сохраняем в last_frames
                jpeg_frames = []
                if windows:  # если захватили хотя бы одно окно
                    last_successful_capture = now
                    
                if now - last_successful_capture > CAPTURE_INTERVAL * 10:
                    print("WARNING: broadcast thread seems stuck, restarting capture...")
                    # можно попытаться перезапустить подсистему (например, пересоздать Xvfb), но для простоты просто сбросим флаг размещения окон
                    capture_app_windows._windows_placed = False  # заставит переразместить окна при следующем захвате
                    last_successful_capture = now

                for win in windows[:MAX_WINDOWS]:
                    ret, buf = cv2.imencode('.jpg', win['image'], [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                    if ret:
                        jpeg_frames.append(buf.tobytes())
                        
                    else:
                        jpeg_frames.append(None)
                
                # Обновляем глобальный массив с блокировкой
                with last_frames_lock:
                    for i, jpeg in enumerate(jpeg_frames):
                        if jpeg is not None:
                            last_frames[i] = jpeg
                        # Если окно не захватилось, оставляем предыдущий кадр
                
                last_capture_time = now

            # Очистка неактивных клиентов (только статистика)
            if time.time() - last_cleanup > CLEANUP_INTERVAL:
                removed = cleanup_inactive_clients()
                if removed > 0:
                    print(f"Очистка: удалено {removed} неактивных пользователей")
                last_cleanup = time.time()

            # Небольшая задержка для снижения нагрузки
            time.sleep(0.001)

        except Exception as e:
            print(f"Ошибка в broadcast_frames: {e}")
            traceback.print_exc()
            time.sleep(0.1)

# ========== Генератор MJPEG для клиента (без очередей) ==========

def generate_window_for_client(client_id, window_idx):
    """
    Генератор MJPEG для конкретного окна.
    Отдаёт последний сохранённый кадр из глобального массива last_frames.
    """
    # Начальная активность уже установлена в get_or_create_client_id() при вызове маршрута
    last_activity_update = time.time()
    
    # Проверяем корректность индекса
    if window_idx < 0 or window_idx >= MAX_WINDOWS:
        yield (b'--frame\r\n'
               b'Content-Type: text/plain\r\n\r\n'
               b'Invalid window index\r\n')
        return

    while True:
        now = time.time()
        # Обновляем активность каждые 5 секунд
        if now - last_activity_update > 5:
            with clients_lock:
                client_last_activity[client_id] = now
            last_activity_update = now

        # Получаем последний кадр для этого окна
        with last_frames_lock:
            frame_bytes = last_frames[window_idx]
        
        if frame_bytes is None:
            # Если кадра ещё нет, отправляем чёрный кадр с текстом
            dummy = np.zeros((480, 640, 3), dtype=np.uint8)
            cv2.putText(dummy, "No frame", (50, 240),
                        cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 3)
            ret, buf = cv2.imencode('.jpg', dummy, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if ret:
                frame_bytes = buf.tobytes()
            else:
                # Если и это не удалось, отправляем минимальный заголовок
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n'
                       b'\r\n')
                time.sleep(CAPTURE_INTERVAL)
                continue

        # Отправляем кадр
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + 
               frame_bytes + b'\r\n')
        
        # Ждём примерно интервал захвата, чтобы не перегружать сеть
        time.sleep(CAPTURE_INTERVAL)

# ========== Blueprint и маршруты Flask ==========

visual_bp = Blueprint('visual', __name__, url_prefix='/visual')

@visual_bp.route('/')
def index():
    client_id = get_or_create_client_id()
    session_id = session.get('session_id', 'No session')
    
    # Получаем количество окон из кэша (без захвата изображений)
    windows_count = len(get_main_window_info_cached())
    
    with clients_lock:
        current_time = time.time()
        active_clients = sum(1 for last_active in client_last_activity.values()
                             if current_time - last_active <= CLIENT_TIMEOUT)
    
    return render_template('visual.html',
                           client_id=client_id,
                           session_id=session_id,
                           windows_count=windows_count,
                           active_clients=active_clients)

@visual_bp.route('/screenshot_window/<int:window_idx>')
def screenshot_window(window_idx):
    """
    Возвращает последний захваченный кадр указанного окна как JPEG.
    """
    client_id = get_or_create_client_id()
    if window_idx < 0 or window_idx >= MAX_WINDOWS:
        return "Invalid window index", 404
    
    with last_frames_lock:
        frame_bytes = last_frames[window_idx]
    
    if frame_bytes is None:
        return "No frame available", 404
    
    response = make_response(frame_bytes)
    response.headers.set('Content-Type', 'image/jpeg')
    response.headers.set('Content-Disposition',
                         f'attachment; filename=screenshot_window_{window_idx}_{int(time.time())}.jpg')
    return response

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
        active_clients = sum(1 for last_active in client_last_activity.values()
                             if current_time - last_active <= CLIENT_TIMEOUT)
        return {
            'active_clients': active_clients,
            'total_sessions': len(client_session_map),
            'server_time': datetime.now().isoformat()
        }

@visual_bp.route('/windows_count')
def windows_count():
    # Используем кэшированный список окон (без захвата)
    count = len(get_main_window_info_cached())
    client_id = get_or_create_client_id()
    return {'count': count}

@visual_bp.route('/force_redraw_all')
def force_redraw_all():
    """Принудительная перерисовка окон (может не требоваться с MSS, но оставлено для совместимости)."""
    try:
        window_infos = get_main_window_info_cached()
        all_windows = []
        for win_info in window_infos:
            all_windows.append(win_info['id'])
            # Пытаемся найти дочерние окна
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
            # Используем xrefresh для перерисовки
            subprocess.run(f"DISPLAY=:99 xrefresh -id {window_id}", shell=True, capture_output=True, timeout=2)
        return f"Принудительная перерисовка выполнена для {len(all_windows)} окон"
    except Exception as e:
        return f"Ошибка: {str(e)}"

@visual_bp.route('/debug')
def debug():
    client_id = get_or_create_client_id()
    with clients_lock:
        current_time = time.time()
        clients_info = []
        for client_id, last_active in sorted(client_last_activity.items()):
            age = current_time - last_active
            active = age <= CLIENT_TIMEOUT
            sessions = [sid[:8] + '...' for sid, cid in client_session_map.items() if cid == client_id]
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
            'cleanup_timeout': CLIENT_TIMEOUT,
            'capture_interval_ms': CAPTURE_INTERVAL * 1000,
            'jpeg_quality': JPEG_QUALITY,
            'max_windows': MAX_WINDOWS
        }

@visual_bp.route('/place_windows')
def place_windows():
    """Ручное размещение окон по половинкам (полезно после изменения списка окон)."""
    placed = place_all_windows_on_halves()
    return f"Размещено {placed} окон по половинам. <a href='/'>Назад</a>"

@visual_bp.route('/place_window/<window_id>/<half>')
def place_window_route(window_id, half):
    if half not in ['left', 'right']:
        return "Неверный параметр half. Используйте 'left' или 'right'."
    success = place_window_on_half(window_id, half)
    if success:
        return f"Окно {window_id} размещено на {half} половине. <a href='/'>Назад</a>"
    else:
        return f"Не удалось разместить окно {window_id}"

# ========== Запуск приложений и управление процессами ==========

def launch_applications():
    """Запускает оконный менеджер и пользовательские приложения на виртуальном дисплее."""
    print("Запуск приложений на виртуальном дисплее...")
    
    # Запуск оконного менеджера (openbox)
    try:
        print("  Запуск оконного менеджера openbox...")
        env = os.environ.copy()
        env['DISPLAY'] = ':99'
        wm_process = subprocess.Popen(
            ["openbox", "--sm-disable"],
            shell=False,
            env=env,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        app_processes.append(wm_process)
        time.sleep(2)
    except Exception as e:
        print(f"  Не удалось запустить оконный менеджер: {e}")
    
    # Запуск пользовательских приложений
    for i, app_cmd in enumerate(apps):
        try:
            print(f"  Запуск: {app_cmd}")
            env = os.environ.copy()
            env['DISPLAY'] = ':99'
            process = subprocess.Popen(
                app_cmd,
                shell=False,
                env=env,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            app_processes.append(process)
            time.sleep(5)
            if process.poll() is None:
                print(f"  Приложение запущено (PID: {process.pid})")
            else:
                print(f"  Приложение завершилось с кодом {process.poll()}")
        except Exception as e:
            print(f"  Ошибка запуска {app_cmd}: {e}")
    
    # Размещение окон после запуска
    print("\nРазмещение окон...")
    time.sleep(2)
    placed = place_all_windows_on_halves()
    if placed == 0:
        print("  Не удалось разместить окна, повтор через 3 секунды...")
        time.sleep(3)
        place_all_windows_on_halves()
    
    # Проверка
    try:
        result = subprocess.run("DISPLAY=:99 wmctrl -l", shell=True, capture_output=True, text=True, timeout=5)
        print("  Окна на виртуальном дисплее:")
        for line in result.stdout.strip().split('\n'):
            if line:
                print(f"    {line}")
    except Exception as e:
        print(f"  Ошибка проверки окон: {e}")


def cleanup_processes():
    print("Завершение процессов...")
    global broadcast_thread, stop_broadcast

    if stop_broadcast:
        stop_broadcast.set()
    if broadcast_thread and broadcast_thread.is_alive():
        try:
            broadcast_thread.join(timeout=3)
        except RuntimeError as e:
            # Игнорируем ошибки eventlet при завершении
            print(f"⚠️ Поток трансляции завершён с предупреждением: {e}")
        except Exception as e:
            print(f"⚠️ Неожиданная ошибка при ожидании потока: {e}")
        finally:
            if broadcast_thread.is_alive():
                print("⚠️ Поток трансляции не завершился, продолжаем очистку...")

    # Завершение дочерних процессов
    for process in app_processes:
        try:
            if process.poll() is None:
                print(f"  Завершение приложения PID: {process.pid}")
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
        except Exception as e:
            print(f"  Ошибка завершения приложения: {e}")
    
    try:
        if xvfb_process and xvfb_process.poll() is None:
            print("  Завершение Xvfb...")
            xvfb_process.terminate()
            try:
                xvfb_process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                xvfb_process.kill()
    except Exception as e:
        print(f"  Ошибка завершения Xvfb: {e}")
    
    os.environ['DISPLAY'] = original_display
    print("Все процессы завершены")

def start_threads():
    global broadcast_thread, stop_broadcast
    stop_broadcast = threading.Event()
    broadcast_thread = threading.Thread(target=broadcast_frames, args=(stop_broadcast,), daemon=True)
    broadcast_thread.start()
    print("Поток трансляции запущен (демонический).")

# ========== Инициализация Xvfb и запуск ==========

print("Проверка зависимостей...")
check_dependencies()

print("Запуск виртуального дисплея Xvfb...")
xvfb_process = subprocess.Popen(
    ["Xvfb", ":99", "-screen", "0", "1920x1080x24"],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE
)
time.sleep(2)
os.environ['DISPLAY'] = ':99'
subprocess.run(["xhost", "+"], capture_output=True, timeout=1)
print(f"Виртуальный дисплей запущен: {os.environ['DISPLAY']}")

# ========== Функция регистрации blueprint'а ==========

def init_visual(app):
    app.register_blueprint(visual_bp)
    print("=" * 60)
    print("  Многопользовательская трансляция окон")
    print("=" * 60)
    print("\nНастройки:")
    print(f"  Таймаут клиента: {CLIENT_TIMEOUT} сек")
    print(f"  Интервал очистки: {CLEANUP_INTERVAL} сек")
    print(f"  Интервал захвата: {CAPTURE_INTERVAL*1000:.0f} мс (~{1/CAPTURE_INTERVAL:.0f} FPS)")
    print(f"  Качество JPEG: {JPEG_QUALITY}")
    print(f"  Максимум окон: {MAX_WINDOWS}")
    print("\nДополнительные маршруты:")
    print("  /debug - отладочная информация")
    print("  /client_stats - статистика пользователей")
    print("  /place_windows - принудительное размещение окон")
    print("=" * 60)
    
    launch_applications()
    start_threads()