import mmap
import ctypes
import threading
import time
from flask import Flask, render_template_string, request
from flask_socketio import SocketIO, emit

from ipc_shared import TelemetryPage, CommandPage, MAX_CARS
from ipc_shared import TELEMETRY_TAG, COMMAND_TAG

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
    cars = []
    for i in range(telem.car_count):
        c = telem.cars[i]
        if not c.is_connected:
            continue
        cars.append({
            'car_id': c.car_id,
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
            'name': c.driver_name,
        })

    cars.sort(key=lambda x: x['position'])
    track_length = telem.track_length

    for idx, car in enumerate(cars):
        car['total_progress'] = car['lap_count'] + car['spline']

    for idx, car in enumerate(cars):
        if idx == 0:
            car['gap'] = 'Leader'
            car['interval'] = '-'
            continue

        leader = cars[0]
        ahead = cars[idx - 1]

        # Gap to leader
        car['gap'] = _format_gap(leader, car, track_length)
        # Interval to car ahead
        car['interval'] = _format_gap(ahead, car, track_length)

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


def build_update_data(telem):
    """Build the SocketIO update payload from telemetry."""
    cars_with_gaps = compute_gaps(telem)
    drivers = []
    for c in cars_with_gaps:
        status = ""
        if c['is_in_pit']:
            status = "PIT"
        if not c['is_connected']:
            status = "OFFLINE"
        drivers.append({
            'name': c['name'],
            'num': c['car_id'] + 1,
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
            data = build_update_data(telem)
            socketio.emit('update', data)

        time.sleep(0.1)


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
            driver = selected_num - 1
            # Read current camera from telemetry so we preserve it
            telem = read_telemetry()
            camera = telem.current_camera if telem else 0
            send_command(driver, camera)

        elif action == 'change_camera':
            camera = int(request.form.get('camera'))
            car_camera = int(request.form.get('car_camera', -1))
            telem = read_telemetry()
            driver = telem.focused_car if telem else 0
            send_command(driver, camera, car_camera)

        return '', 204  # No content response for AJAX

    # HTML template for the interface
    html = '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Broadcaster Remote Control</title>
        <meta name=viewport content="width=device-width, initial-scale=1">
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; }
            table { border-collapse: collapse; width: 100%; }
            th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
            th { background-color: #f2f2f2; }
            select, button { padding: 15px; margin: 5px 0; font-size: 1.2em; width: 100%; box-sizing: border-box; }
            .drivers-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(350px, 1fr));
                gap: 10px;
            }
            .driver-item {
                border: 1px solid #ddd;
                padding: 10px;
                display: flex;
                flex-direction: column;
                justify-content: space-between;
                transition: background-color 0.3s;
            }
            @keyframes collision-flash {
                0%, 100% { background-color: rgba(229, 57, 53, 0.1); }
                50% { background-color: rgba(229, 57, 53, 0.35); }
            }
            .driver-item.colliding {
                animation: collision-flash 1s ease-in-out infinite;
                border-color: rgba(229, 57, 53, 0.5);
            }
            .driver-item button {
                padding: 8px 12px;
                font-size: 0.9em;
                margin-top: 5px;
            }
            .driver-info {
                display: flex;
                justify-content: space-between;
                align-items: center;
            }
            .driver-gaps {
                font-size: 0.85em;
                color: #666;
                margin-top: 4px;
            }
            .driver-gaps span {
                margin-right: 12px;
            }
            .camera-buttons {
                display: flex;
                gap: 8px;
                margin-bottom: 20px;
            }
            .camera-buttons button {
                flex: 1;
                padding: 12px 8px;
                font-size: 1em;
            }
            .camera-buttons button.active {
                background-color: #007bff;
                color: white;
                border-color: #0056b3;
            }
            .subcam-bar {
                display: flex;
                gap: 6px;
                margin-bottom: 20px;
            }
            .subcam-bar button {
                flex: 1;
                padding: 8px 4px;
                font-size: 0.9em;
            }
            .subcam-bar button.active {
                background-color: #28a745;
                color: white;
                border-color: #1e7e34;
            }
            .ac-status {
                padding: 10px;
                margin-bottom: 15px;
                border-radius: 4px;
                font-weight: bold;
            }
            .ac-connected { background-color: #d4edda; color: #155724; }
            .ac-disconnected { background-color: #f8d7da; color: #721c24; }
        </style>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.js"></script>
        <script>
            const socket = io();
            var collisionTimers = {};

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
                    var btn = e.target.closest('.select-btn');
                    if (btn && !btn.disabled) {
                        var fd = new FormData();
                        fd.append('action', 'select_driver');
                        fd.append('driver_num', btn.dataset.num);
                        fetch('/', { method: 'POST', body: fd });
                    }
                });
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

                // Highlight active camera button
                document.querySelectorAll('.camera-buttons button').forEach(function(btn) {
                    btn.classList.toggle('active', parseInt(btn.dataset.cam) === data.current_camera);
                });

                // Build subcamera bar for F6 cameras
                var subcamEl = document.getElementById('subcam-bar');
                if (data.car_cameras_count > 0) {
                    var html = '';
                    for (var i = 0; i < data.car_cameras_count; i++) {
                        var cls = (data.current_camera === 4 && data.current_car_camera === i) ? ' active' : '';
                        html += '<button class="' + cls + '" onclick="changeCamera(4,' + i + ')">Cam ' + (i + 1) + '</button>';
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
                        if (d.colliding) { collisionTimers[d.num] = Date.now(); }
                        var isColliding = collisionTimers[d.num] && (Date.now() - collisionTimers[d.num] < 1000);
                        grid += '<div class="driver-item' + (isColliding ? ' colliding' : '') + '">';
                        grid += '<div class="driver-info"><strong>' + d.position + '. ' + d.name + '</strong> <span>' + (d.status || '') + '</span></div>';
                        grid += '<div class="driver-gaps">';
                        grid += '<span>Gap: ' + d.gap + '</span>';
                        var intColor = '';
                        var m = d.interval.match(/^\+(\d+\.?\d*)s$/);
                        if (m) { var v = parseFloat(m[1]); if (v < 0.4) intColor = ' style="color:#e53935"'; else if (v < 1.0) intColor = ' style="color:#fb8c00"'; }
                        grid += '<span' + intColor + '>Int: ' + d.interval + '</span>';
                        grid += '</div>';
                        grid += '<button class="select-btn" data-num="' + d.num + '"' + (d.num - 1 === data.current_driver || d.status === "OFFLINE" ? ' disabled' : '') + '>Select</button>';
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
        <h1>Broadcaster Remote Control</h1>

        <div id="ac-status" class="ac-status ac-disconnected">Connecting...</div>

        <div class="camera-buttons">
            <button data-cam="1" onclick="changeCamera(1)">Track</button>
            <button data-cam="2" onclick="changeCamera(2)">Cockpit</button>
            <button data-cam="3" onclick="changeCamera(3)">Heli</button>
            <button data-cam="4" onclick="changeCamera(4)">F6</button>
            <button data-cam="5" onclick="changeCamera(5)">Orbit</button>
        </div>
        <div id="subcam-bar" class="subcam-bar" style="display:none"></div>

        <h2>Drivers List</h2>
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
