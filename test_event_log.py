import unittest

from event_log import EventLog


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def car(car_id, position=1, **kw):
    base = {
        'car_id': car_id,
        'position': position,
        'is_connected': 1,
        'is_in_pit': 0,
        'is_colliding': 0,
        'is_rolled_over': 0,
        'display_name': 'Driver {}'.format(car_id),
    }
    base.update(kw)
    return base


class CollisionTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.log = EventLog(clock=self.clock)

    def test_collision_flag_logs_one_event(self):
        events = self.log.observe([car(0, 1, is_colliding=1)])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['kind'], 'collision')
        self.assertEqual(events[0]['label'], 'HIT')
        self.assertEqual(events[0]['car_id'], 0)

    def test_sustained_collision_is_gated_by_cooldown(self):
        self.log.observe([car(0, 1, is_colliding=1)])
        self.clock.advance(0.1)
        self.assertEqual(self.log.observe([car(0, 1, is_colliding=1)]), [])

        self.clock.advance(EventLog.COOLDOWN_S)
        self.assertEqual(len(self.log.observe([car(0, 1, is_colliding=1)])), 1)

    def test_disconnected_cars_are_ignored(self):
        events = self.log.observe([car(0, 1, is_colliding=1, is_connected=0)])
        self.assertEqual(events, [])


class RolloverTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.log = EventLog(clock=self.clock)

    def test_only_the_rising_edge_logs(self):
        events = self.log.observe([car(0, 1, is_rolled_over=1)])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['kind'], 'rollover')

        # Still flipped many ticks later — one incident, one event.
        for _ in range(5):
            self.clock.advance(2.0)
            self.assertEqual(self.log.observe([car(0, 1, is_rolled_over=1)]), [])

    def test_recovery_then_flip_again_logs_a_second_event(self):
        self.log.observe([car(0, 1, is_rolled_over=1)])
        self.clock.advance(EventLog.COOLDOWN_S + 1)
        self.log.observe([car(0, 1, is_rolled_over=0)])
        self.clock.advance(1.0)
        events = self.log.observe([car(0, 1, is_rolled_over=1)])
        self.assertEqual(len(events), 1)


class OvertakeTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.log = EventLog(clock=self.clock)

    def test_position_gain_logs_an_overtake(self):
        self.log.observe([car(0, 1), car(1, 2)])
        self.clock.advance(1.0)
        events = self.log.observe([car(1, 1), car(0, 2)])
        self.assertEqual([e['kind'] for e in events], ['overtake'])
        self.assertEqual(events[0]['car_id'], 1)

    def test_first_sighting_of_a_car_never_logs(self):
        self.assertEqual(self.log.observe([car(0, 1), car(1, 2)]), [])

    def test_position_inherited_from_a_pitting_car_is_not_an_overtake(self):
        self.log.observe([car(0, 1), car(1, 2)])
        self.clock.advance(1.0)
        # Car 0 peels into the pits; car 1 gains P1 without passing anyone.
        events = self.log.observe([car(1, 1), car(0, 2, is_in_pit=1)])
        self.assertEqual(events, [])

    def test_a_car_gaining_while_in_the_pits_is_not_an_overtake(self):
        self.log.observe([car(0, 1), car(1, 2)])
        self.clock.advance(1.0)
        events = self.log.observe([car(1, 1, is_in_pit=1), car(0, 2)])
        self.assertEqual(events, [])

    def test_overtakes_can_be_switched_off(self):
        # Practice/qualifying: a position gain is timing-screen shuffling,
        # not a pass — but contact still matters.
        self.log.observe([car(0, 1), car(1, 2)], detect_overtakes=False)
        self.clock.advance(1.0)
        events = self.log.observe(
            [car(1, 1, is_colliding=1), car(0, 2)], detect_overtakes=False)
        self.assertEqual([e['kind'] for e in events], ['collision'])

    def test_position_state_is_tracked_even_while_gated(self):
        # The gate is per-call, so a race running after the server saw only
        # gated ticks must detect on its second tick, not rebuild history.
        self.log.observe([car(0, 1), car(1, 2)], detect_overtakes=False)
        self.clock.advance(1.0)
        events = self.log.observe([car(1, 1), car(0, 2)])
        self.assertEqual([e['kind'] for e in events], ['overtake'])


class MarkTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.log = EventLog(clock=self.clock)

    def test_mark_logs_immediately(self):
        event = self.log.mark(3, name='Alex Driver')
        self.assertEqual(event['kind'], 'mark')
        self.assertEqual(event['label'], 'MRK')
        self.assertNotIn('car_number', event)

    def test_double_tap_is_swallowed(self):
        self.log.mark(3)
        self.clock.advance(0.3)
        self.assertIsNone(self.log.mark(3))
        self.clock.advance(EventLog.MARK_COOLDOWN_S)
        self.assertIsNotNone(self.log.mark(3))


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.log = EventLog(clock=self.clock)

    def test_snapshot_is_newest_first_and_carries_age(self):
        self.log.mark(0)
        self.clock.advance(10.0)
        self.log.mark(1)

        snap = self.log.snapshot()
        self.assertEqual([e['car_id'] for e in snap], [1, 0])
        self.assertAlmostEqual(snap[0]['age'], 0.0)
        self.assertAlmostEqual(snap[1]['age'], 10.0)

    def test_events_past_the_replay_buffer_are_dropped(self):
        self.log.mark(0)
        self.clock.advance(EventLog.BUFFER_S + 1)
        self.assertEqual(self.log.snapshot(), [])
        self.assertIsNone(self.log.get(1))

    def test_a_measured_window_keeps_events_the_default_would_drop(self):
        self.log.set_window(1800.0)
        self.log.mark(0)
        self.clock.advance(EventLog.BUFFER_S + 1)

        self.assertEqual(len(self.log.snapshot()), 1)

    def test_window_is_clamped_and_bad_values_are_ignored(self):
        self.log.set_window(10.0)
        self.assertEqual(self.log.window_s, EventLog.MIN_WINDOW_S)

        self.log.set_window(999999.0)
        self.assertEqual(self.log.window_s, EventLog.MAX_WINDOW_S)

        # A zeroed telemetry page must not reset the window.
        self.log.set_window(0)
        self.assertEqual(self.log.window_s, EventLog.MAX_WINDOW_S)
        self.log.set_window(None)
        self.assertEqual(self.log.window_s, EventLog.MAX_WINDOW_S)

    def test_ring_caps_at_max_events(self):
        for i in range(EventLog.MAX_EVENTS + 10):
            self.log.mark(i)
        self.assertEqual(len(self.log.snapshot()), EventLog.MAX_EVENTS)

    def test_get_returns_the_event_for_a_jump(self):
        event = self.log.mark(7)
        found = self.log.get(event['id'])
        self.assertEqual(found['car_id'], 7)
        self.assertEqual(found['t'], event['t'])

    def test_clear_drops_everything(self):
        self.log.mark(0)
        self.log.clear()
        self.assertEqual(self.log.snapshot(), [])


if __name__ == '__main__':
    unittest.main()
