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
  local mediaPlayers = {}
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
  local clippedTexts = {}
  local dwriteTexts = {}
  local dwriteFonts = {}
  local tonemappingCalls = {}
  local geometryCreations = {}
  local sliderResults = {}
  local sliderCalls = {}
  local buttonClicks = {}
  local driverTeam = 'PRO 7 | Team'
  local carsByIndex = {}
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
    timeToSessionStart = -6000,
    windowWidth = 1920,
    windowHeight = 1080,
  }
  local car = {
    sessionID = 18,
    racePosition = 1,
    splinePosition = 0.5,
    speedKmh = 120,
    acceleration = { x = 0, y = 1, z = 0 },
    collisionDepth = 0,
    position = { x = 0, y = 0, z = 0 },
    velocity = { x = 0, y = 0, z = 0 },
    resetCounter = 0,
    lapTimeMs = 50000,
    bestLapTimeMs = 100000,
    previousLapTimeMs = 101000,
    lapCount = 3,
    isInPitlane = false,
    isInPit = false,
    wheelsOutside = 0,
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
    StyleVar = { Alpha = 1, ItemSpacing = 2 },
    WindowFlags = { None = 0 },
    ImageFit = { Stretch = 0 },
    windowSize = function() return { x = 1920, y = 1080 } end,
    availableSpace = function() return { x = 480, y = 270 } end,
    getCursor = function() return { x = 0, y = 0 } end,
    setCursor = function() end,
    pushFont = function() end,
    popFont = function() end,
    pushStyleVar = function() end,
    popStyleVar = function() end,
    measureText = function(value)
      return { x = #value * 7, y = 16 }
    end,
    beginChild = function() end,
    endChild = function() end,
    availableSpaceX = function() return 480 end,
    dummy = function() end,
    invisibleButton = function() return false end,
    drawRectFilled = function(p1, p2, color)
      drawnRects[#drawnRects + 1] = { p1 = p1, p2 = p2, color = color }
    end,
    drawTextClipped = function(text, posMin, posMax, color)
      clippedTexts[#clippedTexts + 1] = {
        text = text, posMin = posMin, posMax = posMax, color = color,
      }
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
  _G.ui.MediaPlayer = function(source, audioParams)
    local player = {
      source = source,
      audioParams = audioParams,
      ready = false,
      playing = false,
      endedFlag = false,
      time = 0,
      durationS = 2.0,
    }
    function player:setSource(value) self.source = value; return self end
    function player:setMuted() return self end
    function player:setVolume() return self end
    function player:setLooping() return self end
    function player:setAutoPlay() return self end
    function player:setGenerateMips() return self end
    function player:setUpdatePeriod() return self end
    function player:linkToSimulationSpeed() return self end
    function player:play() self.playing = true; self.endedFlag = false; return self end
    function player:pause() self.playing = false; return self end
    function player:setCurrentTime(value) self.time = value; return self end
    function player:currentTime() return self.time end
    function player:duration() return self.durationS end
    function player:playing() return self.playing end
    function player:ended() return self.endedFlag end
    function player:hasVideo() return self.ready end
    mediaPlayers[#mediaPlayers + 1] = player
    return player
  end
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
    DrivableCamera = { Chase = 0, Bumper = 3 },
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
    getCar = function(i) return carsByIndex[i or 0] or car end,
    getSessionName = function() return 'Race' end,
    getTrackName = function() return 'Test Track' end,
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
    setCurrentDrivableCamera = function(camera) sim.driveableCameraMode = camera end,
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
    mediaPlayers = mediaPlayers,
    stingerPlayer = function() return mediaPlayers[1] end,
    setStingerVideoReady = function(value)
      if mediaPlayers[1] ~= nil then mediaPlayers[1].ready = value end
    end,
    setStingerVideoTime = function(value)
      if mediaPlayers[1] ~= nil then mediaPlayers[1].time = value end
    end,
    setStingerVideoEnded = function(value)
      if mediaPlayers[1] ~= nil then mediaPlayers[1].endedFlag = value end
    end,
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
    clippedTexts = clippedTexts,
    dwriteTexts = dwriteTexts,
    dwriteFonts = dwriteFonts,
    tonemappingCalls = tonemappingCalls,
    geometryCreations = geometryCreations,
    sliderCalls = sliderCalls,
    car = car,
    setCar = function(index, overrides)
      local copy = {}
      for key, value in pairs(car) do copy[key] = value end
      for key, value in pairs(overrides or {}) do copy[key] = value end
      carsByIndex[index] = copy
      return copy
    end,
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
    'the scene pass falls back to the png strip without a decoded video')
  assertEqual(ctx.renderCalls[1].values.gUseGreenKey, 0,
    'the png fallback does not apply the green key')
  assertEqual(ctx.renderCalls[1].values.gOffsetX, -2,
    'the stinger starts just off the left edge')
  assertEqual(ctx.renderCalls[1].values.gEmissive, 3,
    'the scene pass supplies HDR luminance for exposure-resistant color')
  assertEqual(ctx.renderCalls[1].depthMode, 2,
    'the stinger ignores scene depth')

  ctx.setTime(0.300)
  ctx.update()
  renderStinger()
  assertEqual(ctx.renderCalls[2].values.gOffsetX, -0.5,
    'the two-screen strip fully covers the game at the replay toggle')
end

local function testStingerUsesGreenScreenVideoWhenAvailable()
  local ctx = loadRemote(true)
  ctx.setStingerVideoReady(true)
  requestReplay(ctx, 2)

  ctx.update()
  assertEqual(#ctx.toggleCalls, 0, 'video stinger waits for its cover cue')
  assertEqual(ctx.stingerPlayer().playing, true,
    'video starts as soon as the stinger is queued')

  ctx.setStingerVideoTime(0.5)
  ctx.update()
  assertEqual(#ctx.toggleCalls, 1, 'video stinger toggles at the cover cue')
  assertEqual(ctx.toggleCalls[1].active, false, 'video go-live exits replay')
  assertEqual(ctx.stingerPlayer().playing, false,
    'video holds the covered frame while replay settles')

  renderStinger()
  local pass = ctx.renderCalls[#ctx.renderCalls]
  assertEqual(pass.textures.txStinger, ctx.stingerPlayer(),
    'the scene pass samples the media player')
  assertEqual(pass.values.gUseGreenKey, 1, 'the video path keys the green screen')
  assertEqual(pass.values.gEmissive, 1,
    'video frames are display-referred without the png HDR boost')
  assertEqual(pass.values.gOffsetX, 0, 'the clip supplies its own wipe')

  ctx.sim.isReplayActive = false
  ctx.setStingerVideoTime(0.6)
  ctx.update()
  assertEqual(ctx.stingerPlayer().playing, true,
    'video resumes for the reveal once replay settles')

  ctx.setStingerVideoTime(2.0)
  ctx.setStingerVideoEnded(true)
  ctx.update()
  assertEqual(ctx.stingerPlayer().time, 0, 'the video rewinds for the next stinger')
  local callsBefore = #ctx.renderCalls
  renderStinger()
  assertEqual(#ctx.renderCalls, callsBefore,
    'the stinger stops rendering after the reveal')
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

local function testBumperCameraSelectionAndTelemetry()
  local ctx = loadRemote(false)
  ctx.deliver({
    version = 1, type = 'command', command_seq = 1,
    connection_id = 'server-a',
    target_driver = 4, target_camera = 6, target_car_camera = -1,
  })
  ctx.update(0.1)
  assertEqual(ctx.sim.cameraMode, ac.CameraMode.Drivable, 'bumper uses drivable mode')
  assertEqual(ctx.sim.driveableCameraMode, ac.DrivableCamera.Bumper,
    'bumper selects the explicit sub-camera')
  assertEqual(ctx.telemetryPayloads[1].current_camera, 6, 'telemetry identifies bumper')

  ctx.deliver({
    version = 1, type = 'command', command_seq = 2,
    connection_id = 'server-a',
    target_driver = 5, target_camera = 6, target_car_camera = -1,
  })
  ctx.update(0.1)
  assertEqual(ctx.sim.focusedCar, 5, 'bumper supports changing driver')
  assertEqual(ctx.telemetryPayloads[2].current_camera, 6, 'driver picks preserve bumper')

  ctx.sim.driveableCameraMode = ac.DrivableCamera.Chase
  ctx.update(0.1)
  assertEqual(ctx.telemetryPayloads[3].current_camera, 2,
    'other drivable views retain their existing mapping')
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

local function testReplayTemporarilyClosesAndRestoresPiP()
  local ctx = loadRemote(false)
  ctx.deliver(pipCommand('server-a', 1, true, 0, 0))
  ctx.update()

  ctx.sim.isReplayActive = true
  ctx.update()
  assertEqual(ctx.windowOpenCalls[#ctx.windowOpenCalls].opened, false,
    'entering replay closes the PiP window')

  ctx.sim.isReplayActive = false
  ctx.update()
  assertEqual(ctx.windowOpenCalls[#ctx.windowOpenCalls].opened, true,
    'returning live restores an enabled PiP window')
end

local function testPiPDisabledDuringReplayStaysOffWhenLive()
  local ctx = loadRemote(false)
  ctx.deliver(pipCommand('server-a', 1, true, 0, 0))
  ctx.update()
  ctx.sim.isReplayActive = true
  ctx.update()

  ctx.deliver(pipCommand('server-a', 2, false, 0, 0))
  ctx.update()
  local callsBeforeLive = #ctx.windowOpenCalls

  ctx.sim.isReplayActive = false
  ctx.update()
  assertEqual(#ctx.windowOpenCalls, callsBeforeLive,
    'returning live does not restore PiP after it was disabled in replay')
  assertEqual(ctx.windowOpenCalls[#ctx.windowOpenCalls].opened, false,
    'PiP remains closed after returning live')
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

local function addSecondCar(ctx, z)
  return ctx.setCar(1, {
    racePosition = 2,
    position = { x = 0, y = 0, z = z or 3 },
    velocity = { x = 0, y = 0, z = 0 },
  })
end

local function testHitFromCollisionDepthFlagsLocalCar()
  local cases = {
    { depth = 0.01, want = true },
    { depth = 0, want = false },
    { depth = 0.01, pit = true, want = false },
    { depth = 0.01, pitlane = true, want = false },
    { depth = 0.01, connected = false, want = false },
    { depth = 0.01, replay = true, want = false },
    { depth = 0.01, replayOnly = true, want = false },
  }
  for _, case in ipairs(cases) do
    local ctx = loadRemote(case.replay == true)
    ctx.car.collisionDepth = case.depth
    ctx.car.isInPit = case.pit == true
    ctx.car.isInPitlane = case.pitlane == true
    ctx.car.isConnected = case.connected ~= false
    ctx.sim.isReplayOnlyMode = case.replayOnly == true
    ctx.update(0.1)
    assertEqual(ctx.telemetryPayloads[1].cars[1].is_colliding, case.want,
      'contact depth still flags the local car and respects live-session gates')
  end
end

local function testHitLatchesBriefContactUntilTelemetry()
  local ctx = loadRemote(false)
  ctx.car.collisionDepth = 0.1
  ctx.update(0.01)             -- first frame: depth latches, no telemetry yet
  ctx.car.collisionDepth = 0
  ctx.setTime(0.1)
  ctx.update(0.095)            -- the 10 Hz tick must still carry the contact
  assertEqual(ctx.telemetryPayloads[1].cars[1].is_colliding, true,
    'a one-frame contact survives the telemetry gap')
  ctx.setTime(1.0)
  ctx.update(0.1)
  assertEqual(ctx.telemetryPayloads[2].cars[1].is_colliding, false,
    'the latch clears once its window has passed')
end

local function testHitImpulseFlagsOnlyTheHarderCar()
  local ctx = loadRemote(false)
  ctx.sim.carsCount = 2
  ctx.car.position = { x = 0, y = 0, z = 0 }
  ctx.car.velocity = { x = 0, y = 0, z = 0 }
  local other = addSecondCar(ctx, 3)
  ctx.update(0.05)             -- baselines for every car
  ctx.setTime(0.05)
  ctx.car.velocity = { x = 0, y = 0, z = 8.5 }   -- residual ~139 m/s^2
  other.velocity = { x = 0, y = 0, z = 8.0 }     -- residual ~131 m/s^2
  ctx.update(0.05)             -- detect and reach the 10 Hz telemetry tick
  local cars = ctx.telemetryPayloads[1].cars
  assertEqual(cars[1].is_colliding, true,
    'the harder impulse owns the HIT')
  assertEqual(cars[2].is_colliding, false,
    'the partner does not log a second event for the same contact')
end

local function testHitPartnerImpulseIsNotASecondEvent()
  local ctx = loadRemote(false)
  ctx.sim.carsCount = 2
  ctx.car.position = { x = 0, y = 0, z = 0 }
  ctx.car.velocity = { x = 0, y = 0, z = 0 }
  local other = addSecondCar(ctx, 3)
  ctx.update(0.05)
  ctx.setTime(0.05)
  ctx.car.velocity = { x = 0, y = 0, z = 9 }
  ctx.update(0.05)
  assertEqual(ctx.telemetryPayloads[1].cars[1].is_colliding, true,
    'the first car owns the event')
  ctx.setTime(0.2)
  other.velocity = { x = 0, y = 0, z = 9 }       -- replicated partner impulse
  ctx.update(0.1)
  assertEqual(ctx.telemetryPayloads[2].cars[2].is_colliding, false,
    'the partner impulse inside the pair cooldown is swallowed')
end

local function testHitIgnoresLandingBrakingTeleportAndReset()
  local ctx = loadRemote(false)
  ctx.update(0.05)                                -- baselines
  ctx.setTime(0.05)
  ctx.car.velocity.y = 9                          -- landing, purely vertical
  ctx.update(0.05)
  assertEqual(ctx.telemetryPayloads[1].cars[1].is_colliding, false,
    'a vertical landing is not a hit')

  local braking = loadRemote(false)
  braking.car.velocity = { x = 0, y = 0, z = 50 }
  braking.update(0.05)
  for step = 1, 12 do
    braking.setTime(step * 0.05)
    braking.car.velocity.z = 50 - step * 0.5      -- ~1 g, tracked by the EMA
    braking.update(0.05)
  end
  for _, payload in ipairs(braking.telemetryPayloads) do
    assertEqual(payload.cars[1].is_colliding, false,
      'sustained braking is not a contact')
  end

  local snap = loadRemote(false)
  snap.update(0.05)
  snap.setTime(0.05)
  snap.car.position = { x = 500, y = 0, z = 0 }   -- network snap
  snap.car.velocity = { x = 0, y = 0, z = 9 }
  snap.update(0.05)
  assertEqual(snap.telemetryPayloads[1].cars[1].is_colliding, false,
    'a position snap does not read as a contact')
  snap.setTime(0.1)
  snap.car.resetCounter = 1
  snap.car.velocity = { x = 0, y = 0, z = 9 }
  snap.update(0.1)
  assertEqual(snap.telemetryPayloads[2].cars[1].is_colliding, false,
    'a car reset does not read as a contact')
end

local function drawnBadgeLabels(ctx, from)
  local labels = {}
  for i = (from or 0) + 1, #ctx.clippedTexts do
    labels[ctx.clippedTexts[i].text] = true
  end
  return labels
end

local function testDriverListShowsHitBadgeAfterContact()
  local ctx = loadRemote(false)
  ctx.car.collisionDepth = 0.1
  ctx.update(0.1)             -- latch contact and build the driver rows
  ctx.car.collisionDepth = 0
  ctx.update(0.1)             -- contact ends; the notice window keeps HIT lit
  script.windowMain(0)

  local labels = drawnBadgeLabels(ctx)
  assertEqual(labels['HIT'], true,
    'the car keeps a HIT badge after contact ends')
  assertEqual(labels['FLP'], nil,
    'a car that did not roll over shows no FLP badge')
end

local function testDriverListShowsFlipBadgeForRollover()
  local ctx = loadRemote(false)
  ctx.car.up.y = 0.4
  ctx.update(0.1)             -- rollover hold tick 1
  ctx.update(0.1)             -- rollover hold tick 2 -> flagged
  script.windowMain(0)

  local labels = drawnBadgeLabels(ctx)
  assertEqual(labels['FLP'], true, 'a rolled-over driver shows a FLP badge')
  assertEqual(labels['HIT'], nil,
    'a car that did not contact anything shows no HIT badge')
end

local function testDriverListShowsImpulseDebug()
  local ctx = loadRemote(false)
  ctx.car.position = { x = 0, y = 0, z = 0 }
  ctx.car.velocity = { x = 0, y = 0, z = 0 }
  ctx.update(0.05)            -- baselines
  ctx.setTime(0.05)
  ctx.car.velocity = { x = 0, y = 0, z = 8 }
  ctx.update(0.05)            -- residual ~131, telemetry tick, driver rows
  script.windowMain(0)

  local labels = drawnBadgeLabels(ctx)
  assertEqual(labels['131'], true,
    'the debug column shows the live impulse that fired the hit')

  ctx.setTime(0.1)
  ctx.update(0.1)             -- impulse ends, the peak stays readable
  local before = #ctx.clippedTexts
  script.windowMain(0)
  labels = drawnBadgeLabels(ctx, before)
  assertEqual(labels['131'], nil, 'the live impulse clears when motion settles')
  assertEqual(labels['(131)'], true,
    'the peak holds an impulse the 10 Hz redraw could have missed')
end

testHitFromCollisionDepthFlagsLocalCar()
testHitLatchesBriefContactUntilTelemetry()
testHitImpulseFlagsOnlyTheHarderCar()
testHitPartnerImpulseIsNotASecondEvent()
testHitIgnoresLandingBrakingTeleportAndReset()
testDriverListShowsHitBadgeAfterContact()
testDriverListShowsFlipBadgeForRollover()
testDriverListShowsImpulseDebug()

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
testStingerUsesGreenScreenVideoWhenAvailable()
testWebSocketConnectsToLoopbackWithReconnect()
testTelemetryUsesVersionedProtocolAtTenHertz()
testBumperCameraSelectionAndTelemetry()
testCameraCommandsApplyOnceAndMalformedMessagesAreIgnored()
testNewServerEpochAcceptsAResetSequence()
testPiPCommandDoesNotChangeMainShot()
testDuplicateAndMalformedPiPCommandsAreIgnored()
testPiPDisableAndNewServerEpochAreAccepted()
testReplayTemporarilyClosesAndRestoresPiP()
testPiPDisabledDuringReplayStaysOffWhenLive()
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
