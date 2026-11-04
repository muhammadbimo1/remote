"""
Append-only on-disk record of race incidents, one JSON object per line.

Separate from EventLog on purpose. EventLog is a live window: it prunes to
whatever AC still has in its instant-replay buffer, because an event it can no
longer rewind into is a dead button. This journal is the opposite — nothing is
ever pruned, because its job is to annotate the .acreplay that gets saved at
the end of the session, long after the buffer has rolled over.

Each record carries three independent ways to find the moment again:

  * `iso` / `t`      — wall clock, matches OBS recordings and chat logs
  * `session_s`      — seconds of recorded replay at that instant, which is the
                       scrub position in a replay saved from the session start
  * `replay_frame`   — AC's own frame counter at that instant

Frame and session offsets are only as good as AC's counters: if the instant
replay buffer wrapped mid-session, the saved replay starts later than frame 0
and both shift by the truncation. Wall clock never shifts, so it is written
first and always.

AC records one .acreplay per session under replay/temp, created at session
start and grown live, named `AC_DDMMYY-HHMMSS_<S>_<car>_<track>.acreplay` with
S being O (practice/other), Q or R. The temp filename timestamp is the session
start, so a journal can initially take the same stem. When AC saves the replay
out of temp it gives the copy a new timestamp for the save time; review pairs
that name against journal end time and replay frame count.
"""
import json
import os
import re
import struct
import threading
import time
import unicodedata
from datetime import datetime


# AC replay filename: AC_DDMMYY-HHMMSS_<S>_<car>_<track>.acreplay
REPLAY_NAME_RE = re.compile(
    r'^AC_(?P<date>\d{6})-(?P<time>\d{6})_(?P<letter>[A-Z])_', re.IGNORECASE)

# How far the parsed replay start may sit from the session start we observed.
# Generous, because the server may be started mid-session and can then only
# estimate the start from AC's recorded frame count.
REPLAY_MATCH_TOLERANCE_S = 300.0

# How far a loaded replay's duration may sit from a journal's recorded length
# before the fingerprint match is refused. Used when a replay file was renamed
# after being saved, so its stem no longer matches the journal's.
REPLAY_FINGERPRINT_TOLERANCE_S = 5.0

# AC renames an autosaved replay when it is moved out of `replay/temp`: the
# timestamp in the saved filename is the save time, not the temp recording's
# start time. Event journals can also be split across server reconnects. A
# saved replay is therefore allowed to end a few minutes after its last event,
# and adjacent journal fragments can be joined across a short logging gap.
REPLAY_TIMELINE_TOLERANCE_S = 300.0
REPLAY_FRAGMENT_GAP_S = 600.0


def read_replay_header(path):
    """Read the small fixed prefix needed to understand replay wrapping.

    AC stores a circular recording cursor immediately before the total frame
    count. Reading only the prefix keeps this safe for multi-gigabyte replay
    files. Returns None for an unavailable or unrecognised file.
    """
    try:
        with open(path, 'rb') as fh:
            version, frame_ms = struct.unpack('<Id', fh.read(12))
            for _ in range(3):
                length = struct.unpack('<I', fh.read(4))[0]
                if length > 4096:
                    return None
                if len(fh.read(length)) != length:
                    return None
            cars, recording_index, frames, _ = struct.unpack(
                '<IIII', fh.read(16))
    except (OSError, struct.error):
        return None
    if version <= 0 or frame_ms <= 0 or cars > 256 or frames <= 0 or \
            recording_index > frames:
        return None
    return {
        'version': version,
        'frame_ms': frame_ms,
        'recording_index': recording_index,
        'frames': frames,
    }


def remap_replay_records(records, replay_frames, replay_frame_ms,
                         recording_index):
    """Map live journal frame numbers into a loaded replay's final buffer.

    Before AC's circular buffer fills, journal frame numbers are absolute.
    Once full, `sim.replayFrames` stays pinned at the capacity. For those later
    records the race countdown supplies the elapsed frames from the final
    unsaturated record. Records overwritten from the front of the ring, or
    beyond the saved live edge, are omitted.
    """
    if replay_frames <= 0 or replay_frame_ms <= 0:
        return [dict(record) for record in records]
    wrapped = 0 <= recording_index < replay_frames
    anchor = None
    if wrapped:
        for record in records:
            frame = record.get('replay_frame')
            if frame is not None and frame < replay_frames and \
                    record.get('time_remaining_ms') is not None:
                anchor = record

    mapped = []
    for record in records:
        frame = record.get('replay_frame')
        if frame is None and record.get('session_s') is not None:
            frame = int(round(
                record['session_s'] * 1000.0 / replay_frame_ms))
        if frame is None:
            continue

        absolute_frame = frame
        if wrapped and frame >= replay_frames:
            if anchor is None or record.get('time_remaining_ms') is None:
                continue
            elapsed_frames = (
                anchor['time_remaining_ms'] - record['time_remaining_ms']) / \
                replay_frame_ms
            absolute_frame = anchor['replay_frame'] + int(round(elapsed_frames))

        target_frame = absolute_frame - recording_index if wrapped \
            else absolute_frame
        if target_frame < 0 or target_frame >= replay_frames:
            continue
        output = dict(record)
        output['replay_frame'] = target_frame
        output['session_s'] = round(
            target_frame * replay_frame_ms / 1000.0, 2)
        mapped.append(output)
    return mapped


def parse_replay_name(name):
    """Return (filename_datetime, session_letter) for an AC replay filename.

    Returns None for anything that is not AC's autosave naming — replays saved
    by hand get arbitrary names and must not be matched by guesswork. For a
    temp replay the timestamp is its start; for a saved copy it is its save
    time.
    """
    match = REPLAY_NAME_RE.match(name)
    if not match:
        return None
    try:
        started = datetime.strptime(
            match.group('date') + match.group('time'), '%d%m%y%H%M%S')
    except ValueError:
        return None
    return started, match.group('letter').upper()


def find_session_replay(directory, started_at, letter=None,
                        tolerance=REPLAY_MATCH_TOLERANCE_S):
    """Find the .acreplay AC is recording for a session that began at `started_at`.

    Matches on the session start encoded in the filename, not on file mtime:
    the file is still being written, so its mtime tracks now, not the session.
    Returns a filename, or None when nothing is close enough — an unmatched
    journal is better than one pointing at the wrong session.
    """
    if not directory or not os.path.isdir(directory):
        return None
    try:
        names = os.listdir(directory)
    except OSError:
        return None

    best = None
    best_delta = None
    for name in names:
        if not name.lower().endswith('.acreplay'):
            continue
        parsed = parse_replay_name(name)
        if not parsed:
            continue
        started, name_letter = parsed
        if letter and name_letter != letter.upper():
            continue
        delta = abs(started.timestamp() - started_at)
        if delta > tolerance:
            continue
        if best_delta is None or delta < best_delta:
            best, best_delta = name, delta
    return best


def match_replay(replay_file, replay_frames, replay_frame_ms, sessions):
    """Pick the journal that annotates a loaded replay file.

    `replay_file` is ac.getReplayFilename() (a full path, empty when no saved
    replay is loaded). Primary match is exact, on the filename stem: a journal
    names itself after the .acreplay it annotates, and each record repeats the
    original replay name in case the file was renamed on its way out of temp.

    AC gives an autosaved replay a new timestamp when it is saved out of the
    temp directory. For that case, adjacent journal fragments are grouped and
    matched against the save time and loaded frame count. A duration
    fingerprint remains as the fallback for hand-renamed files. Returns a
    (session, confidence) tuple, or (None, None).
    """
    if not replay_file or not sessions:
        return None, None
    stem = os.path.splitext(os.path.basename(replay_file))[0]
    if not stem:
        return None, None

    for session in sessions:
        if os.path.splitext(session['file'])[0] == stem:
            return session, 'exact'
        if session.get('replay_file') and \
                os.path.splitext(session['replay_file'])[0] == stem:
            return session, 'exact'

    parsed_name = parse_replay_name(os.path.basename(replay_file))
    if parsed_name and replay_frames and replay_frame_ms:
        saved_at = parsed_name[0].timestamp()
        candidates = []
        for group in _group_session_fragments(sessions):
            end_delta = saved_at - group['last_t']
            frame_gap = replay_frames - group['max_replay_frame']
            frame_gap_s = frame_gap * replay_frame_ms / 1000.0
            if end_delta < -30.0 or end_delta > REPLAY_TIMELINE_TOLERANCE_S:
                continue
            if frame_gap < 0 or frame_gap_s > REPLAY_TIMELINE_TOLERANCE_S:
                continue
            candidates.append((end_delta + frame_gap_s, group))
        if candidates:
            candidates.sort(key=lambda item: item[0])
            return candidates[0][1], 'timeline'

    if not replay_frames or not replay_frame_ms:
        return None, None
    duration = replay_frames * replay_frame_ms / 1000.0
    best, best_err = None, None
    for session in sessions:
        if session.get('duration_s') is None:
            continue
        err = abs(session['duration_s'] - duration)
        if best_err is None or err < best_err:
            best, best_err = session, err
    if best is not None and best_err <= REPLAY_FINGERPRINT_TOLERANCE_S:
        return best, 'fingerprint'
    return None, None


def _group_session_fragments(sessions):
    """Combine journal files that continue one AC replay frame sequence."""
    ordered = sorted(
        (session for session in sessions
         if session.get('first_t') is not None and
         session.get('last_t') is not None and
         session.get('first_replay_frame') is not None and
         session.get('last_replay_frame') is not None),
        key=lambda session: session['first_t'])
    groups = []
    for session in ordered:
        previous = groups[-1] if groups else None
        same_identity = previous is not None and \
            session.get('session') is not None and \
            session.get('session') == previous.get('session') and \
            session.get('session_index') == previous.get('session_index')
        short_gap = previous is not None and \
            0 <= session['first_t'] - previous['last_t'] <= \
            REPLAY_FRAGMENT_GAP_S
        frame_continues = previous is not None and \
            session['first_replay_frame'] >= previous['last_replay_frame']
        frame_ms_matches = previous is not None and \
            session.get('replay_frame_ms') == previous.get('replay_frame_ms')
        replay_names_compatible = previous is not None and not (
            session.get('replay_file') and previous.get('replay_file') and
            session['replay_file'] != previous['replay_file'])

        if same_identity and short_gap and frame_continues and \
                frame_ms_matches and replay_names_compatible:
            previous['files'].append(session['file'])
            previous['file'] = session['file']
            previous['count'] += session.get('count', 0)
            previous['last_t'] = session['last_t']
            previous['last_replay_frame'] = session['last_replay_frame']
            previous['max_replay_frame'] = max(
                previous['max_replay_frame'], session['max_replay_frame'])
            previous['duration_s'] = max(
                previous.get('duration_s') or 0,
                session.get('duration_s') or 0) or None
            if not previous.get('replay_file'):
                previous['replay_file'] = session.get('replay_file')
            continue

        group = dict(session)
        group['files'] = [session['file']]
        groups.append(group)
    return groups


class EventJournal:
    def __init__(self, directory, clock=time.time):
        self.directory = directory
        self._clock = clock
        self._lock = threading.Lock()
        self._path = None
        self._count = 0
        self._label = None
        self._track_name = None
        self._replay_dir = None
        self._replay_file = None
        self._session_started_at = None
        self._session_letter = None

    @property
    def path(self):
        """Path of the current session file, or None before the first write."""
        return self._path

    @property
    def count(self):
        return self._count

    @property
    def replay_file(self):
        """Basename of the .acreplay this journal annotates, if it was found."""
        return self._replay_file

    def list_sessions(self):
        """Summary of every journalled session, newest file first.

        Used to offer a manual override when a loaded replay can't be matched
        automatically. Never raises — the panel must survive a corrupt or
        half-written file.
        """
        sessions = []
        if not os.path.isdir(self.directory):
            return sessions
        try:
            names = os.listdir(self.directory)
        except OSError:
            return sessions

        for name in sorted(names, reverse=True):
            if not name.endswith('.jsonl'):
                continue
            path = os.path.join(self.directory, name)
            records = self._read(path)
            if not records:
                continue
            session = {
                'file': name,
                'path': path,
                'replay_file': None,
                'count': len(records),
                'first_t': None,
                'last_t': None,
                'duration_s': None,
                'first_replay_frame': None,
                'last_replay_frame': None,
                'max_replay_frame': None,
                'replay_frame_ms': None,
                'session': None,
                'session_index': None,
            }
            for record in records:
                if session['replay_file'] is None and record.get('replay_file'):
                    session['replay_file'] = record['replay_file']
                t = record.get('t')
                if t is not None:
                    if session['first_t'] is None or t < session['first_t']:
                        session['first_t'] = t
                    if session['last_t'] is None or t > session['last_t']:
                        session['last_t'] = t
                # Records are appended in time order, so the last value wins.
                if record.get('session_s') is not None:
                    session['duration_s'] = record['session_s']
                if record.get('replay_frame') is not None:
                    frame = record['replay_frame']
                    if session['first_replay_frame'] is None:
                        session['first_replay_frame'] = frame
                    session['last_replay_frame'] = frame
                    if session['max_replay_frame'] is None or \
                            frame > session['max_replay_frame']:
                        session['max_replay_frame'] = frame
                if record.get('replay_frame_ms') is not None:
                    session['replay_frame_ms'] = record['replay_frame_ms']
                if session['session'] is None and record.get('session') is not None:
                    session['session'] = record['session']
                if session['session_index'] is None and \
                        record.get('session_index') is not None:
                    session['session_index'] = record['session_index']
            sessions.append(session)
        return sessions

    def load(self, filename):
        """Read one journal file back as a list of records, or None.

        `filename` is a basename from list_sessions(); anything that isn't a
        plain .jsonl name is refused so a client can't read arbitrary paths.
        """
        if os.path.basename(filename) != filename or \
                not filename.endswith('.jsonl'):
            return None
        path = os.path.join(self.directory, filename)
        if not os.path.isfile(path):
            return None
        return self._read(path)

    def _read(self, path):
        records = []
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict):
                        records.append(record)
        except OSError:
            return []
        return records

    def start_session(self, label=None, replay_dir=None, started_at=None,
                      letter=None, track_name=None):
        """Begin a new file on the next append, tagged with `label`.

        AC records a separate replay per session and wipes the instant-replay
        buffer when a new one starts, so markers must not span sessions: their
        frame and session offsets would point into a different recording.

        Given `replay_dir` (AC's replay/temp) and the session start, the
        journal names itself after the .acreplay being recorded, so the pair
        sits together in a directory listing. If no replay matches,
        `track_name` is included in the timestamped fallback filename.
        """
        with self._lock:
            self._path = None
            self._count = 0
            self._label = label
            self._track_name = track_name
            self._replay_dir = replay_dir
            self._replay_file = None
            self._session_started_at = started_at
            self._session_letter = letter

    def end_session(self):
        """Forget the active AC run without modifying its journal file."""
        with self._lock:
            self._path = None
            self._count = 0
            self._label = None
            self._track_name = None
            self._replay_dir = None
            self._replay_file = None
            self._session_started_at = None
            self._session_letter = None

    def append(self, event, context=None):
        """Write one event. Returns the record written, or None on failure.

        Never raises: a broadcast must not stop because a disk write failed.
        """
        try:
            with self._lock:
                # Retried until it lands: on the first event of a session AC may
                # not have created the file yet.
                if self._replay_file is None:
                    self._replay_file = find_session_replay(
                        self._replay_dir, self._session_started_at,
                        self._session_letter)
                record = self._build_record(event, context or {})
                if self._path is None:
                    self._path = self._new_path()
                    os.makedirs(self.directory, exist_ok=True)
                # Opened per write and flushed: the rig can lose power mid-race
                # and everything logged up to that point must survive.
                with open(self._path, 'a', encoding='utf-8') as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + '\n')
                    fh.flush()
                self._count += 1
        except OSError as e:
            print('[event_journal] write failed: {}'.format(e))
            return None
        return record

    def _new_path(self):
        # Same stem as the replay it annotates, when that could be identified.
        if self._replay_file:
            stem = os.path.splitext(self._replay_file)[0]
            return os.path.join(self.directory, stem + '.jsonl')
        stamp = datetime.fromtimestamp(self._clock()).strftime('%Y%m%d-%H%M%S')
        slug = '-'.join(filter(None, (
            self._slug(self._track_name), self._slug(self._label))))
        name = 'events-{}-{}.jsonl'.format(stamp, slug) if slug \
            else 'events-{}.jsonl'.format(stamp)
        return os.path.join(self.directory, name)

    @staticmethod
    def _slug(label):
        if not label:
            return ''
        ascii_label = unicodedata.normalize('NFKD', str(label)).encode(
            'ascii', 'ignore').decode('ascii')
        slug = re.sub(r'[^A-Za-z0-9]+', '-', ascii_label).strip('-').lower()
        return slug[:32]

    def _build_record(self, event, context):
        t = event.get('t', self._clock())
        record = {
            'iso': datetime.fromtimestamp(t).isoformat(timespec='seconds'),
            't': round(t, 3),
            'kind': event.get('kind'),
            'label': event.get('label'),
            'car_id': event.get('car_id'),
            'driver': event.get('name'),
        }
        if event.get('preroll_s') is not None:
            record['preroll_s'] = event['preroll_s']
        if event.get('mark_back_s') is not None:
            record['mark_back_s'] = event['mark_back_s']
        if event.get('lap') is not None:
            record['leader_lap'] = event['lap']
        if event.get('time_remaining_ms') is not None:
            record['time_remaining_ms'] = event['time_remaining_ms']
        if self._replay_file:
            # Repeated per record: the file may be renamed on its way out of
            # temp, but a record still says which recording it belongs to.
            record['replay_file'] = self._replay_file
        for key in ('session', 'session_index', 'session_s', 'replay_frame',
                    'replay_frame_ms', 'team', 'position', 'class_position',
                    'car_class', 'lap'):
            if context.get(key) is not None:
                record[key] = context[key]
        return record
