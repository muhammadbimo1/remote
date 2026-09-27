"""Create video clips from Broadcaster Remote event-journal records."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class ClipJob:
    start_s: float
    event_vod_s: float
    output_path: str


def parse_timecode(value: str) -> float:
    """Parse seconds, MM:SS, or HH:MM:SS into seconds."""
    text = str(value).strip()
    parts = text.split(':')
    if not text or len(parts) > 3:
        raise ValueError(f'invalid timecode: {value!r}')
    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise ValueError(f'invalid timecode: {value!r}') from exc
    if any(number < 0 for number in numbers):
        raise ValueError('timecode values must not be negative')
    if len(numbers) == 1:
        return numbers[0]
    if numbers[-1] >= 60:
        raise ValueError('seconds must be less than 60')
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    if numbers[-2] >= 60:
        raise ValueError('minutes must be less than 60')
    return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]


def read_journal(path: str) -> list[dict]:
    records = []
    with open(path, encoding='utf-8') as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f'{path}:{line_number}: invalid JSON: {exc.msg}') from exc
            if not isinstance(record, dict):
                raise ValueError(f'{path}:{line_number}: expected a JSON object')
            records.append(record)
    return records


def filter_events(events: list[dict], driver: str | None = None,
                  include_overtakes: bool = False) -> list[dict]:
    wanted_driver = driver.strip().casefold() if driver else None
    result = []
    for event in events:
        if not include_overtakes and event.get('kind') == 'overtake':
            continue
        if wanted_driver is not None and \
                str(event.get('driver', '')).strip().casefold() != wanted_driver:
            continue
        if not isinstance(event.get('session_s'), (int, float)):
            continue
        result.append(event)
    return result


def _format_timestamp(seconds: float) -> str:
    rounded = int(math.floor(seconds + 0.5))
    hours, remainder = divmod(rounded, 3600)
    minutes, secs = divmod(remainder, 60)
    return f'{hours:02d}-{minutes:02d}-{secs:02d}'


def _safe_filename_part(value: object) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', str(value))
    return re.sub(r'\s+', '_', text.strip()).strip('_') or 'unknown'


def make_clip_job(event: dict, index: int, vod_offset_s: float,
                  seek_ahead_s: float, output_dir: str) -> ClipJob:
    event_vod_s = vod_offset_s + float(event['session_s'])
    start_s = max(0.0, event_vod_s - seek_ahead_s)
    label = event.get('label') or str(event.get('kind', 'event')).upper()
    name = (f'{index:03d}_{_format_timestamp(event_vod_s)}_'
            f'{_safe_filename_part(label)}_'
            f'{_safe_filename_part(event.get("driver", "unknown"))}.mp4')
    return ClipJob(start_s=start_s, event_vod_s=event_vod_s,
                   output_path=os.path.join(output_dir, name))


def _number(value: float) -> str:
    return f'{value:.6f}'.rstrip('0').rstrip('.')


def build_ffmpeg_command(job: ClipJob, video: str, audio: str | None,
                         clip_length_s: float, encoder: str,
                         overwrite: bool) -> list[str]:
    command = ['ffmpeg', '-hide_banner', '-loglevel', 'error',
               '-y' if overwrite else '-n',
               '-ss', _number(job.start_s), '-i', video]
    if audio:
        command.extend(['-ss', _number(job.start_s), '-i', audio])
    command.extend(['-t', _number(clip_length_s), '-map', '0:v:0'])
    if audio:
        command.extend(['-map', '1:a:0'])
    command.extend(['-c:v', encoder])
    if encoder == 'h264_nvenc':
        command.extend(['-preset', 'p4', '-tune', 'hq', '-rc', 'vbr',
                        '-cq', '23', '-b:v', '5M', '-maxrate', '8M',
                        '-bufsize', '10M'])
    else:
        command.extend(['-preset', 'veryfast', '-crf', '20'])
    command.extend(['-pix_fmt', 'yuv420p'])
    if audio:
        command.extend(['-c:a', 'copy'])
    command.extend(['-movflags', '+faststart', '-shortest', job.output_path])
    return command


def validate_jobs_fit_source(jobs: list[ClipJob], clip_length_s: float,
                             source_duration_s: float) -> None:
    for job in jobs:
        if job.start_s + clip_length_s > source_duration_s + 0.1:
            raise ValueError(
                f'clip extends past the source duration: {job.output_path}')


def _media_duration(path: str) -> float:
    command = [
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', path,
    ]
    result = subprocess.run(command, check=False, capture_output=True,
                            text=True)
    if result.returncode:
        detail = result.stderr.strip() or f'exit code {result.returncode}'
        raise ValueError(f'could not read media duration for {path}: {detail}')
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise ValueError(f'could not read media duration for {path}') from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f'invalid media duration for {path}: {duration}')
    return duration


def _positive_timecode(value: str) -> float:
    try:
        result = parse_timecode(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if result <= 0:
        raise argparse.ArgumentTypeError('value must be greater than zero')
    return result


def _nonnegative_timecode(value: str) -> float:
    try:
        return parse_timecode(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Create clips from an event journal JSONL file.')
    parser.add_argument('--journal', required=True)
    parser.add_argument('--video', required=True)
    parser.add_argument('--audio')
    parser.add_argument('--clip-length', required=True,
                        type=_positive_timecode, metavar='TIME')
    parser.add_argument('--vod-offset', required=True,
                        type=_nonnegative_timecode, metavar='TIME')
    parser.add_argument('--event-seek-ahead', required=True,
                        type=_nonnegative_timecode, metavar='TIME')
    parser.add_argument('--driver', help='Exact driver field, case-insensitive')
    parser.add_argument('--output', default='clips')
    parser.add_argument('--include-overtakes', action='store_true')
    parser.add_argument('--video-encoder', choices=('h264_nvenc', 'libx264'),
                        default='h264_nvenc')
    parser.add_argument('--overwrite', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    for label, path in (('journal', args.journal), ('video', args.video),
                        ('audio', args.audio)):
        if path and not os.path.isfile(path):
            print(f'{label} file does not exist: {path}', file=sys.stderr)
            return 2

    records = read_journal(args.journal)
    events = filter_events(records, driver=args.driver,
                           include_overtakes=args.include_overtakes)
    if not events:
        print('No matching events found.', file=sys.stderr)
        return 1

    jobs = [make_clip_job(event, index, args.vod_offset,
                          args.event_seek_ahead, args.output)
            for index, event in enumerate(events, 1)]
    try:
        durations = [_media_duration(args.video)]
        if args.audio:
            durations.append(_media_duration(args.audio))
        validate_jobs_fit_source(jobs, args.clip_length, min(durations))
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not args.dry_run:
        os.makedirs(args.output, exist_ok=True)

    created = skipped = 0
    for index, job in enumerate(jobs, 1):
        if os.path.exists(job.output_path) and not args.overwrite:
            print(f'SKIP {index}/{len(jobs)} {job.output_path}')
            skipped += 1
            continue
        command = build_ffmpeg_command(
            job, args.video, args.audio, args.clip_length,
            args.video_encoder, overwrite=args.overwrite)
        if args.dry_run:
            print(shlex.join(command))
            continue
        print(f'CLIP {index}/{len(jobs)} {job.output_path}', flush=True)
        result = subprocess.run(command, check=False)
        if result.returncode:
            print(f'FFmpeg failed with exit code {result.returncode}: '
                  f'{job.output_path}', file=sys.stderr)
            return result.returncode
        created += 1

    if args.dry_run:
        print(f'Planned {len(jobs)} clip(s).', file=sys.stderr)
    else:
        print(f'Created {created} clip(s); skipped {skipped}.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
