"""
Shared ctypes struct definitions for mmap-based IPC between
the AC plugin and the web server. Python 3.3 compatible.
"""
import ctypes
from ctypes import c_int32, c_float, c_wchar

MAX_CARS = 128
TELEMETRY_TAG = "broadcaster_remote_telemetry"
COMMAND_TAG = "broadcaster_remote_commands"


class CarData(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ('car_id', c_int32),
        ('session_id', c_int32),            # online server entry-list slot
        ('position', c_int32),              # realtime leaderboard pos (1-based)
        ('normalized_spline_pos', c_float),  # 0.0-1.0 track position
        ('speed_kmh', c_float),
        ('lap_time', c_int32),              # current lap ms
        ('best_lap', c_int32),              # best lap ms
        ('last_lap', c_int32),              # last lap ms
        ('lap_count', c_int32),
        ('is_in_pit', c_int32),
        ('is_connected', c_int32),
        ('is_colliding', c_int32),
        ('is_rolled_over', c_int32),
        ('driver_name', c_wchar * 64),
        ('team_name', c_wchar * 64),
    ]


class TelemetryPage(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ('packet_id', c_int32),             # sequence counter (torn-read detection)
        ('car_count', c_int32),
        ('focused_car', c_int32),
        ('current_camera', c_int32),
        ('car_cameras_count', c_int32),
        ('current_car_camera', c_int32),
        ('track_length', c_float),
        ('session_type', c_int32),          # 1 = race, 0 = other (practice/qualify/hotlap)
        ('session_index', c_int32),         # sim.currentSessionIndex (0-based)
        ('session_type_raw', c_int32),      # ac.SessionType: 1 P, 2 Q, 3 R, ...
        ('session_gen', c_int32),           # bumped on every session start/restart
        ('session_name', c_wchar * 64),     # ac.getSessionName(), e.g. "Race"
        ('is_replay', c_int32),             # sim.isReplayActive
        ('replay_frame', c_int32),          # sim.replayCurrentFrame
        ('replay_frames', c_int32),         # sim.replayFrames
        ('replay_frame_ms', c_float),       # sim.replayFrameMs
        ('replay_last_result', c_int32),    # 0 = untried, 1 = accepted, 2 = refused
        ('replay_temp_dir', c_wchar * 256),  # ac.getFolder(ac.FolderID.ReplaysTemp)
        ('timetable_url', c_wchar * 128),
        ('cars', CarData * MAX_CARS),
    ]


class CommandPage(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ('packet_id', c_int32),
        ('target_driver', c_int32),
        ('target_camera', c_int32),
        ('target_car_camera', c_int32),
        ('command_seq', c_int32),           # increment = new camera/focus command
        ('replay_seq', c_int32),            # increment = new replay command
        ('replay_action', c_int32),         # see REPLAY_* below
        ('replay_rewind_s', c_float),       # seconds to rewind for REPLAY_ENTER
        ('replay_frame', c_int32),          # target frame for REPLAY_SEEK_FRAME
    ]


# Replay command actions (CommandPage.replay_action)
REPLAY_NONE = 0
REPLAY_ENTER = 1        # start instant replay, rewound replay_rewind_s seconds
REPLAY_LIVE = 2         # stop replay, return to live
REPLAY_SEEK_FRAME = 3   # jump to replay_frame (wired, no UI yet)
