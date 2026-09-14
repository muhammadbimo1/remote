# Independent Onboard Picture-in-Picture

## Goal

Add a small movable Assetto Corsa app window that shows an onboard view from a driver selected independently of the main broadcast shot. The browser operator can select the PiP driver, cycle that driver's onboard cameras, and enable or disable the PiP without changing the main focused car or camera.

## Acceptance criteria

- The browser panel has a PiP enable/disable control, previous/next onboard-camera controls, and a dedicated PiP action on every connected driver row.
- Selecting a PiP driver does not call the existing main camera command path and does not change `sim.focusedCar` or `sim.cameraMode`.
- Enabling PiP opens a separate movable CSP window and renders the selected driver's onboard camera.
- Disabling PiP closes the CSP window and stops updating its render texture.
- Selecting another driver while PiP is disabled selects that driver and enables PiP in one action.
- Invalid, disconnected, or camera-less targets do not change the main broadcast shot; the PiP window shows a concise unavailable state or remains hidden.
- A Python server restart cannot make a stale PiP command collide with an earlier sequence number.
- Existing camera, replay, telemetry, auto-director, and browser controls continue to work.

## Non-goals

- Trackside, helicopter, orbit, or free-camera modes inside PiP.
- Multiple simultaneous PiP windows.
- Audio from the PiP driver.
- PiP compositing into OBS or the main game framebuffer.
- CMRT's duplicate-track and duplicate-car rendering workaround in the first pass.
- Changing the main broadcast camera when PiP state changes.

## Architecture

### IPC

Add a third independent receive sequence, `pip_seq`, alongside `command_seq` and `replay_seq`. Python sends messages with:

```json
{
  "version": 1,
  "connection_id": "server epoch",
  "type": "pip",
  "pip_seq": 1,
  "pip_enabled": true,
  "target_driver": 4,
  "target_car_camera": 0
}
```

`pip_enabled` is a JSON boolean. Driver and camera indices are zero-based integers. A changed `connection_id` resets all three Lua receive counters. PiP commands never bump `command_seq` or `replay_seq` and never enter the replay shot-hold path.

`ACIPCTransport` owns `pip_seq` and exposes `send_pip_command(enabled, target_driver, target_car_camera)`. `remote_web.py` keeps the operator's current PiP state so browser updates are coherent and sends the full state for every PiP action.

### Browser controls

The existing driver rows gain a dedicated `PiP` button beside the main-shot action. The control bar gains:

- `PiP ON/OFF`, reflecting server-side PiP state;
- `CAM -` and `CAM +`, which wrap through the selected driver's advertised `car_cameras_count`;
- a compact readout containing the selected driver and onboard-camera number.

Clicking a driver's `PiP` button selects that driver, resets its PiP camera to camera 0, and enables PiP. Main camera buttons and the existing driver-selection behavior remain unchanged. Browser-provided indices are validated against the latest live telemetry before a command is sent.

### Lua PiP state

Lua owns only the state needed to render the latest accepted command:

- enabled;
- target driver index;
- onboard camera index;
- most recently applied `pip_seq`.

On enable, Lua calls `ac.setWindowOpen('pip', true)`. On disable, it calls `ac.setWindowOpen('pip', false)` and skips all geometry-shot updates. Closing the window manually does not affect the main shot; a later server enable or target selection opens it again.

### Camera calculation

For the selected driver, Lua loads `cameras.ini` with `ac.INIConfig.carData(driver, 'cameras.ini')` and reads `CAMERA_n` values `POSITION`, `FORWARD`, `UP`, and `FOV`. It converts these car-local values to world-space camera position and orientation using the selected car's `bodyTransform`, following the transform order already proven by CMRT Broadcast HUD.

If the requested camera section is missing or invalid, Lua attempts camera 0. If camera 0 is also invalid, it does not call `GeometryShot:update()` and the window displays an unavailable message.

### Rendering and window

Create a single 640×360 `ac.GeometryShot` from `ac.findNodes('sceneRoot:yes')`. Configure it for original lighting, sky, transparent surfaces, main shaders, fake car shadows, and the maximum scene layer. Update it only while PiP is enabled, the window is open, and the target camera is valid, using `render.onSceneReady()`.

Add a manifest window with ID `pip`, a 16:9-oriented default size, a normal movable title bar, and a `windowPiP` callback. The callback letterboxes the geometry-shot texture into the available content area without stretching it. It also overlays the driver's name and onboard-camera number in the existing monochrome style.

The first pass renders the existing scene directly. It does not load duplicate KN5 track or car geometry. If rig testing shows missing distant geometry, incorrect LOD, or absent cockpit meshes, that observation is the explicit trigger for a separate CMRT-style rendering phase.

## Error handling and lifecycle

- Reject malformed PiP messages without mutating PiP state.
- Ignore duplicate `pip_seq` values.
- Clamp or reject targets using current `sim.carsCount`, connection state, and available camera count.
- A missing `cameras.ini` section produces an unavailable PiP state, never a main-camera fallback.
- Disabling PiP immediately stops the additional scene render.
- The one geometry-shot object is reused instead of recreated on driver or camera changes.

## Testing

Python tests cover message shape, sequence independence, epoch behavior, server-side validation, state transitions, and browser update data. Lua harness tests cover malformed and duplicate PiP messages, independence from main focus/camera, enable/disable window calls, camera fallback, and render gating.

After automated tests pass, rig verification checks:

1. Select a different PiP driver while the main camera remains unchanged.
2. Cycle through available onboard cameras.
3. Move and resize the PiP window.
4. Disable PiP and confirm the window closes and rendering stops.
5. Re-enable PiP and confirm the last server-selected target is restored.
6. Observe distant drivers for missing track, car, or cockpit geometry.

