"""The agent loop: ask Claude, run the tools it calls, repeat until done.

History is append-only (never edited), so thinking blocks stay valid and the
prompt cache keeps hitting. When a task grows too large, the run starts a
fresh context segment from its progress notes instead of trimming history.
"""

from __future__ import annotations

import queue
import traceback
from typing import Any

import anthropic

from .config import Config
from .control import Control, StopRequested
from .prompts import SYSTEM_PROMPT, resume_brief, task_message, text_of_results
from .state import Run
from .tools import Registry, ToolContext, ToolError
from .tools.computer import ComputerTools
from .ui import UI

BETA_FALLBACK = "server-side-fallback-2026-07-01"     # retry on another model if declined
BETA_UPDATES = "thinking-display-updates-2026-08-18"  # short progress notes between tool calls

NOT_EXECUTED = "Not executed: an earlier action in this turn failed."
STOPPED_TEXT = "Not executed: the user stopped the task."
INTERRUPTED = "Not executed: the task was interrupted before this action ran."
CUT_OFF = "Not executed: your response was cut off by the output limit. Retry with fewer steps per turn."
_RETRYABLE = (408, 409, 429, 500, 502, 503, 504, 529)


class Agent:
    def __init__(self, cfg: Config, ui: UI, control: Control) -> None:
        self.cfg, self.ui, self.control = cfg, ui, control
        self.client = anthropic.Anthropic(max_retries=4)
        self.registry = Registry(cfg)
        self.tools = self.registry.definitions()
        self.inbox: queue.Queue[str] = queue.Queue()  # messages typed while a task runs
        self._optional = True  # fallbacks + progress updates; dropped if the API rejects them
        self.current: Run | None = None

    # --- public ----------------------------------------------------------------
    def start(self, instruction: str, unattended: bool = False) -> Run:
        run = Run.create(self.cfg, instruction, unattended)
        screen = self.registry.get(ComputerTools).screen_summary
        run.messages = [{"role": "user", "content": task_message(instruction, self.cfg, screen, unattended)}]
        run.log("task", instruction=instruction, unattended=unattended)
        run.save()
        self.ui.rule(run.id)
        return self._loop(run)

    def resume(self, run: Run, extra: str | None = None) -> Run:
        """Continue a stopped/failed run, or give a finished run a follow-up."""
        self._close_dangling(run, INTERRUPTED)
        if extra:
            run.follow_ups.append(extra)
        if not run.messages:
            run.messages = [{"role": "user", "content": resume_brief(run, "Resumed after a restart.", "")}]
        elif run.status == "done":
            run.messages.append({"role": "user", "content": f"Follow-up instruction from the user:\n{extra or 'Continue.'}"})
        else:
            note = ("[Resumed] You were stopped earlier and the user asked you to continue. Time has "
                    "passed and the screen may have changed - re-check the current state before acting.")
            if extra:
                note += f"\n\nAdditional instruction from the user: {extra}"
            run.messages.append({"role": "user", "content": note})
        run.step_limit = max(run.step_limit, run.steps + self.cfg.max_steps)
        run.budget_limit = max(run.budget_limit, run.cost_usd + self.cfg.budget_usd)
        run.log("resume", extra=extra or "")
        run.save()
        self.ui.rule(f"resuming {run.id}")
        return self._loop(run)

    def close(self) -> None:
        self.registry.close()

    # --- the loop --------------------------------------------------------------
    def _loop(self, run: Run) -> Run:
        self.current = run
        run.set_status("running")
        ctx = ToolContext(self.cfg, self.ui, self.control, run)
        try:
            while True:
                self.control.checkpoint()
                self._check_limits(run)
                self._maybe_rollover(run)

                response = self._ask_claude(run)
                run.messages.append({"role": "assistant",
                                     "content": [b.to_dict(mode="json", exclude_none=True) for b in response.content]})
                run.steps += 1
                run.cost_usd += self.cfg.cost(response.usage)
                u = response.usage
                run.last_input_tokens = ((u.input_tokens or 0) + (u.cache_read_input_tokens or 0)
                                         + (u.cache_creation_input_tokens or 0))
                run.save()
                self._show(response, run)

                if response.stop_reason == "refusal":
                    details = getattr(response, "stop_details", None)
                    category = getattr(details, "category", None) if details else None
                    self.ui.error(f"Claude declined to continue this task (safety system{': ' + category if category else ''}).")
                    run.set_status("failed", f"refusal {category or ''}".strip())
                    return run

                tool_uses = [b for b in response.content if b.type == "tool_use"]
                if not tool_uses:
                    extra = self._drain_inbox(run)
                    if extra:  # the user said something while Claude was finishing
                        run.messages.append({"role": "user", "content": extra})
                        run.save()
                        continue
                    if response.stop_reason == "max_tokens":
                        run.messages.append({"role": "user", "content": "Your reply was cut off by the output limit. Continue."})
                        run.save()
                        continue
                    run.result = "\n".join(b.text for b in response.content if b.type == "text").strip()
                    run.set_status("done")
                    self.ui.ok(f"✓ Done - {run.steps} steps, about ${run.cost_usd:.2f}")
                    return run

                if response.stop_reason == "max_tokens":
                    results = [self._result(b, CUT_OFF, error=True) for b in tool_uses]
                    stopped = False
                else:
                    results, stopped = self._run_tools(tool_uses, ctx)
                content: list[dict] = results + self._drain_inbox(run)
                note = self.control.take_pause_note()
                if note:
                    content.append({"type": "text", "text": note})
                run.messages.append({"role": "user", "content": content})
                run.save()
                if stopped:
                    raise StopRequested()

        except StopRequested:
            self._close_dangling(run, STOPPED_TEXT)
            run.set_status("stopped")
            self.ui.warn(f"■ Stopped after {run.steps} steps (about ${run.cost_usd:.2f}). "
                         "Type 'continue' to pick up from here.")
        except anthropic.AuthenticationError:
            run.set_status("failed", "authentication")
            self.ui.error("The Anthropic API key was rejected. Put a valid ANTHROPIC_API_KEY in .env.")
        except anthropic.APIError as e:
            run.log("error", message=str(e))
            run.set_status("failed", str(e)[:300])
            self.ui.error(f"API error: {e}\nType 'continue' to retry from here.")
        except Exception as e:
            run.log("error", message=traceback.format_exc())
            run.set_status("failed", f"{type(e).__name__}: {e}"[:300])
            self.ui.error(f"Unexpected error: {type(e).__name__}: {e}\nType 'continue' to retry from here.")
        finally:
            self.current = None
        return run

    # --- talking to Claude -----------------------------------------------------
    def _request(self, run: Run) -> dict[str, Any]:
        thinking: dict[str, Any] = {"type": "adaptive"}
        betas = []
        kwargs: dict[str, Any] = dict(
            model=self.cfg.model,
            max_tokens=self.cfg.max_tokens,
            system=SYSTEM_PROMPT,
            tools=self.tools,
            messages=run.messages,
            output_config={"effort": self.cfg.effort},
            cache_control={"type": "ephemeral"},
        )
        if self._optional and self.cfg.progress_updates:
            thinking["display"] = "updates"
            betas.append(BETA_UPDATES)
        if self._optional and self.cfg.fallbacks:
            kwargs["fallbacks"] = "default"
            betas.append(BETA_FALLBACK)
        kwargs["thinking"] = thinking
        if betas:
            kwargs["betas"] = betas
        return kwargs

    def _stream(self, run: Run):
        with self.client.beta.messages.stream(**self._request(run)) as stream:
            for _event in stream:
                if self.control.stop_requested:
                    raise StopRequested()
            return stream.get_final_message()

    def _ask_claude(self, run: Run):
        attempt = 0
        rolled = False
        while True:
            try:
                return self._stream(run)
            except anthropic.BadRequestError as e:
                run.log("error", message=f"400: {e}")
                if self._optional:
                    self._optional = False
                    self.ui.warn("The API rejected an optional feature; retrying without fallbacks/progress notes.")
                    continue
                if not rolled and len(run.messages) > 1:
                    rolled = True
                    self.ui.warn("The conversation couldn't be replayed; continuing from progress notes.")
                    self._rollover(run, "Context refreshed after an API error.")
                    continue
                raise
            except (anthropic.APIConnectionError, anthropic.APIStatusError) as e:
                status = getattr(e, "status_code", None)
                if isinstance(e, anthropic.APIStatusError) and status not in _RETRYABLE:
                    raise
                attempt += 1
                if attempt > 4:
                    raise
                wait = 15 * attempt
                self.ui.warn(f"API unavailable ({status or 'connection'}); retrying in {wait}s…")
                self.control.sleep(wait)

    def _show(self, response, run: Run) -> None:
        for b in response.content:
            if b.type == "thinking" and (b.thinking or "").strip():
                self.ui.thought(b.thinking)
                run.log("thought", text=b.thinking)
            elif b.type == "text" and b.text.strip():
                self.ui.say(b.text)
                run.log("say", text=b.text)
            elif b.type == "fallback":
                src = getattr(getattr(b, "from_", None), "model", "?")
                dst = getattr(getattr(b, "to", None), "model", "?")
                self.ui.warn(f"({src} declined this step; {dst} continued)")
        self.ui.set_title(f"auto-bot · step {run.steps} · ${run.cost_usd:.2f}")

    # --- tools -----------------------------------------------------------------
    @staticmethod
    def _result(block, text: str, error: bool = False) -> dict:
        r: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id, "content": text}
        if getattr(block, "toolset_name", None):
            r["toolset_name"] = block.toolset_name
        if error:
            r["is_error"] = True
        return r

    def _run_tools(self, tool_uses: list, ctx: ToolContext) -> tuple[list[dict], bool]:
        """Run calls in order. After a failure, skip the rest (Claude planned on success)."""
        results: list[dict] = []
        failed = stopped = False
        for block in tool_uses:
            if stopped or failed:
                results.append(self._result(block, STOPPED_TEXT if stopped else NOT_EXECUTED, error=True))
                continue
            args = block.input if isinstance(block.input, dict) else {}
            cap = self.registry.find(block.name, block.toolset_name)
            try:
                self.control.checkpoint()
                if cap is None:
                    raise ToolError(f"Unknown tool '{block.name}'.")
                label = cap.describe(block.name, args)
                self.ui.action(label)
                ctx.run.log("action", tool=block.name, toolset=block.toolset_name, label=label,
                            input=_loggable(args))
                output = cap.execute(block.name, args, ctx)
                result = self._result(block, "")
                result["content"] = [{"type": "text", "text": output or "OK"}] if isinstance(output, str) else output
                summary = _summary(output)
                ctx.run.log("result", tool=block.name, summary=summary)
                if block.name == "powershell":
                    self.ui.detail(summary.splitlines()[0] if summary else "")
            except StopRequested:
                stopped = True
                result = self._result(block, STOPPED_TEXT, error=True)
            except ToolError as e:
                failed = True
                result = self._result(block, f"Error: {e}", error=True)
                self.ui.detail(f"✗ {e}")
                ctx.run.log("error", tool=block.name, message=str(e))
            except Exception as e:
                failed = True
                first = str(e).strip().splitlines()[0] if str(e).strip() else ""
                result = self._result(block, f"Error: {type(e).__name__}: {first}", error=True)
                self.ui.detail(f"✗ {type(e).__name__}: {first}")
                ctx.run.log("error", tool=block.name, message=traceback.format_exc())
            results.append(result)
        return results, stopped

    def _drain_inbox(self, run: Run) -> list[dict]:
        blocks = []
        while True:
            try:
                msg = self.inbox.get_nowait()
            except queue.Empty:
                break
            run.follow_ups.append(msg)
            run.log("user_message", text=msg)
            blocks.append({"type": "text", "text": f"[Message from the user while you were working]\n{msg}"})
        return blocks

    def _close_dangling(self, run: Run, text: str) -> None:
        """Answer tool calls left without results (stop or crash mid-turn)."""
        if not run.messages or run.messages[-1]["role"] != "assistant":
            return
        content = run.messages[-1]["content"]
        pending = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
        if not pending:
            return
        results = []
        for b in pending:
            r = {"type": "tool_result", "tool_use_id": b["id"], "content": text, "is_error": True}
            if b.get("toolset_name"):
                r["toolset_name"] = b["toolset_name"]
            results.append(r)
        run.messages.append({"role": "user", "content": results})
        run.save()

    # --- limits and context size -----------------------------------------------
    def _check_limits(self, run: Run) -> None:
        if run.steps >= run.step_limit:
            if not self._ask_to_extend(run, f"I've taken {run.steps} steps on this task. Keep going?"):
                raise StopRequested()
            run.step_limit = run.steps + self.cfg.max_steps
        if run.cost_usd >= run.budget_limit:
            if not self._ask_to_extend(run, f"This task has cost about ${run.cost_usd:.2f} "
                                            f"(budget ${run.budget_limit:.2f}). Keep going?"):
                raise StopRequested()
            run.budget_limit = run.cost_usd + self.cfg.budget_usd

    def _ask_to_extend(self, run: Run, question: str) -> bool:
        if run.unattended or not self.ui.can_ask:
            self.ui.warn(question.replace("Keep going?", "Limit reached - stopping (unattended run)."))
            return False
        return self.ui.confirm(question)

    def _maybe_rollover(self, run: Run) -> None:
        if len(run.messages) <= 2:
            return
        if run.last_input_tokens > self.cfg.rollover_tokens or run.messages_bytes > 24_000_000:
            self._rollover(run, "Context refreshed to keep requests small.")

    def _rollover(self, run: Run, reason: str) -> None:
        last = run.messages[-1] if run.messages else None
        last_results, image = "", None
        if last and last["role"] == "user" and isinstance(last["content"], list):
            last_results = text_of_results(last["content"])
            for block in reversed(last["content"]):
                inner = block.get("content") if block.get("type") == "tool_result" else None
                if isinstance(inner, list):
                    image = next((b for b in reversed(inner) if b.get("type") == "image"), None)
                    if image:
                        break
        content: list[dict] = [{"type": "text", "text": resume_brief(run, reason, last_results)}]
        if image:
            content += [{"type": "text", "text": "Latest screenshot before the refresh:"}, image]
        run.messages = [{"role": "user", "content": content}]
        run.segment += 1
        run.last_input_tokens = 0
        run.log("rollover", reason=reason, segment=run.segment)
        run.save()
        self.ui.info(f"(context refreshed - segment {run.segment})")


def _loggable(args: dict) -> dict:
    out = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 400:
            out[k] = v[:400] + f"…[{len(v)} chars]"
        else:
            out[k] = v
    return out


def _summary(output: Any) -> str:
    if isinstance(output, str):
        return output[:300]
    parts = []
    for b in output or []:
        if b.get("type") == "text":
            parts.append(b["text"][:300])
        elif b.get("type") == "image":
            parts.append("[image]")
        elif b.get("type") == "browser_state":
            parts.append(f"[{len(b.get('tabs', []))} tabs]")
    return " ".join(parts)[:400]
