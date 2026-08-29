# WebSocket IPC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace broadcaster mmap IPC with a CSP-native WebSocket connection on the existing Flask port while retaining LAN browser access and replay safety.

**Architecture:** A focused Python `ACIPCTransport` validates JSON telemetry, owns the active loopback WebSocket, and serializes outgoing commands. The existing telemetry processing loop consumes attribute-based immutable snapshots from that transport, while Lua sends telemetry at 10 Hz and converts incoming command messages into the same per-frame command state used today.

**Tech Stack:** CSP Lua (`web.socket`, `JSON`), Python 3, Flask, Flask-SocketIO, simple-websocket, unittest.

**Spec:** `docs/superpowers/specs/2026-08-29-websocket-ipc-design.md`

## Global Constraints

- Flask-SocketIO must continue binding to `0.0.0.0:5000` for LAN browser clients.
- `/ac-ipc` must accept only actual loopback peers and must not trust forwarded-address headers.
- The protocol version is exactly `1`; telemetry is capped at 128 cars and stale after 2 seconds.
- Camera `command_seq` and `replay_seq` remain independent.
- Replay toggles continue to flow only through `enterReplay()` and `exitReplay()`.
- Replay settle timing, leaderboard suppression, pending seek, stinger, and shot holding are behavior-preserving code.
- Lua code must not invoke shell commands or external tools.
- Production behavior is implemented only after its failing test has been observed.

---

### Task 1: JSON snapshot validation and transport state

**Files:**
- Create: `ac_ipc.py`
- Create: `test_ac_ipc.py`

**Interfaces:**
- Produces: `PROTOCOL_VERSION`, `MAX_CARS`, `REPLAY_NONE`, `REPLAY_ENTER`, `REPLAY_LIVE`, and `REPLAY_SEEK_FRAME` constants.
- Produces: `TelemetrySnapshot.from_message(message)` returning an attribute-based snapshot whose `cars` is a tuple of `CarSnapshot` objects.
- Produces: `ACIPCTransport(clock=time.monotonic, stale_after=2.0)` with `attach(socket)`, `detach(socket)`, `ingest(raw)`, `latest()`, `is_connected()`, `send_command(...)`, and `send_replay_command(...)`.

- [ ] **Step 1: Write failing validation tests**

Create `test_ac_ipc.py` with real JSON messages and assertions for valid normalization, protocol rejection, required fields, boolean conversion, car-count consistency, and the 128-car bound:

```python
import json
import unittest

from ac_ipc import (
    ACIPCTransport, MAX_CARS, ProtocolError, TelemetrySnapshot,
)


def telemetry_message(cars=None, **overrides):
    cars = list(cars or [])
    message = {
        'version': 1, 'type': 'telemetry', 'packet_id': 7,
        'car_count': len(cars), 'focused_car': 0, 'current_camera': 1,
        'car_cameras_count': 3, 'current_car_camera': 0,
        'track_length': 5000.0, 'session_type': 1, 'session_index': 0,
        'session_type_raw': 3, 'session_gen': 1, 'session_name': 'Race',
        'is_replay': False, 'replay_frame': 0, 'replay_frames': 1000,
        'replay_frame_ms': 60.0, 'replay_last_result': 0,
        'is_replay_only': False, 'replay_file': '',
        'replay_temp_dir': 'C:/AC/replay/temp', 'timetable_url': '',
        'cars': cars,
    }
    message.update(overrides)
    return message


def car_message(car_id=0):
    return {
        'car_id': car_id, 'session_id': 18, 'position': 1,
        'normalized_spline_pos': 0.5, 'speed_kmh': 120.0,
        'lap_time': 50000, 'best_lap': 100000, 'last_lap': 101000,
        'lap_count': 3, 'is_in_pit': False, 'is_connected': True,
        'is_colliding': False, 'is_rolled_over': False,
        'driver_name': 'Alex Driver', 'team_name': 'PRO 7 | Team',
    }


class TelemetrySnapshotTest(unittest.TestCase):
    def test_valid_message_becomes_attribute_snapshot(self):
        snapshot = TelemetrySnapshot.from_message(
            telemetry_message([car_message()]))
        self.assertEqual(snapshot.packet_id, 7)
        self.assertEqual(snapshot.car_count, 1)
        self.assertEqual(snapshot.cars[0].driver_name, 'Alex Driver')
        self.assertTrue(snapshot.cars[0].is_connected)

    def test_wrong_version_is_rejected(self):
        with self.assertRaises(ProtocolError):
            TelemetrySnapshot.from_message(telemetry_message(version=2))

    def test_declared_car_count_must_match_array(self):
        with self.assertRaises(ProtocolError):
            TelemetrySnapshot.from_message(
                telemetry_message([car_message()], car_count=2))

    def test_more_than_max_cars_is_rejected(self):
        cars = [car_message(i) for i in range(MAX_CARS + 1)]
        with self.assertRaises(ProtocolError):
            TelemetrySnapshot.from_message(telemetry_message(cars))
```

- [ ] **Step 2: Run validation tests and verify the expected failure**

Run: `python -m unittest test_ac_ipc.TelemetrySnapshotTest -v`

Expected: import failure because `ac_ipc` does not exist.

- [ ] **Step 3: Implement immutable attribute snapshots and strict validation**

In `ac_ipc.py`, use ordinary Python classes compatible with the project rather than dataclasses. Define the complete scalar and car field lists, reject booleans where an integer is required, copy strings, normalize the four boolean car fields and two replay booleans, and store cars as a tuple. Raise `ProtocolError` with a field-specific message for malformed input.

- [ ] **Step 4: Run validation tests and verify they pass**

Run: `python -m unittest test_ac_ipc.TelemetrySnapshotTest -v`

Expected: 4 tests pass.

- [ ] **Step 5: Write failing connection, freshness, and command tests**

Add a real in-memory socket recorder and tests demonstrating replacement, atomic snapshots, stale timeout, send failure, JSON shapes, and independent counters:

```python
class FakeSocket(object):
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    def send(self, data):
        if self.fail:
            raise OSError('closed')
        self.sent.append(json.loads(data))

    def close(self):
        pass


class ACIPCTransportTest(unittest.TestCase):
    def test_ingest_replaces_latest_snapshot_atomically(self):
        now = [10.0]
        transport = ACIPCTransport(clock=lambda: now[0])
        socket = FakeSocket()
        transport.attach(socket)
        self.assertTrue(transport.ingest(json.dumps(telemetry_message())))
        self.assertEqual(transport.latest().packet_id, 7)
        self.assertTrue(transport.is_connected())

    def test_telemetry_becomes_stale_after_two_seconds(self):
        now = [10.0]
        transport = ACIPCTransport(clock=lambda: now[0], stale_after=2.0)
        transport.attach(FakeSocket())
        transport.ingest(json.dumps(telemetry_message()))
        now[0] = 12.01
        self.assertFalse(transport.is_connected())
        self.assertIsNone(transport.latest())

    def test_replay_send_does_not_increment_camera_sequence(self):
        transport = ACIPCTransport()
        socket = FakeSocket()
        transport.attach(socket)
        self.assertTrue(transport.send_command(3, 1, -1))
        camera_seq = socket.sent[-1]['command_seq']
        self.assertTrue(transport.send_replay_command(1, 12.5, 0, 3, 1))
        self.assertEqual(socket.sent[-1]['type'], 'replay')
        self.assertNotIn('command_seq', socket.sent[-1])
        self.assertEqual(camera_seq, 1)
        self.assertEqual(socket.sent[-1]['replay_seq'], 1)

    def test_failed_send_disconnects_only_the_failed_socket(self):
        transport = ACIPCTransport()
        socket = FakeSocket(fail=True)
        transport.attach(socket)
        self.assertFalse(transport.send_command(0, 1, -1))
        self.assertFalse(transport.is_connected())
```

- [ ] **Step 6: Run transport tests and verify the expected failures**

Run: `python -m unittest test_ac_ipc.ACIPCTransportTest -v`

Expected: failures because `ACIPCTransport` is not implemented.

- [ ] **Step 7: Implement the minimal thread-safe transport**

Use `threading.RLock` around socket ownership, snapshots, timestamps, and sends. `attach()` closes the displaced socket after swapping ownership. `detach(socket)` clears state only if that exact socket is still current. `ingest()` parses JSON, accepts only telemetry, and replaces the snapshot only after full validation. `send_command()` and `send_replay_command()` increment their own counters, serialize compact JSON, and detach on send exceptions.

- [ ] **Step 8: Run the transport test module**

Run: `python -m unittest test_ac_ipc -v`

Expected: all tests pass.

- [ ] **Step 9: Commit the transport unit**

```powershell
git add -- ac_ipc.py test_ac_ipc.py
git commit -m "feat: add websocket IPC transport"
```

---

### Task 2: Flask endpoint and JSON-backed telemetry processing

**Files:**
- Modify: `remote_web.py`
- Modify: `test_remote_web.py`
- Test: `test_ac_ipc.py`

**Interfaces:**
- Consumes: `ACIPCTransport`, `TelemetrySnapshot`, and replay constants from `ac_ipc.py`.
- Produces: `is_loopback_peer(address) -> bool`, the `/ac-ipc` WebSocket route, and compatibility function `read_telemetry()` returning the latest `TelemetrySnapshot` or `None`.

- [ ] **Step 1: Write failing route security tests**

Add tests that call a small pure helper so access control is verified independently of Werkzeug upgrade machinery:

```python
class IPCPeerSecurityTest(unittest.TestCase):
    def test_ipv4_and_ipv6_loopback_are_allowed(self):
        self.assertTrue(remote_web.is_loopback_peer('127.0.0.1'))
        self.assertTrue(remote_web.is_loopback_peer('::1'))

    def test_lan_and_forwarded_text_are_rejected(self):
        self.assertFalse(remote_web.is_loopback_peer('192.168.1.25'))
        self.assertFalse(remote_web.is_loopback_peer('127.0.0.1, 192.168.1.25'))
```

Add a route test with `simple_websocket.Server.accept` patched to prove a LAN peer receives 403 before `accept()` is invoked, and a loopback peer reaches `accept()`.

```python
class IPCWebSocketRouteTest(unittest.TestCase):
    def test_lan_peer_is_rejected_before_websocket_accept(self):
        with patch.object(remote_web.Server, 'accept') as accept:
            response = remote_web.app.test_client().get(
                '/ac-ipc', environ_base={'REMOTE_ADDR': '192.168.1.25'})
        self.assertEqual(response.status_code, 403)
        accept.assert_not_called()

    def test_loopback_peer_reaches_websocket_accept(self):
        fake = unittest.mock.MagicMock()
        fake.receive.side_effect = remote_web.ConnectionClosed()
        with patch.object(remote_web.Server, 'accept', return_value=fake) as accept:
            remote_web.app.test_client().get(
                '/ac-ipc', environ_base={'REMOTE_ADDR': '127.0.0.1'})
        accept.assert_called_once()
```

- [ ] **Step 2: Run route tests and verify the expected failure**

Run: `python -m unittest test_remote_web.IPCPeerSecurityTest -v`

Expected: failure because `is_loopback_peer` and `/ac-ipc` do not exist.

- [ ] **Step 3: Add the loopback-only WebSocket endpoint**

Remove `mmap`, `ctypes`, and `ipc_shared` imports. Import `ipaddress`, `ConnectionClosed`, and `Server` from `simple_websocket`, plus transport/constants from `ac_ipc`. Create one process-global `ac_transport`.

Implement `is_loopback_peer()` using `ipaddress.ip_address(address).is_loopback`, returning `False` on parse errors. In `/ac-ipc`, check `request.remote_addr` before `Server.accept(request.environ, max_message_size=262144)`. Attach the accepted socket, receive with a one-second timeout, pass text frames to `ac_transport.ingest()`, log `ProtocolError`, and always detach the exact socket in `finally`.

- [ ] **Step 4: Run route tests and verify they pass**

Run: `python -m unittest test_remote_web.IPCPeerSecurityTest -v`

Expected: all route-security tests pass.

- [ ] **Step 5: Convert existing telemetry fixtures before production consumers**

Replace `TelemetryPage()`/ctypes mutation in `test_remote_web.py` with `TelemetrySnapshot.from_message(telemetry_message(...))`. Keep fixture builders in test code and ensure every existing payload, standings, timetable, replay, and session test runs against JSON-backed snapshots.

- [ ] **Step 6: Run the converted tests and observe failures at mmap-dependent functions**

Run: `python -m unittest test_remote_web -v`

Expected: failures in `read_telemetry`, monitor setup, or command tests because production still references mmap state.

- [ ] **Step 7: Replace mmap reads and writes in the web server**

Delete mmap globals, `open_telemetry_mmap()`, `open_command_mmap()`, and ctypes command counters. Make `read_telemetry()` return `ac_transport.latest()`. Make `send_command()` and `send_replay_command()` delegate to the transport, preserving their existing signatures and boolean return values.

Refactor `monitor_telemetry()` to detect disconnected→connected and connected→disconnected edges using `ac_transport.is_connected()`. Preserve the current one-time reset of offsets, event/session state, highlight state, and browser disconnect payload. Keep packet-ID duplicate suppression and the 100 ms processing cadence.

Update `handle_connect()` and the timetable polling thread through the compatibility `read_telemetry()` function so downstream behavior remains unchanged.

- [ ] **Step 8: Replace the old replay command test**

Patch `remote_web.ac_transport` with a real `ACIPCTransport` plus `FakeSocket`; call `send_replay_command()` and assert on the emitted JSON rather than `CommandPage` memory. Retain the assertion that no `command_seq` appears in a replay message.

- [ ] **Step 9: Run Python integration tests**

Run: `python -m unittest test_ac_ipc test_remote_web -v`

Expected: all tests pass.

- [ ] **Step 10: Commit the Python integration**

```powershell
git add -- remote_web.py test_remote_web.py test_ac_ipc.py
git commit -m "feat: serve AC IPC over websocket"
```

---

### Task 3: CSP WebSocket client and command ingestion

**Files:**
- Modify: `remote.lua`
- Modify: `test_remote_lua.lua`

**Interfaces:**
- Consumes: JSON protocol version 1 and message shapes defined by `ac_ipc.py`.
- Produces: one CSP `web.socket()` connection to `ws://127.0.0.1:5000/ac-ipc`, telemetry JSON every 100 ms, and in-memory camera/replay command records.

- [ ] **Step 1: Extend the Lua harness with a fake WebSocket**

Replace the mmap fake in `loadRemote()` with:

```lua
local socketSends = {}
local socketCallback
_G.JSON = {
  stringify = function(value) socketSends[#socketSends + 1] = value return 'encoded' end,
  parse = function(value) return value end,
}
_G.web = {
  socket = function(url, callback, params)
    socketCallback = callback
    return function(payload) socketSends[#socketSends + 1] = payload end
  end,
}
```

Return `deliver(message)` to call the captured callback and expose the captured URL, parameters, and sends.

- [ ] **Step 2: Write failing connection and telemetry tests**

Add tests asserting:

- the URL is `ws://127.0.0.1:5000/ac-ipc`;
- `encoding == 'utf8'` and `reconnect == true`;
- no `ac.writeMemoryMappedFile` call exists in the loaded script;
- the first 100 ms update sends protocol version 1, type `telemetry`, all session/replay fields, and no more than 128 cars;
- updates before 100 ms send no telemetry.

Run: `lua test_remote_lua.lua`

Expected: failure because `remote.lua` still creates mmap pages and never calls `web.socket()`.

- [ ] **Step 3: Replace Lua mmap setup with WebSocket state**

Remove `require('ffi')`, layout strings, mmap tags, `writeWchar()`, and page objects. Add `PROTOCOL_VERSION = 1`, `packetID`, `pendingCommand`, and `pendingReplay` locals. Create the socket once with UTF-8 encoding, reconnect enabled, concise error/close logging, and a callback that parses incoming JSON with `pcall(JSON.parse, raw)`.

Validate version, type, numeric sequence, and required numeric command fields before replacing a pending record. Ignore malformed, unsupported, duplicate, and older messages.

- [ ] **Step 4: Convert command processors to pending records**

Make `processCommands()` consume `pendingCommand` and make `processReplayCommands()` consume `pendingReplay`. Copy record values into locals at the beginning of processing so later WebSocket callbacks cannot alter an in-flight replay decision. Keep the existing per-frame order and every replay safety branch unchanged.

- [ ] **Step 5: Build and send telemetry JSON**

Change `updateTelemetry()` to build a plain table using the exact protocol field names in the spec, construct only connected and disconnected car slots up to `math.min(sim.carsCount, 128)`, increment `packetID`, and call the WebSocket sender with `JSON.stringify(payload)`. Preserve collision flag consumption, rollover state, session generation, replay metadata, and timetable URL logic.

- [ ] **Step 6: Run Lua tests and verify telemetry behavior passes**

Run: `lua test_remote_lua.lua`

Expected: connection, cadence, and telemetry tests pass along with existing stinger/replay tests.

- [ ] **Step 7: Write failing malformed and sequence tests**

Add Lua tests that deliver a wrong version, unknown type, missing numeric field, duplicate camera sequence, replay sequence, and camera command immediately followed by replay command. Assert malformed messages do nothing, duplicates apply once, camera picks cancel shot hold, and replay messages retain their independent target shot.

- [ ] **Step 8: Run sequence tests and verify expected failures, then minimally tighten validation**

Run: `lua test_remote_lua.lua`

Expected before implementation: at least one malformed or duplicate message is applied. Add only the validation/last-sequence checks needed for the tests, then rerun until all Lua tests pass.

- [ ] **Step 9: Commit the Lua integration**

```powershell
git add -- remote.lua test_remote_lua.lua
git commit -m "feat: connect CSP app over websocket"
```

---

### Task 4: Remove mmap contract, document dependencies, and verify

**Files:**
- Delete: `ipc_shared.py`
- Modify: `readme.md`
- Modify: `AGENTS.md`
- Test: all existing `test_*.py` and `test_remote_lua.lua`

**Interfaces:**
- Consumes: completed WebSocket transport from Tasks 1–3.
- Produces: a repository with no broadcaster mmap runtime dependency and current operator/developer documentation.

- [ ] **Step 1: Write a failing repository guard test**

Add to `test_ac_ipc.py`:

```python
class RemovedMmapContractTest(unittest.TestCase):
    def test_runtime_files_do_not_reference_broadcaster_mmap(self):
        for filename in ('remote.lua', 'remote_web.py'):
            with open(filename, 'r', encoding='utf-8') as source:
                text = source.read()
            self.assertNotIn('writeMemoryMappedFile', text)
            self.assertNotIn('broadcaster_remote_telemetry', text)
            self.assertNotIn('broadcaster_remote_commands', text)
```

- [ ] **Step 2: Run the guard and verify the expected failure before cleanup**

Run: `python -m unittest test_ac_ipc.RemovedMmapContractTest -v`

Expected: failure until all old runtime references are removed.

- [ ] **Step 3: Remove the obsolete contract and update documentation**

Delete `ipc_shared.py`. Update `readme.md` to describe the local WebSocket IPC, the unchanged LAN URL, the loopback-only `/ac-ipc` route, and dependencies `flask`, `flask-socketio`, `simple-websocket`, and `requests`. Rewrite the IPC sections of `AGENTS.md` while retaining the replay, journal, offline-assets, one-hue, and CSP API rules verbatim.

- [ ] **Step 4: Run static and syntax checks**

Run:

```powershell
python -m py_compile ac_ipc.py remote_web.py auto_director.py event_log.py event_journal.py broadcast_highlight.py
git diff --check
rg -n "broadcaster_remote_(telemetry|commands)|writeMemoryMappedFile|import mmap|import ctypes|ipc_shared" remote.lua remote_web.py test_*.py
```

Expected: compilation and diff checks exit 0; `rg` returns no matches.

- [ ] **Step 5: Run the complete automated suite**

Run:

```powershell
python -m unittest discover -v
lua test_remote_lua.lua
```

Expected: all Python and Lua tests pass with no errors.

- [ ] **Step 6: Start the server for a local endpoint smoke check**

Run `python remote_web.py`, confirm it still reports/listens on port 5000, open `http://127.0.0.1:5000`, and verify a normal HTTP request from the machine succeeds. Attempting a normal non-WebSocket request to `/ac-ipc` must not expose control functionality.

- [ ] **Step 7: Perform the on-rig acceptance check**

With AC running and the Lua app enabled:

1. Open `http://<race-rig-LAN-IP>:5000` from another device.
2. Confirm driver telemetry refreshes at roughly 10 Hz.
3. Select a driver and each camera type, including an F6 sub-camera.
4. Jump to an event, seek in replay, and go live.
5. Confirm the leaderboard remains suppressed across the replay transition and returns afterward.
6. Restart `remote_web.py` and confirm Lua reconnects without reloading the app.
7. Reload the Lua app and confirm Python reconnects without restarting.

- [ ] **Step 8: Commit cleanup and documentation**

```powershell
git add -- AGENTS.md readme.md test_ac_ipc.py
git rm -- ipc_shared.py
git commit -m "docs: complete websocket IPC migration"
```

- [ ] **Step 9: Review the final branch**

Run `git status --short`, `git log -5 --oneline`, and `git diff HEAD~3 -- remote.lua remote_web.py ac_ipc.py test_ac_ipc.py test_remote_lua.lua readme.md AGENTS.md`. Confirm only the approved transport migration, tests, and documentation are present.
