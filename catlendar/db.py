"""SQLite storage. Everything stays on this machine."""
import sqlite3
import time
from contextlib import contextmanager

from .paths import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,      -- unix seconds, start of the sampled slice
    dur       INTEGER NOT NULL,      -- seconds this sample stands for
    app       TEXT,                  -- localized app name, e.g. "Cursor"
    bundle_id TEXT,
    title     TEXT,                  -- frontmost window title (may be redacted)
    project   TEXT,                  -- classified project key
    activity  TEXT,                  -- classified activity key
    state     TEXT NOT NULL          -- active | idle | locked | meeting
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
CREATE INDEX IF NOT EXISTS idx_samples_project ON samples(project);

CREATE TABLE IF NOT EXISTS events (
    uid       TEXT PRIMARY KEY,
    title     TEXT,
    start_ts  INTEGER NOT NULL,
    end_ts    INTEGER NOT NULL,
    calendar  TEXT,
    location  TEXT,
    all_day   INTEGER DEFAULT 0,
    organizer TEXT,
    attendees INTEGER DEFAULT 0,
    status    TEXT,
    project   TEXT,
    source    TEXT,                  -- eventkit | outlook
    synced_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_ts);

CREATE TABLE IF NOT EXISTS signals (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    end_ts  INTEGER,              -- set for spans of activity, null for instants
    kind    TEXT NOT NULL,        -- claude | file
    project TEXT,
    detail  TEXT,                 -- the prompt, or the file name
    path    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_unique ON signals(kind, ts, detail);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);

CREATE TABLE IF NOT EXISTS manual (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts INTEGER NOT NULL,
    end_ts   INTEGER NOT NULL,
    project  TEXT,
    note     TEXT,
    mode     TEXT DEFAULT 'add'      -- add: counts alongside what was detected
);                                   -- only: this slot counts as this project alone
CREATE INDEX IF NOT EXISTS idx_manual_start ON manual(start_ts);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init():
    with connect() as conn:
        conn.executescript(SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(signals)")}
        if "end_ts" not in cols:          # upgrade an existing database in place
            conn.execute("ALTER TABLE signals ADD COLUMN end_ts INTEGER")


@contextmanager
def cursor():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def insert_sample(ts, dur, app, bundle_id, title, project, activity, state):
    with cursor() as conn:
        conn.execute(
            "INSERT INTO samples (ts, dur, app, bundle_id, title, project, activity, state)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (int(ts), int(dur), app, bundle_id, title, project, activity, state),
        )


def upsert_events(rows):
    """rows: list of dicts matching the events columns."""
    now = int(time.time())
    with cursor() as conn:
        for r in rows:
            conn.execute(
                "INSERT INTO events (uid,title,start_ts,end_ts,calendar,location,all_day,"
                "organizer,attendees,status,project,source,synced_at)"
                " VALUES (:uid,:title,:start_ts,:end_ts,:calendar,:location,:all_day,"
                ":organizer,:attendees,:status,:project,:source,:synced_at)"
                " ON CONFLICT(uid) DO UPDATE SET title=excluded.title, start_ts=excluded.start_ts,"
                " end_ts=excluded.end_ts, calendar=excluded.calendar, location=excluded.location,"
                " all_day=excluded.all_day, organizer=excluded.organizer,"
                " attendees=excluded.attendees, status=excluded.status,"
                " project=excluded.project, source=excluded.source, synced_at=excluded.synced_at",
                {**r, "synced_at": now},
            )


def insert_signals(rows):
    """Ignores anything already recorded, so scans can overlap safely."""
    if not rows:
        return 0
    added = 0
    with cursor() as conn:
        for r in rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO signals (ts, end_ts, kind, project, detail, path)"
                " VALUES (:ts,:end_ts,:kind,:project,:detail,:path)", r)
            added += cur.rowcount or 0
    return added


def signals_between(start_ts, end_ts, kinds=None):
    sql = "SELECT * FROM signals WHERE ts >= ? AND ts < ?"
    args = [int(start_ts), int(end_ts)]
    if kinds:
        sql += " AND kind IN ({})".format(",".join("?" * len(kinds)))
        args += list(kinds)
    with cursor() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY ts", args)]


def manual_between(start_ts, end_ts):
    with cursor() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM manual WHERE end_ts > ? AND start_ts < ? ORDER BY start_ts",
            (int(start_ts), int(end_ts)))]


def add_manual(row):
    with cursor() as conn:
        conn.execute(
            "INSERT INTO manual (start_ts, end_ts, project, note, mode)"
            " VALUES (:start_ts,:end_ts,:project,:note,:mode)", row)
    return True


def clear_manual_overlapping(project, start_ts, end_ts, mode=None):
    """Drop earlier overrides for this project in this window, so repeated
    edits to the same block replace each other instead of piling up."""
    sql = ("DELETE FROM manual WHERE project IS ? AND end_ts > ? AND start_ts < ?"
           " AND note LIKE 'block edit%'")
    args = [project, int(start_ts), int(end_ts)]
    if mode:
        sql += " AND mode = ?"
        args.append(mode)
    with cursor() as conn:
        return conn.execute(sql, args).rowcount


def replace_manual_for_day(day_start, day_end, rows):
    """The dashboard sends the whole day back, so swap that day wholesale."""
    with cursor() as conn:
        conn.execute("DELETE FROM manual WHERE start_ts >= ? AND start_ts < ?",
                     (int(day_start), int(day_end)))
        for r in rows:
            conn.execute(
                "INSERT INTO manual (start_ts, end_ts, project, note, mode)"
                " VALUES (:start_ts,:end_ts,:project,:note,:mode)", r)
    return len(rows)


def set_meta(key, value):
    with cursor() as conn:
        conn.execute(
            "INSERT INTO meta (key,value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def get_meta(key, default=None):
    with cursor() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def events_between(start_ts, end_ts):
    with cursor() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM events WHERE end_ts > ? AND start_ts < ? ORDER BY start_ts",
                (int(start_ts), int(end_ts)),
            )
        ]


def samples_between(start_ts, end_ts):
    with cursor() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM samples WHERE ts >= ? AND ts < ? ORDER BY ts",
                (int(start_ts), int(end_ts)),
            )
        ]
