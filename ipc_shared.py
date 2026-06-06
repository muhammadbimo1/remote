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
        ('cars', CarData * MAX_CARS),
    ]


class CommandPage(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ('packet_id', c_int32),
        ('target_driver', c_int32),
        ('target_camera', c_int32),
        ('target_car_camera', c_int32),
        ('command_seq', c_int32),           # increment = new command
    ]
