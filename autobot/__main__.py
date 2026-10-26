"""Command line.

  python -m autobot                      interactive console
  python -m autobot <task...>            run one task, then exit
  python -m autobot --unattended <task>  run with nobody to answer questions
  python -m autobot continue [id] [more] resume the last (or given) task
  python -m autobot tasks                list recent tasks
  python -m autobot schedules            list scheduled tasks
  python -m autobot scheduled <name>     (used by Task Scheduler)
"""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    from .config import Config
    from .console import App, _split_continue, print_runs, print_schedules, run_once

    cfg = Config()
    cfg.ensure_dirs()
    unattended = "--unattended" in argv
    args = [a for a in argv if a != "--unattended"]

    if not args:
        return App(cfg).run()

    cmd = args[0].lower()
    if cmd in ("tasks", "list", "history"):
        print_runs(cfg)
        return 0
    if cmd == "schedules":
        print_schedules(cfg)
        return 0
    if cmd == "telegram":
        from .telegram_bot import main as telegram_main
        return telegram_main()
    if cmd == "telegram-autostart-remove":
        from .tools.schedule import remove_telegram_autostart
        print(remove_telegram_autostart())
        return 0
    if cmd == "telegram-autostart":
        from .tools.schedule import install_telegram_autostart
        print(install_telegram_autostart())
        return 0
    if cmd == "do":  # headless Claude Code runs the task now and prints its report
        from .headless import run_with_claude_code
        if len(args) >= 3 and args[1] == "--file":
            instruction = open(args[2], encoding="utf-8").read()
        else:
            instruction = " ".join(args[1:])
        if not instruction.strip():
            print('usage: do "<task>"  |  do --file <task.txt>')
            return 2
        code, report = run_with_claude_code(cfg, instruction, "do", capture=True)
        print(report)
        return code
    if cmd == "scheduled":
        if len(args) < 2:
            print("usage: scheduled <name>")
            return 2
        task_file = cfg.schedules_dir / f"{args[1]}.txt"
        if not task_file.exists():
            print(f"No schedule named {args[1]}")
            return 2
        instruction = task_file.read_text(encoding="utf-8")
        from .headless import find_claude, run_with_claude_code
        if find_claude():  # free: runs on the Claude subscription
            return run_with_claude_code(cfg, instruction, args[1])
        return run_once(cfg, instruction, unattended=True)
    if cmd in ("continue", "resume"):
        run, extra = _split_continue(cfg, " ".join(args[1:]))
        if run is None:
            print("Nothing to continue.")
            return 1
        return run_once(cfg, unattended=unattended, resume=(run, extra))
    return run_once(cfg, " ".join(args), unattended=unattended)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
