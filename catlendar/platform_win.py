"""Windows support, through ctypes only, so there is nothing to install."""
import ctypes
import ctypes.wintypes as wt
import logging

from .platform_base import Platform

log = logging.getLogger("catlendar.windows")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


class WindowsPlatform(Platform):
    name = "windows"

    def idle_seconds(self):
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        # both are millisecond tick counts that wrap about every 49 days
        elapsed = (kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
        return elapsed / 1000.0

    def screen_locked(self):
        """No direct API, so use the proxy Windows itself uses: on a locked
        desktop the foreground window belongs to the secure desktop and there is
        no foreground window we can read."""
        return user32.GetForegroundWindow() == 0

    def frontmost(self):
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None, None, None
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return self._process_name(pid.value), None, pid.value

    def window_title(self, pid=None):
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return None
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value or None

    def presenting(self, skip_pid=None):
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if skip_pid is not None and pid.value == skip_pid:
            return False
        rect = wt.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        screen_w = user32.GetSystemMetrics(0)
        screen_h = user32.GetSystemMetrics(1)
        return abs(width - screen_w) <= 2 and abs(height - screen_h) <= 2

    def _process_name(self, pid):
        PROCESS_QUERY_LIMITED = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
        if not handle:
            return None
        try:
            buf = ctypes.create_unicode_buffer(512)
            size = wt.DWORD(512)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                name = buf.value.rsplit("\\", 1)[-1]
                return name[:-4] if name.lower().endswith(".exe") else name
        finally:
            kernel32.CloseHandle(handle)
        return None
