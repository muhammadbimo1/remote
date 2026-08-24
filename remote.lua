local sim = ac.getSim()
local ffi = require('ffi')

---------------------------------------------------------------------
-- Mmap IPC — layout strings must match ipc_shared.py exactly
-- Using the layout-string API so the GC handle lives on the returned
-- pointer (which surviving functions reference), not on a temporary.
---------------------------------------------------------------------
local TELEM_TAG = 'broadcaster_remote_telemetry'
local CMD_TAG = 'broadcaster_remote_commands'

local telemPage = ac.writeMemoryMappedFile(TELEM_TAG, [[
  int packet_id;
  int car_count;
  int focused_car;
  int current_camera;
  int car_cameras_count;
  int current_car_camera;
  float track_length;
  int session_type;
  int session_index;
  int session_type_raw;
  int session_gen;
  wchar_t session_name[64];
  int is_replay;
  int replay_frame;
  int replay_frames;
  float replay_frame_ms;
  int replay_last_result;
  int is_replay_only;
  wchar_t replay_file[256];
  wchar_t replay_temp_dir[256];
  wchar_t timetable_url[128];
  struct {
    int car_id;
    int session_id;
    int position;
    float normalized_spline_pos;
    float speed_kmh;
    int lap_time;
    int best_lap;
    int last_lap;
    int lap_count;
    int is_in_pit;
    int is_connected;
    int is_colliding;
    int is_rolled_over;
    wchar_t driver_name[64];
    wchar_t team_name[64];
  } cars[128];
//<__EXPAL:0:4>]])

local cmdPage = ac.writeMemoryMappedFile(CMD_TAG, [[
  int packet_id;
  int target_driver;
  int target_camera;
  int target_car_camera;
  int command_seq;
  int replay_seq;
  int replay_action;
  float replay_rewind_s;
  int replay_frame;
//<__EXPAL:0:4>]])

-- Replay actions — must match the REPLAY_* constants in ipc_shared.py
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
local updateAccum = 0
local UPDATE_INTERVAL = 0.1 -- 100 ms

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

-- ac.tryToToggleReplay's rewindS overshoots on this rig (it converts seconds at
-- a fixed 60 fps while the recording is ~16.7 fps). So entry uses only a nominal
-- rewind, and the real position is applied by a precise seek to
-- `sim.replayFrames - rewind_s/replayFrameMs` once replay is active.
local REPLAY_ENTER_NOMINAL_REWIND_S = 0.5
local pendingSeek = nil        -- absolute frame to seek to once replay is active

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

-- Collision flags: consumed (and reset) by telemetry writer.
-- Callback fires reliably only for the local car; damage-array deltas
-- cover remote cars since damage state is replicated online.
-- Severity gate: kerb hits and wall brushes trigger the callback/damage
-- without any real impact, so we require a minimum speed drop in the
-- same tick before reporting a collision.
local COLLISION_SPEED_DROP_KMH = 12
local collisionFlags = {}
local prevDamageSum = {}
local prevSpeedKmh = {}

-- Rollover detection: car.up.y < 0.5 means chassis is tilted > 60° from
-- vertical (cos(60°) = 0.5). Asymmetric thresholds + a hold counter
-- prevent flicker during hard landings or curb hops.
local ROLLOVER_UP_Y_ENTER = 0.5   -- cos(60°)
local ROLLOVER_UP_Y_EXIT  = 0.6   -- cos(~53°)
local ROLLOVER_HOLD_TICKS = 2     -- ~200ms at 10 Hz telemetry
local rolloverState = {}
local rolloverTickCount = {}

ac.onCarCollision(-1, function(carIndex)
  collisionFlags[carIndex] = true
end)

-- A replay start/stop/jump teleports every car, which produces speed and damage
-- deltas large enough to trip the collision and rollover heuristics above. Drop
-- the previous-tick baselines so the next tick starts clean.


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
  return CSP_TO_CUSTOM[mode] or 0
end

local function getCameraName(mode)
  return CAMERA_NAMES[mode] or 'Unknown'
end

---------------------------------------------------------------------
-- UTF-16 helper: write a Lua string into a wchar_t[64] field
---------------------------------------------------------------------
local function writeWchar(dst, str, maxChars)
  ffi.fill(dst, maxChars * 2, 0)
  if not str or str == '' then return end
  local utf16 = ac.utf8To16(str)
  if utf16 then
    local maxBytes = (maxChars - 1) * 2
    local copyLen = math.min(#utf16, maxBytes)
    ffi.copy(dst, utf16, copyLen)
  end
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

local function processReplayCommands()
  local seq = cmdPage.replay_seq
  if seq == lastReplaySeq then return end
  lastReplaySeq = seq

  local action = cmdPage.replay_action
  local active = sim.isReplayActive
  local now = os.preciseClock()

  -- A toggle landing inside another toggle's transition is what crashed AC.
  if now < replayBusyUntil then
    ac.log(string.format('Replay command %d ignored: transition in progress (active=%s)',
      action, tostring(active)))
    return
  end

  if action == REPLAY_ENTER then
    local rewindS = cmdPage.replay_rewind_s
    local driver = cmdPage.target_driver
    local camera = cmdPage.target_camera
    local subCam = cmdPage.target_car_camera
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
      enterReplay(rewindS, driver, camera, subCam)
    end
  elseif action == REPLAY_LIVE then
    if active then
      -- Keep the car that was on screen in the replay: leaving replay makes AC
      -- snap focus back to the player car, which is never what a director wants.
      exitReplay('command')
    else
      ac.log('Replay live ignored: not in replay')
    end
  elseif action == REPLAY_SEEK_FRAME then
    if active or sim.isReplayOnlyMode then
      ac.setReplayPosition(cmdPage.replay_frame, 0)
    else
      ac.log('Replay seek ignored: not in replay')
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

ac.onReplay(function(event)
  ac.log('Replay event: ' .. tostring(event))
  -- Every AC-side transition re-arms the settle window, so a command arriving
  -- mid-transition is ignored rather than stacked on top of it.
  replayBusyUntil = math.max(replayBusyUntil, os.preciseClock() + REPLAY_SETTLE_S)

  -- A replay start/stop/jump teleports every car, which produces speed and
  -- damage deltas large enough to trip the collision and rollover heuristics.
  collisionFlags = {}
  prevDamageSum = {}
  prevSpeedKmh = {}
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
  local seq = cmdPage.command_seq
  if seq ~= lastCommandSeq then
    lastCommandSeq = seq
    -- An explicit pick from the panel wins over any shot being held, otherwise
    -- the hold would drag the operator back to the replay's car.
    shotHold = nil
    applyFocus(cmdPage.target_driver, cmdPage.target_camera, cmdPage.target_car_camera)
  end
end

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

  for i = 0, carCount - 1 do
    local car = ac.getCar(i)
    if car.isConnected then
      rows[#rows + 1] = {
        carId       = i,
        position    = car.racePosition,
        spline      = car.splinePosition,
        speedKmh    = car.speedKmh,
        lapTimeMs   = car.lapTimeMs,
        bestLapMs   = car.bestLapTimeMs,
        lastLapMs   = car.previousLapTimeMs,
        lapCount    = car.lapCount,
        isInPit     = car.isInPitlane,
        isConnected = car.isConnected,
        name        = car:driverName(),
        totalProgress = car.lapCount + car.splinePosition,
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

  telemPage.car_count = carCount
  telemPage.focused_car = sim.focusedCar
  telemPage.current_camera = cspCameraToCustom(sim.cameraMode)
  local focusedCar = ac.getCar(sim.focusedCar)
  telemPage.car_cameras_count = focusedCar and focusedCar.carCamerasCount or 0
  telemPage.current_car_camera = sim.carCameraIndex
  telemPage.track_length = sim.trackLengthM
  telemPage.session_type = (sim.raceSessionType == ac.SessionType.Race) and 1 or 0

  local sessionIndex = sim.currentSessionIndex
  local sessionTypeRaw = sim.raceSessionType
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
  end
  telemPage.session_index = sessionIndex
  telemPage.session_type_raw = sessionTypeRaw
  telemPage.session_gen = sessionGen
  writeWchar(telemPage.session_name, ac.getSessionName(sessionIndex) or '', 64)

  telemPage.is_replay = sim.isReplayActive and 1 or 0
  telemPage.replay_frame = sim.replayCurrentFrame
  telemPage.replay_frames = sim.replayFrames
  telemPage.replay_frame_ms = sim.replayFrameMs
  telemPage.replay_last_result = replayLastResult
  telemPage.is_replay_only = sim.isReplayOnlyMode and 1 or 0
  writeWchar(telemPage.replay_file, ac.getReplayFilename() or '', 256)
  -- Where AC keeps the per-session .acreplay it is recording right now. The
  -- web server pairs its event journal to that file by name.
  writeWchar(telemPage.replay_temp_dir, ac.getFolder(ac.FolderID.ReplaysTemp) or '', 256)
  local serverIP = ac.getServerIP()
  local httpPort = ac.getServerPortHTTP()
  local timetableURL = ''
  if serverIP ~= nil and serverIP ~= '' and httpPort ~= nil and httpPort >= 0 then
    timetableURL = 'http://' .. serverIP .. ':' .. tostring(httpPort) .. '/timetable.json'
  end
  writeWchar(telemPage.timetable_url, timetableURL, 128)

  for i = 0, carCount - 1 do
    local car = ac.getCar(i)
    local c = telemPage.cars[i]
    c.car_id = i
    c.session_id = car.sessionID
    c.position = car.racePosition
    c.normalized_spline_pos = car.splinePosition
    c.speed_kmh = car.speedKmh
    c.lap_time = car.lapTimeMs
    c.best_lap = car.bestLapTimeMs
    c.last_lap = car.previousLapTimeMs
    c.lap_count = car.lapCount
    c.is_in_pit = car.isInPitlane and 1 or 0
    c.is_connected = car.isConnected and 1 or 0
    local dmgSum = car.damage[0] + car.damage[1] + car.damage[2] + car.damage[3] + car.damage[4]
    local dmgJumped = prevDamageSum[i] ~= nil and dmgSum > prevDamageSum[i] + 0.01
    prevDamageSum[i] = dmgSum
    local speedDrop = prevSpeedKmh[i] and (prevSpeedKmh[i] - car.speedKmh) or 0
    prevSpeedKmh[i] = car.speedKmh
    local hardImpact = speedDrop >= COLLISION_SPEED_DROP_KMH
    c.is_colliding = ((collisionFlags[i] or dmgJumped) and hardImpact) and 1 or 0
    collisionFlags[i] = false

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
    c.is_rolled_over = rolledOver and 1 or 0

    writeWchar(c.driver_name, car:driverName(), 64)
    writeWchar(c.team_name, ac.getDriverTeam(i), 64)
  end

  telemPage.packet_id = telemPage.packet_id + 1
end

---------------------------------------------------------------------
-- Main update loop
---------------------------------------------------------------------
function script.update(dt)
  -- Commands run every frame: they are a sequence compare when idle, and the
  -- deferred replay shot needs to land the frame replay comes up, not up to
  -- 100 ms later.
  processReplayCommands()
  processPendingSeek()
  processShotHold()
  processReplayResultExpiry()
  processCommands()
  syncLeaderboard()

  updateAccum = updateAccum + dt
  if updateAccum < UPDATE_INTERVAL then return end
  updateAccum = 0

  updateTelemetry()
  driverRows = buildDriverRows()
end

---------------------------------------------------------------------
-- In-game UI
---------------------------------------------------------------------
local COLOR_FOCUSED = rgbm(0.2, 0.6, 1.0, 1.0)
local COLOR_PIT     = rgbm(1.0, 0.7, 0.2, 1.0)
local COLOR_OFFLINE = rgbm(0.5, 0.5, 0.5, 1.0)
local COLOR_DEFAULT = rgbm(1.0, 1.0, 1.0, 1.0)
local COLOR_DIM     = rgbm(0.6, 0.6, 0.6, 1.0)

local CAMERA_LABELS = { 'Track', 'Cockpit', 'Heli', 'F6', 'Orbit' }

function script.windowMain(dt)
  local focusedCar = sim.focusedCar

  -- Status line
  ui.pushFont(ui.Font.Small)
  ui.textColored(
    string.format('Focused: #%d  |  Camera: %s', focusedCar, getCameraName(sim.cameraMode)),
    rgbm(0.7, 0.9, 1.0, 1.0)
  )
  ui.popFont()

  ui.offsetCursorY(4)

  -- Camera buttons
  local btnW = math.floor((ui.availableSpaceX() - 4 * 4) / 5)
  for i = 1, 5 do
    if i > 1 then ui.sameLine(0, 4) end
    if ui.button(CAMERA_LABELS[i], vec2(btnW, 28)) then
      changeCamera(i, focusedCar, -1)
    end
  end

  ui.offsetCursorY(6)
  ui.separator()
  ui.offsetCursorY(4)

  -- Column headers
  ui.pushFont(ui.Font.Small)
  ui.textColored('P', COLOR_DIM) ui.sameLine(24)
  ui.textColored('Driver', COLOR_DIM) ui.sameLine(180)
  ui.textColored('Gap', COLOR_DIM) ui.sameLine(250)
  ui.textColored('Int', COLOR_DIM)
  ui.popFont()
  ui.offsetCursorY(2)

  -- Driver list (scrollable)
  ui.beginChild('drivers', vec2(0, 0), false, ui.WindowFlags.None)

  for _, d in ipairs(driverRows) do
    local isFocused = d.carId == focusedCar
    local statusText = ''
    local nameColor = COLOR_DEFAULT

    if not d.isConnected then
      statusText = 'OFF'
      nameColor = COLOR_OFFLINE
    elseif d.isInPit then
      statusText = 'PIT'
      nameColor = COLOR_PIT
    end

    if isFocused then
      nameColor = COLOR_FOCUSED
    end

    -- Position
    ui.text(tostring(d.position))
    ui.sameLine(24)

    -- Name + status
    ui.textColored(d.name, nameColor)
    if statusText ~= '' then
      ui.sameLine()
      ui.pushFont(ui.Font.Small)
      ui.textColored(statusText, nameColor)
      ui.popFont()
    end
    ui.sameLine(180)

    -- Gap
    ui.pushFont(ui.Font.Small)
    ui.text(d.gap or '')
    ui.popFont()
    ui.sameLine(250)

    -- Interval
    ui.pushFont(ui.Font.Small)
    ui.text(d.interval or '')
    ui.popFont()

    -- Select button
    ui.sameLine(310)
    local disabled = isFocused or not d.isConnected
    if disabled then
      ui.pushStyleVar(ui.StyleVar.Alpha, 0.3)
    end
    if ui.button('Sel##' .. d.carId, vec2(50, 0)) and not disabled then
      ac.focusCar(d.carId)
    end
    if disabled then
      ui.popStyleVar()
    end
  end

  ui.endChild()
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

  ui.pushDWriteFont('static/Michroma-Regular.ttf')
  local textSize = ui.measureDWriteText(label, indicatorFontSize)
  ui.image('static/logo.png', vec2(textSize.y, textSize.y))
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
end
