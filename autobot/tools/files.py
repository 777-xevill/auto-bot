"""Claude's Anthropic-defined text editor tool, run against the local disk."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .base import Capability, Output, ToolContext, ToolError

_PROTECTED = [Path(os.environ.get("SystemRoot", r"C:\Windows")),
              Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
              Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))]
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build"}
_MAX_VIEW = 40_000


class FileEditor(Capability):
    NAME = "str_replace_based_edit_tool"

    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self._created: set[Path] = set()

    def definitions(self) -> list[dict]:
        return [{"type": "text_editor_20250728", "name": self.NAME, "max_characters": _MAX_VIEW}]

    def tool_names(self) -> set[str]:
        return {self.NAME}

    # --- helpers -------------------------------------------------------------
    def _path(self, raw: Any) -> Path:
        if not raw:
            raise ToolError("path is required.")
        p = Path(os.path.expandvars(os.path.expanduser(str(raw))))
        if not p.is_absolute():
            p = self.cfg.output_dir / p
        return p.resolve()

    def _check_writable(self, p: Path) -> None:
        for root in _PROTECTED:
            try:
                p.relative_to(root.resolve())
                raise ToolError(f"Writing inside {root} is not allowed.")
            except ValueError:
                continue

    @staticmethod
    def _read(p: Path) -> tuple[str, bool]:
        if not p.exists():
            raise ToolError(f"File not found: {p}")
        raw = p.read_bytes()
        if b"\x00" in raw[:8192]:
            raise ToolError(f"{p.name} is a binary file ({len(raw)} bytes). Use powershell/python to "
                            "work with it (e.g. pandas for .xlsx).")
        text = raw.decode("utf-8", errors="replace")
        crlf = "\r\n" in text
        return text.replace("\r\n", "\n"), crlf

    @staticmethod
    def _write(p: Path, text: str, crlf: bool) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))

    @staticmethod
    def _numbered(text: str, start: int = 1) -> str:
        return "\n".join(f"{i:6}\t{line}" for i, line in enumerate(text.split("\n"), start))

    def _owned(self, p: Path) -> bool:
        if p in self._created:
            return True
        try:
            p.relative_to(self.cfg.output_dir.resolve())
            return True
        except ValueError:
            return False

    # --- commands ------------------------------------------------------------
    def execute(self, name: str, args: dict[str, Any], ctx: ToolContext) -> Output:
        command = args.get("command")
        p = self._path(args.get("path"))

        if command == "view":
            if p.is_dir():
                return self._list_dir(p)
            text, _ = self._read(p)
            rng = args.get("view_range")
            lines = text.split("\n")
            start = 1
            if rng:
                start = max(1, int(rng[0]))
                end = len(lines) if int(rng[1]) == -1 else min(len(lines), int(rng[1]))
                lines = lines[start - 1:end]
            out = self._numbered("\n".join(lines), start)
            if len(out) > _MAX_VIEW:
                out = out[:_MAX_VIEW] + "\n[truncated - use view_range to see more]"
            return out

        if command == "create":
            self._check_writable(p)
            if p.exists() and not self._owned(p):
                raise ToolError(f"{p} already exists and is not a file you created. Use str_replace to "
                                "edit it, or ask the user before replacing it.")
            self._write(p, str(args.get("file_text", "")), crlf=False)
            self._created.add(p)
            return f"File written: {p}"

        if command == "str_replace":
            self._check_writable(p)
            text, crlf = self._read(p)
            old = str(args.get("old_str", ""))
            new = str(args.get("new_str", "") or "")
            if not old:
                raise ToolError("old_str must not be empty.")
            count = text.count(old)
            if count == 0:
                raise ToolError("old_str was not found in the file (it must match exactly, including whitespace).")
            if count > 1:
                lines = [i + 1 for i, line in enumerate(text.split("\n")) if old.split("\n")[0] in line]
                raise ToolError(f"old_str appears {count} times (lines {lines[:10]}). Include more context.")
            text = text.replace(old, new, 1)
            self._write(p, text, crlf)
            line = text[: text.index(new)].count("\n") + 1 if new else 1
            snippet = text.split("\n")[max(0, line - 4): line + new.count("\n") + 3]
            return f"Edited {p}:\n" + self._numbered("\n".join(snippet), max(1, line - 3))

        if command == "insert":
            self._check_writable(p)
            text, crlf = self._read(p)
            at = int(args.get("insert_line", 0))
            new = str(args.get("insert_text", args.get("new_str", "")))
            lines = text.split("\n")
            if not 0 <= at <= len(lines):
                raise ToolError(f"insert_line must be between 0 and {len(lines)}.")
            lines[at:at] = new.split("\n")
            self._write(p, "\n".join(lines), crlf)
            return f"Inserted {new.count(chr(10)) + 1} line(s) after line {at} in {p}."

        raise ToolError(f"Unsupported command '{command}'. Use view, create, str_replace or insert.")

    def _list_dir(self, root: Path) -> str:
        out = [f"{root}"]
        count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            depth = Path(dirpath).relative_to(root).parts
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
            if len(depth) >= 2:
                dirnames[:] = []
            indent = "  " * (len(depth) + 1)
            for d in dirnames:
                out.append(f"{indent}{d}/")
            for f in sorted(filenames):
                out.append(f"{indent}{f}")
                count += 1
                if count > 400:
                    out.append("[listing truncated]")
                    return "\n".join(out)
        return "\n".join(out)

    def describe(self, name: str, args: dict[str, Any]) -> str:
        return f"file {args.get('command')}: {args.get('path')}"
