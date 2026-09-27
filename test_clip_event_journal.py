import os
import tempfile
import unittest

from clip_event_journal import (build_ffmpeg_command, filter_events,
                                make_clip_job, parse_timecode, read_journal,
                                validate_jobs_fit_source)


class ClipEventJournalTest(unittest.TestCase):
    def test_read_journal_reads_json_objects_in_order(self):
        with tempfile.NamedTemporaryFile('w', encoding='utf-8',
                                         delete=False) as stream:
            stream.write('{"kind":"collision","session_s":2}\n')
            stream.write('\n')
            stream.write('{"kind":"mark","session_s":3}\n')
            path = stream.name
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

        self.assertEqual(read_journal(path), [
            {'kind': 'collision', 'session_s': 2},
            {'kind': 'mark', 'session_s': 3},
        ])

    def test_parse_timecode_accepts_seconds_minutes_and_hours(self):
        self.assertEqual(parse_timecode('18'), 18.0)
        self.assertEqual(parse_timecode('26:37'), 1597.0)
        self.assertEqual(parse_timecode('1:02:03.5'), 3723.5)

    def test_parse_timecode_rejects_invalid_minutes(self):
        with self.assertRaisesRegex(ValueError, 'minutes'):
            parse_timecode('1:60:00')

    def test_filter_events_excludes_overtakes_and_matches_driver_exactly(self):
        events = [
            {'kind': 'collision', 'driver': '223 Yoga Widyatama',
             'session_s': 102.6},
            {'kind': 'overtake', 'driver': '223 Yoga Widyatama',
             'session_s': 110.0},
            {'kind': 'collision', 'driver': '223 Yoga',
             'session_s': 120.0},
            {'kind': 'mark', 'driver': '223 YOGA WIDYATAMA',
             'session_s': 130.0},
        ]

        result = filter_events(events, driver='223 yoga widyatama')

        self.assertEqual(result, [events[0], events[3]])

    def test_filter_events_can_include_overtakes(self):
        event = {'kind': 'overtake', 'driver': 'Driver', 'session_s': 10}

        self.assertEqual(filter_events([event], include_overtakes=True),
                         [event])

    def test_make_clip_job_applies_offset_and_lead_in(self):
        event = {
            'kind': 'collision',
            'label': 'HIT',
            'driver': '223 Yoga Widyatama',
            'session_s': 102.6,
        }

        job = make_clip_job(event, index=1, vod_offset_s=1597,
                            seek_ahead_s=18, output_dir='clips')

        self.assertAlmostEqual(job.start_s, 1681.6)
        self.assertAlmostEqual(job.event_vod_s, 1699.6)
        self.assertEqual(
            job.output_path,
            os.path.join('clips',
                         '001_00-28-20_HIT_223_Yoga_Widyatama.mp4'))

    def test_make_clip_job_clamps_start_to_zero_and_sanitizes_filename(self):
        event = {'kind': 'mark', 'label': 'MRK',
                 'driver': 'Driver: One/Two', 'session_s': 2}

        job = make_clip_job(event, index=7, vod_offset_s=0,
                            seek_ahead_s=18, output_dir='clips')

        self.assertEqual(job.start_s, 0)
        self.assertTrue(job.output_path.endswith(
            '007_00-00-02_MRK_Driver_OneTwo.mp4'))

    def test_build_ffmpeg_command_seeks_video_and_audio_together(self):
        event = {'kind': 'collision', 'label': 'HIT',
                 'driver': 'Driver', 'session_s': 30}
        job = make_clip_job(event, index=1, vod_offset_s=10,
                            seek_ahead_s=5, output_dir='clips')

        command = build_ffmpeg_command(
            job, video='video.mp4', audio='audio.m4a', clip_length_s=60,
            encoder='h264_nvenc', overwrite=True)

        self.assertEqual(command.count('-ss'), 2)
        self.assertEqual(command[command.index('-ss') + 1], '35')
        second_seek = command.index('-ss', command.index('-ss') + 1)
        self.assertEqual(command[second_seek + 1], '35')
        self.assertIn('video.mp4', command)
        self.assertIn('audio.m4a', command)
        self.assertIn('h264_nvenc', command)
        self.assertIn('-y', command)

    def test_build_ffmpeg_command_supports_video_without_audio(self):
        event = {'kind': 'mark', 'label': 'MRK', 'driver': 'Driver',
                 'session_s': 30}
        job = make_clip_job(event, index=1, vod_offset_s=0,
                            seek_ahead_s=0, output_dir='clips')

        command = build_ffmpeg_command(
            job, video='video.mp4', audio=None, clip_length_s=45,
            encoder='libx264', overwrite=False)

        self.assertEqual(command.count('-i'), 1)
        self.assertNotIn('1:a:0', command)
        self.assertIn('libx264', command)
        self.assertIn('-n', command)

    def test_validate_jobs_rejects_a_clip_past_the_source_end(self):
        event = {'kind': 'collision', 'label': 'HIT', 'driver': 'Driver',
                 'session_s': 95}
        job = make_clip_job(event, index=1, vod_offset_s=0,
                            seek_ahead_s=5, output_dir='clips')

        with self.assertRaisesRegex(ValueError, 'past the source duration'):
            validate_jobs_fit_source([job], clip_length_s=60,
                                     source_duration_s=120)


if __name__ == '__main__':
    unittest.main()
