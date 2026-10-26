"""Tools for talking to the user and keeping state: ask, progress notes, memory."""

from __future__ import annotations

from datetime import date
from typing import Any

from .base import Capability, Output, ToolContext, custom_tool, validate


class UserTools(Capability):
    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self._defs = {
            "ask_user": custom_tool(
                "ask_user",
                "Ask the user a question and wait for the answer. Use it for confirmations before "
                "irreversible or outward-facing actions, for genuinely ambiguous instructions, and to "
                "ask the user to do something only they can (log in, solve a CAPTCHA, approve a UAC "
                "prompt). Keep it to one concise question; offer options when you can.",
                {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"},
                                "description": "Optional short answer choices."},
                },
                ["question"],
            ),
            "update_progress": custom_tool(
                "update_progress",
                "Save your checkpoint notes for this task (replaces the previous notes). Write them so "
                "you could continue from cold after a stop or restart: what is done, where results are "
                "saved, current position (e.g. 'processed 37 of 100, next row is ...'), what's next, "
                "and any decisions or user answers that matter. Never include secrets.",
                {"notes": {"type": "string"}},
                ["notes"],
            ),
            "remember": custom_tool(
                "remember",
                "Save a durable fact to long-term memory that future tasks should know (user "
                "preferences, important file locations, an approved message template, account names). "
                "Not for task progress and never for secrets.",
                {"fact": {"type": "string"}},
                ["fact"],
            ),
        }

    def definitions(self) -> list[dict]:
        return list(self._defs.values())

    def execute(self, name: str, args: dict[str, Any], ctx: ToolContext) -> Output:
        validate(self._defs[name], args)

        if name == "ask_user":
            if ctx.unattended:
                return ("No user is available (this is an unattended run). Continue only with steps "
                        "that need no answer; otherwise stop and report what you need.")
            ctx.run.set_status("waiting")
            try:
                answer = ctx.ui.ask(args["question"], args.get("options"))
            finally:
                ctx.run.set_status("running")
            ctx.run.log("user_answer", question=args["question"], answer=answer or "")
            if answer is None:
                return "The user did not answer. Don't proceed with anything that needed the answer."
            return f"User answered: {answer}"

        if name == "update_progress":
            ctx.run.progress = args["notes"]
            ctx.run.log("progress", notes=args["notes"])
            return "Progress notes saved."

        if name == "remember":
            mem = self.cfg.memory_file
            mem.parent.mkdir(parents=True, exist_ok=True)
            if not mem.exists():
                mem.write_text("# auto-bot memory\n\nFacts saved from earlier tasks. "
                               "Edit freely.\n\n", encoding="utf-8")
            with open(mem, "a", encoding="utf-8") as f:
                f.write(f"- {args['fact'].strip()} ({date.today().isoformat()})\n")
            return "Saved to memory."

        return "Unknown tool."

    def describe(self, name: str, args: dict[str, Any]) -> str:
        if name == "ask_user":
            return "asking you a question"
        if name == "update_progress":
            return "saving progress notes"
        return f"remembering: {str(args.get('fact', ''))[:80]}"
