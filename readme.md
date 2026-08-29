
## What This Is

**Broadcaster Remote** — an Assetto Corsa CSP Lua app that exposes broadcast camera and driver controls via a web interface. It has two runtime halves communicating through shared memory (mmap)

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
