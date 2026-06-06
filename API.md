# HTTP API

Polling JSON endpoints exposed by the relay. Modeled on the AC Server Manager Web API. WebSocket (`/ws`) remains the canonical real-time stream; these endpoints exist for tools that prefer to poll.

---

## Conventions

| | |
| --- | --- |
| Base URL | `http://<host>:<listenPort>` (default `http://localhost:3000`) |
| Auth | None |
| CORS | `Access-Control-Allow-Origin: *` on every `/api/*` response |
| Caching | `Cache-Control: no-store` on read endpoints |
| Encoding | `application/json; charset=utf-8` |
| Error shape | `{ "error": "<message>" }` with `4xx`/`5xx` status |

State is in-memory and seeded from upstream FULL_STATUS messages. If the relay is paused or upstream has not sent data yet, read endpoints return empty payloads (still `200 OK`).

---

## `GET /api/connected_drivers`

Snapshot of every currently connected driver, ordered by their position on track.

### Response

```json
{
  "ConnectedDrivers": [
    {
      "DriverName": "Example Driver",
      "DriverGUID": "12345678987654321",
      "TeamName": "GT3 7 | Apex Team",
      "Class": "GT3",
      "RaceNumber": 7,
      "CarName": "Mazda MX5 Cup",
      "Tyres": "H",
      "Position": 1,
      "IsInPits": false,
      "NumLongPits": 0,
      "NumPits": 1,
      "BlueFlag": false,
      "Split": "00:00.000",
      "GapToLeader": 0,
      "Interval": 0,
      "NumLaps": 2,
      "BestLap": 105181000000,
      "LastLap": 105181000000
    }
  ]
}
```

When no upstream data is cached, the array is empty: `{ "ConnectedDrivers": [] }`.

### Field reference

| Field | Type | Notes |
| --- | --- | --- |
| `DriverName` | string | Steam display name. |
| `DriverGUID` | string | Steam ID. Falls back to the order key if upstream omits it. |
| `TeamName` | string | Raw team name as configured upstream — typically `"<CLASS> <NUMBER> \| <Team>"`. |
| `Class` | string | Parsed from `TeamName` prefix, trailing digits stripped. `""` when `TeamName` has no `\|`. |
| `RaceNumber` | integer | `CarInfo.RaceNumber` if upstream provides it, otherwise the trailing digits of the `TeamName` prefix. `0` if neither is available. |
| `CarName` | string | Display car name. Falls back to `CarModel` when upstream has not supplied a friendly name. |
| `Tyres` | string | Short tyre code (`"H"`, `"M"`, `"S"`, …). Empty until the driver leaves the pits. |
| `Position` | integer | Live position on track. `0` before the session starts. |
| `IsInPits` | boolean | `true` while in pit lane. |
| `NumLongPits` | integer | Count of long-duration pit stops. |
| `NumPits` | integer | Count of pit stops. |
| `BlueFlag` | boolean | Currently always `false` — upstream does not surface this yet; reserved for forward compatibility. |
| `Split` | string\|number | Raw gap-to-leader as upstream reports it — usually a numeric millisecond value, sometimes `"MM:SS.mmm"`, sometimes `"1 Lap"` for lapped cars. Falls back to `"00:00.000"` before any session data arrives. |
| `GapToLeader` | integer | Gap to the leader in **milliseconds**, parsed from `Split`. `0` for the leader and `0` when the gap is non-numeric (e.g. lapped) — fall back to `Split` for the display string. |
| `Interval` | integer | Gap in **milliseconds** to the car directly ahead in `ConnectedDrivers` order, computed as the difference between consecutive `GapToLeader` values. `0` for the leader, and `0` whenever either the current or preceding car has a non-numeric `Split` (so the running calculation doesn't span a lapped car). |
| `NumLaps` | integer | Laps completed in the current session. |
| `BestLap` | integer | Best lap time in **nanoseconds**. `0` if no valid lap yet. |
| `LastLap` | integer | Most recent lap time in **nanoseconds**. `0` if no completed lap yet. |

### Class & race number parsing

`TeamName` is configured upstream as `"CLASS NUMBER | Team Name"`, e.g. `"GT3 7 | Apex Team"`.

| `TeamName` | `Class` | `RaceNumber` (when `CarInfo.RaceNumber` is absent) |
| --- | --- | --- |
| `"GT3 7 \| Apex Team"` | `"GT3"` | `7` |
| `"GTC \| Endurance Squad"` | `"GTC"` | `0` |
| `"NoPipeFormat"` | `""` | `0` |
| `""` or missing | `""` | `0` |

The same regex powers the `extractClass` helpers in `frontend/js/broadcast.js` and `frontend/js/podium.js` — keep all three sites in sync.

### Polling

Upstream FULL_STATUS arrives ~1/sec, so polling faster than `1 Hz` only repeats data. `2–5 s` is plenty for overlays and scrapers.

### Example

```bash
curl -s http://localhost:3000/api/connected_drivers | jq '.ConnectedDrivers[] | {Position, DriverName, Class, BestLap}'
```

```powershell
Invoke-RestMethod http://localhost:3000/api/connected_drivers
```

---

## Other endpoints

These already existed before the polling API was added. Listed here for completeness.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/config` | Returns `{ upstreamContentUrl, upstreamQuery, server, enabled }` — used by the frontend to resolve content paths and read relay state. |
| `POST` | `/api/server` | Body `{ "server": <int 0-255> }`. Switches which upstream race server the relay pulls from. Responds `{ server, changed }`. |
| `POST` | `/api/relay` | Body `{ "enabled": <bool> }`. Pauses or resumes the upstream connection. Responds `{ enabled, changed }`. |
| `GET` | `/api/collisions` | Returns `{ counts: { <GUID>: { env, car, name, teamName } } }` — per-driver collision tallies for the current session. |
| `WS` | `/ws` | Real-time event stream. Each message is `{ EventType, Message }`; new clients receive a cached `FULL_STATUS` (event 200) on connect. See `relay/state-cache.js` for the full event-type table. |

---

## Implementation pointers

- Route table: `relay/server.js`
- Payload builders: `relay/state-cache.js` (`getConnectedDriversPayload`, `getCollisionCountsPayload`, `parseTeamName`)
- Cloudflare Worker mirror: `worker/relay-do.js` — the polling endpoint is **not yet** mirrored there; only the Node relay serves `/api/connected_drivers`.
