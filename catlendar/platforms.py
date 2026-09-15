"""Pick the platform module once, at import."""
import logging

from .paths import IS_MAC, IS_WINDOWS
from .platform_base import Platform

log = logging.getLogger("catlendar.platform")


def _load():
    if IS_MAC:
        try:
            from .platform_mac import MacPlatform
            return MacPlatform()
        except Exception:
            log.exception("macOS support failed to load; falling back")
    elif IS_WINDOWS:
        try:
            from .platform_win import WindowsPlatform
            return WindowsPlatform()
        except Exception:
            log.exception("Windows support failed to load; falling back")
    return Platform()


current = _load()
