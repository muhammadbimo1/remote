# Gap Re-sync Findings

Engineering notes for porting the disconnect/reconnect gap-correction fix to other timing-aware apps that consume AC telemetry alongside the `livetiming.buayabalap.my.id` relay.

---

## 1. The bug

When a driver disconnects and reconnects mid-race, AC desyncs that car's `lapCount` (sometimes inflated by phantom lap-line crossings, sometimes deflated by missed ones). `splinePosition` recovers immediately on reconnect — it's just the world coordinate. `lapCount` does not.

Any gap math that derives `total_progress = lapCount + splinePosition` is poisoned for the rest of the session unless something resets the baseline. AC's own `racePosition` field stays correct (the official server tells AC where each car ranks), but the position-only field doesn't tell you the *gap* — only the order.

Symptom: after a disconnect, one or more drivers show wildly wrong gaps (e.g. "+N Laps" when they're seconds behind, or "Leader" when they're not). The UI doesn't recover even after the driver is back on track for many laps.

---

## 2. Live-timing API findings (`https://livetiming.buayabalap.my.id`)

Endpoint: `GET /api/connected_drivers`. Auth: none. Polling cadence: 1 Hz upstream, so 2–5 s polls are plenty.

**Reliable fields per driver**:
- `Position` — track position (1-based), survives disconnects.
- `RaceNumber` — integer car number. Best join key.
- `DriverName` — sometimes returned as `"NN | Name"` (same prefix format as AC mmap), sometimes as bare `"Name"`.
- `GapToLeader` — milliseconds. `0` for leader and `0` whenever Split is non-numeric.
- `Split` — display string. `"00:00.000"` for leader, `"MM:SS.mmm"` for within-lap gaps, `"N lap"` / `"N laps"` for lapped cars.

**Unreliable / quirky**:
- `NumLaps` — observed `0` for every driver in the test race. Don't anchor on this.
- `Split == "00:00.000"` for non-leaders → almost always means upstream hasn't seen this car yet, **not** a real zero gap. Skip those rows when computing offsets.
- `RaceNumber == 0` → broadcast / marshal cars (e.g. "Endu RD", "Endu Broadcast"). Skip them.
- `Position` is **track position**, not race-count position. A car that's "1 lap" behind the leader but physically right behind them on track will appear at Position 2, not at Position N. Sort displays by AC `racePosition` (which uses race count) if your UI expects race-order, not the API's `Position`.

---

## 3. Why a per-car `progress_offset` float (not lap-count delta)

The desync magnitude isn't always integer laps — partial-lap miscounts happen too (missed lap line then re-crossed near the same spline). Storing a `float progress_offset[car_id]` and using `total_progress = lapCount + splinePos + progress_offset` covers every flavour with one mechanism. All downstream gap math (`progress_diff`, lapped detection, spline-based seconds gap) inherits the correction without further changes.

---

## 4. Deriving offsets from a single API snapshot

Triggered by an explicit user button, not background polling.

```
1. Fetch /api/connected_drivers.
2. Find the API leader (Position == 1). Match it to a local car by RaceNumber, fall back to lowered DriverName (handle "NN | Name" prefix on the API side too).
3. Anchor: leader_progress = leader.local_lap_count + leader.local_spline. The leader's own offset is 0.0. Their absolute progress can be "wrong" — what matters is that all other offsets are computed relative to the same leader.
4. Estimate lap_time_s from leader.local_best_lap (fall back to last_lap, then a 90s default). Clamp to [30, 600] s to keep the conversion sane.
5. For every other matched API row:
     skip if RaceNumber == 0
     if Split contains "lap":
         N = leading integer in Split   ("1 lap" → 1, "4 laps" → 4)
         target = leader_progress - N
     else:
         gap_ms = float(GapToLeader)
         skip if gap_ms <= 0   (stale "00:00.000" rows)
         target = leader_progress - (gap_ms / 1000) / lap_time_s
     offset = target - (local.lap_count + local.spline)
6. Atomically replace the offsets dict.
7. Clear the dict on AC reconnect (mmap reopen) — car_ids are reused.
```

The conversion `gap_seconds / lap_time_s = gap_in_progress_units` is the key insight: a 10-second gap at a 67-second lap is `10/67 ≈ 0.149` of a lap of progress. This is approximate (faster cars cover more progress per second than slower ones), but it's accurate to within a fraction of a second of the relay value.

---

## 5. Driver matching strategy

Race number first, name fallback. Race number is robust to display-name encoding differences and renames mid-race.

```
RaceNumber from local mmap: parsed from "NN | Driver Name" prefix.
RaceNumber from API: top-level field, reliable except for broadcast cars (0).
Name fallback: lowered, trimmed; also strip any "NN | " prefix on the API side
  before comparing, since some upstreams pass that through.
```

---

## 6. Assetto server `timetable.json` matching

For in-game Lua apps that read the AC server HTTP endpoint (`/timetable.json`), do **not** treat `EntryList[*].CarID` as a local CSP car index.

Observed server row shape:

```json
{
  "CarID": 2,
  "Driver": "Zidni Rizky",
  "GUID": "76561199059327794",
  "Car": "bm_toyota_gt86_cup",
  "Team": "Croco Juniors",
  "Ping": 11,
  "Laps": 1,
  "LastLapTime": 92454000000,
  "BestLapTime": 92454000000,
  "TotalConnectionTime": 151879602355,
  "Tyres": "SM"
}
```

Here `CarID` is the server entry-list slot. CSP local car indices are different: the player's local car is always `ac.getCar(0)`, while its online entry slot is exposed as `ac.getCar(0).sessionID`.

Correct join:

```lua
server_slot = entry.CarID
local_car_index = first i where ac.getCar(i).isConnected
  and ac.getCar(i).sessionID == server_slot
```

Do not use remote `GUID` as the primary join in CSP Lua unless a local GUID source is available. The remote row has Steam GUID, but the app-level CSP Lua API does not expose an obvious per-car Steam GUID getter. Driver name and car model are useful debug fields, not a reliable identity join.

Debug evidence from `SubStandingLua`:

```text
SubStandingLua sync map: PLAYER remoteSlot=18 -> localCar=0 sessionID=18 connected=true remoteDriver=Bimo Arya remoteGUID=76561198088024975 remoteCar=peugeot_9x8_2024 localDriver=Bimo Arya localCar=ACF HY - Peugeot 9X8 2024 LMH 2024 laps=0
SubStandingLua sync map: remoteSlot=34 -> localCar=- sessionID=- connected=false remoteDriver=Bimo Arya remoteGUID=76561198088024975 remoteCar=vanwall_vandervell_680 localDriver=- localCar=- laps=0
```

The duplicate Bimo row at server slot 34 was a stale/unmatched server entry. Requiring `car.isConnected` in the sessionID map prevented that stale row from updating local driver state.

---

## 7. Calibration data (one-shot snapshot)

Captured during the live race to validate the math:

| Driver         | AC P | mmap laps | mmap spline | API P | API Split    | computed offset |
|----------------|------|-----------|-------------|-------|--------------|-----------------|
| Iga (leader)   | 1    | 37        | 0.840       | 1     | 00:00.000    | 0.000           |
| Rafid          | 2    | 38        | 0.216       | 2     | 1 lap        | -1.378          |
| Ferdy          | 3    | 34        | 0.002       | 3     | 00:13.201    | +3.636          |
| Joaquin        | 4    | 35        | 0.171       | 4     | 01:00.397    | +1.784          |
| Hanif          | 5    | 36        | 0.702       | 5     | 1 lap        | +0.125          |
| Gerlandi       | 7    | 36        | 0.985       | 7     | 00:03.599    | +0.798          |
| Rafif          | 8    | 36        | 0.980       | 8     | 00:00.800    | +0.857          |

Pre-fix Rafid appears 1.376 laps *ahead* of Iga (38.216 > 37.840). Post-fix his effective progress is 36.838, exactly 1.002 laps behind — matching the API "1 lap" report. Ferdy's local lap_count was deflated by 3-4 laps from a reconnect; the offset puts him back at the right gap.

---

## 8. Edge cases worth handling explicitly

- **AC restart**: clear offsets when the mmap is (re)opened. car_ids get reused across game sessions and stale offsets corrupt new races.
- **Leader can't be matched**: fail closed. Set status `'no_leader'`, return False, leave existing offsets alone.
- **API unreachable / non-200 / non-JSON**: silent fallback. Existing offsets stay, single warning logged.
- **Driver in mmap but not in API**: leave their offset at whatever it was (typically 0). Don't try to invent one.
- **Driver in API but not in mmap**: skip silently.
- **`gap_ms <= 0` for non-leader**: treat as missing data, skip.
- **`RaceNumber == 0` in API**: broadcast/marshal car, skip.
- **Server `timetable.json` row with no connected local match**: skip it. Stale server entries can retain a real driver name/GUID but should not update local cars.
- **Lock the offset dict**: writers (resync) and readers (compute_gaps) run on different threads. A short-held lock + dict-snapshot copy is enough; no need for finer granularity.

---

## 9. UI / control surface

A single user-triggered button is enough. We considered background polling (every 2–5 s) and rejected it: gap calc is mostly correct mid-race, the bug only matters after a reconnect event, and the broadcaster operator is the human watching for those events anyway. One button click whenever the operator notices a wrong gap is sufficient.

Recommended feedback states on the button:
- **Idle** ("Sync"): default state, no offsets active.
- **Synced** (subtle green): offsets non-empty, at least one non-trivial. Pull this from a `gap_offsets_active` flag in the regular update payload.
- **Flash green / flash red** (3-4 s after click): success/failure feedback. Auto-revert.

---

## 10. Verification recipe

1. Run a session, identify a driver whose local gap is visibly wrong (e.g. shows "+1 Lap" but the live timing site shows seconds, or vice versa).
2. Click Sync.
3. Within one telemetry tick (~100 ms) the leaderboard's gap-to-leader column should converge to within ~0.5 s of `livetiming.buayabalap.my.id` for that driver.
4. Leave the page open for several minutes; gaps should evolve smoothly (cars closing/extending through corner sequences) and stay aligned with the live site for cars that haven't disconnected since the last sync.
5. If a *new* disconnect happens, that driver's gap will drift again — click Sync again to re-converge.

---

## 11. Reference implementation (this repo)

All changes are in `remote_web.py`. Key entry points:

- Module state: `LIVE_TIMING_URL`, `progress_offsets`, `_resync_lock`, `last_resync_*`.
- `_parse_driver_name(raw_name, car_id)` — shared "NN | Name" parser.
- `fetch_live_timing_drivers()` — single GET with timeout.
- `_match_local(api_record, by_number, by_name)` — race-number-first matching.
- `resync_progress_offsets()` — the work behind the button.
- `compute_gaps()` — adds `progress_offsets[car_id]` to `total_progress` under the lock.
- `@socketio.on('resync_gaps')` — UI trigger.
- Mmap-reopen path in `monitor_telemetry()` clears the dict.

Lua port note: `SubStandingLua` consumes AC server `/timetable.json` for lap-count sync. Its `EntryList[*].CarID` values must be mapped through local connected cars' `StateCar.sessionID` before applying `Laps`; direct `drivers[entry.CarID]` indexing is wrong whenever local car indices differ from server entry slots.
