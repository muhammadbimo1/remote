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
        self._desired = _UNSET
        self._version = 0
        self._thread = None
        self._disabled = False

    def publish(self, remote_car_id):
        """Queue a changed target without blocking the telemetry thread."""
        with self._condition:
            if self._disabled:
                return False
            if self._desired is not _UNSET and self._desired == remote_car_id:
                return False
            self._desired = remote_car_id
            self._version += 1
            self._condition.notify()
            return True

    def send_once(self, remote_car_id):
        """Send one request and classify it according to the API contract."""
        try:
            response = self._post(
                self.config['url'],
                json={'carId': remote_car_id},
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
                target = self._desired

            retry_index = 0
            while True:
                outcome = self.send_once(target)
                if outcome == AUTH_ERROR:
                    with self._condition:
                        self._disabled = True
                    print('[remote_web] broadcast highlight authentication failed; '
                          'publisher disabled')
                    return
                if outcome != RETRY:
                    if outcome == TERMINAL:
                        print('[remote_web] broadcast highlight rejected for CarID {}'.format(
                            target))
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
