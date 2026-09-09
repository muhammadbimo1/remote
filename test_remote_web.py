import unittest
from unittest.mock import patch
from werkzeug.exceptions import Forbidden

from ac_ipc import ACIPCTransport
from test_ac_ipc import FakeSocket
import remote_web
from remote_web import apply_timetable_offsets, build_update_data


class CarFixture(object):
    def __init__(self):
        self.car_id = 0
        self.session_id = 0
        self.position = 0
        self.normalized_spline_pos = 0.0
        self.speed_kmh = 0.0
        self.lap_time = 0
        self.best_lap = 0
        self.last_lap = 0
        self.lap_count = 0
        self.is_in_pit = False
        self.is_connected = False
        self.is_colliding = False
        self.is_rolled_over = False
        self.driver_name = ''
        self.team_name = ''


class TelemetryPage(object):
    """Mutable test fixture mirroring a validated telemetry snapshot."""

    def __init__(self):
        self.packet_id = 0
        self.car_count = 0
        self.focused_car = 0
        self.current_camera = 0
        self.car_cameras_count = 0
        self.current_car_camera = 0
        self.track_length = 0.0
        self.session_type = 0
        self.session_index = 0
        self.session_type_raw = 0
        self.session_gen = 0
        self.session_name = ''
        self.is_replay = False
        self.replay_frame = 0
        self.replay_frames = 0
        self.replay_frame_ms = 0.0
        self.replay_last_result = 0
        self.is_replay_only = False
        self.replay_file = ''
        self.replay_temp_dir = ''
        self.timetable_url = ''
        self.cars = [CarFixture() for _ in range(remote_web.MAX_CARS)]


class TeamNamePayloadTest(unittest.TestCase):
    def test_driver_payload_includes_team_name(self):
        telem = TelemetryPage()
        telem.car_count = 1
        telem.focused_car = 0
        telem.track_length = 5000.0

        car = telem.cars[0]
        car.car_id = 0
        car.position = 1
        car.normalized_spline_pos = 0.5
        car.speed_kmh = 120.0
        car.lap_count = 3
        car.is_connected = 1
        car.driver_name = "Alex Driver"
        car.team_name = "Blue Arrow Racing"

        data = build_update_data(telem)

        self.assertEqual(data["drivers"][0]["name"], "Alex Driver")
        self.assertEqual(data["drivers"][0]["team_name"], "Blue Arrow Racing")

    def test_driver_class_is_derived_from_team_name_prefix(self):
        cases = [
            ("GT3 42 | Team A", "GT3"),
            ("LMP2 7 | Team B", "LMP2"),
            ("Hypercar 51 | Team C", "Hypercar"),
            ("Team Without Pipe", "Unclassed"),
        ]

        for team_name, expected_class in cases:
            with self.subTest(team_name=team_name):
                class_name, _ = remote_web.get_car_class(team_name)
                self.assertEqual(class_name, expected_class)

    def test_known_classes_use_fixed_colors(self):
        self.assertEqual(remote_web.get_car_class("PRO 42 | Team A"), ("PRO", "#34a853"))
        self.assertEqual(remote_web.get_car_class("HY 7 | Team B"), ("HY", "#ff0000"))
        self.assertEqual(remote_web.get_car_class("AM 51 | Team C"), ("AM", "#e69138"))


class IncidentNoticeMarkupTest(unittest.TestCase):
    def test_collision_notice_has_independent_six_second_duration(self):
        html = remote_web.app.test_client().get('/').get_data(as_text=True)

        self.assertIn('var INCIDENT_NOTICE_MS = 6000;', html)
        self.assertIn(
            'Date.now() - collisionTimers[d.num] < INCIDENT_NOTICE_MS', html)


class AutoDirectorMonitorIntegrationTest(unittest.TestCase):
    def test_monitor_passes_explicit_race_context(self):
        self.assertTrue(hasattr(remote_web, 'run_auto_director'))

        class RecordingDirector(object):
            enabled = True

            def __init__(self):
                self.race_contexts = []

            def tick(self, cars, track_length, is_race):
                self.race_contexts.append(is_race)
                return None

        original = remote_web.director
        recorder = RecordingDirector()
        remote_web.director = recorder
        try:
            qualifying = TelemetryPage()
            qualifying.session_type = 0
            qualifying.track_length = 5000.0
            race = TelemetryPage()
            race.session_type = 1
            race.track_length = 5000.0

            remote_web.run_auto_director([], qualifying, 7)
            remote_web.run_auto_director([], race, 7)
        finally:
            remote_web.director = original

        self.assertEqual(recorder.race_contexts, [False, True])


class DriverNameTest(unittest.TestCase):
    def test_driver_name_is_passed_through_unchanged(self):
        telem = TelemetryPage()
        telem.car_count = 1
        telem.track_length = 5000.0

        car = telem.cars[0]
        car.car_id = 4
        car.position = 1
        car.is_connected = 1
        car.driver_name = "23 | Alex Driver"

        driver = build_update_data(telem)["drivers"][0]

        self.assertEqual(driver["name"], "23 | Alex Driver")
        self.assertNotIn("car_number", driver)
        self.assertEqual(driver["num"], 5)


class StandingsOrderTest(unittest.TestCase):
    def _telem(self, session_type_raw, cars):
        telem = TelemetryPage()
        telem.session_type_raw = session_type_raw
        telem.car_count = len(cars)
        telem.track_length = 5000.0
        for index, spec in enumerate(cars):
            car = telem.cars[index]
            car.car_id = index
            car.position = spec['position']
            car.normalized_spline_pos = spec.get('spline', 0.5)
            car.speed_kmh = 120.0
            car.best_lap = spec['best_lap']
            car.lap_count = spec.get('lap_count', 3)
            car.is_connected = 1
            car.driver_name = spec['name']
            car.team_name = spec.get('team_name', 'PRO | Team')
        return telem

    def test_practice_and_qualifying_rank_by_fastest_lap(self):
        cars = [
            {'name': 'Slower', 'position': 1, 'best_lap': 95000},
            {'name': 'Fastest', 'position': 3, 'best_lap': 93000},
            {'name': 'No Time', 'position': 2, 'best_lap': 0},
        ]

        for session_type_raw in (1, 2):
            with self.subTest(session_type_raw=session_type_raw):
                drivers = build_update_data(
                    self._telem(session_type_raw, cars))['drivers']

                self.assertEqual(
                    [d['name'] for d in drivers],
                    ['Fastest', 'Slower', 'No Time'])
                self.assertEqual([d['position'] for d in drivers], [1, 2, 3])
                self.assertEqual(
                    [d['gap'] for d in drivers],
                    ['Leader', '+2.000s', '-'])
                self.assertEqual(
                    [d['interval'] for d in drivers],
                    ['-', '+2.000s', '-'])

    def test_qualifying_class_standings_use_fastest_lap(self):
        telem = self._telem(2, [
            {'name': 'PRO Fast', 'position': 3, 'best_lap': 93000,
             'team_name': 'PRO | Team A'},
            {'name': 'AM Fast', 'position': 2, 'best_lap': 94000,
             'team_name': 'AM | Team B'},
            {'name': 'PRO Slow', 'position': 1, 'best_lap': 95000,
             'team_name': 'PRO | Team C'},
        ])

        drivers = build_update_data(telem)['drivers']

        self.assertEqual([d['name'] for d in drivers],
                         ['PRO Fast', 'AM Fast', 'PRO Slow'])
        self.assertEqual([d['class_position'] for d in drivers], [1, 1, 2])
        self.assertEqual(drivers[2]['class_interval'], '+2.000s')

    def test_race_standings_remain_ordered_by_race_position(self):
        drivers = build_update_data(self._telem(3, [
            {'name': 'Race Leader', 'position': 1, 'best_lap': 95000,
             'spline': 0.7},
            {'name': 'Faster Lap', 'position': 2, 'best_lap': 93000,
             'spline': 0.6},
        ]))['drivers']

        self.assertEqual([d['name'] for d in drivers],
                         ['Race Leader', 'Faster Lap'])
        self.assertEqual([d['position'] for d in drivers], [1, 2])
        self.assertEqual(drivers[0]['gap'], 'Leader')

    def test_physical_battle_context_is_symmetric(self):
        cars = remote_web.compute_gaps(self._telem(3, [
            {'name': 'Defender', 'position': 1, 'best_lap': 95000,
             'spline': 0.500},
            {'name': 'Attacker', 'position': 2, 'best_lap': 96000,
             'spline': 0.499},
        ]))

        self.assertIn('battle_gap_behind_seconds', cars[0])
        self.assertIn('battle_gap_ahead_seconds', cars[1])
        self.assertLess(cars[0]['battle_gap_behind_seconds'], 1.5)
        self.assertLess(cars[1]['battle_gap_ahead_seconds'], 1.5)
        self.assertEqual(cars[0]['battle_opponent_ids'], [1])
        self.assertEqual(cars[1]['battle_opponent_ids'], [0])

    def test_qualifying_lap_delta_is_not_physical_battle_gap(self):
        cars = remote_web.compute_gaps(self._telem(2, [
            {'name': 'Fast', 'position': 1, 'best_lap': 95000,
             'spline': 0.8},
            {'name': 'Close Lap', 'position': 2, 'best_lap': 95100,
             'spline': 0.2},
        ]))

        self.assertIn('battle_gap_behind_seconds', cars[0])
        self.assertIn('battle_gap_ahead_seconds', cars[1])
        self.assertGreater(cars[0]['battle_gap_behind_seconds'], 2.0)
        self.assertGreater(cars[1]['battle_gap_ahead_seconds'], 2.0)


class TimetableOffsetTest(unittest.TestCase):
    def tearDown(self):
        with remote_web._resync_lock:
            remote_web.progress_offsets.clear()

    def test_timetable_car_id_matches_connected_session_id_not_local_car_id(self):
        telem = TelemetryPage()
        telem.car_count = 3

        stale_same_slot = telem.cars[1]
        stale_same_slot.car_id = 1
        stale_same_slot.session_id = 1
        stale_same_slot.lap_count = 99
        stale_same_slot.is_connected = 0

        local_car = telem.cars[2]
        local_car.car_id = 2
        local_car.session_id = 18
        local_car.lap_count = 4
        local_car.is_connected = 1

        matched = apply_timetable_offsets({
            "EntryList": [
                {"CarID": 18, "Ping": 24, "Laps": 7},
                {"CarID": 1, "Ping": 24, "Laps": 1},
            ],
        }, telem)

        self.assertEqual(matched, 1)
        with remote_web._resync_lock:
            self.assertEqual(remote_web.progress_offsets, {2: 3.0})


class BroadcastHighlightIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.old_client = remote_web.highlight_client

    def tearDown(self):
        remote_web.highlight_client = self.old_client

    def test_focused_local_car_publishes_remote_session_id(self):
        published = []

        class Recorder(object):
            def publish(self, car_id):
                published.append(car_id)
                return True

        telem = TelemetryPage()
        telem.car_count = 3
        telem.focused_car = 2
        car = telem.cars[2]
        car.car_id = 2
        car.session_id = 18
        car.is_connected = 1
        remote_web.highlight_client = Recorder()

        self.assertTrue(remote_web.publish_focused_highlight(telem))
        self.assertEqual(published, [18])

    def test_missing_telemetry_publishes_clear(self):
        published = []

        class Recorder(object):
            def publish(self, car_id):
                published.append(car_id)
                return True

        remote_web.highlight_client = Recorder()

        self.assertTrue(remote_web.publish_focused_highlight(None))
        self.assertEqual(published, [None])


class TimetableUrlTest(unittest.TestCase):
    def test_timetable_url_comes_from_telemetry(self):
        telem = TelemetryPage()
        telem.timetable_url = "http://103.129.148.255:14103/timetable.json"

        self.assertEqual(
            remote_web.get_timetable_url(telem),
            "http://103.129.148.255:14103/timetable.json")

    def test_blank_timetable_url_disables_polling(self):
        telem = TelemetryPage()
        telem.timetable_url = ""

        self.assertEqual(remote_web.get_timetable_url(telem), "")


class TimetablePollTest(unittest.TestCase):
    def setUp(self):
        self.old_ac_connected = remote_web.ac_connected
        remote_web.ac_connected = True

    def tearDown(self):
        remote_web.ac_connected = self.old_ac_connected
        with remote_web._resync_lock:
            remote_web.progress_offsets.clear()
        remote_web.last_poll_status = 'idle'
        remote_web.last_poll_error = ''
        remote_web.last_poll_matched = 0

    def test_local_race_applies_timetable_even_if_server_session_type_is_stale(self):
        telem = TelemetryPage()
        telem.session_type = 1
        telem.car_count = 1

        car = telem.cars[0]
        car.car_id = 0
        car.session_id = 18
        car.lap_count = 4
        car.is_connected = 1

        with patch.object(remote_web, 'fetch_timetable', return_value={
            'SessionType': 'PRACTICE',
            'EntryList': [{'CarID': 18, 'Ping': 12, 'Laps': 7}],
        }):
            remote_web.poll_timetable_once('http://example.test/timetable.json', telem)

        self.assertEqual(remote_web.last_poll_status, 'ok')
        self.assertEqual(remote_web.last_poll_matched, 1)
        with remote_web._resync_lock:
            self.assertEqual(remote_web.progress_offsets, {0: 3.0})


class ReplayPayloadTest(unittest.TestCase):
    def tearDown(self):
        remote_web.event_log.clear()
        remote_web._last_live_payload = None
        remote_web._replay_fallback_payload = None

    def _live_telem(self):
        telem = TelemetryPage()
        telem.car_count = 1
        telem.focused_car = 0
        telem.track_length = 5000.0
        car = telem.cars[0]
        car.car_id = 0
        car.position = 1
        car.is_connected = 1
        car.driver_name = "23 | Alex Driver"
        return telem

    def test_live_payload_carries_an_inactive_replay_block(self):
        data = build_update_data(self._live_telem())

        self.assertFalse(data["replay"]["active"])
        self.assertEqual(data["events"], [])

    def test_replay_fields_are_published(self):
        telem = self._live_telem()
        telem.is_replay = 1
        telem.replay_frame = 120
        telem.replay_frames = 400
        telem.replay_frame_ms = 25.0
        telem.replay_last_result = 2

        replay = build_update_data(telem)["replay"]

        self.assertTrue(replay["active"])
        self.assertEqual(replay["frame"], 120)
        self.assertEqual(replay["frames"], 400)
        self.assertEqual(replay["last_result"], 2)

    def test_replay_payload_freezes_the_driver_list(self):
        live = build_update_data(self._live_telem())
        remote_web._last_live_payload = live

        telem = self._live_telem()
        telem.is_replay = 1
        telem.replay_frame = 7
        telem.focused_car = 0
        # Replay-time car data: the frozen list must not follow it.
        telem.cars[0].position = 9
        telem.cars[0].driver_name = "23 | Alex Driver"

        data = remote_web.build_replay_update_data(telem)

        self.assertEqual(data["drivers"], live["drivers"])
        self.assertTrue(data["replay"]["active"])
        self.assertEqual(data["replay"]["frame"], 7)

    def test_replay_payload_without_a_live_snapshot_seeds_driver_grid(self):
        telem = self._live_telem()
        telem.is_replay = 1

        data = remote_web.build_replay_update_data(telem)

        self.assertEqual(len(data["drivers"]), 1)
        self.assertEqual(data["drivers"][0]["name"], "23 | Alex Driver")
        self.assertTrue(data["ac_connected"])

    def test_replay_seeded_driver_grid_stays_frozen(self):
        telem = self._live_telem()
        telem.is_replay = 1
        first = remote_web.build_replay_update_data(telem)

        telem.cars[0].position = 9
        telem.cars[0].driver_name = "Replay Frame Name"
        second = remote_web.build_replay_update_data(telem)

        self.assertEqual(second["drivers"], first["drivers"])
        self.assertEqual(second["drivers"][0]["name"], "23 | Alex Driver")


class ReplayPollGuardTest(unittest.TestCase):
    def setUp(self):
        self.old_ac_connected = remote_web.ac_connected
        remote_web.ac_connected = True

    def tearDown(self):
        remote_web.ac_connected = self.old_ac_connected
        with remote_web._resync_lock:
            remote_web.progress_offsets.clear()
        remote_web.last_poll_status = 'idle'

    def test_timetable_poll_is_skipped_while_replay_is_active(self):
        telem = TelemetryPage()
        telem.session_type = 1
        telem.is_replay = 1

        with patch.object(remote_web, 'fetch_timetable') as fetch:
            result = remote_web.poll_timetable_once(
                'http://example.test/timetable.json', telem)

        self.assertFalse(result)
        self.assertEqual(remote_web.last_poll_status, 'disabled_replay')
        fetch.assert_not_called()


class SessionRotationTest(unittest.TestCase):
    def setUp(self):
        remote_web._current_session = None
        self.rotations = []
        patcher = patch.object(
            remote_web.event_journal, 'start_session',
            side_effect=lambda label=None, **kw: self.rotations.append(label))
        self.addCleanup(patcher.stop)
        patcher.start()

    def tearDown(self):
        remote_web._current_session = None
        remote_web.event_log.clear()

    def _telem(self, gen=1, index=0, type_raw=3, name="Race"):
        telem = TelemetryPage()
        telem.session_gen = gen
        telem.session_index = index
        telem.session_type_raw = type_raw
        telem.session_name = name
        return telem

    def test_first_telemetry_opens_a_journal(self):
        self.assertEqual(remote_web.maybe_rotate_session(self._telem()), 'Race')
        self.assertEqual(self.rotations, ['Race'])

    def test_same_session_does_not_rotate(self):
        remote_web.maybe_rotate_session(self._telem())
        self.assertIsNone(remote_web.maybe_rotate_session(self._telem()))
        self.assertEqual(len(self.rotations), 1)

    def test_session_restart_rotates_on_generation_alone(self):
        remote_web.maybe_rotate_session(self._telem(gen=1))
        # Same index and type, restarted race: only session_gen moves.
        self.assertEqual(remote_web.maybe_rotate_session(self._telem(gen=2)), 'Race')
        self.assertEqual(len(self.rotations), 2)

    def test_practice_to_qualify_rotates_and_labels(self):
        remote_web.maybe_rotate_session(self._telem(gen=1, index=0, type_raw=1,
                                                    name="Practice"))
        label = remote_web.maybe_rotate_session(self._telem(gen=2, index=1, type_raw=2,
                                                            name="Qualify"))
        self.assertEqual(label, 'Qualify 2')

    def test_label_falls_back_to_the_session_type(self):
        telem = self._telem(type_raw=2, name="")
        self.assertEqual(remote_web.session_label(telem), 'Qualify')

    def test_rotation_drops_events_and_offsets_from_the_previous_session(self):
        remote_web.maybe_rotate_session(self._telem(gen=1))
        remote_web.event_log.mark(3, name='Alex Driver')
        with remote_web._resync_lock:
            remote_web.progress_offsets[3] = 2.0

        remote_web.maybe_rotate_session(self._telem(gen=2))

        self.assertEqual(remote_web.event_log.snapshot(), [])
        with remote_web._resync_lock:
            self.assertEqual(remote_web.progress_offsets, {})


class ReplayCommandTest(unittest.TestCase):
    def test_replay_command_does_not_bump_the_camera_sequence(self):
        """Lua parks the shot until replay is live; bumping command_seq here
        would make it apply the focus immediately, where the toggle eats it."""
        transport = ACIPCTransport()
        socket = FakeSocket()
        transport.attach(socket)
        with patch.object(remote_web, 'ac_transport', transport):
            ok = remote_web.send_replay_command(
                remote_web.REPLAY_ENTER, rewind_s=12.5, driver=3, camera=1)

        self.assertTrue(ok)
        message = socket.sent[-1]
        self.assertNotIn('command_seq', message)
        self.assertEqual(message['replay_action'], remote_web.REPLAY_ENTER)
        self.assertAlmostEqual(message['replay_rewind_s'], 12.5, places=3)
        self.assertEqual(message['target_driver'], 3)
        self.assertEqual(message['target_camera'], 1)
        self.assertEqual(message['replay_seq'], 1)

    def test_camera_command_is_sent_over_active_ac_socket(self):
        transport = ACIPCTransport()
        socket = FakeSocket()
        transport.attach(socket)
        with patch.object(remote_web, 'ac_transport', transport):
            ok = remote_web.send_command(4, 4, 2)

        self.assertTrue(ok)
        self.assertEqual(socket.sent[-1]['type'], 'command')
        self.assertEqual(socket.sent[-1]['target_car_camera'], 2)


class IPCPeerSecurityTest(unittest.TestCase):
    def test_ipv4_and_ipv6_loopback_are_allowed(self):
        self.assertTrue(remote_web.is_loopback_peer('127.0.0.1'))
        self.assertTrue(remote_web.is_loopback_peer('::1'))

    def test_lan_and_forwarded_text_are_rejected(self):
        self.assertFalse(remote_web.is_loopback_peer('192.168.1.25'))
        self.assertFalse(remote_web.is_loopback_peer(
            '127.0.0.1, 192.168.1.25'))

    def test_lan_peer_is_rejected_before_websocket_accept(self):
        with remote_web.app.test_request_context(
                '/ac-ipc', environ_base={'REMOTE_ADDR': '192.168.1.25'}), \
                patch.object(remote_web.Server, 'accept') as accept:
            with self.assertRaises(Forbidden):
                remote_web.handle_ac_ipc()
        accept.assert_not_called()

    def test_loopback_peer_can_send_telemetry(self):
        observed_packet_ids = []

        class OneMessageSocket(object):
            def __init__(self):
                self.connected = True
                self.messages = [None, remote_web.json.dumps({
                    'version': 1, 'type': 'telemetry', 'packet_id': 9,
                    'car_count': 0, 'focused_car': 0, 'current_camera': 1,
                    'car_cameras_count': 0, 'current_car_camera': 0,
                    'track_length': 5000.0, 'session_type': 1,
                    'session_index': 0, 'session_type_raw': 3,
                    'session_gen': 1, 'session_name': 'Race',
                    'is_replay': False, 'replay_frame': 0,
                    'replay_frames': 100, 'replay_frame_ms': 60.0,
                    'replay_last_result': 0, 'is_replay_only': False,
                    'replay_file': '', 'replay_temp_dir': '',
                    'timetable_url': '', 'cars': [],
                })]

            def receive(self, timeout=None):
                if self.messages:
                    return self.messages.pop(0)
                snapshot = remote_web.ac_transport.latest()
                observed_packet_ids.append(snapshot.packet_id)
                raise remote_web.ConnectionClosed()

            def close(self):
                pass

        socket = OneMessageSocket()
        with remote_web.app.test_request_context(
                '/ac-ipc', environ_base={'REMOTE_ADDR': '127.0.0.1'}), \
                patch.object(remote_web.Server, 'accept', return_value=socket):
            self.assertEqual(remote_web.handle_ac_ipc(), '')

        self.assertEqual(observed_packet_ids, [9])


class DisconnectStateResetTest(unittest.TestCase):
    def test_reset_clears_all_state_that_belongs_to_the_old_ac_run(self):
        remote_web._current_session = (1, 0, 3)
        remote_web._last_live_payload = {'drivers': [{'num': 1}]}
        remote_web._latest_replay_context = {'session': 'Race'}
        remote_web._latest_cars_by_id = {0: {'car_id': 0}}
        remote_web.review_journal = {'events': [{'id': 1}]}
        remote_web.latest_focused_car = 7
        remote_web.latest_current_camera = 4
        remote_web.latest_current_car_camera = 2
        remote_web.latest_replay_file = 'old.acreplay'
        remote_web._prev_rollover_state[0] = True
        remote_web.event_log.mark(0, name='Old Driver')
        with remote_web._resync_lock:
            remote_web.progress_offsets[0] = 3.0

        remote_web.reset_ac_run_state()

        self.assertIsNone(remote_web._current_session)
        self.assertIsNone(remote_web._last_live_payload)
        self.assertEqual(remote_web._latest_replay_context, {})
        self.assertEqual(remote_web._latest_cars_by_id, {})
        self.assertIsNone(remote_web.review_journal)
        self.assertEqual(remote_web.latest_focused_car, 0)
        self.assertEqual(remote_web.latest_current_camera, 0)
        self.assertEqual(remote_web.latest_current_car_camera, 0)
        self.assertEqual(remote_web.latest_replay_file, '')
        self.assertEqual(remote_web._prev_rollover_state, {})
        self.assertEqual(remote_web.event_log.snapshot(), [])
        with remote_web._resync_lock:
            self.assertEqual(remote_web.progress_offsets, {})

    def test_mark_is_rejected_without_fresh_ac_telemetry(self):
        with patch.object(remote_web.ac_transport, 'is_connected',
                          return_value=False), \
                patch.object(remote_web.event_log, 'mark') as mark, \
                patch.object(remote_web, 'record_event') as record, \
                patch.object(remote_web, 'emit'):
            remote_web.handle_mark_event()

        mark.assert_not_called()
        record.assert_not_called()


class ReviewModeTest(unittest.TestCase):
    def tearDown(self):
        remote_web.exit_review()
        remote_web._last_live_payload = None

    def test_review_event_frame_falls_back_to_session_seconds(self):
        ev = remote_web._review_event_from_record(
            {'session_s': 100.0, 'kind': 'collision', 'label': 'HIT'},
            3, 25.0)

        self.assertEqual(ev['frame'], 4000)
        self.assertEqual(ev['seek_s'], 100.0)
        self.assertEqual(ev['id'], 3)

    def test_review_event_seek_falls_back_to_frame(self):
        ev = remote_web._review_event_from_record(
            {'replay_frame': 4000, 'kind': 'collision', 'label': 'HIT'},
            1, 25.0)

        self.assertEqual(ev['frame'], 4000)
        self.assertEqual(ev['seek_s'], 100.0)

    def test_enter_review_loads_the_matched_journal(self):
        telem = TelemetryPage()
        telem.replay_frames = 4000
        telem.replay_frame_ms = 25.0
        session = {'file': 'AC_1.jsonl', 'replay_frame_ms': 25.0}
        records = [{'kind': 'collision', 'label': 'HIT', 'driver': 'A',
                    'replay_frame': 400, 'session_s': 10.0}]

        with patch.object(remote_web.event_journal, 'list_sessions',
                          return_value=[session]), \
                patch.object(remote_web.event_journal, 'load',
                             return_value=records), \
                patch.object(remote_web, 'match_replay',
                             return_value=(session, 'exact')):
            remote_web.enter_review('C:/AC_1.acreplay', telem)

        self.assertEqual(remote_web.review_journal['session'], session)
        self.assertEqual(remote_web.review_journal['replay_file'],
                         'C:/AC_1.acreplay')
        self.assertFalse(remote_web.review_journal['manual'])
        self.assertEqual(len(remote_web.review_journal['events']), 1)
        self.assertEqual(remote_web.review_journal['events'][0]['frame'], 400)

    def test_enter_review_without_a_match_has_empty_events(self):
        telem = TelemetryPage()
        telem.replay_frames = 0
        telem.replay_frame_ms = 0.0

        with patch.object(remote_web.event_journal, 'list_sessions',
                          return_value=[]), \
                patch.object(remote_web, 'match_replay',
                             return_value=(None, None)):
            remote_web.enter_review('C:/AC_1.acreplay', telem)

        self.assertIsNone(remote_web.review_journal['session'])
        self.assertEqual(remote_web.review_journal['events'], [])

    def test_review_payload_serves_journal_events(self):
        remote_web.review_journal = {
            'replay_file': 'x', 'session': {'file': 'AC_1.jsonl'},
            'manual': False,
            'events': [{'id': 1, 'kind': 'collision', 'label': 'HIT',
                        'car_id': 0, 'name': 'A', 't': 1.0,
                        'age': 0, 'frame': 400, 'seek_s': 10.0}],
        }
        telem = TelemetryPage()
        telem.is_replay = 1
        telem.replay_frame = 1

        data = remote_web.build_replay_update_data(telem)

        self.assertEqual(len(data['events']), 1)
        self.assertTrue(data['review']['active'])
        self.assertTrue(data['review']['matched'])

    def test_live_replay_payload_carries_an_inactive_review_block(self):
        telem = TelemetryPage()
        telem.is_replay = 1

        data = remote_web.build_replay_update_data(telem)

        self.assertFalse(data['review']['active'])
        self.assertFalse(data['review']['matched'])

    def test_jump_in_review_seeks_by_frame(self):
        remote_web.review_journal = {
            'replay_file': 'x', 'session': {'file': 'AC_1.jsonl'},
            'manual': False,
            'events': [{'id': 1, 'kind': 'collision', 'label': 'HIT',
                        'car_id': 0, 'frame': 400}],
        }
        with patch.object(remote_web, 'send_replay_command',
                          return_value=True) as send, \
                patch.object(remote_web, 'emit'):
            remote_web.handle_jump_to_event({'id': 1})

        send.assert_called_once_with(
            remote_web.REPLAY_SEEK_FRAME, frame=400, driver=0, camera=1)

    def test_jump_in_review_missing_event_does_not_seek(self):
        remote_web.review_journal = {
            'replay_file': 'x', 'session': {'file': 'AC_1.jsonl'},
            'manual': False, 'events': [],
        }
        with patch.object(remote_web, 'send_replay_command') as send, \
                patch.object(remote_web, 'emit'):
            remote_web.handle_jump_to_event({'id': 99})

        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
