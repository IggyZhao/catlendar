"""Catlendar: the agent process. Menu bar item, floating cat, dashboard window."""
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time

import objc
from AppKit import (
    NSApplication, NSApplicationActivationPolicyAccessory, NSMenu, NSStatusBar,
    NSVariableStatusItemLength,
)
from Foundation import NSObject, NSTimer
from PyObjCTools import AppHelper

from . import calsync, config, dashboard, db, goodnews, pipeline, report, sources, tracker, ui
from .paths import ASSETS_DIR, DATA_DIR, LOG_DIR, REPORT_DIR

LOG_PATH = os.path.join(LOG_DIR, "catlendar.log")
REFRESH_SECONDS = 20.0


def setup_logging():
    handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr and sys.stderr.isatty():
        root.addHandler(logging.StreamHandler())
    return logging.getLogger("catlendar")


log = logging.getLogger("catlendar.app")


class CatlendarDelegate(NSObject):
    # ------------------------------------------------------------------ setup
    def init(self):
        self = objc.super(CatlendarDelegate, self).init()
        if self is None:
            return None
        self.paused_until = 0
        self.dashboard_window = None
        self.building = False
        self.dashboard_scope = "day"
        self.dashboard_date = None      # None means today
        self._auto_hidden = False       # hidden by us for a full screen app
        self._hotkey_monitor = None
        self._raised = False            # lifted above the windows for a meeting alert
        return self

    def applicationDidFinishLaunching_(self, notification):
        db.init()
        config.ensure_user_config()

        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength)
        button = self.status_item.button()
        image = ui.status_image()
        if image is not None:
            button.setImage_(image)
        button.setTitle_(" ...")
        menu = NSMenu.alloc().init()
        menu.setDelegate_(self)
        self.status_item.setMenu_(menu)
        self.menu = menu

        self.pet = ui.PetWindow.alloc().initWithAssets_handlers_layer_(
            ASSETS_DIR,
            {"click": self.open_dashboard, "moved": self.remember_pet_position,
             "menu": self.context_menu, "double": self.feed_cat},
            str(config.setting("cat_layer")),
        )
        saved = db.get_meta("pet_origin")
        if saved:
            try:
                x, y = saved.split(",")
                self.pet.move_to(float(x), float(y))
            except ValueError:
                pass
        if db.get_meta("pet_hidden", "0") != "1":
            self.pet.show()
        frame = self.pet.window.frame()
        log.info("cat window visible=%s at (%.0f,%.0f) size %.0fx%.0f",
                 self.pet.window.isVisible(), frame.origin.x, frame.origin.y,
                 frame.size.width, frame.size.height)

        self.tracker = tracker.Tracker()
        self.tracker.paused_until = lambda: self.paused_until
        self.tracker.start()
        self.calendar = calsync.CalendarSync()
        self.calendar.start()
        self.signals = sources.SignalScanner()
        self.signals.start()

        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            REFRESH_SECONDS, self, "tick:", None, True)
        # a separate, quicker beat: nobody wants to wait twenty seconds for the
        # cat to get out of the way when a slideshow starts
        self.screen_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            3.0, self, "checkFullScreen:", None, True)
        self.install_hotkey()
        self.request_permissions_if_needed()
        self.tick_(None)
        if os.environ.get("CATLENDAR_TEST_OPEN") == "1":
            self.open_dashboard(why="launch-hook")
        log.info("Catlendar started")

    def checkFullScreen_(self, timer):
        """Step aside while something is running full screen, come back after."""
        if (self.pet is None or not config.setting("hide_when_fullscreen")
                or config.setting("cat_layer") == "desktop"):
            return                      # on the desktop, windows already cover it
        if db.get_meta("pet_hidden", "0") == "1":
            return                      # you hid it yourself; leave it hidden
        try:
            full = tracker.presenting(skip_pid=os.getpid())
        except Exception:
            return
        if full and self.pet.visible():
            self.pet.hide()
            self._auto_hidden = True
        elif not full and self._auto_hidden:
            self.pet.show()
            self._auto_hidden = False

    @objc.python_method
    def install_hotkey(self):
        """Control + Option + C toggles the cat from anywhere."""
        from AppKit import NSEvent, NSEventMaskKeyDown
        mods = (1 << 18) | (1 << 19)          # control | option
        def handler(event):
            try:
                if (event.modifierFlags() & mods) == mods and event.keyCode() == 8:  # C
                    AppHelper.callAfter(self.toggle_pet)
            except Exception:
                pass
        try:
            self._hotkey_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                NSEventMaskKeyDown, handler)
            log.info("hotkey ready: control option C")
        except Exception:
            log.exception("could not register the hotkey")

    @objc.python_method
    def toggle_pet(self):
        if self.pet is None:
            return
        self._auto_hidden = False
        if self.pet.visible():
            self.pet.hide()
            db.set_meta("pet_hidden", "1")
        else:
            self.pet.show()
            db.set_meta("pet_hidden", "0")

    # ------------------------------------------------------------ permissions
    def request_permissions_if_needed(self):
        if not tracker.accessibility_trusted():
            log.info("asking for Accessibility permission")
            tracker.accessibility_trusted(prompt=True)
        try:
            if calsync.authorization_status() == 0:
                log.info("asking for Calendar permission")
                granted = calsync.request_access()
                log.info("calendar permission granted=%s", granted)
                if granted:
                    threading.Thread(target=calsync.sync, daemon=True).start()
        except Exception:
            log.exception("calendar permission request failed")

    # ------------------------------------------------------------------ state
    @objc.python_method
    def pet_state(self, status):
        """Every pose is tied to something the app actually knows."""
        if time.time() < self.paused_until:
            return "paused"
        idle = tracker.idle_seconds()
        if status["in_meeting"]:
            return "meeting"
        soon = status.get("next_meeting_ts")
        # Alert whatever you are doing. Being away from the keyboard is the very
        # case where a five minute warning is worth most; only a locked screen
        # means nobody is there to see it.
        if soon and 0 < soon - time.time() <= 5 * 60 and status["state"] != "locked":
            return "soon"
        if idle >= config.setting("asleep_after_minutes") * 60 or status["state"] == "locked":
            return "asleep"
        if idle >= config.setting("idle_after_seconds") or status["state"] in ("idle", "unknown"):
            return "away"
        if status.get("block_seconds", 0) >= 1500:
            return "focus"
        return "work"

    @objc.python_method
    def pet_text(self, status, state):
        if state == "paused":
            return "paused", report.fmt_hm(max(0, self.paused_until - time.time())) + " left"
        if state == "meeting":
            ends = status["meeting_ends"]
            sub = "ends " + _clock(ends) if ends else ""
            return _clip(status["meeting_title"] or "in a meeting", 26), sub
        if state == "soon":
            mins = max(1, int((status["next_meeting_ts"] - time.time()) // 60))
            return _clip(status["next_meeting"] or "a meeting", 24), "in {}m".format(mins)
        if state == "asleep":
            return "asleep", report.fmt_hm(tracker.idle_seconds()) + " away"
        if state == "away":
            return "away", report.fmt_hm(status["today_seconds"]) + " today"
        if not tracker.accessibility_trusted():
            return report.fmt_hm(status["today_seconds"]), "needs permission"
        project = status["project_label"]
        if project == "Unassigned" and status["app"]:
            project = status["app"]
        return report.fmt_hm(status["today_seconds"]), _clip(project, 24)

    def tick_(self, timer):
        try:
            status = report.current_status()
        except Exception:
            log.exception("status failed")
            return
        state = self.pet_state(status)
        main, sub = self.pet_text(status, state)
        hour = time.localtime().tm_hour
        night = hour >= 23 or hour < int(config.setting("day_start_hour"))
        if self.pet is not None:
            self.pet.update(state, main, sub, night)
            self.raise_for_alert(state == "soon")
        title = " paused" if state == "paused" else " " + report.fmt_hm(status["today_seconds"])
        self.status_item.button().setTitle_(title)
        self.status = status
        # never reload under someone editing the pipeline
        # a past day never changes, so there is nothing to refresh
        if (self.dashboard_window is not None and self.dashboard_window.visible()
                and self.dashboard_date is None
                and self.dashboard_scope != "pipeline"
                and (time.time() - getattr(self, "_last_build", 0)) > 120):
            self.rebuild_dashboard(show=False, why="tick")

    @objc.python_method
    def raise_for_alert(self, alerting):
        """A meeting warning is no use behind a window. Lift the cat above
        everything while it is counting down, then put it back where it lives.
        A cat you hid yourself stays hidden: that was a deliberate choice."""
        if self.pet is None or not config.setting("raise_for_meeting_alert"):
            return
        if db.get_meta("pet_hidden", "0") == "1":
            return
        if alerting and not self._raised:
            self.pet.set_layer("floating")
            self.pet.show()
            self._raised = True
            log.info("cat raised for a meeting starting soon")
        elif not alerting and self._raised:
            self.pet.set_layer(str(config.setting("cat_layer")))
            self._raised = False

    # ------------------------------------------------------------------- menu
    def menuNeedsUpdate_(self, menu):
        menu.removeAllItems()
        for item in self.build_menu_items():
            menu.addItem_(item)

    @objc.python_method
    def context_menu(self):
        menu = NSMenu.alloc().init()
        for item in self.build_menu_items():
            menu.addItem_(item)
        return menu

    @objc.python_method
    def build_menu_items(self):
        status = getattr(self, "status", None) or report.current_status()
        items = []
        now_line = "Now: " + status["project_label"]
        if status["app"]:
            now_line += "  (" + status["app"] + ")"
        if time.time() < self.paused_until:
            now_line = "Tracking paused"
        elif status["in_meeting"]:
            now_line = "In: " + _clip(status["meeting_title"] or "a meeting", 34)
        items.append(ui.menu_item(now_line, self, None, enabled=False))
        items.append(ui.menu_item(
            "Today: {}  ·  {} meetings".format(report.fmt_hm(status["today_seconds"]),
                                               status["today_meetings"]),
            self, None, enabled=False))
        if status["next_meeting"]:
            items.append(ui.menu_item(
                "Next: {} at {}".format(_clip(status["next_meeting"], 28),
                                        _clock(status["next_meeting_ts"])),
                self, None, enabled=False))
        try:
            due = pipeline.summary()
            if due["overdue"]:
                items.append(ui.menu_item(
                    "Past date: {}".format(_clip(due["overdue"][0]["text"], 30)),
                    self, "openDashboard:"))
            if due["upcoming"]:
                nxt = due["upcoming"][0]
                when = "today" if nxt["days"] == 0 else "in {} days".format(nxt["days"])
                items.append(ui.menu_item(
                    "Due {}: {}".format(when, _clip(nxt["text"], 26)), self, "openDashboard:"))
        except Exception:
            log.exception("pipeline summary failed")
        items.append(ui.separator())
        items.append(ui.menu_item("Open dashboard", self, "openDashboard:", "d"))
        items.append(ui.menu_item(
            ("Hide cat" if self.pet.visible() else "Show cat") + "   ^\u2325C",
            self, "togglePet:"))
        items.append(ui.menu_item(
            "Keep cat on top" if config.setting("cat_layer") == "desktop"
            else "Keep cat on the desktop", self, "toggleLayer:"))
        fed = int(db.get_meta("fed_today_count", 0) or 0) \
            if db.get_meta("fed_today_date", "") == report.today().isoformat() else 0
        items.append(ui.menu_item(
            "Feed the cat" + ("  ({} today)".format(fed) if fed else ""), self, "feedCat:"))
        paused = time.time() < self.paused_until
        items.append(ui.menu_item("Resume tracking" if paused else "Pause tracking for 1 hour",
                                  self, "togglePause:"))
        items.append(ui.separator())
        items.append(ui.menu_item("Sync calendar now", self, "syncCalendar:"))
        items.append(ui.menu_item("Edit project rules", self, "editRules:"))
        items.append(ui.menu_item("Open data folder", self, "openFolder:"))
        source = db.get_meta("calendar_source", "none")
        cal_state = {"eventkit": "macOS Calendar", "outlook": "Outlook"}.get(source, "not connected")
        items.append(ui.menu_item(
            "Calendar: {}  ·  Window titles: {}".format(
                cal_state, "on" if tracker.accessibility_trusted() else "needs permission"),
            self, "openPrivacy:", enabled=True))
        items.append(ui.separator())
        items.append(ui.menu_item("Quit Catlendar", self, "quit:", "q"))
        return items

    # ---------------------------------------------------------------- actions
    @objc.python_method
    def open_dashboard(self, why="click"):
        self.rebuild_dashboard(show=True, why=why)

    def openDashboard_(self, sender):
        self.open_dashboard(why="menu")

    @objc.python_method
    def rebuild_dashboard(self, show, why="?"):
        log.debug("rebuild requested by %s (show=%s, building=%s)", why, show, self.building)
        if self.building:
            if show and self.dashboard_window is not None:
                self.dashboard_window.show()
            return
        self.building = True

        def work():
            try:
                path = dashboard.render(scope=self.dashboard_scope,
                                        today=self.dashboard_date)
            except Exception:
                log.exception("dashboard build failed")
                path = None
            AppHelper.callAfter(self._present_dashboard, path, show)

        threading.Thread(target=work, daemon=True).start()

    @objc.python_method
    def on_page_message(self, body):
        action = (body or {}).get("action")
        if action == "savePipeline":
            before = self._finished_count()
            count = pipeline.save(body.get("data") or {})
            log.info("pipeline saved (%d sections)", count)
            if self._finished_count() > before and self.pet is not None:
                self.pet.move("celebrate")        # something got ticked off
        elif action == "saveGoodNews":
            before = len(goodnews.load())
            count = goodnews.save(body.get("items") or [])
            log.info("good news saved (%d items)", count)
            if count > before and self.pet is not None:
                self.pet.move("celebrate")
        elif action == "saveManual":
            rows = []
            for r in body.get("rows") or []:
                try:
                    start, end = int(r["start_ts"]), int(r["end_ts"])
                except (KeyError, TypeError, ValueError):
                    continue
                if end <= start:
                    continue
                rows.append({"start_ts": start, "end_ts": end,
                             "project": r.get("project") or None,
                             "note": r.get("note") or "",
                             "mode": "only" if r.get("mode") == "only" else "add"})
            count = db.replace_manual_for_day(int(body.get("day_start", 0)),
                                              int(body.get("day_end", 0)), rows)
            log.info("manual entries saved: %d", count)
        elif action == "scope":
            self.dashboard_scope = str(body.get("scope") or "day")
        elif action == "showDate":
            import datetime as _dt
            raw = str(body.get("date") or "")
            try:
                self.dashboard_date = _dt.date.fromisoformat(raw) if raw else None
            except ValueError:
                self.dashboard_date = None
            if self.dashboard_date == report.today():
                self.dashboard_date = None
            self.rebuild_dashboard(show=False, why="date")

    @objc.python_method
    def _present_dashboard(self, path, show):
        self.building = False
        self._last_build = time.time()
        if path is None:
            return
        if self.dashboard_window is None:
            self.dashboard_window = ui.DashboardWindow.alloc().initWithTitle_bridge_(
                "Catlendar", self.on_page_message)
            f = self.dashboard_window.window.frame()
            log.info("dashboard window opened at %.0fx%.0f", f.size.width, f.size.height)
        self.dashboard_window.load(path)
        if show:
            self.dashboard_window.show()
        log.info("dashboard loaded from %s (window visible=%s)",
                 path, self.dashboard_window.visible())

    @objc.python_method
    def feed_cat(self):
        if self.pet is None:
            return
        self.pet.move("feed")
        count = int(db.get_meta("fed_today_count", 0) or 0)
        day = db.get_meta("fed_today_date", "")
        today = report.today().isoformat()
        count = count + 1 if day == today else 1
        db.set_meta("fed_today_count", count)
        db.set_meta("fed_today_date", today)
        log.info("fed the cat (%d today)", count)

    def feedCat_(self, sender):
        self.feed_cat()

    def togglePet_(self, sender):
        self.toggle_pet()

    def toggleLayer_(self, sender):
        """Desktop: every window covers it. Floating: it sits above them."""
        layer = "floating" if config.setting("cat_layer") == "desktop" else "desktop"
        config.set_setting("cat_layer", layer)
        self._raised = False
        if self.pet is not None:
            self.pet.set_layer(layer)
            self.pet.show()
        log.info("cat layer is now %s", layer)

    def togglePause_(self, sender):
        self.paused_until = 0 if time.time() < self.paused_until else time.time() + 3600
        self.tick_(None)

    def syncCalendar_(self, sender):
        threading.Thread(target=self._sync_now, daemon=True).start()

    @objc.python_method
    def _sync_now(self):
        try:
            if calsync.authorization_status() == 0:
                AppHelper.callAfter(self.request_permissions_if_needed)
                return
            count, source = calsync.sync()
            log.info("manual sync: %s events from %s", count, source)
        except Exception:
            log.exception("manual calendar sync failed")

    def editRules_(self, sender):
        subprocess.Popen(["open", config.ensure_user_config()])

    def openFolder_(self, sender):
        subprocess.Popen(["open", DATA_DIR])

    def openPrivacy_(self, sender):
        pane = ("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
                if not tracker.accessibility_trusted()
                else "x-apple.systempreferences:com.apple.preference.security?Privacy_Calendars")
        subprocess.Popen(["open", pane])

    @objc.python_method
    def _finished_count(self):
        try:
            counts = pipeline.summary()["counts"]
            return counts.get("accepted", 0) + counts.get("done", 0)
        except Exception:
            return 0

    @objc.python_method
    def remember_pet_position(self):
        x, y = self.pet.origin()
        db.set_meta("pet_origin", "{},{}".format(int(x), int(y)))

    def quit_(self, sender):
        NSApplication.sharedApplication().terminate_(None)

    def applicationWillTerminate_(self, notification):
        try:
            self.tracker.stop()
            self.calendar.stop()
        except Exception:
            pass


def _clip(text, n):
    text = str(text or "")
    return text if len(text) <= n else text[: n - 1] + "…"


def _clock(ts):
    if not ts:
        return ""
    t = time.localtime(ts)
    hour = t.tm_hour % 12 or 12
    return "{}:{:02d}{}".format(hour, t.tm_min, "am" if t.tm_hour < 12 else "pm")


def main():
    setup_logging()
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = CatlendarDelegate.alloc().init()
    app.setDelegate_(delegate)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
