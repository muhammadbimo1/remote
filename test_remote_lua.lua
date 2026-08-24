local function assertEqual(actual, expected, message)
  if actual ~= expected then
    error(string.format('%s: expected %s, got %s', message, tostring(expected), tostring(actual)), 2)
  end
end

local function loadRemote(isReplayActive)
  local now = 0
  local telemetry = { cars = {} }
  local commands = {
    replay_seq = 0,
    replay_action = 0,
    replay_rewind_s = 0,
    replay_frame = 0,
    command_seq = 0,
    target_driver = -1,
    target_camera = 1,
    target_car_camera = -1,
  }
  local toggleCalls = {}
  local seekCalls = {}
  local renderCalls = {}
  local mmapCount = 0
  local sim = {
    isReplayActive = isReplayActive,
    isReplayOnlyMode = false,
    focusedCar = 0,
    cameraMode = 1,
    carCameraIndex = 0,
    replayFrames = 1000,
    replayFrameMs = 60,
    windowWidth = 1920,
    windowHeight = 1080,
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
  _G.ac = {
    CameraMode = {
      Track = 1, Cockpit = 2, Helicopter = 3, Car = 4, OnBoardFree = 5,
      Drivable = 6, Free = 7,
    },
    FolderID = { ReplaysTemp = 1 },
    INIConfig = {
      scriptSettings = function()
        return { get = function(_, _, _, fallback) return fallback end }
      end,
    },
    getSim = function() return sim end,
    writeMemoryMappedFile = function()
      mmapCount = mmapCount + 1
      return mmapCount == 1 and telemetry or commands
    end,
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
    commands = commands,
    sim = sim,
    toggleCalls = toggleCalls,
    seekCalls = seekCalls,
    renderCalls = renderCalls,
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
  ctx.commands.replay_seq = ctx.commands.replay_seq + 1
  ctx.commands.replay_action = action
  ctx.commands.replay_rewind_s = 12
  ctx.commands.target_driver = 4
  ctx.commands.target_camera = 1
  ctx.commands.target_car_camera = -1
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
print('test_remote_lua.lua: all tests passed')
