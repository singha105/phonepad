"""Synthetic keyboard and mouse input for macOS (Quartz), plus a dry-run stand-in.

Key names are lowercase strings: "a".."z", "0".."9", "space", "return", "tab",
"escape", "backspace", "left"/"right"/"up"/"down", "shift", "control",
"option", "command", the mouse buttons "mouse_left" / "mouse_right", and
"wheel_up" / "wheel_down" (one scroll notch per press).
"""
import sys
import time

# macOS virtual key codes, US layout (HIToolbox/Events.h).
KEYCODES = {
    "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05, "z": 0x06,
    "x": 0x07, "c": 0x08, "v": 0x09, "b": 0x0B, "q": 0x0C, "w": 0x0D, "e": 0x0E,
    "r": 0x0F, "y": 0x10, "t": 0x11, "o": 0x1F, "u": 0x20, "i": 0x22, "p": 0x23,
    "l": 0x25, "j": 0x26, "k": 0x28, "n": 0x2D, "m": 0x2E,
    "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "5": 0x17, "6": 0x16, "7": 0x1A,
    "8": 0x1C, "9": 0x19, "0": 0x1D,
    "return": 0x24, "tab": 0x30, "space": 0x31, "backspace": 0x33, "escape": 0x35,
    "left": 0x7B, "right": 0x7C, "down": 0x7D, "up": 0x7E,
    "command": 0x37, "shift": 0x38, "option": 0x3A, "control": 0x3B,
}
MODIFIERS = frozenset({"shift", "control", "option", "command"})
MOUSE_BUTTONS = frozenset({"mouse_left", "mouse_right"})
WHEEL = {"wheel_up": 1, "wheel_down": -1}
KEY_NAMES = frozenset(KEYCODES) | MOUSE_BUTTONS | frozenset(WHEEL)


class QuartzBackend:
    """Posts real events into the HID event stream, as if from hardware."""

    name = "quartz"

    def __init__(self, edge="recenter"):
        import Quartz  # pyobjc-framework-Quartz

        self.Q = Quartz
        self.edge = edge
        # Events stamped with the HID system source look like real hardware and
        # keep the system's key-state table in sync (games that poll it see them).
        self._src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        self._mod_masks = {
            "shift": Quartz.kCGEventFlagMaskShift,
            "control": Quartz.kCGEventFlagMaskControl,
            "option": Quartz.kCGEventFlagMaskAlternate,
            "command": Quartz.kCGEventFlagMaskCommand,
        }
        self._mods = set()
        self._buttons = set()
        self._displays = []
        self._displays_at = 0.0
        self._window = None
        self._window_at = 0.0

    def press(self, name):
        self._key(name, True)

    def release(self, name):
        self._key(name, False)

    def move(self, dx, dy):
        """Relative mouse move, like a real mouse in a game.

        Games that lock the camera (first person, Shift Lock) read the delta
        fields, not the cursor position, so the cursor itself is kept inside the
        game window: when a move would leave it, the cursor jumps back to the
        window's centre while the deltas still carry the full movement.
        """
        Q = self.Q
        if "mouse_left" in self._buttons:
            etype, btn = Q.kCGEventLeftMouseDragged, Q.kCGMouseButtonLeft
        elif "mouse_right" in self._buttons:
            etype, btn = Q.kCGEventRightMouseDragged, Q.kCGMouseButtonRight
        else:
            etype, btn = Q.kCGEventMouseMoved, Q.kCGMouseButtonLeft
        pos = self._place(self._cursor(), dx, dy)
        ev = Q.CGEventCreateMouseEvent(self._src, etype, pos, btn)
        Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventDeltaX, int(dx))
        Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventDeltaY, int(dy))
        self._post(ev)

    def scroll(self, lines):
        """Mouse wheel; positive scrolls up."""
        Q = self.Q
        ev = Q.CGEventCreateScrollWheelEvent(self._src, Q.kCGScrollEventUnitLine, 1, int(lines))
        self._post(ev)

    def _flags(self):
        flags = self.Q.kCGEventFlagMaskNonCoalesced
        for mod in self._mods:
            flags |= self._mod_masks[mod]
        return flags

    def _post(self, ev):
        # Every event carries the current modifier state, so a held ⇧ applies
        # to clicks and mouse moves too, and a released one never sticks.
        self.Q.CGEventSetFlags(ev, self._flags())
        self.Q.CGEventPost(self.Q.kCGHIDEventTap, ev)

    def _key(self, name, down):
        if name in MOUSE_BUTTONS:
            self._button(name, down)
            return
        Q = self.Q
        ev = Q.CGEventCreateKeyboardEvent(self._src, KEYCODES[name], down)
        if name in MODIFIERS:
            # Modifiers are flag changes, not key down/up, as far as apps care.
            if down:
                self._mods.add(name)
            else:
                self._mods.discard(name)
            Q.CGEventSetType(ev, Q.kCGEventFlagsChanged)
        self._post(ev)

    def _button(self, name, down):
        Q = self.Q
        left = name == "mouse_left"
        if down:
            self._buttons.add(name)
        else:
            self._buttons.discard(name)
        if left:
            etype = Q.kCGEventLeftMouseDown if down else Q.kCGEventLeftMouseUp
            btn = Q.kCGMouseButtonLeft
        else:
            etype = Q.kCGEventRightMouseDown if down else Q.kCGEventRightMouseUp
            btn = Q.kCGMouseButtonRight
        ev = Q.CGEventCreateMouseEvent(self._src, etype, self._cursor(), btn)
        Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventClickState, 1)
        self._post(ev)

    def _cursor(self):
        return self.Q.CGEventGetLocation(self.Q.CGEventCreate(None))

    def _place(self, cur, dx, dy):
        """Where the cursor goes for this move: never outside the game window."""
        x, y = cur.x + dx, cur.y + dy
        box = self._game_box(cur)
        if box is None:
            return (x, y)
        left, top, right, bottom = box
        if left <= x <= right and top <= y <= bottom:
            return (x, y)
        if self.edge == "recenter":
            return ((left + right) / 2, (top + bottom) / 2)
        return (min(max(x, left), right), min(max(y, top), bottom))

    def _game_box(self, cur):
        """(left, top, right, bottom) the cursor must stay inside: the frontmost
        app window minus its title bar, or the current display as a fallback."""
        now = time.monotonic()
        if now - self._window_at > 0.5:
            self._window = self._front_window()
            self._window_at = now
        if now - self._displays_at > 2.0:
            err, ids, count = self.Q.CGGetActiveDisplayList(16, None, None)
            self._displays = [] if err else [self.Q.CGDisplayBounds(i) for i in ids[:count]]
            self._displays_at = now

        displays = [(b.origin.x, b.origin.y, b.size.width, b.size.height) for b in self._displays]
        if self._window:
            x, y, w, h = self._window
            fullscreen = any(abs(x - dx) < 2 and abs(y - dy) < 2 and abs(w - dw) < 2 and abs(h - dh) < 2
                             for dx, dy, dw, dh in displays)
            top_inset = 8 if fullscreen else 34      # stay off the title bar
            return (x + 8, y + top_inset, x + w - 8, y + h - 8)
        for x, y, w, h in displays:
            if x <= cur.x < x + w and y <= cur.y < y + h:
                return (x + 8, y + 40, x + w - 8, y + h - 90)   # menu bar / Dock
        return None

    def _front_window(self):
        """Bounds of the frontmost normal window (the game, once it's clicked into)."""
        Q = self.Q
        try:
            infos = Q.CGWindowListCopyWindowInfo(
                Q.kCGWindowListOptionOnScreenOnly | Q.kCGWindowListExcludeDesktopElements,
                Q.kCGNullWindowID)
        except Exception:
            return None
        for info in infos or []:   # front to back
            if info.get("kCGWindowLayer") != 0 or not info.get("kCGWindowAlpha", 1):
                continue
            b = info.get("kCGWindowBounds") or {}
            w, h = b.get("Width", 0), b.get("Height", 0)
            if w >= 300 and h >= 200:
                return (b.get("X", 0), b.get("Y", 0), w, h)
        return None


class DryRunBackend:
    """Prints events instead of sending them. Used for testing."""

    name = "dry-run"

    def press(self, name):
        print(f"[dry-run] down {name}", flush=True)

    def release(self, name):
        print(f"[dry-run] up {name}", flush=True)

    def move(self, dx, dy):
        print(f"[dry-run] move {dx} {dy}", flush=True)

    def scroll(self, lines):
        print(f"[dry-run] scroll {lines}", flush=True)


def get_backend(dry_run=False, edge="recenter"):
    if dry_run:
        return DryRunBackend()
    if sys.platform != "darwin":
        raise SystemExit("Real input only works on macOS. Use --dry-run elsewhere.")
    return QuartzBackend(edge)


def accessibility_ok(prompt=True):
    """True if this process may post input events. With prompt=True, macOS shows
    its "would like to control this computer" dialog the first time."""
    if sys.platform != "darwin":
        return False
    try:
        from ApplicationServices import (AXIsProcessTrustedWithOptions,
                                         kAXTrustedCheckOptionPrompt)
    except ImportError:
        return False
    return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: prompt}))
