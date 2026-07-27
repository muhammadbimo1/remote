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


if __name__ == "__main__":
    unittest.main()
