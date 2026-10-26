"""Task runs on disk: runs/<id>/ holds everything needed to resume.

  run.json       status, counters, original instruction, follow-ups
  messages.json  the exact conversation of the current context segment
  progress.md    the agent's own checkpoint notes (update_progress)
  log.jsonl      every action, result and status change (secrets masked)
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Config
from .redact import redact

RESUMABLE = ("running", "paused", "stopped", "failed", "waiting")


def _slug(text: str) -> str:
    words = re.sub(r"[^a-zA-Z0-9 ]+", " ", text).lower().split()
    return "-".join(words[:6])[:40] or "task"


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class Run:
    id: str
    instruction: str
    status: str = "running"
    created: str = ""
    updated: str = ""
    steps: int = 0
    cost_usd: float = 0.0
    step_limit: int = 0
    budget_limit: float = 0.0
    segment: int = 1
    unattended: bool = False
    follow_ups: list[str] = field(default_factory=list)
    result: str = ""
    last_input_tokens: int = 0
    messages_bytes: int = 0

    # not persisted in run.json
    dir: Path = field(default=Path("."), repr=False, compare=False)
    messages: list[dict] = field(default_factory=list, repr=False, compare=False)

    # --- create / load -----------------------------------------------------
    @classmethod
    def create(cls, cfg: Config, instruction: str, unattended: bool = False) -> "Run":
        now = datetime.now()
        run_id = f"{now:%Y%m%d-%H%M%S}-{_slug(instruction)}"
        run = cls(id=run_id, instruction=instruction, created=now.isoformat(timespec="seconds"),
                  step_limit=cfg.max_steps, budget_limit=cfg.budget_usd, unattended=unattended)
        run.dir = cfg.runs_dir / run_id
        run.dir.mkdir(parents=True, exist_ok=True)
        return run

    @classmethod
    def load(cls, path: Path, with_messages: bool = True) -> "Run":
        data = json.loads((path / "run.json").read_text(encoding="utf-8"))
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        run = cls(**known)
        run.dir = path
        msg_file = path / "messages.json"
        if with_messages and msg_file.exists():
            try:
                run.messages = json.loads(msg_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                run.messages = []
        return run

    # --- persistence -------------------------------------------------------
    def save(self) -> None:
        self.updated = datetime.now().isoformat(timespec="seconds")
        payload = json.dumps(self.messages, ensure_ascii=False)
        self.messages_bytes = len(payload)
        _atomic_write(self.dir / "messages.json", payload)
        meta = {k: v for k, v in asdict(self).items() if k not in ("dir", "messages")}
        _atomic_write(self.dir / "run.json", json.dumps(meta, indent=2, ensure_ascii=False))

    def set_status(self, status: str, note: str = "") -> None:
        self.status = status
        self.log("status", status=status, note=note)
        self.save()

    def log(self, kind: str, **data) -> None:
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), "type": kind}
        for key, value in data.items():
            entry[key] = redact(value) if isinstance(value, str) else value
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with open(self.dir / "log.jsonl", "a", encoding="utf-8") as f:
            f.write(redact(line) + "\n")

    def recent_log(self, n: int = 30) -> list[dict]:
        path = self.dir / "log.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()[-n:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out

    # --- progress notes ----------------------------------------------------
    @property
    def progress(self) -> str:
        path = self.dir / "progress.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    @progress.setter
    def progress(self, notes: str) -> None:
        _atomic_write(self.dir / "progress.md", notes)


def list_runs(cfg: Config, limit: int = 15) -> list[Run]:
    if not cfg.runs_dir.exists():
        return []
    dirs = sorted((d for d in cfg.runs_dir.iterdir() if (d / "run.json").exists()),
                  key=lambda d: d.name, reverse=True)
    runs = []
    for d in dirs[:limit]:
        try:
            runs.append(Run.load(d, with_messages=False))
        except Exception:
            continue
    return runs


def find_run(cfg: Config, query: str | None = None, resumable_only: bool = False) -> Run | None:
    """Most recent run (optionally matching an id prefix), loaded with messages."""
    for run in list_runs(cfg, limit=200):
        if query and not run.id.startswith(query) and query not in run.id:
            continue
        if resumable_only and run.status not in RESUMABLE:
            continue
        return Run.load(run.dir)
    return None


def elapsed(since_iso: str) -> str:
    try:
        secs = time.time() - datetime.fromisoformat(since_iso).timestamp()
    except ValueError:
        return "?"
    if secs < 90:
        return f"{secs:.0f}s ago"
    if secs < 5400:
        return f"{secs / 60:.0f} min ago"
    if secs < 172800:
        return f"{secs / 3600:.0f} h ago"
    return f"{secs / 86400:.0f} days ago"
