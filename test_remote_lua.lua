local function assertEqual(actual, expected, message)
  if actual ~= expected then
    error(string.format('%s: expected %s, got %s', message, tostring(expected), tostring(actual)), 2)
  end
end

local function loadRemote(isReplayActive, hideObsHudInReplay)
  local now = 0
  local replaySeq = 0
  local toggleCalls = {}
  local seekCalls = {}
  local renderCalls = {}
  local socketURL = nil
  local socketParams = nil
  local socketCallback = nil
  local telemetryPayloads = {}
  local releaseCallbacks = {}
  local settingsWrites = {}
  local settingsSaveCount = 0
  local clickCheckbox = false
  local windows = {
    { name = 'IMGUI_LUA_cmrt_main', title = 'CMRT Broadcast',
      layer = 3, layerDuplicate = true },
    { name = 'IMGUI_LUA_remote_live_replay', title = 'Live / Replay',
      layer = 3, layerDuplicate = true },
    { name = 'IMGUI_LUA_remote_main', title = 'Broadcaster Remote',
      layer = 2, layerDuplicate = false },
    { name = 'IMGUI_LUA_local', title = 'Local App',
      layer = 0, layerDuplicate = false },
  }
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
    slider = function(_, value) return value, false end,
    checkbox = function()
      local clicked = clickCheckbox
      clickCheckbox = false
      return clicked
    end,
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
        return {
          get = function(_, section, key, fallback)
            if section == 'OBS_HUD' and key == 'HIDE_OTHERS_IN_REPLAY'
                and hideObsHudInReplay ~= nil then
              return hideObsHudInReplay
            end
            return fallback
          end,
          set = function(_, section, key, value)
            settingsWrites[#settingsWrites + 1] = {
              section = section, key = key, value = value,
            }
          end,
          save = function() settingsSaveCount = settingsSaveCount + 1 end,
        }
      end,
    },
    getAppWindows = function() return windows end,
    accessAppWindow = function(name)
      for _, window in ipairs(windows) do
        if window.name == name then
          return {
            valid = function() return true end,
            redirectLayer2 = function()
              return window.layer, window.layerDuplicate
            end,
            setRedirectLayer = function(_, layer, duplicate)
              window.layer = layer
              window.layerDuplicate = duplicate == true
            end,
          }
        end
      end
      return nil
    end,
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
    onRelease = function(callback)
      releaseCallbacks[#releaseCallbacks + 1] = callback
    end,
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
    windows = windows,
    addWindow = function(window) windows[#windows + 1] = window end,
    settingsWrites = settingsWrites,
    settingsSaveCount = function() return settingsSaveCount end,
    nextReplaySeq = function()
      replaySeq = replaySeq + 1
      return replaySeq
    end,
    socketURL = function() return socketURL end,
    socketParams = function() return socketParams end,
    telemetryPayloads = telemetryPayloads,
    deliver = function(message) socketCallback(message) end,
    update = function(dt) script.update(dt or 0) end,
    clickObsSetting = function()
      clickCheckbox = true
      script.windowLiveReplaySettings(0)
    end,
    release = function()
      for _, callback in ipairs(releaseCallbacks) do callback() end
    end,
    setTime = function(value) now = value end,
  }
end

local requestReplay

local function testReplaySuppressesForwardedAppsExceptIndicator()
  local ctx = loadRemote(true)

  ctx.update()

  assertEqual(ctx.windows[1].layer, 0,
    'replay removes the broadcast HUD from its OBS layer')
  assertEqual(ctx.windows[2].layer, 3,
    'the Live / Replay indicator stays forwarded to OBS')
  assertEqual(ctx.windows[3].layer, 0,
    'replay removes every other redirected app from OBS')
  assertEqual(ctx.windows[4].layer, 0,
    'an app that was not forwarded remains untouched')
end

local function testReplaySuppressesNewlyForwardedApps()
  local ctx = loadRemote(true)
  ctx.update()
  ctx.addWindow({
    name = 'IMGUI_LUA_late', title = 'Late HUD',
    layer = 4, layerDuplicate = true,
  })

  ctx.update()

  assertEqual(ctx.windows[5].layer, 0,
    'apps forwarded after replay starts are also suppressed')
end

local function testReplayResuppressesAWindowWithoutLosingOriginalRedirect()
  local ctx = loadRemote(true)
  ctx.update()
  ctx.windows[1].layer = 5
  ctx.windows[1].layerDuplicate = false

  ctx.update()

  assertEqual(ctx.windows[1].layer, 0,
    'OBS cannot re-forward a suppressed window during replay')
  ctx.sim.isReplayActive = false
  ctx.update()
  assertEqual(ctx.windows[1].layer, 3,
    're-suppression keeps the redirect captured before replay')
  assertEqual(ctx.windows[1].layerDuplicate, true,
    're-suppression keeps the original duplicate mode')
end

local function testSavedReplaySuppressesForwardedApps()
  local ctx = loadRemote(false)
  ctx.sim.isReplayOnlyMode = true

  ctx.update()

  assertEqual(ctx.windows[1].layer, 0,
    'saved-replay mode suppresses forwarded OBS apps')
  assertEqual(ctx.windows[2].layer, 3,
    'saved-replay mode preserves the Live / Replay indicator')
end

local function testLeavingReplayRestoresOriginalObsRedirects()
  local ctx = loadRemote(true)
  ctx.update()

  ctx.sim.isReplayActive = false
  ctx.update()

  assertEqual(ctx.windows[1].layer, 3,
    'leaving replay restores the broadcast HUD layer')
  assertEqual(ctx.windows[1].layerDuplicate, true,
    'leaving replay restores duplicate forwarding')
  assertEqual(ctx.windows[3].layer, 2,
    'leaving replay restores every suppressed app layer')
  assertEqual(ctx.windows[3].layerDuplicate, false,
    'leaving replay preserves move-vs-duplicate mode')
end

local function testDisabledObsSuppressionLeavesForwardingAlone()
  local ctx = loadRemote(true, false)

  ctx.update()

  assertEqual(ctx.windows[1].layer, 3,
    'disabled replay suppression leaves the broadcast HUD forwarded')
  assertEqual(ctx.windows[3].layer, 2,
    'disabled replay suppression leaves every OBS app untouched')
end

local function testSettingsCheckboxDisablesAndRestoresSuppression()
  local ctx = loadRemote(true)
  ctx.update()

  ctx.clickObsSetting()
  ctx.update()

  assertEqual(ctx.windows[1].layer, 3,
    'disabling suppression in settings restores OBS forwarding immediately')
  assertEqual(ctx.settingsWrites[1].section, 'OBS_HUD',
    'the checkbox persists in the OBS HUD settings section')
  assertEqual(ctx.settingsWrites[1].key, 'HIDE_OTHERS_IN_REPLAY',
    'the checkbox persists the replay suppression setting')
  assertEqual(ctx.settingsWrites[1].value, false,
    'the checkbox saves the disabled value')
  assertEqual(ctx.settingsSaveCount(), 1,
    'changing replay suppression saves app settings')
end

local function testUnloadRestoresSuppressedObsWindows()
  local ctx = loadRemote(true)
  ctx.update()

  ctx.release()

  assertEqual(ctx.windows[1].layer, 3,
    'unloading the app restores the broadcast HUD redirect')
  assertEqual(ctx.windows[3].layer, 2,
    'unloading the app restores every suppressed redirect')
end

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
    connection_id = 'server-a',
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
    connection_id = 'server-a',
    target_driver = 4, target_camera = 1, target_car_camera = -1,
  })
  ctx.update()
  assertEqual(ctx.sim.focusedCar, 4, 'a valid camera command applies')

  ctx.sim.focusedCar = 2
  ctx.deliver({
    version = 1, type = 'command', command_seq = 1,
    connection_id = 'server-a',
    target_driver = 5, target_camera = 1, target_car_camera = -1,
  })
  ctx.update()
  assertEqual(ctx.sim.focusedCar, 2, 'a duplicate command sequence is ignored')

  ctx.deliver({
    version = 2, type = 'command', command_seq = 2,
    connection_id = 'server-a',
    target_driver = 6, target_camera = 1, target_car_camera = -1,
  })
  ctx.deliver({
    version = 1, type = 'command', command_seq = 3,
    connection_id = 'server-a',
    target_driver = 7,
  })
  ctx.update()
  assertEqual(ctx.sim.focusedCar, 2, 'unsupported and malformed commands are ignored')
end

local function testNewServerEpochAcceptsAResetSequence()
  local ctx = loadRemote(false)
  ctx.deliver({
    version = 1, connection_id = 'server-a', type = 'command', command_seq = 1,
    target_driver = 4, target_camera = 1, target_car_camera = -1,
  })
  ctx.update()
  assertEqual(ctx.sim.focusedCar, 4, 'the first server command applies')

  ctx.deliver({
    version = 1, connection_id = 'server-b', type = 'command', command_seq = 1,
    target_driver = 6, target_camera = 1, target_car_camera = -1,
  })
  ctx.update()
  assertEqual(ctx.sim.focusedCar, 6,
    'a replacement server can restart its sequence counter')
end

local function testEnterTogglesOnFirstStingerFrame()
  local ctx = loadRemote(false)
  requestReplay(ctx, 1)

  ctx.update()
  assertEqual(#ctx.toggleCalls, 1, 'replay enter toggles on the first stinger frame')
  assertEqual(ctx.toggleCalls[1].active, true, 'replay enter uses the enter toggle')
end

local function testLiveTogglesOnFirstStingerFrame()
  local ctx = loadRemote(true)
  requestReplay(ctx, 2)

  ctx.update()
  assertEqual(#ctx.toggleCalls, 1, 'go-live toggles on the first stinger frame')
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

local function testSavedReplaySeekAppliesEventShot()
  local ctx = loadRemote(false)
  ctx.sim.isReplayOnlyMode = true
  ctx.deliver({
    version = 1,
    connection_id = 'server-a',
    type = 'replay',
    replay_seq = ctx.nextReplaySeq(),
    replay_action = 3,
    replay_rewind_s = 0,
    replay_frame = 400,
    target_driver = 4,
    target_camera = 1,
    target_car_camera = -1,
  })

  ctx.update()
  assertEqual(ctx.seekCalls[1].frame, 400, 'a saved replay jump seeks to the event frame')
  assertEqual(ctx.sim.focusedCar, 4, 'a saved replay jump focuses the event driver')
  assertEqual(ctx.sim.cameraMode, 1, 'a saved replay jump selects the requested camera')
end

testReplaySuppressesForwardedAppsExceptIndicator()
testReplaySuppressesNewlyForwardedApps()
testReplayResuppressesAWindowWithoutLosingOriginalRedirect()
testSavedReplaySuppressesForwardedApps()
testLeavingReplayRestoresOriginalObsRedirects()
testDisabledObsSuppressionLeavesForwardingAlone()
testSettingsCheckboxDisablesAndRestoresSuppression()
testUnloadRestoresSuppressedObsWindows()
testEnterTogglesOnFirstStingerFrame()
testLiveTogglesOnFirstStingerFrame()
testSeekWithinReplayDoesNotRunAStinger()
testSavedReplaySeekAppliesEventShot()
testStingerRendersInScenePassForCleanOutput()
testWebSocketConnectsToLoopbackWithReconnect()
testTelemetryUsesVersionedProtocolAtTenHertz()
testCameraCommandsApplyOnceAndMalformedMessagesAreIgnored()
testNewServerEpochAcceptsAResetSequence()
print('test_remote_lua.lua: all tests passed')
