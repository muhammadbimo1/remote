"""
Automated broadcast director for Assetto Corsa.
Scores cars by interest (battles, collisions, overtakes) and issues
camera-cut commands. Class-aware and adaptive to sprint vs endurance races.
"""
import time
from collections import defaultdict


class AutoDirector:
    def __init__(self):
        self.enabled = False
        self.current_focus = None        # car_id currently shown
        self.focus_start = 0.0           # when we cut to current car
        self.last_cut_time = 0.0
        self.min_dwell = 24.0             # current min dwell (varies by reason)

        # Previous frame state (keyed by car_id)
        self.prev_positions = {}
        self.prev_class_positions = {}
        self.prev_in_pit = {}
        self.prev_best_lap = {}

        # Event cooldown timestamps (keyed by car_id)
        self.collision_seen = {}
        self.position_change_seen = {}
        self.class_position_change_seen = {}
        self.fast_lap_seen = {}

        # Per-class tracking
        self.class_best_laps = {}          # {class_name: best_lap_ms}
        self.last_class_leader_shown = {}  # {class_name: monotonic_time}

        # Global
        self.overall_best_lap = 0
        self.last_leader_show = 0.0
        self.last_shown = {}               # {car_id: monotonic_time}
        self.endurance_mode = False

    def tick(self, cars, track_length):
        """Called every telemetry cycle (~100ms).

        Args:
            cars: list of car dicts from compute_gaps(), sorted by position,
                  with interval_seconds, class_interval_seconds, etc.
            track_length: track length in meters.

        Returns:
            dict {'driver': car_id} if a cut should happen, or None.
        """
        if not cars:
            return None

        now = time.monotonic()
        connected = [c for c in cars if c.get('is_connected') and not c.get('is_in_pit')]
        if not connected:
            connected = [c for c in cars if c.get('is_connected')]
        if not connected:
            return None

        self._detect_endurance(cars)
        self._detect_events(cars, now)

        scores = []
        for car in connected:
            score, reason = self._score_car(car, cars, now)
            scores.append((car, score, reason))

        result = self._pick_best(scores, now)
        self._update_prev(cars)
        return result

    # ------------------------------------------------------------------
    # Event detection
    # ------------------------------------------------------------------

    def _detect_endurance(self, cars):
        """Auto-detect sprint vs endurance from field spread."""
        if len(cars) < 2:
            self.endurance_mode = False
            return
        leader = cars[0]
        leader_progress = leader.get('total_progress', 0)
        lapped = sum(
            1 for c in cars[1:]
            if c.get('is_connected')
            and leader_progress - c.get('total_progress', 0) >= 1.0
        )
        connected = sum(1 for c in cars if c.get('is_connected'))
        self.endurance_mode = connected > 2 and lapped / connected > 0.3

    def _detect_events(self, cars, now):
        """Compare current frame to previous, populate event cooldowns."""
        for car in cars:
            cid = car['car_id']
            if not car.get('is_connected'):
                continue

            # Collision (persist the transient flag)
            if car.get('is_colliding'):
                self.collision_seen[cid] = now

            # Overall position change (improvement = overtake)
            prev_pos = self.prev_positions.get(cid)
            cur_pos = car.get('position')
            if prev_pos is not None and cur_pos is not None and cur_pos < prev_pos:
                self.position_change_seen[cid] = now

            # Class position change
            prev_cls_pos = self.prev_class_positions.get(cid)
            cur_cls_pos = car.get('class_position')
            if prev_cls_pos is not None and cur_cls_pos is not None and cur_cls_pos < prev_cls_pos:
                self.class_position_change_seen[cid] = now

            # Fast lap (personal best)
            best = car.get('best_lap', 0)
            prev_best = self.prev_best_lap.get(cid, 0)
            if best > 0 and prev_best > 0 and best < prev_best:
                self.fast_lap_seen[cid] = now

            # Track class best laps
            cls = car.get('car_class', 'Unclassed')
            if best > 0:
                if cls not in self.class_best_laps or best < self.class_best_laps[cls]:
                    self.class_best_laps[cls] = best

            # Track overall best lap
            if best > 0 and (self.overall_best_lap == 0 or best < self.overall_best_lap):
                self.overall_best_lap = best

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def _score_car(self, car, all_cars, now):
        """Compute interest score for a single car. Returns (score, reason)."""
        cid = car['car_id']
        score = 0.0
        reason = 'default'

        # --- Collision (highest priority) ---
        col_time = self.collision_seen.get(cid)
        if col_time and now - col_time < 3.0:
            col_score = 100 * (1.0 - (now - col_time) / 3.0)
            if col_score > score:
                score = col_score
                reason = 'collision'

        # --- Overall position change ---
        pc_time = self.position_change_seen.get(cid)
        if pc_time and now - pc_time < 5.0:
            pc_score = 70 * (1.0 - (now - pc_time) / 5.0)
            if pc_score > score:
                score = pc_score
                reason = 'overtake'

        # --- Class position change ---
        cpc_time = self.class_position_change_seen.get(cid)
        if cpc_time and now - cpc_time < 5.0:
            cpc_score = 60 * (1.0 - (now - cpc_time) / 5.0)
            score += cpc_score
            if cpc_score > 50 and reason == 'default':
                reason = 'class_overtake'

        # --- Class battle (same class, tight interval) ---
        cls_int = car.get('class_interval_seconds', float('inf'))
        if cls_int < 1.5:
            battle_score = 60 * (1.0 - cls_int / 1.5)
            score += battle_score
            if battle_score > 20 and reason in ('default',):
                reason = 'class_battle'

        # --- Overall battle (different class proximity on track) ---
        ovr_int = car.get('interval_seconds', float('inf'))
        if ovr_int < 1.5:
            ovr_battle = 40 * (1.0 - ovr_int / 1.5)
            score += ovr_battle
            if reason == 'default' and ovr_battle > 15:
                reason = 'battle'

        # --- Fast lap ---
        fl_time = self.fast_lap_seen.get(cid)
        if fl_time and now - fl_time < 5.0:
            fl_score = 40 * (1.0 - (now - fl_time) / 5.0)
            # Bonus if it's the class best
            best = car.get('best_lap', 0)
            cls = car.get('car_class', 'Unclassed')
            if best > 0 and best <= self.class_best_laps.get(cls, float('inf')):
                fl_score += 20 * (1.0 - (now - fl_time) / 5.0)
            score += fl_score
            if fl_score > 30 and reason == 'default':
                reason = 'fast_lap'

        # --- Class leader bonus ---
        cls_pos = car.get('class_position', 999)
        cls = car.get('car_class', 'Unclassed')
        if cls_pos == 1:
            cls_leader_age = now - self.last_class_leader_shown.get(cls, 0)
            bonus = 25 if cls_leader_age > 45 else 15
            score += bonus
            if reason == 'default':
                reason = 'class_leader'

        # --- Overall leader bonus ---
        if car.get('position') == 1:
            leader_age = now - self.last_leader_show
            bonus = 25 if leader_age > 30 else 15
            score += bonus
            if reason == 'default':
                reason = 'leader'

        # --- Front-runner bonus (class P2-P5) ---
        if 2 <= cls_pos <= 5:
            score += max(0, 10 - cls_pos)

        # --- Staleness penalty (currently focused car) ---
        if cid == self.current_focus:
            focused_time = now - self.focus_start
            over_dwell = focused_time - self.min_dwell
            if over_dwell > 0:
                score -= 5 * over_dwell

        # --- Recency penalty (recently shown) ---
        last = self.last_shown.get(cid)
        if last and cid != self.current_focus:
            age = now - last
            if age < 20:
                score -= 15 * (1.0 - age / 20.0)

        return score, reason

    # ------------------------------------------------------------------
    # Cut decision
    # ------------------------------------------------------------------

    def _pick_best(self, scores, now):
        """Apply hysteresis and timing rules. Returns command dict or None."""
        if not scores:
            return None

        scores.sort(key=lambda x: x[1], reverse=True)
        best_car, best_score, best_reason = scores[0]

        elapsed = now - self.last_cut_time if self.last_cut_time else float('inf')

        # Collision override (1s floor)
        if best_reason == 'collision' and best_score > 80 and elapsed > 1.0:
            return self._do_cut(best_car, best_reason, now)

        # Respect minimum dwell
        if elapsed < self.min_dwell:
            return None

        # First cut (no current focus)
        if self.current_focus is None:
            return self._do_cut(best_car, best_reason, now)

        # Hysteresis: need 10+ points over current car's score
        current_score = 0
        for car, sc, _ in scores:
            if car['car_id'] == self.current_focus:
                current_score = sc
                break

        if best_car['car_id'] != self.current_focus and best_score > current_score + 10:
            return self._do_cut(best_car, best_reason, now)

        return None

    def _do_cut(self, car, reason, now):
        """Record cut and return command."""
        cid = car['car_id']

        # Track when we last showed each car
        if self.current_focus is not None:
            self.last_shown[self.current_focus] = now

        self.current_focus = cid
        self.focus_start = now
        self.last_cut_time = now

        # Track leader/class leader show times
        if car.get('position') == 1:
            self.last_leader_show = now
        cls = car.get('car_class', 'Unclassed')
        if car.get('class_position') == 1:
            self.last_class_leader_shown[cls] = now

        # Set min dwell based on reason and mode
        mult = 1.5 if self.endurance_mode else 1.0
        dwell_map = {
            'collision': 4,
            'overtake': 5,
            'class_overtake': 5,
            'class_battle': 6,
            'battle': 6,
            'fast_lap': 5,
            'leader': 7,
            'class_leader': 7,
            'default': 7,
        }
        self.min_dwell = dwell_map.get(reason, 7) * mult

        return {'driver': cid}

    # ------------------------------------------------------------------
    # State update
    # ------------------------------------------------------------------

    def _update_prev(self, cars):
        """Store current frame as previous for next tick."""
        self.prev_positions = {}
        self.prev_class_positions = {}
        self.prev_in_pit = {}
        self.prev_best_lap = {}
        for car in cars:
            cid = car['car_id']
            self.prev_positions[cid] = car.get('position')
            self.prev_class_positions[cid] = car.get('class_position')
            self.prev_in_pit[cid] = car.get('is_in_pit')
            self.prev_best_lap[cid] = car.get('best_lap', 0)

        # Prune old cooldowns (> 10s)
        now = time.monotonic()
        for d in (self.collision_seen, self.position_change_seen,
                  self.class_position_change_seen, self.fast_lap_seen):
            stale = [k for k, v in d.items() if now - v > 10]
            for k in stale:
                del d[k]
