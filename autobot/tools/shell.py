"""PowerShell commands. Destructive-looking commands need the user's OK."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .base import Capability, Output, ToolContext, ToolError, custom_tool, validate

_RISKY = re.compile(
    r"(?ix)("
    r"\b(remove-item|rm|rmdir|rd|del|erase|clear-content|clear-recyclebin)\b"
    r"|\bformat\s+[a-z]:|\bformat-volume\b|\bclear-disk\b|\bdiskpart\b|\binitialize-disk\b"
    r"|\b(stop-computer|restart-computer|shutdown|logoff)\b"
    r"|\breg(\.exe)?\s+(delete|add|import)\b|\b(remove|set|new)-itemproperty\b"
    r"|\bset-executionpolicy\b|\bbcdedit\b|\bcipher\s+/w\b|\btakeown\b|\bicacls\b"
    r"|\bnet\s+(user|localgroup)\b|\b(disable|enable)-netadapter\b"
    r"|\b(iex|invoke-expression)\b"
    r"|\bgit\s+(push\s+.*--force|reset\s+--hard|clean\s+-[a-z]*f)"
    r"|\bstop-process\b|\btaskkill\b"
    r"|\bsend-mailmessage\b"
    r")"
)

_MAX_OUTPUT = 30_000


class ShellTool(Capability):
    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self._exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
        self._def = custom_tool(
            "powershell",
            "Run a PowerShell 7 command on the user's Windows PC and return its output and exit code. "
            "Each call is a fresh process starting in the output folder (use Set-Location inside the "
            "command if needed). Use it for files and folders, running Python scripts (python has "
            "pandas and openpyxl), downloads from direct URLs, launching apps with Start-Process, "
            "system info, and the clipboard (Get-Clipboard / Set-Clipboard). Never start interactive "
            "or long-running programs in the foreground - use Start-Process for GUI apps and servers.",
            {
                "command": {"type": "string", "description": "The PowerShell command or script."},
                "timeout_seconds": {"type": "integer",
                                    "description": "Max seconds to wait (default 120, max 1800)."},
            },
            ["command"],
        )

    def definitions(self) -> list[dict]:
        return [self._def]

    def execute(self, name: str, args: dict[str, Any], ctx: ToolContext) -> Output:
        validate(self._def, args)
        command = args["command"]
        timeout = max(5, min(1800, int(args.get("timeout_seconds") or 120)))
        self._approve(command, ctx)

        env = dict(os.environ)
        venv_scripts = str(Path(sys.executable).parent)
        env["PATH"] = venv_scripts + os.pathsep + env.get("PATH", "")
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        for secret in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            env.pop(secret, None)

        prelude = ("$ProgressPreference='SilentlyContinue'; "
                   "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
                   "$OutputEncoding=[System.Text.Encoding]::UTF8; ")
        self.cfg.output_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            [self._exe, "-NoProfile", "-NonInteractive", "-Command", prelude + command],
            cwd=str(self.cfg.output_dir), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        waited = 0.0
        out = b""
        timed_out = False
        while True:
            try:
                out, _ = proc.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                waited += 0.5
                if ctx.control.stop_requested or waited >= timeout:
                    timed_out = waited >= timeout
                    self._kill(proc)
                    out, _ = proc.communicate()
                    if not timed_out:
                        ctx.control.checkpoint()  # raises StopRequested
                    break
        text = out.decode("utf-8", errors="replace").replace("\r\n", "\n").strip()
        if len(text) > _MAX_OUTPUT:
            half = _MAX_OUTPUT // 2
            text = f"{text[:half]}\n…[{len(text) - _MAX_OUTPUT} characters truncated]…\n{text[-half:]}"
        status = f"timed out after {timeout}s (process killed)" if timed_out else f"exit code {proc.returncode}"
        return f"[{status}]\n{text}" if text else f"[{status}] (no output)"

    def _approve(self, command: str, ctx: ToolContext) -> None:
        mode = self.cfg.shell_confirm.lower()
        needs = mode == "always" or (mode == "risky" and _RISKY.search(command))
        if not needs:
            return
        if ctx.unattended:
            raise ToolError("This command looks destructive and needs the user's approval, but no "
                            "user is available (unattended run). Skip it and report it instead.")
        ctx.ui.warn("The agent wants to run a command that may change or delete things:")
        ctx.ui.detail(command if len(command) < 800 else command[:800] + "…")
        if not ctx.ui.confirm("Allow this command?"):
            raise ToolError("The user declined to run this command. Choose another approach or ask the user.")

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def describe(self, name: str, args: dict[str, Any]) -> str:
        cmd = str(args.get("command", "")).strip().replace("\n", " ⏎ ")
        return f"powershell: {cmd[:110]}{'…' if len(cmd) > 110 else ''}"
