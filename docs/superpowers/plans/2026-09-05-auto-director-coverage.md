# Auto-Director Coverage Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make automatic camera coverage stable across dense battles and incidents while eliminating false qualifying and pit-cycle action.

**Architecture:** `remote_web.compute_gaps()` will keep UI standings fields but add symmetric physical-proximity fields for the director. `AutoDirector` will consume explicit race context, rank event severity separately from aggregate interest, and own protected incident/battle state. The embedded browser panel will use its own named notification duration.

**Tech Stack:** Python 3 `unittest`, Flask/Socket.IO embedded JavaScript, existing telemetry dictionaries

**Spec:** `docs/superpowers/specs/2026-09-05-auto-director-coverage-design.md`

## Global Constraints

- Preserve the existing `remote_web.py` runtime-directory changes.
- Do not alter Lua camera/replay behavior or the WebSocket protocol.
- Keep all browser assets offline and maintain the single-hue Amber Console rules.
- Use `INCIDENT_ACQUIRE_S = 3.0`, `INCIDENT_COVERAGE_S = 10.0`, and `INCIDENT_NOTICE_S = 6.0`.
- Do not consume replay-time car state or issue director cuts during replay.
- Every production change must follow a failing-test-first red/green cycle.

---

### Task 1: Symmetric physical battle context

**Files:**
- Modify: `remote_web.py:229-361`
- Test: `test_remote_web.py`

**Interfaces:**
- Consumes: car dictionaries containing `car_id`, `car_class`, `total_progress`, `speed_kmh`, and `is_connected`.
- Produces: `_apply_physical_battle_context(cars, track_length) -> None`, adding `battle_gap_ahead_seconds`, `battle_gap_behind_seconds`, `class_battle_gap_ahead_seconds`, `class_battle_gap_behind_seconds`, and `battle_opponent_ids` before UI-specific standings are applied.

- [ ] **Step 1: Write failing race and qualifying proximity tests**

Add tests that construct cars 0.1 seconds apart physically, assert both sides receive reciprocal battle gaps and opponent IDs, and assert qualifying cars with close best laps but distant spline positions have infinite physical battle gaps.

```python
def test_physical_battle_context_is_symmetric(self):
    cars = remote_web.compute_gaps(self._telem(3, [
        {'name': 'Leader', 'position': 1, 'best_lap': 95000, 'spline': 0.500},
        {'name': 'Attacker', 'position': 2, 'best_lap': 96000, 'spline': 0.499},
    ]))
    self.assertLess(cars[0]['battle_gap_behind_seconds'], 1.5)
    self.assertLess(cars[1]['battle_gap_ahead_seconds'], 1.5)
    self.assertEqual(cars[0]['battle_opponent_ids'], [1])
    self.assertEqual(cars[1]['battle_opponent_ids'], [0])

def test_qualifying_lap_delta_is_not_physical_battle_gap(self):
    cars = remote_web.compute_gaps(self._telem(2, [
        {'name': 'Fast', 'position': 1, 'best_lap': 95000, 'spline': 0.8},
        {'name': 'Close Lap', 'position': 2, 'best_lap': 95100, 'spline': 0.2},
    ]))
    self.assertGreater(cars[0]['battle_gap_behind_seconds'], 2.0)
    self.assertGreater(cars[1]['battle_gap_ahead_seconds'], 2.0)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python -m unittest test_remote_web.StandingsOrderTest -v`

Expected: failures because the physical battle keys do not exist.

- [ ] **Step 3: Implement physical battle helpers**

Initialize all four gap fields to infinity and opponent IDs to an empty list. Sort connected cars by `total_progress`, process adjacent overall and same-class pairs, calculate distance from progress delta, and convert it with a 10 m/s speed floor. Add reciprocal ahead/behind values and opponent IDs when the gap is below the 2-second exit threshold. Call the helper before the practice/qualifying early return.

```python
BATTLE_EXIT_S = 2.0
BATTLE_SPEED_FLOOR_MS = 10.0

def _physical_gap_seconds(ahead, behind, track_length):
    progress = ahead['total_progress'] - behind['total_progress']
    if progress < 0 or progress >= 1.0:
        return float('inf')
    distance = progress * track_length
    speed_ms = max(BATTLE_SPEED_FLOOR_MS,
                   (ahead['speed_kmh'] + behind['speed_kmh']) / 7.2)
    return distance / speed_ms
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `python -m unittest test_remote_web.StandingsOrderTest -v`

Expected: all standings and new physical-proximity tests pass.

### Task 2: Race-only, pit-aware overtake acquisition

**Files:**
- Modify: `auto_director.py:19-147`
- Test: `test_auto_director.py`

**Interfaces:**
- Consumes: `AutoDirector.tick(cars, track_length, is_race=True)` and previous/current overall and class standings.
- Produces: one deduplicated `position_change_seen[car_id]` timestamp for an eligible on-track pass; `is_race=False` suppresses all position-derived events.

- [ ] **Step 1: Write failing eligibility tests**

Add three tests that seed one tick, advance the clock, then assert no overtake component for qualifying rank changes or a place inherited from a pitting car, while a genuine race pass produces an overtake component.

```python
with patch('auto_director.time.monotonic', side_effect=[1.0, 2.0]):
    director.tick(before, 5000.0, is_race=True)
    director.tick(after_displaced_car_pits, 5000.0, is_race=True)
self.assertNotIn(gainer_id, director.position_change_seen)
```

- [ ] **Step 2: Run eligibility tests and verify RED**

Run: `python -m unittest test_auto_director.AutoDirectorOvertakeEligibilityTest -v`

Expected: qualifying and pit-inheritance cases incorrectly populate position-change state.

- [ ] **Step 3: Implement shared eligibility rules inside `_detect_events`**

Build previous overall/class holder maps before iterating. Only acquire a pass when `is_race`, the gaining car is not in pit, and the displaced previous holder is not currently in pit. Merge overall and class changes into the existing `position_change_seen` timestamp and stop adding a separate class-overtake score.

- [ ] **Step 4: Run eligibility tests and verify GREEN**

Run: `python -m unittest test_auto_director.AutoDirectorOvertakeEligibilityTest -v`

Expected: all three event-eligibility cases pass.

### Task 3: Stable incident priority and protected coverage

**Files:**
- Modify: `auto_director.py:10-431`
- Test: `test_auto_director.py`

**Interfaces:**
- Consumes: score `parts`, raw `is_colliding`/`is_rolled_over`, and symmetric physical battle fields.
- Produces: `incident_focus`, `incident_reason`, `incident_coverage_until`, and cut decisions where priority is `rollover > collision > overtake > battle > idle`.

- [ ] **Step 1: Write failing incident behavior tests**

Add focused tests for:

```python
def test_sustained_incident_refreshes_coverage_without_recut(self):
    # First collision cuts to car 1; another active tick after 1.1 seconds
    # returns None, keeps focus_start unchanged, and extends coverage.

def test_equal_priority_incident_cannot_interrupt_coverage(self):
    # A second collision on car 2 during car 1's protected window returns None.

def test_rollover_escalates_protected_collision(self):
    # A rollover on car 2 after the one-second emergency floor cuts from car 1.

def test_battle_points_cannot_promote_expired_collision_override(self):
    # A low/expired collision component plus dense battle score does not use
    # the collision override path.
```

Assert the exact constants: acquisition 3 seconds and coverage 10 seconds.

- [ ] **Step 2: Run incident tests and verify RED**

Run: `python -m unittest test_auto_director.AutoDirectorIncidentCoverageTest -v`

Expected: repeated same-car commands and equal-priority incident churn are observed; named coverage state is missing.

- [ ] **Step 3: Implement explicit incident state and priority**

Record every active event component in `parts`. Sort candidates first by incident severity and then aggregate score. Test rollover/collision overrides using their component values. Before any normal cut, enforce `incident_coverage_until`; refresh it only from a live signal on the focused car. If the incident winner is already focused, return `None` without calling `_do_cut()` or changing `focus_start`.

Set incident dwell to the same 10-second protected coverage. Clear incident state when coverage expires or on `reset_run_state()`.

- [ ] **Step 4: Run incident tests and verify GREEN**

Run: `python -m unittest test_auto_director.AutoDirectorIncidentCoverageTest -v`

Expected: all incident acquisition, refresh, escalation, and priority tests pass.

### Task 4: Symmetric battle scoring and order-swap continuity

**Files:**
- Modify: `auto_director.py:203-269,275-429`
- Test: `test_auto_director.py`

**Interfaces:**
- Consumes: four physical gap fields and `battle_opponent_ids` produced by Task 1.
- Produces: closest-neighbor battle scores for both sides and stable current-shot protection across swaps.

- [ ] **Step 1: Write failing defender and swap tests**

```python
def test_defender_receives_battle_score_from_car_behind(self):
    car = make_car(0, 1)
    car['class_battle_gap_behind_seconds'] = 0.1
    score, reason, parts = director._score_car(car, [car], 5.0)
    self.assertIn('class_battle', parts)

def test_battle_focus_survives_order_swap(self):
    # Cut to car 0 in a battle with car 1, swap their positions and reciprocal
    # ahead/behind fields, then assert no cut away from the same battle.
```

- [ ] **Step 2: Run battle tests and verify RED**

Run: `python -m unittest test_auto_director.AutoDirectorBattleContinuityTest -v`

Expected: the defender has no battle score and current-shot protection is lost on swap.

- [ ] **Step 3: Consume symmetric proximity and retain opponent identity**

Score the minimum finite ahead/behind physical gap. Record `current_battle_opponents` and `battle_last_active` when a battle shot starts. Treat the shot as still in battle when the focused car remains close to any recorded opponent; use the 2-second exit threshold and a one-second grace period before enabling staleness.

- [ ] **Step 4: Run battle tests and verify GREEN**

Run: `python -m unittest test_auto_director.AutoDirectorBattleContinuityTest -v`

Expected: defender scoring and post-pass continuity pass.

### Task 5: Independent six-second panel notice

**Files:**
- Modify: `remote_web.py:1219-1233,1613-1634`
- Test: `test_remote_web.py`

**Interfaces:**
- Produces: embedded JavaScript constant `INCIDENT_NOTICE_MS = 6000`; collision telemetry refreshes `collisionTimers[d.num]`, and card emphasis uses the named duration.

- [ ] **Step 1: Write a failing HTML contract test**

```python
def test_collision_notice_has_independent_six_second_duration(self):
    html = remote_web.app.test_client().get('/').get_data(as_text=True)
    self.assertIn('var INCIDENT_NOTICE_MS = 6000;', html)
    self.assertIn('Date.now() - collisionTimers[d.num] < INCIDENT_NOTICE_MS', html)
```

- [ ] **Step 2: Run the HTML test and verify RED**

Run: `python -m unittest test_remote_web.IncidentNoticeMarkupTest -v`

Expected: the page still contains the unnamed 1000 ms threshold.

- [ ] **Step 3: Add and use the named browser timer**

Declare `var INCIDENT_NOTICE_MS = 6000;` next to `collisionTimers` and replace the inline 1000 ms comparison. Preserve the current inverse-video and single-hue styling.

- [ ] **Step 4: Run the HTML test and verify GREEN**

Run: `python -m unittest test_remote_web.IncidentNoticeMarkupTest -v`

Expected: the six-second notice contract passes.

### Task 6: Monitor integration and full verification

**Files:**
- Modify: `remote_web.py:896-904`
- Test: `test_remote_web.py`, `test_auto_director.py`

**Interfaces:**
- Consumes: validated telemetry `telem.session_type == 1`.
- Produces: `director.tick(director_cars, telem.track_length, is_race=telem.session_type == 1)`.

- [ ] **Step 1: Write a failing call-contract test**

Patch the director in a live telemetry monitor fixture and assert the third argument is false for qualifying and true for a race.

- [ ] **Step 2: Run the integration test and verify RED**

Run: `python -m unittest test_remote_web.AutoDirectorMonitorIntegrationTest -v`

Expected: the existing two-argument call does not provide race context.

- [ ] **Step 3: Pass explicit race context**

Update the monitor call to:

```python
cmd = director.tick(
    director_cars,
    telem.track_length,
    is_race=telem.session_type == 1,
)
```

- [ ] **Step 4: Run focused director and web tests**

Run: `python -m unittest test_auto_director.py test_remote_web.py -v`

Expected: all tests pass without warnings or errors.

- [ ] **Step 5: Run the complete Python suite and syntax checks**

Run: `python -m unittest discover -v`

Run: `python -m py_compile ac_ipc.py remote_web.py auto_director.py event_log.py event_journal.py broadcast_highlight.py`

Expected: all tests and compilation pass.

- [ ] **Step 6: Review the final diff for scope and preserved user changes**

Run: `git diff --check`

Run: `git diff -- auto_director.py remote_web.py test_auto_director.py test_remote_web.py`

Confirm the runtime-directory changes in `remote_web.py` remain intact, no Lua or protocol files changed, and no generated cache files are included.
