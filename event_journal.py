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
S being O (practice/other), Q or R. That timestamp is the session start, so a
journal can name itself after the replay it annotates — same stem, .jsonl
extension — and each record carries the replay filename too, in case the file
is later renamed on the way out of temp.
"""
import json
import os
import re
import threading
import time
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


def parse_replay_name(name):
    """Return (start_datetime, session_letter) for an AC replay filename.

    Returns None for anything that is not AC's autosave naming — replays saved
    by hand get arbitrary names and must not be matched by guesswork.
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

    Falls back to a duration fingerprint for renamed files. Returns a
    (session, confidence) tuple, or (None, None) — confidence is 'exact' or
    'fingerprint'.
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


class EventJournal:
    def __init__(self, directory, clock=time.time):
        self.directory = directory
        self._clock = clock
        self._lock = threading.Lock()
        self._path = None
        self._count = 0
        self._label = None
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
                'max_replay_frame': None,
                'replay_frame_ms': None,
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
                    session['max_replay_frame'] = record['replay_frame']
                if record.get('replay_frame_ms') is not None:
                    session['replay_frame_ms'] = record['replay_frame_ms']
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
                      letter=None):
        """Begin a new file on the next append, tagged with `label`.

        AC records a separate replay per session and wipes the instant-replay
        buffer when a new one starts, so markers must not span sessions: their
        frame and session offsets would point into a different recording.

        Given `replay_dir` (AC's replay/temp) and the session start, the
        journal names itself after the .acreplay being recorded, so the pair
        sits together in a directory listing.
        """
        with self._lock:
            self._path = None
            self._count = 0
            self._label = label
            self._replay_dir = replay_dir
            self._replay_file = None
            self._session_started_at = started_at
            self._session_letter = letter

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
        slug = self._slug(self._label)
        name = 'events-{}-{}.jsonl'.format(stamp, slug) if slug \
            else 'events-{}.jsonl'.format(stamp)
        return os.path.join(self.directory, name)

    @staticmethod
    def _slug(label):
        if not label:
            return ''
        slug = re.sub(r'[^A-Za-z0-9]+', '-', str(label)).strip('-').lower()
        return slug[:32]

    def _build_record(self, event, context):
        t = event.get('t', self._clock())
        record = {
            'iso': datetime.fromtimestamp(t).isoformat(timespec='seconds'),
            't': round(t, 3),
            'kind': event.get('kind'),
            'label': event.get('label'),
            'car_id': event.get('car_id'),
            'car_number': event.get('car_number'),
            'driver': event.get('name'),
        }
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
