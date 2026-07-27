"""
Automated broadcast director for Assetto Corsa.
Scores cars by interest (battles, collisions, overtakes) and issues
camera-cut commands. Class-aware and adaptive to sprint vs endurance races.
"""
import time
from collections import defaultdict


class AutoDirector:
    IDLE_REASONS = {'default', 'leader', 'class_leader', 'front_runner'}

    def __init__(self):
        self.enabled = False
        self.current_focus = None        # car_id currently shown
        self.focus_start = 0.0           # when we cut to current car
        self.last_cut_time = 0.0
        self.min_dwell = 24.0             # current min dwell (varies by reason)
        self.max_idle_dwell = 45.0        # no-action shots can breathe before rotating

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
        # Rollover is sustained, not transient: store last time the flag was
        # observed true. While the car remains flipped, the timestamp is
        # refreshed each tick; once recovered, it decays via _score_car.
        self.rollover_active = {}

        # Per-class tracking
        self.class_best_laps = {}          # {class_name: best_lap_ms}
        self.last_class_leader_shown = {}  # {class_name: monotonic_time}

        # Global
        self.overall_best_lap = 0
        self.last_leader_show = 0.0
        self.last_shown = {}               # {car_id: monotonic_time}
        self.endurance_mode = False

        # Debug logging (toggled at runtime via web UI)
        self.debug = False
        self._last_debug_log = 0.0

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
            score, reason, parts = self._score_car(car, cars, now)
            scores.append((car, score, reason, parts))

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

            # Rollover (refresh while sustained; recovery handled in scoring)
            if car.get('is_rolled_over'):
                self.rollover_active[cid] = now

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
        """Compute interest score for a single car.

        Returns (score, reason, parts) where `parts` is a dict mapping each
        scoring component to the points it contributed (positive or negative).
        """
        cid = car['car_id']
        score = 0.0
        reason = 'default'
        parts = {}

        # --- Rollover (top priority while flipped, decays after recovery) ---
        ro_time = self.rollover_active.get(cid)
        if ro_time and now - ro_time < 6.0:
            if car.get('is_rolled_over'):
                ro_score = 150.0
            else:
                ro_score = 150.0 * (1.0 - (now - ro_time) / 6.0)
            if ro_score > score:
                score = ro_score
                reason = 'rollover'
                parts['rollover'] = ro_score

        # --- Collision (highest priority among contact events) ---
        col_time = self.collision_seen.get(cid)
        if col_time and now - col_time < 3.0:
            col_score = 100 * (1.0 - (now - col_time) / 3.0)
            if col_score > score:
                score = col_score
                reason = 'collision'
                parts['collision'] = col_score

        # --- Overall position change ---
        pc_time = self.position_change_seen.get(cid)
        if pc_time and now - pc_time < 5.0:
            pc_score = 70 * (1.0 - (now - pc_time) / 5.0)
            if pc_score > score:
                score = pc_score
                reason = 'overtake'
                parts['overtake'] = pc_score

        # --- Class position change ---
        cpc_time = self.class_position_change_seen.get(cid)
        if cpc_time and now - cpc_time < 5.0:
            cpc_score = 60 * (1.0 - (now - cpc_time) / 5.0)
            score += cpc_score
            parts['class_overtake'] = cpc_score
            if cpc_score > 50 and reason == 'default':
                reason = 'class_overtake'

        # --- Class battle (same class, tight interval) ---
        cls_int = car.get('class_interval_seconds', float('inf'))
        if cls_int < 1.5:
            battle_score = 60 * (1.0 - cls_int / 1.5)
            score += battle_score
            parts['class_battle'] = battle_score
            if battle_score > 20 and reason in ('default',):
                reason = 'class_battle'

        # --- Overall battle (different class proximity on track) ---
        ovr_int = car.get('interval_seconds', float('inf'))
        if ovr_int < 1.5:
            ovr_battle = 40 * (1.0 - ovr_int / 1.5)
            score += ovr_battle
            parts['battle'] = ovr_battle
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
            parts['fast_lap'] = fl_score
            if fl_score > 30 and reason == 'default':
                reason = 'fast_lap'

        # --- Front-runner tie-breaker (no automatic leader bias) ---
        # P1 no longer gets +15..25 unconditionally. Small graded bonus so that
        # in a totally dead race we tilt slightly toward the front of each
        # class; events and battles dominate easily.
        cls_pos = car.get('class_position', 999)
        if 1 <= cls_pos <= 5:
            fr_score = max(0, 6 - cls_pos)   # P1=5, P2=4, P3=3, P4=2, P5=1
            score += fr_score
            parts['front_runner'] = fr_score
            if reason == 'default':
                reason = 'class_leader' if cls_pos == 1 else 'front_runner'

        # --- Staleness penalty (current focus), paused during active battle ---
        if cid == self.current_focus:
            cur_cls_int = car.get('class_interval_seconds', float('inf'))
            cur_ovr_int = car.get('interval_seconds', float('inf'))
            in_battle = cur_cls_int < 2.0 or cur_ovr_int < 2.0
            if not in_battle:
                focused_time = now - self.focus_start
                over_dwell = focused_time - self.min_dwell
                if over_dwell > 0:
                    penalty = -3 * over_dwell
                    score += penalty
                    parts['staleness'] = penalty

        # --- Recency penalty (recently shown), shorter window ---
        last = self.last_shown.get(cid)
        if last and cid != self.current_focus:
            age = now - last
            if age < 12:
                penalty = -10 * (1.0 - age / 12.0)
                score += penalty
                parts['recency'] = penalty

        return score, reason, parts

    # ------------------------------------------------------------------
    # Cut decision
    # ------------------------------------------------------------------

    def _pick_best(self, scores, now):
        """Apply hysteresis and timing rules. Returns command dict or None."""
        if not scores:
            return None

        scores.sort(key=lambda x: x[1], reverse=True)
        best_car, best_score, best_reason, best_parts = scores[0]

        elapsed = now - self.last_cut_time if self.last_cut_time else float('inf')

        # Locate the currently-focused entry for context (used for both
        # overrides logging and hysteresis comparison below).
        current_car = None
        current_score = 0
        current_reason = 'default'
        current_parts = {}
        for car, sc, rsn, pts in scores:
            if car['car_id'] == self.current_focus:
                current_car = car
                current_score = sc
                current_reason = rsn
                current_parts = pts
                break

        # Compute hysteresis up-front so the debug snapshot can show it.
        hysteresis = 15
        if current_car is not None:
            cur_cls_int = current_car.get('class_interval_seconds', float('inf'))
            cur_ovr_int = current_car.get('interval_seconds', float('inf'))
            if cur_cls_int < 2.0 or cur_ovr_int < 2.0:
                hysteresis += 15
        if self.endurance_mode:
            hysteresis += 5

        # Rollover override (1s floor, beats collision since score >= 100)
        if best_reason == 'rollover' and best_score > 100 and elapsed > 1.0:
            return self._do_cut(best_car, best_reason, now,
                                score=best_score, parts=best_parts,
                                current_score=current_score,
                                current_parts=current_parts,
                                hysteresis=hysteresis)

        # Collision override (1s floor)
        if best_reason == 'collision' and best_score > 80 and elapsed > 1.0:
            return self._do_cut(best_car, best_reason, now,
                                score=best_score, parts=best_parts,
                                current_score=current_score,
                                current_parts=current_parts,
                                hysteresis=hysteresis)

        # Periodic snapshot of state while not cutting.
        if self.debug and now - self._last_debug_log > 2.0:
            self._last_debug_log = now
            elapsed_str = (f"{elapsed:.1f}s"
                           if elapsed != float('inf') else 'init')
            print(
                f"[director] HOLD focus=#{self.current_focus} "
                f"elapsed={elapsed_str} min_dwell={self.min_dwell:.1f}s "
                f"hysteresis={hysteresis:.1f} endurance={self.endurance_mode}"
            )
            for car, sc, rsn, _pts in scores[:5]:
                marker = '*' if car['car_id'] == self.current_focus else ' '
                name = (car.get('driver_name', '?') or '?').strip()[:18]
                print(
                    f"[director]  {marker} #{car['car_id']:>3} "
                    f"{name:<18} score={sc:6.1f} reason={rsn}"
                )

        # Respect minimum dwell
        if elapsed < self.min_dwell:
            return None

        # First cut (no current focus)
        if self.current_focus is None:
            return self._do_cut(best_car, best_reason, now,
                                score=best_score, parts=best_parts,
                                current_score=0, current_parts={},
                                hysteresis=hysteresis)

        # Idle mode: once nothing real is happening, avoid cycling through
        # barely-different front-runners just because the current shot is stale.
        if (best_car['car_id'] != self.current_focus
                and best_reason in self.IDLE_REASONS
                and current_reason in self.IDLE_REASONS
                and elapsed < self.max_idle_dwell):
            return None

        # Hysteresis: scale with context (computed above).
        if (best_car['car_id'] != self.current_focus
                and best_score > current_score + hysteresis):
            return self._do_cut(best_car, best_reason, now,
                                score=best_score, parts=best_parts,
                                current_score=current_score,
                                current_parts=current_parts,
                                hysteresis=hysteresis)

        return None

    def _do_cut(self, car, reason, now,
                score=None, parts=None,
                current_score=None, current_parts=None,
                hysteresis=None):
        """Record cut and return command."""
        cid = car['car_id']

        if self.debug:
            prev = self.current_focus
            prev_age = (now - self.focus_start) if prev is not None else 0.0
            name = (car.get('driver_name', '?') or '?').strip()
            print(
                f"[director] CUT #{cid} ({name}) reason={reason} "
                f"score={(score if score is not None else 0):.1f} "
                f"prev=#{prev} held={prev_age:.1f}s "
                f"prev_score={(current_score if current_score is not None else 0):.1f} "
                f"hysteresis={(hysteresis if hysteresis is not None else 0):.1f} "
                f"endurance={self.endurance_mode}"
            )
            print(f"[director]   winner parts: {self._fmt_parts(parts)}")
            if current_parts:
                print(f"[director]   prev   parts: {self._fmt_parts(current_parts)}")
            # Reset the periodic snapshot timer so we don't double-log right after.
            self._last_debug_log = now

        # Track when we last showed each car
        if self.current_focus is not None:
            self.last_shown[self.current_focus] = now

        self.current_focus = cid
        self.focus_start = now
        self.last_cut_time = now

        # Track leader/class leader show times (still used for telemetry,
        # even though the unconditional bonus has been removed).
        if car.get('position') == 1:
            self.last_leader_show = now
        cls = car.get('car_class', 'Unclassed')
        if car.get('class_position') == 1:
            self.last_class_leader_shown[cls] = now

        # Set min dwell based on reason and mode
        mult = 1.5 if self.endurance_mode else 1.0
        dwell_map = {
            'rollover': 6,
            'collision': 4,
            'overtake': 5,
            'class_overtake': 5,
            'class_battle': 10,    # was 6 — protect battles
            'battle': 10,          # was 6 — protect battles
            'fast_lap': 5,
            'leader': 8,           # rarely reached now (no unconditional bonus)
            'class_leader': 8,
            'front_runner': 8,
            'default': 10,         # endurance idle shouldn't churn
        }
        self.min_dwell = dwell_map.get(reason, 8) * mult

        return {'driver': cid}

    def _fmt_parts(self, parts):
        """Format scoring breakdown as 'key=+12.3 key=-4.5' sorted by magnitude."""
        if not parts:
            return '(none)'
        items = sorted(parts.items(), key=lambda kv: abs(kv[1]), reverse=True)
        return ' '.join(f'{k}={v:+.1f}' for k, v in items if abs(v) > 0.05)

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
