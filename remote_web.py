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
from ipc_shared import REPLAY_ENTER, REPLAY_LIVE, REPLAY_SEEK_FRAME
from auto_director import AutoDirector
from event_log import EventLog
from event_journal import EventJournal

app = Flask(__name__)
socketio = SocketIO(app)

# mmap handles (opened lazily by monitor thread)
telemetry_mmap = None
telemetry_page = None
command_mmap = None
command_page = None
command_lock = threading.Lock()
command_seq_counter = 0
replay_seq_counter = 0

ac_connected = False

director = AutoDirector()
director_lock = threading.Lock()

event_log = EventLog()

# Permanent record, kept next to the server. Unlike event_log this is never
# pruned — it exists to annotate the replay saved at the end of the session,
# by which time the live buffer has long rolled over.
EVENT_JOURNAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 'event_logs')
event_journal = EventJournal(EVENT_JOURNAL_DIR)

# Telemetry context for the journal, refreshed on every live tick so the MARK
# handler can stamp a record without reading the mmap itself.
_latest_replay_context = {}
_latest_cars_by_id = {}

# Identity of the AC session currently being journalled. None until the first
# telemetry read; every change rotates the journal and empties the live log.
_current_session = None

SESSION_TYPE_NAMES = {
    1: 'Practice', 2: 'Qualify', 3: 'Race',
    4: 'Hotlap', 5: 'Time Attack', 6: 'Drift', 7: 'Drag',
}

# Letter AC puts in the replay filename. Everything that is not a race or a
# qualifying session lands in the same "other" bucket.
SESSION_TYPE_LETTERS = {2: 'Q', 3: 'R'}


def session_identity(telem):
    """Tuple that changes whenever AC starts or restarts a session."""
    return (telem.session_gen, telem.session_index, telem.session_type_raw)


def session_label(telem):
    """Human name for the session, for filenames and journal records."""
    name = (getattr(telem, 'session_name', '') or '').strip()
    if not name:
        name = SESSION_TYPE_NAMES.get(telem.session_type_raw, 'Session')
    if telem.session_index > 0:
        name = '{} {}'.format(name, telem.session_index + 1)
    return name


def maybe_rotate_session(telem):
    """Start a new journal (and empty the live log) when the session changes.

    AC wipes the instant-replay buffer on a session start, so markers from the
    previous session point into a recording that no longer exists — they must
    not be jumpable, and they must not share a file with the new session's.
    Returns the label if a rotation happened, else None.
    """
    global _current_session, _last_live_payload, _latest_cars_by_id

    identity = session_identity(telem)
    if identity == _current_session:
        return None

    first = _current_session is None
    _current_session = identity
    label = session_label(telem)

    # AC's recorded length is how long this session has been running, which is
    # what pairs the journal with the right .acreplay even when the server was
    # started mid-session.
    recorded_s = telem.replay_frames * telem.replay_frame_ms / 1000.0
    started_at = time.time() - (recorded_s if recorded_s > 0 else 0.0)

    event_log.clear()
    event_log.window_s = EventLog.BUFFER_S
    event_journal.start_session(
        label,
        replay_dir=(getattr(telem, 'replay_temp_dir', '') or '').strip(),
        started_at=started_at,
        letter=SESSION_TYPE_LETTERS.get(telem.session_type_raw, 'O'))
    _last_live_payload = None
    _latest_cars_by_id = {}
    # Lap-count corrections belong to the session that produced them.
    with _resync_lock:
        progress_offsets.clear()

    print('[remote_web] session {}: {} (index={} type={} gen={})'.format(
        'opened' if first else 'changed', label, telem.session_index,
        telem.session_type_raw, telem.session_gen))
    return label

# Seconds of lead-in ahead of an event when jumping back to it: contact is
# only readable with the approach in front of it.
REPLAY_PREROLL_S = 3.0
# AC clamps very short rewinds oddly; keep a floor.
REPLAY_MIN_REWIND_S = 5.0

# Last payload built from live telemetry. While replay is active the driver
# list is served from here instead of from replay-time car data — positions and
# gaps read out of a replay frame are not the race state the operator needs.
_last_live_payload = None

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


def _parse_team_name(team_name):
    """Strip the "CLASS 42 | " prefix, leaving just the team.

    The class and the race number are already rendered as their own badge and
    number on the card, so repeating them in the team line only eats width.
    Names without a pipe are passed through unchanged.
    """
    if not team_name or '|' not in team_name:
        return team_name
    return team_name.split('|', 1)[1].strip()


def _parse_driver_name(raw_name):
    """Parse the "NN | Driver Name" mmap format. Returns (car_number, display_name).
    The name prefix is the only source of truth for the race number: car_number is
    None when the prefix is missing or non-numeric, never the car slot index."""
    parts = raw_name.split('|', 1)
    if len(parts) == 2:
        try:
            car_number = int(parts[0].strip())
        except ValueError:
            car_number = None
        display_name = parts[1].strip()
    else:
        car_number = None
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
        car_number, display_name = _parse_driver_name(raw_name)

        cls_name, cls_color = get_car_class(team_name)

        cars.append({
            'car_id': c.car_id,
            'car_number': car_number,
            'display_name': display_name,
            'team_name': _parse_team_name(team_name),
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
    if telem.is_replay:
        # lap_count here comes from the replay frame on screen, so the
        # server-vs-local diff would be nonsense and would stick in
        # progress_offsets long after the operator returns to live.
        last_poll_status = 'disabled_replay'
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
            'car_number': c.get('car_number'),
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
        'replay': {
            'active': bool(telem.is_replay),
            'frame': telem.replay_frame,
            'frames': telem.replay_frames,
            'frame_ms': telem.replay_frame_ms,
            'last_result': telem.replay_last_result,
        },
        'events': event_log.snapshot(),
    }


def journal_context(car_id):
    """Telemetry + standings context for one journalled event."""
    context = dict(_latest_replay_context)
    car = _latest_cars_by_id.get(car_id)
    if car:
        context['team'] = car.get('team_name')
        context['position'] = car.get('position')
        context['class_position'] = car.get('class_position')
        context['car_class'] = car.get('car_class')
        context['lap'] = car.get('lap_count')
    return context


def record_event(event):
    """Append one event to the on-disk journal."""
    if event is None:
        return
    first = event_journal.path is None
    event_journal.append(event, journal_context(event.get('car_id')))
    if first and event_journal.path:
        print('[remote_web] event journal: {} (replay: {})'.format(
            event_journal.path, event_journal.replay_file or 'unmatched'))


def build_replay_update_data(telem):
    """Payload for a tick where replay is active.

    The driver list is frozen at the last live snapshot; everything the
    operator can still act on (camera state, focus, replay position, the event
    log) is current.
    """
    if _last_live_payload is None:
        data = {'ac_connected': True, 'drivers': []}
    else:
        data = dict(_last_live_payload)

    data['current_driver'] = telem.focused_car
    data['current_camera'] = telem.current_camera
    data['car_cameras_count'] = telem.car_cameras_count
    data['current_car_camera'] = telem.current_car_camera
    data['auto_director'] = director.enabled
    data['auto_director_debug'] = director.debug
    data['timetable_status'] = last_poll_status
    data['replay'] = {
        'active': True,
        'frame': telem.replay_frame,
        'frames': telem.replay_frames,
        'frame_ms': telem.replay_frame_ms,
        'last_result': telem.replay_last_result,
    }
    data['events'] = event_log.snapshot()
    return data


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


def send_replay_command(action, rewind_s=0.0, frame=0, driver=None, camera=None):
    """Write a replay command to shared memory.

    `driver`/`camera` are written but command_seq is deliberately NOT bumped:
    entering instant replay resets the camera, so Lua parks the shot and applies
    it once replay is live. Bumping command_seq here would make Lua apply it
    immediately, where the replay toggle clobbers it.
    """
    global command_mmap, command_page, replay_seq_counter
    with command_lock:
        if command_mmap is None:
            command_mmap, command_page = open_command_mmap()
        if command_page is None:
            return False
        if driver is not None:
            command_page.target_driver = driver
        if camera is not None:
            command_page.target_camera = camera
            command_page.target_car_camera = -1
        replay_seq_counter += 1
        command_page.replay_action = action
        command_page.replay_rewind_s = float(rewind_s)
        command_page.replay_frame = int(frame)
        command_page.replay_seq = replay_seq_counter
        return True


def monitor_telemetry():
    """Background thread: poll telemetry mmap and push updates via SocketIO."""
    global telemetry_mmap, telemetry_page, ac_connected
    global latest_focused_car, latest_current_camera, latest_current_car_camera
    global _last_live_payload, _latest_replay_context, _latest_cars_by_id
    global _current_session

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
            # Same reasoning for the event log, and its timestamps point into a
            # replay buffer that no longer exists. Logged because from the
            # panel this looks identical to events ageing out of the window.
            dropped = len(event_log.snapshot())
            # Forget the session so the next tick rotates the journal with a
            # fresh label rather than appending a new AC run to the old file.
            _current_session = None
            print('[remote_web] telemetry mmap (re)opened '
                  '({} events pending drop)'.format(dropped))

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
            maybe_rotate_session(telem)
            latest_focused_car = telem.focused_car
            latest_current_camera = telem.current_camera
            latest_current_car_camera = telem.current_car_camera

            if telem.is_replay:
                # Car data now comes from the replay frame being shown, so it
                # is not race state: no event detection, no director cuts, and
                # the driver list is served from the last live payload.
                data = build_replay_update_data(telem)
            else:
                # Retention follows AC's actual recorded replay length rather
                # than a guessed constant: an event is only worth keeping while
                # it is still inside the buffer we could rewind into.
                recorded_s = telem.replay_frames * telem.replay_frame_ms / 1000.0
                if recorded_s > 0:
                    before = event_log.window_s
                    after = event_log.set_window(recorded_s)
                    # Only worth a line when it moves materially — this is the
                    # readout that tells you whether AC reports its buffer at
                    # all while live, or whether the fallback is in charge.
                    if abs(after - before) > 30:
                        print('[remote_web] event window now {:.0f}s '
                              '(AC reports {:.0f}s recorded)'.format(after, recorded_s))

                cars_with_gaps = compute_gaps(telem)
                _latest_replay_context = {
                    'session': session_label(telem),
                    'session_index': telem.session_index,
                    'session_s': round(recorded_s, 2) if recorded_s > 0 else None,
                    'replay_frame': telem.replay_frames or None,
                    'replay_frame_ms': telem.replay_frame_ms or None,
                }
                _latest_cars_by_id = {c['car_id']: c for c in cars_with_gaps}

                # Detect before building: build_update_data snapshots the event
                # log, so observing afterwards would hold every event back a
                # tick — and strand the last one if telemetry stops.
                for event in event_log.observe(cars_with_gaps):
                    record_event(event)
                data = build_update_data(telem, cars_with_gaps)
                _last_live_payload = data

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


@socketio.on('jump_to_event')
def handle_jump_to_event(data):
    """Rewind instant replay to just before a logged event, focused on its car."""
    try:
        event_id = int((data or {}).get('id'))
    except (TypeError, ValueError):
        emit('replay_status', {'ok': False, 'error': 'bad event id'})
        return

    event = event_log.get(event_id)
    if event is None:
        emit('replay_status', {'ok': False, 'error': 'event expired'})
        return

    rewind = time.time() - event['t'] + REPLAY_PREROLL_S
    rewind = max(REPLAY_MIN_REWIND_S, min(rewind, event_log.window_s))

    # A jump is a manual cut: the director must not fight it on the way back.
    with director_lock:
        director.enabled = False
        ok = send_replay_command(REPLAY_ENTER, rewind_s=rewind,
                                 driver=event['car_id'], camera=1)

    print('[remote_web] replay jump: event={} kind={} car={} rewind={:.1f}s'.format(
        event_id, event['kind'], event['car_id'], rewind))
    emit('replay_status', {'ok': ok, 'rewind': rewind, 'event_id': event_id},
         broadcast=True)


@socketio.on('go_live')
def handle_go_live():
    ok = send_replay_command(REPLAY_LIVE)
    print('[remote_web] replay live requested (written={})'.format(ok))
    emit('replay_status', {'ok': ok, 'live': True}, broadcast=True)


@socketio.on('mark_event')
def handle_mark_event():
    """Drop a marker on the focused car with no detection involved."""
    name = ''
    number = None
    if _last_live_payload:
        for d in _last_live_payload.get('drivers', []):
            if d['num'] - 1 == latest_focused_car:
                name = d.get('name', '')
                number = d.get('car_number')
                break
    event = event_log.mark(latest_focused_car, name=name, car_number=number)
    record_event(event)
    emit('events', {'events': event_log.snapshot()}, broadcast=True)
    if event:
        print('[remote_web] mark on car {} ({})'.format(latest_focused_car, name))


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
        # A client joining mid-replay gets the frozen list, same as everyone
        # else — gaps built from a replay frame are not race state.
        if telem.is_replay:
            emit('update', build_replay_update_data(telem))
        else:
            emit('update', build_update_data(telem))
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
    <html data-ac-gas="amber">
    <head>
        <title>Broadcaster Remote</title>
        <meta name=viewport content="width=device-width, initial-scale=1">
        <!-- All assets are vendored under static/ so the panel works on a rig
             with no internet. amber-console resolves its webfonts as ../fonts/,
             which is why dist/ and fonts/ must stay siblings. -->
        <link rel="stylesheet" href="/static/amber-console/dist/amber-console.min.css">
        <link rel="stylesheet" href="/static/remote.css">
        <script src="/static/socket.io.min.js"></script>
        <script>
            const socket = io();
            var collisionTimers = {};
            var activeClassFilter = 'ALL';
            var ttStatus = 'idle';

            // Class name -> ramp step 1-4 (a .cls-N class, see remote.css).
            // Amber Console is single-hue, so a class is told apart by its badge
            // text; the ramp step is only a scanning aid and wraps past four.
            var classRank = {};

            function classRankClass(cls) {
                var r = classRank[cls];
                return r ? ' cls-' + r : '';
            }

            // Debug helpers — call from DevTools console
            window.debugCollision = function() {
                console.log('[collision debug]');
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

            function setPressed(el, on) {
                if (el) el.setAttribute('aria-pressed', on ? 'true' : 'false');
            }

            // Race number comes only from the "NN | Name" prefix; there is no
            // slot-index fallback, so an unnumbered entry renders as '--'.
            function formatCarNumber(carNumber) {
                return (carNumber === null || carNumber === undefined) ? '--' : '#' + carNumber;
            }

            // Timetable sync had a colour-coded dot. With one hue available the
            // state has to be spelled out, so it becomes a badge; an error is the
            // only alarm here and gets inverse video + blink.
            function ttBadgeHtml() {
                var label, cls = 'ac-badge';
                switch (ttStatus) {
                    case 'ok': label = 'TT:OK'; break;
                    case 'disabled_not_race': label = 'TT:IDLE'; cls += ' ac-badge--dim'; break;
                    case 'not_configured': label = 'TT:OFF'; cls += ' ac-badge--dim'; break;
                    case 'unreachable':
                    case 'http_error':
                    case 'bad_response': label = 'TT:ERR'; cls += ' ac-badge--filled ac-blink'; break;
                    default: label = 'TT:' + String(ttStatus || 'IDLE').toUpperCase(); cls += ' ac-badge--dim';
                }
                return '<span id="timetable-badge" class="' + cls + '" title="Timetable sync: '
                    + escapeHtml(ttStatus) + '">' + escapeHtml(label) + '</span>';
            }

            function setTimetableStatus(status) {
                ttStatus = status || 'idle';
                var el = document.getElementById('timetable-badge');
                if (el) el.outerHTML = ttBadgeHtml();
            }

            // Replay state. The panel never scrubs frames itself — a jump is
            // "rewind N seconds", computed server-side from the event's age.
            var replayActive = false;
            var replayUnavailable = false;
            var eventsCollapsed = false;

            function replayBadgeHtml() {
                // A refused ac.tryToToggleReplay is otherwise invisible: the
                // button would look like it did nothing.
                if (replayUnavailable) {
                    return '<span id="replay-badge" class="ac-badge ac-badge--filled" '
                        + 'title="AC refused the replay toggle">RPL:N/A</span>';
                }
                return '';
            }

            function buildStatusBar(connected, focusLabel) {
                var linkLabel = connected ? 'Link:OK' : 'Link:None';
                var subLabel = connected ? (replayActive ? 'Replay' : 'Live') : 'Waiting';
                // Link down is an alarm: the .ac-disconnected rule in remote.css
                // pairs inverse video with this blink, so the state survives
                // prefers-reduced-motion.
                return '<div class="status-left">'
                    + '<span class="label-link' + (connected ? '' : ' ac-blink') + '">' + linkLabel + '</span>'
                    + '<span class="status-sub">' + subLabel + '</span>'
                    + '</div>'
                    + '<div class="status-right">'
                    + '<span class="ac-readout ac-readout--inline">'
                    + '<span class="ac-readout__label">Focus</span>'
                    + '<span class="ac-readout__value">' + focusLabel + '</span>'
                    + '</span>'
                    + replayBadgeHtml()
                    + ttBadgeHtml()
                    + '</div>';
            }

            // Events are ranked by recency using ramp position only — the
            // palette is single-hue, so freshness is brightness, and the kind
            // is spelled out in the badge.
            function eventAgeClass(age) {
                if (age < 15) return ' ev-fresh';
                if (age < 60) return ' ev-mid';
                return ' ev-old';
            }

            function formatAge(age) {
                var s = Math.max(0, Math.round(age));
                if (s < 60) return s + 's';
                return Math.floor(s / 60) + 'm' + ('0' + (s % 60)).slice(-2);
            }

            function renderEvents(events) {
                var list = document.getElementById('event-list');
                var toggle = document.getElementById('events-toggle');
                events = events || [];
                if (toggle) toggle.textContent = 'Events ' + events.length;

                if (eventsCollapsed) {
                    list.style.display = 'none';
                    return;
                }
                list.style.display = '';

                if (!events.length) {
                    list.innerHTML = '<div class="ac-banner ac-banner--dim">No Events Yet</div>';
                    return;
                }

                var html = '';
                events.forEach(function(ev) {
                    var who = escapeHtml(formatCarNumber(ev.car_number));
                    if (ev.name) who += ' ' + escapeHtml(ev.name);
                    html += '<div class="event-item' + eventAgeClass(ev.age)
                        + '" data-event="' + ev.id + '">'
                        + '<span class="ev-age">-' + formatAge(ev.age) + '</span>'
                        + '<span class="ac-badge ev-kind">' + escapeHtml(ev.label) + '</span>'
                        + '<span class="ev-car">' + who + '</span>'
                        + '</div>';
                });
                list.innerHTML = html;
            }

            function sortedClasses(drivers) {
                var seen = {};
                drivers.forEach(function(d) {
                    if (d.car_class && d.car_class !== 'Unclassed') seen[d.car_class] = true;
                });
                var classes = Object.keys(seen);
                var order = { PRO: 0, HY: 1, AM: 2 };
                classes.sort(function(a, b) {
                    var au = a.toUpperCase();
                    var bu = b.toUpperCase();
                    var ao = Object.prototype.hasOwnProperty.call(order, au) ? order[au] : 100;
                    var bo = Object.prototype.hasOwnProperty.call(order, bu) ? order[bu] : 100;
                    if (ao !== bo) return ao - bo;
                    return a.localeCompare(b);
                });
                return classes;
            }

            function renderClassFilters(drivers) {
                var bar = document.getElementById('class-filter-bar');
                if (!bar) return;

                var classes = sortedClasses(drivers);
                classRank = {};
                classes.forEach(function(cls, i) { classRank[cls] = (i % 4) + 1; });

                if (activeClassFilter !== 'ALL' && classes.indexOf(activeClassFilter) === -1) {
                    activeClassFilter = 'ALL';
                }

                var html = '<button type="button" class="ac-btn ac-btn--sm" data-class="ALL" aria-pressed="'
                    + (activeClassFilter === 'ALL') + '">'
                    + '<span class="class-swatch"></span><span>All</span></button>';
                classes.forEach(function(cls) {
                    html += '<button type="button" class="ac-btn ac-btn--sm' + classRankClass(cls)
                        + '" data-class="' + escapeHtml(cls) + '" aria-pressed="' + (activeClassFilter === cls) + '">'
                        + '<span class="class-swatch"></span>'
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
                            setPressed(b, b.dataset.class === activeClassFilter);
                        });
                        document.querySelectorAll('.driver-item').forEach(function(card) {
                            card.style.display = activeClassFilter === 'ALL' || card.dataset.class === activeClassFilter ? '' : 'none';
                        });
                    }
                });

                // Same delegated pointerdown pattern: the row is rebuilt on
                // every telemetry tick, so the listener lives on the container.
                document.getElementById('event-list').addEventListener('pointerdown', function(e) {
                    var row = e.target.closest('.event-item');
                    if (row) {
                        e.preventDefault();
                        socket.emit('jump_to_event', { id: parseInt(row.dataset.event) });
                    }
                });
                document.getElementById('live-btn').addEventListener('pointerdown', function(e) {
                    e.preventDefault();
                    socket.emit('go_live');
                });
                document.getElementById('mark-btn').addEventListener('pointerdown', function(e) {
                    e.preventDefault();
                    socket.emit('mark_event');
                });
                document.getElementById('events-toggle').addEventListener('pointerdown', function(e) {
                    e.preventDefault();
                    eventsCollapsed = !eventsCollapsed;
                    setPressed(e.currentTarget, !eventsCollapsed);
                    document.getElementById('event-list').style.display = eventsCollapsed ? 'none' : '';
                });

                socket.emit('get_timetable_url');
            });

            socket.on('events', function(data) {
                renderEvents(data.events);
            });

            socket.on('replay_status', function(data) {
                if (data.ok === false && data.error) {
                    console.log('[replay] ' + data.error);
                }
            });

            socket.on('director_status', function(data) {
                setPressed(document.getElementById('auto-dir-btn'), data.enabled);
            });

            socket.on('director_debug_status', function(data) {
                setPressed(document.getElementById('auto-dir-dbg-btn'), data.enabled);
            });

            socket.on('timetable_url', function(data) {
                if (data.status) setTimetableStatus(data.status);
                if (data.error) console.log('[timetable] ' + data.error);
            });

            socket.on('update', function(data) {
                var statusEl = document.getElementById('ac-status');

                // Timetable state is rendered inside the status bar, so it has to
                // be current before the bar is rebuilt.
                if (data.timetable_status) ttStatus = data.timetable_status;

                // Replay state gates the status bar label and the frozen grid,
                // so it also has to be current before anything is rebuilt.
                var replay = data.replay || {};
                replayActive = !!replay.active;
                replayUnavailable = replay.last_result === 2;

                if (!data.ac_connected) {
                    replayActive = false;
                    statusEl.className = 'ac-statusbar ac-statusbar--line ac-disconnected';
                    statusEl.innerHTML = buildStatusBar(false, '--');
                    renderClassFilters([]);
                    renderEvents([]);
                    document.getElementById('drivers-list').innerHTML =
                        '<div class="ac-banner" role="alert">No Telemetry</div>';
                    return;
                }

                // Resolve focused car number for the header chip
                var focusLabel = '--';
                for (var i = 0; i < data.drivers.length; i++) {
                    if (data.drivers[i].num - 1 === data.current_driver) {
                        focusLabel = formatCarNumber(data.drivers[i].car_number);
                        break;
                    }
                }
                statusEl.className = 'ac-statusbar ac-statusbar--line ac-connected';
                statusEl.innerHTML = buildStatusBar(true, focusLabel);

                setPressed(document.getElementById('auto-dir-btn'), !!data.auto_director);
                setPressed(document.getElementById('auto-dir-dbg-btn'), !!data.auto_director_debug);

                // LIVE is the way out of replay, so it is the alarm state here:
                // inverse video + blink while replay holds the output.
                var liveBtn = document.getElementById('live-btn');
                setPressed(liveBtn, replayActive);
                liveBtn.classList.toggle('ac-blink', replayActive);
                var readout = document.getElementById('replay-readout');
                if (replayActive && replay.frames) {
                    readout.style.display = '';
                    document.getElementById('replay-frame').textContent =
                        replay.frame + '/' + replay.frames;
                } else {
                    readout.style.display = 'none';
                }
                renderEvents(data.events);

                // Highlight active camera
                document.querySelectorAll('.camera-buttons button[data-cam]').forEach(function(btn) {
                    var on = parseInt(btn.dataset.cam) === data.current_camera;
                    btn.classList.toggle('ac-tab--active', on);
                    btn.setAttribute('aria-selected', on ? 'true' : 'false');
                });

                // Build subcamera bar for F6 cameras
                var subcamEl = document.getElementById('subcam-bar');
                if (data.car_cameras_count > 0) {
                    var html = '';
                    for (var i = 0; i < data.car_cameras_count; i++) {
                        var on = data.current_camera === 4 && data.current_car_camera === i;
                        html += '<button class="ac-btn ac-btn--sm" data-carcam="' + i + '" aria-pressed="' + on + '">Cam ' + (i + 1) + '</button>';
                    }
                    subcamEl.innerHTML = html;
                    subcamEl.style.display = 'flex';
                } else {
                    subcamEl.innerHTML = '';
                    subcamEl.style.display = 'none';
                }

                var driversList = document.getElementById('drivers-list');
                // Frozen at the last live snapshot while replay runs — dimmed
                // so nobody reads a stale gap as current.
                driversList.classList.toggle('replay-frozen', replayActive);
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
                        if (hasClass) cls += classRankClass(d.car_class);
                        // Collision is the loudest state the palette allows: the
                        // card snaps to inverse video (see remote.css) and blinks.
                        // The inverse fill is what carries it when blink is off.
                        if (isColliding) cls += ' colliding ac-blink';
                        if (isRolledOver) cls += ' rolled-over';
                        if (isSelected) cls += ' selected';
                        if (isDisabled) cls += ' disabled';
                        if (isOffline) cls += ' offline';

                        grid += '<div class="' + cls + '" data-num="' + d.num + '" data-class="' + escapeHtml(d.car_class || '') + '">';
                        grid += '<span class="class-stripe"></span>';

                        grid += '<div class="driver-info">';
                        grid += '<span class="pos">' + d.position + '</span>';
                        grid += '<span class="num">' + escapeHtml(formatCarNumber(d.car_number)) + '</span>';
                        grid += '<span class="name">' + escapeHtml(d.name) + '</span>';
                        grid += '<span class="right">';
                        // No ON AIR badge: the focused car already carries the
                        // widened stripe and the brightened name, and the badge
                        // was only crowding the row.
                        if (isOffline) {
                            grid += '<span class="ac-badge status-badge ac-badge--dim">Off</span>';
                        } else if (isPit) {
                            grid += '<span class="ac-badge status-badge">Pit</span>';
                        }
                        if (isColliding) {
                            grid += '<span class="ac-badge status-badge ac-badge--filled">Hit</span>';
                        }
                        // FLP blinks but leaves the card body alone, which is how
                        // it stays distinguishable from a collision.
                        if (isRolledOver) {
                            grid += '<span class="ac-badge status-badge ac-badge--filled ac-blink">Flp</span>';
                        }
                        if (hasClass) {
                            grid += '<span class="ac-badge class-badge">' + escapeHtml(d.car_class) + '</span>';
                        }
                        grid += '</span>';
                        grid += '</div>';

                        // Team gets its own full-width row rather than sitting
                        // indented under the name — the card is 240px wide and
                        // an indented line loses most of it to the pos/number
                        // column on the left and the badges on the right.
                        if (d.team_name) {
                            grid += '<div class="team-name">' + escapeHtml(d.team_name) + '</div>';
                        }

                        grid += '<div class="driver-gaps">';
                        grid += '<span class="cell"><span class="label">Gap</span><span class="value">' + escapeHtml(d.gap) + '</span></span>';
                        // Urgency is ramp position, not hue: tight is the brightest
                        // value in the row, normal is the dimmest.
                        var intCls = '';
                        var m = d.interval.match(/^\\+(\\d+\\.?\\d*)s$/);
                        if (m) { var v = parseFloat(m[1]); if (v < 0.4) intCls = ' tight'; else if (v < 1.0) intCls = ' warn'; }
                        grid += '<span class="cell"><span class="label">Int</span><span class="value' + intCls + '">' + escapeHtml(d.interval) + '</span></span>';
                        if (hasClass && d.class_interval && d.class_interval !== '-') {
                            var clsIntCls = '';
                            var mc = d.class_interval.match(/^\\+(\\d+\\.?\\d*)s$/);
                            if (mc) { var vc = parseFloat(mc[1]); if (vc < 0.4) clsIntCls = ' tight'; else if (vc < 1.0) clsIntCls = ' warn'; }
                            grid += '<span class="cell cell-cls"><span class="label">Cls</span><span class="value' + clsIntCls + '">' + escapeHtml(d.class_interval) + '</span></span>';
                        }
                        grid += '</div>';
                        grid += '</div>';
                    });
                    grid += '</div>';
                    driversList.innerHTML = grid;
                } else {
                    driversList.innerHTML = '<div class="ac-banner ac-banner--dim">No Drivers In Class</div>';
                }
            });
        </script>
    </head>
    <body>
        <!-- The simulation overlays (.ac-mesh / .ac-retrace / .ac-persist) are
             intentionally absent, and .ac-bloom is off — see remote.css. -->
        <div class="ac-screen" data-ac-screen>
        <div class="ac-screen__body">

        <div id="ac-status" class="ac-statusbar ac-statusbar--line ac-disconnected">
            <div class="status-left">
                <span class="label-link ac-blink">Link:None</span>
                <span class="status-sub">Connecting</span>
            </div>
            <div class="status-right">
                <span class="ac-readout ac-readout--inline">
                    <span class="ac-readout__label">Focus</span>
                    <span class="ac-readout__value">--</span>
                </span>
                <span id="timetable-badge" class="ac-badge ac-badge--dim" title="Timetable sync: idle">TT:IDLE</span>
            </div>
        </div>

        <div class="control-bar">
            <div class="camera-buttons ac-tabs" role="tablist">
                <button class="ac-tab" role="tab" aria-selected="false" data-cam="1">Track<span class="cam-cap">01</span></button>
                <button class="ac-tab" role="tab" aria-selected="false" data-cam="2">Cockpit<span class="cam-cap">02</span></button>
                <button class="ac-tab" role="tab" aria-selected="false" data-cam="3">Heli<span class="cam-cap">03</span></button>
                <button class="ac-tab" role="tab" aria-selected="false" data-cam="4">F6<span class="cam-cap">04</span></button>
                <button class="ac-tab" role="tab" aria-selected="false" data-cam="5">Orbit<span class="cam-cap">05</span></button>
            </div>
            <div class="dir-group">
                <button id="auto-dir-btn" class="ac-btn" aria-pressed="false" onclick="socket.emit('toggle_director')">Auto<span class="cam-cap">AI</span></button>
                <button id="auto-dir-dbg-btn" class="ac-btn" aria-pressed="false" onclick="socket.emit('toggle_director_debug')" title="Verbose director scoring logs to server stdout">Dbg<span class="cam-cap">DG</span></button>
            </div>
        </div>

        <div id="replay-bar" class="replay-bar">
            <button id="live-btn" class="ac-btn" aria-pressed="false">Live<span class="cam-cap">LV</span></button>
            <button id="mark-btn" class="ac-btn" title="Drop a marker on the focused car">Mark<span class="cam-cap">MK</span></button>
            <span id="replay-readout" class="ac-readout ac-readout--inline" style="display:none">
                <span class="ac-readout__label">Frame</span>
                <span class="ac-readout__value" id="replay-frame">--</span>
            </span>
            <button id="events-toggle" class="ac-btn ac-btn--sm" aria-pressed="true">Events 0</button>
        </div>

        <div id="event-list" class="event-list"></div>

        <div id="subcam-bar" class="subcam-bar" style="display:none"></div>
        <div id="class-filter-bar" class="class-filter-bar"></div>

        <div id="drivers-list">
            <div class="ac-banner ac-banner--dim">Connecting</div>
        </div>

        </div>
        </div>
    </body>
    </html>
    '''

    return render_template_string(html)

if __name__ == '__main__':
    threading.Thread(target=monitor_telemetry, daemon=True).start()
    threading.Thread(target=timetable_poll_loop, daemon=True).start()
    socketio.run(app, host='0.0.0.0')
