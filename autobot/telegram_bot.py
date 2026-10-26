"""Telegram front end: message the bot, your PC does the work.

Each message goes to headless Claude Code (your Claude subscription, no API
key) with the autobot tools; the reply comes back to the chat. Follow-up
messages resume the same Claude session, so "continue" and answers to its
questions just work.

Security: only the paired owner is obeyed. Everyone else is ignored silently.
Pair once by sending  /pair <code>  (the code is shown when the bot starts
with no owner). Or set TELEGRAM_OWNER_ID in .env.
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

from .config import ROOT, Config
from .headless import ALLOWED_TOOLS, find_claude
from .redact import redact

API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
STALE_SECONDS = 120          # messages older than this at startup are skipped, not obeyed
MAX_QUEUE = 5
TASK_TIMEOUT = 40 * 60
HEARTBEAT = 150              # "still working" note interval while a task runs
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

HELP = """auto-bot is listening. Just tell me what to do on your PC, in plain words.

Examples:
• open whatsapp and text Ratul Bro "where are you"
• find 20 restaurants in Dhanmondi with no website and save them to excel
• open vs code and create a project in E:\\test

Commands:
/screen - photo of your screen right now
/stop - stop the current task
/new - start a fresh conversation
/status - what I'm doing
/help - this message

If I ask you a question, just reply to answer it."""

SYSTEM_NOTE = (
    "You are being driven by Rifat through Telegram while he may be away from the PC. Do the task on his "
    "PC with the autobot tools and your shell/file tools, following the rules in CLAUDE.md. Your final reply "
    "is sent to his phone as a chat message: keep it short and plain (a few sentences, no tables, no "
    "markdown headings). If you need a decision, or a confirmation before an irreversible or outward-facing "
    "action he did not explicitly request, ask ONE short question and stop - his next message is the answer. "
    "If he explicitly asked for the action (e.g. 'text X ...'), just do it. Never include secrets in replies."
)


class TelegramBot:
    def __init__(self, cfg: Config, token: str | None = None) -> None:
        self.cfg = cfg
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.state_file = cfg.data_dir / "telegram.json"
        self.log_dir = cfg.runs_dir / "_telegram"
        self.state = self._load_state()
        self.jobs: queue.Queue[tuple[int, str]] = queue.Queue()
        self.proc: subprocess.Popen | None = None
        self.busy = threading.Event()
        self.stopping = threading.Event()   # /stop pressed for the running task
        self.running_text = ""
        self.started = time.time()
        self.bad_pairs = 0
        self._offset = None
        self._warned_conflict = False
        self._worker_started = False

    # --- state -----------------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_file)

    @property
    def owner(self) -> int | None:
        env = os.environ.get("TELEGRAM_OWNER_ID", "").strip()
        if env.isdigit():
            return int(env)
        return self.state.get("owner_id")

    def log(self, kind: str, **data) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), "type": kind, **data}
        with open(self.log_dir / "log.jsonl", "a", encoding="utf-8") as f:
            f.write(redact(json.dumps(entry, ensure_ascii=False, default=str)) + "\n")

    # --- Telegram API ------------------------------------------------------------
    def _call(self, method: str, params: dict | None = None, timeout: int = 40,
              files: dict[str, tuple[str, bytes, str]] | None = None) -> dict:
        url = f"{API_BASE}/bot{self.token}/{method}"
        if files:
            boundary = uuid.uuid4().hex
            body = b""
            for k, v in (params or {}).items():
                body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
            for k, (fname, data, ctype) in files.items():
                body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                         f"filename=\"{fname}\"\r\nContent-Type: {ctype}\r\n\r\n").encode() + data + b"\r\n"
            body += f"--{boundary}--\r\n".encode()
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}"})
        else:
            req = urllib.request.Request(url, data=json.dumps(params or {}).encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                return {"ok": False, "error_code": e.code, "description": str(e)}

    def send(self, chat_id: int, text: str) -> None:
        """Send a reply. Never raises: a flaky network must not kill the bot."""
        text = text.strip() or "(empty)"
        for chunk in _split(text):
            for attempt in range(4):
                try:
                    res = self._call("sendMessage", {"chat_id": chat_id, "text": chunk,
                                                     "disable_web_page_preview": True})
                    if not res.get("ok"):
                        self.log("send_error", error=res.get("description"))
                    break
                except Exception as e:
                    self.log("send_error", error=repr(e), attempt=attempt)
                    time.sleep(3 * (attempt + 1))
        self.log("out", text=text[:2000])

    def typing(self, chat_id: int) -> None:
        try:
            self._call("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=10)
        except Exception:
            pass

    # --- main loop -----------------------------------------------------------------
    def run(self) -> None:
        if not self.token:
            raise SystemExit("TELEGRAM_BOT_TOKEN is not set. Put it in E:\\auto-bot\\.env (see README).")
        if not find_claude():
            raise SystemExit("Claude Code CLI not found - the bot needs it to think (free, uses your subscription).")
        # At Windows logon the network is often not up yet: wait for it instead of dying.
        wait = 3
        while True:
            try:
                me = self._call("getMe", timeout=20)
                if me.get("ok"):
                    self._call("deleteWebhook", {"drop_pending_updates": False}, timeout=20)
                    break
                if me.get("error_code") in (401, 404):
                    raise SystemExit(f"Telegram rejected the bot token: {me.get('description', me)}")
                self.log("startup_error", error=str(me.get("description", me)))
            except SystemExit:
                raise
            except Exception as e:
                self.log("startup_waiting_for_network", error=repr(e))
            time.sleep(wait)
            wait = min(wait * 2, 60)
        name = me["result"].get("username", "?")
        print(f"[autobot] Telegram bot @{name} is running.", flush=True)
        if self.owner is None:
            code = self.state.get("pair_code") or f"{secrets.randbelow(10**6):06d}"
            self.state["pair_code"] = code
            self._save_state()
            print(f"[autobot] Not paired yet. Open Telegram, message @{name} and send:  /pair {code}", flush=True)
        else:
            print(f"[autobot] Paired with owner id {self.owner}. Waiting for messages.", flush=True)

        if not self._worker_started:
            self._worker_started = True
            threading.Thread(target=self._worker, name="tg-worker", daemon=True).start()
        skipped = 0
        while True:
            try:
                res = self._call("getUpdates", {"offset": self._offset, "timeout": 25,
                                                "allowed_updates": ["message"]}, timeout=45)
            except Exception as e:
                self.log("poll_error", error=str(e))
                time.sleep(5)
                continue
            if not res.get("ok"):
                if res.get("error_code") == 409 and not self._warned_conflict:
                    self._warned_conflict = True
                    print("[autobot] Another program is polling this bot token (409). Use a bot "
                          "that nothing else runs.", flush=True)
                time.sleep(10)
                continue
            for upd in res.get("result", []):
                self._offset = upd["update_id"] + 1   # acknowledged now: never run twice
                try:
                    skipped += self._handle(upd)
                except Exception as e:
                    self.log("handle_error", error=repr(e))
            if skipped and self.owner and self.jobs.empty():
                self.send(self.owner, f"Skipped {skipped} old message(s) sent while the PC was off "
                                      f"(older than {STALE_SECONDS}s). Send them again if you still want them.")
                skipped = 0

    # --- incoming ----------------------------------------------------------------
    def _handle(self, upd: dict) -> int:
        msg = upd.get("message")
        if not msg:
            return 0
        chat = msg.get("chat", {})
        sender = msg.get("from", {})
        chat_id, user_id = chat.get("id"), sender.get("id")
        text = (msg.get("text") or "").strip()

        # --- pairing / authorization ---
        if self.owner is None:
            if text.lower().startswith("/pair") and chat.get("type") == "private" and self.bad_pairs < 5:
                supplied = text[5:].strip()
                if hmac.compare_digest(supplied, str(self.state.get("pair_code", ""))):
                    self.state["owner_id"] = user_id
                    self.state.pop("pair_code", None)
                    self._save_state()
                    self.log("paired", user_id=user_id)
                    print(f"[autobot] Paired with Telegram user {user_id}.", flush=True)
                    self.send(chat_id, "✅ Paired. Only you can control this PC now.\n\n" + HELP)
                else:
                    self.bad_pairs += 1
                    self.log("bad_pair", user_id=user_id)
            return 0
        if user_id != self.owner or chat_id != self.owner:
            self.log("ignored", user_id=user_id)   # silent: don't reveal anything to strangers
            return 0

        if msg.get("date", time.time()) < self.started - STALE_SECONDS:  # sent while the PC was off
            return 1
        self.log("in", text=text[:2000])

        if not text:
            self.send(chat_id, "I only understand text messages for now.")
            return 0
        cmd = text.split()[0].lower().split("@")[0]
        if cmd in ("/start", "/help"):
            self.send(chat_id, HELP)
        elif cmd == "/status":
            self.send(chat_id, self._status())
        elif cmd == "/screen":
            self._send_screen(chat_id)
        elif cmd == "/stop":
            self._stop(chat_id)
        elif cmd == "/new":
            self.state.pop("session_id", None)
            self._save_state()
            self.send(chat_id, "Fresh conversation started.")
        else:
            if self.jobs.qsize() >= MAX_QUEUE:
                self.send(chat_id, "Too many queued tasks. Wait, or /stop.")
            else:
                if self.busy.is_set():
                    self.send(chat_id, f"⏳ Queued behind the current task ({self.jobs.qsize() + 1} waiting).")
                self.jobs.put((chat_id, text))
        return 0

    def _status(self) -> str:
        sid = self.state.get("session_id")
        base = f"Busy with: {self.running_text[:120]}" if self.busy.is_set() else "Idle."
        return f"{base}\nQueued: {self.jobs.qsize()}\nSession: {sid[:8] if sid else 'none (new)'}"

    def _stop(self, chat_id: int) -> None:
        drained = 0
        while not self.jobs.empty():
            try:
                self.jobs.get_nowait()
                drained += 1
            except queue.Empty:
                break
        proc = self.proc
        if proc and proc.poll() is None:
            self.stopping.set()
            _kill_tree(proc)
            self.send(chat_id, f"■ Stopped.{f' Cleared {drained} queued.' if drained else ''}")
        else:
            self.send(chat_id, f"Nothing running.{f' Cleared {drained} queued.' if drained else ''}")

    def _send_screen(self, chat_id: int) -> None:
        try:
            import base64
            from .tools.computer import ComputerTools
            shot = ComputerTools(self.cfg).screenshot()
            data = base64.b64decode(shot[0]["source"]["data"])
            res = self._call("sendPhoto", {"chat_id": chat_id}, timeout=60,
                             files={"photo": ("screen.png", data, "image/png")})
            if not res.get("ok"):
                self.send(chat_id, f"Couldn't send the screenshot: {res.get('description')}")
        except Exception as e:
            self.send(chat_id, f"Screenshot failed: {type(e).__name__}: {e}")

    # --- running tasks -------------------------------------------------------------
    def _worker(self) -> None:
        while True:
            chat_id, text = self.jobs.get()
            self.busy.set()
            self.stopping.clear()
            self.running_text = text
            try:
                reply = self._run_claude(chat_id, text)
            except Exception as e:
                reply = f"⚠️ Something went wrong: {type(e).__name__}: {e}"
            finally:
                self.busy.clear()
                self.running_text = ""
                self.proc = None
            if reply:
                self.send(chat_id, reply)

    def _run_claude(self, chat_id: int, text: str, allow_resume: bool = True) -> str | None:
        sid = self.state.get("session_id") if allow_resume else None
        cmd = [find_claude(), "-p", text, "--output-format", "json",
               "--append-system-prompt", SYSTEM_NOTE, "--allowedTools", *ALLOWED_TOOLS, "WebFetch", "WebSearch"]
        if sid:
            cmd += ["--resume", sid]
        self.log("claude_start", resume=bool(sid))
        self.typing(chat_id)
        self.proc = proc = subprocess.Popen(
            cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=_NO_WINDOW)
        started = last_beat = last_typing = time.time()
        out = err = b""
        while True:
            try:
                out, err = proc.communicate(timeout=1)
                break
            except subprocess.TimeoutExpired:
                now = time.time()
                if now - last_typing > 4:
                    self.typing(chat_id)
                    last_typing = now
                if now - last_beat > HEARTBEAT:
                    last_beat = now
                    self.send(chat_id, f"⏳ Still working ({int((now - started) // 60)} min)… /stop to cancel.")
                if now - started > TASK_TIMEOUT:
                    _kill_tree(proc)
                    return "⚠️ That task ran over 40 minutes, so I stopped it."
        if self.stopping.is_set():
            return None                      # the /stop handler already replied
        raw = out.decode("utf-8", errors="replace").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            tail = (err.decode("utf-8", errors="replace") or raw)[-500:]
            self.log("claude_bad_output", output=tail)
            return f"⚠️ Claude Code returned something unexpected:\n{tail}"
        result = (data.get("result") or "").strip()
        if data.get("is_error") and sid and allow_resume and ("session" in result.lower() or "conversation" in result.lower()):
            self.state.pop("session_id", None)           # stale session: start fresh once
            self._save_state()
            return self._run_claude(chat_id, text, allow_resume=False)
        if data.get("session_id"):
            self.state["session_id"] = data["session_id"]
            self._save_state()
        self.log("claude_done", is_error=data.get("is_error"), turns=data.get("num_turns"),
                 seconds=round(time.time() - started))
        denied = [d.get("tool_name", "?") for d in data.get("permission_denials") or []]
        note = f"\n\n(blocked tools: {', '.join(sorted(set(denied)))})" if denied else ""
        if data.get("is_error"):
            return f"⚠️ {result or 'Claude Code reported an error.'}{note}"
        return (result or "Done.") + note


def _split(text: str, limit: int = 3900) -> list[str]:
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks or [""]


def _kill_tree(proc: subprocess.Popen) -> None:
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                   creationflags=_NO_WINDOW)


def main() -> int:
    cfg = Config()
    cfg.ensure_dirs()
    bot = TelegramBot(cfg)
    while True:                      # supervisor: any unexpected crash restarts the loop
        try:
            bot.run()
        except KeyboardInterrupt:
            return 0
        except SystemExit:
            raise                    # configuration problems (bad token, no CLI) are not retried
        except Exception as e:
            bot.log("crash", error=repr(e))
            print(f"[autobot] crashed ({e!r}); restarting in 15s", flush=True)
            time.sleep(15)
