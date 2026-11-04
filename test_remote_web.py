import json
import re
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch
from werkzeug.exceptions import Forbidden

from ac_ipc import ACIPCTransport
from test_ac_ipc import FakeSocket
import remote_web
from remote_web import build_update_data


class MarkRewindTest(unittest.TestCase):
    def test_backdated_mark_orders_by_event_time_and_expires(self):
        from event_log import EventLog
        log = EventLog(clock=lambda: 1000.0)
        log.window_s = 60.0
        recent = log.mark(0)
        earlier = log.mark(1, back_s=10.0)
        self.assertEqual([ev['id'] for ev in log.snapshot()],
                         [recent['id'], earlier['id']])
        expired = log.mark(2, back_s=90.0)
        self.assertIsNone(log.get(expired['id']))
        self.assertIsNotNone(log.get(recent['id']))

    def test_backdated_mark_cooldown_uses_press_time(self):
        from event_log import EventLog
        log = EventLog(clock=lambda: 1000.0)
        log.mark(0, back_s=10.0)
        self.assertIsNone(log.mark(0, back_s=0.0))

    def test_mark_offset_survives_journal_and_saved_replay(self):
        import tempfile
        from event_log import EventLog
        from event_journal import EventJournal

        log = EventLog(clock=lambda: 1000.0)
        with tempfile.TemporaryDirectory() as directory:
            journal = EventJournal(directory)
            with patch.object(remote_web, 'event_log', log), \
                    patch.object(remote_web, 'event_journal', journal), \
                    patch.object(remote_web.ac_transport, 'is_connected', return_value=True), \
                    patch.object(remote_web, 'journal_context', return_value={
                        'replay_frame': 4000, 'replay_frame_ms': 25.0,
                        'session_s': 100.0}), \
                    patch.object(remote_web, 'emit'):
                remote_web.handle_mark_event({'preroll_s': 12.5})
            self.assertEqual(log.snapshot()[0]['t'], 987.5)
            self.assertEqual(log.snapshot()[0]['age'], 12.5)
            with open(journal.path, encoding='utf-8') as fh:
                record = json.loads(fh.readline())
            event = remote_web._review_event_from_record(record, 1, 25.0)
            self.assertEqual(record['t'], 987.5)
            self.assertEqual(record['session_s'], 87.5)
            self.assertEqual(event['frame'], 3500)
            self.assertEqual(event['seek_frame'], 3500)

    def test_live_mark_jump_uses_its_offset_plus_elapsed_time(self):
        from event_log import EventLog
        log = EventLog(clock=lambda: 1000.0)
        with patch.object(remote_web, 'event_log', log), \
                patch.object(remote_web.ac_transport, 'is_connected', return_value=True), \
                patch.object(remote_web, 'record_event'), \
                patch.object(remote_web, 'review_journal', None), \
                patch.object(remote_web.director, 'enabled', False), \
                patch.object(remote_web.time, 'time', return_value=1020.0), \
                patch.object(remote_web, 'send_replay_command', return_value=True) as send, \
                patch.object(remote_web, 'emit'):
            remote_web.handle_mark_event({'preroll_s': 12.5})
            remote_web.handle_jump_to_event({'id': log.snapshot()[0]['id']})
        self.assertEqual(send.call_args.kwargs['rewind_s'], 32.5)

    def test_invalid_mark_offsets_are_rejected(self):
        from event_log import EventLog
        for value in (-1, 121, float('nan'), float('inf'), True, 'bad'):
            with self.subTest(value=value):
                log = EventLog(clock=lambda: 1000.0)
                with patch.object(remote_web, 'event_log', log), \
                        patch.object(remote_web.ac_transport, 'is_connected', return_value=True), \
                        patch.object(remote_web, 'record_event'), \
                        patch.object(remote_web, 'emit') as emit:
                    remote_web.handle_mark_event({'preroll_s': value})
                self.assertEqual(log.snapshot(), [])
                self.assertFalse(emit.call_args.args[1]['ok'])


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
        self.car_cameras_count = 0
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
        self.track_name = ''
        self.session_type = 0
        self.session_index = 0
        self.session_type_raw = 0
        self.session_gen = 0
        self.session_name = ''
        self.is_timed_session = False
        self.session_time_left = 0.0
        self.is_replay = False
        self.replay_frame = 0
        self.replay_frames = 0
        self.replay_frame_ms = 0.0
        self.replay_last_result = 0
        self.is_replay_only = False
        self.replay_file = ''
        self.replay_temp_dir = ''
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


class TimetableStatusMarkupTest(unittest.TestCase):
    def test_page_does_not_display_timetable_status(self):
        html = remote_web.app.test_client().get('/').get_data(as_text=True)

        self.assertNotIn('timetable-badge', html)


class ControlLayoutMarkupTest(unittest.TestCase):
    def setUp(self):
        self.html = remote_web.app.test_client().get('/').get_data(as_text=True)

    def test_bumper_camera_can_be_selected(self):
        self.assertRegex(self.html, r'<button[^>]*data-cam="6"[^>]*>Bumper</button>')
        with patch.object(remote_web, 'send_command') as send:
            response = remote_web.app.test_client().post('/', data={
                'action': 'change_camera', 'camera': '6',
            })
        self.assertEqual(response.status_code, 204)
        send.assert_called_once_with(remote_web.latest_focused_car, 6, -1)

    def test_f6_subcamera_controls_follow_camera_mode_controls(self):
        camera_modes = self.html.index('class="control-bar"')
        subcameras = self.html.index('<div id="subcam-bar"')
        pip_controls = self.html.index('<div id="pip-bar"')

        self.assertLess(camera_modes, subcameras)
        self.assertLess(subcameras, pip_controls)

    def test_control_buttons_have_no_secondary_captions(self):
        self.assertNotIn('class="cam-cap"', self.html)

    def test_class_picker_is_separated_from_driver_grid(self):
        css = (Path(__file__).parent / 'static' / 'remote.css').read_text(encoding='utf-8')
        rule = re.search(r'\.class-filter-bar\s*\{([^}]*)\}', css)

        self.assertIsNotNone(rule)
        self.assertIn('padding-bottom: var(--space-2)', rule.group(1))


class EventNameFilterTest(unittest.TestCase):
    def _matches(self, event, include_overtakes, query):
        html = remote_web.app.test_client().get('/').get_data(as_text=True)
        match = re.search(
            r'function eventMatchesFilters\(ev, includeOvertakes, nameQuery\) '
            r'\{.*?\n            \}',
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(match, 'event name filter function is missing')
        script = '{}\nconsole.log(JSON.stringify(eventMatchesFilters({}, {}, {})));'.format(
            match.group(0),
            json.dumps(event),
            json.dumps(include_overtakes),
            json.dumps(query),
        )
        result = subprocess.run(
            ['node', '-e', script],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    def test_matches_driver_name_by_case_insensitive_partial_text(self):
        event = {'kind': 'collision', 'name': '23 | Alex Driver'}

        self.assertTrue(self._matches(event, False, '  aLeX  '))
        self.assertFalse(self._matches(event, False, 'Morgan'))

    def test_combines_name_and_overtake_filters(self):
        overtake = {'kind': 'overtake', 'name': 'Alex Driver'}

        self.assertFalse(self._matches(overtake, False, 'Alex'))
        self.assertTrue(self._matches(overtake, True, 'Alex'))


class EventLapMarkupTest(unittest.TestCase):
    def _function(self, html, name):
        start = html.index('function {}('.format(name))
        brace = html.index('{', start)
        depth = 0
        for index in range(brace, len(html)):
            if html[index] == '{':
                depth += 1
            elif html[index] == '}':
                depth -= 1
                if depth == 0:
                    return html[start:index + 1]
        self.fail('{} function is incomplete'.format(name))

    def _render(self, event):
        html = remote_web.app.test_client().get('/').get_data(as_text=True)
        script = '''
var showOvertakes = false;
var eventNameQuery = '';
var lastEvents = [];
var list = {innerHTML: ''};
var count = {textContent: ''};
var document = {getElementById: function(id) {
    return id === 'event-list' ? list : count;
}};
function eventAgeClass() { return ''; }
function formatAge() { return '0s'; }
function escapeHtml(value) { return String(value); }
%s
%s
%s
renderEvents([%s]);
console.log(list.innerHTML);
''' % (
            self._function(html, 'eventMatchesFilters'),
            self._function(html, 'formatTimeRemaining'),
            self._function(html, 'renderEvents'),
            json.dumps(event),
        )
        result = subprocess.run(
            ['node', '-e', script], check=True, capture_output=True, text=True)
        return result.stdout

    def test_event_row_shows_the_captured_leader_lap(self):
        markup = self._render({
            'id': 1, 'kind': 'collision', 'label': 'HIT',
            'name': 'Alex Driver', 'age': 0, 'lap': 8,
        })

        self.assertIn('<span class="ev-lap">LAP 8</span>', markup)

    def test_event_row_shows_the_captured_time_remaining(self):
        markup = self._render({
            'id': 1, 'kind': 'collision', 'label': 'HIT',
            'name': 'Alex Driver', 'age': 0, 'lap': 8,
            'time_remaining_ms': 3723000,
        })

        self.assertIn('<span class="ev-time">TIME 1:02:03</span>', markup)

    def test_old_event_without_a_lap_still_renders(self):
        markup = self._render({
            'id': 1, 'kind': 'collision', 'label': 'HIT',
            'name': 'Alex Driver', 'age': 0,
        })

        self.assertNotIn('ev-lap', markup)
        self.assertIn('Alex Driver', markup)


class EventJournalContextTest(unittest.TestCase):
    def setUp(self):
        self.previous_cars = remote_web._latest_cars_by_id

    def tearDown(self):
        remote_web._latest_cars_by_id = self.previous_cars

    def test_context_preserves_the_legacy_incident_completed_lap(self):
        remote_web._latest_cars_by_id = {
            0: {'car_id': 0, 'position': 1, 'lap_count': 7,
                'is_connected': 1},
            3: {'car_id': 3, 'position': 2, 'lap_count': 6,
                'is_connected': 1},
        }

        context = remote_web.journal_context(3)

        self.assertEqual(context['lap'], 6)


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

    def test_race_class_order_survives_delayed_lap_count_at_finish_line(self):
        drivers = build_update_data(self._telem(3, [
            {'name': 'Just Crossed', 'position': 1, 'best_lap': 95000,
             'lap_count': 10, 'spline': 0.01},
            {'name': 'Still Approaching', 'position': 2, 'best_lap': 96000,
             'lap_count': 10, 'spline': 0.99},
        ]))['drivers']

        self.assertEqual([d['name'] for d in drivers],
                         ['Just Crossed', 'Still Approaching'])
        self.assertEqual([d['class_position'] for d in drivers], [1, 2])

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


class LuaTimetableAuthorityTest(unittest.TestCase):
    def test_corrected_lap_count_drives_gap_without_python_offset(self):
        telem = TelemetryPage()
        telem.car_count = 2
        telem.track_length = 5000.0
        for car in telem.cars[:2]:
            car.is_connected = True
            car.speed_kmh = 180.0
        leader, behind = telem.cars[:2]
        leader.car_id = 0
        leader.position = 1
        leader.lap_count = 7
        leader.normalized_spline_pos = 0.4
        behind.car_id = 1
        behind.position = 2
        behind.lap_count = 5
        behind.normalized_spline_pos = 0.9

        cars = remote_web.compute_gaps(telem)

        self.assertEqual(cars[1]['gap'], '+1 Lap')
        self.assertEqual(cars[0]['total_progress'], 7.4)

    def test_corrected_position_controls_output_order(self):
        telem = TelemetryPage()
        telem.car_count = 2
        telem.track_length = 5000.0
        for car in telem.cars[:2]:
            car.is_connected = True
            car.speed_kmh = 100.0
            car.lap_count = 3
        telem.cars[0].car_id = 0
        telem.cars[0].position = 2
        telem.cars[0].normalized_spline_pos = 0.4
        telem.cars[1].car_id = 1
        telem.cars[1].position = 1
        telem.cars[1].normalized_spline_pos = 0.6

        cars = remote_web.compute_gaps(telem)

        self.assertEqual([car['car_id'] for car in cars], [1, 0])


class BroadcastHighlightIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.old_client = remote_web.highlight_client

    def tearDown(self):
        remote_web.highlight_client = self.old_client

    def test_focused_local_car_publishes_remote_session_id(self):
        published = []

        class Recorder(object):
            def publish(self, car_id, show_tower):
                published.append((car_id, show_tower))
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
        self.assertEqual(published, [(18, True)])

    def test_instant_replay_hides_tower(self):
        published = []

        class Recorder(object):
            def publish(self, car_id, show_tower):
                published.append((car_id, show_tower))
                return True

        telem = TelemetryPage()
        telem.is_replay = 1
        remote_web.highlight_client = Recorder()

        self.assertTrue(remote_web.publish_focused_highlight(telem))
        self.assertEqual(published, [(None, False)])

    def test_saved_replay_hides_tower(self):
        published = []

        class Recorder(object):
            def publish(self, car_id, show_tower):
                published.append((car_id, show_tower))
                return True

        telem = TelemetryPage()
        telem.is_replay_only = 1
        remote_web.highlight_client = Recorder()

        self.assertTrue(remote_web.publish_focused_highlight(telem))
        self.assertEqual(published, [(None, False)])

    def test_missing_telemetry_publishes_clear(self):
        published = []

        class Recorder(object):
            def publish(self, car_id, show_tower):
                published.append((car_id, show_tower))
                return True

        remote_web.highlight_client = Recorder()

        self.assertTrue(remote_web.publish_focused_highlight(None))
        self.assertEqual(published, [(None, True)])


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


class SessionRotationTest(unittest.TestCase):
    def setUp(self):
        remote_web._current_session = None
        self.rotations = []
        self.rotation_calls = []
        patcher = patch.object(
            remote_web.event_journal, 'start_session',
            side_effect=lambda label=None, **kw: (
                self.rotations.append(label),
                self.rotation_calls.append((label, kw))))
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

    def test_rotation_passes_track_name_to_the_journal(self):
        telem = self._telem()
        telem.track_name = 'Silverstone Grand Prix'

        remote_web.maybe_rotate_session(telem)

        self.assertEqual(
            self.rotation_calls[0][1]['track_name'],
            'Silverstone Grand Prix')

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

    def test_rotation_drops_events_from_the_previous_session(self):
        remote_web.maybe_rotate_session(self._telem(gen=1))
        remote_web.event_log.mark(3, name='Alex Driver')

        remote_web.maybe_rotate_session(self._telem(gen=2))

        self.assertEqual(remote_web.event_log.snapshot(), [])


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
                    'track_length': 5000.0,
                    'track_name': 'Silverstone Grand Prix', 'session_type': 1,
                    'session_index': 0, 'session_type_raw': 3,
                    'session_gen': 1, 'session_name': 'Race',
                    'is_timed_session': True,
                    'session_time_left': 3723000.0,
                    'is_replay': False, 'replay_frame': 0,
                    'replay_frames': 100, 'replay_frame_ms': 60.0,
                    'replay_last_result': 0, 'is_replay_only': False,
                    'replay_file': '', 'replay_temp_dir': '', 'cars': [],
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


class PiPStateTest(unittest.TestCase):
    def setUp(self):
        remote_web._pip_state = {
            'enabled': False,
            'driver': -1,
            'camera': 0,
            'camera_count': 0,
        }
        self.telem = TelemetryPage()
        self.telem.car_count = 5
        target = self.telem.cars[4]
        target.car_id = 4
        target.is_connected = True
        target.car_cameras_count = 3

    def test_selecting_driver_enables_camera_zero_without_main_command(self):
        with patch.object(remote_web.ac_transport, 'latest_with_generation',
                          return_value=(self.telem, 8)), \
                patch.object(remote_web.ac_transport, 'send_pip_command',
                             return_value=True) as send_pip, \
                patch.object(remote_web, 'send_command') as send_main:
            ok, state, error = remote_web.apply_pip_action({
                'action': 'select', 'driver': 4,
            })

        self.assertTrue(ok, error)
        self.assertEqual(state, {
            'enabled': True, 'driver': 4, 'camera': 0, 'camera_count': 3,
        })
        send_pip.assert_called_once_with(
            True, 4, 0, expected_generation=8)
        send_main.assert_not_called()

    def test_camera_delta_wraps_with_selected_drivers_count(self):
        remote_web._pip_state.update(
            enabled=True, driver=4, camera=2, camera_count=3)
        with patch.object(remote_web.ac_transport, 'send_pip_command',
                          return_value=True) as send_pip:
            ok, state, error = remote_web.apply_pip_action({
                'action': 'camera', 'delta': 1,
            })

        self.assertTrue(ok, error)
        self.assertEqual(state['camera'], 0)
        send_pip.assert_called_once_with(
            True, 4, 0, expected_generation=None)

    def test_disable_sends_full_state_without_fresh_telemetry(self):
        remote_web._pip_state.update(
            enabled=True, driver=4, camera=1, camera_count=3)
        with patch.object(remote_web.ac_transport, 'send_pip_command',
                          return_value=True) as send_pip:
            ok, state, error = remote_web.apply_pip_action({
                'action': 'enable', 'enabled': False,
            })

        self.assertTrue(ok, error)
        self.assertFalse(state['enabled'])
        self.assertEqual(state['driver'], 4)
        send_pip.assert_called_once_with(
            False, 4, 1, expected_generation=None)

    def test_invalid_or_unavailable_targets_are_rejected(self):
        cases = [
            ({'action': 'select', 'driver': True}, self.telem),
            ({'action': 'select', 'driver': 7}, self.telem),
            ({'action': 'select', 'driver': 4}, None),
        ]
        self.telem.cars[4].is_connected = False
        cases.append(({'action': 'select', 'driver': 4}, self.telem))

        for action, telem in cases:
            with self.subTest(action=action, telemetry=telem is not None), \
                    patch.object(remote_web.ac_transport,
                                 'latest_with_generation',
                                 return_value=(telem, 8)), \
                    patch.object(remote_web.ac_transport,
                                 'send_pip_command') as send_pip:
                ok, state, error = remote_web.apply_pip_action(action)
                self.assertFalse(ok)
                self.assertTrue(error)
                self.assertFalse(state['enabled'])
                send_pip.assert_not_called()

    def test_zero_camera_target_and_invalid_actions_are_rejected(self):
        self.telem.cars[4].car_cameras_count = 0
        with patch.object(remote_web.ac_transport, 'latest_with_generation',
                          return_value=(self.telem, 8)), \
                patch.object(remote_web.ac_transport,
                             'send_pip_command') as send_pip:
            ok, _, _ = remote_web.apply_pip_action({
                'action': 'select', 'driver': 4,
            })
            self.assertFalse(ok)
            ok, _, _ = remote_web.apply_pip_action({
                'action': 'enable', 'enabled': 'false',
            })
            self.assertFalse(ok)
            ok, _, _ = remote_web.apply_pip_action({
                'action': 'camera', 'delta': 2,
            })
            self.assertFalse(ok)
            send_pip.assert_not_called()

    def test_handler_broadcasts_confirmed_state(self):
        confirmed = {
            'enabled': True, 'driver': 4, 'camera': 0, 'camera_count': 3,
        }
        with remote_web.app.test_request_context('/'), \
                patch.object(remote_web, 'apply_pip_action',
                             return_value=(True, confirmed, None)), \
                patch.object(remote_web, 'emit') as emit:
            remote_web.handle_pip_control({'action': 'select', 'driver': 4})

        emit.assert_called_once_with(
            'pip_status', {'ok': True, 'pip': confirmed, 'error': None},
            broadcast=True)

    def test_new_generation_synchronizes_complete_state(self):
        remote_web._pip_state.update(
            enabled=True, driver=4, camera=2, camera_count=3)
        with patch.object(remote_web.ac_transport, 'send_pip_command',
                          return_value=True) as send_pip:
            self.assertTrue(remote_web.sync_pip_state(self.telem, 11))

        send_pip.assert_called_once_with(
            True, 4, 2, expected_generation=11)

    def test_update_payload_contains_pip_and_per_driver_camera_count(self):
        self.telem.track_length = 5000.0
        car = self.telem.cars[4]
        car.position = 1
        car.driver_name = 'PiP Driver'
        data = remote_web.build_update_data(self.telem)

        driver = next(d for d in data['drivers'] if d['num'] == 5)
        self.assertEqual(driver['camera_count'], 3)
        self.assertEqual(data['pip'], remote_web.get_pip_state())


class PiPMarkupTest(unittest.TestCase):
    def setUp(self):
        self.html = remote_web.app.test_client().get('/').get_data(as_text=True)

    def test_page_has_independent_pip_controls(self):
        self.assertIn('id="pip-toggle"', self.html)
        self.assertIn('id="pip-camera-prev"', self.html)
        self.assertIn('id="pip-camera-next"', self.html)
        self.assertIn('id="pip-readout"', self.html)
        self.assertIn("socket.emit('pip_control'", self.html)

    def test_driver_rows_have_a_dedicated_pip_action(self):
        self.assertIn('data-pip-driver', self.html)
        self.assertIn("e.target.closest('button[data-pip-driver]')", self.html)

    def test_pip_action_cannot_fall_through_to_main_selection(self):
        handler = re.search(
            r"var pipButton = e\.target\.closest\('button\[data-pip-driver\]'\);"
            r"(.*?)var card = e\.target\.closest\('\.driver-item'\);",
            self.html, re.S)
        self.assertIsNotNone(handler)
        self.assertIn('e.preventDefault();', handler.group(1))
        self.assertIn('e.stopPropagation();', handler.group(1))
        self.assertIn('return;', handler.group(1))


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
        remote_web._pip_state = {
            'enabled': True, 'driver': 7, 'camera': 2, 'camera_count': 3,
        }
        remote_web._prev_rollover_state[0] = True
        remote_web.event_log.mark(0, name='Old Driver')

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
        self.assertEqual(remote_web.get_pip_state(), {
            'enabled': False, 'driver': -1, 'camera': 0, 'camera_count': 0,
        })
        self.assertEqual(remote_web._prev_rollover_state, {})
        self.assertEqual(remote_web.event_log.snapshot(), [])

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
        self.assertEqual(ev['seek_frame'], 3880)
        self.assertEqual(ev['seek_s'], 100.0)
        self.assertEqual(ev['id'], 3)

    def test_review_event_seek_falls_back_to_frame(self):
        ev = remote_web._review_event_from_record(
            {'replay_frame': 4000, 'kind': 'collision', 'label': 'HIT'},
            1, 25.0)

        self.assertEqual(ev['frame'], 4000)
        self.assertEqual(ev['seek_frame'], 3880)
        self.assertEqual(ev['seek_s'], 100.0)

    def test_review_event_lead_in_clamps_to_first_frame(self):
        ev = remote_web._review_event_from_record(
            {'replay_frame': 50, 'kind': 'collision', 'label': 'HIT'},
            1, 25.0)

        self.assertEqual(ev['frame'], 50)
        self.assertEqual(ev['seek_frame'], 0)

    def test_review_event_keeps_the_recorded_leader_lap(self):
        ev = remote_web._review_event_from_record(
            {'replay_frame': 50, 'leader_lap': 8,
             'time_remaining_ms': 3723000}, 1, 25.0)

        self.assertEqual(ev['lap'], 8)
        self.assertEqual(ev['time_remaining_ms'], 3723000)

    def test_review_event_does_not_mislabel_a_legacy_driver_lap(self):
        ev = remote_web._review_event_from_record(
            {'replay_frame': 50, 'lap': 7}, 1, 25.0)

        self.assertIsNone(ev['lap'])

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

    def test_enter_review_merges_fragments_and_remaps_a_wrapped_replay(self):
        telem = TelemetryPage()
        telem.replay_frames = 4000
        telem.replay_frame_ms = 25.0
        session = {
            'file': 'part-2.jsonl',
            'files': ['part-1.jsonl', 'part-2.jsonl'],
            'replay_frame_ms': 25.0,
        }
        records = {
            'part-1.jsonl': [
                {'t': 10.0, 'kind': 'collision', 'replay_frame': 900,
                 'time_remaining_ms': 200000.0},
                {'t': 20.0, 'kind': 'collision', 'replay_frame': 1100,
                 'time_remaining_ms': 195000.0},
            ],
            'part-2.jsonl': [
                {'t': 25.0, 'kind': 'mark', 'replay_frame': 3990,
                 'time_remaining_ms': 100000.0},
                {'t': 30.0, 'kind': 'overtake', 'replay_frame': 4000,
                 'time_remaining_ms': 99750.0},
                {'t': 35.0, 'kind': 'overtake', 'replay_frame': 4000,
                 'time_remaining_ms': 74750.0},
            ],
        }

        with patch.object(remote_web.event_journal, 'list_sessions',
                          return_value=[session]), \
                patch.object(remote_web.event_journal, 'load',
                             side_effect=lambda name: records[name]), \
                patch.object(remote_web, 'match_replay',
                             return_value=(session, 'timeline')), \
                patch.object(remote_web, 'read_replay_header', return_value={
                    'version': 16, 'frame_ms': 25.0,
                    'recording_index': 1000, 'frames': 4000,
                }):
            remote_web.enter_review('C:/saved.acreplay', telem)

        events = remote_web.review_journal['events']
        self.assertEqual([event['frame'] for event in events],
                         [100, 2990, 3000])
        self.assertEqual([event['id'] for event in events], [1, 2, 3])

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
                        'car_id': 0, 'frame': 400, 'seek_frame': 280}],
        }
        with patch.object(remote_web, 'send_replay_command',
                          return_value=True) as send, \
                patch.object(remote_web, 'emit'):
            remote_web.handle_jump_to_event({'id': 1})

        send.assert_called_once_with(
            remote_web.REPLAY_SEEK_FRAME, frame=280, driver=0, camera=1)

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
