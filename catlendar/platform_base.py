"""What Catlendar needs from an operating system.

Each platform module provides these. Anything it cannot do returns a neutral
value rather than raising, so the rest of the app carries on with fewer signals
instead of falling over.
"""


class Platform:
    name = "unknown"

    def idle_seconds(self):
        """Seconds since the last keyboard or mouse input."""
        return 0.0

    def screen_locked(self):
        return False

    def frontmost(self):
        """(app name, app id, pid) of the app in front."""
        return None, None, None

    def window_title(self, pid):
        """Title of that app's focused window, or None if unavailable."""
        return None

    def presenting(self, skip_pid=None):
        """True when the front app has a window the size of a whole screen."""
        return False

    def can_read_titles(self):
        """False when the OS is withholding window titles, so the app can say so."""
        return True

    def permission_hint(self):
        return ""
