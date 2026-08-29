"""Versioned WebSocket IPC between the CSP Lua app and the web server."""

import json
import threading
import time
import uuid


PROTOCOL_VERSION = 1
MAX_CARS = 128

REPLAY_NONE = 0
REPLAY_ENTER = 1
REPLAY_LIVE = 2
REPLAY_SEEK_FRAME = 3


class ProtocolError(ValueError):
    """Raised when a WebSocket message does not match the IPC contract."""


def _required(message, name):
    if name not in message:
        raise ProtocolError('missing field: {}'.format(name))
    return message[name]


def _integer(message, name):
    value = _required(message, name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError('{} must be an integer'.format(name))
    return value


def _number(message, name):
    value = _required(message, name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError('{} must be a number'.format(name))
    return float(value)


def _boolean(message, name):
    value = _required(message, name)
    if not isinstance(value, bool):
        raise ProtocolError('{} must be a boolean'.format(name))
    return value


def _string(message, name):
    value = _required(message, name)
    if not isinstance(value, str):
        raise ProtocolError('{} must be a string'.format(name))
    return value


class _FrozenSnapshot(object):
    __slots__ = ('_frozen',)

    def __setattr__(self, name, value):
        if getattr(self, '_frozen', False):
            raise AttributeError('{} is immutable'.format(
                type(self).__name__))
        object.__setattr__(self, name, value)

    def _freeze(self):
        object.__setattr__(self, '_frozen', True)
        return self


class CarSnapshot(_FrozenSnapshot):
    __slots__ = (
        'car_id', 'session_id', 'position', 'normalized_spline_pos',
        'speed_kmh', 'lap_time', 'best_lap', 'last_lap', 'lap_count',
        'is_in_pit', 'is_connected', 'is_colliding', 'is_rolled_over',
        'driver_name', 'team_name',
    )

    @classmethod
    def from_message(cls, message):
        if not isinstance(message, dict):
            raise ProtocolError('car must be an object')
        result = cls()
        for name in ('car_id', 'session_id', 'position', 'lap_time',
                     'best_lap', 'last_lap', 'lap_count'):
            setattr(result, name, _integer(message, name))
        for name in ('normalized_spline_pos', 'speed_kmh'):
            setattr(result, name, _number(message, name))
        for name in ('is_in_pit', 'is_connected', 'is_colliding',
                     'is_rolled_over'):
            setattr(result, name, _boolean(message, name))
        result.driver_name = _string(message, 'driver_name')
        result.team_name = _string(message, 'team_name')
        return result._freeze()


class TelemetrySnapshot(_FrozenSnapshot):
    __slots__ = (
        'packet_id', 'car_count', 'focused_car', 'current_camera',
        'car_cameras_count', 'current_car_camera', 'track_length',
        'session_type', 'session_index', 'session_type_raw', 'session_gen',
        'session_name', 'is_replay', 'replay_frame', 'replay_frames',
        'replay_frame_ms', 'replay_last_result', 'is_replay_only',
        'replay_file', 'replay_temp_dir', 'timetable_url', 'cars',
    )

    @classmethod
    def from_message(cls, message):
        if not isinstance(message, dict):
            raise ProtocolError('message must be an object')
        if _integer(message, 'version') != PROTOCOL_VERSION:
            raise ProtocolError('unsupported protocol version')
        if _string(message, 'type') != 'telemetry':
            raise ProtocolError('expected telemetry message')

        cars_message = _required(message, 'cars')
        if not isinstance(cars_message, list):
            raise ProtocolError('cars must be an array')
        if len(cars_message) > MAX_CARS:
            raise ProtocolError('cars exceeds maximum of {}'.format(MAX_CARS))

        result = cls()
        for name in (
                'packet_id', 'car_count', 'focused_car', 'current_camera',
                'car_cameras_count', 'current_car_camera', 'session_type',
                'session_index', 'session_type_raw', 'session_gen',
                'replay_frame', 'replay_frames', 'replay_last_result'):
            setattr(result, name, _integer(message, name))
        for name in ('track_length', 'replay_frame_ms'):
            setattr(result, name, _number(message, name))
        for name in ('is_replay', 'is_replay_only'):
            setattr(result, name, _boolean(message, name))
        for name in ('session_name', 'replay_file', 'replay_temp_dir',
                     'timetable_url'):
            setattr(result, name, _string(message, name))

        if result.car_count != len(cars_message):
            raise ProtocolError('car_count does not match cars array')
        result.cars = tuple(CarSnapshot.from_message(car)
                            for car in cars_message)
        return result._freeze()


class ACIPCTransport(object):
    """Owns the active AC socket, latest telemetry and command counters."""

    def __init__(self, clock=None, stale_after=2.0, connection_id=None):
        self._clock = clock or time.monotonic
        self._stale_after = float(stale_after)
        self._lock = threading.RLock()
        self._socket = None
        self._latest = None
        self._last_received = None
        self._command_seq = 0
        self._replay_seq = 0
        self._generation = 0
        self._connection_id = connection_id or uuid.uuid4().hex
        if not isinstance(self._connection_id, str) or not self._connection_id:
            raise ValueError('connection_id must be a non-empty string')

    def attach(self, socket):
        with self._lock:
            old_socket = self._socket
            self._socket = socket
            self._generation += 1
            self._latest = None
            self._last_received = None
        if old_socket is not None and old_socket is not socket:
            try:
                old_socket.close()
            except Exception:
                pass

    def detach(self, socket):
        with self._lock:
            if self._socket is not socket:
                return False
            self._socket = None
            self._latest = None
            self._last_received = None
            return True

    def is_current(self, socket):
        with self._lock:
            return self._socket is socket

    def has_socket(self):
        with self._lock:
            return self._socket is not None

    def connection_generation(self):
        with self._lock:
            return self._generation

    def ingest(self, raw, source=None):
        if isinstance(raw, bytes):
            try:
                raw = raw.decode('utf-8')
            except UnicodeDecodeError as exc:
                raise ProtocolError('message is not valid UTF-8') from exc
        if not isinstance(raw, str):
            raise ProtocolError('message must be text')
        try:
            message = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ProtocolError('message is not valid JSON') from exc
        snapshot = TelemetrySnapshot.from_message(message)

        with self._lock:
            if source is not None and source is not self._socket:
                return False
            if (self._latest is not None and
                    snapshot.packet_id == self._latest.packet_id):
                return False
            self._latest = snapshot
            self._last_received = self._clock()
            return True

    def _fresh_locked(self):
        return (self._socket is not None and self._latest is not None and
                self._last_received is not None and
                self._clock() - self._last_received <= self._stale_after)

    def latest(self):
        with self._lock:
            return self._latest if self._fresh_locked() else None

    def latest_with_generation(self):
        """Return one coherent view of telemetry and socket generation."""
        with self._lock:
            snapshot = self._latest if self._fresh_locked() else None
            return snapshot, self._generation

    def is_connected(self):
        with self._lock:
            return self._fresh_locked()

    def _send(self, message):
        payload = json.dumps(message, separators=(',', ':'), ensure_ascii=False)
        with self._lock:
            socket = self._socket
            if socket is None:
                return False
            try:
                socket.send(payload)
                return True
            except Exception:
                if self._socket is socket:
                    self._socket = None
                    self._latest = None
                    self._last_received = None
                try:
                    socket.close()
                except Exception:
                    pass
                return False

    def send_command(self, target_driver, target_camera,
                     target_car_camera=-1):
        with self._lock:
            self._command_seq += 1
            return self._send({
                'version': PROTOCOL_VERSION,
                'connection_id': self._connection_id,
                'type': 'command',
                'command_seq': self._command_seq,
                'target_driver': int(target_driver),
                'target_camera': int(target_camera),
                'target_car_camera': int(target_car_camera),
            })

    def send_replay_command(self, action, rewind_s=0.0, frame=0,
                            driver=None, camera=None, target_car_camera=-1):
        with self._lock:
            self._replay_seq += 1
            return self._send({
                'version': PROTOCOL_VERSION,
                'connection_id': self._connection_id,
                'type': 'replay',
                'replay_seq': self._replay_seq,
                'replay_action': int(action),
                'replay_rewind_s': float(rewind_s),
                'replay_frame': int(frame),
                'target_driver': -1 if driver is None else int(driver),
                'target_camera': -1 if camera is None else int(camera),
                'target_car_camera': int(target_car_camera),
            })
