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

- **CSP Lua Skill**: Use the [CSP Lua skill](./.claude/skills/csp-lua/SKILL.md) for API reference. Update it when it points you into the wrong place.
- **Full API Reference**: See [.claude/skills/csp-lua/reference/lib.lua](./.claude/skills/csp-lua/reference/lib.lua) for complete CSP type definitions (17k+ lines).