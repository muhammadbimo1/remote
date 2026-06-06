import mmap
import ctypes
import threading
import time
import json
import os
import re
from collections import defaultdict
from urllib.parse import urlparse
from flask import Flask, render_template_string, request
from flask_socketio import SocketIO, emit

import requests

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

# --- Class colors ---
CLASS_COLORS = {
    'PRO': '#34a853',
    'HY': '#ff0000',
    'AM': '#e69138',
}

# --- Timetable re-sync (poll AC server /timetable.json) ---
TIMETABLE_POLL_INTERVAL = 60.0
TIMETABLE_TIMEOUT = 3.0

_resync_lock = threading.Lock()
progress_offsets = {}        # { car_id:int -> float lap-count correction }
last_poll_time = 0.0
last_poll_status = 'idle'    # 'idle' | 'ok' | 'not_configured' | 'disabled_not_race' | 'http_error' | 'unreachable' | 'bad_response'
last_poll_error = ''
last_poll_matched = 0


def get_car_class(team_name):
    """Return (class_name, class_color) parsed from a team name.

    Format: "CLASS 42 | Team" -> "CLASS". Names without a pipe are unclassed.
    """
    if not team_name or '|' not in team_name:
        return 'Unclassed', '#999'
    class_name = re.sub(r'\d+\s*$', '', team_name.split('|', 1)[0]).strip()
    if not class_name:
        return 'Unclassed', '#999'
    return class_name, CLASS_COLORS.get(class_name.upper(), '#999')


def _parse_driver_name(raw_name, car_id):
    """Parse the "NN | Driver Name" mmap format. Returns (car_number, display_name).
    Falls back to (car_id+1, raw_name) when the prefix is missing or non-numeric."""
    parts = raw_name.split('|', 1)
    if len(parts) == 2:
        try:
            car_number = int(parts[0].strip())
        except ValueError:
            car_number = car_id + 1
        display_name = parts[1].strip()
    else:
        car_number = car_id + 1
        display_name = raw_name
    return car_number, display_name



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

        raw_name = c.driver_name
        team_name = c.team_name
        car_number, display_name = _parse_driver_name(raw_name, c.car_id)

        cls_name, cls_color = get_car_class(team_name)

        cars.append({
            'car_id': c.car_id,
            'car_number': car_number,
            'display_name': display_name,
            'team_name': team_name,
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
            'is_rolled_over': c.is_rolled_over,
            'name': raw_name,
        })

    cars.sort(key=lambda x: x['position'])
    track_length = telem.track_length

    with _resync_lock:
        offsets_snapshot = dict(progress_offsets)

    for idx, car in enumerate(cars):
        offset = offsets_snapshot.get(car['car_id'], 0.0)
        car['total_progress'] = car['lap_count'] + car['spline'] + offset

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


def get_timetable_url(telem):
    """Return the AC server timetable URL provided by the Lua mmap."""
    return (getattr(telem, 'timetable_url', '') or '').strip()


def _validate_timetable_url(url):
    """Return (ok, error). Empty url is valid (clears config)."""
    if not url:
        return True, ''
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        return False, 'URL must use http:// or https://'
    if not parsed.hostname:
        return False, 'URL missing host'
    if not parsed.path.endswith('/timetable.json'):
        return False, "URL path must end with /timetable.json"
    return True, ''


def fetch_timetable(url):
    """GET the timetable URL. Returns parsed JSON dict, or None on failure.
    Updates last_poll_status/last_poll_error on failure."""
    global last_poll_status, last_poll_error
    ok, err = _validate_timetable_url(url)
    if not ok:
        last_poll_status = 'bad_response'
        last_poll_error = 'invalid url: {}'.format(err)
        return None
    try:
        resp = requests.get(url, timeout=TIMETABLE_TIMEOUT, allow_redirects=False)
    except requests.RequestException as e:
        last_poll_status = 'unreachable'
        last_poll_error = str(e)
        print('[remote_web] timetable unreachable: {}'.format(e))
        return None
    if resp.status_code != 200:
        last_poll_status = 'http_error'
        last_poll_error = 'HTTP {}'.format(resp.status_code)
        print('[remote_web] timetable http {}'.format(resp.status_code))
        return None
    try:
        return resp.json()
    except ValueError as e:
        last_poll_status = 'bad_response'
        last_poll_error = 'JSON parse: {}'.format(e)
        return None


def apply_timetable_offsets(data, telem):
    """Compute per-car offset = timetable.Laps - local.lap_count from the
    EntryList. Timetable CarID is the online server slot, so match it to
    connected local cars by session_id, not by local car_id. The poller
    skips rows with Ping == 0 because offline rows can report zero laps.
    Atomically replace the offsets dict. Returns the number of matched offsets."""
    global last_poll_matched
    entries = data.get('EntryList') or []
    local_by_session_id = {}
    for i in range(telem.car_count):
        c = telem.cars[i]
        if not c.is_connected:
            continue
        local_by_session_id[int(c.session_id)] = (int(c.car_id), int(c.lap_count))

    new_offsets = {}
    matched = 0
    for entry in entries:
        try:
            car_id = int(entry.get('CarID'))
        except (TypeError, ValueError):
            continue
        if int(entry.get('Ping') or 0) == 0:
            continue
        local_match = local_by_session_id.get(car_id)
        if local_match is None:
            continue
        try:
            server_laps = int(entry.get('Laps') or 0)
        except (TypeError, ValueError):
            continue
        local_car_id, local_lap_count = local_match
        new_offsets[local_car_id] = float(server_laps - local_lap_count)
        matched += 1

    with _resync_lock:
        progress_offsets.clear()
        progress_offsets.update(new_offsets)
    last_poll_matched = matched
    return matched


def poll_timetable_once(url, telem):
    """Fetch and apply one timetable snapshot. Local AC telemetry is the
    source of truth for race/non-race state; some server timetable endpoints
    can lag and report a stale SessionType."""
    global last_poll_time, last_poll_status, last_poll_error
    if telem is None or not ac_connected:
        last_poll_status = 'not_configured'
        last_poll_error = 'no telemetry'
        return False
    if telem.session_type != 1:
        last_poll_status = 'disabled_not_race'
        last_poll_error = ''
        return False

    data = fetch_timetable(url)
    if data is None:
        return False

    matched = apply_timetable_offsets(data, telem)
    last_poll_time = time.time()
    last_poll_status = 'ok'
    last_poll_error = ''
    print('[remote_web] timetable poll ok: matched={}'.format(matched))
    return True


def timetable_poll_loop():
    """Daemon thread: poll the timetable URL every TIMETABLE_POLL_INTERVAL
    seconds during race sessions only."""
    global last_poll_time, last_poll_status, last_poll_error
    while True:
        try:
            telem = read_telemetry()
            url = get_timetable_url(telem) if telem is not None else ''
            if not url:
                last_poll_status = 'not_configured'
                last_poll_error = 'no server HTTP endpoint'
            else:
                poll_timetable_once(url, telem)
        except Exception as e:
            last_poll_status = 'bad_response'
            last_poll_error = 'internal: {}'.format(e)
            print('[remote_web] poll loop error: {}'.format(e))

        time.sleep(TIMETABLE_POLL_INTERVAL)


_prev_rollover_state = {}


def build_update_data(telem, cars_with_gaps=None):
    """Build the SocketIO update payload from telemetry."""
    if cars_with_gaps is None:
        cars_with_gaps = compute_gaps(telem)
    for c in cars_with_gaps:
        if c.get('is_colliding'):
            print('[remote_web] collision flag from car {} ({})'.format(c['car_id'], c['name']))
        cid = c['car_id']
        rolled = bool(c.get('is_rolled_over'))
        if rolled != _prev_rollover_state.get(cid, False):
            print('[ROLLOVER] Car #{} ({}) {}'.format(
                cid, c['name'], 'flipped' if rolled else 'recovered'))
            _prev_rollover_state[cid] = rolled
    drivers = []
    for c in cars_with_gaps:
        status = ""
        if c['is_in_pit']:
            status = "PIT"
        if not c['is_connected']:
            status = "OFFLINE"
        drivers.append({
            'name': c.get('display_name', c['name']),
            'team_name': c.get('team_name', ''),
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
            'rolled_over': bool(c.get('is_rolled_over')),
        })

    with _resync_lock:
        offsets_active = any(abs(v) > 1e-6 for v in progress_offsets.values())
    url = get_timetable_url(telem)

    return {
        'current_driver': telem.focused_car,
        'current_camera': telem.current_camera,
        'car_cameras_count': telem.car_cameras_count,
        'current_car_camera': telem.current_car_camera,
        'drivers': drivers,
        'ac_connected': True,
        'auto_director': director.enabled,
        'auto_director_debug': director.debug,
        'gap_offsets_active': offsets_active,
        'timetable_url': url,
        'timetable_status': last_poll_status,
        'timetable_last_poll': last_poll_time,
        'timetable_last_error': last_poll_error,
        'timetable_matched': last_poll_matched,
        'session_type': telem.session_type,
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
            # Stale offsets must not survive an AC restart — car_ids are reused.
            with _resync_lock:
                progress_offsets.clear()

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


@socketio.on('toggle_director_debug')
def handle_toggle_director_debug():
    with director_lock:
        director.debug = not director.debug
        state = director.debug
    print(f"[director] debug logging {'ON' if state else 'OFF'}")
    emit('director_debug_status', {'enabled': state}, broadcast=True)


@socketio.on('get_timetable_url')
def handle_get_timetable_url():
    emit('timetable_url', {
        'ok': True,
        'status': last_poll_status,
        'last_poll': last_poll_time,
        'error': last_poll_error,
    })


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
        <title>Broadcaster Remote</title>
        <meta name=viewport content="width=device-width, initial-scale=1">
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Oswald:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
        <style>
            :root {
                --bg-base: #0a0d11;
                --bg-panel: #131820;
                --bg-elev: #1b2230;
                --bg-elev-hi: #232b38;
                --border-subtle: #232b38;
                --border-strong: #354154;
                --text-primary: #e6ecf3;
                --text-muted: #8d99aa;
                --text-dim: #5a6678;
                --accent-amber: #ffb71f;
                --accent-amber-deep: #b88500;
                --accent-amber-glow: rgba(255,183,31,0.25);
                --live-green: #2fd17a;
                --live-green-glow: rgba(47,209,122,0.28);
                --live-red: #ff3838;
                --live-red-glow: rgba(255,56,56,0.32);
                --warn-orange: #ff8a1f;
            }
            * { box-sizing: border-box; }
            body {
                font-family: 'Oswald', system-ui, sans-serif;
                margin: 0;
                padding: 10px 12px;
                height: 100dvh;
                height: 100vh;
                display: flex;
                flex-direction: column;
                overflow: hidden;
                background-color: var(--bg-base);
                background-image:
                    linear-gradient(transparent 31px, rgba(255,255,255,0.022) 32px),
                    linear-gradient(90deg, transparent 31px, rgba(255,255,255,0.022) 32px);
                background-size: 32px 32px;
                color: var(--text-primary);
                -webkit-font-smoothing: antialiased;
            }
            @supports (height: 100dvh) {
                body { height: 100dvh; }
            }

            #ac-status {
                display: flex;
                align-items: center;
                justify-content: space-between;
                padding: 8px 14px;
                margin-bottom: 8px;
                background: linear-gradient(180deg, var(--bg-panel) 0%, #0d1218 100%);
                border: 1px solid var(--border-subtle);
                box-shadow: 0 1px 0 0 rgba(255,183,31,0.14) inset, 0 2px 6px rgba(0,0,0,0.5);
                font-family: 'Oswald', sans-serif;
                font-weight: 500;
                text-transform: uppercase;
                letter-spacing: 0.14em;
                font-size: 0.78em;
                color: var(--text-muted);
            }
            #ac-status .status-left, #ac-status .status-right {
                display: flex;
                align-items: center;
                gap: 10px;
            }
            #ac-status .indicator {
                width: 9px;
                height: 9px;
                border-radius: 50%;
                background: var(--live-red);
                box-shadow: 0 0 10px var(--live-red);
                animation: pulse-dot 1.4s ease-in-out infinite;
            }
            #ac-status.ac-connected .indicator {
                background: var(--live-green);
                box-shadow: 0 0 10px var(--live-green);
                animation: none;
            }
            #ac-status .label-link {
                color: var(--live-red);
                font-weight: 600;
                letter-spacing: 0.18em;
            }
            #ac-status.ac-connected .label-link { color: var(--live-green); }
            #ac-status .focus-chip {
                font-family: 'JetBrains Mono', monospace;
                font-feature-settings: "tnum";
                color: var(--text-primary);
                padding: 2px 8px;
                background: var(--bg-base);
                border: 1px solid var(--border-strong);
                letter-spacing: 0.08em;
            }
            @keyframes pulse-dot {
                0%, 100% { opacity: 0.45; }
                50% { opacity: 1; }
            }

            .camera-buttons {
                display: flex;
                gap: 6px;
                margin-bottom: 8px;
            }
            .camera-buttons button {
                flex: 1;
                position: relative;
                padding: 12px 4px 8px;
                background: var(--bg-elev);
                color: var(--text-primary);
                border: 1px solid var(--border-strong);
                font-family: 'Oswald', sans-serif;
                font-weight: 500;
                font-size: 0.95em;
                text-transform: uppercase;
                letter-spacing: 0.14em;
                cursor: pointer;
                transition: background-color 0.12s, border-color 0.12s, transform 0.06s, box-shadow 0.18s;
                overflow: hidden;
            }
            .camera-buttons button::before {
                content: '';
                position: absolute;
                top: 0; left: 0; right: 0;
                height: 2px;
                background: var(--border-strong);
                transition: background 0.15s, box-shadow 0.15s;
            }
            .camera-buttons button .cam-cap {
                display: block;
                margin-top: 4px;
                font-family: 'JetBrains Mono', monospace;
                font-feature-settings: "tnum";
                font-size: 0.62em;
                color: var(--text-dim);
                letter-spacing: 0.22em;
                font-weight: 500;
            }
            .camera-buttons button:hover { background: var(--bg-elev-hi); }
            .camera-buttons button:active { transform: translateY(1px); }
            .camera-buttons button.active {
                background: var(--accent-amber);
                color: #0a0d11;
                border-color: var(--accent-amber-deep);
                box-shadow: 0 0 0 1px var(--accent-amber), 0 0 22px var(--accent-amber-glow);
            }
            .camera-buttons button.active::before {
                background: #ffe58a;
                box-shadow: 0 0 10px rgba(255,229,138,0.95);
            }
            .camera-buttons button.active .cam-cap { color: rgba(10,13,17,0.6); }

            #auto-dir-btn {
                background: var(--bg-elev);
                color: var(--live-green);
                border-color: rgba(47,209,122,0.4);
            }
            #auto-dir-btn::before { background: rgba(47,209,122,0.35); }
            #auto-dir-btn .cam-cap { color: rgba(47,209,122,0.7); }
            #auto-dir-btn.active {
                background: var(--live-green);
                color: #0a0d11;
                border-color: #1c9a55;
                box-shadow: 0 0 0 1px var(--live-green), 0 0 22px var(--live-green-glow);
            }
            #auto-dir-btn.active::before {
                background: #b8f0d0;
                box-shadow: 0 0 10px rgba(184,240,208,0.95);
                animation: tally-pulse 1.6s ease-in-out infinite;
            }
            #auto-dir-btn.active .cam-cap { color: rgba(10,13,17,0.6); }
            @keyframes tally-pulse {
                0%, 100% { opacity: 1; }
                50% { opacity: 0.55; }
            }

            .timetable-bar {
                display: flex;
                align-items: center;
                justify-content: center;
                width: 22px;
                margin-bottom: 8px;
                padding: 6px 0;
                background: var(--bg-panel);
                border: 1px solid var(--border-subtle);
            }
            .timetable-bar .tt-dot {
                width: 9px;
                height: 9px;
                border-radius: 50%;
                background: var(--text-dim);
                box-shadow: 0 0 0 1px rgba(0,0,0,0.4);
                flex: 0 0 auto;
            }
            .timetable-bar .tt-dot.ok { background: var(--live-green); box-shadow: 0 0 8px var(--live-green-glow); }
            .timetable-bar .tt-dot.err { background: var(--live-red); box-shadow: 0 0 8px var(--live-red-glow); }
            .timetable-bar .tt-dot.idle { background: var(--text-dim); }
            .timetable-bar .tt-dot.warn { background: var(--accent-amber); box-shadow: 0 0 8px var(--accent-amber-glow); }
            .subcam-bar {
                display: flex;
                gap: 6px;
                margin-bottom: 8px;
            }
            .subcam-bar button {
                flex: 1;
                padding: 6px 4px;
                background: var(--bg-panel);
                color: var(--text-muted);
                border: 1px solid var(--border-subtle);
                font-family: 'JetBrains Mono', monospace;
                font-feature-settings: "tnum";
                font-size: 0.7em;
                letter-spacing: 0.2em;
                text-transform: uppercase;
                cursor: pointer;
                transition: background-color 0.12s, color 0.12s, border-color 0.12s;
            }
            .subcam-bar button:hover {
                background: var(--bg-elev);
                color: var(--text-primary);
                border-color: var(--border-strong);
            }
            .subcam-bar button.active {
                background: var(--accent-amber);
                color: #0a0d11;
                border-color: var(--accent-amber-deep);
                box-shadow: 0 0 14px var(--accent-amber-glow);
            }

            .class-filter-bar {
                display: flex;
                align-items: center;
                gap: 6px;
                margin-bottom: 8px;
                min-height: 30px;
                overflow-x: auto;
            }
            .class-filter-bar button {
                display: inline-flex;
                align-items: center;
                gap: 6px;
                flex: 0 0 auto;
                padding: 6px 10px;
                background: var(--bg-panel);
                color: var(--text-muted);
                border: 1px solid var(--border-subtle);
                font-family: 'JetBrains Mono', monospace;
                font-size: 0.68em;
                letter-spacing: 0.16em;
                text-transform: uppercase;
                cursor: pointer;
                transition: background-color 0.12s, color 0.12s, border-color 0.12s;
            }
            .class-filter-bar button:hover {
                background: var(--bg-elev);
                color: var(--text-primary);
                border-color: var(--border-strong);
            }
            .class-filter-bar button.active {
                background: var(--bg-elev-hi);
                color: var(--text-primary);
                border-color: var(--accent-amber);
                box-shadow: inset 0 0 0 1px rgba(255,196,35,0.22);
            }
            .class-filter-bar .class-dot {
                width: 8px;
                height: 8px;
                border-radius: 50%;
                background: var(--border-strong);
                box-shadow: 0 0 0 1px rgba(0,0,0,0.3);
            }

            #drivers-list {
                flex: 1;
                min-height: 0;
            }
            .drivers-grid {
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
                grid-auto-rows: 1fr;
                gap: 6px;
                height: 100%;
            }
            .driver-item {
                position: relative;
                display: flex;
                flex-direction: column;
                justify-content: center;
                padding: 8px 10px 8px 16px;
                background: var(--bg-panel);
                border: 1px solid var(--border-subtle);
                overflow: hidden;
                cursor: pointer;
                transition: background-color 0.18s, border-color 0.18s;
                min-height: 0;
            }
            .driver-item .class-stripe {
                position: absolute;
                top: 0; bottom: 0; left: 0;
                width: 4px;
                background: var(--border-strong);
            }
            .driver-item:hover {
                background: var(--bg-elev);
                border-color: var(--border-strong);
            }
            .driver-item.disabled { cursor: default; }
            .driver-item.disabled:hover {
                background: var(--bg-panel);
                border-color: var(--border-subtle);
            }
            .driver-item.selected {
                background: linear-gradient(180deg, rgba(255,56,56,0.08) 0%, rgba(255,56,56,0) 90%), var(--bg-panel);
                border-color: var(--live-red);
                box-shadow: inset 0 0 0 1px rgba(255,56,56,0.22), 0 0 18px rgba(255,56,56,0.12);
            }
            .driver-item.selected:hover {
                background: linear-gradient(180deg, rgba(255,56,56,0.08) 0%, rgba(255,56,56,0) 90%), var(--bg-panel);
                border-color: var(--live-red);
            }
            .driver-item.offline { opacity: 0.32; }
            .driver-item.offline.selected { opacity: 0.55; }

            .driver-item.colliding {
                border-color: var(--live-red) !important;
                z-index: 1;
            }
            .driver-item.colliding .class-stripe {
                background: var(--live-red) !important;
                box-shadow: 0 0 10px rgba(255,56,56,0.8);
                animation: stripe-flash 0.72s ease-in-out infinite;
            }
            .driver-item.colliding .name {
                color: #ffe6e6;
                text-shadow: 0 0 8px rgba(255,56,56,0.5);
            }
            @keyframes stripe-flash {
                0%, 100% { opacity: 0.55; }
                50% { opacity: 1; }
            }

            .driver-item.rolled-over {
                border-color: #ff8a1f !important;
                z-index: 1;
            }
            .driver-item.rolled-over .class-stripe {
                background: #ff8a1f !important;
                box-shadow: 0 0 10px rgba(255,138,31,0.85);
                animation: stripe-flash 0.6s ease-in-out infinite;
            }
            .driver-item.rolled-over .name {
                color: #fff1dc;
                text-shadow: 0 0 8px rgba(255,138,31,0.55);
            }
            .status-chip.rollover-chip {
                background: #ff3030;
                color: #fff;
                font-weight: 700;
                letter-spacing: 0.04em;
                text-transform: uppercase;
            }

            .driver-info {
                display: flex;
                align-items: center;
                gap: 8px;
                min-width: 0;
            }
            .driver-info .pos {
                font-family: 'Oswald', sans-serif;
                font-weight: 600;
                font-size: 1.3em;
                line-height: 1;
                color: var(--text-primary);
                font-feature-settings: "tnum";
                min-width: 1.5em;
                text-align: right;
                flex: 0 0 auto;
            }
            .driver-info .num {
                font-family: 'JetBrains Mono', monospace;
                font-feature-settings: "tnum";
                font-size: 0.78em;
                color: var(--accent-amber);
                letter-spacing: 0.04em;
                flex: 0 0 auto;
            }
            .driver-info .name {
                font-family: 'Oswald', sans-serif;
                font-weight: 500;
                font-size: 0.95em;
                text-transform: uppercase;
                letter-spacing: 0.05em;
                color: var(--text-primary);
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
                min-width: 0;
                flex: 1 1 auto;
            }
            .driver-info .name-stack {
                display: flex;
                flex-direction: column;
                min-width: 0;
                flex: 1 1 auto;
                line-height: 1.05;
            }
            .driver-info .name-stack .name {
                flex: 0 1 auto;
            }
            .driver-info .team-name {
                margin-top: 2px;
                font-family: 'JetBrains Mono', monospace;
                font-size: 0.62em;
                color: var(--text-dim);
                letter-spacing: 0.04em;
                text-transform: uppercase;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
            }
            .driver-info .right {
                margin-left: auto;
                display: flex;
                align-items: center;
                gap: 4px;
                flex: 0 0 auto;
            }
            .class-badge {
                display: inline-block;
                padding: 1px 5px;
                font-family: 'Oswald', sans-serif;
                font-size: 0.65em;
                font-weight: 600;
                color: #fff;
                text-transform: uppercase;
                letter-spacing: 0.12em;
                line-height: 1.5;
            }
            .status-chip {
                display: inline-block;
                padding: 1px 5px;
                font-family: 'JetBrains Mono', monospace;
                font-size: 0.62em;
                font-weight: 500;
                letter-spacing: 0.16em;
                text-transform: uppercase;
                line-height: 1.6;
            }
            .status-chip.pit {
                background: var(--accent-amber);
                color: #0a0d11;
            }
            .status-chip.offline {
                border: 1px solid var(--border-strong);
                color: var(--text-dim);
            }
            .on-air-chip {
                display: inline-flex;
                align-items: center;
                gap: 4px;
                padding: 2px 6px;
                background: var(--live-red);
                color: #fff;
                font-family: 'Oswald', sans-serif;
                font-size: 0.65em;
                font-weight: 600;
                letter-spacing: 0.16em;
                text-transform: uppercase;
                line-height: 1.4;
                box-shadow: 0 0 12px var(--live-red-glow);
            }
            .on-air-chip::before {
                content: '';
                width: 5px;
                height: 5px;
                border-radius: 50%;
                background: #fff;
                animation: pulse-dot 1s ease-in-out infinite;
            }

            .driver-gaps {
                display: flex;
                align-items: baseline;
                gap: 12px;
                margin-top: 5px;
                font-family: 'JetBrains Mono', monospace;
                font-feature-settings: "tnum";
                font-size: 0.78em;
            }
            .driver-gaps .cell {
                display: inline-flex;
                align-items: baseline;
                gap: 5px;
            }
            .driver-gaps .cell .label {
                font-size: 0.78em;
                color: var(--text-dim);
                letter-spacing: 0.18em;
                text-transform: uppercase;
            }
            .driver-gaps .cell .value { color: var(--text-primary); }
            .driver-gaps .cell .value.warn { color: var(--warn-orange); }
            .driver-gaps .cell .value.tight { color: var(--live-red); }
            .driver-gaps .class-pos {
                color: var(--text-dim);
                font-size: 0.75em;
                margin-left: auto;
                letter-spacing: 0.16em;
                text-transform: uppercase;
                white-space: nowrap;
            }

            #drivers-list .empty {
                padding: 24px 4px;
                color: var(--text-dim);
                font-family: 'Oswald', sans-serif;
                letter-spacing: 0.14em;
                text-transform: uppercase;
                font-size: 0.85em;
            }

            @media (max-width: 600px) {
                body { padding: 6px 8px; overflow: auto; background-size: 24px 24px; }
                #ac-status { padding: 5px 8px; font-size: 0.66em; margin-bottom: 5px; letter-spacing: 0.1em; }
                .camera-buttons { gap: 4px; margin-bottom: 5px; }
                .camera-buttons button { padding: 7px 2px 5px; font-size: 0.78em; letter-spacing: 0.08em; }
                .camera-buttons button .cam-cap { display: none; }
                .subcam-bar { gap: 4px; margin-bottom: 5px; }
                .subcam-bar button { padding: 4px 2px; font-size: 0.62em; }
                .class-filter-bar { gap: 4px; margin-bottom: 5px; min-height: 26px; }
                .class-filter-bar button { padding: 4px 7px; font-size: 0.58em; letter-spacing: 0.1em; }
                .timetable-bar { width: 20px; padding: 4px 0; margin-bottom: 5px; }
                .drivers-grid {
                    grid-template-columns: repeat(2, 1fr);
                    grid-auto-rows: auto;
                    height: auto;
                    gap: 4px;
                }
                .driver-item { padding: 5px 6px 5px 11px; }
                .driver-item .class-stripe { width: 3px; }
                .driver-info { gap: 5px; }
                .driver-info .pos { font-size: 1.05em; min-width: 1.3em; }
                .driver-info .num { font-size: 0.65em; }
                .driver-info .name { font-size: 0.78em; letter-spacing: 0.02em; }
                .driver-info .team-name { font-size: 0.55em; letter-spacing: 0.02em; }
                .driver-gaps { font-size: 0.65em; gap: 8px; margin-top: 2px; }
                .driver-gaps .class-pos { display: none; }
                .class-badge, .status-chip, .on-air-chip { font-size: 0.55em; padding: 1px 3px; letter-spacing: 0.1em; }
            }
        </style>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.js"></script>
        <script>
            const socket = io();
            var collisionTimers = {};
            var activeClassFilter = 'ALL';

            var rafTickCount = 0;
            var rafCollidingCount = 0;
            function animateCollisions() {
                rafTickCount++;
                var pulse = (Math.sin(Date.now() / 360 * Math.PI) + 1) / 2;
                var ring = 0.7 + pulse * 0.3;       // 0.70 - 1.00 inner ring
                var halo = 0.45 + pulse * 0.45;     // 0.45 - 0.90 outer glow
                var tint = 0.10 + pulse * 0.18;     // 0.10 - 0.28 bg tint
                var els = document.querySelectorAll('.driver-item.colliding');
                rafCollidingCount = els.length;
                els.forEach(function(el) {
                    el.style.boxShadow = 'inset 0 0 0 2px rgba(255,56,56,' + ring + '), 0 0 38px rgba(255,56,56,' + halo + ')';
                    el.style.backgroundColor = 'rgba(255,56,56,' + tint + ')';
                });

                // Rollover throb — orange/red, slightly slower than collision.
                var rPulse = (Math.sin(Date.now() / 420 * Math.PI) + 1) / 2;
                var rRing = 0.7 + rPulse * 0.3;
                var rHalo = 0.4 + rPulse * 0.45;
                var rTint = 0.08 + rPulse * 0.18;
                var rolledEls = document.querySelectorAll('.driver-item.rolled-over:not(.colliding)');
                rolledEls.forEach(function(el) {
                    el.style.boxShadow = 'inset 0 0 0 2px rgba(255,138,31,' + rRing + '), 0 0 38px rgba(255,138,31,' + rHalo + ')';
                    el.style.backgroundColor = 'rgba(255,138,31,' + rTint + ')';
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

            function escapeHtml(s) {
                return String(s).replace(/[&<>"']/g, function(c) {
                    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
                });
            }

            function buildStatusBar(connected, focusLabel) {
                var linkLabel = connected ? 'AC Link' : 'No Link';
                var subLabel = connected ? 'Live' : 'Waiting';
                return '<div class="status-left">'
                    + '<span class="indicator"></span>'
                    + '<span class="label-link">' + linkLabel + '</span>'
                    + '<span>' + subLabel + '</span>'
                    + '</div>'
                    + '<div class="status-right">'
                    + '<span>Focus</span>'
                    + '<span class="focus-chip">' + focusLabel + '</span>'
                    + '</div>';
            }

            function renderClassFilters(drivers) {
                var bar = document.getElementById('class-filter-bar');
                if (!bar) return;

                var classMap = {};
                drivers.forEach(function(d) {
                    if (d.car_class && d.car_class !== 'Unclassed') {
                        classMap[d.car_class] = d.class_color || '#999';
                    }
                });

                var classes = Object.keys(classMap);
                var order = { PRO: 0, HY: 1, AM: 2 };
                classes.sort(function(a, b) {
                    var au = a.toUpperCase();
                    var bu = b.toUpperCase();
                    var ao = Object.prototype.hasOwnProperty.call(order, au) ? order[au] : 100;
                    var bo = Object.prototype.hasOwnProperty.call(order, bu) ? order[bu] : 100;
                    if (ao !== bo) return ao - bo;
                    return a.localeCompare(b);
                });

                if (activeClassFilter !== 'ALL' && !classMap[activeClassFilter]) {
                    activeClassFilter = 'ALL';
                }

                var html = '<button type="button" data-class="ALL" class="' + (activeClassFilter === 'ALL' ? 'active' : '') + '">'
                    + '<span class="class-dot"></span><span>All</span></button>';
                classes.forEach(function(cls) {
                    var active = activeClassFilter === cls ? 'active' : '';
                    html += '<button type="button" data-class="' + escapeHtml(cls) + '" class="' + active + '">'
                        + '<span class="class-dot" style="background:' + escapeHtml(classMap[cls]) + '"></span>'
                        + '<span>' + escapeHtml(cls) + '</span></button>';
                });
                bar.innerHTML = html;
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
                document.getElementById('class-filter-bar').addEventListener('pointerdown', function(e) {
                    var btn = e.target.closest('button[data-class]');
                    if (btn) {
                        e.preventDefault();
                        activeClassFilter = btn.dataset.class || 'ALL';
                        document.querySelectorAll('#class-filter-bar button').forEach(function(b) {
                            b.classList.toggle('active', b.dataset.class === activeClassFilter);
                        });
                        document.querySelectorAll('.driver-item').forEach(function(card) {
                            card.style.display = activeClassFilter === 'ALL' || card.dataset.class === activeClassFilter ? '' : 'none';
                        });
                    }
                });

                socket.emit('get_timetable_url');
            });

            socket.on('director_status', function(data) {
                var btn = document.getElementById('auto-dir-btn');
                if (btn) btn.classList.toggle('active', data.enabled);
            });

            socket.on('director_debug_status', function(data) {
                var btn = document.getElementById('auto-dir-dbg-btn');
                if (btn) btn.classList.toggle('active', data.enabled);
            });

            function setTimetableDot(status) {
                var dot = document.getElementById('timetable-dot');
                if (!dot) return;
                dot.classList.remove('ok', 'err', 'idle', 'warn');
                var label = '';
                switch (status) {
                    case 'ok': dot.classList.add('ok'); label = 'OK'; break;
                    case 'disabled_not_race': dot.classList.add('idle'); label = 'Not Race'; break;
                    case 'not_configured': dot.classList.add('idle'); label = 'Idle'; break;
                    case 'unreachable':
                    case 'http_error':
                    case 'bad_response': dot.classList.add('err'); label = 'Error'; break;
                    default: dot.classList.add('idle'); label = status || '';
                }
                dot.title = 'Timetable sync: ' + (label || status || 'unknown');
            }

            socket.on('timetable_url', function(data) {
                if (data.status) setTimetableDot(data.status);
                if (data.error) console.log('[timetable] ' + data.error);
            });

            socket.on('update', function(data) {
                var statusEl = document.getElementById('ac-status');
                if (!data.ac_connected) {
                    statusEl.className = 'ac-status ac-disconnected';
                    statusEl.innerHTML = buildStatusBar(false, '--');
                    renderClassFilters([]);
                    document.getElementById('drivers-list').innerHTML = '<p class="empty">No telemetry. Start Assetto Corsa with Broadcaster Remote enabled.</p>';
                    return;
                }

                // Resolve focused car number for the header chip
                var focusLabel = '--';
                for (var i = 0; i < data.drivers.length; i++) {
                    if (data.drivers[i].num - 1 === data.current_driver) {
                        focusLabel = '#' + data.drivers[i].car_number;
                        break;
                    }
                }
                statusEl.className = 'ac-status ac-connected';
                statusEl.innerHTML = buildStatusBar(true, focusLabel);

                // Auto-director button state
                var autoBtn = document.getElementById('auto-dir-btn');
                if (autoBtn) autoBtn.classList.toggle('active', !!data.auto_director);

                // Director-debug button state
                var dbgBtn = document.getElementById('auto-dir-dbg-btn');
                if (dbgBtn) dbgBtn.classList.toggle('active', !!data.auto_director_debug);

                // Timetable status dot reflects last poll outcome
                if (data.timetable_status) setTimetableDot(data.timetable_status);

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
                renderClassFilters(data.drivers);
                var visibleDrivers = data.drivers.filter(function(d) {
                    return activeClassFilter === 'ALL' || d.car_class === activeClassFilter;
                });
                if (visibleDrivers.length > 0) {
                    var grid = '<div class="drivers-grid">';
                    visibleDrivers.forEach(function(d) {
                        if (d.colliding) {
                            collisionTimers[d.num] = Date.now();
                            console.log('[collision] socket reports colliding: num=' + d.num + ' name=' + d.name);
                        }
                        var isColliding = collisionTimers[d.num] && (Date.now() - collisionTimers[d.num] < 1000);
                        var isRolledOver = !!d.rolled_over;
                        var isSelected = d.num - 1 === data.current_driver;
                        var isOffline = d.status === 'OFFLINE';
                        var isPit = d.status === 'PIT';
                        var isDisabled = isSelected || isOffline;
                        var hasClass = d.car_class && d.car_class !== 'Unclassed';

                        var cls = 'driver-item';
                        if (isColliding) cls += ' colliding';
                        if (isRolledOver) cls += ' rolled-over';
                        if (isSelected) cls += ' selected';
                        if (isDisabled) cls += ' disabled';
                        if (isOffline) cls += ' offline';

                        grid += '<div class="' + cls + '" data-num="' + d.num + '" data-class="' + escapeHtml(d.car_class || '') + '">';
                        var stripeStyle = hasClass ? ' style="background:' + escapeHtml(d.class_color) + '"' : '';
                        grid += '<span class="class-stripe"' + stripeStyle + '></span>';

                        grid += '<div class="driver-info">';
                        grid += '<span class="pos">' + d.position + '</span>';
                        grid += '<span class="num">#' + escapeHtml(d.car_number) + '</span>';
                        grid += '<span class="name-stack">';
                        grid += '<span class="name">' + escapeHtml(d.name) + '</span>';
                        if (d.team_name) {
                            grid += '<span class="team-name">' + escapeHtml(d.team_name) + '</span>';
                        }
                        grid += '</span>';
                        grid += '<span class="right">';
                        if (isSelected) {
                            grid += '<span class="on-air-chip">On Air</span>';
                        } else if (isOffline) {
                            grid += '<span class="status-chip offline">Off</span>';
                        } else if (isPit) {
                            grid += '<span class="status-chip pit">Pit</span>';
                        }
                        if (isRolledOver) {
                            grid += '<span class="status-chip rollover-chip">Flip</span>';
                        }
                        if (hasClass) {
                            grid += '<span class="class-badge" style="background-color:' + escapeHtml(d.class_color) + '">' + escapeHtml(d.car_class) + '</span>';
                        }
                        grid += '</span>';
                        grid += '</div>';

                        grid += '<div class="driver-gaps">';
                        grid += '<span class="cell"><span class="label">Gap</span><span class="value">' + escapeHtml(d.gap) + '</span></span>';
                        var intCls = '';
                        var m = d.interval.match(/^\\+(\\d+\\.?\\d*)s$/);
                        if (m) { var v = parseFloat(m[1]); if (v < 0.4) intCls = ' tight'; else if (v < 1.0) intCls = ' warn'; }
                        grid += '<span class="cell"><span class="label">Int</span><span class="value' + intCls + '">' + escapeHtml(d.interval) + '</span></span>';
                        if (hasClass && d.class_interval && d.class_interval !== '-') {
                            var clsIntCls = '';
                            var mc = d.class_interval.match(/^\\+(\\d+\\.?\\d*)s$/);
                            if (mc) { var vc = parseFloat(mc[1]); if (vc < 0.4) clsIntCls = ' tight'; else if (vc < 1.0) clsIntCls = ' warn'; }
                            grid += '<span class="cell"><span class="label">Cls</span><span class="value' + clsIntCls + '">' + escapeHtml(d.class_interval) + '</span></span>';
                        }
                        if (hasClass) {
                            grid += '<span class="class-pos">P' + d.class_position + ' &middot; ' + escapeHtml(d.car_class) + '</span>';
                        }
                        grid += '</div>';
                        grid += '</div>';
                    });
                    grid += '</div>';
                    driversList.innerHTML = grid;
                } else {
                    driversList.innerHTML = '<p class="empty">No drivers in selected class.</p>';
                }
            });
        </script>
    </head>
    <body>
        <div id="ac-status" class="ac-status ac-disconnected">
            <div class="status-left">
                <span class="indicator"></span>
                <span class="label-link">No Link</span>
                <span>Connecting</span>
            </div>
            <div class="status-right">
                <span>Focus</span>
                <span class="focus-chip">--</span>
            </div>
        </div>

        <div class="camera-buttons">
            <button data-cam="1">Track<span class="cam-cap">01</span></button>
            <button data-cam="2">Cockpit<span class="cam-cap">02</span></button>
            <button data-cam="3">Heli<span class="cam-cap">03</span></button>
            <button data-cam="4">F6<span class="cam-cap">04</span></button>
            <button data-cam="5">Orbit<span class="cam-cap">05</span></button>
            <button id="auto-dir-btn" onclick="socket.emit('toggle_director')">Auto<span class="cam-cap">AI</span></button>
            <button id="auto-dir-dbg-btn" onclick="socket.emit('toggle_director_debug')" title="Verbose director scoring logs to server stdout">Dbg<span class="cam-cap">DG</span></button>
        </div>
        <div id="timetable-bar" class="timetable-bar">
            <span id="timetable-dot" class="tt-dot idle" title="Timetable sync: idle"></span>
        </div>
        <div id="subcam-bar" class="subcam-bar" style="display:none"></div>
        <div id="class-filter-bar" class="class-filter-bar"></div>

        <div id="drivers-list">
            <p class="empty">Connecting</p>
        </div>

    </body>
    </html>
    '''

    return render_template_string(html)

if __name__ == '__main__':
    threading.Thread(target=monitor_telemetry, daemon=True).start()
    threading.Thread(target=timetable_poll_loop, daemon=True).start()
    socketio.run(app, host='0.0.0.0')
