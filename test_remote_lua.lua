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
  local windowOpenCalls = {}
  local windowOpenState = { pip = false }
  local sceneReadyCallbacks = {}
  local geometryUpdates = {}
  local geometryDisposed = 0
  local drawnImages = {}
  local drawnTexts = {}
  local drawnRects = {}
  local dwriteTexts = {}
  local dwriteFonts = {}
  local tonemappingCalls = {}
  local geometryCreations = {}
  local sliderResults = {}
  local sliderCalls = {}
  local buttonClicks = {}
  local driverTeam = 'PRO 7 | Team'
  local cameraSections = {
    CAMERA_0 = {
      POSITION = { x = 1, y = 2, z = 3 },
      FORWARD = { x = 0, y = 0, z = 1 },
      UP = { x = 0, y = 1, z = 0 },
      FOV = 60,
    },
    CAMERA_1 = {
      POSITION = { x = 4, y = 5, z = 6 },
      FORWARD = { x = 1, y = 0, z = 0 },
      UP = { x = 0, y = 1, z = 0 },
      FOV = 72,
    },
  }
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
    isTimedRace = true,
    sessionTimeLeft = 3723000,
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
    bodyTransform = {
      transformPoint = function(_, value)
        return { x = value.x + 100, y = value.y + 200, z = value.z + 300 }
      end,
      transformVector = function(_, value)
        return { x = value.x, y = value.y, z = value.z }
      end,
    },
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
  _G.vec3 = function(x, y, z) return { x = x or 0, y = y or 0, z = z or 0 } end
  math.normalize = function(value) return value end
  _G.rgbm = setmetatable({ colors = { white = {} } }, {
    __call = function(_, r, g, b, a)
      return { r = r, g = g, b = b, a = a }
    end,
  })
  _G.ui = {
    Font = { Small = 1 },
    StyleVar = { Alpha = 1 },
    WindowFlags = { None = 0 },
    ImageFit = { Stretch = 0 },
    windowSize = function() return { x = 1920, y = 1080 } end,
    availableSpace = function() return { x = 480, y = 270 } end,
    getCursor = function() return { x = 0, y = 0 } end,
    setCursor = function() end,
    drawRectFilled = function(p1, p2, color)
      drawnRects[#drawnRects + 1] = { p1 = p1, p2 = p2, color = color }
    end,
    drawImage = function(image, p1, p2, color)
      drawnImages[#drawnImages + 1] = {
        image = image, p1 = p1, p2 = p2, color = color,
      }
    end,
    text = function(value) drawnTexts[#drawnTexts + 1] = value end,
    pushDWriteFont = function(font)
      dwriteFonts[#dwriteFonts + 1] = font
    end,
    popDWriteFont = function() end,
    measureDWriteText = function(value, size)
      return { x = #value * size * 0.6, y = size }
    end,
    dwriteText = function(value, size, color)
      dwriteTexts[#dwriteTexts + 1] = {
        value = value, size = size, color = color,
      }
    end,
    beginTonemapping = function() end,
    endTonemapping = function(gamma, whitePoint, lcsAware)
      tonemappingCalls[#tonemappingCalls + 1] = {
        gamma = gamma, whitePoint = whitePoint, lcsAware = lcsAware,
      }
    end,
    slider = function(label, value, minimum, maximum, format, power)
      sliderCalls[#sliderCalls + 1] = {
        label = label, value = value, minimum = minimum, maximum = maximum,
        format = format, power = power,
      }
      local result = sliderResults[label]
      sliderResults[label] = nil
      if result ~= nil then return result.value, result.changed end
      return value, false
    end,
    button = function(label)
      local clicked = buttonClicks[label] == true
      buttonClicks[label] = nil
      return clicked
    end,
    checkbox = function()
      local clicked = clickCheckbox
      clickCheckbox = false
      return clicked
    end,
  }
  _G.render = {
    BlendMode = { AlphaBlend = 1 },
    DepthMode = { Off = 2 },
    AntialiasingMode = { FXAA = 3 },
    TextureFormat = {
      R8G8B8A8 = { UNorm = 4 },
      R32G32B32A32 = { Float = 6 },
    },
    ShadersType = { Main = 5 },
    onSceneReady = function(callback)
      sceneReadyCallbacks[#sceneReadyCallbacks + 1] = callback
    end,
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
      carData = function()
        return {
          get = function(_, section, key, fallback)
            local values = cameraSections[section]
            if values == nil or values[key] == nil then return fallback end
            return values[key]
          end,
        }
      end,
    },
    GeometryShot = function(scene, resolution, mips, withDepth,
        antialiasing, textureFormat, flags)
      geometryCreations[#geometryCreations + 1] = {
        scene = scene, resolution = resolution, mips = mips,
        withDepth = withDepth, antialiasing = antialiasing,
        textureFormat = textureFormat, flags = flags,
      }
      local shot = {}
      local function chain() return shot end
      shot.setName = chain
      shot.setSky = chain
      shot.setOriginalLighting = chain
      shot.setShadersType = chain
      shot.setFakeCarShadows = chain
      shot.setMaxLayer = chain
      shot.setOpaqueAlphaFix = chain
      shot.setTransparentPass = chain
      shot.update = function(_, pos, look, up, fov)
        geometryUpdates[#geometryUpdates + 1] = {
          pos = pos, look = look, up = up, fov = fov,
        }
      end
      shot.dispose = function() geometryDisposed = geometryDisposed + 1 end
      return shot
    end,
    findNodes = function() return {} end,
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
    getSession = function()
      return { durationMinutes = 90 }
    end,
    getCar = function() return car end,
    getSessionName = function() return 'Race' end,
    getReplayFilename = function() return '' end,
    getFolder = function() return 'C:/AC/replay/temp' end,
    getServerIP = function() return '' end,
    getServerPortHTTP = function() return -1 end,
    getDriverTeam = function() return driverTeam end,
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
    setWindowOpen = function(id, opened)
      windowOpenCalls[#windowOpenCalls + 1] = { id = id, opened = opened }
      windowOpenState[id] = opened
    end,
    isWindowOpen = function(id) return windowOpenState[id] == true end,
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
    windowOpenCalls = windowOpenCalls,
    geometryUpdates = geometryUpdates,
    drawnImages = drawnImages,
    drawnTexts = drawnTexts,
    drawnRects = drawnRects,
    dwriteTexts = dwriteTexts,
    dwriteFonts = dwriteFonts,
    tonemappingCalls = tonemappingCalls,
    geometryCreations = geometryCreations,
    sliderCalls = sliderCalls,
    car = car,
    setDriverTeam = function(value) driverTeam = value end,
    setSlider = function(label, value)
      sliderResults[label] = { value = value, changed = true }
    end,
    clickButton = function(label) buttonClicks[label] = true end,
    runSceneReady = function()
      for _, callback in ipairs(sceneReadyCallbacks) do callback() end
    end,
    setCameraSections = function(value) cameraSections = value end,
    geometryDisposed = function() return geometryDisposed end,
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
  assertEqual(ctx.renderCalls[1].textures.txStinger, 'stinger.png',
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
  assertEqual(payload.cars[1].car_cameras_count, 3,
    'telemetry includes each drivers onboard camera count')
  assertEqual(payload.is_timed_session, true, 'telemetry identifies timed sessions')
  assertEqual(payload.session_time_left, 3723000, 'telemetry includes remaining session milliseconds')
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

local function pipCommand(connectionID, sequence, enabled, driver, camera)
  return {
    version = 1,
    connection_id = connectionID,
    type = 'pip',
    pip_seq = sequence,
    pip_enabled = enabled,
    target_driver = driver,
    target_car_camera = camera,
  }
end

local function testPiPCommandDoesNotChangeMainShot()
  local ctx = loadRemote(false)
  ctx.sim.focusedCar = 2
  ctx.sim.cameraMode = ac.CameraMode.Track
  ctx.sim.carCameraIndex = 2

  ctx.deliver(pipCommand('server-a', 1, true, 4, 1))
  ctx.update()

  assertEqual(ctx.sim.focusedCar, 2, 'PiP keeps main focus')
  assertEqual(ctx.sim.cameraMode, ac.CameraMode.Track, 'PiP keeps main camera')
  assertEqual(ctx.sim.carCameraIndex, 2, 'PiP keeps main onboard camera')
  assertEqual(#ctx.windowOpenCalls, 1, 'PiP updates only its own window once')
  assertEqual(ctx.windowOpenCalls[1].id, 'pip', 'PiP targets its own window')
  assertEqual(ctx.windowOpenCalls[1].opened, true, 'enabled PiP opens its window')
end

local function testDuplicateAndMalformedPiPCommandsAreIgnored()
  local ctx = loadRemote(false)
  ctx.deliver(pipCommand('server-a', 1, true, 0, 0))
  ctx.update()
  ctx.deliver(pipCommand('server-a', 1, false, 0, 0))
  ctx.update()
  ctx.deliver(pipCommand('server-a', 2, 1, 0, 0))
  ctx.update()
  ctx.deliver({
    version = 1, connection_id = 'server-a', type = 'pip', pip_seq = 3,
    pip_enabled = false, target_driver = 0,
  })
  ctx.update()

  assertEqual(#ctx.windowOpenCalls, 1,
    'duplicate and malformed PiP commands do not mutate window state')
end

local function testPiPDisableAndNewServerEpochAreAccepted()
  local ctx = loadRemote(false)
  ctx.deliver(pipCommand('server-a', 1, true, 0, 0))
  ctx.update()
  ctx.deliver(pipCommand('server-b', 1, false, 0, 0))
  ctx.update()

  assertEqual(#ctx.windowOpenCalls, 2,
    'a replacement server can restart the PiP sequence counter')
  assertEqual(ctx.windowOpenCalls[2].opened, false,
    'disabled PiP closes its window')
end

local function testDisabledPiPDoesNotRender()
  local ctx = loadRemote(false)
  ctx.runSceneReady()
  assertEqual(#ctx.geometryUpdates, 0,
    'disabled PiP skips its scene render')
end

local function testEnabledPiPRendersSelectedOnboardCamera()
  local ctx = loadRemote(false)
  ctx.sim.focusedCar = 2
  ctx.sim.cameraMode = ac.CameraMode.Track
  ctx.deliver(pipCommand('server-a', 1, true, 0, 1))
  ctx.update()
  ctx.runSceneReady()

  local update = ctx.geometryUpdates[1]
  assertEqual(update.fov, 72, 'selected cameras.ini FOV is used')
  assertEqual(update.pos.x, 104,
    'camera position is transformed by the target car')
  assertEqual(ctx.sim.focusedCar, 2, 'PiP render keeps main focus')
  assertEqual(ctx.sim.cameraMode, ac.CameraMode.Track,
    'PiP render keeps main camera')
end

local function testMissingRequestedPiPCameraFallsBackToCameraZero()
  local ctx = loadRemote(false)
  ctx.deliver(pipCommand('server-a', 1, true, 0, 2))
  ctx.update()
  ctx.runSceneReady()

  assertEqual(ctx.geometryUpdates[1].fov, 60,
    'missing requested camera falls back to camera zero')
end

local function testUnavailablePiPCameraDoesNotDrawAStaleFrame()
  local ctx = loadRemote(false)
  ctx.setCameraSections({})
  ctx.deliver(pipCommand('server-a', 1, true, 0, 0))
  ctx.update()
  ctx.runSceneReady()
  script.windowPiP(0)

  assertEqual(#ctx.geometryUpdates, 0,
    'missing camera data does not update the geometry shot')
  assertEqual(#ctx.drawnImages, 0,
    'missing camera data does not draw a stale image')
  assertEqual(ctx.drawnTexts[#ctx.drawnTexts], 'PiP unavailable',
    'missing camera data reports an unavailable view')
end

local function testPiPGeometryShotIsDisposedOnRelease()
  local ctx = loadRemote(false)
  ctx.deliver(pipCommand('server-a', 1, true, 0, 0))
  ctx.update()
  ctx.runSceneReady()
  ctx.release()
  assertEqual(ctx.geometryDisposed(), 1,
    'PiP releases its GPU texture when the app unloads')
end

local function testPiPOverlayUsesPositionParsedNameTeamAndMichroma()
  local ctx = loadRemote(false)
  ctx.car.racePosition = 6
  ctx.car.driverName = function() return ' [GT3] [VIP] Alex Driver ' end
  ctx.setDriverTeam('Apex Racing')
  ctx.deliver(pipCommand('server-a', 1, true, 0, 0))
  ctx.update()
  ctx.runSceneReady()
  script.windowPiP(0)

  assertEqual(ctx.dwriteFonts[#ctx.dwriteFonts], 'Michroma-Regular.ttf',
    'PiP identity plate uses Michroma Regular')
  assertEqual(ctx.dwriteTexts[1].value, 'P6 | Alex Driver',
    'PiP first line combines position with a tag-free full name')
  assertEqual(ctx.dwriteTexts[2].value, 'Apex Racing',
    'PiP second line shows the complete team name')
  local plate = ctx.drawnRects[#ctx.drawnRects]
  local image = ctx.drawnImages[1]
  assertEqual(plate.p2.y, image.p1.y,
    'PiP identity plate sits directly above the onboard image')
  assertEqual(plate.color.r, 0, 'PiP identity plate background is black')
  assertEqual(plate.color.g, 0, 'PiP identity plate background is black')
  assertEqual(plate.color.b, 0, 'PiP identity plate background is black')
  assertEqual(plate.color.a, 1, 'PiP identity plate background is opaque')
end

local function preparePiPFrame(ctx)
  ctx.deliver(pipCommand('server-a', 1, true, 0, 0))
  ctx.update()
  ctx.runSceneReady()
  script.windowPiP(0)
end

local function testPiPUsesHdrTextureAndTonemappingDefaults()
  local ctx = loadRemote(false)
  preparePiPFrame(ctx)

  assertEqual(ctx.geometryCreations[1].textureFormat, 6,
    'PiP preserves HDR highlights in a floating-point render target')
  local tint = ctx.drawnImages[1].color
  assertEqual(tint.r, 1, 'PiP default red gain is neutral')
  assertEqual(tint.g, 1, 'PiP default green gain is neutral')
  assertEqual(tint.b, 1, 'PiP default blue gain is neutral')
  assertEqual(tint.a, 1, 'PiP image multiplier is neutral')
  local tone = ctx.tonemappingCalls[1]
  assertEqual(tone.gamma, 1, 'PiP default gamma is neutral')
  assertEqual(tone.whitePoint, 0,
    'PiP uses the CMRT white point that disables extra tonemapping')
  assertEqual(tone.lcsAware, true,
    'PiP converts AC HDR colors to LDR like CMRT')
end

local function testPiPColorSettingsApplyLiveAndPersist()
  local ctx = loadRemote(false)
  ctx.setSlider('Gamma', 1.2)
  ctx.setSlider('White point', 0.8)
  ctx.setSlider('Red gain', 0.9)
  ctx.setSlider('Green gain', 0.85)
  ctx.setSlider('Blue gain', 0.75)
  script.windowPiPSettings(0)
  preparePiPFrame(ctx)

  local tint = ctx.drawnImages[1].color
  assertEqual(tint.r, 0.9, 'PiP applies configured red gain')
  assertEqual(tint.g, 0.85, 'PiP applies configured green gain')
  assertEqual(tint.b, 0.75, 'PiP applies configured blue gain')
  assertEqual(tint.a, 1, 'PiP does not apply a brightness multiplier')
  local tone = ctx.tonemappingCalls[1]
  assertEqual(tone.gamma, 1.2, 'PiP applies configured gamma')
  assertEqual(tone.whitePoint, 0.8, 'PiP applies configured white point')
  assertEqual(ctx.settingsSaveCount(), 5,
    'each changed PiP control is persisted')
end

local function testPiPColorResetRestoresDefaults()
  local ctx = loadRemote(false)
  ctx.setSlider('White point', 0.8)
  script.windowPiPSettings(0)
  ctx.clickButton('Reset color')
  script.windowPiPSettings(0)
  preparePiPFrame(ctx)

  local tint = ctx.drawnImages[1].color
  assertEqual(tint.r, 1, 'PiP reset restores red gain')
  assertEqual(tint.g, 1, 'PiP reset restores green gain')
  assertEqual(tint.b, 1, 'PiP reset restores blue gain')
  assertEqual(tint.a, 1, 'PiP reset keeps the image multiplier neutral')
  assertEqual(ctx.tonemappingCalls[1].gamma, 1,
    'PiP reset restores gamma')
  assertEqual(ctx.tonemappingCalls[1].whitePoint, 0,
    'PiP reset restores white point')
end

local function testPiPColorSlidersUseFractionalMode()
  local ctx = loadRemote(false)
  script.windowPiPSettings(0)

  assertEqual(#ctx.sliderCalls, 5, 'PiP settings exposes all color sliders')
  local callsByLabel = {}
  for _, call in ipairs(ctx.sliderCalls) do
    callsByLabel[call.label] = call
    assertEqual(call.format, '%.2f',
      call.label .. ' displays fractional precision')
    assertEqual(call.power, nil,
      call.label .. ' does not enable CSP integer slider mode')
  end
  assertEqual(callsByLabel.Gamma.maximum, 5.0,
    'PiP gamma can be raised to 5')
  assertEqual(callsByLabel['White point'].maximum, 25.0,
    'PiP preserves the configured white point range')
  assertEqual(callsByLabel.Brightness, nil,
    'PiP settings omits the brightness multiplier')
  assertEqual(callsByLabel['Exposure (EV)'], nil,
    'PiP settings omits the exposure multiplier')
end

local function testEnterTogglesOnFirstStingerFrame()
  local ctx = loadRemote(false)
  requestReplay(ctx, 1)

  ctx.update()
  assertEqual(#ctx.toggleCalls, 1, 'replay enter toggles on the first stinger frame')
  assertEqual(ctx.toggleCalls[1].active, true, 'replay enter uses the enter toggle')
end

local function testLiveWaitsUntilScreenIsCovered()
  local ctx = loadRemote(true)
  requestReplay(ctx, 2)

  ctx.update()
  assertEqual(#ctx.toggleCalls, 0, 'go-live must not toggle before the wipe covers the game')

  ctx.setTime(0.299)
  ctx.update()
  assertEqual(#ctx.toggleCalls, 0, 'go-live must remain queued during the cover phase')

  ctx.setTime(0.300)
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
testLiveWaitsUntilScreenIsCovered()
testSeekWithinReplayDoesNotRunAStinger()
testSavedReplaySeekAppliesEventShot()
testStingerRendersInScenePassForCleanOutput()
testWebSocketConnectsToLoopbackWithReconnect()
testTelemetryUsesVersionedProtocolAtTenHertz()
testCameraCommandsApplyOnceAndMalformedMessagesAreIgnored()
testNewServerEpochAcceptsAResetSequence()
testPiPCommandDoesNotChangeMainShot()
testDuplicateAndMalformedPiPCommandsAreIgnored()
testPiPDisableAndNewServerEpochAreAccepted()
testDisabledPiPDoesNotRender()
testEnabledPiPRendersSelectedOnboardCamera()
testMissingRequestedPiPCameraFallsBackToCameraZero()
testUnavailablePiPCameraDoesNotDrawAStaleFrame()
testPiPGeometryShotIsDisposedOnRelease()
testPiPOverlayUsesPositionParsedNameTeamAndMichroma()
testPiPUsesHdrTextureAndTonemappingDefaults()
testPiPColorSettingsApplyLiveAndPersist()
testPiPColorResetRestoresDefaults()
testPiPColorSlidersUseFractionalMode()
print('test_remote_lua.lua: all tests passed')
