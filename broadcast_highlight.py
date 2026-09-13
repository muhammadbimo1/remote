"""Broadcast-highlight relay client.

Keeps remote CarID resolution separate from the HTTP transport so the
telemetry loop never blocks on the timing relay.
"""
import json
import os
import threading
from urllib.parse import urlparse

import requests


SUCCESS = 'success'
RETRY = 'retry'
TERMINAL = 'terminal'
AUTH_ERROR = 'auth_error'

_UNSET = object()
_RETRY_DELAYS = (0.5, 1.0, 2.0, 5.0)


class HighlightConfigError(ValueError):
    pass


def load_highlight_config(path):
    """Load broadcastHighlight from a local JSON file.

    A missing file or section means the optional integration is disabled.
    An explicitly configured but incomplete section is an operator error.
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r') as stream:
            root = json.load(stream)
    except (IOError, ValueError) as exc:
        raise HighlightConfigError('cannot read remote_config.json: {}'.format(exc))

    section = root.get('broadcastHighlight') if isinstance(root, dict) else None
    if section is None:
        return None
    if not isinstance(section, dict):
        raise HighlightConfigError('broadcastHighlight must be a JSON object')
    if section.get('enabled') is False:
        return None

    url = (section.get('url') or '').strip()
    username = (section.get('username') or 'lua-app').strip()
    password = section.get('password') or ''
    parsed = urlparse(url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
            parsed.path != '/api/broadcast-highlight'):
        raise HighlightConfigError(
            'broadcastHighlight.url must end with /api/broadcast-highlight')
    if not username or not password:
        raise HighlightConfigError(
            'broadcastHighlight.username and password must be non-empty')
    return {'url': url, 'username': username, 'password': password}


def focused_remote_car_id(telem):
    """Resolve CSP's focused local car index to the relay/timetable CarID."""
    if telem is None:
        return None
    focused_local_id = int(telem.focused_car)
    if focused_local_id < 0:
        return None
    for index in range(int(telem.car_count)):
        car = telem.cars[index]
        if (int(car.car_id) == focused_local_id and car.is_connected and
                int(car.session_id) >= 0):
            return int(car.session_id)
    return None


class BroadcastHighlightClient(object):
    """Coalescing background publisher for the relay highlight endpoint."""

    def __init__(self, config, post=None):
        self.config = config
        self._post = post or requests.post
        self._condition = threading.Condition()
        self._desired_car_id = _UNSET
        self._desired_show_tower = _UNSET
        self._sent_car_id = _UNSET
        self._sent_show_tower = _UNSET
        self._version = 0
        self._thread = None
        self._disabled = False

    def publish(self, remote_car_id=_UNSET, show_tower=_UNSET):
        """Queue changed broadcast controls without blocking telemetry."""
        if remote_car_id is _UNSET and show_tower is _UNSET:
            raise ValueError('remote_car_id or show_tower is required')
        with self._condition:
            if self._disabled:
                return False
            changed = False
            if (remote_car_id is not _UNSET and
                    (self._desired_car_id is _UNSET or
                     self._desired_car_id != remote_car_id)):
                self._desired_car_id = remote_car_id
                changed = True
            if (show_tower is not _UNSET and
                    (self._desired_show_tower is _UNSET or
                     self._desired_show_tower != show_tower)):
                self._desired_show_tower = bool(show_tower)
                changed = True
            if not changed:
                return False
            self._version += 1
            self._condition.notify()
            return True

    def send_once(self, remote_car_id=_UNSET, show_tower=_UNSET):
        """Send one request and classify it according to the API contract."""
        payload = {}
        if remote_car_id is not _UNSET:
            payload['carId'] = remote_car_id
        if show_tower is not _UNSET:
            payload['showTower'] = bool(show_tower)
        if not payload:
            raise ValueError('remote_car_id or show_tower is required')
        try:
            response = self._post(
                self.config['url'],
                json=payload,
                auth=(self.config['username'], self.config['password']),
                timeout=2.0,
                allow_redirects=False)
        except requests.RequestException:
            return RETRY
        if response.status_code == 200:
            return SUCCESS
        if response.status_code == 401:
            return AUTH_ERROR
        if response.status_code >= 500:
            return RETRY
        return TERMINAL

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        handled_version = 0
        while True:
            with self._condition:
                while self._version == handled_version:
                    self._condition.wait()
                version = self._version
                remote_car_id = (self._desired_car_id
                                 if self._desired_car_id != self._sent_car_id
                                 else _UNSET)
                show_tower = (self._desired_show_tower
                              if self._desired_show_tower != self._sent_show_tower
                              else _UNSET)

            if remote_car_id is _UNSET and show_tower is _UNSET:
                handled_version = version
                continue

            retry_index = 0
            while True:
                outcome = self.send_once(remote_car_id, show_tower)
                if outcome == AUTH_ERROR:
                    with self._condition:
                        self._disabled = True
                    print('[remote_web] broadcast highlight authentication failed; '
                          'publisher disabled')
                    return
                if outcome != RETRY:
                    if outcome == TERMINAL:
                        print('[remote_web] broadcast control update rejected')
                    with self._condition:
                        if remote_car_id is not _UNSET:
                            self._sent_car_id = remote_car_id
                        if show_tower is not _UNSET:
                            self._sent_show_tower = show_tower
                    handled_version = version
                    break

                delay = _RETRY_DELAYS[min(retry_index, len(_RETRY_DELAYS) - 1)]
                retry_index += 1
                with self._condition:
                    if self._version != version:
                        handled_version = version
                        break
                    self._condition.wait(delay)
                    if self._version != version:
                        handled_version = version
                        break
