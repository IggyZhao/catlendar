"""macOS support. Window titles come from the Accessibility API, which the user
grants once in System Settings."""
import logging

import ApplicationServices as AS
import Quartz
from AppKit import NSScreen, NSWorkspace

from .platform_base import Platform

log = logging.getLogger("catlendar.macos")


class MacPlatform(Platform):
    name = "macos"

    def idle_seconds(self):
        return Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateHIDSystemState, Quartz.kCGAnyInputEventType)

    def screen_locked(self):
        info = Quartz.CGSessionCopyCurrentDictionary()
        return bool(info.get("CGSSessionScreenIsLocked", 0)) if info else False

    def frontmost(self):
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None, None, None
        return app.localizedName(), app.bundleIdentifier(), app.processIdentifier()

    def can_read_titles(self):
        return bool(AS.AXIsProcessTrusted())

    def request_titles(self):
        return bool(AS.AXIsProcessTrustedWithOptions({AS.kAXTrustedCheckOptionPrompt: True}))

    def permission_hint(self):
        return ("Open System Settings, Privacy & Security, Accessibility and switch "
                "Catlendar on, so window titles can be read.")

    def window_title(self, pid):
        if not pid:
            return None
        try:
            app_el = AS.AXUIElementCreateApplication(pid)
            AS.AXUIElementSetMessagingTimeout(app_el, 0.6)
            win = self._value(app_el, AS.kAXFocusedWindowAttribute) \
                or self._value(app_el, AS.kAXMainWindow)
            if win is None:
                windows = self._value(app_el, AS.kAXWindowsAttribute)
                win = windows[0] if windows and len(windows) else None
            if win is None:
                return None
            title = self._value(win, AS.kAXTitleAttribute)
            if not title:
                doc = self._value(win, AS.kAXDocumentAttribute)
                title = doc.split("/")[-1] if doc else None
            return str(title) if title else None
        except Exception:
            log.debug("could not read a window title", exc_info=True)
            return None

    def presenting(self, skip_pid=None):
        """A maximized window is not full screen. A maximized window sits below
        the menu bar; a full screen one matches the display exactly."""
        try:
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            if app is None:
                return False
            pid = app.processIdentifier()
            if skip_pid is not None and pid == skip_pid:
                return False
            sizes = [(s.frame().size.width, s.frame().size.height) for s in NSScreen.screens()]
            windows = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
            for win in windows:
                if win.get("kCGWindowOwnerPID") != pid:
                    continue
                bounds = win.get("kCGWindowBounds")
                if not bounds:
                    continue
                w, h = bounds.get("Width", 0), bounds.get("Height", 0)
                if any(abs(w - sw) <= 2 and abs(h - sh) <= 2 for sw, sh in sizes):
                    return True
        except Exception:
            log.debug("full screen check failed", exc_info=True)
        return False

    @staticmethod
    def _value(element, attribute):
        err, value = AS.AXUIElementCopyAttributeValue(element, attribute, None)
        return value if err == 0 else None
