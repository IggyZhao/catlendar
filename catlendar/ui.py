"""The floating cat, the menu bar item and the dashboard window."""
import json
import logging
import os
import threading

import objc
from AppKit import (
    NSApp, NSBackingStoreBuffered, NSColor, NSEvent, NSImage, NSMakeRect, NSMenu,
    NSMenuItem, NSScreen, NSView, NSWindow, NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary, NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless, NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable, NSWindowStyleMaskTitled, NSFloatingWindowLevel,
)
from Foundation import NSURL, NSObject
from PyObjCTools import AppHelper
from WebKit import WKWebView, WKWebViewConfiguration

log = logging.getLogger("catlendar.ui")

PET_W, PET_H = 196, 214


def _js(value):
    """A JS string literal that survives quotes, newlines and non-ascii."""
    return json.dumps(str(value))


def _cursor_screen():
    """The display the mouse is on, so the cat shows up where you are looking."""
    loc = NSEvent.mouseLocation()
    for screen in NSScreen.screens():
        f = screen.frame()
        if (f.origin.x <= loc.x <= f.origin.x + f.size.width
                and f.origin.y <= loc.y <= f.origin.y + f.size.height):
            return screen
    return NSScreen.mainScreen()


class DragView(NSView):
    """Transparent lid over the cat: drag to move, click to open the dashboard."""

    def initWithFrame_handlers_(self, frame, handlers):
        self = objc.super(DragView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.handlers = handlers
        self._down = None
        self._moved = False
        self._pending = None      # a single click waiting to see if a second lands
        return self

    def mouseDown_(self, event):
        self._down = NSEvent.mouseLocation()
        self._origin = self.window().frame().origin
        self._moved = False

    def mouseDragged_(self, event):
        if self._down is None:
            return
        now = NSEvent.mouseLocation()
        dx, dy = now.x - self._down.x, now.y - self._down.y
        if abs(dx) > 3 or abs(dy) > 3:
            self._moved = True
        self.window().setFrameOrigin_((self._origin.x + dx, self._origin.y + dy))

    def mouseUp_(self, event):
        if self._down is not None and not self._moved:
            if event.clickCount() >= 2:
                self._pending = None          # cancel the pending single click
                self.handlers.get("double", lambda: None)()
            else:
                # hold the single click briefly, in case a second one is coming,
                # so a double click does not also open the dashboard
                token = object()
                self._pending = token

                def fire():
                    if self._pending is token:
                        self._pending = None
                        self.handlers.get("click", lambda: None)()

                timer = threading.Timer(0.28, lambda: AppHelper.callAfter(fire))
                timer.daemon = True
                timer.start()
        elif self._moved:
            self.handlers.get("moved", lambda: None)()
        self._down = None

    def rightMouseDown_(self, event):
        menu = self.handlers.get("menu", lambda: None)()
        if menu is not None:
            NSMenu.popUpContextMenu_withEvent_forView_(menu, event, self)


class PetWindow(NSObject):
    """A borderless, always-on-top window holding the animated cat."""

    def initWithAssets_handlers_(self, assets_dir, handlers):
        self = objc.super(PetWindow, self).init()
        if self is None:
            return None
        screen = _cursor_screen().visibleFrame()
        rect = NSMakeRect(screen.origin.x + screen.size.width - PET_W - 26,
                          screen.origin.y + 36, PET_W, PET_H)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False)
        win.setOpaque_(False)
        win.setBackgroundColor_(NSColor.clearColor())
        win.setHasShadow_(False)
        win.setLevel_(NSFloatingWindowLevel)
        win.setIgnoresMouseEvents_(False)
        win.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary)

        conf = WKWebViewConfiguration.alloc().init()
        web = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, PET_W, PET_H), conf)
        web.setValue_forKey_(False, "drawsBackground")
        path = os.path.join(assets_dir, "cat.html")
        web.loadFileURL_allowingReadAccessToURL_(
            NSURL.fileURLWithPath_(path), NSURL.fileURLWithPath_(assets_dir))

        lid = DragView.alloc().initWithFrame_handlers_(NSMakeRect(0, 0, PET_W, PET_H), handlers)
        container = win.contentView()
        container.addSubview_(web)
        container.addSubview_(lid)

        self.window = win
        self.web = web
        self.ready = False
        return self

    @objc.python_method
    def show(self):
        self.window.orderFrontRegardless()

    @objc.python_method
    def hide(self):
        self.window.orderOut_(None)

    @objc.python_method
    def visible(self):
        return bool(self.window.isVisible())

    @objc.python_method
    def move_to(self, x, y):
        """Ignore a saved spot that now sits on a display that is gone."""
        if x is None or y is None:
            return
        x, y = float(x), float(y)
        for screen in NSScreen.screens():
            f = screen.frame()
            if (f.origin.x - 40 <= x <= f.origin.x + f.size.width - 40
                    and f.origin.y - 40 <= y <= f.origin.y + f.size.height - 40):
                self.window.setFrameOrigin_((x, y))
                return
        log.info("saved cat position %s,%s is off screen, keeping the default", x, y)

    @objc.python_method
    def origin(self):
        o = self.window.frame().origin
        return o.x, o.y

    @objc.python_method
    def update(self, state, main, sub, night=False):
        js = "window.setCat && window.setCat({},{},{},{});".format(
            _js(state), _js(main or ""), _js(sub or ""), "true" if night else "false")
        self.web.evaluateJavaScript_completionHandler_(js, None)

    @objc.python_method
    def move(self, name):
        """One-off animations: celebrate, groom, stretch, feed."""
        self.web.evaluateJavaScript_completionHandler_(
            "window.catMove && window.catMove({});".format(_js(name)), None)


def _plain(value):
    """Objective-C containers into plain Python, so JSON-ish payloads are usable."""
    if isinstance(value, dict) or hasattr(value, "allKeys"):
        return {str(k): _plain(value[k]) for k in value}
    if isinstance(value, (list, tuple)) or (hasattr(value, "count") and hasattr(value, "objectAtIndex_")):
        return [_plain(v) for v in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


class PageBridge(NSObject):
    """Receives window.webkit.messageHandlers.catlendar.postMessage(...)."""

    def initWithCallback_(self, callback):
        self = objc.super(PageBridge, self).init()
        if self is None:
            return None
        self.callback = callback
        return self

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        try:
            self.callback(_plain(message.body()))
        except Exception:
            log.exception("page message failed")


class DashboardWindow(NSObject):
    def initWithTitle_bridge_(self, title, on_message):
        self = objc.super(DashboardWindow, self).init()
        if self is None:
            return None
        screen = _cursor_screen().visibleFrame()
        # open wide by default; the frame autosave below remembers whatever you
        # resize it to afterwards
        w = min(1760, screen.size.width - 80)
        h = min(1040, screen.size.height - 70)
        rect = NSMakeRect(screen.origin.x + (screen.size.width - w) / 2,
                          screen.origin.y + (screen.size.height - h) / 2, w, h)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect,
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable,
            NSBackingStoreBuffered, False)
        win.setTitle_(title)
        win.setReleasedWhenClosed_(False)
        win.setMinSize_((880, 560))
        win.setBackgroundColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(0.055, 0.055, 0.075, 1.0))
        conf = WKWebViewConfiguration.alloc().init()
        self.bridge = None
        if on_message is not None:
            self.bridge = PageBridge.alloc().initWithCallback_(on_message)
            conf.userContentController().addScriptMessageHandler_name_(self.bridge, "catlendar")
        web = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, rect.size.width, rect.size.height), conf)
        web.setAutoresizingMask_(2 | 16)  # width | height
        win.contentView().addSubview_(web)
        # AppKit persists the frame under this name, so the window comes back
        # the size and place you left it
        win.setFrameAutosaveName_("CatlendarDashboard")
        self.window = win
        self.web = web
        self._loaded_path = None
        return self

    @objc.python_method
    def load(self, path):
        url = NSURL.fileURLWithPath_(path)
        self.web.loadFileURL_allowingReadAccessToURL_(
            url, NSURL.fileURLWithPath_(os.path.dirname(path)))
        self._loaded_path = path

    @objc.python_method
    def reload(self):
        if self._loaded_path:
            self.load(self._loaded_path)

    @objc.python_method
    def show(self):
        NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    @objc.python_method
    def visible(self):
        return bool(self.window.isVisible())


def menu_item(title, target, action, key="", enabled=True):
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
    item.setTarget_(target)
    item.setEnabled_(enabled)
    return item


def separator():
    return NSMenuItem.separatorItem()


def status_image():
    """A cat glyph for the menu bar, falling back if the symbol is unavailable."""
    for name in ("cat.fill", "cat", "pawprint.fill"):
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, "Catlendar")
        if img is not None:
            img.setTemplate_(True)
            return img
    return None
