import json
import threading
import time
import unittest

from ac_ipc import ACIPCTransport, MAX_CARS, ProtocolError, TelemetrySnapshot


def car_message(car_id=0):
    return {
        'car_id': car_id,
        'session_id': 18,
        'position': car_id + 1,
        'normalized_spline_pos': 0.5,
        'speed_kmh': 120.0,
        'lap_time': 50000,
        'best_lap': 100000,
        'last_lap': 101000,
        'lap_count': 3,
        'is_in_pit': False,
        'is_connected': True,
        'is_colliding': False,
        'is_rolled_over': False,
        'driver_name': 'Alex Driver',
        'team_name': 'PRO 7 | Team',
    }


def telemetry_message(cars=None, **overrides):
    cars = list(cars or [])
    message = {
        'version': 1,
        'type': 'telemetry',
        'packet_id': 7,
        'car_count': len(cars),
        'focused_car': 0,
        'current_camera': 1,
        'car_cameras_count': 3,
        'current_car_camera': 0,
        'track_length': 5000.0,
        'track_name': 'Silverstone Grand Prix',
        'session_type': 1,
        'session_index': 0,
        'session_type_raw': 3,
        'session_gen': 1,
        'session_name': 'Race',
        'is_replay': False,
        'replay_frame': 0,
        'replay_frames': 1000,
        'replay_frame_ms': 60.0,
        'replay_last_result': 0,
        'is_replay_only': False,
        'replay_file': '',
        'replay_temp_dir': 'C:/AC/replay/temp',
        'timetable_url': '',
        'cars': cars,
    }
    message.update(overrides)
    return message


class FakeSocket(object):
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []
        self.closed = False

    def send(self, data):
        if self.fail:
            raise OSError('closed')
        self.sent.append(json.loads(data))

    def close(self):
        self.closed = True


class TelemetrySnapshotTest(unittest.TestCase):
    def test_valid_message_becomes_attribute_snapshot(self):
        snapshot = TelemetrySnapshot.from_message(
            telemetry_message([car_message()]))

        self.assertEqual(snapshot.packet_id, 7)
        self.assertEqual(snapshot.car_count, 1)
        self.assertEqual(snapshot.track_name, 'Silverstone Grand Prix')
        self.assertEqual(snapshot.cars[0].driver_name, 'Alex Driver')
        self.assertTrue(snapshot.cars[0].is_connected)

    def test_wrong_version_is_rejected(self):
        with self.assertRaises(ProtocolError):
            TelemetrySnapshot.from_message(telemetry_message(version=2))

    def test_declared_car_count_must_match_array(self):
        with self.assertRaises(ProtocolError):
            TelemetrySnapshot.from_message(
                telemetry_message([car_message()], car_count=2))

    def test_more_than_max_cars_is_rejected(self):
        cars = [car_message(i) for i in range(MAX_CARS + 1)]
        with self.assertRaises(ProtocolError):
            TelemetrySnapshot.from_message(telemetry_message(cars))

    def test_bool_is_not_accepted_for_integer_field(self):
        with self.assertRaises(ProtocolError):
            TelemetrySnapshot.from_message(telemetry_message(packet_id=True))

    def test_missing_car_field_is_rejected(self):
        car = car_message()
        del car['session_id']
        with self.assertRaises(ProtocolError):
            TelemetrySnapshot.from_message(telemetry_message([car]))

    def test_published_snapshots_cannot_be_mutated(self):
        snapshot = TelemetrySnapshot.from_message(
            telemetry_message([car_message()]))

        with self.assertRaises(AttributeError):
            snapshot.focused_car = 7
        with self.assertRaises(AttributeError):
            snapshot.cars[0].position = 9


class ACIPCTransportTest(unittest.TestCase):
    def test_ingest_replaces_latest_snapshot_atomically(self):
        now = [10.0]
        transport = ACIPCTransport(clock=lambda: now[0])
        socket = FakeSocket()
        transport.attach(socket)

        self.assertTrue(transport.ingest(json.dumps(telemetry_message())))
        self.assertEqual(transport.latest().packet_id, 7)
        self.assertTrue(transport.is_connected())

    def test_invalid_message_does_not_replace_latest_snapshot(self):
        transport = ACIPCTransport()
        transport.attach(FakeSocket())
        transport.ingest(json.dumps(telemetry_message(packet_id=7)))

        with self.assertRaises(ProtocolError):
            transport.ingest(json.dumps(telemetry_message(
                packet_id=8, car_count=1)))

        self.assertEqual(transport.latest().packet_id, 7)

    def test_telemetry_becomes_stale_after_two_seconds(self):
        now = [10.0]
        transport = ACIPCTransport(clock=lambda: now[0], stale_after=2.0)
        transport.attach(FakeSocket())
        transport.ingest(json.dumps(telemetry_message()))

        now[0] = 12.01

        self.assertFalse(transport.is_connected())
        self.assertIsNone(transport.latest())

    def test_new_socket_replaces_and_closes_old_socket(self):
        transport = ACIPCTransport()
        old_socket = FakeSocket()
        new_socket = FakeSocket()
        transport.attach(old_socket)

        transport.attach(new_socket)

        self.assertTrue(old_socket.closed)
        self.assertFalse(new_socket.closed)

    def test_each_attached_socket_starts_a_new_connection_generation(self):
        transport = ACIPCTransport()
        self.assertEqual(transport.connection_generation(), 0)
        transport.attach(FakeSocket())
        self.assertEqual(transport.connection_generation(), 1)
        transport.attach(FakeSocket())
        self.assertEqual(transport.connection_generation(), 2)

    def test_latest_snapshot_and_generation_are_read_as_one_state(self):
        transport = ACIPCTransport()
        first_socket = FakeSocket()
        transport.attach(first_socket)
        transport.ingest(json.dumps(telemetry_message(packet_id=7)),
                         source=first_socket)

        snapshot, generation = transport.latest_with_generation()
        self.assertEqual(snapshot.packet_id, 7)
        self.assertEqual(generation, 1)

        transport.attach(FakeSocket())
        snapshot, generation = transport.latest_with_generation()
        self.assertIsNone(snapshot)
        self.assertEqual(generation, 2)

    def test_detaching_displaced_socket_does_not_clear_new_socket(self):
        transport = ACIPCTransport()
        old_socket = FakeSocket()
        new_socket = FakeSocket()
        transport.attach(old_socket)
        transport.attach(new_socket)

        transport.detach(old_socket)
        self.assertTrue(transport.send_command(0, 1, -1))
        self.assertEqual(new_socket.sent[-1]['type'], 'command')

    def test_command_from_superseded_telemetry_generation_is_rejected(self):
        transport = ACIPCTransport()
        old_socket = FakeSocket()
        new_socket = FakeSocket()
        transport.attach(old_socket)
        observed_generation = transport.connection_generation()
        transport.attach(new_socket)

        self.assertFalse(transport.send_command(
            0, 1, -1, expected_generation=observed_generation))
        self.assertEqual(new_socket.sent, [])

    def test_generation_guard_rejects_superseded_tick(self):
        transport = ACIPCTransport()
        transport.attach(FakeSocket())
        observed_generation = transport.connection_generation()
        transport.attach(FakeSocket())

        with transport.generation_guard(observed_generation) as current:
            self.assertFalse(current)

    def test_generation_guard_serializes_socket_replacement(self):
        transport = ACIPCTransport()
        first_socket = FakeSocket()
        transport.attach(first_socket)
        transport.ingest(json.dumps(telemetry_message()), source=first_socket)
        generation = transport.connection_generation()
        replacement_started = threading.Event()
        replacement_finished = threading.Event()

        def replace_socket():
            replacement_started.set()
            transport.attach(FakeSocket())
            replacement_finished.set()

        with transport.generation_guard(generation) as current:
            self.assertTrue(current)
            thread = threading.Thread(target=replace_socket)
            thread.start()
            self.assertTrue(replacement_started.wait(1.0))
            self.assertFalse(replacement_finished.wait(0.05))

        thread.join(1.0)
        self.assertTrue(replacement_finished.is_set())

    def test_replay_send_does_not_increment_camera_sequence(self):
        transport = ACIPCTransport()
        socket = FakeSocket()
        transport.attach(socket)

        self.assertTrue(transport.send_command(3, 1, -1))
        camera_seq = socket.sent[-1]['command_seq']
        self.assertTrue(transport.send_replay_command(1, 12.5, 0, 3, 1, -1))

        self.assertEqual(socket.sent[-1]['type'], 'replay')
        self.assertNotIn('command_seq', socket.sent[-1])
        self.assertEqual(camera_seq, 1)
        self.assertEqual(socket.sent[-1]['replay_seq'], 1)

    def test_command_and_replay_payloads_are_versioned(self):
        transport = ACIPCTransport(connection_id='server-a')
        socket = FakeSocket()
        transport.attach(socket)

        transport.send_command(4, 4, 2)
        transport.send_replay_command(3, frame=200)

        self.assertEqual(socket.sent[0], {
            'version': 1, 'type': 'command', 'command_seq': 1,
            'connection_id': 'server-a',
            'target_driver': 4, 'target_camera': 4,
            'target_car_camera': 2,
        })
        self.assertEqual(socket.sent[1], {
            'version': 1, 'type': 'replay', 'replay_seq': 1,
            'connection_id': 'server-a',
            'replay_action': 3, 'replay_rewind_s': 0.0,
            'replay_frame': 200, 'target_driver': -1,
            'target_camera': -1, 'target_car_camera': -1,
        })

    def test_failed_send_disconnects_only_the_failed_socket(self):
        transport = ACIPCTransport()
        socket = FakeSocket(fail=True)
        transport.attach(socket)

        self.assertFalse(transport.send_command(0, 1, -1))

        self.assertTrue(socket.closed)
        self.assertFalse(transport.has_socket())

    def test_concurrent_commands_are_sent_in_sequence_order(self):
        class GateInt(object):
            def __init__(self, entered, release):
                self.entered = entered
                self.release = release

            def __int__(self):
                self.entered.set()
                self.release.wait(2)
                return 1

        entered = threading.Event()
        release = threading.Event()
        transport = ACIPCTransport()
        socket = FakeSocket()
        transport.attach(socket)
        first = threading.Thread(target=transport.send_command,
                                 args=(GateInt(entered, release), 1, -1))
        second = threading.Thread(target=transport.send_command,
                                  args=(2, 1, -1))

        first.start()
        self.assertTrue(entered.wait(1))
        second.start()
        time.sleep(0.02)
        release.set()
        first.join(2)
        second.join(2)

        self.assertEqual([m['command_seq'] for m in socket.sent], [1, 2])
        self.assertEqual([m['target_driver'] for m in socket.sent], [1, 2])


if __name__ == '__main__':
    unittest.main()
