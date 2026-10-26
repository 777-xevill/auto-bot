"""Recurring tasks through Windows Task Scheduler.

Each schedule stores its instruction in schedules/<name>.txt and a task
"auto-bot\\<name>" that runs `autobot.cmd scheduled <name>` unattended.
Tasks run only while the user is logged on, so the agent has a desktop.
"""

from __future__ import annotations

import csv
import io
import re
import subprocess
from typing import Any

from ..config import ROOT
from .base import Capability, Output, ToolContext, ToolError, custom_tool, validate

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DAYS = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True,
                          creationflags=_NO_WINDOW)


def create_schedule(cfg, name: str, instruction: str, frequency: str, time: str | None,
                    day: str | None) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name or ""):
        raise ToolError("name must be 1-40 letters, digits, '-' or '_'.")
    frequency = (frequency or "daily").lower()
    sc = {"daily": "DAILY", "weekly": "WEEKLY", "hourly": "HOURLY", "once": "ONCE",
          "logon": "ONLOGON"}.get(frequency)
    if not sc:
        raise ToolError("frequency must be daily, weekly, hourly, once or logon.")
    if sc in ("DAILY", "WEEKLY", "ONCE") and not (time and re.fullmatch(r"\d{2}:\d{2}", time)):
        raise ToolError("time (HH:MM, 24h) is required for daily, weekly and once.")
    cfg.schedules_dir.mkdir(parents=True, exist_ok=True)
    (cfg.schedules_dir / f"{name}.txt").write_text(instruction.strip(), encoding="utf-8")

    launcher = ROOT / "autobot.cmd"
    args = ["/Create", "/F", "/TN", f"auto-bot\\{name}", "/TR", f'"{launcher}" scheduled {name}',
            "/SC", sc]
    if sc == "WEEKLY":
        d = (day or "MON").upper()[:3]
        if d not in _DAYS:
            raise ToolError("day must be MON..SUN.")
        args += ["/D", d]
    if time and sc != "ONLOGON":
        args += ["/ST", time]
    res = _schtasks(*args)
    if res.returncode != 0:
        raise ToolError(f"Task Scheduler refused: {(res.stderr or res.stdout).strip()}")
    when = {"DAILY": f"every day at {time}", "WEEKLY": f"every {day or 'MON'} at {time}",
            "HOURLY": "every hour", "ONCE": f"once at {time}", "ONLOGON": "at every logon"}[sc]
    return f"Scheduled '{name}' to run {when}."


def install_telegram_autostart() -> str:
    """Start the Telegram bot hidden at every logon (it needs the desktop, so not at boot)."""
    import winreg  # per-user Run key: no admin rights needed (a logon task would need them)
    py = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    entry = ROOT / "autobot_telegram.py"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                        0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "auto-bot-telegram", 0, winreg.REG_SZ, f'"{py}" "{entry}"')
    return ("Installed: the Telegram bot will start (hidden) every time you log in to Windows. "
            "Remove it with: python -m autobot telegram-autostart-remove")


def remove_telegram_autostart() -> str:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, "auto-bot-telegram")
        return "Removed the Telegram autostart."
    except FileNotFoundError:
        return "Telegram autostart was not installed."


def list_schedules(cfg) -> list[dict]:
    out = []
    for f in sorted(cfg.schedules_dir.glob("*.txt")) if cfg.schedules_dir.exists() else []:
        name = f.stem
        info = {"name": name, "instruction": f.read_text(encoding="utf-8"), "next_run": "?",
                "status": "missing from Task Scheduler"}
        res = _schtasks("/Query", "/TN", f"auto-bot\\{name}", "/FO", "CSV", "/V")
        if res.returncode == 0:
            rows = list(csv.DictReader(io.StringIO(res.stdout)))
            if rows:
                info["next_run"] = rows[0].get("Next Run Time", "?")
                info["status"] = rows[0].get("Status", "?")
                info["last_run"] = rows[0].get("Last Run Time", "?")
        out.append(info)
    return out


def delete_schedule(cfg, name: str) -> str:
    res = _schtasks("/Delete", "/F", "/TN", f"auto-bot\\{name}")
    f = cfg.schedules_dir / f"{name}.txt"
    existed = f.exists()
    f.unlink(missing_ok=True)
    if res.returncode != 0 and not existed:
        raise ToolError(f"No schedule named '{name}'.")
    return f"Removed schedule '{name}'."


class ScheduleTool(Capability):
    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self._def = custom_tool(
            "manage_schedule",
            "Create, list or delete recurring auto-bot tasks (Windows Task Scheduler). A scheduled "
            "task runs your instruction unattended at the given time while the user is logged in. "
            "Write the instruction so it stands alone (include files, folders, criteria).",
            {
                "action": {"type": "string", "enum": ["create", "list", "delete"]},
                "name": {"type": "string", "description": "Short id, e.g. 'daily-leads'."},
                "instruction": {"type": "string", "description": "What to do each time it runs."},
                "frequency": {"type": "string", "enum": ["daily", "weekly", "hourly", "once", "logon"]},
                "time": {"type": "string", "description": "HH:MM, 24-hour local time."},
                "day": {"type": "string", "description": "For weekly: MON..SUN."},
            },
            ["action"],
        )

    def definitions(self) -> list[dict]:
        return [self._def]

    def execute(self, name: str, args: dict[str, Any], ctx: ToolContext) -> Output:
        validate(self._def, args)
        action = args["action"]
        if action == "list":
            items = list_schedules(self.cfg)
            if not items:
                return "No scheduled tasks."
            return "\n".join(f"- {i['name']}: next run {i['next_run']} ({i['status']}) - {i['instruction'][:150]}"
                             for i in items)
        if action == "delete":
            if not args.get("name"):
                raise ToolError("name is required.")
            return delete_schedule(self.cfg, args["name"])
        if action == "create":
            if not args.get("name") or not args.get("instruction"):
                raise ToolError("name and instruction are required.")
            if ctx.unattended:
                raise ToolError("Creating schedules needs the user's approval; not possible in an unattended run.")
            summary = (f"Create scheduled task '{args['name']}' ({args.get('frequency', 'daily')} "
                       f"{args.get('time') or ''}): {args['instruction'][:200]}")
            if not ctx.ui.confirm(summary):
                raise ToolError("The user declined to create this schedule.")
            return create_schedule(self.cfg, args["name"], args["instruction"],
                                   args.get("frequency", "daily"), args.get("time"), args.get("day"))
        raise ToolError("action must be create, list or delete.")

    def describe(self, name: str, args: dict[str, Any]) -> str:
        return f"schedule {args.get('action')} {args.get('name') or ''}".strip()
