
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
