# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

**Broadcaster Remote** — an Assetto Corsa CSP Lua app that exposes broadcast camera and driver controls via a web interface. It has two runtime halves communicating through shared memory (mmap):

1. **`remote.lua`** — CSP Lua plugin running inside AC. Writes telemetry (car positions, gaps, camera state) to a memory-mapped file at 10 Hz, reads camera/focus commands from a second mmap, and renders an in-game IMGUI window.
2. **`remote_web.py`** + **`ipc_shared.py`** — standalone Flask + SocketIO web server (Python 3). Reads the telemetry mmap, pushes live updates to browser clients, and writes commands back to the command mmap.

### IPC Contract

The FFI structs in `remote.lua` (lines 7-39) and the ctypes structs in `ipc_shared.py` **must stay in sync** — field order, types, packing (`#pragma pack(4)` / `_pack_ = 4`), and `wchar_t[64]` sizing. A mismatch causes silent data corruption.

- **Telemetry mmap** (`broadcaster_remote_telemetry`): Lua writes, Python reads. Torn-read protection via `packet_id`.
- **Command mmap** (`broadcaster_remote_commands`): Python writes, Lua reads. New command detected by `command_seq` change.

Camera IDs use a custom 1-5 numbering (Track, Cockpit, Helicopter, Car/F6, Free) mapped to/from `ac.CameraMode` enums in Lua.

### Action Replay

The command mmap carries a **second sequence counter**, `replay_seq`, alongside `command_seq`.
Replay commands (`REPLAY_ENTER` / `REPLAY_LIVE` / `REPLAY_SEEK_FRAME` in `ipc_shared.py`) bump
only `replay_seq`, deliberately leaving `command_seq` alone: entering instant replay resets the
camera, so Lua parks the requested driver/camera in `shotHold` and applies it once
`sim.isReplayActive` matches. Bumping `command_seq` too would make Lua apply the shot
immediately, where the replay toggle eats it.

**Both** replay transitions reset the shot — entering resets the camera, leaving snaps focus back
to the player car — and AC does it a frame or two *after* the toggle returns, so one apply is not
enough. `shotHold` in `remote.lua` re-asserts the wanted driver/camera every frame for
`SHOT_HOLD_S`, only touching what actually drifted (a blind re-apply would cycle the F6 sub-camera,
since `changeCamera(4, …, -1)` advances it). Any command on `command_seq` clears the hold, so an
operator pick always wins. Leaving replay — commanded or AC's own exit at the live edge — captures
the on-screen shot first and holds it on the live side.

Jumps are expressed as **seconds to rewind**, never frame indices — `event_log.py` only has to
timestamp incidents in wall clock. `replay_frame` / `replay_frames` in the telemetry page feed the
readout and nothing else.

`ac.tryToToggleReplay(active, rewindS)`'s `rewindS` **overshoots** on this rig: it converts seconds
at a fixed 60 fps while the recording is ~16.7 fps (`sim.replayFrameMs` = 60), so a requested 11.9 s
rewind actually lands ~43 s back, and a large rewind can overshoot the buffer and be refused. So
`enterReplay` enters with only a nominal `REPLAY_ENTER_NOMINAL_REWIND_S` (0.5 s) and parks the real
target in `pendingSeek` (`sim.replayFrames - rewind_s/replayFrameMs`), which `processPendingSeek`
applies with `ac.setReplayPosition` the frame replay comes up. A `REPLAY_ENTER` landing while replay
is already active seeks to that same target directly. Both paths are precise; never trust
`tryToToggleReplay`'s own rewind.

**Exiting instant replay crashes AC's leaderboard overlay.** Four dumps (2026-08-08
`dmp-687-42914`/`dmp-687-42a55`, and 2026-08-16 `dmp-6816-3c002`/`dmp-6816-3d701`) are all an
access violation in `acs.exe` at `OverlayLeaderboard::renderHUD` (overlayleaderboard.cpp:246), a
few ms after `ac.tryToToggleReplay(false)` returns — on the replay→live transition, when the
leaderboard redraws while AC resets the grid. The mitigation is two-fold: (1) `remote.lua`
serialises every toggle behind a `REPLAY_SETTLE_S` window (re-armed on each `ac.onReplay` event),
and a `REPLAY_ENTER` landing while replay is already active is answered with `ac.setReplayPosition()`
— a seek to `sim.replayFrames - rewind_s/replayFrameMs` — **not** an exit → enter; and (2) the
leaderboard HUD is hidden via `ac.disableExtraHUDElements('leaderboard', true)` for the whole time
replay is active (plus the settle window after), so `renderHUD` is skipped during the transition,
then restored. Do not add a code path that calls `ac.tryToToggleReplay` without going through
`enterReplay` / `exitReplay`, do not reintroduce an exit-while-in-replay, and do not let the
leaderboard suppression lapse while a replay transition can still be in flight.

A **refused** toggle (`ac.tryToToggleReplay` returns `false`) still arms `replayBusyUntil`: a
refusal can mean AC is mid-transition, and poking it again inside the settle window is exactly what
crashes the leaderboard overlay — so a refused toggle serialises the next command too, at the cost
of a 0.75 s wait after a genuinely-refused rewind. The result of the last attempt is published as
`replay_last_result` and reverts to `UNTRIED` after `REPLAY_RESULT_VISIBLE_S` (3 s), so the panel's
"RPL:N/A" badge clears instead of sticking after one transient refusal.

`REPLAY_SEEK_FRAME` is gated on `sim.isReplayActive or sim.isReplayOnlyMode`: in saved-replay
review (`isReplayOnlyMode`) the replay may not be "active" yet (paused at load), but a frame seek
still has to land.

`sim.replayFrames` is the live count of recorded frames since session start (confirmed on
the rig: it counts up ~17/s while live, `sim.replayFrameMs` holds a constant 60 ms, and it keeps
counting while instant replay plays back). So `replay_frames * replay_frame_ms` — which drives the
event-log retention window, the journal's `session_s`/`replay_frame`, and the session-start
estimate — equals the recorded session length until the instant-replay buffer wraps.

### Event log vs event journal

Two separate stores, on purpose. `event_log.py` is the **live window** feeding the panel: it
prunes to AC's actual recorded replay length (`replay_frames * replay_frame_ms`, fed in from the
telemetry loop via `set_window()`), because an event that can no longer be rewound into is a dead
button. `event_journal.py` is the **permanent record**, one JSONL file per AC session under
`event_logs/`, never pruned, meant for annotating the `.acreplay` saved at the end of the session.

AC records one `.acreplay` per session under `replay/temp`, created at session start (its
`CreationTime` equals the timestamp in the name) and grown live, named
`AC_DDMMYY-HHMMSS_<S>_<car>_<track>.acreplay` with `S` being `O` (practice/other), `Q` or `R`.
`find_session_replay()` matches on **that filename timestamp**, never on file mtime — the file is
still being written, so its mtime tracks now, not the session. The journal then takes the replay's
stem as its own filename, and each record repeats `replay_file` in case the replay is renamed on
its way out of `temp`. Hand-saved replays have arbitrary names, don't parse, and are never matched.

Session start is estimated as `now - replay_frames * replay_frame_ms`, so the pairing still works
when the server is started mid-session. No match within `REPLAY_MATCH_TOLERANCE_S` leaves the
journal unpaired on purpose — an unmatched journal beats one pointing at the wrong session.

Journal records carry wall clock (`iso`/`t`), `session_s` (recorded replay seconds at that
instant) and `replay_frame`. The latter two are only trustworthy while the instant-replay buffer
has not wrapped — after a wrap the saved replay no longer starts at frame 0 and both offsets
shift. Wall clock is the anchor that never moves. Context keys are omitted rather than written as
`null` when AC reports nothing.

While `is_replay` is set, car data comes from the replay frame on screen, not the race: the
server freezes the driver list at the last live payload, suspends the auto-director, skips event
detection, and stops the timetable poll. Anything reading car state per tick needs the same
guard.

## Critical Rules

**NEVER use shell commands or external tools** (`io.popen`, `os.execute`, etc.) in Lua code. CSP provides comprehensive APIs for all file/directory operations:
- `io.createDir(path)` - Create directories
- `io.open(path, mode)` - Read/write files
- `io.exists(path)` - Check if file exists
- `io.scanDir(path)` - List directory contents

Shell commands launch visible CLI windows which breaks the user experience.

## Running

- **Lua side**: Loads automatically when the "Broadcaster Remote" app is enabled in AC's app sidebar (CSP required).
- **Web server**: `python remote_web.py` — serves on `0.0.0.0:5000`. Requires `flask` and `flask-socketio`.

## Web UI

The browser panel is themed with [Amber Console](https://github.com/DutchDiederik/AmberConsole), a
monochrome amber-terminal CSS framework. Two rules govern any change to it:

- **Everything under `static/` is vendored, on purpose.** The panel runs on a race rig that may have
  no internet, so there are no CDN links — not for the framework, not for the fonts, not for
  socket.io. `amber-console.min.css` resolves its webfonts as `../fonts/`, so `dist/` and `fonts/`
  must stay siblings. Never reintroduce an external `<link>` or `<script src>`.
- **One hue, no exceptions.** Amber Console forbids a second colour: no red, no green, no gradients.
  State is encoded by ramp position (`--ink-bright` → `--ink-faint`), inverse video, `.ac-blink`, and
  text labels. `CLASS_COLORS` in `remote_web.py` still ships a `class_color` hex in the telemetry
  payload, but the UI ignores it — car classes are told apart by their badge text and a four-step
  amber ramp (`.cls-1` … `.cls-4`). App-specific styling lives in `static/remote.css`; the template
  is still an inline string in `index()`.

## Resources

Full CSP Lua API stubs ship with the game. No internet needed.

```
C:\Program Files (x86)\Steam\steamapps\common\assettocorsa\extension\internal\lua-sdk\
```

- **`ac_apps/lib.lua`** — the one that matters here. ~18k lines, 1 MB. Every `ac.*`, `ui.*`, `physics.*`, `render.*` entry with LDoc annotations: param types, return types, doc comments. **Grep this for any signature instead of recalling from memory** — the API drifts between CSP versions.
- `ac_apps/README.md` — app-script notes