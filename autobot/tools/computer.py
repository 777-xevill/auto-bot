"""Desktop control: executes Claude's computer toolset on the real Windows screen.

Screenshots are downscaled (default 1280 px wide) and Claude's coordinates
are scaled back up. Typing uses Unicode SendInput so any language works and
the clipboard is never touched.
"""

from __future__ import annotations

import base64
import ctypes
import io
import time
from contextlib import contextmanager
from ctypes import wintypes
from typing import Any

import mss
import pyautogui
from PIL import Image, ImageDraw

from ..control import StopRequested
from .base import Capability, Output, ToolContext, ToolError


def _make_dpi_aware() -> None:
    """Make screen capture and mouse coordinates use real pixels on scaled displays."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_make_dpi_aware()
pyautogui.FAILSAFE = False  # replaced by our own corner check (see _check_failsafe)
pyautogui.PAUSE = 0.02

# --- Unicode typing via SendInput ---------------------------------------------
_ULONG_PTR = ctypes.c_size_t
_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_HWHEEL = 0x1000


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send_unicode(ch: str) -> None:
    data = ch.encode("utf-16-le")
    events = []
    for i in range(0, len(data), 2):
        unit = int.from_bytes(data[i:i + 2], "little")
        for flags in (_KEYEVENTF_UNICODE, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP):
            ev = _INPUT(type=_INPUT_KEYBOARD)
            ev.u.ki = _KEYBDINPUT(0, unit, flags, 0, 0)
            events.append(ev)
    arr = (_INPUT * len(events))(*events)
    ctypes.windll.user32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))


def _wheel(direction: str, notches: int) -> None:
    vertical = direction in ("up", "down")
    flag = _MOUSEEVENTF_WHEEL if vertical else _MOUSEEVENTF_HWHEEL
    delta = 120 if direction in ("up", "right") else -120
    for _ in range(notches):
        ctypes.windll.user32.mouse_event(flag, 0, 0, delta, 0)  # negative wraps to DWORD
        time.sleep(0.03)


# --- key names: xdotool-style (what Claude sends) -> pyautogui ---------------
_KEY_ALIASES = {
    "return": "enter", "kp_enter": "enter", "escape": "esc", "back_space": "backspace",
    "del": "delete", "iso_left_tab": "tab", "page_up": "pageup", "prior": "pageup",
    "page_down": "pagedown", "next": "pagedown", "super": "win", "super_l": "winleft",
    "super_r": "winright", "windows": "win", "cmd": "win", "command": "win", "meta": "win",
    "control": "ctrl", "control_l": "ctrlleft", "control_r": "ctrlright", "alt_l": "altleft",
    "alt_r": "altright", "option": "alt", "shift_l": "shiftleft", "shift_r": "shiftright",
    "menu": "apps", "print": "printscreen", "caps_lock": "capslock", "num_lock": "numlock",
    "scroll_lock": "scrolllock", "minus": "-", "plus": "+", "equal": "=", "comma": ",",
    "period": ".", "slash": "/", "backslash": "\\", "semicolon": ";", "apostrophe": "'",
    "quoteright": "'", "grave": "`", "bracketleft": "[", "bracketright": "]", "asterisk": "*",
    "xf86audioraisevolume": "volumeup", "xf86audiolowervolume": "volumedown",
    "xf86audiomute": "volumemute", "xf86audioplay": "playpause", "xf86audionext": "nexttrack",
    "xf86audioprev": "prevtrack",
}


def _key_name(raw: str) -> str:
    k = raw.strip()
    if not k:
        return "space"
    if len(k) == 1:
        return k.lower() if k.isalpha() else k
    name = _KEY_ALIASES.get(k.lower(), k.lower())
    if name not in pyautogui.KEYBOARD_KEYS:
        raise ToolError(f"Unknown key '{raw}'.")
    return name


def _combos(text: str) -> list[list[str]]:
    """'ctrl+a BackSpace' -> [['ctrl','a'], ['backspace']]"""
    text = text.strip()
    if text in ("+", " "):
        return [[_key_name(text)]]
    return [[_key_name(part) for part in combo.split("+") if part != ""] or ["+"]
            for combo in text.split()]


class ComputerTools(Capability):
    toolset = "computer"

    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        with mss.mss() as sct:
            mon = sct.monitors[1]  # primary display
        self.mon = {k: mon[k] for k in ("left", "top", "width", "height")}
        self.scale = min(1.0, cfg.screenshot_width / self.mon["width"])
        self.shot_w = round(self.mon["width"] * self.scale)
        self.shot_h = round(self.mon["height"] * self.scale)
        self._agent_pos: tuple[int, int] | None = None
        self._shots = 0

    def definitions(self) -> list[dict]:
        return [{"type": "computer_toolset_20260801"}]

    @property
    def screen_summary(self) -> str:
        if self.scale < 1:
            return (f"{self.mon['width']}x{self.mon['height']} - screenshots arrive scaled to "
                    f"{self.shot_w}x{self.shot_h}; always use screenshot coordinates")
        return f"{self.mon['width']}x{self.mon['height']}"

    # --- coordinates -------------------------------------------------------
    def _to_screen(self, coord: Any) -> tuple[int, int]:
        if not (isinstance(coord, (list, tuple)) and len(coord) == 2):
            raise ToolError("coordinate must be [x, y].")
        x, y = float(coord[0]), float(coord[1])
        if not (0 <= x <= self.shot_w and 0 <= y <= self.shot_h):
            raise ToolError(f"Coordinate ({x:.0f}, {y:.0f}) is outside the screen "
                            f"({self.shot_w}x{self.shot_h}).")
        sx = self.mon["left"] + min(self.mon["width"] - 1, round(x / self.scale))
        sy = self.mon["top"] + min(self.mon["height"] - 1, round(y / self.scale))
        return sx, sy

    def _from_screen(self, x: int, y: int) -> tuple[int, int]:
        return round((x - self.mon["left"]) * self.scale), round((y - self.mon["top"]) * self.scale)

    # --- capture -----------------------------------------------------------
    def _grab(self) -> Image.Image:
        with mss.mss() as sct:
            raw = sct.grab(self.mon)
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

    def _draw_cursor(self, img: Image.Image) -> None:
        x, y = pyautogui.position()
        x, y = x - self.mon["left"], y - self.mon["top"]
        if not (0 <= x < img.width and 0 <= y < img.height):
            return
        s = max(1.0, img.width / 1280) * 1.2
        arrow = [(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)]
        pts = [(x + px * s, y + py * s) for px, py in arrow]
        draw = ImageDraw.Draw(img)
        draw.polygon(pts, fill="white", outline="black")

    def _image_block(self, img: Image.Image, ctx: ToolContext | None = None) -> list[dict]:
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=False)
        if ctx and self.cfg.save_screenshots:
            shots = ctx.run.dir / "screens"
            shots.mkdir(exist_ok=True)
            self._shots += 1
            (shots / f"{self._shots:04d}.png").write_bytes(buf.getvalue())
        data = base64.standard_b64encode(buf.getvalue()).decode()
        return [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}]

    def screenshot(self, ctx: ToolContext | None = None) -> list[dict]:
        img = self._grab()
        self._draw_cursor(img)
        if self.scale < 1:
            img = img.resize((self.shot_w, self.shot_h), Image.LANCZOS)
        return self._image_block(img, ctx)

    def _zoom(self, region: Any, ctx: ToolContext) -> list[dict]:
        if not (isinstance(region, (list, tuple)) and len(region) == 4):
            raise ToolError("region must be [x0, y0, x1, y1].")
        x0, y0 = self._to_screen(region[:2])
        x1, y1 = self._to_screen(region[2:])
        if x1 <= x0 or y1 <= y0:
            raise ToolError("region must have x1 > x0 and y1 > y0.")
        img = self._grab()
        self._draw_cursor(img)
        crop = img.crop((x0 - self.mon["left"], y0 - self.mon["top"],
                         x1 - self.mon["left"], y1 - self.mon["top"]))
        ratio = min(self.shot_w / crop.width, self.shot_h / crop.height)
        crop = crop.resize((max(1, round(crop.width * ratio)), max(1, round(crop.height * ratio))),
                           Image.LANCZOS)
        return self._image_block(crop, ctx)

    # --- safety ------------------------------------------------------------
    def _check_failsafe(self, ctx: ToolContext) -> None:
        """Mouse slammed into the top-left corner by the user = emergency stop."""
        x, y = pyautogui.position()
        in_corner = x <= self.mon["left"] + 2 and y <= self.mon["top"] + 2
        if in_corner and self._agent_pos != (x, y):
            ctx.control.request_stop()
            ctx.ui.warn("Mouse moved to the top-left corner - emergency stop.")
            raise StopRequested()

    @contextmanager
    def _modifiers(self, text: str | None):
        keys = [_key_name(k) for k in (text or "").split("+") if k.strip()]
        for k in keys:
            pyautogui.keyDown(k)
        try:
            yield
        finally:
            for k in reversed(keys):
                pyautogui.keyUp(k)

    # --- dispatch ----------------------------------------------------------
    def execute(self, name: str, args: dict[str, Any], ctx: ToolContext) -> Output:
        self._check_failsafe(ctx)
        result: Output = "OK"

        if name == "screenshot":
            return self.screenshot(ctx)
        if name == "zoom":
            return self._zoom(args.get("region"), ctx)

        if name in ("left_click", "right_click", "middle_click", "double_click", "triple_click"):
            button = {"right_click": "right", "middle_click": "middle"}.get(name, "left")
            clicks = {"double_click": 2, "triple_click": 3}.get(name, 1)
            pos = self._to_screen(args["coordinate"]) if args.get("coordinate") is not None else None
            with self._modifiers(args.get("text")):
                if pos:
                    pyautogui.moveTo(*pos)
                pyautogui.click(button=button, clicks=clicks, interval=0.08)
            time.sleep(0.25)

        elif name == "left_click_drag":
            start = self._to_screen(args["start_coordinate"])
            end = self._to_screen(args["coordinate"])
            with self._modifiers(args.get("text")):
                pyautogui.moveTo(*start)
                pyautogui.mouseDown()
                pyautogui.moveTo(*end, duration=0.4)
                pyautogui.mouseUp()

        elif name == "mouse_move":
            pyautogui.moveTo(*self._to_screen(args["coordinate"]))

        elif name == "left_mouse_down":
            pyautogui.mouseDown()

        elif name == "left_mouse_up":
            pyautogui.mouseUp()

        elif name == "cursor_position":
            x, y = self._from_screen(*pyautogui.position())
            result = f"Cursor is at x={x}, y={y}"

        elif name == "scroll":
            direction = str(args.get("scroll_direction", "down")).lower()
            if direction not in ("up", "down", "left", "right"):
                raise ToolError("scroll_direction must be up, down, left or right.")
            amount = max(1, min(50, int(args.get("scroll_amount") or 3)))
            if args.get("coordinate") is not None:
                pyautogui.moveTo(*self._to_screen(args["coordinate"]))
            with self._modifiers(args.get("text")):
                _wheel(direction, amount)
            time.sleep(0.2)

        elif name == "type":
            self._type(str(args.get("text", "")), ctx)

        elif name == "key":
            repeat = max(1, min(100, int(args.get("repeat") or 1)))
            combos = _combos(str(args.get("text", "")))
            for _ in range(repeat):
                for combo in combos:
                    ctx.control.checkpoint()
                    if len(combo) == 1:
                        pyautogui.press(combo[0])
                    else:
                        pyautogui.hotkey(*combo, interval=0.03)
                    time.sleep(0.05)
            time.sleep(0.2)

        elif name == "hold_key":
            keys = [k for combo in _combos(str(args.get("text", ""))) for k in combo]
            duration = max(0.0, min(300.0, float(args.get("duration") or 1)))
            for k in keys:
                pyautogui.keyDown(k)
            try:
                ctx.control.sleep(duration)
            finally:
                for k in reversed(keys):
                    pyautogui.keyUp(k)

        elif name == "wait":
            ctx.control.sleep(max(0.0, min(300.0, float(args.get("duration") or 1))))

        else:
            raise ToolError(f"Unsupported computer action '{name}'.")

        self._agent_pos = pyautogui.position()
        return result

    def _type(self, text: str, ctx: ToolContext) -> None:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        for i, ch in enumerate(text):
            if i % 25 == 0:
                ctx.control.checkpoint()
            if ch == "\n":
                pyautogui.press("enter")
            elif ch == "\t":
                pyautogui.press("tab")
            else:
                _send_unicode(ch)
            time.sleep(0.008)

    def describe(self, name: str, args: dict[str, Any]) -> str:
        coord = args.get("coordinate")
        at = f" ({coord[0]}, {coord[1]})" if isinstance(coord, (list, tuple)) and len(coord) == 2 else ""
        mods = f" +{args['text']}" if args.get("text") and name.endswith("click") else ""
        if name == "type":
            t = str(args.get("text", "")).replace("\n", "⏎")
            return f"type \"{t[:60]}{'…' if len(t) > 60 else ''}\""
        if name == "key":
            rep = f" ×{args['repeat']}" if args.get("repeat") else ""
            return f"key {args.get('text', '')}{rep}"
        if name == "scroll":
            return f"scroll {args.get('scroll_direction')} ×{args.get('scroll_amount', 3)}{at}"
        if name == "zoom":
            return f"zoom {args.get('region')}"
        if name == "left_click_drag":
            return f"drag {args.get('start_coordinate')} → {coord}"
        if name in ("wait", "hold_key"):
            return f"{name} {args.get('text', '')} {args.get('duration', '')}s".replace("  ", " ")
        return f"{name.replace('_', ' ')}{at}{mods}"
