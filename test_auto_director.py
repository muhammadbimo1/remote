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


class AutoDirectorOvertakeEligibilityTest(unittest.TestCase):
    def setUp(self):
        self.director = AutoDirector()

    def _tick(self, cars, now, is_race):
        try:
            with patch('auto_director.time.monotonic', return_value=now):
                return self.director.tick(
                    cars, track_length=5000.0, is_race=is_race)
        except TypeError as exc:
            self.fail('AutoDirector.tick must accept explicit race context: {}'.format(exc))

    def test_qualifying_rank_gain_is_not_an_overtake(self):
        self._tick([make_car(0, 1), make_car(1, 2)], 1.0, is_race=False)
        self._tick([make_car(1, 1), make_car(0, 2)], 2.0, is_race=False)

        self.assertNotIn(1, self.director.position_change_seen)

    def test_position_inherited_from_pitting_car_is_not_an_overtake(self):
        self._tick([make_car(0, 1), make_car(1, 2)], 1.0, is_race=True)
        pitting = make_car(0, 2)
        pitting['is_in_pit'] = True
        self._tick([make_car(1, 1), pitting], 2.0, is_race=True)

        self.assertNotIn(1, self.director.position_change_seen)

    def test_genuine_race_pass_is_an_overtake(self):
        self._tick([make_car(0, 1), make_car(1, 2)], 1.0, is_race=True)
        self._tick([make_car(1, 1), make_car(0, 2)], 2.0, is_race=True)

        self.assertEqual(self.director.position_change_seen.get(1), 2.0)


class AutoDirectorIncidentCoverageTest(unittest.TestCase):
    def _tick(self, director, cars, now):
        with patch('auto_director.time.monotonic', return_value=now):
            return director.tick(cars, track_length=5000.0, is_race=True)

    def test_sustained_incident_refreshes_coverage_without_recut(self):
        director = AutoDirector()
        incident = make_car(1, 1)
        incident['is_colliding'] = True

        self.assertEqual(self._tick(director, [incident], 1.0), {'driver': 1})
        self.assertTrue(hasattr(director, 'incident_coverage_until'))
        original_focus_start = director.focus_start
        original_coverage = director.incident_coverage_until

        self.assertIsNone(self._tick(director, [incident], 2.1))
        self.assertEqual(director.focus_start, original_focus_start)
        self.assertGreater(director.incident_coverage_until, original_coverage)

    def test_equal_priority_incident_waits_for_coverage_to_expire(self):
        director = AutoDirector()
        first = make_car(0, 1)
        first['is_colliding'] = True
        self.assertEqual(self._tick(director, [first, make_car(1, 2)], 1.0),
                         {'driver': 0})

        second = make_car(1, 2)
        second['is_colliding'] = True
        self.assertIsNone(self._tick(
            director, [make_car(0, 1), second], 2.1))
        self.assertEqual(self._tick(
            director, [make_car(0, 1), second], 11.1), {'driver': 1})

    def test_rollover_escalates_protected_collision(self):
        director = AutoDirector()
        collision = make_car(0, 1)
        collision['is_colliding'] = True
        self.assertEqual(self._tick(
            director, [collision, make_car(1, 2)], 1.0), {'driver': 0})

        rollover = make_car(1, 2)
        rollover['is_rolled_over'] = True
        self.assertEqual(self._tick(
            director, [make_car(0, 1), rollover], 2.1), {'driver': 1})
        self.assertTrue(hasattr(director, 'incident_reason'))
        self.assertTrue(hasattr(director, 'incident_focus'))
        self.assertEqual(director.incident_reason, 'rollover')
        self.assertEqual(director.incident_focus, 1)

    def test_battle_points_cannot_promote_weak_collision_override(self):
        director = AutoDirector()
        director.current_focus = 0
        director.focus_start = 1.0
        director.last_cut_time = 1.0
        director.min_dwell = 24.0
        director.collision_seen[1] = 0.1

        current = make_car(0, 1)
        candidate = make_car(
            1, 2, class_position=2, interval=0.0, class_interval=0.0)
        self.assertIsNone(self._tick(director, [current, candidate], 3.0))


class AutoDirectorBattleContinuityTest(unittest.TestCase):
    @staticmethod
    def _physical(car, ahead=99.0, behind=99.0,
                  class_ahead=99.0, class_behind=99.0, opponents=None):
        car['battle_gap_ahead_seconds'] = ahead
        car['battle_gap_behind_seconds'] = behind
        car['class_battle_gap_ahead_seconds'] = class_ahead
        car['class_battle_gap_behind_seconds'] = class_behind
        car['battle_opponent_ids'] = list(opponents or [])
        return car

    def test_defender_receives_battle_score_from_car_behind(self):
        director = AutoDirector()
        defender = self._physical(
            make_car(0, 1), behind=0.1, class_behind=0.1, opponents=[1])

        _score, reason, parts = director._score_car(defender, [defender], 5.0)

        self.assertEqual(reason, 'class_battle')
        self.assertGreater(parts.get('class_battle', 0), 0)

    def test_same_battle_stays_protected_after_order_swap(self):
        director = AutoDirector()
        defender = self._physical(
            make_car(0, 1), behind=0.2, class_behind=0.2, opponents=[1])
        director._do_cut(defender, 'class_battle', 1.0)

        swapped = self._physical(
            make_car(0, 2), ahead=1.8, class_ahead=1.8, opponents=[1])
        _score, _reason, parts = director._score_car(swapped, [swapped], 20.0)

        self.assertNotIn('staleness', parts)
        self.assertEqual(director.current_battle_opponents, {1})


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
        director.incident_focus = 9
        director.incident_reason = 'collision'
        director.incident_coverage_until = 24.0
        director.current_battle_opponents = {8}
        director.battle_last_active = 14.0
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
        self.assertIsNone(director.incident_focus)
        self.assertIsNone(director.incident_reason)
        self.assertEqual(director.incident_coverage_until, 0.0)
        self.assertEqual(director.current_battle_opponents, set())
        self.assertEqual(director.battle_last_active, 0.0)

if __name__ == "__main__":
    unittest.main()
