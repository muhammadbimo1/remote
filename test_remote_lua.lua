local function assertEqual(actual, expected, message)
  if actual ~= expected then
    error(string.format('%s: expected %s, got %s', message, tostring(expected), tostring(actual)), 2)
  end
end

local function loadRemote(isReplayActive)
  local now = 0
  local replaySeq = 0
  local toggleCalls = {}
  local seekCalls = {}
  local renderCalls = {}
  local socketURL = nil
  local socketParams = nil
  local socketCallback = nil
  local telemetryPayloads = {}
  local sim = {
    isReplayActive = isReplayActive,
    isReplayOnlyMode = false,
    focusedCar = 0,
    cameraMode = 1,
    carCameraIndex = 0,
    replayFrames = 1000,
    replayFrameMs = 60,
    replayCurrentFrame = 100,
    carsCount = 1,
    trackLengthM = 5000,
    currentSessionIndex = 0,
    raceSessionType = 3,
    windowWidth = 1920,
    windowHeight = 1080,
  }
  local car = {
    sessionID = 18,
    racePosition = 1,
    splinePosition = 0.5,
    speedKmh = 120,
    lapTimeMs = 50000,
    bestLapTimeMs = 100000,
    previousLapTimeMs = 101000,
    lapCount = 3,
    isInPitlane = false,
    isConnected = true,
    damage = { [0] = 0, 0, 0, 0, 0 },
    up = { y = 1 },
    carCamerasCount = 3,
    driverName = function() return 'Alex Driver' end,
  }

  package.preload.ffi = function()
    return {
      fill = function() end,
      copy = function() end,
      stringToFFI = function() return '' end,
    }
  end

  _G.script = {}
  _G.vec2 = function(x, y) return { x = x, y = y } end
  _G.rgbm = setmetatable({ colors = { white = {} } }, {
    __call = function() return {} end,
  })
  _G.ui = {
    Font = { Small = 1 },
    StyleVar = { Alpha = 1 },
    WindowFlags = { None = 0 },
    ImageFit = { Stretch = 0 },
    windowSize = function() return { x = 1920, y = 1080 } end,
    drawImage = function() end,
  }
  _G.render = {
    BlendMode = { AlphaBlend = 1 },
    DepthMode = { Off = 2 },
    fullscreenPass = function(params)
      renderCalls[#renderCalls + 1] = params
    end,
  }
  _G.JSON = {
    stringify = function(value)
      telemetryPayloads[#telemetryPayloads + 1] = value
      return 'encoded telemetry'
    end,
    parse = function(value) return value end,
  }
  _G.web = {
    socket = function(url, callback, params)
      socketURL = url
      socketCallback = callback
      socketParams = params
      return function() end
    end,
  }
  _G.ac = {
    CameraMode = {
      Track = 1, Cockpit = 2, Helicopter = 3, Car = 4, OnBoardFree = 5,
      Drivable = 6, Free = 7,
    },
    SessionType = { Race = 3 },
    FolderID = { ReplaysTemp = 1 },
    INIConfig = {
      scriptSettings = function()
        return { get = function(_, _, _, fallback) return fallback end }
      end,
    },
    getSim = function() return sim end,
    getCar = function() return car end,
    getSessionName = function() return 'Race' end,
    getReplayFilename = function() return '' end,
    getFolder = function() return 'C:/AC/replay/temp' end,
    getServerIP = function() return '' end,
    getServerPortHTTP = function() return -1 end,
    getDriverTeam = function() return 'PRO 7 | Team' end,
    onSessionStart = function() end,
    onCarCollision = function() end,
    onReplay = function() end,
    disableExtraHUDElements = function() end,
    tryToToggleReplay = function(active, rewind)
      toggleCalls[#toggleCalls + 1] = { active = active, rewind = rewind }
      return true
    end,
    setReplayPosition = function(frame, mode)
      seekCalls[#seekCalls + 1] = { frame = frame, mode = mode }
    end,
    focusCar = function(car) sim.focusedCar = car end,
    setCurrentCamera = function(camera) sim.cameraMode = camera end,
    setCurrentCarCamera = function(camera) sim.carCameraIndex = camera end,
    log = function() end,
  }
  os.preciseClock = function() return now end

  dofile('remote.lua')

  return {
    sim = sim,
    toggleCalls = toggleCalls,
    seekCalls = seekCalls,
    renderCalls = renderCalls,
    nextReplaySeq = function()
      replaySeq = replaySeq + 1
      return replaySeq
    end,
    socketURL = function() return socketURL end,
    socketParams = function() return socketParams end,
    telemetryPayloads = telemetryPayloads,
    deliver = function(message) socketCallback(message) end,
    update = function(dt) script.update(dt or 0) end,
    setTime = function(value) now = value end,
  }
end

local requestReplay

local function testStingerRendersInScenePassForCleanOutput()
  local ctx = loadRemote(false)
  requestReplay(ctx, 1)
  ctx.update()

  assertEqual(type(renderStinger), 'function', 'the app exposes a scene-render stinger callback')
  renderStinger()
  assertEqual(ctx.renderCalls[1].textures.txStinger, 'static/stinger.png',
    'the scene pass samples the stinger asset')
  assertEqual(ctx.renderCalls[1].values.gOffsetX, -2,
    'the stinger starts just off the left edge')
  assertEqual(ctx.renderCalls[1].values.gEmissive, 4,
    'the scene pass supplies HDR luminance for exposure-resistant color')
  assertEqual(ctx.renderCalls[1].depthMode, 2,
    'the stinger ignores scene depth')

  ctx.setTime(0.200)
  ctx.update()
  renderStinger()
  assertEqual(ctx.renderCalls[2].values.gOffsetX, -0.5,
    'the two-screen strip fully covers the game at the replay toggle')
end

requestReplay = function(ctx, action)
  ctx.deliver({
    version = 1,
    type = 'replay',
    replay_seq = ctx.nextReplaySeq(),
    replay_action = action,
    replay_rewind_s = 12,
    replay_frame = 0,
    target_driver = 4,
    target_camera = 1,
    target_car_camera = -1,
  })
end

local function testWebSocketConnectsToLoopbackWithReconnect()
  local ctx = loadRemote(false)
  assertEqual(ctx.socketURL(), 'ws://127.0.0.1:5000/ac-ipc',
    'the CSP app connects to the local server on its existing port')
  assertEqual(ctx.socketParams().encoding, 'utf8', 'the socket uses UTF-8 text frames')
  assertEqual(ctx.socketParams().reconnect, true, 'the socket reconnects automatically')
end

local function testTelemetryUsesVersionedProtocolAtTenHertz()
  local ctx = loadRemote(false)
  ctx.update(0.099)
  assertEqual(#ctx.telemetryPayloads, 0, 'telemetry waits for the 100 ms cadence')
  ctx.update(0.001)
  local payload = ctx.telemetryPayloads[1]
  assertEqual(payload.version, 1, 'telemetry carries protocol version 1')
  assertEqual(payload.type, 'telemetry', 'telemetry has its message discriminator')
  assertEqual(payload.car_count, 1, 'telemetry carries the bounded car array')
  assertEqual(payload.cars[1].session_id, 18, 'telemetry includes remote session IDs')
  assertEqual(payload.cars[1].driver_name, 'Alex Driver', 'telemetry includes driver names')
end

local function testCameraCommandsApplyOnceAndMalformedMessagesAreIgnored()
  local ctx = loadRemote(false)
  ctx.deliver({
    version = 1, type = 'command', command_seq = 1,
    target_driver = 4, target_camera = 1, target_car_camera = -1,
  })
  ctx.update()
  assertEqual(ctx.sim.focusedCar, 4, 'a valid camera command applies')

  ctx.sim.focusedCar = 2
  ctx.deliver({
    version = 1, type = 'command', command_seq = 1,
    target_driver = 5, target_camera = 1, target_car_camera = -1,
  })
  ctx.update()
  assertEqual(ctx.sim.focusedCar, 2, 'a duplicate command sequence is ignored')

  ctx.deliver({
    version = 2, type = 'command', command_seq = 2,
    target_driver = 6, target_camera = 1, target_car_camera = -1,
  })
  ctx.deliver({
    version = 1, type = 'command', command_seq = 3,
    target_driver = 7,
  })
  ctx.update()
  assertEqual(ctx.sim.focusedCar, 2, 'unsupported and malformed commands are ignored')
end

local function testEnterWaitsUntilScreenIsCovered()
  local ctx = loadRemote(false)
  requestReplay(ctx, 1)

  ctx.update()
  assertEqual(#ctx.toggleCalls, 0, 'replay enter must not toggle before the wipe covers the game')

  ctx.setTime(0.199)
  ctx.update()
  assertEqual(#ctx.toggleCalls, 0, 'replay enter must remain queued during the cover phase')

  ctx.setTime(0.200)
  ctx.update()
  assertEqual(#ctx.toggleCalls, 1, 'replay enter toggles at full coverage')
  assertEqual(ctx.toggleCalls[1].active, true, 'replay enter uses the enter toggle')
end

local function testLiveWaitsUntilScreenIsCovered()
  local ctx = loadRemote(true)
  requestReplay(ctx, 2)

  ctx.update()
  assertEqual(#ctx.toggleCalls, 0, 'go-live must not toggle before the wipe covers the game')

  ctx.setTime(0.200)
  ctx.update()
  assertEqual(#ctx.toggleCalls, 1, 'go-live toggles at full coverage')
  assertEqual(ctx.toggleCalls[1].active, false, 'go-live uses the exit toggle')
end

local function testSeekWithinReplayDoesNotRunAStinger()
  local ctx = loadRemote(true)
  requestReplay(ctx, 1)

  ctx.update()
  assertEqual(#ctx.toggleCalls, 0, 'an in-replay jump must not toggle replay')
  assertEqual(#ctx.seekCalls, 1, 'an in-replay jump seeks immediately')
  assertEqual(ctx.seekCalls[1].frame, 800, 'the rewind converts to the expected replay frame')
end

testEnterWaitsUntilScreenIsCovered()
testLiveWaitsUntilScreenIsCovered()
testSeekWithinReplayDoesNotRunAStinger()
testStingerRendersInScenePassForCleanOutput()
testWebSocketConnectsToLoopbackWithReconnect()
testTelemetryUsesVersionedProtocolAtTenHertz()
testCameraCommandsApplyOnceAndMalformedMessagesAreIgnored()
print('test_remote_lua.lua: all tests passed')
