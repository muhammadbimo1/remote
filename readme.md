
## What This Is

**Broadcaster Remote** — an Assetto Corsa CSP Lua app that exposes broadcast camera and driver controls through a web interface.

The CSP app connects to the Python server over a native CSP WebSocket at
`ws://127.0.0.1:5000/ac-ipc`. It sends telemetry at 10 Hz and receives camera
and replay commands over the same connection. The IPC route is loopback-only,
while the browser panel continues to listen on `0.0.0.0:5000` and is accessible
from other devices on the same network at `http://<race-rig-ip>:5000`.

## Running

- Enable **Broadcaster Remote** in AC's app sidebar (CSP required).
- Install Python dependencies: `flask`, `flask-socketio`,
  `simple-websocket`, and `requests`.
- Start the server with `python remote_web.py`.

The Lua WebSocket reconnects automatically if the Python server is restarted.
The Python server accepts a replacement connection if the Lua app is reloaded.

## Independent PiP

Each eligible driver card in the web panel has a **PiP** button. Selecting it
opens Assetto Corsa's movable **Driver PiP** app window with that driver's
onboard camera. **Cam -** and **Cam +** cycle only that driver's available
onboard cameras, and **PiP Off** closes the window.

PiP is independent from the program shot: selecting its driver, changing its
camera, moving or resizing its window, and hiding it do not change the main
broadcast driver or camera. The rendered picture stays at a centered 16:9
aspect ratio when the app window is resized.

Use the settings button on the **Driver PiP** window to adjust brightness,
gamma, white point, and individual red, green, and blue gains. Changes apply
live and persist across app reloads; **Reset color** restores the defaults.

## Broadcast highlight relay

Copy `remote_config.example.json` to `remote_config.json` (a disabled local
file is created by default), then set the relay URL and its admin password:

```json
{
  "broadcastHighlight": {
    "enabled": true,
    "url": "https://timing.example.com/api/broadcast-highlight",
    "username": "lua-app",
    "password": "your-admin-password"
  }
}
```

The web server posts only when the focused car changes. CSP local car indices
are translated through `session_id`, so the published `carId` is the same
remote/server CarID used by `timetable.json` lap-count synchronization. The
password stays in the git-ignored local config and is never logged.
