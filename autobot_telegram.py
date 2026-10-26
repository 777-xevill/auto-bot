"""Entry point for the Telegram bot (also what Task Scheduler runs, windowless, at logon)."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

if sys.stdout is None or sys.stderr is None:   # pythonw has no console: log to a file instead
    (ROOT / "data").mkdir(exist_ok=True)
    log = open(ROOT / "data" / "telegram-console.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or log
    sys.stderr = sys.stderr or log

from autobot.telegram_bot import main  # noqa: E402

sys.exit(main())
