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
  struct {
    int car_id;
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
    wchar_t driver_name[64];
  } cars[128];
//<__EXPAL:0:4>]])

local cmdPage = ac.writeMemoryMappedFile(CMD_TAG, [[
  int packet_id;
  int target_driver;
  int target_camera;
  int target_car_camera;
  int command_seq;
//<__EXPAL:0:4>]])

---------------------------------------------------------------------
-- State
---------------------------------------------------------------------
local lastCommandSeq = 0
local updateAccum = 0
local UPDATE_INTERVAL = 0.1 -- 100 ms

-- Collision flags: set by ac.onCarCollision callbacks, consumed by telemetry writer
local collisionFlags = {}
local registeredCollisionCars = 0

local function registerCollisionCallbacks(carCount)
  for i = registeredCollisionCars, carCount - 1 do
    ac.onCarCollision(i, function()
      collisionFlags[i] = true
    end)
  end
  registeredCollisionCars = carCount
end

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
local function processCommands()
  local seq = cmdPage.command_seq
  if seq ~= lastCommandSeq then
    lastCommandSeq = seq
    local driver = cmdPage.target_driver
    local camera = cmdPage.target_camera
    local subCam = cmdPage.target_car_camera
    ac.focusCar(driver)
    changeCamera(camera, driver, subCam)
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

  for i = 0, carCount - 1 do
    local car = ac.getCar(i)
    local c = telemPage.cars[i]
    c.car_id = i
    c.position = car.racePosition
    c.normalized_spline_pos = car.splinePosition
    c.speed_kmh = car.speedKmh
    c.lap_time = car.lapTimeMs
    c.best_lap = car.bestLapTimeMs
    c.last_lap = car.previousLapTimeMs
    c.lap_count = car.lapCount
    c.is_in_pit = car.isInPitlane and 1 or 0
    c.is_connected = car.isConnected and 1 or 0
    c.is_colliding = collisionFlags[i] and 1 or 0
    collisionFlags[i] = false
    writeWchar(c.driver_name, car:driverName(), 64)
  end

  telemPage.packet_id = telemPage.packet_id + 1
end

---------------------------------------------------------------------
-- Main update loop
---------------------------------------------------------------------
function script.update(dt)
  updateAccum = updateAccum + dt
  if updateAccum < UPDATE_INTERVAL then return end
  updateAccum = 0

  if sim.carsCount > registeredCollisionCars then
    registerCollisionCallbacks(sim.carsCount)
  end

  updateTelemetry()
  processCommands()
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
