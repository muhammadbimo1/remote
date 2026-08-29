# Broadcast Highlight API

This endpoint lets an Assetto Corsa Lua app tell the live-timing relay which
connected car the broadcast tower should highlight. The relay distributes the
selection to every open `/broadcast` and `/broadcast-control` instance.

## Endpoint

```http
POST /api/broadcast-highlight
Authorization: Basic <credentials>
Content-Type: application/json
```

Use the relay's public HTTPS origin, for example:

```text
https://timing.example.com/api/broadcast-highlight
```

The endpoint uses the same HTTP Basic password as `/admin` and
`/broadcast-control`. Any non-empty username is accepted; the password must
equal `adminPassword` from the relay's merged configuration. Basic credentials
are only encoded, not encrypted, so the app must use HTTPS outside a trusted
local network.

## Set the highlighted car

Send the current Assetto Corsa CarID as a non-negative JSON integer:

```json
{
  "carId": 17
}
```

CarID is resolved against the relay's current connected-driver roster. Driver
name, race number, and GUID are deliberately not accepted because they are not
stable identifiers at the Lua-app boundary.

Successful response:

```json
{
  "accepted": true,
  "changed": true,
  "control": {
    "revision": 1,
    "manual": false,
    "displayMode": "code",
    "className": null,
    "pageIndex": 0,
    "highlightedCarId": 17,
    "battleThresholdMs": 1000,
    "acceptLuaSignals": true
  }
}
```

`changed` is `false` when that CarID was already highlighted. Repeating the
same request is safe.

## Clear the highlight

Send JSON `null` as the CarID when the camera no longer has a driver target:

```json
{
  "carId": null
}
```

This clears both the primary highlight and any automatically detected battle
pair.

## Operator override

The operator can disable **Accept Lua signals** on `/broadcast-control`. While
disabled, valid requests still return HTTP 200 so the Lua app does not treat an
intentional override as an outage:

```json
{
  "accepted": false,
  "changed": false,
  "control": {
    "highlightedCarId": 17,
    "acceptLuaSignals": false
  }
}
```

The `control` object in the real response always includes every control field;
the shortened object above highlights the two relevant values. A disabled
request never changes the current target.

## Errors

All errors return JSON with an `error` string.

| Status | Meaning | App behavior |
|---|---|---|
| `400` | Missing, malformed, negative, or non-integer `carId` | Fix the request; do not retry unchanged. |
| `401` | Missing or incorrect admin password | Stop sending and surface a configuration error. |
| `404` | The CarID is not currently connected | Clear or wait for the next valid camera target. |
| `500` | Relay error | Retry with bounded backoff. |

## Sending policy

- Send only when the camera target changes; do not post every physics frame.
- Send `{ "carId": null }` when leaving a driver camera if no replacement is
  immediately available.
- Treat HTTP 200 with `accepted: false` as a successful operator override.
- Retry network failures and HTTP 500 responses with bounded exponential
  backoff, for example 0.5 s, 1 s, 2 s, then at most every 5 s.
- Do not retry HTTP 400, 401, or 404 until the input or configuration changes.
- Use a short request timeout (about 2 seconds). Highlight requests are
  idempotent, so retrying after an ambiguous timeout is safe.

## Authentication header construction

HTTP Basic sends the Base64 encoding of `username:password`:

```text
Authorization: Basic base64("lua-app:<adminPassword>")
```

Store the password in the Lua app's local configuration. Do not hard-code it in
a repository or log the header. The relay compares only the password, so
`lua-app` is a descriptive username rather than a separate account.

## Minimal integration flow

```lua
-- Pseudocode: adapt the HTTP call to the API available in the AC Lua runtime.
local lastCarId = nil

function onCameraCarChanged(carId)
  if carId == lastCarId then return end
  lastCarId = carId

  postJson(
    relayBaseUrl .. "/api/broadcast-highlight",
    { carId = carId }, -- use the JSON value null when clearing
    { Authorization = basicAuth("lua-app", adminPassword) }
  )
end
```

The future app should keep HTTP transport and camera-target detection separate:
camera code emits a changed CarID, while one small client owns serialization,
authentication, timeout, and retry behavior.
