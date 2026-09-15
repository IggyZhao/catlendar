"""Reads meetings from macOS Calendar (EventKit), where the Outlook/Exchange
account lives. Falls back to asking Outlook directly over AppleScript."""
import hashlib
import logging
import subprocess
import threading
import time

from . import config, db
from .paths import IS_MAC

if IS_MAC:
    from Foundation import NSDate, NSRunLoop
else:                                   # calendar reading is macOS only for now
    NSDate = NSRunLoop = None

log = logging.getLogger("catlendar.calendar")

STATUS_NAMES = {0: "notDetermined", 1: "restricted", 2: "denied", 3: "authorized", 4: "writeOnly", 5: "fullAccess"}
_local = threading.local()


def _event_store():
    """EKEventStore is not thread safe, and a store built before access was
    granted returns nothing, so keep one per thread and reset it after a grant."""
    store = getattr(_local, "store", None)
    if store is None:
        import EventKit
        store = EventKit.EKEventStore.alloc().init()
        _local.store = store
    return store


def reset_store():
    store = getattr(_local, "store", None)
    if store is not None:
        store.reset()


def authorization_status():
    if not IS_MAC:
        return 1                        # restricted: nothing to ask for
    import EventKit
    return int(EventKit.EKEventStore.authorizationStatusForEntityType_(EventKit.EKEntityTypeEvent))


def authorized():
    return authorization_status() in (3, 5)


def request_access(timeout=60):
    """Shows the macOS permission sheet. Must run on the main thread."""
    store = _event_store()
    done, result = threading.Event(), {}

    def handler(granted, error):
        result["granted"] = bool(granted)
        result["error"] = error
        done.set()

    if hasattr(store, "requestFullAccessToEventsWithCompletion_"):
        store.requestFullAccessToEventsWithCompletion_(handler)
    else:
        import EventKit
        store.requestAccessToEntityType_completion_(EventKit.EKEntityTypeEvent, handler)

    deadline = time.time() + timeout
    while not done.is_set() and time.time() < deadline:
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
    if result.get("granted"):
        store.reset()
    return result.get("granted", False)


def calendars():
    import EventKit
    store = _event_store()
    return [
        {"title": c.title(), "type": int(c.type()), "source": c.source().title() if c.source() else None}
        for c in store.calendarsForEntityType_(EventKit.EKEntityTypeEvent)
    ]


def _excluded(name):
    """Holiday and birthday subscriptions are not meetings."""
    skip = [x.lower() for x in (config.load()["settings"].get("exclude_calendars") or [])]
    return (name or "").lower() in skip


def _uid(title, start_ts, end_ts):
    """Keyed on the event itself, so the same meeting subscribed in two
    calendars is stored once instead of double counting its minutes."""
    raw = "{}|{}|{}".format((title or "").strip().lower(), int(start_ts), int(end_ts))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _fetch_eventkit(start_ts, end_ts):
    store = _event_store()
    start = NSDate.dateWithTimeIntervalSince1970_(start_ts)
    end = NSDate.dateWithTimeIntervalSince1970_(end_ts)
    pred = store.predicateForEventsWithStartDate_endDate_calendars_(start, end, None)
    out = []
    for ev in store.eventsMatchingPredicate_(pred) or []:
        try:
            title = ev.title() or "(no title)"
            cal_name = str(ev.calendar().title()) if ev.calendar() else None
            if _excluded(cal_name):
                continue
            attendees = ev.attendees() or []
            organizer = ev.organizer().name() if ev.organizer() else None
            out.append(
                {
                    "uid": _uid(title, ev.startDate().timeIntervalSince1970(),
                                ev.endDate().timeIntervalSince1970()),
                    "title": str(title),
                    "start_ts": int(ev.startDate().timeIntervalSince1970()),
                    "end_ts": int(ev.endDate().timeIntervalSince1970()),
                    "calendar": cal_name,
                    "location": str(ev.location()) if ev.location() else None,
                    "all_day": 1 if ev.isAllDay() else 0,
                    "organizer": str(organizer) if organizer else None,
                    "attendees": len(attendees),
                    "status": str(int(ev.status())),
                    "project": config.classify(None, str(title))[0],
                    "source": "eventkit",
                }
            )
        except Exception:
            log.exception("skipping an unreadable event")
    return out


OUTLOOK_SCRIPT = r'''
on isoOf(d)
    set y to year of d as integer
    set m to (month of d as integer)
    set dd to day of d as integer
    set hh to hours of d
    set mi to minutes of d
    set ss to seconds of d
    return (y as string) & "-" & text -2 thru -1 of ("0" & m) & "-" & text -2 thru -1 of ("0" & dd) & "T" & text -2 thru -1 of ("0" & hh) & ":" & text -2 thru -1 of ("0" & mi) & ":" & text -2 thru -1 of ("0" & ss)
end isoOf

set startDate to (current date) - (%DAYS_BACK% * days)
set endDate to (current date) + (%DAYS_FWD% * days)
set out to ""
tell application "Microsoft Outlook"
    set evs to (every calendar event of calendar 1 whose start time is greater than startDate and start time is less than endDate)
    repeat with e in evs
        set out to out & (id of e as string) & tab & (subject of e) & tab & my isoOf(start time of e) & tab & my isoOf(end time of e) & tab & (all day flag of e as string) & linefeed
    end repeat
end tell
return out
'''


def _fetch_outlook(days_back, days_forward):
    import datetime
    script = OUTLOOK_SCRIPT.replace("%DAYS_BACK%", str(days_back)).replace("%DAYS_FWD%", str(days_forward))
    try:
        res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        log.warning("Outlook AppleScript timed out")
        return []
    if res.returncode != 0:
        log.warning("Outlook AppleScript failed: %s", res.stderr.strip()[:200])
        return []
    out = []
    for line in res.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        uid, subject, start_s, end_s, allday = parts[:5]
        try:
            start = datetime.datetime.fromisoformat(start_s)
            end = datetime.datetime.fromisoformat(end_s)
        except ValueError:
            continue
        out.append(
            {
                "uid": _uid(subject, start.timestamp(), end.timestamp()),
                "title": subject,
                "start_ts": int(start.timestamp()),
                "end_ts": int(end.timestamp()),
                "calendar": "Outlook",
                "location": None,
                "all_day": 1 if allday.lower() == "true" else 0,
                "organizer": None,
                "attendees": 0,
                "status": None,
                "project": config.classify(None, subject)[0],
                "source": "outlook",
            }
        )
    return out


def sync(days_back=45, days_forward=21):
    """Pull events into the local DB. Returns (count, source)."""
    if not IS_MAC:
        return 0, "unsupported"
    now = int(time.time())
    rows, source = [], "none"
    if authorized():
        try:
            rows = _fetch_eventkit(now - days_back * 86400, now + days_forward * 86400)
            if not rows:                      # a stale store can come back empty
                reset_store()
                rows = _fetch_eventkit(now - days_back * 86400, now + days_forward * 86400)
            source = "eventkit" if rows else source
        except Exception:
            log.exception("EventKit fetch failed")
    if config.load()["settings"].get("outlook_applescript"):
        # Only useful on the older Outlook builds that still answer AppleScript.
        # The current Outlook for Mac reports an empty calendar here.
        seen = {r["uid"] for r in rows}
        extra = [r for r in _fetch_outlook(days_back, days_forward) if r["uid"] not in seen]
        if extra:
            rows += extra
            source = "eventkit+outlook" if source == "eventkit" else "outlook"
    if source == "eventkit" and db.get_meta("logged_calendars") != "1":
        try:
            log.info("calendars visible to Catlendar: %s",
                     [(c["source"], c["title"]) for c in calendars()])
            db.set_meta("logged_calendars", "1")
        except Exception:
            log.exception("could not list calendars")
    if rows:
        db.upsert_events(rows)
        db.set_meta("last_calendar_sync", now)
        db.set_meta("calendar_source", source)
    log.info("calendar sync: %d events from %s", len(rows), source)
    return len(rows), source


class CalendarSync(threading.Thread):
    daemon = True

    def __init__(self):
        super().__init__(name="catlendar-calendar")
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                sync()
            except Exception:
                log.exception("calendar sync failed")
            self._stop.wait(max(60, config.setting("calendar_sync_minutes") * 60))
