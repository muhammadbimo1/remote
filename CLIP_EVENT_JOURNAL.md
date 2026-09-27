# Event journal clip exporter

`clip_event_journal.py` creates fixed-length MP4 clips from a Broadcaster
Remote event-journal JSONL file. It reads each event's `session_s` value,
aligns it to the VOD, and invokes FFmpeg once per matching event.

## Requirements

- Python 3.10 or newer.
- `ffmpeg` and `ffprobe` available on `PATH`.
- A Broadcaster Remote event journal under `event_logs/`.
- A VOD video file. A separate audio file is optional.

The default video encoder is NVIDIA NVENC (`h264_nvenc`). Use
`--video-encoder libx264` on a system without a compatible NVIDIA GPU.

## Timestamp calculation

The VOD offset is the point in the VOD that corresponds to `session_s = 0`
in the selected journal:

```text
event position in VOD = VOD offset + session_s
clip start            = event position in VOD - event seek ahead
```

The clip start is clamped to zero. For example, with a VOD offset of `26:37`,
an event at `session_s = 102.6`, and an event seek-ahead value of `18`, the
event is at VOD `28:19.6` and the clip begins at `28:01.6`. In a 60-second
clip, the event therefore appears 18 seconds into the clip.

`--event-seek-ahead` means the amount of context to include *before* the
event. It does not move forward past the event.

Times can be written as seconds, `MM:SS`, or `HH:MM:SS`:

```text
18
26:37
1:02:03.5
```

Use the journal associated with the VOD segment being clipped. If Assetto
Corsa was restarted during a race, `session_s` restarts as well, so each
journal/VOD segment needs its own correct `--vod-offset`.

## Basic usage

```powershell
python clip_event_journal.py `
  --journal "event_logs\race-part-2.jsonl" `
  --video "vod\race-part-2.f299.mp4" `
  --audio "vod\race-part-2.f140.m4a" `
  --clip-length 60 `
  --vod-offset 26:37 `
  --event-seek-ahead 18 `
  --output "clips_part2"
```

This processes all drivers, excludes overtakes, and creates 60-second clips.
Omit `--audio` to create video-only clips.

## Clip one driver

Use the exact value of the journal's `driver` field. Matching is
case-insensitive, but it is not a partial-name search. Include the car number
when it is present in the journal.

```powershell
python clip_event_journal.py `
  --journal "event_logs\AC_190926-212612_R_abarth500_rt_sebring_raceday_endu_crc.jsonl" `
  --video "vod\ProjectNexus Croco Endurance Championship - Round 6 - 12H of Sebring  - Race - Part 2 [vLCP83hUaGc].f299.mp4" `
  --audio "vod\ProjectNexus Croco Endurance Championship - Round 6 - 12H of Sebring  - Race - Part 2 [vLCP83hUaGc].f140.m4a" `
  --clip-length 60 `
  --vod-offset 26:37 `
  --event-seek-ahead 18 `
  --driver "223 Yoga Widyatama" `
  --output "clips_yoga"
```

Only non-overtake events for `223 Yoga Widyatama` are selected.

## Preview before encoding

Add `--dry-run` to print the planned FFmpeg commands without creating the
output directory or clips:

```powershell
python clip_event_journal.py `
  --journal "event_logs\race-part-2.jsonl" `
  --video "vod\race-part-2.mp4" `
  --clip-length 60 `
  --vod-offset 26:37 `
  --event-seek-ahead 18 `
  --driver "223 Yoga Widyatama" `
  --output "clips_preview" `
  --dry-run
```

Existing output files are skipped even during a dry run. Use a new output
directory or add `--overwrite` when the preview should include every matching
event.

## Options

| Option | Required | Meaning |
| --- | --- | --- |
| `--journal PATH` | Yes | Event-journal JSONL file. |
| `--video PATH` | Yes | VOD video input. |
| `--audio PATH` | No | Separate VOD audio input. |
| `--clip-length TIME` | Yes | Duration of every output clip. Must be greater than zero. |
| `--vod-offset TIME` | Yes | VOD position corresponding to journal `session_s = 0`. |
| `--event-seek-ahead TIME` | Yes | Seconds of context placed before the event. |
| `--driver NAME` | No | Exact, case-insensitive match against the journal `driver` field. |
| `--output DIRECTORY` | No | Output directory. Defaults to `clips`. |
| `--include-overtakes` | No | Include events whose `kind` is `overtake`. Overtakes are excluded by default. |
| `--video-encoder ENCODER` | No | `h264_nvenc` (default) or `libx264`. |
| `--overwrite` | No | Replace existing clips instead of skipping them. |
| `--dry-run` | No | Print commands without encoding clips. |

Run `python clip_event_journal.py --help` for the command-line summary.

## Event selection

An event is eligible when:

- It has a numeric `session_s` value.
- It matches `--driver`, when that option is supplied.
- It is not an overtake, unless `--include-overtakes` is supplied.

Filtering happens before clips are numbered. The number at the start of a
filename is therefore its position within the filtered result, not its JSONL
line number.

## Output filenames

Filenames contain the filtered sequence number, rounded VOD event timestamp,
event label, and driver:

```text
001_00-28-20_HIT_223_Yoga_Widyatama.mp4
002_00-52-22_MRK_223_Yoga_Widyatama.mp4
```

Windows-invalid filename characters are removed automatically. Separate
events at the same timestamp remain distinct because each receives a unique
sequence number.

## Resume or replace a batch

By default, an output file that already exists is skipped. This makes it safe
to resume an interrupted batch:

```powershell
python clip_event_journal.py <options from the original run>
```

To recreate every matching clip, add:

```text
--overwrite
```

Deleting a reviewed clip and rerunning without `--overwrite` recreates that
missing clip while leaving existing files untouched.

## Troubleshooting

### No matching events found

Check that the driver value exactly matches the journal's `driver` field and
that the selected records contain `session_s`. Overtakes alone will not match
unless `--include-overtakes` is used.

### FFmpeg cannot initialize `h264_nvenc`

Use the CPU encoder:

```text
--video-encoder libx264
```

### A clip extends past the source duration

The script probes the video and optional audio before encoding. Correct the
VOD offset or reduce the clip length if a planned clip would extend past the
shorter source file.

### Audio and video are separate

Pass both `--video` and `--audio`. The script applies the same seek position
to both inputs and copies the AAC audio stream into each MP4.
