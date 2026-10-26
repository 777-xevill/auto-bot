"""Front ends: the interactive console, and one-shot / scheduled runs."""

from __future__ import annotations

import os
import queue
import re
import signal
import sys
import threading

from rich.table import Table

from .agent import Agent
from .config import Config
from .control import Control
from .state import RESUMABLE, Run, elapsed, find_run, list_runs
from .tools.base import ToolError
from .tools.schedule import delete_schedule, list_schedules
from .ui import UI, console

HELP = """\
Type what you want done, in plain words. For example:
  open chrome and find 20 restaurants in Dhanmondi without a website, save them to excel
  clean E:\\leads\\raw.csv - remove duplicates and empty phone numbers
  open my project E:\\myapp in vs code and add a README

While a task is running:
  pause / resume     pause or continue            (hotkey Ctrl+Alt+P)
  stop               stop it                      (hotkey Ctrl+Alt+X, or slam the mouse into the top-left corner)
  anything else      is passed to the agent as an extra instruction

When idle:
  continue [more]        resume the last stopped task, optionally with extra instructions
  continue <id> [more]   resume (or follow up on) a specific task
  tasks                  recent tasks
  schedules              scheduled tasks      unschedule <name>   remove one
  memory                 what the agent remembers between tasks
  exit                   quit"""

STOP_WORDS = {"stop", "cancel", "abort", "halt", "stop it", "stop now", "thamo"}
PAUSE_WORDS = {"pause", "hold", "hold on", "wait"}
RESUME_WORDS = {"resume", "continue", "go", "go on", "unpause"}
_FILLER = re.compile(r"^(from )?(where|what) (you|we|u) (stopped|left( off)?|were doing)|^(please|pls|now)$")


def _norm(text: str) -> str:
    return re.sub(r"[^\w\s]", "", text.lower()).strip()


def _check_key(ui: UI) -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    ui.warn("No ANTHROPIC_API_KEY, so this standalone console can't think on its own. Free option: "
            "just tell Claude Code what to do - it controls the PC through the autobot tools. "
            "(Paid option: put an API key in E:\\auto-bot\\.env.)")
    return False


def _install_ctrl_c(control: Control, ui: UI) -> None:
    def handler(signum, frame):
        if control.stop_requested:
            raise KeyboardInterrupt
        control.request_stop()
        ui.warn("Stopping… (press Ctrl+C again to force quit)")
    signal.signal(signal.SIGINT, handler)


def _start_hotkeys(cfg: Config, control: Control, ui: UI) -> None:
    if not cfg.hotkeys:
        return

    def on_event(kind: str) -> None:
        msg = {"paused": "⏸ Paused - press the hotkey again (or type 'resume') to continue.",
               "resumed": "▶ Resumed.", "stop": "■ Stop requested."}[kind]
        ui.warn(msg)

    if not control.start_hotkeys(cfg.pause_hotkey, cfg.stop_hotkey, on_event):
        ui.warn("Global hotkeys unavailable; use the console commands instead.")


def print_runs(cfg: Config, active_id: str | None = None) -> None:
    runs = list_runs(cfg, 15)
    if not runs:
        console.print("[dim]No tasks yet.[/]")
        return
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    for col in ("id", "status", "steps", "cost", "updated", "task"):
        table.add_column(col, overflow="fold" if col == "task" else "ellipsis")
    for r in runs:
        status = "interrupted" if r.status in ("running", "waiting") and r.id != active_id else r.status
        color = {"done": "green", "failed": "red", "stopped": "yellow", "interrupted": "yellow"}.get(status, "cyan")
        table.add_row(r.id[:15], f"[{color}]{status}[/]", str(r.steps), f"${r.cost_usd:.2f}",
                      elapsed(r.updated or r.created), r.instruction[:70])
    console.print(table)


def print_schedules(cfg: Config) -> None:
    items = list_schedules(cfg)
    if not items:
        console.print("[dim]No scheduled tasks. Ask the agent, e.g. 'every day at 9am, ...'.[/]")
        return
    for i in items:
        console.print(f"[bold]{i['name']}[/]  next: {i['next_run']}  ({i['status']})\n  [dim]{i['instruction'][:200]}[/]")


def _split_continue(cfg: Config, rest: str) -> tuple[Run | None, str | None]:
    """'continue [id] [extra]' -> (run, extra instruction)"""
    rest = rest.strip()
    m = re.match(r"^(\d{8}(-\d{1,6})?\S*)\s*(.*)$", rest)
    run_id, extra = (m.group(1), m.group(3).strip()) if m else (None, rest)
    if extra and _FILLER.match(_norm(extra)):
        extra = ""
    if run_id:
        return find_run(cfg, run_id), (extra or None)
    run = find_run(cfg, None, resumable_only=True)
    latest = find_run(cfg, None)
    if latest and (run is None or (extra and latest.updated > run.updated)):
        run = latest  # the most recent task, for a follow-up
    return run, (extra or None)


class App:
    """Interactive console: one agent worker thread, input on the main thread."""

    def __init__(self, cfg: Config) -> None:
        cfg.ensure_dirs()
        self.cfg = cfg
        self.control = Control()
        self.ui = UI(self.control, mode="repl")
        self.agent = Agent(cfg, self.ui, self.control)
        self.jobs: queue.Queue = queue.Queue()
        self.busy = threading.Event()
        self.worker = threading.Thread(target=self._work, name="agent", daemon=True)

    # --- worker ----------------------------------------------------------------
    def _work(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                break
            self.busy.set()
            self.control.reset()
            try:
                job()
            except Exception as e:  # never kill the worker
                self.ui.error(f"{type(e).__name__}: {e}")
            finally:
                self.busy.clear()
        self.agent.close()

    def submit(self, job) -> None:
        self.busy.set()  # show "running" immediately
        self.jobs.put(job)

    # --- input -----------------------------------------------------------------
    def handle(self, line: str) -> str | None:
        text = line.strip()
        if not text:
            return None
        norm = _norm(text)

        if self.ui.question_pending:
            if norm in STOP_WORDS:
                self.control.request_stop()
            else:
                self.ui.deliver_answer(text)
            return None

        if self.busy.is_set():
            if norm in STOP_WORDS:
                self.control.request_stop()
                self.ui.warn("Stopping…")
            elif norm in PAUSE_WORDS:
                self.control.pause()
                self.ui.warn("⏸ Paused after the current action. Type 'resume' or 'stop'.")
            elif norm in RESUME_WORDS:
                if self.control.paused:
                    self.control.resume()
                    self.ui.warn("▶ Resumed.")
                else:
                    self.ui.info("Already running.")
            else:
                self.agent.inbox.put(text)
                self.ui.info("Got it - passing that to the agent at its next step.")
            return None

        cmd, _, rest = text.partition(" ")
        cmd = _norm(cmd)
        if norm in ("exit", "quit", "bye", "q"):
            return "exit"
        if norm in ("help", "h", "?"):
            console.print(HELP, markup=False)
        elif norm in ("tasks", "history", "list"):
            print_runs(self.cfg)
        elif norm in ("schedules", "schedule"):
            print_schedules(self.cfg)
        elif cmd == "unschedule" and rest.strip():
            try:
                self.ui.ok(delete_schedule(self.cfg, rest.strip()))
            except ToolError as e:
                self.ui.error(str(e))
        elif norm == "memory":
            mem = self.cfg.memory_file
            console.print(mem.read_text(encoding="utf-8") if mem.exists() else "[dim]Nothing saved yet.[/]")
            console.print(f"[dim]({mem})[/]")
        elif not _check_key(self.ui):
            pass  # every remaining branch talks to Claude
        elif cmd in ("continue", "resume"):
            run, extra = _split_continue(self.cfg, rest)
            if run is None:
                self.ui.info("Nothing to continue yet.")
            elif run.status == "done" and not extra:
                self.ui.info("The last task finished. Type a new task, or 'continue <what else to do>' "
                             "to follow up on it.")
            else:
                self.submit(lambda: self.agent.resume(run, extra))
        else:
            self.submit(lambda: self.agent.start(text))
        return None

    def _prompt_text(self):
        from prompt_toolkit.formatted_text import HTML
        if self.ui.question_pending:
            return HTML("<ansiyellow><b>answer › </b></ansiyellow>")
        if self.busy.is_set():
            if self.control.paused:
                return HTML("<ansiyellow>paused (resume / stop) › </ansiyellow>")
            return HTML("<ansigray>running (stop / pause / add instruction) › </ansigray>")
        return HTML("<ansicyan><b>you › </b></ansicyan>")

    # --- main ------------------------------------------------------------------
    def run(self) -> int:
        self.ui.set_title("auto-bot")
        console.print("[bold cyan]auto-bot[/] - tell me what to do on this PC.")
        console.print(f"[dim]{self.cfg.model} · effort {self.cfg.effort} · ${self.cfg.budget_usd:.0f} budget per task · "
                      "Ctrl+Alt+P pause · Ctrl+Alt+X stop · 'help' for commands[/]")
        _check_key(self.ui)
        resumable = find_run(self.cfg, None, resumable_only=True)
        if resumable:
            console.print(f"[dim]Unfinished task: \"{resumable.instruction[:70]}\" - type 'continue' to resume it.[/]")
        _start_hotkeys(self.cfg, self.control, self.ui)
        self.worker.start()
        try:
            if sys.stdin.isatty():
                self._prompt_loop()
            else:
                self._stdin_loop()
                self.jobs.put(None)
                self.worker.join()  # piped input: let queued work finish
                return 0
        finally:
            if self.worker.is_alive():
                self.control.request_stop()
                self.jobs.put(None)
                self.worker.join(timeout=15)
        return 0

    def _prompt_loop(self) -> None:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.patch_stdout import patch_stdout

        session = PromptSession()
        with patch_stdout(raw=True):
            while True:
                try:
                    line = session.prompt(self._prompt_text, refresh_interval=0.5)
                except KeyboardInterrupt:
                    if self.busy.is_set():
                        self.control.request_stop()
                        self.ui.warn("Stopping…")
                        continue
                    break
                except EOFError:
                    break
                if self.handle(line) == "exit":
                    break

    def _stdin_loop(self) -> None:
        for line in sys.stdin:
            if self.handle(line) == "exit":
                break


def run_once(cfg: Config, instruction: str | None = None, unattended: bool = False,
             resume: tuple[Run, str | None] | None = None) -> int:
    """Run one task (or resume one) in the foreground, then exit."""
    cfg.ensure_dirs()
    control = Control()
    ui = UI(control, mode="none" if unattended else "direct")
    if not _check_key(ui):
        return 2
    _install_ctrl_c(control, ui)
    _start_hotkeys(cfg, control, ui)
    agent = Agent(cfg, ui, control)
    try:
        if resume:
            run = agent.resume(*resume)
        else:
            run = agent.start(instruction or "", unattended=unattended)
    finally:
        agent.close()
    if run.result:
        (run.dir / "result.md").write_text(run.result, encoding="utf-8")
    return 0 if run.status == "done" else 1


__all__ = ["App", "run_once", "print_runs", "print_schedules", "RESUMABLE"]
