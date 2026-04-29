import mmap
import ctypes
import threading
import time
import json
import os
from collections import defaultdict
from flask import Flask, render_template_string, request
from flask_socketio import SocketIO, emit

from ipc_shared import TelemetryPage, CommandPage, MAX_CARS
from ipc_shared import TELEMETRY_TAG, COMMAND_TAG
from auto_director import AutoDirector

app = Flask(__name__)
socketio = SocketIO(app)

# mmap handles (opened lazily by monitor thread)
telemetry_mmap = None
telemetry_page = None
command_mmap = None
command_page = None
command_lock = threading.Lock()
command_seq_counter = 0

ac_connected = False

director = AutoDirector()
director_lock = threading.Lock()

# Latest telemetry snapshot captured by the monitor thread. The request
# handlers read this instead of calling read_telemetry() themselves —
# concurrent seek/read on the shared mmap object races and produces torn
# reads, which cause the handler to fall back to driver 0.
latest_focused_car = 0
latest_current_camera = 0
latest_current_car_camera = 0

# --- Class config ---
CLASS_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'class_config.json')
class_config = {}
class_config_mtime = 0


def load_class_config():
    """Load class config from JSON, hot-reload if file changed."""
    global class_config, class_config_mtime
    try:
        mtime = os.path.getmtime(CLASS_CONFIG_PATH)
        if mtime != class_config_mtime:
            with open(CLASS_CONFIG_PATH, 'r') as f:
                class_config = json.load(f)
            class_config_mtime = mtime
    except (OSError, ValueError):
        class_config = {}


def get_car_class(car_number):
    """Return (class_name, class_color) for a car number."""
    for cls_name, cls_data in class_config.get('classes', {}).items():
        if car_number in cls_data.get('cars', []):
            return cls_name, cls_data.get('color', '#999')
    return 'Unclassed', '#999'



def open_telemetry_mmap():
    """Try to open the telemetry shared memory. Returns (mmap, page) or (None, None)."""
    try:
        m = mmap.mmap(0, ctypes.sizeof(TelemetryPage), TELEMETRY_TAG)
        page = TelemetryPage.from_buffer_copy(m)
        return m, page
    except Exception:
        return None, None


def open_command_mmap():
    """Try to open the command shared memory. Returns (mmap, page) or (None, None)."""
    try:
        m = mmap.mmap(0, ctypes.sizeof(CommandPage), COMMAND_TAG)
        page = CommandPage.from_buffer(m)
        return m, page
    except Exception:
        return None, None


def read_telemetry():
    """Read telemetry with torn-read protection. Returns a TelemetryPage copy or None."""
    global telemetry_mmap
    if telemetry_mmap is None:
        return None
    try:
        telemetry_mmap.seek(0)
        buf = telemetry_mmap.read(ctypes.sizeof(TelemetryPage))
        page1 = TelemetryPage.from_buffer_copy(bytearray(buf))
        pid1 = page1.packet_id

        telemetry_mmap.seek(0)
        buf2 = telemetry_mmap.read(ctypes.sizeof(TelemetryPage))
        page2 = TelemetryPage.from_buffer_copy(bytearray(buf2))
        pid2 = page2.packet_id

        if pid1 != pid2:
            return None  # torn read, skip this cycle
        return page2
    except Exception:
        return None


def compute_gaps(telem):
    """Compute gap to leader and interval to car ahead for each car."""
    load_class_config()

    cars = []
    for i in range(telem.car_count):
        c = telem.cars[i]
        if not c.is_connected:
            continue

        # Parse "Number | Driver Name" format
        raw_name = c.driver_name
        parts = raw_name.split('|', 1)
        if len(parts) == 2:
            try:
                car_number = int(parts[0].strip())
            except ValueError:
                car_number = c.car_id + 1
            display_name = parts[1].strip()
        else:
            car_number = c.car_id + 1
            display_name = raw_name

        cls_name, cls_color = get_car_class(car_number)

        cars.append({
            'car_id': c.car_id,
            'car_number': car_number,
            'display_name': display_name,
            'car_class': cls_name,
            'class_color': cls_color,
            'position': c.position,
            'spline': c.normalized_spline_pos,
            'speed_kmh': c.speed_kmh,
            'lap_time': c.lap_time,
            'best_lap': c.best_lap,
            'last_lap': c.last_lap,
            'lap_count': c.lap_count,
            'is_in_pit': c.is_in_pit,
            'is_connected': c.is_connected,
            'is_colliding': c.is_colliding,
            'name': raw_name,
        })

    cars.sort(key=lambda x: x['position'])
    track_length = telem.track_length

    for idx, car in enumerate(cars):
        car['total_progress'] = car['lap_count'] + car['spline']

    # Overall gaps and intervals
    for idx, car in enumerate(cars):
        if idx == 0:
            car['gap'] = 'Leader'
            car['interval'] = '-'
            car['interval_seconds'] = float('inf')
            continue

        leader = cars[0]
        ahead = cars[idx - 1]

        car['gap'] = _format_gap(leader, car, track_length)
        car['interval'] = _format_gap(ahead, car, track_length)
        car['interval_seconds'] = _compute_gap_seconds(ahead, car, track_length)

    # Class positions and class intervals
    by_class = defaultdict(list)
    for car in cars:
        by_class[car['car_class']].append(car)

    for cls_name, cls_cars in by_class.items():
        # Already sorted by overall position; within a class, sort by total_progress descending
        cls_cars.sort(key=lambda c: c['total_progress'], reverse=True)
        for idx, car in enumerate(cls_cars):
            car['class_position'] = idx + 1
            if idx == 0:
                car['class_interval'] = '-'
                car['class_interval_seconds'] = float('inf')
            else:
                cls_ahead = cls_cars[idx - 1]
                car['class_interval'] = _format_gap(cls_ahead, car, track_length)
                car['class_interval_seconds'] = _compute_gap_seconds(cls_ahead, car, track_length)

    # Re-sort by overall position for output
    cars.sort(key=lambda x: x['position'])
    return cars


def _format_gap(ahead, behind, track_length):
    """Format the gap between two cars."""
    progress_diff = ahead['total_progress'] - behind['total_progress']

    if progress_diff >= 1.0:
        laps = int(progress_diff)
        if laps == 1:
            return "+1 Lap"
        return "+{} Laps".format(laps)

    # Within same lap - convert spline distance to time
    spline_diff = (ahead['spline'] - behind['spline']) % 1.0
    if spline_diff < 0.001:
        return "+0.0s"

    gap_meters = spline_diff * track_length
    speed_ms = behind['speed_kmh'] / 3.6

    if speed_ms < 1.0:
        # Car is nearly stationary - show distance
        return "+{}m".format(int(gap_meters))

    gap_seconds = gap_meters / speed_ms
    return "+{:.1f}s".format(gap_seconds)


def _compute_gap_seconds(ahead, behind, track_length):
    """Return numeric gap in seconds, or inf for lapped/stationary."""
    progress_diff = ahead['total_progress'] - behind['total_progress']
    if progress_diff >= 1.0:
        return float('inf')
    spline_diff = (ahead['spline'] - behind['spline']) % 1.0
    if spline_diff < 0.001:
        return 0.0
    gap_meters = spline_diff * track_length
    speed_ms = behind['speed_kmh'] / 3.6
    if speed_ms < 1.0:
        return float('inf')
    return gap_meters / speed_ms


def build_update_data(telem, cars_with_gaps=None):
    """Build the SocketIO update payload from telemetry."""
    if cars_with_gaps is None:
        cars_with_gaps = compute_gaps(telem)
    for c in cars_with_gaps:
        if c.get('is_colliding'):
            print('[remote_web] collision flag from car {} ({})'.format(c['car_id'], c['name']))
    drivers = []
    for c in cars_with_gaps:
        status = ""
        if c['is_in_pit']:
            status = "PIT"
        if not c['is_connected']:
            status = "OFFLINE"
        drivers.append({
            'name': c.get('display_name', c['name']),
            'num': c['car_id'] + 1,
            'car_number': c.get('car_number', c['car_id'] + 1),
            'car_class': c.get('car_class', 'Unclassed'),
            'class_color': c.get('class_color', '#999'),
            'class_position': c.get('class_position', c['position']),
            'class_interval': c.get('class_interval', '-'),
            'status': status,
            'position': c['position'],
            'gap': c['gap'],
            'interval': c['interval'],
            'colliding': c['is_colliding'],
        })

    return {
        'current_driver': telem.focused_car,
        'current_camera': telem.current_camera,
        'car_cameras_count': telem.car_cameras_count,
        'current_car_camera': telem.current_car_camera,
        'drivers': drivers,
        'ac_connected': True,
        'auto_director': director.enabled,
    }


def send_command(target_driver, target_camera, target_car_camera=-1):
    """Write a command to shared memory."""
    global command_mmap, command_page, command_seq_counter
    with command_lock:
        if command_mmap is None:
            command_mmap, command_page = open_command_mmap()
        if command_page is None:
            return False
        command_seq_counter += 1
        command_page.target_driver = target_driver
        command_page.target_camera = target_camera
        command_page.target_car_camera = target_car_camera
        command_page.command_seq = command_seq_counter
        return True


def monitor_telemetry():
    """Background thread: poll telemetry mmap and push updates via SocketIO."""
    global telemetry_mmap, telemetry_page, ac_connected
    global latest_focused_car, latest_current_camera, latest_current_car_camera

    last_packet_id = -1

    while True:
        # Try to connect if not connected
        if telemetry_mmap is None:
            telemetry_mmap, telemetry_page = open_telemetry_mmap()
            if telemetry_mmap is None:
                if ac_connected:
                    ac_connected = False
                    socketio.emit('update', {'ac_connected': False, 'drivers': []})
                time.sleep(2)
                continue
            ac_connected = True

        telem = read_telemetry()
        if telem is None:
            # Check if mmap is still valid
            try:
                telemetry_mmap.seek(0)
                telemetry_mmap.read(4)
            except Exception:
                telemetry_mmap = None
                telemetry_page = None
                ac_connected = False
                continue
            time.sleep(0.1)
            continue

        if telem.packet_id != last_packet_id:
            last_packet_id = telem.packet_id
            latest_focused_car = telem.focused_car
            latest_current_camera = telem.current_camera
            latest_current_car_camera = telem.current_car_camera
            cars_with_gaps = compute_gaps(telem)
            data = build_update_data(telem, cars_with_gaps)

            with director_lock:
                if director.enabled:
                    cmd = director.tick(cars_with_gaps, telem.track_length)
                    if cmd:
                        send_command(cmd['driver'], 1)  # always Track cam

            socketio.emit('update', data)

        time.sleep(0.1)


@socketio.on('toggle_director')
def handle_toggle_director():
    with director_lock:
        director.enabled = not director.enabled
    emit('director_status', {'enabled': director.enabled}, broadcast=True)


@socketio.on('connect')
def handle_connect():
    if not ac_connected:
        emit('update', {'ac_connected': False, 'drivers': []})
        return

    telem = read_telemetry()
    if telem:
        data = build_update_data(telem)
        emit('update', data)
    else:
        emit('update', {'ac_connected': False, 'drivers': []})



# Main route for the web interface
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'select_driver':
            selected_num = int(request.form.get('driver_num'))
            drv = selected_num - 1
            # Use the latest cached camera from the monitor thread so we
            # preserve it. (Doing our own read_telemetry here races the
            # monitor's seek/read on the same mmap object.)
            camera = latest_current_camera
            # Preserve current F6 sub-cam, otherwise Lua's cam=4/subCam=-1 path
            # cycles to the next angle on every driver pick.
            car_camera = latest_current_car_camera if camera == 4 else -1
            with director_lock:
                director.enabled = False
                send_command(drv, camera, car_camera)

        elif action == 'change_camera':
            camera = int(request.form.get('camera'))
            car_camera = int(request.form.get('car_camera', -1))
            drv = latest_focused_car
            with director_lock:
                director.enabled = False
                send_command(drv, camera, car_camera)

        return '', 204  # No content response for AJAX

    # HTML template for the interface
    html = '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Broadcaster Remote Control</title>
        <meta name=viewport content="width=device-width, initial-scale=1">
        <style>
            body {
                font-family: Arial, sans-serif;
                margin: 0;
                padding: 8px 12px;
                height: 100dvh;
                height: 100vh;
                box-sizing: border-box;
                display: flex;
                flex-direction: column;
                overflow: hidden;
            }
            @supports (height: 100dvh) {
                body { height: 100dvh; }
            }
            h1 { margin: 0 0 4px; font-size: 1.1em; }
            h2 { margin: 4px 0; font-size: 0.95em; }
            table { border-collapse: collapse; width: 100%; }
            th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
            th { background-color: #f2f2f2; }
            select, button { padding: 10px; margin: 3px 0; font-size: 1em; width: 100%; box-sizing: border-box; }
            #drivers-list {
                flex: 1;
                min-height: 0;
            }
            .drivers-grid {
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
                grid-auto-rows: 1fr;
                gap: 6px;
                height: 100%;
            }
            .driver-item {
                border: 1px solid #ddd;
                padding: 6px 8px;
                display: flex;
                flex-direction: column;
                justify-content: center;
                transition: background-color 0.3s;
                min-height: 0;
                overflow: hidden;
                cursor: pointer;
            }
            .driver-item:hover {
                background-color: #f0f0f0;
            }
            .driver-item.selected {
                background-color: #cce5ff;
                border-color: #007bff;
            }
            .driver-item.disabled {
                opacity: 0.4;
                cursor: default;
            }
            .driver-item.colliding {
                border-color: rgba(229, 57, 53, 0.8);
            }
            .driver-item.disabled:hover {
                background-color: transparent;
            }
            .driver-item.colliding.disabled:hover {
                background-color: rgba(229, 57, 53, 0.1);
            }
            .driver-info {
                display: flex;
                justify-content: space-between;
                align-items: center;
                font-size: 0.9em;
                min-width: 0;
            }
            .driver-info strong {
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
                min-width: 0;
            }
            .driver-gaps {
                font-size: 0.8em;
                color: #666;
                margin-top: 2px;
            }
            .driver-gaps span {
                margin-right: 10px;
            }
            .camera-buttons {
                display: flex;
                gap: 6px;
                margin-bottom: 8px;
            }
            .camera-buttons button {
                flex: 1;
                padding: 8px 6px;
                font-size: 0.9em;
            }
            .camera-buttons button.active {
                background-color: #007bff;
                color: white;
                border-color: #0056b3;
            }
            .subcam-bar {
                display: flex;
                gap: 6px;
                margin-bottom: 8px;
            }
            .subcam-bar button {
                flex: 1;
                padding: 6px 4px;
                font-size: 0.85em;
            }
            .subcam-bar button.active {
                background-color: #28a745;
                color: white;
                border-color: #1e7e34;
            }
            .ac-status {
                padding: 6px 10px;
                margin-bottom: 8px;
                border-radius: 4px;
                font-weight: bold;
                font-size: 0.9em;
            }
            .ac-connected { background-color: #d4edda; color: #155724; }
            .ac-disconnected { background-color: #f8d7da; color: #721c24; }
            .class-badge {
                display: inline-block;
                padding: 1px 5px;
                border-radius: 3px;
                font-size: 0.7em;
                font-weight: bold;
                color: white;
                margin-right: 4px;
                vertical-align: middle;
            }
            #auto-dir-btn {
                background: #333;
                color: #fff;
                border: 1px solid #555;
                transition: background-color 0.3s;
            }
            #auto-dir-btn.active {
                background-color: #28a745 !important;
                border-color: #1e7e34;
            }

            @media (max-width: 600px) {
                body { padding: 4px 6px; overflow: auto; }
                .ac-status { padding: 3px 6px; margin-bottom: 3px; font-size: 0.75em; }
                .camera-buttons { gap: 3px; margin-bottom: 3px; }
                .camera-buttons button { padding: 5px 2px; font-size: 0.75em; }
                .subcam-bar { gap: 3px; margin-bottom: 3px; }
                .subcam-bar button { padding: 3px 2px; font-size: 0.7em; }
                .drivers-grid {
                    grid-template-columns: repeat(2, 1fr);
                    grid-auto-rows: auto;
                    height: auto;
                    gap: 3px;
                }
                .driver-item { padding: 3px 5px; }
                .driver-info { font-size: 0.75em; }
                .driver-info strong {
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                    max-width: 80%;
                }
                .driver-info span { font-size: 0.85em; }
                .driver-gaps { font-size: 0.7em; margin-top: 1px; }
                .driver-gaps span { margin-right: 6px; }
            }
        </style>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.js"></script>
        <script>
            const socket = io();
            var collisionTimers = {};

            var rafTickCount = 0;
            var rafCollidingCount = 0;
            function animateCollisions() {
                rafTickCount++;
                var pulse = (Math.sin(Date.now() / 500 * Math.PI) + 1) / 2;
                var intensity = 0.15 + pulse * 0.35;
                var color = 'rgba(229, 57, 53, ' + intensity + ')';
                var els = document.querySelectorAll('.driver-item.colliding');
                rafCollidingCount = els.length;
                els.forEach(function(el) {
                    el.style.backgroundColor = color;
                });
                requestAnimationFrame(animateCollisions);
            }
            requestAnimationFrame(animateCollisions);

            // Debug helpers — call from DevTools console
            window.debugCollision = function() {
                console.log('[collision debug]');
                console.log('  rAF ticks:', rafTickCount, '(should grow ~60/sec)');
                console.log('  currently animated elements:', rafCollidingCount);
                console.log('  collisionTimers:', collisionTimers);
                console.log('  .driver-item count:', document.querySelectorAll('.driver-item').length);
                console.log('  .driver-item.colliding count:', document.querySelectorAll('.driver-item.colliding').length);
            };
            window.fakeCollision = function(num) {
                num = num || 1;
                collisionTimers[num] = Date.now();
                console.log('[collision debug] forced collision on num', num, '— will last 1s');
            };

            function changeCamera(camId, carCam) {
                var fd = new FormData();
                fd.append('action', 'change_camera');
                fd.append('camera', camId);
                fd.append('car_camera', carCam !== undefined ? carCam : -1);
                fetch('/', { method: 'POST', body: fd });
            }

            // Delegated listener on the stable container — survives innerHTML rebuilds
            document.addEventListener('DOMContentLoaded', function() {
                document.getElementById('drivers-list').addEventListener('pointerdown', function(e) {
                    var card = e.target.closest('.driver-item');
                    if (card && !card.classList.contains('disabled')) {
                        var fd = new FormData();
                        fd.append('action', 'select_driver');
                        fd.append('driver_num', card.dataset.num);
                        fetch('/', { method: 'POST', body: fd });
                    }
                });

                // Same pointerdown pattern for camera bar & subcam bar — onclick has
                // touch latency and gets cancelled by tiny finger movement.
                document.querySelector('.camera-buttons').addEventListener('pointerdown', function(e) {
                    var btn = e.target.closest('button[data-cam]');
                    if (btn) {
                        e.preventDefault();
                        changeCamera(parseInt(btn.dataset.cam));
                    }
                });
                document.getElementById('subcam-bar').addEventListener('pointerdown', function(e) {
                    var btn = e.target.closest('button[data-carcam]');
                    if (btn) {
                        e.preventDefault();
                        changeCamera(4, parseInt(btn.dataset.carcam));
                    }
                });
            });

            socket.on('director_status', function(data) {
                var btn = document.getElementById('auto-dir-btn');
                if (btn) btn.classList.toggle('active', data.enabled);
            });

            socket.on('update', function(data) {
                var statusEl = document.getElementById('ac-status');
                if (!data.ac_connected) {
                    statusEl.className = 'ac-status ac-disconnected';
                    statusEl.textContent = 'Waiting for Assetto Corsa...';
                    document.getElementById('drivers-list').innerHTML = '<p>No data available. Start Assetto Corsa with the Broadcaster Remote app enabled.</p>';
                    return;
                }
                statusEl.className = 'ac-status ac-connected';
                statusEl.textContent = 'Connected to Assetto Corsa | Focused: Driver ' + data.current_driver;

                // Auto-director button state
                var autoBtn = document.getElementById('auto-dir-btn');
                if (autoBtn) autoBtn.classList.toggle('active', !!data.auto_director);

                // Highlight active camera button
                document.querySelectorAll('.camera-buttons button[data-cam]').forEach(function(btn) {
                    btn.classList.toggle('active', parseInt(btn.dataset.cam) === data.current_camera);
                });

                // Build subcamera bar for F6 cameras
                var subcamEl = document.getElementById('subcam-bar');
                if (data.car_cameras_count > 0) {
                    var html = '';
                    for (var i = 0; i < data.car_cameras_count; i++) {
                        var cls = (data.current_camera === 4 && data.current_car_camera === i) ? ' active' : '';
                        html += '<button class="' + cls + '" data-carcam="' + i + '">Cam ' + (i + 1) + '</button>';
                    }
                    subcamEl.innerHTML = html;
                    subcamEl.style.display = 'flex';
                } else {
                    subcamEl.innerHTML = '';
                    subcamEl.style.display = 'none';
                }

                var driversList = document.getElementById('drivers-list');
                if (data.drivers.length > 0) {
                    var grid = '<div class="drivers-grid">';
                    data.drivers.forEach(function(d) {
                        if (d.colliding) {
                            collisionTimers[d.num] = Date.now();
                            console.log('[collision] socket reports colliding: num=' + d.num + ' name=' + d.name);
                        }
                        var isColliding = collisionTimers[d.num] && (Date.now() - collisionTimers[d.num] < 1000);
                        var isSelected = d.num - 1 === data.current_driver;
                        var isDisabled = isSelected || d.status === "OFFLINE";
                        var cls = 'driver-item';
                        if (isColliding) cls += ' colliding';
                        if (isSelected) cls += ' selected';
                        if (isDisabled) cls += ' disabled';
                        grid += '<div class="' + cls + '" data-num="' + d.num + '">';
                        grid += '<div class="driver-info">';
                        // Class badge
                        if (d.car_class && d.car_class !== 'Unclassed') {
                            grid += '<span class="class-badge" style="background-color:' + d.class_color + '">' + d.car_class + '</span>';
                        }
                        grid += '<strong>P' + d.position + ' #' + d.car_number + ' ' + d.name + '</strong>';
                        // Class position
                        if (d.car_class && d.car_class !== 'Unclassed') {
                            grid += ' <span style="font-size:0.8em;color:#888">(' + d.car_class + ' P' + d.class_position + ')</span>';
                        }
                        grid += ' <span>' + (d.status || '') + '</span>';
                        grid += '</div>';
                        grid += '<div class="driver-gaps">';
                        grid += '<span>Gap: ' + d.gap + '</span>';
                        var intColor = '';
                        var m = d.interval.match(/^\+(\d+\.?\d*)s$/);
                        if (m) { var v = parseFloat(m[1]); if (v < 0.4) intColor = ' style="color:#e53935"'; else if (v < 1.0) intColor = ' style="color:#fb8c00"'; }
                        grid += '<span' + intColor + '>Int: ' + d.interval + '</span>';
                        // Class interval
                        if (d.car_class && d.car_class !== 'Unclassed' && d.class_interval && d.class_interval !== '-') {
                            var clsIntColor = '';
                            var mc = d.class_interval.match(/^\+(\d+\.?\d*)s$/);
                            if (mc) { var vc = parseFloat(mc[1]); if (vc < 0.4) clsIntColor = ' style="color:#e53935"'; else if (vc < 1.0) clsIntColor = ' style="color:#fb8c00"'; }
                            grid += '<span' + clsIntColor + '>Cls: ' + d.class_interval + '</span>';
                        }
                        grid += '</div>';
                        grid += '</div>';
                    });
                    grid += '</div>';
                    driversList.innerHTML = grid;
                } else {
                    driversList.innerHTML = '<p>No drivers data available.</p>';
                }
            });
        </script>
    </head>
    <body>
        <div id="ac-status" class="ac-status ac-disconnected">Connecting...</div>

        <div class="camera-buttons">
            <button data-cam="1">Track</button>
            <button data-cam="2">Cockpit</button>
            <button data-cam="3">Heli</button>
            <button data-cam="4">F6</button>
            <button data-cam="5">Orbit</button>
            <button id="auto-dir-btn" onclick="socket.emit('toggle_director')">AUTO</button>
        </div>
        <div id="subcam-bar" class="subcam-bar" style="display:none"></div>

        <div id="drivers-list">
            <p>Connecting...</p>
        </div>

    </body>
    </html>
    '''

    return render_template_string(html)

if __name__ == '__main__':
    threading.Thread(target=monitor_telemetry, daemon=True).start()
    socketio.run(app, host='0.0.0.0')
