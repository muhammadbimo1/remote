# Independent Onboard Picture-in-Picture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a server-controlled movable CSP window showing an onboard camera from a driver selected independently of the main broadcast shot.

**Architecture:** A new `pip` WebSocket message has its own epoch-aware sequence counter and carries the complete desired PiP state. The Lua app renders one reusable `ac.GeometryShot` from the existing scene and opens or closes a dedicated manifest window; the Python server validates operator actions against fresh telemetry and exposes the state to the existing Amber Console browser panel.

**Tech Stack:** CSP Lua app APIs (`ac.GeometryShot`, `render.onSceneReady`, `ac.setWindowOpen`), UTF-8 JSON over loopback WebSocket, Python 3, Flask-SocketIO, unittest, Lua test harness, vendored Amber Console CSS.

**Spec:** `docs/superpowers/specs/2026-09-15-independent-pip-design.md`

## Global Constraints

- PiP is independent of `sim.focusedCar`, `sim.cameraMode`, `command_seq`, `replay_seq`, and replay shot hold.
- PiP supports one driver and onboard `CAMERA_n` views only.
- The initial render uses the existing `sceneRoot`; it does not duplicate track or car KN5 geometry.
- The browser and all server assets remain offline-capable; add no external links, scripts, fonts, or dependencies.
- The browser UI remains monochrome amber; state uses labels, brightness, and inverse video rather than a second hue.
- Never use shell commands or external processes from Lua.
- The `/ac-ipc` peer remains restricted using the actual loopback address, never forwarded headers.
- Disabled PiP closes its window and performs no geometry-shot update.
- Existing telemetry, camera, replay, event, auto-director, and OBS HUD behavior must remain unchanged.

## File structure

- `ac_ipc.py`: validate the new per-car onboard-camera count and own `pip_seq` plus PiP message serialization.
- `remote.lua`: receive PiP commands, resolve onboard camera transforms, own the reusable geometry shot, and draw the PiP window.
- `manifest.ini`: register the movable `pip` app window.
- `remote_web.py`: own browser-facing PiP state, validate operator actions, send complete PiP commands, and render the controls.
- `static/remote.css`: lay out PiP controls using the existing Amber Console visual language.
- `test_ac_ipc.py`: prove telemetry validation and sequence/message independence.
- `test_remote_lua.lua`: prove command validation, epoch behavior, main-shot independence, render gating, fallback, and window visibility.
- `test_remote_web.py`: prove server state transitions, validation, output data, and browser markup/events.
- `API.md` and `readme.md`: document the new protocol and operator behavior.

---

### Task 1: Extend the transport contract for PiP

**Files:**
- Modify: `test_ac_ipc.py`
- Modify: `test_remote_lua.lua`
- Modify: `ac_ipc.py`
- Modify: `remote.lua`

**Interfaces:**
- Consumes: existing `ACIPCTransport._send(payload)` and `connection_id` epoch.
- Produces: `CarSnapshot.car_cameras_count: int` and `ACIPCTransport.send_pip_command(enabled: bool, target_driver: int, target_car_camera: int, expected_generation: int | None = None) -> bool`.

- [ ] **Step 1: Write failing transport and validation tests**

Add `car_cameras_count: 3` to the valid Python car-message fixture. Add tests equivalent to:

```python
def test_car_camera_count_is_required_and_validated(self):
    message = telemetry_message([car_message()])
    del message['cars'][0]['car_cameras_count']
    with self.assertRaises(ProtocolError):
        TelemetrySnapshot.from_message(message)

    message = telemetry_message([car_message()])
    message['cars'][0]['car_cameras_count'] = 3
    snapshot = TelemetrySnapshot.from_message(message)
    self.assertEqual(snapshot.cars[0].car_cameras_count, 3)

def test_pip_sequence_is_independent(self):
    transport = ACIPCTransport(connection_id='server-a')
    socket = FakeSocket()
    transport.attach(socket)
    self.assertTrue(transport.send_command(1, 2, -1))
    self.assertTrue(transport.send_pip_command(True, 4, 0))
    self.assertTrue(transport.send_replay_command(2))
    self.assertEqual(socket.sent[0]['command_seq'], 1)
    self.assertEqual(socket.sent[1]['pip_seq'], 1)
    self.assertEqual(socket.sent[2]['replay_seq'], 1)

def test_pip_message_carries_complete_state_and_epoch(self):
    transport = ACIPCTransport(connection_id='server-a')
    socket = FakeSocket()
    transport.attach(socket)
    self.assertTrue(transport.send_pip_command(False, 7, 2))
    self.assertEqual(socket.sent[-1], {
        'version': 1,
        'connection_id': 'server-a',
        'type': 'pip',
        'pip_seq': 1,
        'pip_enabled': False,
        'target_driver': 7,
        'target_car_camera': 2,
    })
```

In `test_remote_lua.lua`, add a telemetry assertion that the first emitted car contains `car_cameras_count = 3`, matching the harness car fixture.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m unittest test_ac_ipc -v
lua test_remote_lua.lua
```

Expected: Python failures report the missing `car_cameras_count` snapshot field and missing `send_pip_command` method. `lua test_remote_lua.lua` fails its new telemetry assertion because the Lua payload does not yet advertise the count.

- [ ] **Step 3: Add the per-car field and PiP counter**

In `CarSnapshot`, add `car_cameras_count` to the required scalar field set and parse it as a bounded non-negative integer. In `ACIPCTransport.__init__`, add `self._pip_seq = 0`. Implement:

```python
def send_pip_command(self, enabled, target_driver, target_car_camera,
                     expected_generation=None):
    with self._lock:
        if (expected_generation is not None and
                self._generation != expected_generation):
            return False
        self._pip_seq += 1
        return self._send({
            'version': PROTOCOL_VERSION,
            'connection_id': self._connection_id,
            'type': 'pip',
            'pip_seq': self._pip_seq,
            'pip_enabled': bool(enabled),
            'target_driver': int(target_driver),
            'target_car_camera': int(target_car_camera),
        })
```

Do not alter `_command_seq` or `_replay_seq` in this method.

In the car object assembled by `remote.lua`, publish:

```lua
car_cameras_count = car.carCamerasCount,
```

This keeps the newly required Python telemetry contract compatible in the same commit.

- [ ] **Step 4: Run the focused test module and verify GREEN**

Run:

```powershell
python -m unittest test_ac_ipc -v
lua test_remote_lua.lua
```

Expected: all `test_ac_ipc` and Lua tests pass.

- [ ] **Step 5: Commit the transport slice**

```powershell
git add ac_ipc.py remote.lua test_ac_ipc.py test_remote_lua.lua
git commit -m "feat: add independent PiP transport"
```

---

### Task 2: Add server-side PiP state and validated actions

**Files:**
- Modify: `test_remote_web.py`
- Modify: `remote_web.py`

**Interfaces:**
- Consumes: `ACIPCTransport.send_pip_command(enabled, target_driver, target_car_camera, expected_generation=None)` and `CarSnapshot.car_cameras_count` from Task 1.
- Produces: `get_pip_state() -> dict`, `apply_pip_action(data: dict) -> tuple[bool, dict, str | None]`, Socket.IO event `pip_control`, and update payload key `pip`.

- [ ] **Step 1: Write failing state-transition tests**

Create a connected telemetry fixture with driver 4 connected and `car_cameras_count = 3`. Patch `ac_transport.latest` and `send_pip_command`, then cover:

```python
def test_selecting_pip_driver_enables_camera_zero_without_main_command(self):
    with patch.object(remote_web.ac_transport, 'latest', return_value=self.telem), \
            patch.object(remote_web.ac_transport, 'latest_with_generation',
                         return_value=(self.telem, 8)), \
            patch.object(remote_web.ac_transport, 'send_pip_command', return_value=True) as send_pip, \
            patch.object(remote_web, 'send_command') as send_main:
        ok, state, error = remote_web.apply_pip_action(
            {'action': 'select', 'driver': 4})
    self.assertTrue(ok, error)
    self.assertEqual(state, {'enabled': True, 'driver': 4,
                             'camera': 0, 'camera_count': 3})
    send_pip.assert_called_once_with(True, 4, 0, expected_generation=8)
    send_main.assert_not_called()

def test_camera_delta_wraps_with_selected_drivers_count(self):
    remote_web._pip_state.update(enabled=True, driver=4,
                                 camera=2, camera_count=3)
    ok, state, error = remote_web.apply_pip_action(
        {'action': 'camera', 'delta': 1})
    self.assertTrue(ok, error)
    self.assertEqual(state['camera'], 0)

def test_disable_sends_full_state_without_fresh_telemetry(self):
    remote_web._pip_state.update(enabled=True, driver=4,
                                 camera=1, camera_count=3)
    ok, state, error = remote_web.apply_pip_action({'action': 'enable',
                                                     'enabled': False})
    self.assertTrue(ok, error)
    self.assertFalse(state['enabled'])
```

Also test rejection of stale telemetry, offline drivers, zero-camera drivers, non-integer driver/delta values, and enabling before a valid driver has been selected.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m unittest test_remote_web.PiPStateTest -v
```

Expected: failure because `_pip_state` and `apply_pip_action` do not exist.

- [ ] **Step 3: Implement synchronized state and action validation**

Add a dedicated `pip_lock` and this initial state near the other server state:

```python
_pip_state = {
    'enabled': False,
    'driver': -1,
    'camera': 0,
    'camera_count': 0,
}
```

Implement `get_pip_state()` as a locked copy. Implement `apply_pip_action(data)` with exactly three actions. For actions requiring telemetry, read the snapshot and generation atomically with `latest_with_generation()` and pass that generation to `send_pip_command(..., expected_generation=generation)`:

- `select`: require fresh telemetry, a zero-based driver within `telem.cars`, `is_connected`, and `car_cameras_count > 0`; set driver, camera 0, camera count, enabled true.
- `camera`: require an already selected driver and `delta` equal to `-1` or `1`; calculate `(camera + delta) % camera_count`.
- `enable`: coerce only actual JSON booleans; disabling is always valid, while enabling requires a selected driver with a positive camera count.

Send the complete candidate state with `ac_transport.send_pip_command`. Commit the candidate into `_pip_state` only after `_send` succeeds. Return `(False, old_state, concise_error)` on validation or send failure.

- [ ] **Step 4: Add the Socket.IO handler and fresh-generation synchronization test**

First add a failing test that directly invokes the handler under a Flask request context, patches `apply_pip_action`, and asserts a `pip_status` emit. Add a separate failing monitor helper test asserting that the first valid telemetry snapshot for a new connection generation sends the current complete PiP state with `expected_generation` set. This makes a Python restart close the old Lua PiP deterministically while avoiding a command before the new AC socket has published telemetry.

Then implement:

```python
@socketio.on('pip_control')
def handle_pip_control(data):
    ok, state, error = apply_pip_action(data or {})
    emit('pip_status', {'ok': ok, 'pip': state, 'error': error},
         broadcast=True)
```

Implement `sync_pip_state(telem, connection_generation)`. If an enabled target is not connected or has no cameras in the new snapshot, reset it to the disabled/no-target state. Send the resulting full state with the generation guard. Call this helper once from `monitor_telemetry()` when it observes a new connection generation, after any old-run reset and before publishing the first browser update. Use the existing transport `connection_id` epoch; do not synthesize a main camera command.

- [ ] **Step 5: Add PiP data to browser updates**

Write a failing `build_update_data` assertion, then add:

```python
'camera_count': c.get('car_cameras_count', 0),
```

to each driver dictionary and:

```python
'pip': get_pip_state(),
```

to both live and replay update payloads. Reset `_pip_state` to disabled/no target in `reset_ac_run_state()` and update its existing reset test.

- [ ] **Step 6: Run server tests and verify GREEN**

Run:

```powershell
python -m unittest test_ac_ipc test_remote_web -v
```

Expected: both modules pass with PiP state independent from main camera state.

- [ ] **Step 7: Commit the server slice**

```powershell
git add remote_web.py test_remote_web.py
git commit -m "feat: add server-side PiP controls"
```

---

### Task 3: Receive PiP commands without touching the main shot

**Files:**
- Modify: `test_remote_lua.lua`
- Modify: `remote.lua`

**Interfaces:**
- Consumes: protocol message `{type='pip', pip_seq, pip_enabled, target_driver, target_car_camera}`.
- Produces: accepted Lua PiP state, `processPiPCommands()`, and calls `ac.setWindowOpen('pip', enabled)`.

- [ ] **Step 1: Extend the Lua harness and write failing command tests**

Record calls made to `ac.setWindowOpen`, expose them from `loadRemote`, and add tests covering:

```lua
local function testPiPCommandDoesNotChangeMainShot()
  local ctx = loadRemote(false)
  ctx.sim.focusedCar = 2
  ctx.sim.cameraMode = ac.CameraMode.Track
  ctx.deliver({
    version = 1, connection_id = 'server-a', type = 'pip', pip_seq = 1,
    pip_enabled = true, target_driver = 4, target_car_camera = 1,
  })
  ctx.update()
  assertEqual(ctx.sim.focusedCar, 2, 'PiP keeps main focus')
  assertEqual(ctx.sim.cameraMode, ac.CameraMode.Track, 'PiP keeps main camera')
  assertEqual(ctx.windowOpenCalls[#ctx.windowOpenCalls].id, 'pip',
    'PiP targets its own window')
  assertEqual(ctx.windowOpenCalls[#ctx.windowOpenCalls].opened, true,
    'enabled command opens PiP')
end
```

Add tests proving duplicate `pip_seq` is ignored, malformed boolean/number fields are rejected, a changed `connection_id` accepts `pip_seq = 1` again, and disable calls `ac.setWindowOpen('pip', false)`. Assert none of these calls `ac.focusCar`, `ac.setCurrentCamera`, or `ac.setCurrentCarCamera`.

- [ ] **Step 2: Run the Lua suite and verify RED**

Run:

```powershell
lua test_remote_lua.lua
```

Expected: the first PiP assertion fails because the parser does not accept `type = 'pip'` and no PiP window call is made.

- [ ] **Step 3: Add independent PiP parsing and processing**

Add `lastPiPSeq`, `pendingPiP`, and the render-state fields `pipEnabled`, `pipDriver`, and `pipCamera`. Reset the PiP sequence and pending value inside the existing `acceptConnection()` epoch reset.

In `onIPCMessage`, accept `type == 'pip'` only when:

```lua
type(message.connection_id) == 'string' and message.connection_id ~= ''
and validNumber(message.pip_seq)
and type(message.pip_enabled) == 'boolean'
and validNumber(message.target_driver)
and validNumber(message.target_car_camera)
```

`processPiPCommands()` ignores the duplicate sequence, stores the three desired values, and calls `ac.setWindowOpen('pip', pipEnabled)`. Call it from `script.update()` beside the existing camera and replay processors without clearing `shotHold`.

- [ ] **Step 4: Run the Lua suite and verify GREEN**

Run `lua test_remote_lua.lua`.

Expected: all existing and new command tests pass.

- [ ] **Step 5: Commit the independent command receiver**

```powershell
git add remote.lua test_remote_lua.lua
git commit -m "feat: receive independent PiP state"
```

---

### Task 4: Render the selected onboard view in a movable window

**Files:**
- Modify: `test_remote_lua.lua`
- Modify: `remote.lua`
- Modify: `manifest.ini`

**Interfaces:**
- Consumes: `pipEnabled`, `pipDriver`, and `pipCamera` established in Task 3.
- Produces: `resolvePiPCamera(driver, cameraIndex)`, one reusable `pipShot`, `render.onSceneReady` callback, and `script.windowPiP(dt)`.

- [ ] **Step 1: Add geometry-shot and camera fixtures to the Lua harness**

Provide deterministic stubs for:

- `render.onSceneReady(callback)`, storing the callback for explicit invocation;
- `ac.GeometryShot`, recording construction, configuration calls, and `update(pos, look, up, fov)`;
- `ac.findNodes('sceneRoot:yes')`;
- `ac.INIConfig.carData(driver, 'cameras.ini')` returning camera 0 and camera 1 vectors/FOV;
- `car.bodyTransform:transformPoint(vec)` and `transformVector(vec)` returning recognizable world-space values;
- `vec3`, vector normalization needed by production code;
- `ui.availableSpace`, `ui.drawImage`, and text calls used by `windowPiP`.

Expose `runSceneReady()` and recorded shot/window calls from `loadRemote`.

Add this message helper beside the existing replay helper:

```lua
local function validPiPCommand(enabled, driver, camera)
  return {
    version = 1,
    connection_id = 'server-a',
    type = 'pip',
    pip_seq = 1,
    pip_enabled = enabled,
    target_driver = driver,
    target_car_camera = camera,
  }
end
```

- [ ] **Step 2: Write failing render-gating and transform tests**

Cover four observable behaviors:

```lua
local function testDisabledPiPDoesNotRender()
  local ctx = loadRemote(false)
  ctx.runSceneReady()
  assertEqual(ctx.geometryUpdates(), 0, 'disabled PiP skips scene render')
end

local function testEnabledPiPRendersSelectedOnboardCamera()
  local ctx = loadRemote(false)
  ctx.deliver(validPiPCommand(true, 4, 1))
  ctx.update()
  ctx.runSceneReady()
  local update = ctx.lastGeometryUpdate()
  assertEqual(update.fov, 72, 'selected cameras.ini FOV is used')
  assertEqual(update.pos.x, 104, 'camera position is transformed by target car')
  assertEqual(ctx.sim.focusedCar, 0, 'render leaves main focus unchanged')
end
```

Also test that a missing requested `CAMERA_n` falls back to `CAMERA_0`, and that a missing camera 0 performs no update and makes `windowPiP` draw `PiP unavailable`.

- [ ] **Step 3: Run Lua tests and verify RED**

Run `lua test_remote_lua.lua`.

Expected: failures report no geometry-shot update and no PiP drawing callback.

- [ ] **Step 4: Implement camera resolution**

Implement a helper which validates the selected car is connected and the requested index is within `car.carCamerasCount`. Read `POSITION`, `FORWARD`, `UP`, and `FOV` from `CAMERA_<index>`; retry camera 0 once when any required direction or FOV is invalid. Transform with:

```lua
local transform = car.bodyTransform
local worldPosition = transform:transformPoint(localPosition)
local worldForward = math.normalize(transform:transformVector(localForward))
local worldUp = math.normalize(transform:transformVector(localUp))
return worldPosition, worldForward, worldUp, fov, actualCameraIndex
```

Return `nil` values without calling any main-camera API when both sections are invalid.

- [ ] **Step 5: Implement the reusable geometry shot and render gate**

Lazily create exactly one 640×360 shot:

```lua
pipShot = ac.GeometryShot(
  ac.findNodes('sceneRoot:yes'), vec2(640, 360), 1, false,
  render.AntialiasingMode.FXAA,
  render.TextureFormat.R8G8B8A8.UNorm, 0)
pipShot:setName('Broadcaster Remote PiP')
pipShot:setSky(true)
pipShot:setOriginalLighting(true)
pipShot:setShadersType(render.ShadersType.Main)
pipShot:setFakeCarShadows(true)
pipShot:setMaxLayer(5)
pipShot:setOpaqueAlphaFix(true)
pipShot:setTransparentPass(true)
```

Register `render.onSceneReady(function() ... end)`. Return immediately unless PiP is enabled and `ac.isWindowOpen('pip')`. Resolve the camera and call `pipShot:update(...)`; otherwise store an unavailable flag. Reuse the object across all selections and dispose it from the existing release callback.

- [ ] **Step 6: Register and draw the movable PiP window**

Add to `manifest.ini`:

```ini
[WINDOW_...]
ID = pip
NAME = Driver PiP
FUNCTION_MAIN = windowPiP
SIZE = 480, 300
PADDING = 0
FLAGS = NO_SCROLLBAR, NO_SCROLL_WITH_MOUSE
```

Implement `script.windowPiP(dt)` to compute the largest centered 16:9 rectangle within `ui.availableSpace()`, draw black letterboxing, wrap the image in `ui.beginTonemapping()`/`ui.endTonemapping(1, 0, true)`, and overlay the driver name plus `CAM <n>`. When no valid frame exists, draw `PiP unavailable` instead of stale imagery.

- [ ] **Step 7: Run Lua tests and verify GREEN**

Run `lua test_remote_lua.lua`.

Expected: all tests pass, including disabled rendering, fallback, and main-shot independence.

- [ ] **Step 8: Commit the rendered window**

```powershell
git add remote.lua manifest.ini test_remote_lua.lua
git commit -m "feat: render movable onboard PiP window"
```

---

### Task 5: Add the Amber Console browser controls

**Files:**
- Modify: `test_remote_web.py`
- Modify: `remote_web.py`
- Modify: `static/remote.css`

**Interfaces:**
- Consumes: Socket.IO event `pip_control`, response/update key `pip`, and driver `camera_count` from Task 2.
- Produces: PiP toolbar, dedicated driver-row selection buttons, camera-cycle actions, and visible selected/disabled state.

- [ ] **Step 1: Write failing markup and event-wiring tests**

Fetch `/` with the Flask client and assert the returned HTML contains:

```python
self.assertIn('id="pip-toggle"', html)
self.assertIn('id="pip-camera-prev"', html)
self.assertIn('id="pip-camera-next"', html)
self.assertIn("socket.emit('pip_control'", html)
self.assertIn('data-pip-driver', html)
self.assertIn("e.target.closest('button[data-pip-driver]')", html)
```

Also assert the dedicated PiP button branch calls `e.preventDefault()` and `e.stopPropagation()` before returning, so clicking PiP cannot fall through to the existing whole-card main-shot selection.

- [ ] **Step 2: Run the markup tests and verify RED**

Run:

```powershell
python -m unittest test_remote_web.PiPMarkupTest -v
```

Expected: assertions fail because the PiP controls are absent.

- [ ] **Step 3: Add the toolbar and local event functions**

Add a compact group beside the existing main camera controls:

```html
<div class="pip-controls" aria-label="Picture-in-picture controls">
  <button id="pip-toggle" class="ac-btn" aria-pressed="false">PiP OFF</button>
  <button id="pip-camera-prev" class="ac-btn" aria-label="Previous PiP onboard camera">Cam -</button>
  <button id="pip-camera-next" class="ac-btn" aria-label="Next PiP onboard camera">Cam +</button>
  <span id="pip-readout" class="ac-readout ac-readout--inline">PiP --</span>
</div>
```

Bind the controls with `pointerdown` handlers emitting:

```javascript
socket.emit('pip_control', {action: 'enable', enabled: !pipState.enabled});
socket.emit('pip_control', {action: 'camera', delta: -1});
socket.emit('pip_control', {action: 'camera', delta: 1});
```

Keep `pipState` synchronized from both ordinary `update` messages and `pip_status` broadcasts.

- [ ] **Step 4: Add the per-driver PiP action without changing card selection**

For each connected driver, append a labeled button:

```javascript
grid += '<button type="button" class="ac-btn ac-btn--sm pip-target"'
  + ' data-pip-driver="' + (d.num - 1) + '" aria-pressed="' + isPipTarget + '">PiP</button>';
```

At the start of the existing `drivers-list` delegated `pointerdown` handler, detect `button[data-pip-driver]`, stop propagation, emit `{action: 'select', driver: ...}`, and return. Offline drivers do not receive an enabled PiP button. Do not mark the entire card disabled merely because it is the main selected driver; PiP selection remains independently available.

- [ ] **Step 5: Render server-confirmed state and monochrome styles**

Implement `renderPiPState(state, drivers)` to update:

- toggle label and `aria-pressed`;
- selected row PiP button `aria-pressed`;
- camera buttons disabled when PiP is off or no target exists;
- readout text `<driver name> · CAM <camera + 1>`.

In `static/remote.css`, use existing ink variables and established `.ac-btn` styles. Add layout only for `.pip-controls` and `.pip-target`; do not introduce red, green, gradients, external assets, or a new font.

- [ ] **Step 6: Run browser/server tests and verify GREEN**

Run:

```powershell
python -m unittest test_remote_web -v
```

Expected: all markup, event, state, and pre-existing web tests pass.

- [ ] **Step 7: Commit the operator UI**

```powershell
git add remote_web.py static/remote.css test_remote_web.py
git commit -m "feat: control PiP from web panel"
```

---

### Task 6: Document, verify, and stop

**Files:**
- Modify: `API.md`
- Modify: `readme.md`
- Test: all `test_*.py`
- Test: `test_remote_lua.lua`

**Interfaces:**
- Consumes: completed PiP protocol and operator workflow.
- Produces: documented contract and final verification evidence.

- [ ] **Step 1: Document the operator behavior and protocol**

In `readme.md`, add a short “Independent PiP” section explaining that a driver-row `PiP` button opens a movable onboard window, `Cam -/+` cycles that driver's cameras, and `PiP OFF` closes the window without touching the program shot.

In `API.md`, document the `pip` message fields, zero-based indices, `pip_seq` independence, `connection_id` reset behavior, boolean validation, and that disabled PiP performs no render updates.

- [ ] **Step 2: Run whitespace and placeholder checks**

Run:

```powershell
git diff --check
rg -n "TBD|TODO|FIXME" API.md readme.md ac_ipc.py remote.lua remote_web.py static/remote.css test_ac_ipc.py test_remote_lua.lua test_remote_web.py
```

Expected: `git diff --check` exits 0. Review `rg` output only for pre-existing intentional comments; no newly added placeholder remains.

- [ ] **Step 3: Run the complete automated test suites**

Run:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -m unittest discover -s . -p 'test_*.py' -v
lua test_remote_lua.lua
```

Expected: all Python and Lua tests pass with no traceback or Lua assertion failure.

- [ ] **Step 4: Run the distribution build**

Run:

```powershell
.\build.bat
```

Expected: tests pass, PyInstaller succeeds, and the script prints `Build complete` for `dist\remote`. If `lua.exe` is unavailable, record that the build skipped Lua while retaining the explicit Lua result from Step 3.

- [ ] **Step 5: Perform the rig acceptance check**

With AC running and the Python server connected:

1. Put the main broadcast on one driver and track camera.
2. Select a different driver's `PiP` button.
3. Confirm the movable `Driver PiP` window appears without changing the main focus or camera.
4. Cycle every available onboard camera with `Cam -/+`.
5. Move and resize the window and confirm the image stays 16:9 without stretching.
6. Disable PiP and confirm the window closes.
7. Watch a distant target and record whether track, cockpit, or surrounding cars disappear or use visibly incorrect LOD.

Missing geometry in step 7 is not repaired inside this plan. It is the trigger for a separate CMRT-style duplicate-scene design.

- [ ] **Step 6: Commit documentation and any verification-only corrections**

```powershell
git add API.md readme.md
git commit -m "docs: describe independent onboard PiP"
```
