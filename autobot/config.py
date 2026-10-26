"""Settings, read from environment variables / the project's .env file.

Secrets (ANTHROPIC_API_KEY) are only ever read from the environment - never
stored in code or logs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# USD per million tokens: (input, output, 5-minute cache write, cache read)
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
    "claude-fable-5-1": (10.00, 50.00, 12.50, 0.25),
    "claude-opus-5": (5.00, 25.00, 6.25, 0.50),
}


def _str(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(os.path.expandvars(os.path.expanduser(raw))) if raw else default


@dataclass
class Config:
    # Model
    model: str = field(default_factory=lambda: _str("AUTOBOT_MODEL", "claude-opus-5-5"))
    effort: str = field(default_factory=lambda: _str("AUTOBOT_EFFORT", "medium"))
    max_tokens: int = field(default_factory=lambda: _int("AUTOBOT_MAX_TOKENS", 64000))
    fallbacks: bool = field(default_factory=lambda: _bool("AUTOBOT_FALLBACKS", True))
    progress_updates: bool = field(default_factory=lambda: _bool("AUTOBOT_PROGRESS_UPDATES", True))

    # Limits - the agent pauses and asks before going past these
    max_steps: int = field(default_factory=lambda: _int("AUTOBOT_MAX_STEPS", 300))
    budget_usd: float = field(default_factory=lambda: _float("AUTOBOT_BUDGET_USD", 5.0))
    # Start a fresh context segment (from progress notes) once a request gets this big
    rollover_tokens: int = field(default_factory=lambda: _int("AUTOBOT_ROLLOVER_TOKENS", 150_000))

    # Screen
    screenshot_width: int = field(default_factory=lambda: _int("AUTOBOT_SCREENSHOT_WIDTH", 1280))
    save_screenshots: bool = field(default_factory=lambda: _bool("AUTOBOT_SAVE_SCREENSHOTS", False))

    # Browser (Playwright)
    browser_channel: str = field(default_factory=lambda: _str("AUTOBOT_BROWSER_CHANNEL", "chrome"))
    browser_headless: bool = field(default_factory=lambda: _bool("AUTOBOT_BROWSER_HEADLESS", False))
    viewport_width: int = field(default_factory=lambda: _int("AUTOBOT_VIEWPORT_WIDTH", 1280))
    viewport_height: int = field(default_factory=lambda: _int("AUTOBOT_VIEWPORT_HEIGHT", 800))
    browser_js: bool = field(default_factory=lambda: _bool("AUTOBOT_BROWSER_JS", True))

    # Shell confirmation: "risky" (destructive-looking commands), "always", or "never"
    shell_confirm: str = field(default_factory=lambda: _str("AUTOBOT_SHELL_CONFIRM", "risky"))

    # Emergency controls (global hotkeys, pynput syntax)
    hotkeys: bool = field(default_factory=lambda: _bool("AUTOBOT_HOTKEYS", True))
    pause_hotkey: str = field(default_factory=lambda: _str("AUTOBOT_PAUSE_HOTKEY", "<ctrl>+<alt>+p"))
    stop_hotkey: str = field(default_factory=lambda: _str("AUTOBOT_STOP_HOTKEY", "<ctrl>+<alt>+x"))

    # Folders
    output_dir: Path = field(default_factory=lambda: _path("AUTOBOT_OUTPUT_DIR", ROOT / "output"))
    downloads_dir: Path = field(default_factory=lambda: _path("AUTOBOT_DOWNLOADS_DIR", Path.home() / "Downloads"))
    runs_dir: Path = field(default_factory=lambda: ROOT / "runs")
    data_dir: Path = field(default_factory=lambda: ROOT / "data")

    @property
    def browser_profile_dir(self) -> Path:
        return self.data_dir / "browser-profile"

    @property
    def memory_file(self) -> Path:
        return self.data_dir / "memory.md"

    @property
    def schedules_dir(self) -> Path:
        return ROOT / "schedules"

    def ensure_dirs(self) -> None:
        for d in (self.output_dir, self.runs_dir, self.data_dir, self.schedules_dir):
            d.mkdir(parents=True, exist_ok=True)

    def cost(self, usage) -> float:
        """Dollar cost of one response's usage block (approximate under fallbacks)."""
        p_in, p_out, p_cw, p_cr = PRICES.get(self.model, PRICES["claude-opus-5-5"])
        return (
            (getattr(usage, "input_tokens", 0) or 0) * p_in
            + (getattr(usage, "output_tokens", 0) or 0) * p_out
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * p_cw
            + (getattr(usage, "cache_read_input_tokens", 0) or 0) * p_cr
        ) / 1_000_000
