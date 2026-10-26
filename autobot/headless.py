"""Run a task with headless Claude Code (your Claude subscription, no API key).

Used for scheduled tasks: Task Scheduler -> autobot.cmd scheduled <name> ->
claude -p "<instruction>" with the autobot MCP tools allowed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from .config import ROOT, Config

# Tools a scheduled run may use without asking (nobody is there to approve).
ALLOWED_TOOLS = ["mcp__autobot", "Read", "Write", "Edit", "Glob", "Grep", "PowerShell", "Bash"]


def find_claude() -> str | None:
    """The Claude Code CLI: on PATH, or the binary bundled with the VS Code extension."""
    on_path = shutil.which("claude")
    if on_path:
        return on_path

    def version(p: Path) -> tuple:
        m = re.search(r"claude-code-(\d+)\.(\d+)\.(\d+)", str(p))
        return tuple(int(x) for x in m.groups()) if m else (0,)

    found = []
    for editor in (".vscode", ".vscode-insiders", ".cursor", ".windsurf"):
        base = Path.home() / editor / "extensions"
        found += base.glob("anthropic.claude-code-*/resources/native-binary/claude.exe")
    return str(max(found, key=version)) if found else None


def run_with_claude_code(cfg: Config, instruction: str, name: str = "task", capture: bool = False):
    """Returns the exit code, or (exit code, report text) when capture=True."""
    exe = find_claude()
    if not exe:
        msg = "Claude Code CLI not found (install Claude Code or set an ANTHROPIC_API_KEY)."
        if capture:
            return 2, msg
        print(msg)
        return 2
    prompt = (f"{instruction.strip()}\n\n"
              "(This is an unattended scheduled run started by auto-bot - nobody can answer questions. "
              "Use the autobot tools for the screen and the browser. Do only what is safe and already "
              "authorized, save results to files, and end with a short report.)")
    log_dir = cfg.runs_dir / "_scheduled"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{name}.md"
    with open(log_file, "w", encoding="utf-8") as out:
        out.write(f"# {name} - {datetime.now():%Y-%m-%d %H:%M}\n\n**Task:** {instruction.strip()}\n\n## Report\n\n")
        out.flush()
        proc = subprocess.run([exe, "-p", prompt, "--allowedTools", *ALLOWED_TOOLS],
                              cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    if capture:
        text = log_file.read_text(encoding="utf-8")
        return proc.returncode, text.split("## Report", 1)[-1].strip()
    return proc.returncode
