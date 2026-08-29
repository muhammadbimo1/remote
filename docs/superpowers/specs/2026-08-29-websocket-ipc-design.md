# WebSocket IPC Design

## Purpose

Replace the Windows memory-mapped-file boundary between the CSP Lua app and
the standalone Python web server with a direct, bidirectional WebSocket
connection supplied natively by CSP. Preserve the browser panel, LAN access,
telemetry cadence, command semantics, and all replay-transition safeguards.

## Scope

This change affects only communication between `remote.lua` and
`remote_web.py`. The browser continues to use the existing Socket.IO interface,
and Flask-SocketIO continues to listen on `0.0.0.0:5000` so other devices on the
same network can access the panel.

The mmap transport and its duplicated Lua/ctypes structure definitions are
removed from the runtime path. `ipc_shared.py` can be deleted once no tests or
runtime modules import it. Git history is the rollback mechanism; there is no
dual-transport mode.

## Architecture

`remote_web.py` exposes a raw WebSocket endpoint at `/ac-ipc` on the existing
Flask port. `remote.lua` connects to
`ws://127.0.0.1:5000/ac-ipc` using CSP's native `web.socket()` client with
automatic reconnect enabled.

The connection is full duplex:

- Lua sends one telemetry message every 100 ms.
- Python sends camera and replay commands over the same socket as soon as they
  are requested by the browser or auto-director.
- The existing Python Socket.IO connection between the server and browsers is
  unchanged.

The Flask server still binds to all interfaces. Only `/ac-ipc` rejects
non-loopback peers; normal web and Socket.IO routes remain available to LAN
clients.

## Protocol

Messages are UTF-8 JSON objects. Every message includes `version: 1` and a
`type` discriminator. Unknown message types or unsupported versions are logged
and ignored without closing a healthy connection.

### Lua to Python: telemetry

```json
{
  "version": 1,
  "type": "telemetry",
  "packet_id": 42,
  "car_count": 2,
  "focused_car": 0,
  "current_camera": 1,
  "car_cameras_count": 3,
  "current_car_camera": 0,
  "track_length": 5421.0,
  "session_type": 1,
  "session_index": 0,
  "session_type_raw": 3,
  "session_gen": 1,
  "session_name": "Race",
  "is_replay": false,
  "replay_frame": 100,
  "replay_frames": 1000,
  "replay_frame_ms": 60.0,
  "replay_last_result": 0,
  "is_replay_only": false,
  "replay_file": "",
  "replay_temp_dir": "C:/.../replay/temp",
  "timetable_url": "http://server/timetable.json",
  "cars": [
    {
      "car_id": 0,
      "session_id": 7,
      "position": 1,
      "normalized_spline_pos": 0.5,
      "speed_kmh": 180.0,
      "lap_time": 50000,
      "best_lap": 100000,
      "last_lap": 101000,
      "lap_count": 4,
      "is_in_pit": false,
      "is_connected": true,
      "is_colliding": false,
      "is_rolled_over": false,
      "driver_name": "Driver",
      "team_name": "GT3 7 | Team"
    }
  ]
}
```

Field names deliberately match the existing Python telemetry consumers. JSON
booleans replace mmap integer booleans. `packet_id` remains monotonically
increasing for duplicate suppression and connection diagnostics; torn-read
protection is no longer needed because each WebSocket message is atomic.

### Python to Lua: camera command

```json
{
  "version": 1,
  "connection_id": "6b71930ce8a64f31978da36ec260de65",
  "type": "command",
  "command_seq": 12,
  "target_driver": 4,
  "target_camera": 1,
  "target_car_camera": -1
}
```

### Python to Lua: replay command

```json
{
  "version": 1,
  "connection_id": "6b71930ce8a64f31978da36ec260de65",
  "type": "replay",
  "replay_seq": 8,
  "replay_action": 1,
  "replay_rewind_s": 11.9,
  "replay_frame": 0,
  "target_driver": 4,
  "target_camera": 1,
  "target_car_camera": -1
}
```

Camera and replay counters remain separate. Replay messages never synthesize a
camera command or increment `command_seq`, preserving the rule that the replay
transition parks and later reapplies the requested shot.

Python creates a new opaque `connection_id` on every server start and includes
it in both outgoing message types. Lua resets its receive-side sequence state
when that ID changes, so the first command after a Python restart cannot collide
with the previous server process's last sequence number.

## Python Transport Boundary

A focused transport module owns the active AC WebSocket, the latest telemetry
snapshot, connection timestamps, and serialized command sends. It exposes
small operations to the existing web-server logic:

- accept and validate a loopback WebSocket client;
- ingest and validate a telemetry dictionary;
- return the latest telemetry snapshot;
- send a camera command;
- send a replay command;
- report whether telemetry is currently fresh.

Only one AC connection is active at a time. A new loopback connection replaces
an older one so restarting the Lua app recovers without restarting Flask.
Outgoing sends are serialized because browser handlers and the auto-director
can issue commands from different Python threads. A failed send marks the
connection unavailable and returns `False` to the existing caller.

Telemetry is considered disconnected if no valid message arrives for 2
seconds. On disconnect, Python publishes the same empty `ac_connected: false`
browser update used today and clears per-AC-run state such as gap corrections
and the current journal association. Reconnection performs that reset once,
not once per packet.

The existing telemetry processing loop remains responsible for gaps, event
detection, journal rotation, replay freezing, the timetable poll, highlight
publishing, the auto-director, and Socket.IO updates. It reads immutable
snapshots from the transport rather than ctypes structures.

## Lua Transport Boundary

Lua creates the WebSocket once at script load with UTF-8 encoding and CSP's
automatic reconnect option. Incoming messages are decoded with `JSON.parse()`
inside a protected call. The callback validates the protocol version, type,
and numeric sequence before updating an in-memory pending-command record.

The existing per-frame replay and camera functions consume those records. All
replay functions, settle timing, leaderboard suppression, stinger behavior,
pending seek behavior, and shot holding stay unchanged.

Telemetry construction changes from writing FFI fields to constructing a Lua
table and sending `JSON.stringify(table)`. Collection remains at 10 Hz and is
capped at 128 cars. If the socket is unavailable, telemetry for that tick is
dropped; simulation work never blocks or retries synchronously.

## Security

The browser server remains reachable on the LAN. Access control applies only
to `/ac-ipc`:

- accept `127.0.0.1` and `::1` as the peer address;
- reject all other addresses before upgrading/processing messages;
- do not trust forwarded-address headers for this decision;
- validate message type, version, required fields, and the 128-car limit;
- apply a bounded WebSocket message size sufficient for 128 telemetry rows.

This reflects the existing deployment constraint: mmap required AC and Python
to run on the same Windows machine, so the new IPC connection can remain local
without reducing supported deployments.

## Dependency and Startup Behavior

The raw endpoint uses `simple-websocket`, which is already installed on the rig
as part of the current Flask-SocketIO environment. It becomes an explicit
documented runtime dependency alongside `flask` and `flask-socketio`.

`python remote_web.py` remains the only server command. No additional process,
port, shell invocation, or CSP-side executable is introduced.

## Testing

Python tests cover:

- telemetry validation and normalization;
- rejecting unsupported versions and malformed messages;
- accepting loopback and rejecting LAN peers for `/ac-ipc`;
- atomic latest-snapshot replacement;
- command and replay JSON shapes and independent counters;
- serialized send failure and disconnect behavior;
- freshness timeout and one-time reconnect reset;
- existing gap, event, journal, replay-freeze, and director behavior using the
  JSON-backed telemetry representation.

Lua tests cover:

- removal of mmap calls and FFI layout coupling;
- creation of `web.socket()` with loopback URL and reconnect enabled;
- telemetry protocol fields and 10 Hz send cadence;
- malformed/unknown command rejection;
- independent command/replay sequence handling;
- preservation of the existing replay safety invariants.

The full existing Python and Lua test suites must pass. A final on-rig smoke
test starts `remote_web.py`, loads the CSP app, confirms telemetry in a browser
from another LAN device, changes driver/camera, enters and exits replay, and
confirms reconnect after restarting the Python server.

## Acceptance Criteria

- No runtime module creates, opens, reads, or writes the broadcaster mmap tags.
- The browser panel remains reachable from other devices on the LAN.
- `/ac-ipc` refuses non-loopback clients.
- Live telemetry updates at approximately 10 Hz.
- Driver, camera, sub-camera, replay-enter, replay-seek, and go-live commands
  retain their current behavior.
- Replay transitions still use only `enterReplay()` and `exitReplay()` and
  preserve settle timing and leaderboard suppression.
- Loss and restoration of either process recovers without restarting the other.
- Automated tests pass, followed by the stated on-rig smoke test.
