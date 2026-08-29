import json
import os
import tempfile
import unittest

from ipc_shared import TelemetryPage
from broadcast_highlight import (
    AUTH_ERROR,
    BroadcastHighlightClient,
    HighlightConfigError,
    RETRY,
    SUCCESS,
    TERMINAL,
    focused_remote_car_id,
    load_highlight_config,
)


class HighlightConfigTest(unittest.TestCase):
    def test_loads_json_config_and_defaults_username(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'remote_config.json')
            with open(path, 'w') as stream:
                json.dump({
                    'broadcastHighlight': {
                        'url': 'https://timing.example/api/broadcast-highlight',
                        'password': 'secret',
                    },
                }, stream)

            config = load_highlight_config(path)

        self.assertEqual(config['url'],
                         'https://timing.example/api/broadcast-highlight')
        self.assertEqual(config['username'], 'lua-app')
        self.assertEqual(config['password'], 'secret')

    def test_missing_section_disables_integration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'remote_config.json')
            with open(path, 'w') as stream:
                json.dump({}, stream)

            self.assertIsNone(load_highlight_config(path))

    def test_incomplete_section_is_a_configuration_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'remote_config.json')
            with open(path, 'w') as stream:
                json.dump({
                    'broadcastHighlight': {
                        'url': 'https://timing.example/api/broadcast-highlight',
                        'password': '',
                    },
                }, stream)

            with self.assertRaises(HighlightConfigError):
                load_highlight_config(path)


class FocusedRemoteCarIdTest(unittest.TestCase):
    def test_maps_focused_local_car_to_connected_session_id(self):
        telem = TelemetryPage()
        telem.car_count = 3
        telem.focused_car = 2

        stale = telem.cars[1]
        stale.car_id = 2
        stale.session_id = 2
        stale.is_connected = 0

        focused = telem.cars[2]
        focused.car_id = 2
        focused.session_id = 18
        focused.is_connected = 1

        self.assertEqual(focused_remote_car_id(telem), 18)

    def test_returns_none_without_a_connected_focused_car(self):
        telem = TelemetryPage()
        telem.car_count = 1
        telem.focused_car = 0
        telem.cars[0].car_id = 0
        telem.cars[0].session_id = 18
        telem.cars[0].is_connected = 0

        self.assertIsNone(focused_remote_car_id(telem))


class FakeResponse(object):
    def __init__(self, status_code):
        self.status_code = status_code


class HighlightClientTest(unittest.TestCase):
    def _config(self):
        return {
            'url': 'https://timing.example/api/broadcast-highlight',
            'username': 'remote-app',
            'password': 'secret',
        }

    def test_posts_remote_car_id_with_basic_auth(self):
        calls = []

        def post(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeResponse(200)

        client = BroadcastHighlightClient(self._config(), post=post)

        result = client.send_once(18)

        self.assertEqual(result, SUCCESS)
        self.assertEqual(calls, [(('https://timing.example/api/broadcast-highlight',), {
            'json': {'carId': 18},
            'auth': ('remote-app', 'secret'),
            'timeout': 2.0,
            'allow_redirects': False,
        })])

    def test_null_target_clears_highlight(self):
        payloads = []

        def post(url, **kwargs):
            payloads.append(kwargs['json'])
            return FakeResponse(200)

        client = BroadcastHighlightClient(self._config(), post=post)

        self.assertEqual(client.send_once(None), SUCCESS)
        self.assertEqual(payloads, [{'carId': None}])

    def test_only_queues_target_changes(self):
        client = BroadcastHighlightClient(self._config(),
                                          post=lambda *a, **kw: FakeResponse(200))

        self.assertTrue(client.publish(18))
        self.assertFalse(client.publish(18))
        self.assertTrue(client.publish(7))
        self.assertTrue(client.publish(None))
        self.assertFalse(client.publish(None))

    def test_server_errors_retry_but_client_errors_stop(self):
        statuses = iter([500, 401, 404])
        client = BroadcastHighlightClient(
            self._config(), post=lambda *a, **kw: FakeResponse(next(statuses)))

        self.assertEqual(client.send_once(18), RETRY)
        self.assertEqual(client.send_once(18), AUTH_ERROR)
        self.assertEqual(client.send_once(18), TERMINAL)

    def test_network_errors_retry(self):
        def unavailable(*args, **kwargs):
            import requests
            raise requests.ConnectionError('offline')

        client = BroadcastHighlightClient(self._config(), post=unavailable)

        self.assertEqual(client.send_once(18), RETRY)


if __name__ == '__main__':
    unittest.main()
