"""Pause / resume / stop signals shared by the console, hotkeys and the agent."""

from __future__ import annotations

import threading
import time
from typing import Callable


class StopRequested(Exception):
    """Raised inside the agent when the user asked it to stop."""


class Control:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._paused_total = 0.0  # seconds paused since the agent last read it
        self._hotkeys = None

    # --- signals -----------------------------------------------------------
    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def request_stop(self) -> None:
        self._stop.set()
        self._paused.clear()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def toggle_pause(self) -> bool:
        if self.paused:
            self.resume()
        else:
            self.pause()
        return self.paused

    def reset(self) -> None:
        self._stop.clear()
        self._paused.clear()
        self._paused_total = 0.0

    # --- used by the agent -------------------------------------------------
    def checkpoint(self) -> None:
        """Block while paused; raise StopRequested if a stop was requested."""
        if self._stop.is_set():
            raise StopRequested()
        if self._paused.is_set():
            started = time.monotonic()
            while self._paused.is_set() and not self._stop.is_set():
                time.sleep(0.1)
            self._paused_total += time.monotonic() - started
            if self._stop.is_set():
                raise StopRequested()

    def sleep(self, seconds: float) -> None:
        """Sleep that wakes up early for stop and waits out a pause."""
        end = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < end:
            self.checkpoint()
            time.sleep(min(0.1, max(0.0, end - time.monotonic())))
        self.checkpoint()

    def take_pause_note(self) -> str | None:
        """A note for Claude if it was paused since the last call, else None."""
        if self._paused_total < 1:
            return None
        minutes = self._paused_total / 60
        self._paused_total = 0.0
        span = f"{minutes:.0f} min" if minutes >= 1 else "a few seconds"
        return (
            f"[The user paused you for {span}. They may have changed the screen or "
            "windows meanwhile - re-check the current state before continuing.]"
        )

    # --- global hotkeys ----------------------------------------------------
    def start_hotkeys(self, pause_combo: str, stop_combo: str,
                      on_event: Callable[[str], None] | None = None) -> bool:
        try:
            from pynput import keyboard
        except Exception:
            return False

        def _pause() -> None:
            state = "paused" if self.toggle_pause() else "resumed"
            if on_event:
                on_event(state)

        def _stop() -> None:
            self.request_stop()
            if on_event:
                on_event("stop")

        try:
            self._hotkeys = keyboard.GlobalHotKeys({pause_combo: _pause, stop_combo: _stop})
            self._hotkeys.daemon = True
            self._hotkeys.start()
            return True
        except Exception:
            self._hotkeys = None
            return False
