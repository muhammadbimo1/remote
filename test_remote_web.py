import unittest
from unittest.mock import patch

from ipc_shared import TelemetryPage
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


if __name__ == "__main__":
    unittest.main()
