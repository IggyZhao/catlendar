"""Samples the frontmost window on a timer and writes one row per slice."""
import logging
import threading
import time

from . import config, db
from .platforms import current as platform

log = logging.getLogger("catlendar.tracker")

MEETING_BUNDLES = {
    "us.zoom.xos",
    "com.microsoft.teams",
    "com.microsoft.teams2",
    "com.cisco.webexmeetingsapp",
    "com.google.Chrome.app.meet",
}


def accessibility_trusted(prompt=False):
    """Whether the OS will hand over window titles."""
    if prompt and hasattr(platform, "request_titles"):
        return bool(platform.request_titles())
    return bool(platform.can_read_titles())


def permission_hint():
    return platform.permission_hint()


def idle_seconds():
    return platform.idle_seconds()


def screen_locked():
    return platform.screen_locked()


def front_window_title(pid):
    return platform.window_title(pid)


def frontmost():
    return platform.frontmost()


def presenting(skip_pid=None):
    return platform.presenting(skip_pid)


class Tracker(threading.Thread):
    daemon = True

    def __init__(self, on_sample=None):
        super().__init__(name="catlendar-tracker")
        self._stop = threading.Event()
        self.on_sample = on_sample
        self.last = {}
        self.paused_until = None   # optional callable returning an epoch time

    def stop(self):
        self._stop.set()

    def active_event(self, now):
        """The calendar event covering `now`, if any."""
        grace = config.setting("meeting_grace_minutes") * 60
        for ev in db.events_between(now - 3600, now + 3600):
            if ev["all_day"]:
                continue
            if ev["start_ts"] - grace <= now <= ev["end_ts"] + grace:
                return ev
        return None

    def sample(self, now=None):
        now = int(now or time.time())
        if self.paused_until and now < self.paused_until():
            self.last = {"ts": now, "state": "paused"}
            return self.last          # paused time is not recorded at all
        interval = int(config.setting("sample_interval_seconds"))
        app, bundle_id, pid = frontmost()

        if screen_locked():
            state, title, project, activity = "locked", None, None, None
        elif idle_seconds() > config.setting("idle_after_seconds"):
            state, title, project, activity = "idle", None, None, None
        else:
            state = "active"
            title = config.redact_title(app, front_window_title(pid) if pid else None)
            project, activity = config.classify(app, title)
            ev = self.active_event(now)
            in_meeting_app = bundle_id in MEETING_BUNDLES or activity == "meeting"
            if ev and in_meeting_app:
                state = "meeting"
                activity = "meeting"
                project = ev["project"] or project
            elif ev and activity == "meeting":
                state = "meeting"

        row = dict(
            ts=now, dur=interval, app=app, bundle_id=bundle_id, title=title,
            project=project, activity=activity, state=state,
        )
        db.insert_sample(**row)
        self.last = row
        if self.on_sample:
            try:
                self.on_sample(row)
            except Exception:
                log.exception("on_sample callback failed")
        return row

    def run(self):
        log.info("tracker started (accessibility=%s)", accessibility_trusted())
        while not self._stop.is_set():
            started = time.time()
            try:
                self.sample(started)
            except Exception:
                log.exception("sample failed")
            interval = int(config.setting("sample_interval_seconds"))
            self._stop.wait(max(1.0, interval - (time.time() - started)))
