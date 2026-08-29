"""
Rolling log of race incidents worth replaying.

Fed one telemetry tick at a time from remote_web.py's monitor thread. Detection
mirrors AutoDirector._detect_events, but this class keeps its own previous-tick
state on purpose: the log has to fill whether or not the auto-director is
enabled, and it needs to be testable without the scoring machinery.

Events are timestamped with wall-clock seconds. Jumping to one rewinds AC's
instant replay by (now - event time), so anything older than the usable replay
buffer is dropped rather than offered as a dead button.
"""
import threading
import time
from collections import deque


class EventLog:
    MAX_EVENTS = 40
    # Fallback replay window in seconds, used until AC reports how much it has
    # actually recorded. AC's buffer is sized in MB (Documents/Assetto Corsa/
    # cfg/replay.ini, MAX_SIZE_MB), so its length in seconds depends on the
    # grid size — never assume this constant is the real figure.
    BUFFER_S = 300.0
    # Bounds for the measured window, so a garbage frame count can't shrink the
    # log to nothing or keep events that are far past any usable rewind.
    MIN_WINDOW_S = 60.0
    MAX_WINDOW_S = 3600.0
    # Per-car, per-kind gate. Contact tends to arrive as a burst of ticks.
    COOLDOWN_S = 5.0
    # Shorter gate for the operator's MARK button: only there to swallow a
    # double tap, not to stop deliberate repeat marks.
    MARK_COOLDOWN_S = 2.0

    KIND_LABELS = {
        'collision': 'HIT',
        'rollover': 'FLP',
        'overtake': 'OVT',
        'mark': 'MRK',
    }

    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._events = deque(maxlen=self.MAX_EVENTS)
        self._next_id = 1
        # How far back an event stays jumpable. Replaced by AC's actual
        # recorded length as soon as telemetry reports one (set_window).
        self.window_s = self.BUFFER_S

        # Previous-tick state, keyed by car_id
        self._prev_position = {}
        self._prev_rolled = {}

        # Cooldowns, keyed by (kind, car_id)
        self._last_seen = {}

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def observe(self, cars, detect_overtakes=True):
        """Compare this telemetry tick to the previous one. Returns new events.

        `cars` is the list of dicts produced by remote_web.compute_gaps().
        `detect_overtakes` is False for practice/qualifying: positions there
        reshuffle with every lap improvement, so a "position gained" is not a
        pass on track — just timing-screen noise. Collisions, rollovers and
        marks still land.
        """
        now = self._clock()
        new_events = []

        # Who held each position last tick — used to tell a real overtake from
        # a position inherited when the car ahead peeled into the pits.
        prev_holder = {}
        for car in cars:
            prev_pos = self._prev_position.get(car['car_id'])
            if prev_pos is not None:
                prev_holder[prev_pos] = car['car_id']
        in_pit_now = {c['car_id']: bool(c.get('is_in_pit')) for c in cars}

        for car in cars:
            cid = car['car_id']
            if not car.get('is_connected'):
                continue

            if car.get('is_colliding'):
                ev = self._add(now, 'collision', car)
                if ev:
                    new_events.append(ev)

            rolled = bool(car.get('is_rolled_over'))
            if rolled and not self._prev_rolled.get(cid, False):
                ev = self._add(now, 'rollover', car)
                if ev:
                    new_events.append(ev)
            self._prev_rolled[cid] = rolled

            prev_pos = self._prev_position.get(cid)
            cur_pos = car.get('position')
            gained = (prev_pos is not None and cur_pos is not None
                      and cur_pos < prev_pos)
            if detect_overtakes and gained and not car.get('is_in_pit'):
                # The car that used to hold this position pitted: not a pass.
                displaced = prev_holder.get(cur_pos)
                if displaced is None or not in_pit_now.get(displaced, False):
                    ev = self._add(now, 'overtake', car)
                    if ev:
                        new_events.append(ev)

            self._prev_position[cid] = cur_pos

        return new_events

    def mark(self, car_id, name=''):
        """Operator-triggered marker on the currently focused car."""
        now = self._clock()
        car = {'car_id': car_id, 'display_name': name}
        return self._add(now, 'mark', car, cooldown=self.MARK_COOLDOWN_S)

    def _add(self, now, kind, car, cooldown=None):
        cid = car['car_id']
        key = (kind, cid)
        gate = self.COOLDOWN_S if cooldown is None else cooldown
        with self._lock:
            last = self._last_seen.get(key)
            if last is not None and now - last < gate:
                return None
            self._last_seen[key] = now

            event = {
                'id': self._next_id,
                'kind': kind,
                'label': self.KIND_LABELS.get(kind, kind.upper()[:3]),
                'car_id': cid,
                'name': car.get('display_name') or car.get('name') or '',
                't': now,
            }
            self._next_id += 1
            self._events.append(event)
            return event

    # ------------------------------------------------------------------
    # Readout
    # ------------------------------------------------------------------

    def snapshot(self):
        """Newest-first list for the socket payload, with a live `age`.

        Events past the replay buffer are dropped: rewinding that far would be
        refused by AC, so offering the button would be a lie.
        """
        now = self._clock()
        with self._lock:
            self._prune(now)
            out = []
            for ev in self._events:
                item = dict(ev)
                item['age'] = now - ev['t']
                out.append(item)
        out.reverse()
        return out

    def get(self, event_id):
        """Return the event with this id, or None if it expired or never was."""
        now = self._clock()
        with self._lock:
            self._prune(now)
            for ev in self._events:
                if ev['id'] == event_id:
                    return dict(ev)
        return None

    def clear(self):
        """Drop everything — used when AC disconnects, since car_ids get reused."""
        with self._lock:
            self._events.clear()
            self._last_seen.clear()
            self._prev_position.clear()
            self._prev_rolled.clear()

    def set_window(self, seconds):
        """Adopt AC's real recorded replay length as the retention window.

        Called from the telemetry loop with `replay_frames * replay_frame_ms`.
        Returns the window actually applied.
        """
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            return self.window_s
        if seconds <= 0:
            return self.window_s
        self.window_s = max(self.MIN_WINDOW_S, min(seconds, self.MAX_WINDOW_S))
        return self.window_s

    def _prune(self, now):
        while self._events and now - self._events[0]['t'] > self.window_s:
            self._events.popleft()
