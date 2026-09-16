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
            # insertion order, so a later edit wins over an earlier one
            "SELECT * FROM manual WHERE end_ts > ? AND start_ts < ? ORDER BY id",
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

# ---------------------------------------------------------------- housekeeping
SLOT = 600


def compact_samples(before_ts, slot=SLOT):
    """Roll old samples up to one row per slot per distinct activity.

    Every number the reports show is bucketed into ten minute slots, so
    collapsing the sixty raw samples inside one old slot into one row per
    distinct (app, title, project, activity, state) changes no total. What it
    drops is the second by second ordering inside a slot on an old day, which
    only makes that day's timeline blockier. Recent days are left alone.
    """
    before = int(before_ts)
    with connect() as conn:
        rows = conn.execute(
            "SELECT MIN(ts) AS ts, SUM(dur) AS dur, app, bundle_id, title, "
            "       project, activity, state, COUNT(*) AS n "
            "FROM samples WHERE ts < ? "
            "GROUP BY ts / ?, app, bundle_id, title, project, activity, state",
            (before, int(slot)),
        ).fetchall()
        if not rows:
            return 0, 0
        was = conn.execute(
            "SELECT COUNT(*) FROM samples WHERE ts < ?", (before,)
        ).fetchone()[0]
        if was <= len(rows):
            return was, was
        conn.execute("DELETE FROM samples WHERE ts < ?", (before,))
        conn.executemany(
            "INSERT INTO samples (ts, dur, app, bundle_id, title, project, "
            "activity, state) VALUES (?,?,?,?,?,?,?,?)",
            [
                (r["ts"], r["dur"], r["app"], r["bundle_id"], r["title"],
                 r["project"], r["activity"], r["state"])
                for r in rows
            ],
        )
    return was, len(rows)


def drop_before(cutoff_ts):
    """Forget everything older than the cutoff. Only runs when history_days is
    set; the default keeps every day you have ever tracked."""
    cutoff = int(cutoff_ts)
    with connect() as conn:
        n = conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,)).rowcount
        n += conn.execute("DELETE FROM signals WHERE ts < ?", (cutoff,)).rowcount
        n += conn.execute("DELETE FROM events WHERE end_ts < ?", (cutoff,)).rowcount
    return n


def vacuum():
    conn = sqlite3.connect(DB_PATH, timeout=60, isolation_level=None)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()


def footprint():
    import os
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(DB_PATH + suffix)
        except OSError:
            pass
    with cursor() as conn:
        counts = {
            t: conn.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
            for t in ("samples", "signals", "events", "manual")
        }
    return {"bytes": total, "rows": counts}


def maintain(compact_after_days=3, history_days=0, verbose=False):
    """Daily housekeeping. Cheap, safe to run at any time."""
    now = int(time.time())
    before, after = compact_samples(now - int(compact_after_days) * 86400)
    dropped = 0
    if int(history_days) > 0:
        dropped = drop_before(now - int(history_days) * 86400)
    freed = (before - after) + dropped
    if freed > 20000:
        vacuum()
    result = {"compacted_from": before, "compacted_to": after,
              "dropped": dropped, "vacuumed": freed > 20000}
    if verbose:
        print(result)
    return result
