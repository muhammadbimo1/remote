# Auto-Director Coverage Stability Design

## Goal

Keep the automated broadcast camera on a coherent battle or incident long
enough for viewers to understand it, while preventing timing-screen changes,
pit-cycle gains, and additive battle scores from masquerading as higher-priority
events.

## Scope

This change covers the Python auto-director, the physical proximity data passed
to it, and the browser's transient incident emphasis. It does not change Lua
camera execution, replay commands, the permanent event journal, the event-log
retention window, or operator-selected camera behavior.

## Explicit Durations

The three incident timelines have separate names and purposes:

- `INCIDENT_ACQUIRE_S = 3.0`: how long a collision remains eligible to attract
  the camera after its last telemetry signal.
- `INCIDENT_COVERAGE_S = 10.0`: minimum protected coverage after the director
  selects an incident. A continuing signal refreshes protection without sending
  another camera command or resetting the original shot start.
- `INCIDENT_NOTICE_S = 6.0`: how long a driver's HIT indicator remains visibly
  emphasized in the browser after the last collision signal.

Rollover detection remains active while the car is rolled and decays for six
seconds after recovery. A rollover is more severe than a collision and may
replace collision coverage after the one-second emergency cut floor. Equal or
lower-severity incidents cannot interrupt protected incident coverage.

## Director Inputs

`AutoDirector.tick()` receives an explicit `is_race` flag. Event detection only
creates position-change or class-position-change interest when `is_race` is
true. Collision, rollover, physical battles, and lap events remain available in
all live sessions.

`compute_gaps()` continues to provide timing-table positions and intervals for
the UI, but also provides director-only physical proximity fields computed from
live spline progress:

- `battle_gap_ahead_seconds`
- `battle_gap_behind_seconds`
- `class_battle_gap_ahead_seconds`
- `class_battle_gap_behind_seconds`
- `battle_opponent_ids`, containing physically close cars on either side

Physical gaps use track distance and speed, with low-speed distance thresholds
so stopping or braking does not turn a close group into an infinite gap. The
director scores the closest applicable opponent on either side instead of the
timing-table interval.

## Battle Continuity

The director records the opponent set associated with the current shot. If the
focused driver and one of those opponents swap order, the same battle remains
protected. Battle protection ends only when no recorded opponent remains within
the battle exit threshold for a short grace period, or when a higher-priority
incident legitimately preempts it.

The entry threshold stays tighter than the exit threshold to avoid boundary
oscillation. Battle scoring is symmetric: both the attacker and defender receive
the same proximity basis, so swapping position does not transfer all interest
from one car to the other.

## Overtake Eligibility

The director mirrors the event log's race-only and pit-aware rules. A gained
position is not an overtake when:

- the session is practice or qualifying;
- the gaining car is currently in the pit; or
- the previous holder of the gained position is now in the pit.

Overall and class-position changes from the same on-track pass are treated as
one overtake event for priority and dwell purposes. Class relevance can affect
the event's score, but it does not create a second additive event.

## Priority and Cut Rules

Event priority is explicit and independent from aggregate interest:

1. rollover;
2. collision;
3. overtake;
4. physical battle;
5. fast lap and idle rotation.

Rollover and collision overrides test their own active component, not total
score. Additive battle or lap points cannot promote an expired incident back
into the override path.

When the highest-priority target is already focused, the director refreshes any
applicable protection and returns no command. When incident coverage is active,
a different target can replace it only with strictly higher severity. Normal
minimum dwell and hysteresis apply outside protected coverage.

## Browser Highlighting

The driver's HIT state remains telemetry-driven but uses
`INCIDENT_NOTICE_S = 6.0` instead of the current one-second expiry. Repeated
collision telemetry refreshes the notice timestamp. The panel timer is
deliberately independent from camera acquisition and protected coverage.

## State Reset and Replay

All new timestamps, active-incident state, and battle-continuity state are
cleared by `reset_run_state()`. The existing replay guard remains unchanged:
the director does not consume replay-time car state or issue cuts during replay.

## Verification

Automated tests must demonstrate:

- a sustained rollover or collision refreshes coverage without another command;
- equal-priority incidents cannot churn a protected incident shot;
- rollover can escalate from protected collision coverage;
- incident acquisition, protected coverage, and browser notice use their three
  independent durations;
- both sides of a close battle receive protection;
- protection survives an attacker/defender order swap;
- qualifying timing gaps and rank changes do not create battles or overtakes;
- a place inherited from a pitting car is not an overtake;
- a genuine race pass remains eligible;
- incident override decisions depend on incident severity, not additive battle
  score.

The focused auto-director tests and the complete Python test suite must pass.
The existing unrelated worktree changes must remain intact.
