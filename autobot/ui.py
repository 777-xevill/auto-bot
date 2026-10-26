"""Console output, and the channel for questions the agent asks the user."""

from __future__ import annotations

import queue
import sys
import threading

from rich.console import Console
from rich.markup import escape

from .control import Control, StopRequested
from .redact import redact

# The console resolves sys.stdout on every write, so prompt_toolkit's
# patch_stdout() keeps the input line pinned below the agent's output.
console = Console(force_terminal=sys.stdout.isatty() or None, legacy_windows=False,
                  highlight=False, soft_wrap=True)


class UI:
    """mode: "repl" - answers arrive from the interactive console's input loop
             "direct" - ask with input() on the calling thread (one-shot runs)
             "none" - nobody is there (unattended / scheduled runs)"""

    def __init__(self, control: Control, mode: str = "repl") -> None:
        self.control = control
        self.mode = mode
        self._answers: queue.Queue[str] = queue.Queue()
        self._pending = threading.Event()

    # --- output ------------------------------------------------------------
    def info(self, text: str) -> None:
        console.print(f"[cyan]{escape(text)}[/]")

    def ok(self, text: str) -> None:
        console.print(f"[green]{escape(text)}[/]")

    def warn(self, text: str) -> None:
        console.print(f"[yellow]{escape(text)}[/]")

    def error(self, text: str) -> None:
        console.print(f"[red]{escape(text)}[/]")

    def thought(self, text: str) -> None:
        console.print(f"[dim italic]  {escape(redact(text.strip()))}[/]")

    def say(self, text: str) -> None:
        """Text the agent wrote for the user (narration or final report)."""
        console.print(f"[bold white]{escape(redact(text.strip()))}[/]")

    def action(self, label: str) -> None:
        console.print(f"[magenta]  › {escape(redact(label))}[/]")

    def detail(self, text: str) -> None:
        console.print(f"[dim]    {escape(redact(text))}[/]")

    def rule(self, title: str = "") -> None:
        console.rule(f"[dim]{escape(title)}[/]" if title else "", style="dim")

    @staticmethod
    def set_title(title: str) -> None:
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception:
            pass

    # --- input -------------------------------------------------------------
    @property
    def can_ask(self) -> bool:
        return self.mode != "none"

    @property
    def question_pending(self) -> bool:
        return self._pending.is_set()

    def deliver_answer(self, text: str) -> None:
        self._answers.put(text)

    def ask(self, question: str, options: list[str] | None = None) -> str | None:
        """Ask the user and wait. Returns None if nobody can answer."""
        if self.mode == "none":
            return None
        console.print()
        console.print(f"[bold yellow]? {escape(question.strip())}[/]")
        for i, opt in enumerate(options or [], 1):
            console.print(f"[yellow]   {i}. {escape(opt)}[/]")
        if self.mode == "direct":
            try:
                answer = input("answer > ").strip()
            except EOFError:
                return None
        else:
            console.print("[dim]   (type your answer below and press Enter)[/]")
            while not self._answers.empty():
                self._answers.get_nowait()
            self._pending.set()
            try:
                while True:
                    if self.control.stop_requested:
                        raise StopRequested()
                    try:
                        answer = self._answers.get(timeout=0.2).strip()
                        break
                    except queue.Empty:
                        continue
            finally:
                self._pending.clear()
        if options and answer.isdigit() and 1 <= int(answer) <= len(options):
            answer = options[int(answer) - 1]
        return answer

    def confirm(self, question: str) -> bool:
        answer = self.ask(f"{question} (y/n)")
        return bool(answer) and answer.strip().lower() in ("y", "yes", "ok", "sure", "go", "ha", "haan", "hmm")
