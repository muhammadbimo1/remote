import unittest
from unittest.mock import patch

from ipc_shared import TelemetryPage, CommandPage
import remote_web
from remote_web import apply_timetable_offsets, build_update_data


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
        car.driver_name = "23 | Alex Driver"
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


class CarNumberTest(unittest.TestCase):
    def test_car_number_comes_from_name_prefix(self):
        self.assertEqual(remote_web._parse_driver_name("23 | Alex Driver"),
                         (23, "Alex Driver"))

    def test_car_number_is_none_without_a_numeric_prefix(self):
        cases = ["Alex Driver", "Alex | Driver", " | Alex Driver"]
        for raw_name in cases:
            with self.subTest(raw_name=raw_name):
                car_number, _ = remote_web._parse_driver_name(raw_name)
                self.assertIsNone(car_number)

    def test_payload_car_number_is_null_and_not_the_slot_index(self):
        telem = TelemetryPage()
        telem.car_count = 1
        telem.track_length = 5000.0

        car = telem.cars[0]
        car.car_id = 4
        car.position = 1
        car.is_connected = 1
        car.driver_name = "Unnumbered Driver"

        driver = build_update_data(telem)["drivers"][0]

        self.assertIsNone(driver["car_number"])
        self.assertEqual(driver["num"], 5)


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

    def test_replay_payload_without_a_live_snapshot_is_empty_not_crashing(self):
        telem = self._live_telem()
        telem.is_replay = 1

        data = remote_web.build_replay_update_data(telem)

        self.assertEqual(data["drivers"], [])
        self.assertTrue(data["ac_connected"])


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
        page = CommandPage()
        with patch.object(remote_web, 'command_page', page), \
                patch.object(remote_web, 'command_mmap', object()):
            before = page.command_seq
            ok = remote_web.send_replay_command(
                remote_web.REPLAY_ENTER, rewind_s=12.5, driver=3, camera=1)

        self.assertTrue(ok)
        self.assertEqual(page.command_seq, before)
        self.assertEqual(page.replay_action, remote_web.REPLAY_ENTER)
        self.assertAlmostEqual(page.replay_rewind_s, 12.5, places=3)
        self.assertEqual(page.target_driver, 3)
        self.assertEqual(page.target_camera, 1)
        self.assertNotEqual(page.replay_seq, 0)


if __name__ == "__main__":
    unittest.main()
