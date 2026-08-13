import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime

from event_journal import EventJournal, find_session_replay, parse_replay_name


def event(**kw):
    base = {
        'id': 1,
        'kind': 'collision',
        'label': 'HIT',
        'car_id': 7,
        'car_number': 23,
        'name': 'Alex Driver',
        't': 1754600000.5,
    }
    base.update(kw)
    return base


class EventJournalTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.journal = EventJournal(os.path.join(self.dir, 'event_logs'))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _lines(self, path):
        with open(path, encoding='utf-8') as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_directory_is_created_on_first_write(self):
        self.assertIsNone(self.journal.path)
        self.journal.append(event())
        self.assertTrue(os.path.isfile(self.journal.path))

    def test_record_carries_wall_clock_and_driver(self):
        self.journal.append(event())
        record = self._lines(self.journal.path)[0]

        self.assertEqual(record['kind'], 'collision')
        self.assertEqual(record['car_number'], 23)
        self.assertEqual(record['driver'], 'Alex Driver')
        self.assertEqual(record['t'], 1754600000.5)
        self.assertIn('iso', record)

    def test_replay_position_is_recorded_when_available(self):
        self.journal.append(event(), {
            'session_s': 1834.25,
            'replay_frame': 73370,
            'replay_frame_ms': 25.0,
            'position': 4,
            'lap': 12,
            'team': 'Vector Racing',
        })
        record = self._lines(self.journal.path)[0]

        self.assertEqual(record['session_s'], 1834.25)
        self.assertEqual(record['replay_frame'], 73370)
        self.assertEqual(record['position'], 4)
        self.assertEqual(record['lap'], 12)

    def test_missing_context_keys_are_omitted_not_nulled(self):
        self.journal.append(event(), {'session_s': None, 'position': 3})
        record = self._lines(self.journal.path)[0]

        self.assertNotIn('session_s', record)
        self.assertEqual(record['position'], 3)

    def test_events_append_to_one_file(self):
        self.journal.append(event(id=1))
        self.journal.append(event(id=2, kind='mark', label='MRK'))

        records = self._lines(self.journal.path)
        self.assertEqual([r['kind'] for r in records], ['collision', 'mark'])
        self.assertEqual(self.journal.count, 2)

    def test_session_label_goes_in_the_filename(self):
        self.journal.start_session('Qualify 2')
        self.journal.append(event())

        self.assertTrue(os.path.basename(self.journal.path).endswith('-qualify-2.jsonl'),
                        os.path.basename(self.journal.path))

    def test_label_is_slugged_for_the_filesystem(self):
        self.journal.start_session('Race / Session #1')
        self.journal.append(event())
        name = os.path.basename(self.journal.path)

        self.assertNotIn('/', name)
        self.assertNotIn('#', name)
        self.assertTrue(name.endswith('-race-session-1.jsonl'), name)

    def test_new_session_starts_a_new_file(self):
        self.journal.append(event())
        first = self.journal.path

        self.journal.start_session()
        self.assertIsNone(self.journal.path)
        self.assertEqual(self.journal.count, 0)

        # Clock moves on so the timestamped filename differs.
        self.journal._clock = lambda: 1754699999.0
        self.journal.append(event(id=9))

        self.assertNotEqual(self.journal.path, first)
        self.assertEqual(len(self._lines(first)), 1)
        self.assertEqual(len(self._lines(self.journal.path)), 1)

    def test_a_failed_write_does_not_raise(self):
        # Directory path occupied by a file: makedirs fails, append must not.
        blocked = os.path.join(self.dir, 'blocked')
        open(blocked, 'w').close()
        journal = EventJournal(blocked)

        self.assertIsNone(journal.append(event()))


class ReplayNameTest(unittest.TestCase):
    def test_parses_ac_autosave_names(self):
        started, letter = parse_replay_name(
            'AC_080826-230531_Q_btcc_alfa_romeo_giulietta_bop_ks_silverstone.acreplay')

        self.assertEqual(started, datetime(2026, 8, 8, 23, 5, 31))
        self.assertEqual(letter, 'Q')

    def test_hand_saved_replays_are_not_matched(self):
        cases = [
            'abarth500_fn_lemans_layout_24h_perf_070626-152305.acreplay',
            'my cool crash.acreplay',
            'AC_999999-999999_R_car_track.acreplay',
        ]
        for name in cases:
            with self.subTest(name=name):
                self.assertIsNone(parse_replay_name(name))


class FindSessionReplayTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _touch(self, name):
        open(os.path.join(self.dir, name), 'w').close()

    def _at(self, *args):
        return datetime(*args).timestamp()

    def test_picks_the_replay_started_with_the_session(self):
        self._touch('AC_080826-200132_O_abarth500_road_atlanta.acreplay')
        self._touch('AC_080826-201501_Q_abarth500_road_atlanta.acreplay')
        self._touch('AC_080826-212044_R_abarth500_road_atlanta.acreplay')

        found = find_session_replay(self.dir, self._at(2026, 8, 8, 20, 15, 3), 'Q')

        self.assertEqual(found, 'AC_080826-201501_Q_abarth500_road_atlanta.acreplay')

    def test_session_letter_disambiguates_back_to_back_sessions(self):
        self._touch('AC_080826-201501_Q_car_track.acreplay')
        self._touch('AC_080826-201502_R_car_track.acreplay')

        found = find_session_replay(self.dir, self._at(2026, 8, 8, 20, 15, 1), 'R')

        self.assertEqual(found, 'AC_080826-201502_R_car_track.acreplay')

    def test_nothing_close_enough_matches_nothing(self):
        self._touch('AC_080826-201501_Q_car_track.acreplay')

        # Hours away: better unmatched than pointing at the wrong session.
        self.assertIsNone(
            find_session_replay(self.dir, self._at(2026, 8, 8, 23, 30, 0), 'Q'))

    def test_missing_directory_is_not_an_error(self):
        self.assertIsNone(find_session_replay(os.path.join(self.dir, 'nope'),
                                              self._at(2026, 8, 8, 20, 15, 1), 'Q'))
        self.assertIsNone(find_session_replay('', 1.0, 'Q'))


class ReplayPairingTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.replays = os.path.join(self.dir, 'temp')
        os.makedirs(self.replays)
        self.replay = 'AC_080826-212044_R_abarth500_road_atlanta.acreplay'
        open(os.path.join(self.replays, self.replay), 'w').close()
        self.journal = EventJournal(os.path.join(self.dir, 'event_logs'))

    def test_journal_takes_the_replay_stem_as_its_name(self):
        self.journal.start_session('Race', replay_dir=self.replays,
                                   started_at=datetime(2026, 8, 8, 21, 20, 46).timestamp(),
                                   letter='R')
        self.journal.append(event())

        self.assertEqual(os.path.basename(self.journal.path),
                         os.path.splitext(self.replay)[0] + '.jsonl')

    def test_records_name_the_replay_they_annotate(self):
        self.journal.start_session('Race', replay_dir=self.replays,
                                   started_at=datetime(2026, 8, 8, 21, 20, 46).timestamp(),
                                   letter='R')
        self.journal.append(event())

        with open(self.journal.path, encoding='utf-8') as fh:
            record = json.loads(fh.readline())
        self.assertEqual(record['replay_file'], self.replay)

    def test_falls_back_to_the_label_when_no_replay_matches(self):
        self.journal.start_session('Race', replay_dir=self.replays,
                                   started_at=datetime(2026, 8, 8, 2, 0, 0).timestamp(),
                                   letter='R')
        self.journal.append(event())

        self.assertTrue(os.path.basename(self.journal.path).endswith('-race.jsonl'))
        self.assertIsNone(self.journal.replay_file)


if __name__ == '__main__':
    unittest.main()
