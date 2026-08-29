import unittest
from unittest.mock import patch

from auto_director import AutoDirector


def make_car(car_id, position, class_position=None, interval=99.0, class_interval=99.0):
    return {
        'car_id': car_id,
        'driver_name': f'Driver {car_id}',
        'is_connected': True,
        'is_in_pit': False,
        'position': position,
        'class_position': class_position if class_position is not None else position,
        'interval_seconds': interval,
        'class_interval_seconds': class_interval,
        'car_class': 'GT3',
        'best_lap': 0,
        'total_progress': 10.0 - position,
    }


class AutoDirectorIdleModeTest(unittest.TestCase):
    def setUp(self):
        self.director = AutoDirector()
        self.director.current_focus = 2
        self.director.focus_start = 1.0
        self.director.last_cut_time = 1.0
        self.director.min_dwell = 8.0

    def test_idle_front_runner_does_not_replace_stale_focus_before_max_idle_hold(self):
        cars = [
            make_car(0, 1),
            make_car(1, 2),
            make_car(2, 3),
        ]

        with patch('auto_director.time.monotonic', return_value=40.0):
            self.assertIsNone(self.director.tick(cars, track_length=5000.0))

    def test_idle_front_runner_can_rotate_after_max_idle_hold(self):
        cars = [
            make_car(0, 1),
            make_car(1, 2),
            make_car(2, 3),
        ]

        with patch('auto_director.time.monotonic', return_value=50.0):
            self.assertEqual(
                self.director.tick(cars, track_length=5000.0),
                {'driver': 0},
            )

    def test_real_battle_can_replace_stale_focus_after_min_dwell(self):
        cars = [
            make_car(0, 1, class_interval=0.4),
            make_car(1, 2),
            make_car(2, 3),
        ]

        with patch('auto_director.time.monotonic', return_value=12.0):
            self.assertEqual(
                self.director.tick(cars, track_length=5000.0),
                {'driver': 0},
            )


class AutoDirectorRunResetTest(unittest.TestCase):
    def test_reset_clears_run_state_but_preserves_operator_settings(self):
        director = AutoDirector()
        director.enabled = True
        director.debug = True
        director.current_focus = 9
        director.focus_start = 12.0
        director.last_cut_time = 13.0
        director.prev_positions = {9: 1}
        director.prev_class_positions = {9: 1}
        director.prev_in_pit = {9: False}
        director.prev_best_lap = {9: 90000}
        director.collision_seen = {9: 14.0}
        director.position_change_seen = {9: 14.0}
        director.class_position_change_seen = {9: 14.0}
        director.fast_lap_seen = {9: 14.0}
        director.rollover_active = {9: 14.0}
        director.class_best_laps = {'GT3': 90000}
        director.last_class_leader_shown = {'GT3': 14.0}
        director.overall_best_lap = 90000
        director.last_leader_show = 14.0
        director.last_shown = {9: 14.0}
        director.endurance_mode = True
        director._last_debug_log = 14.0

        director.reset_run_state()

        self.assertTrue(director.enabled)
        self.assertTrue(director.debug)
        self.assertIsNone(director.current_focus)
        self.assertEqual(director.focus_start, 0.0)
        self.assertEqual(director.last_cut_time, 0.0)
        for value in (
                director.prev_positions, director.prev_class_positions,
                director.prev_in_pit, director.prev_best_lap,
                director.collision_seen, director.position_change_seen,
                director.class_position_change_seen, director.fast_lap_seen,
                director.rollover_active, director.class_best_laps,
                director.last_class_leader_shown, director.last_shown):
            self.assertEqual(value, {})
        self.assertEqual(director.overall_best_lap, 0)
        self.assertEqual(director.last_leader_show, 0.0)
        self.assertFalse(director.endurance_mode)
        self.assertEqual(director._last_debug_log, 0.0)

if __name__ == "__main__":
    unittest.main()
