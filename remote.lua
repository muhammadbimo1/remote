local sim = ac.getSim()

---------------------------------------------------------------------
-- WebSocket IPC — CSP connects to the local web server on its existing port.
---------------------------------------------------------------------
local PROTOCOL_VERSION = 1

-- Replay actions — must match the REPLAY_* constants in ac_ipc.py.
local REPLAY_ENTER = 1
local REPLAY_LIVE = 2
local REPLAY_SEEK_FRAME = 3

-- replay_last_result values published back to the web panel
local REPLAY_RESULT_UNTRIED = 0
local REPLAY_RESULT_OK = 1
local REPLAY_RESULT_REFUSED = 2

---------------------------------------------------------------------
-- State
---------------------------------------------------------------------
local lastCommandSeq = 0
local lastReplaySeq = 0
local lastPiPSeq = 0
local lastConnectionID = nil
local pendingCommand = nil
local pendingReplay = nil
local pendingPiP = nil
local packetID = 0
local updateAccum = 0
local UPDATE_INTERVAL = 0.1 -- 100 ms

-- AC server standings are more reliable online than CSP's per-car lap and
-- position fields. Poll them here so both this app and the Python panel consume
-- one corrected telemetry stream.
local TIMETABLE_POLL_INTERVAL_S = 2.0
local timetableNextPoll = 0
local timetableRequestActive = false
local timetableStandings = {}

local pipEnabled = false
local pipDriver = -1
local pipCamera = 0
local pipActualCamera = 0
local pipFrameReady = false
local pipShot = nil
local pipColorSettings = ac.INIConfig.scriptSettings()
local pipGamma = pipColorSettings:get('PIP_COLOR', 'GAMMA', 1.0)
local pipWhitePoint = pipColorSettings:get('PIP_COLOR', 'WHITE_POINT', 0.0)
local pipRedGain = pipColorSettings:get('PIP_COLOR', 'RED_GAIN', 1.0)
local pipGreenGain = pipColorSettings:get('PIP_COLOR', 'GREEN_GAIN', 1.0)
local pipBlueGain = pipColorSettings:get('PIP_COLOR', 'BLUE_GAIN', 1.0)

local function validNumber(value)
  return type(value) == 'number'
end

local function pollTimetable()
  local now = os.preciseClock()
  if sim.raceSessionType ~= ac.SessionType.Race
      or sim.isReplayActive or sim.isReplayOnlyMode
      or timetableRequestActive or now < timetableNextPoll then return end
  local serverIP, httpPort = ac.getServerIP(), ac.getServerPortHTTP()
  if serverIP == nil or serverIP == '' or httpPort == nil or httpPort < 0 then return end

  local url = 'http://' .. serverIP .. ':' .. tostring(httpPort) .. '/timetable.json'
  timetableRequestActive = true
  timetableNextPoll = now + TIMETABLE_POLL_INTERVAL_S
  local requestedSessionIndex = sim.currentSessionIndex
  web.get(url, function(err, response)
    timetableRequestActive = false
    if err ~= nil or response == nil or response.status ~= 200
        or sim.currentSessionIndex ~= requestedSessionIndex
        or sim.isReplayActive or sim.isReplayOnlyMode then return end
    local ok, data = pcall(JSON.parse, response.body)
    if not ok or type(data) ~= 'table' or type(data.EntryList) ~= 'table' then return end

    local bySessionID, connected = {}, 0
    for i = 0, math.min(sim.carsCount, 128) - 1 do
      local car = ac.getCar(i)
      if car ~= nil and car.isConnected then
        bySessionID[tonumber(car.sessionID)] = i
        connected = connected + 1
      end
    end

    local nextStandings, ordered = {}, {}
    for _, entry in pairs(data.EntryList) do
      local carID = bySessionID[tonumber(entry.CarID)]
      local laps, trackPosition = tonumber(entry.Laps), tonumber(entry.Position)
      if carID ~= nil and (tonumber(entry.Ping) or 0) > 0 and laps ~= nil then
        local standing = {
          carID = carID,
          lapCount = math.max(0, math.floor(laps)),
          trackPosition = trackPosition and math.floor(trackPosition) or nil,
        }
        nextStandings[carID] = standing
        if standing.trackPosition ~= nil and standing.trackPosition > 0 then
          ordered[#ordered + 1] = standing
        end
      end
    end

    if #ordered == connected then
      table.sort(ordered, function(a, b)
        if a.lapCount ~= b.lapCount then return a.lapCount > b.lapCount end
        if a.trackPosition ~= b.trackPosition then
          return a.trackPosition < b.trackPosition
        end
        return a.carID < b.carID
      end)
      for position, standing in ipairs(ordered) do standing.position = position end
    end
    timetableStandings = nextStandings
  end)
end

local function correctedStandings(carID, car)
  local corrected = timetableStandings[carID]
  if corrected == nil or sim.isReplayActive or sim.isReplayOnlyMode then
    return car.racePosition, car.lapCount
  end
  return corrected.position or car.racePosition, corrected.lapCount
end

local function onIPCMessage(raw)
  local ok, message = pcall(JSON.parse, raw)
  if not ok or type(message) ~= 'table' then
    ac.log('Broadcaster IPC ignored malformed JSON')
    return
  end
  if message.version ~= PROTOCOL_VERSION then
    ac.log('Broadcaster IPC ignored unsupported protocol version')
    return
  end

  local function acceptConnection()
    if message.connection_id ~= lastConnectionID then
      lastConnectionID = message.connection_id
      lastCommandSeq = 0
      lastReplaySeq = 0
      lastPiPSeq = 0
      pendingCommand = nil
      pendingReplay = nil
      pendingPiP = nil
    end
  end

  if message.type == 'command' then
    if type(message.connection_id) ~= 'string' or message.connection_id == ''
        or not validNumber(message.command_seq)
        or not validNumber(message.target_driver)
        or not validNumber(message.target_camera)
        or not validNumber(message.target_car_camera) then
      return
    end
    acceptConnection()
    pendingCommand = message
  elseif message.type == 'replay' then
    if type(message.connection_id) ~= 'string' or message.connection_id == ''
        or not validNumber(message.replay_seq)
        or not validNumber(message.replay_action)
        or not validNumber(message.replay_rewind_s)
        or not validNumber(message.replay_frame)
        or not validNumber(message.target_driver)
        or not validNumber(message.target_camera)
        or not validNumber(message.target_car_camera) then
      return
    end
    acceptConnection()
    pendingReplay = message
  elseif message.type == 'pip' then
    if type(message.connection_id) ~= 'string' or message.connection_id == ''
        or not validNumber(message.pip_seq)
        or type(message.pip_enabled) ~= 'boolean'
        or not validNumber(message.target_driver)
        or not validNumber(message.target_car_camera) then
      return
    end
    acceptConnection()
    pendingPiP = message
  else
    ac.log('Broadcaster IPC ignored unknown message type')
  end
end

local ipcSocket = web.socket(
  'ws://127.0.0.1:5000/ac-ipc',
  onIPCMessage,
  {
    encoding = 'utf8',
    reconnect = true,
    onError = function(err)
      ac.log('Broadcaster IPC WebSocket error: ' .. tostring(err))
    end,
    onClose = function(reason)
      ac.log('Broadcaster IPC WebSocket closed: ' .. tostring(reason or ''))
    end,
  })

-- Replay state. Both replay transitions reset the shot: entering resets the
-- camera, and leaving snaps focus back to the player car. AC does it a frame
-- or two *after* the toggle, so a single focusCar + setCurrentCamera gets
-- clobbered. The wanted shot is held here and re-asserted for as long as AC
-- keeps fighting it, then released.
local replayLastResult = REPLAY_RESULT_UNTRIED
-- How long the last toggle result stays visible before reverting to UNTRIED.
-- Decoupled from the settle window: a refused toggle is otherwise invisible
-- except through replay_last_result, but it must not stick forever.
local REPLAY_RESULT_VISIBLE_S = 3.0
local replayResultUntil = 0
local shotHold = nil            -- { driver, camera, subCam, wantReplay, deadline }
-- Long enough to cover AC's fade in and out of replay, where the reset lands.
local SHOT_HOLD_S = 2.5

-- Replay toggles are serialised. Overlapping them crashed AC inside its own
-- OverlayLeaderboard::renderHUD (see logs/dmp-687-*): a jump issued while
-- replay was already active stacked two status transitions and the native
-- leaderboard overlay rendered across them. Never toggle while a previous
-- transition is settling, and turn "jump while already in replay" into
-- exit -> settle -> enter instead of a second enter.
local REPLAY_SETTLE_S = 0.75
local replayBusyUntil = 0
-- AC's OverlayLeaderboard::renderHUD crashes (acs.exe access violation) during
-- the replay->live transition. The leaderboard HUD element is hidden while
-- replay is active (and for the settle window after) so that render is skipped
-- while AC resets the grid.
local leaderboardSuppressed = false

-- Windows redirected to OBS are temporarily returned to the regular HUD layer
-- during replay. Keep their original redirect state so it can be restored.
local LIVE_REPLAY_WINDOW_TITLE = 'Live / Replay'
local obsHudRedirects = {}
local obsHudSettings = ac.INIConfig.scriptSettings()
local hideOtherObsHudAppsInReplay = obsHudSettings:get(
  'OBS_HUD', 'HIDE_OTHERS_IN_REPLAY', true)

-- ac.tryToToggleReplay's rewindS overshoots on this rig (it converts seconds at
-- a fixed 60 fps while the recording is ~16.7 fps). So entry uses only a nominal
-- rewind, and the real position is applied by a precise seek to
-- `sim.replayFrames - rewind_s/replayFrameMs` once replay is active.
local REPLAY_ENTER_NOMINAL_REWIND_S = 0.5
local pendingSeek = nil        -- absolute frame to seek to once replay is active

-- Stinger timing configuration. The 16:9 green-screen clip carries its own
-- wipe, so `videoToggleAtS` is the clip time where it fully covers the screen
-- and the replay toggle may fire. The slide timings remain for the PNG strip
-- used as a fallback until Media Foundation has decoded the clip.
local STINGER_TIMING = {
  slideInS = 0.3,
  coveredWaitTimeoutS = 1.5,
  slideOutS = 0.4,
  videoToggleAtS = 0.5,
  videoCoverTimeoutS = 3.0,
  videoRevealTimeoutS = 4.0,
}
local stinger = nil            -- { phase, phaseStarted, action, ..., targetReplay, deadline }

-- Video stinger. `ui.MediaPlayer` decodes through Media Foundation, so the clip
-- must use a Windows codec (H.264 MP4). MMF initialises lazily, so the player
-- is created once at load; until `hasVideo()` reports a decoded stream the PNG
-- strip keeps working exactly as before.
local STINGER_VIDEO_FILE = 'stinger.mp4'
local STINGER_VIDEO_KEY_THRESHOLD = 0.35
local STINGER_VIDEO_KEY_SOFTNESS = 0.25
local stingerPlayer = nil
if ui.MediaPlayer ~= nil then
  local ok, player = pcall(ui.MediaPlayer, STINGER_VIDEO_FILE)
  if ok and player ~= nil then
    stingerPlayer = player
    pcall(function()
      player:setMuted(true)
      player:setLooping(false)
      player:setAutoPlay(false)
      player:setUpdatePeriod(0)
      player:linkToSimulationSpeed(false)
    end)
  end
end

local function stingerVideoReady()
  if stingerPlayer == nil then return false end
  local ok, ready = pcall(stingerPlayer.hasVideo, stingerPlayer)
  return ok and ready == true
end

local function stingerVideoTime()
  if stingerPlayer == nil then return 0 end
  local ok, value = pcall(stingerPlayer.currentTime, stingerPlayer)
  if ok and validNumber(value) then return value end
  return 0
end

local function stingerVideoEnded()
  if stingerPlayer == nil then return true end
  local ok, ended = pcall(stingerPlayer.ended, stingerPlayer)
  if ok and ended == true then return true end
  local okTime, value = pcall(stingerPlayer.currentTime, stingerPlayer)
  local okDuration, duration = pcall(stingerPlayer.duration, stingerPlayer)
  if not (okTime and okDuration and validNumber(value) and validNumber(duration)) then
    return true
  end
  return duration > 0 and value >= duration - 0.01
end

local function restartStingerVideo()
  if stingerPlayer == nil then return end
  pcall(function()
    stingerPlayer:pause()
    stingerPlayer:setCurrentTime(0)
    stingerPlayer:play()
  end)
end

local function holdStingerVideo()
  if stingerPlayer == nil then return end
  pcall(stingerPlayer.pause, stingerPlayer)
end

local function resumeStingerVideo()
  if stingerPlayer == nil then return end
  pcall(stingerPlayer.play, stingerPlayer)
end

local function resetStingerVideo()
  if stingerPlayer == nil then return end
  pcall(function()
    stingerPlayer:pause()
    stingerPlayer:setCurrentTime(0)
  end)
end

-- Session identity. AC saves a separate replay per session and wipes the
-- instant-replay buffer when one starts, so markers from the previous session
-- point into a recording that no longer exists. `sessionGen` is what the web
-- server watches to rotate its journal; the index/type drift check covers the
-- session already running when this script loads, which onSessionStart (by
-- documented design) never fires for.
local sessionGen = 0
local lastSessionIndex = -1
local lastSessionTypeRaw = -1

ac.onSessionStart(function(sessionIndex, restarted)
  sessionGen = sessionGen + 1
  lastSessionIndex = sessionIndex
  lastSessionTypeRaw = sim.raceSessionType
  ac.log(string.format('Session start: index=%d restarted=%s gen=%d',
    sessionIndex, tostring(restarted), sessionGen))
end)

-- Hit detection.
--
-- `collisionDepth` and `ac.onCarCollision` are fed by AC's regular physics
-- component. Remote cars online, and every car in a replay, run without it,
-- so both signals stay empty exactly where the broadcast needs them. Per-pair
-- ODE queries (`ac.areCarsColliding` / `physics.Collider.TrackWalls`) would
-- cover remote cars but do not scale to a full 50+ car grid.
--
-- What every car does have is `car.velocity`. A contact is a large impulse
-- that the car's own recent acceleration - cornering, braking, network
-- extrapolation - does not explain. Track that residual per car, every frame.
--
-- One event per incident: the larger residual owns the hit. A remote partner's
-- replicated impulse usually lands a packet or two later, so a short pair
-- cooldown swallows it instead of logging a second event.
local HIT_NOTICE_S = 6.0          -- badge hold, mirrors INCIDENT_NOTICE_MS
local HIT_LATCH_S = 0.35          -- keep is_colliding up for the 10 Hz telemetry
local HIT_RESIDUAL_MS2 = 120      -- unexplained impulse, ~12 g at 60 fps
local HIT_ACCEL_TAU_S = 0.25      -- how fast the learned acceleration tracks
local HIT_VERTICAL_RATIO = 0.8    -- |ry| > ratio * horizontal = landing or kerb
local HIT_PAIR_RADIUS_M = 6.0     -- neighbour that counts as the other car
local HIT_PAIR_HEIGHT_M = 3.0     -- overpass guard
local HIT_PAIR_COOLDOWN_S = 1.0
local HIT_CAR_COOLDOWN_S = 1.0
local HIT_TELEPORT_SPEED_MPS = 20
local HIT_TELEPORT_MARGIN_M = 0.5

local hitState = {}          -- car_id -> velocity/position/damage baselines
local hitLatchUntil = {}     -- car_id -> is_colliding holds until
local hitPending = {}        -- car_id -> send this tick even if the latch just ended
local hitNoticeUntil = {}    -- car_id -> badge holds until
local hitCooldownUntil = {}  -- car_id -> cannot own another event until
local hitPairCooldown = {}   -- lo * 128 + hi -> the pair already logged an event
local hitDebugResidual = {}  -- car_id -> last residual, in m/s^2
local hitDebugPeak = {}      -- car_id -> biggest residual this session
local hitDebugDepth = {}     -- car_id -> collisionDepth when AC reports one
local hitDebugPartner = {}   -- car_id -> neighbour at the last candidate
local hitCandidates = {}
local hitSessionIndex = -1
local hitSessionTypeRaw = -1

local function resetHitState()
  table.clear(hitState)
  table.clear(hitLatchUntil)
  table.clear(hitPending)
  table.clear(hitNoticeUntil)
  table.clear(hitCooldownUntil)
  table.clear(hitPairCooldown)
  table.clear(hitDebugResidual)
  table.clear(hitDebugPeak)
  table.clear(hitDebugDepth)
  table.clear(hitDebugPartner)
end

local function hitCarActive(car)
  return car ~= nil and car.isConnected
    and not car.isInPit and not car.isInPitlane
end

local function updateHitDetection(dt)
  if sim.isReplayActive or sim.isReplayOnlyMode
      or sim.timeToSessionStart >= -5000 or dt <= 0 then
    resetHitState()
    return
  end

  -- A new session wipes the replay too, so every baseline is meaningless.
  if sim.currentSessionIndex ~= hitSessionIndex
      or sim.raceSessionType ~= hitSessionTypeRaw then
    resetHitState()
    hitSessionIndex = sim.currentSessionIndex
    hitSessionTypeRaw = sim.raceSessionType
  end

  local now = os.preciseClock()
  local carCount = math.min(sim.carsCount, 128)
  local alpha = 1 - math.exp(-dt / HIT_ACCEL_TAU_S)
  table.clear(hitCandidates)

  for i = 0, carCount - 1 do
    local car = ac.getCar(i)
    if not hitCarActive(car) then
      hitState[i] = nil
      hitLatchUntil[i] = nil
      hitDebugResidual[i] = 0
      hitDebugDepth[i] = nil
      hitDebugPartner[i] = nil
    else
      local vx, vy, vz = car.velocity.x, car.velocity.y, car.velocity.z
      local px, py, pz = car.position.x, car.position.y, car.position.z
      local dmg = (car.damage[0] or 0) + (car.damage[1] or 0)
        + (car.damage[2] or 0) + (car.damage[3] or 0) + (car.damage[4] or 0)
      local st = hitState[i]
      local residual = 0
      local residualHit = false
      if st == nil then
        -- First frame for this car in this session: only take baselines.
        st = { vx = vx, vy = vy, vz = vz,
               px = px, py = py, pz = pz,
               ax = 0, ay = 0, az = 0, dmg = dmg,
               reset = car.resetCounter, depthActive = false }
        hitState[i] = st
      else
        local moved = math.sqrt((px - st.px) * (px - st.px)
          + (py - st.py) * (py - st.py) + (pz - st.pz) * (pz - st.pz))
        local allowed = (math.abs(car.speedKmh) / 3.6 + HIT_TELEPORT_SPEED_MPS) * dt
          + HIT_TELEPORT_MARGIN_M
        if car.resetCounter ~= st.reset or moved > allowed then
          -- Reset, respawn or a network snap. Not a contact: restart clean.
          st.vx, st.vy, st.vz = vx, vy, vz
          st.ax, st.ay, st.az = 0, 0, 0
          hitDebugResidual[i] = 0
        else
          local axInst = (vx - st.vx) / dt
          local ayInst = (vy - st.vy) / dt
          local azInst = (vz - st.vz) / dt
          st.ax = st.ax + (axInst - st.ax) * alpha
          st.ay = st.ay + (ayInst - st.ay) * alpha
          st.az = st.az + (azInst - st.az) * alpha
          local rx = axInst - st.ax
          local ry = ayInst - st.ay
          local rz = azInst - st.az
          residual = math.sqrt(rx * rx + ry * ry + rz * rz)
          hitDebugResidual[i] = residual
          if residual > (hitDebugPeak[i] or 0) then hitDebugPeak[i] = residual end
          if residual > HIT_RESIDUAL_MS2 then
            -- A landing or a kerb is mostly vertical; a wall or car hit is not.
            local horizontal = math.sqrt(rx * rx + rz * rz)
            residualHit = math.abs(ry) <= HIT_VERTICAL_RATIO * horizontal
          end
          st.vx, st.vy, st.vz = vx, vy, vz
        end
        st.px, st.py, st.pz = px, py, pz
        st.reset = car.resetCounter
      end

      -- These two need no velocity baseline and still cover the local car.
      -- Depth is edge-triggered: a long wall scrape must not log every second.
      local depth = car.collisionDepth or 0
      local depthEdge = depth > 0 and not st.depthActive
      st.depthActive = depth > 0
      hitDebugDepth[i] = depth > 0 and depth or nil
      local dmgJumped = st.dmg ~= nil and dmg > st.dmg + 0.01
      st.dmg = dmg

      local score = 0
      local fires = false
      if residualHit then
        fires = true
        score = residual
      end
      if dmgJumped then
        fires = true
        score = score + 100
      end
      if depthEdge then
        fires = true
        score = score + 200
      end
      if fires then
        hitCandidates[#hitCandidates + 1] = {
          id = i, score = score, residual = residual,
        }
      end
    end
  end

  -- Attribution: the highest score owns the incident.
  if #hitCandidates > 1 then
    table.sort(hitCandidates, function(a, b) return a.score > b.score end)
  end

  for ci = 1, #hitCandidates do
    local candidate = hitCandidates[ci]
    local i = candidate.id
    local car = ac.getCar(i)
    if hitCarActive(car) and now >= (hitCooldownUntil[i] or 0) then
      local cx, cy, cz = car.position.x, car.position.y, car.position.z
      local partner, bestD2 = nil, HIT_PAIR_RADIUS_M * HIT_PAIR_RADIUS_M
      for j = 0, carCount - 1 do
        if j ~= i then
          local other = ac.getCar(j)
          if hitCarActive(other) then
            local dx = other.position.x - cx
            local dy = other.position.y - cy
            local dz = other.position.z - cz
            local d2 = dx * dx + dy * dy + dz * dz
            if math.abs(dy) <= HIT_PAIR_HEIGHT_M and d2 < bestD2 then
              bestD2 = d2
              partner = j
            end
          end
        end
      end
      hitDebugPartner[i] = partner

      local blocked = false
      if partner ~= nil then
        local lo, hi = i, partner
        if lo > hi then lo, hi = hi, lo end
        local key = lo * 128 + hi
        local untilS = hitPairCooldown[key]
        if untilS ~= nil and now < untilS then
          blocked = true
        else
          hitPairCooldown[key] = now + HIT_PAIR_COOLDOWN_S
        end
      end

      if not blocked then
        hitLatchUntil[i] = now + HIT_LATCH_S
        hitPending[i] = true
        hitNoticeUntil[i] = now + HIT_NOTICE_S
        hitCooldownUntil[i] = now + HIT_CAR_COOLDOWN_S
        ac.log(string.format('Hit car=%d residual=%.0f partner=%s',
          i, candidate.residual, partner and tostring(partner) or 'wall'))
      end
    end
  end
end

-- DEBUG: the detector state per car, shown in the driver list. `d` is
-- collisionDepth (local car only), `@n` the nearest car at a candidate, and a
-- bare number is the live residual; the parenthesised number is the session
-- peak. The peak is what makes threshold tuning readable at 10 Hz.
local HIT_DEBUG = true
local HIT_DEBUG_COL_W = 52

-- Rollover detection: car.up.y < 0.5 means chassis is tilted > 60° from
-- vertical (cos(60°) = 0.5). Asymmetric thresholds + a hold counter
-- prevent flicker during hard landings or curb hops.
local ROLLOVER_UP_Y_ENTER = 0.5   -- cos(60°)
local ROLLOVER_UP_Y_EXIT  = 0.6   -- cos(~53°)
local ROLLOVER_HOLD_TICKS = 2     -- ~200ms at 10 Hz telemetry
local rolloverState = {}
local rolloverTickCount = {}


-- Cached driver data for the UI (rebuilt each telemetry tick)
local driverRows = {}

---------------------------------------------------------------------
-- Camera mapping
---------------------------------------------------------------------
-- Custom numbering (used by web remote) <-> CSP ac.CameraMode
local CUSTOM_TO_CSP = {
  [1] = ac.CameraMode.Track,
  [2] = ac.CameraMode.Cockpit,
  [3] = ac.CameraMode.Helicopter,
  [4] = ac.CameraMode.Car,
  [5] = ac.CameraMode.OnBoardFree,
  [6] = ac.CameraMode.Drivable,
}

local CSP_TO_CUSTOM = {
  [ac.CameraMode.Track]       = 1,
  [ac.CameraMode.Cockpit]     = 2,
  [ac.CameraMode.Helicopter]  = 3,
  [ac.CameraMode.Car]         = 4,
  [ac.CameraMode.OnBoardFree] = 5,
  [ac.CameraMode.Drivable]    = 2,
  [ac.CameraMode.Free]        = 5,
}

local CAMERA_NAMES = {
  [0] = 'Cockpit', 'Car (F6)', 'Chase', 'Track (TV)', 'Helicopter',
  'OnBoard Free', 'Free', 'Deprecated', 'Image Gen', 'Start',
}

local function cspCameraToCustom(mode)
  if mode == ac.CameraMode.Drivable
      and sim.driveableCameraMode == ac.DrivableCamera.Bumper then
    return 6
  end
  return CSP_TO_CUSTOM[mode] or 0
end

local function getCameraName(mode)
  return CAMERA_NAMES[mode] or 'Unknown'
end

---------------------------------------------------------------------
-- Camera switching (mirrors Python changeCamera logic)
---------------------------------------------------------------------
local function changeCamera(cam, carIndex, subCam)
  if cam == 1 then
    ac.setCurrentCamera(ac.CameraMode.Track)
  elseif cam == 2 then
    ac.setCurrentCamera(ac.CameraMode.Cockpit)
  elseif cam == 3 then
    ac.setCurrentCamera(ac.CameraMode.Helicopter)
  elseif cam == 4 then
    local car = ac.getCar(carIndex)
    local maxF6 = car.carCamerasCount - 1
    if maxF6 >= 0 then
      ac.setCurrentCamera(ac.CameraMode.Car)
      if subCam >= 0 then
        ac.setCurrentCarCamera(math.min(subCam, maxF6))
      else
        local cur = sim.carCameraIndex
        ac.setCurrentCarCamera(cur >= maxF6 and 0 or cur + 1)
      end
    end
  elseif cam == 5 then
    ac.setCurrentCamera(ac.CameraMode.OnBoardFree)
  elseif cam == 6 then
    ac.setCurrentCamera(ac.CameraMode.Drivable)
    ac.setCurrentDrivableCamera(ac.DrivableCamera.Bumper)
  end
end

---------------------------------------------------------------------
-- Command processing
---------------------------------------------------------------------
local function applyFocus(driver, camera, subCam)
  -- Only refocus when the driver actually changes. Calling ac.focusCar on
  -- the already-focused car queues an internal camera reset that clobbers
  -- the setCurrentCamera below on the following frame.
  if driver >= 0 and driver ~= sim.focusedCar then
    ac.focusCar(driver)
  end
  changeCamera(camera, driver, subCam)
end

local function holdShot(driver, camera, subCam, wantReplay)
  shotHold = {
    driver = driver,
    camera = camera,
    subCam = subCam,
    wantReplay = wantReplay,
    deadline = os.preciseClock() + SHOT_HOLD_S,
  }
end

-- Snapshot of what is on screen right now, to be re-asserted on the other side
-- of a replay transition.
local function holdCurrentShot(wantReplay)
  holdShot(sim.focusedCar, cspCameraToCustom(sim.cameraMode), sim.carCameraIndex, wantReplay)
end

-- Convert "seconds to rewind from the live edge" into an absolute replay frame.
-- sim.replayFrames keeps counting to the live edge during playback, so this is
-- the "now" the rewind is relative to.
local function seekTargetFromRewind(rewindS)
  local frameMs = sim.replayFrameMs
  if frameMs and frameMs > 0 then
    local target = sim.replayFrames - math.floor(rewindS * 1000.0 / frameMs)
    if target < 0 then target = 0 end
    return target
  end
  return 0
end

local function enterReplay(rewindS, driver, camera, subCam)
  -- Enter with only a nominal rewind: tryToToggleReplay's own rewindS overshoots,
  -- and a large rewind can overshoot past the buffer and be refused. The precise
  -- position is applied by processPendingSeek once replay is active.
  local ok = ac.tryToToggleReplay(true, REPLAY_ENTER_NOMINAL_REWIND_S)
  replayLastResult = ok and REPLAY_RESULT_OK or REPLAY_RESULT_REFUSED
  replayResultUntil = os.preciseClock() + REPLAY_RESULT_VISIBLE_S
  -- Serialise the next toggle even when this one was refused: a refusal can
  -- mean AC is mid-transition, and poking it again inside the settle window is
  -- exactly what crashes the leaderboard overlay.
  replayBusyUntil = os.preciseClock() + REPLAY_SETTLE_S
  ac.log(string.format('Replay enter: rewind=%.1fs accepted=%s', rewindS, tostring(ok)))
  if ok then
    pendingSeek = seekTargetFromRewind(rewindS)
    holdShot(driver, camera, subCam, true)
  else
    pendingSeek = nil
    shotHold = nil
  end
  return ok
end

-- Leaving replay snaps focus back to the player car, which is never what a
-- director wants, so capture the on-screen shot first and re-assert it live.
local function exitReplay(reason)
  holdCurrentShot(false)
  local ok = ac.tryToToggleReplay(false)
  replayLastResult = ok and REPLAY_RESULT_OK or REPLAY_RESULT_REFUSED
  replayResultUntil = os.preciseClock() + REPLAY_RESULT_VISIBLE_S
  -- Always arm the settle window: a second go_live (or any toggle) landing
  -- while AC is still exiting is what crashes the leaderboard overlay, and a
  -- refusal can mask an in-flight transition.
  replayBusyUntil = os.preciseClock() + REPLAY_SETTLE_S
  ac.log(string.format('Replay live (%s): accepted=%s', reason, tostring(ok)))
  if not ok then shotHold = nil end
  return ok
end

local function fireStingerToggle(state)
  if state.targetReplay then
    return enterReplay(state.rewindS, state.driver, state.camera, state.subCam)
  end
  return exitReplay('command')
end

local function queueStinger(action, rewindS, driver, camera, subCam)
  local targetReplay = action == REPLAY_ENTER
  local video = stingerVideoReady()
  stinger = {
    phase = 'cover',
    phaseStarted = os.preciseClock(),
    action = action,
    rewindS = rewindS,
    driver = driver,
    camera = camera,
    subCam = subCam,
    targetReplay = targetReplay,
    video = video,
    toggled = false,
    toggleOk = false,
  }
  if video then restartStingerVideo() end
  -- Instant-replay enter: toggle before the first stinger frame so AC's
  -- replay-in animation plays under the wipe. Go-live waits for coverage.
  if targetReplay then
    stinger.toggleOk = fireStingerToggle(stinger) == true
    stinger.toggled = true
  end
end

local function processReplayCommands()
  local command = pendingReplay
  if command == nil then return end
  pendingReplay = nil

  local seq = command.replay_seq
  if seq == lastReplaySeq then return end
  lastReplaySeq = seq

  local action = command.replay_action
  local active = sim.isReplayActive
  local now = os.preciseClock()

  -- A toggle landing inside another toggle's transition is what crashed AC.
  if stinger ~= nil or now < replayBusyUntil then
    ac.log(string.format('Replay command %d ignored: transition in progress (active=%s)',
      action, tostring(active)))
    return
  end

  if action == REPLAY_ENTER then
    local rewindS = command.replay_rewind_s
    local driver = command.target_driver
    local camera = command.target_camera
    local subCam = command.target_car_camera
    if active then
      -- Already in replay: seek straight to the target frame instead of
      -- exit -> settle -> enter. That exit toggle is what crashes AC
      -- (OverlayLeaderboard::renderHUD, dmp-6816-*), and a seek does the same
      -- job without a replay transition.
      local target = seekTargetFromRewind(rewindS)
      ac.setReplayPosition(target, 0)
      holdShot(driver, camera, subCam, true)
      ac.log(string.format('Replay seek (already active): rewind=%.1fs frame=%d',
        rewindS, target))
    else
      queueStinger(action, rewindS, driver, camera, subCam)
    end
  elseif action == REPLAY_LIVE then
    if active then
      -- Keep the car that was on screen in the replay: leaving replay makes AC
      -- snap focus back to the player car, which is never what a director wants.
      queueStinger(action, 0, -1, -1, -1)
    else
      ac.log('Replay live ignored: not in replay')
    end
  elseif action == REPLAY_SEEK_FRAME then
    if active or sim.isReplayOnlyMode then
      ac.setReplayPosition(command.replay_frame, 0)
      if command.target_driver >= 0 or command.target_camera > 0 then
        -- Seeking a saved replay can reset the shot just like an instant-replay
        -- jump. Replay-only mode reports isReplayActive=false, so hold against
        -- the actual flag value instead of assuming the instant-replay side.
        holdShot(command.target_driver, command.target_camera,
          command.target_car_camera, active)
      end
    else
      ac.log('Replay seek ignored: not in replay')
    end
  end
end

local function processStinger()
  if stinger == nil then return end
  -- The player can drop the stream (unsupported codec, decode failure) after
  -- the stinger was queued; degrade to the PNG path rather than stall.
  if stinger.video and not stingerVideoReady() then stinger.video = false end

  local now = os.preciseClock()
  if stinger.phase == 'cover' then
    local covered
    if stinger.video then
      -- A clip shorter than the cue, or a stalled decoder, must not wedge the
      -- transition: replay commands are ignored while a stinger is active.
      covered = stingerVideoEnded()
        or stingerVideoTime() >= STINGER_TIMING.videoToggleAtS
        or now - stinger.phaseStarted >= STINGER_TIMING.videoCoverTimeoutS
    else
      covered = now - stinger.phaseStarted >= STINGER_TIMING.slideInS
    end
    if not covered then return end
    if not stinger.toggled then
      stinger.toggleOk = fireStingerToggle(stinger) == true
      stinger.toggled = true
    end
    if stinger.toggleOk then
      stinger.phase = 'wait'
      stinger.deadline = now + STINGER_TIMING.coveredWaitTimeoutS
      -- Hold the cover frame; the reveal must not finish while AC settles.
      if stinger.video then holdStingerVideo() end
    else
      stinger.phase = 'reveal'
      stinger.phaseStarted = now
    end
  elseif stinger.phase == 'wait' then
    if sim.isReplayActive == stinger.targetReplay or now >= stinger.deadline then
      stinger.phase = 'reveal'
      stinger.phaseStarted = now
      if stinger.video then resumeStingerVideo() end
    end
  elseif stinger.phase == 'reveal' then
    local finished
    if stinger.video then
      finished = stingerVideoEnded()
        or now - stinger.phaseStarted >= STINGER_TIMING.videoRevealTimeoutS
    else
      finished = now - stinger.phaseStarted >= STINGER_TIMING.slideOutS
    end
    if finished then
      if stinger.video then resetStingerVideo() end
      stinger = nil
    end
  end
end

-- Apply the precise seek parked by enterReplay once replay is actually active.
-- Fires the frame replay comes up, before the shot hold re-asserts the camera.
local function processPendingSeek()
  if pendingSeek == nil then return end
  if not sim.isReplayActive then return end
  ac.setReplayPosition(pendingSeek, 0)
  pendingSeek = nil
end

-- Re-assert the wanted shot until AC stops resetting it (or the operator picks
-- something else, which clears the hold). Only touches what actually drifted,
-- so an F6 sub-camera is never cycled by a redundant re-apply.
local function processShotHold()
  if not shotHold then return end
  if os.preciseClock() > shotHold.deadline then
    shotHold = nil
    return
  end
  -- Wait for the side of the transition this shot was meant for.
  if sim.isReplayActive ~= shotHold.wantReplay then return end

  local needFocus = shotHold.driver >= 0 and sim.focusedCar ~= shotHold.driver
  local needCamera = shotHold.camera > 0 and cspCameraToCustom(sim.cameraMode) ~= shotHold.camera
  local needSubCam = shotHold.camera == 4 and shotHold.subCam >= 0
    and sim.carCameraIndex ~= shotHold.subCam

  if needFocus or needCamera or needSubCam then
    applyFocus(shotHold.driver, shotHold.camera, shotHold.subCam)
  end
end

-- Clear the last toggle result once it has been visible long enough. A refusal
-- must not stick as "RPL:N/A" forever, but must outlast the immediate ack.
local function processReplayResultExpiry()
  if replayLastResult ~= REPLAY_RESULT_UNTRIED
      and os.preciseClock() >= replayResultUntil then
    replayLastResult = REPLAY_RESULT_UNTRIED
  end
end

-- Keep AC's leaderboard HUD hidden across replay (and the settle window after),
-- otherwise its renderHUD dereferences a car during the replay->live transition
-- and takes the whole game down.
local function syncLeaderboard()
  local hide = sim.isReplayActive or sim.isReplayOnlyMode
      or os.preciseClock() < replayBusyUntil
  if hide ~= leaderboardSuppressed then
    ac.disableExtraHUDElements('leaderboard', hide)
    leaderboardSuppressed = hide
  end
end

local function suppressObsHudWindows()
  for _, info in ipairs(ac.getAppWindows()) do
    if info.layer ~= 0 and info.title ~= LIVE_REPLAY_WINDOW_TITLE then
      local saved = obsHudRedirects[info.name]
      local window = saved ~= nil and saved.window
          or ac.accessAppWindow(info.name)
      if window ~= nil and not window:valid() then
        window = ac.accessAppWindow(info.name)
      end
      if window ~= nil and window:valid() then
        if saved == nil then
          saved = {
            window = window,
            layer = info.layer,
            duplicate = info.layerDuplicate == true,
          }
          obsHudRedirects[info.name] = saved
        else
          saved.window = window
        end
        window:setRedirectLayer(0)
      end
    end
  end
end

local function restoreObsHudWindows()
  for _, saved in pairs(obsHudRedirects) do
    if saved.window:valid() then
      saved.window:setRedirectLayer(saved.layer, saved.duplicate)
    end
  end
  obsHudRedirects = {}
end

local function syncObsHudForwarding()
  if hideOtherObsHudAppsInReplay
      and (sim.isReplayActive or sim.isReplayOnlyMode) then
    suppressObsHudWindows()
  else
    restoreObsHudWindows()
  end
end

ac.onRelease(restoreObsHudWindows)

ac.onReplay(function(event)
  resetHitState()
  ac.log('Replay event: ' .. tostring(event))
  -- Every AC-side transition re-arms the settle window, so a command arriving
  -- mid-transition is ignored rather than stacked on top of it.
  replayBusyUntil = math.max(replayBusyUntil, os.preciseClock() + REPLAY_SETTLE_S)

  -- A replay start/stop/jump teleports cars: restart rollover hold counters.
  rolloverState = {}
  rolloverTickCount = {}

  -- AC ends instant replay on its own when playback reaches the live edge, and
  -- that exit snaps focus to the player car exactly like the commanded one. No
  -- hold is pending in that case, so take one now, while the replay shot is
  -- still on screen.
  if event == 'stop' and (shotHold == nil or shotHold.wantReplay) then
    holdCurrentShot(false)
  end
end)


local function processCommands()
  local command = pendingCommand
  if command == nil then return end
  pendingCommand = nil

  local seq = command.command_seq
  if seq == lastCommandSeq then return end
  lastCommandSeq = seq
  -- An explicit pick from the panel wins over any shot being held, otherwise
  -- the hold would drag the operator back to the replay's car.
  shotHold = nil
  applyFocus(command.target_driver, command.target_camera, command.target_car_camera)
end

local function processPiPCommands()
  local command = pendingPiP
  if command == nil then return end
  pendingPiP = nil

  local seq = command.pip_seq
  if seq == lastPiPSeq then return end
  lastPiPSeq = seq
  pipEnabled = command.pip_enabled
  pipDriver = command.target_driver
  pipCamera = command.target_car_camera
  pipFrameReady = false
end

local function syncPiPWindow()
  local shouldOpen = pipEnabled and not sim.isReplayActive
  if ac.isWindowOpen('pip') ~= shouldOpen then
    ac.setWindowOpen('pip', shouldOpen)
  end
end

local function validPiPVector(value, allowZero)
  if value == nil or not validNumber(value.x)
      or not validNumber(value.y) or not validNumber(value.z) then
    return false
  end
  return allowZero or value.x * value.x + value.y * value.y
      + value.z * value.z > 1e-8
end

local function readPiPCamera(config, cameraIndex)
  local section = string.format('CAMERA_%d', cameraIndex)
  local position = config:get(section, 'POSITION', vec3())
  local forward = config:get(section, 'FORWARD', vec3())
  local up = config:get(section, 'UP', vec3())
  local fov = config:get(section, 'FOV', 0)
  if not validPiPVector(position, true)
      or not validPiPVector(forward, false)
      or not validPiPVector(up, false)
      or not validNumber(fov) or fov <= 0 then
    return nil
  end
  return position, forward, up, fov
end

local function resolvePiPCamera(driver, requestedCamera)
  if driver < 0 or driver >= sim.carsCount then return nil end
  local car = ac.getCar(driver)
  if car == nil or car.isConnected ~= true or car.carCamerasCount <= 0
      or car.bodyTransform == nil then
    return nil
  end

  local config = ac.INIConfig.carData(driver, 'cameras.ini')
  if config == nil then return nil end
  local cameraIndex = math.floor(requestedCamera)
  if cameraIndex < 0 or cameraIndex >= car.carCamerasCount then
    cameraIndex = 0
  end
  local position, forward, up, fov = readPiPCamera(config, cameraIndex)
  if position == nil and cameraIndex ~= 0 then
    cameraIndex = 0
    position, forward, up, fov = readPiPCamera(config, cameraIndex)
  end
  if position == nil then return nil end

  local transform = car.bodyTransform
  return transform:transformPoint(position),
      math.normalize(transform:transformVector(forward)),
      math.normalize(transform:transformVector(up)), fov, cameraIndex
end

local function ensurePiPShot()
  if pipShot ~= nil then return pipShot end
  pipShot = ac.GeometryShot(
    ac.findNodes('sceneRoot:yes'), vec2(640, 360), 1, false,
    render.AntialiasingMode.FXAA,
    render.TextureFormat.R32G32B32A32.Float, 0)
  pipShot:setName('Broadcaster Remote PiP')
  pipShot:setSky(true)
  pipShot:setOriginalLighting(true)
  pipShot:setShadersType(render.ShadersType.Main)
  pipShot:setFakeCarShadows(true)
  pipShot:setMaxLayer(5)
  pipShot:setOpaqueAlphaFix(true)
  pipShot:setTransparentPass(true)
  return pipShot
end

render.onSceneReady(function()
  if not pipEnabled or not ac.isWindowOpen('pip') then
    pipFrameReady = false
    return
  end
  local position, forward, up, fov, actualCamera =
      resolvePiPCamera(pipDriver, pipCamera)
  if position == nil then
    pipFrameReady = false
    return
  end
  ensurePiPShot():update(position, forward, up, fov)
  pipActualCamera = actualCamera
  pipFrameReady = true
end)

ac.onRelease(function()
  if pipShot ~= nil then
    pipShot:dispose()
    pipShot = nil
  end
end)

---------------------------------------------------------------------
-- Gap / interval computation (ported from remote_web.py)
---------------------------------------------------------------------
local function formatGap(ahead, behind, trackLength)
  local progressDiff = ahead.totalProgress - behind.totalProgress
  if progressDiff >= 1.0 then
    local laps = math.floor(progressDiff)
    return laps == 1 and '+1 Lap' or ('+' .. laps .. ' Laps')
  end
  local splineDiff = (ahead.spline - behind.spline) % 1.0
  if splineDiff < 0.001 then return '+0.0s' end
  local gapMeters = splineDiff * trackLength
  local speedMs = behind.speedKmh / 3.6
  if speedMs < 1.0 then
    return '+' .. math.floor(gapMeters) .. 'm'
  end
  return string.format('+%.1fs', gapMeters / speedMs)
end

local function buildDriverRows()
  local rows = {}
  local trackLength = sim.trackLengthM
  local carCount = sim.carsCount
  local now = os.preciseClock()

  for i = 0, carCount - 1 do
    local car = ac.getCar(i)
    if car.isConnected then
      local position, lapCount = correctedStandings(i, car)
      rows[#rows + 1] = {
        carId       = i,
        position    = position,
        spline      = car.splinePosition,
        speedKmh    = car.speedKmh,
        lapTimeMs   = car.lapTimeMs,
        bestLapMs   = car.bestLapTimeMs,
        lastLapMs   = car.previousLapTimeMs,
        lapCount    = lapCount,
        isInPit     = car.isInPitlane,
        isConnected = car.isConnected,
        name        = car:driverName(),
        totalProgress = lapCount + car.splinePosition,
        -- HIT and FLIP are per-car: the detector tracks every car because
        -- collisionDepth only exists for the local one.
        isHit = now < (hitNoticeUntil[i] or 0),
        isRolledOver = rolloverState[i] == true,
      }
    end
  end

  table.sort(rows, function(a, b) return a.position < b.position end)

  for idx, row in ipairs(rows) do
    if idx == 1 then
      row.gap = 'Leader'
      row.interval = '-'
    else
      row.gap = formatGap(rows[1], row, trackLength)
      row.interval = formatGap(rows[idx - 1], row, trackLength)
    end
  end

  return rows
end

---------------------------------------------------------------------
-- Telemetry update
---------------------------------------------------------------------
local function updateTelemetry()
  local carCount = math.min(sim.carsCount, 128)
  local focusedCar = ac.getCar(sim.focusedCar)

  local sessionIndex = sim.currentSessionIndex
  local sessionTypeRaw = sim.raceSessionType
  local session = ac.getSession(sessionIndex)
  local isTimedSession = (session ~= nil and session.durationMinutes > 0)
    or sim.isTimedRace == true
  if sessionIndex ~= lastSessionIndex or sessionTypeRaw ~= lastSessionTypeRaw then
    -- Covers the session running at script load and anything onSessionStart
    -- misses. A duplicate bump costs an empty rotation, which is free.
    if lastSessionIndex ~= -1 then
      sessionGen = sessionGen + 1
      ac.log(string.format('Session change detected: index=%d type=%d gen=%d',
        sessionIndex, sessionTypeRaw, sessionGen))
    end
    lastSessionIndex = sessionIndex
    lastSessionTypeRaw = sessionTypeRaw
    table.clear(timetableStandings)
    timetableNextPoll = 0
  end

  local now = os.preciseClock()
  local cars = {}

  for i = 0, carCount - 1 do
    local car = ac.getCar(i)
    local position, lapCount = correctedStandings(i, car)
    local isHit = hitPending[i] == true
      or (hitLatchUntil[i] ~= nil and hitLatchUntil[i] > now)

    local upY = car.up.y
    local threshold = rolloverState[i] and ROLLOVER_UP_Y_EXIT or ROLLOVER_UP_Y_ENTER
    if upY < threshold then
      rolloverTickCount[i] = (rolloverTickCount[i] or 0) + 1
    else
      rolloverTickCount[i] = 0
    end
    local rolledOver = (rolloverTickCount[i] or 0) >= ROLLOVER_HOLD_TICKS
    if rolledOver ~= (rolloverState[i] == true) then
      ac.log(string.format('Car #%d rollover %s (up.y=%.2f)', i, rolledOver and 'STARTED' or 'ENDED', upY))
      rolloverState[i] = rolledOver
    end
    cars[#cars + 1] = {
      car_id = i,
      session_id = car.sessionID,
      position = position,
      normalized_spline_pos = car.splinePosition,
      speed_kmh = car.speedKmh,
      lap_time = car.lapTimeMs,
      best_lap = car.bestLapTimeMs,
      last_lap = car.previousLapTimeMs,
      lap_count = lapCount,
      car_cameras_count = car.carCamerasCount,
      is_in_pit = car.isInPitlane == true,
      is_connected = car.isConnected == true,
      is_colliding = isHit,
      is_rolled_over = rolledOver,
      driver_name = car:driverName() or '',
      team_name = ac.getDriverTeam(i) or '',
    }
  end

  table.clear(hitPending)
  packetID = packetID + 1
  local payload = {
    version = PROTOCOL_VERSION,
    type = 'telemetry',
    packet_id = packetID,
    car_count = #cars,
    focused_car = sim.focusedCar,
    current_camera = cspCameraToCustom(sim.cameraMode),
    car_cameras_count = focusedCar and focusedCar.carCamerasCount or 0,
    current_car_camera = sim.carCameraIndex,
    track_length = sim.trackLengthM,
    track_name = ac.getTrackName() or '',
    session_type = (sim.raceSessionType == ac.SessionType.Race) and 1 or 0,
    session_index = sessionIndex,
    session_type_raw = sessionTypeRaw,
    session_gen = sessionGen,
    session_name = ac.getSessionName(sessionIndex) or '',
    is_timed_session = isTimedSession,
    session_time_left = isTimedSession and math.max(0, sim.sessionTimeLeft) or 0,
    is_replay = sim.isReplayActive == true,
    replay_frame = sim.replayCurrentFrame,
    replay_frames = sim.replayFrames,
    replay_frame_ms = sim.replayFrameMs,
    replay_last_result = replayLastResult,
    is_replay_only = sim.isReplayOnlyMode == true,
    replay_file = ac.getReplayFilename() or '',
    -- Where AC keeps the per-session .acreplay it is recording right now. The
    -- web server pairs its event journal to that file by name.
    replay_temp_dir = ac.getFolder(ac.FolderID.ReplaysTemp) or '',
    cars = cars,
  }
  local ok, encoded = pcall(JSON.stringify, payload)
  if ok then pcall(ipcSocket, encoded) end
end

---------------------------------------------------------------------
-- Main update loop
---------------------------------------------------------------------
function script.update(dt)
  -- Commands run every frame: they are a sequence compare when idle, and the
  -- deferred replay shot needs to land the frame replay comes up, not up to
  -- 100 ms later.
  syncObsHudForwarding()
  processReplayCommands()
  processStinger()
  processPendingSeek()
  processShotHold()
  processReplayResultExpiry()
  processCommands()
  processPiPCommands()
  syncPiPWindow()
  syncLeaderboard()
  updateHitDetection(dt)
  pollTimetable()

  updateAccum = updateAccum + dt
  if updateAccum < UPDATE_INTERVAL then return end
  updateAccum = 0

  updateTelemetry()
  driverRows = buildDriverRows()
end

---------------------------------------------------------------------
-- In-game UI
---------------------------------------------------------------------
local function smoothstep01(value)
  local t = math.max(0, math.min(1, value))
  return t * t * (3 - 2 * t)
end

local stingerPass = {
  blendMode = render.BlendMode.AlphaBlend,
  depthMode = render.DepthMode.Off,
  textures = { txStinger = 'stinger.png' },
  values = {
    gOffsetX = -2,
    gEmissive = 3,
    gWhiteReference = 1,
    gUseGreenKey = 0,
    gKeyThreshold = STINGER_VIDEO_KEY_THRESHOLD,
    gKeySoftness = STINGER_VIDEO_KEY_SOFTNESS,
  },
  shader = [[
    float4 main(PS_IN pin) {
      if (gUseGreenKey > 0.5) {
        // Green-screen key on green dominance, not RGB distance: H.264 is
        // limited-range 4:2:0, so the flat key green drifts at edges.
        float4 video = txStinger.Sample(samLinear, pin.Tex);
        float dominance = video.g - max(video.r, video.b);
        float alpha = saturate((gKeyThreshold - dominance) / gKeySoftness);
        float spill = max(0.0, video.g - max(video.r, video.b));
        video.g = video.g - spill * (1.0 - alpha);
        return float4(video.rgb * gWhiteReference * gEmissive, alpha);
      }
      float2 uv = float2((pin.Tex.x - gOffsetX) * 0.5, pin.Tex.y);
      if (uv.x < 0 || uv.x > 1) return float4(0, 0, 0, 0);
      float4 color = txStinger.Sample(samLinear, uv);
      return float4(color.rgb * gWhiteReference * gEmissive, color.a);
    }
  ]],
}

-- Registered as a scene render callback, not a UI callback: CSP's clean-OBS
-- mode removes the UI layer but keeps this transparent render pass.
function renderStinger()
  if stinger == nil then return end

  local useVideo = stinger.video == true and stingerVideoReady()
  stingerPass.textures.txStinger = useVideo and stingerPlayer or 'stinger.png'
  stingerPass.values.gUseGreenKey = useVideo and 1 or 0
  -- Video frames are display-referred; the PNG strip needs CSP's HDR boost.
  stingerPass.values.gEmissive = useVideo and 1 or 3

  if useVideo then
    -- The clip carries its own wipe, so it is sampled full-screen.
    stingerPass.values.gOffsetX = 0
  else
    local x
    if stinger.phase == 'cover' then
      local t = smoothstep01((os.preciseClock() - stinger.phaseStarted) / STINGER_TIMING.slideInS)
      x = -2 + 1.5 * t
    elseif stinger.phase == 'wait' then
      x = -0.5
    else
      local t = smoothstep01((os.preciseClock() - stinger.phaseStarted) / STINGER_TIMING.slideOutS)
      x = -0.5 + 1.5 * t
    end
    stingerPass.values.gOffsetX = x
  end

  -- Match CSP's HDR value for display white so exposure changes do not dim
  -- or brighten the stinger along with the scene.
  stingerPass.values.gWhiteReference = sim.whiteReferencePoint
  render.fullscreenPass(stingerPass)
end

local COLOR_FOCUSED = rgbm(0.2, 0.6, 1.0, 1.0)
local COLOR_OFFLINE = rgbm(0.5, 0.5, 0.5, 1.0)
local COLOR_DEFAULT = rgbm(1.0, 1.0, 1.0, 1.0)
local COLOR_DIM     = rgbm(0.6, 0.6, 0.6, 1.0)

local COLOR_ROW_FOCUSED = rgbm(0.16, 0.22, 0.28, 1.0)

-- Incident badges in the in-game driver list. Unlike the amber web panel this
-- window already uses a blue focus colour, so HIT/FLIP get their own hues.
local COLOR_HIT  = rgbm(0.95, 0.24, 0.12, 1.0)   -- contact, most severe
local COLOR_FLIP = rgbm(1.00, 0.82, 0.18, 1.0)   -- rollover

local STATUS_COL_W = 42

local function drawStatusBadge(pos, height, label, fill)
  local p1 = vec2(pos.x, pos.y + 2)
  local p2 = vec2(pos.x + STATUS_COL_W - 3, pos.y + height - 2)
  ui.drawRectFilled(p1, p2, fill)
  ui.drawTextClipped(label, p1, p2, rgbm(0, 0, 0, 1), vec2(0.5, 0.5))
end

local function drawDriverColumns(pos, width, height, position, name,
    interval, color, status, statusColor, depth, depthColor)
  local nameX = pos.x + 28
  local hasStatus = status ~= nil and status ~= ''
  local statusW = hasStatus and STATUS_COL_W or 0
  -- When no badge is present the interval stays flush to the window edge,
  -- exactly as before; with a badge it yields 4 px so the two never touch.
  local intervalRightX = pos.x + width - statusW - (hasStatus and 4 or 0)
  local intervalX = intervalRightX - 58
  -- The debug signal column takes its space from the name, not the interval:
  -- gaps are the operator's primary read and must not shift.
  local depthW = (depth ~= nil and HIT_DEBUG) and HIT_DEBUG_COL_W or 0
  local depthRightX = intervalX - 4
  local depthX = depthRightX - depthW
  local nameRightX = depthW > 0 and (depthX - 4) or (intervalX - 4)
  ui.drawTextClipped(position, pos, vec2(nameX - 4, pos.y + height),
    COLOR_DIM, vec2(0, 0.5))
  ui.drawTextClipped(name, vec2(nameX, pos.y), vec2(nameRightX, pos.y + height),
    color, vec2(0, 0.5), true)
  ui.drawTextClipped(interval, vec2(intervalX, pos.y), vec2(intervalRightX, pos.y + height),
    color, vec2(1, 0.5))
  if depthW > 0 then
    ui.drawTextClipped(depth, vec2(depthX, pos.y), vec2(depthRightX, pos.y + height),
      depthColor or COLOR_DIM, vec2(1, 0.5))
  end
  if hasStatus then
    drawStatusBadge(vec2(pos.x + width - STATUS_COL_W, pos.y), height, status, statusColor)
  end
end

function script.windowMain(dt)
  local focusedCar = sim.focusedCar

  ui.pushFont(ui.Font.Small)
  ui.pushStyleVar(ui.StyleVar.ItemSpacing, vec2(0, 0))
  local rowHeight = math.max(20, ui.measureText('Ag').y + 4)

  -- Keep all three columns aligned even when the list needs a scrollbar.
  ui.beginChild('drivers', vec2(0, 0), false, ui.WindowFlags.None)
  local width = ui.availableSpaceX()
  drawDriverColumns(ui.getCursor(), width, rowHeight, 'P', 'Driver', 'Int',
    COLOR_DIM, nil, nil, HIT_DEBUG and 'Imp' or nil, COLOR_DIM)
  ui.dummy(vec2(width, rowHeight))

  for _, d in ipairs(driverRows) do
    local isFocused = d.carId == focusedCar
    local color = isFocused and COLOR_FOCUSED
      or (d.isConnected and COLOR_DEFAULT or COLOR_OFFLINE)
    local status, statusColor = nil, nil
    if d.isRolledOver then
      status, statusColor = 'FLP', COLOR_FLIP
    elseif d.isHit then
      status, statusColor = 'HIT', COLOR_HIT
    end
    local depthText, depthColor
    if HIT_DEBUG then
      local depth = hitDebugDepth[d.carId]
      local live = hitDebugResidual[d.carId] or 0
      local peak = hitDebugPeak[d.carId]
      local partner = hitDebugPartner[d.carId]
      if depth ~= nil then
        depthText, depthColor = string.format('d%.2f', depth), COLOR_HIT
      elseif live > HIT_RESIDUAL_MS2 * 0.25 then
        depthText, depthColor = string.format('%.0f', live), COLOR_HIT
      elseif partner ~= nil and d.isHit then
        depthText, depthColor = string.format('@%d', partner), COLOR_HIT
      elseif peak ~= nil and peak > HIT_RESIDUAL_MS2 * 0.25 then
        depthText, depthColor = string.format('(%.0f)', peak), COLOR_DIM
      else
        depthText, depthColor = '-', COLOR_OFFLINE
      end
    end
    local pos = ui.getCursor()
    if ui.invisibleButton('driver##' .. d.carId, vec2(width, rowHeight))
        and d.isConnected and not isFocused then
      ac.focusCar(d.carId)
    end
    if isFocused then
      ui.drawRectFilled(pos, vec2(pos.x + width, pos.y + rowHeight), COLOR_ROW_FOCUSED)
    end
    drawDriverColumns(pos, width, rowHeight, tostring(d.position), d.name,
      d.interval or '', color, status, statusColor, depthText, depthColor)
  end

  ui.endChild()
  ui.popStyleVar()
  ui.popFont()
end

local function parsePiPDriverName(value)
  local name = tostring(value or '')
  while true do
    local stripped, count = name:gsub('^%s*%b[]%s*', '', 1)
    name = stripped
    if count == 0 then break end
  end
  name = name:match('^%s*(.-)%s*$') or ''
  return name
end

function script.windowPiP(dt)
  local available = ui.availableSpace()
  local origin = ui.getCursor()
  ui.drawRectFilled(origin,
    vec2(origin.x + available.x, origin.y + available.y),
    rgbm(0, 0, 0, 1))

  if not pipFrameReady or pipShot == nil then
    ui.setCursor(vec2(origin.x + 12, origin.y + 12))
    ui.text('PiP unavailable')
    return
  end

  local car = ac.getCar(pipDriver)
  local name = parsePiPDriverName(car ~= nil and car:driverName() or '')
  if name == '' then name = 'Car ' .. tostring(pipDriver) end
  local position = 0
  if car ~= nil then position = correctedStandings(pipDriver, car) end
  local team = ac.getDriverTeam(pipDriver) or ''
  local identity = string.format('P%d | %s', position, name)
  local identityFontSize = 18
  local teamFontSize = 13
  local paddingX = 8
  local paddingY = 6
  local lineGap = 3

  ui.pushDWriteFont('Michroma-Regular.ttf')
  local identitySize = ui.measureDWriteText(identity, identityFontSize)
  local teamSize = ui.measureDWriteText(team, teamFontSize)
  local headerHeight =
    identitySize.y + teamSize.y + paddingY * 2 + lineGap
  local videoHeight = math.max(available.y - headerHeight, 0)
  local width = available.x
  local height = width * 9 / 16
  if height > videoHeight then
    height = videoHeight
    width = height * 16 / 9
  end
  local x = origin.x + (available.x - width) * 0.5
  local y = origin.y + headerHeight
  local p1 = vec2(x, y)
  local p2 = vec2(x + width, y + height)
  ui.beginTonemapping()
  ui.drawImage(pipShot, p1, p2,
    rgbm(pipRedGain, pipGreenGain, pipBlueGain, 1))
  ui.endTonemapping(pipGamma, pipWhitePoint, true)

  local plateP1 = origin
  local plateP2 = vec2(origin.x + available.x, y)
  ui.drawRectFilled(plateP1, plateP2, rgbm(0, 0, 0, 1))
  ui.setCursor(vec2(plateP1.x + paddingX, plateP1.y + paddingY))
  ui.dwriteText(identity, identityFontSize, rgbm(1, 1, 1, 1))
  ui.setCursor(vec2(
    plateP1.x + paddingX,
    plateP1.y + paddingY + identitySize.y + lineGap))
  ui.dwriteText(team, teamFontSize, rgbm(0.78, 0.78, 0.78, 1))
  ui.popDWriteFont()
end

local function pipColorSlider(label, key, value, minimum, maximum)
  local updated, changed = ui.slider(
    label, value, minimum, maximum, '%.2f')
  if changed then
    pipColorSettings:set('PIP_COLOR', key, updated)
    pipColorSettings:save()
  end
  return updated
end

function script.windowPiPSettings(dt)
  pipGamma = pipColorSlider('Gamma', 'GAMMA', pipGamma, 0.4, 5.0)
  pipWhitePoint = pipColorSlider(
    'White point', 'WHITE_POINT', pipWhitePoint, 0.0, 25.0)
  pipRedGain = pipColorSlider('Red gain', 'RED_GAIN', pipRedGain, 0.0, 2.0)
  pipGreenGain = pipColorSlider(
    'Green gain', 'GREEN_GAIN', pipGreenGain, 0.0, 2.0)
  pipBlueGain = pipColorSlider(
    'Blue gain', 'BLUE_GAIN', pipBlueGain, 0.0, 2.0)

  if ui.button('Reset color') then
    pipGamma = 1.0
    pipWhitePoint = 0.0
    pipRedGain = 1.0
    pipGreenGain = 1.0
    pipBlueGain = 1.0
    pipColorSettings:set('PIP_COLOR', 'GAMMA', pipGamma)
    pipColorSettings:set('PIP_COLOR', 'WHITE_POINT', pipWhitePoint)
    pipColorSettings:set('PIP_COLOR', 'RED_GAIN', pipRedGain)
    pipColorSettings:set('PIP_COLOR', 'GREEN_GAIN', pipGreenGain)
    pipColorSettings:set('PIP_COLOR', 'BLUE_GAIN', pipBlueGain)
    pipColorSettings:save()
  end
end

local indicatorSettings = ac.INIConfig.scriptSettings()
local indicatorFontSize = indicatorSettings:get('INDICATOR', 'FONT_SIZE', 32)

function script.windowLiveReplay(dt)
  local label = 'LIVE'
  local color = rgbm(0.2, 1.0, 0.4, 1.0)
  if sim.isReplayActive or sim.isReplayOnlyMode then
    label = 'REPLAY'
    color = rgbm(1.0, 0.6, 0.2, 1.0)
  end

  ui.pushDWriteFont('Michroma-Regular.ttf')
  local textSize = ui.measureDWriteText(label, indicatorFontSize)
  ui.image('logo.png', vec2(textSize.y, textSize.y))
  ui.sameLine(0, 8)
  ui.dwriteText(label, indicatorFontSize, color)
  ui.popDWriteFont()
end

function script.windowLiveReplaySettings(dt)
  local value, changed = ui.slider('Font size', indicatorFontSize, 12, 96, '%.0f', true)
  if changed then
    indicatorFontSize = value
    indicatorSettings:set('INDICATOR', 'FONT_SIZE', value)
    indicatorSettings:save()
  end
  if ui.checkbox('Hide other OBS HUD apps during replay',
      hideOtherObsHudAppsInReplay) then
    hideOtherObsHudAppsInReplay = not hideOtherObsHudAppsInReplay
    obsHudSettings:set(
      'OBS_HUD', 'HIDE_OTHERS_IN_REPLAY', hideOtherObsHudAppsInReplay)
    obsHudSettings:save()
  end
end
