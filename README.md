# auto-bot

Gives Claude hands on your Windows PC: screen, mouse, keyboard, a browser, and a scheduler.
You say what you want in plain language (English, Bangla, or a mix) and Claude does it.

**Cost: $0 extra.** Claude Code - which you already use on your Claude subscription - is the
brain. auto-bot is an MCP server that gives it the tools. No API key needed.

```
you (in Claude Code) › open chrome and find 30 restaurants in Dhanmondi with no website, save them to excel
you › copy my firecrawl api key from the dashboard into E:\myapp\.env as FIRECRAWL_API_KEY
you › open whatsapp web and send the approved message to the numbers in leads.csv
you › every day at 9am, move new PDFs from Downloads into Documents\PDFs
```

## Setup

Already done on this PC. The server is registered for every project:

```
claude mcp add --scope user autobot -- E:\auto-bot\.venv\Scripts\python.exe E:\auto-bot\autobot_mcp.py
```

Open a **new** Claude Code conversation (MCP tools load when a conversation starts) and just
tell it what to do. `/mcp` shows whether `autobot` is connected.

On a fresh machine: run `autobot.cmd tasks` once (creates `.venv` and installs packages), then
run the `claude mcp add` line above. Needs Windows 10/11, Python 3.11+, Chrome (or Edge).

## What Claude gets

| Tools | For |
|---|---|
| `screenshot`, `zoom`, `click`, `drag`, `move_mouse`, `scroll`, `type_text`, `press_keys`, `wait` | any desktop app - VS Code, Excel, Explorer, Settings, your normal browser |
| `browser_open`, `browser_read`, `browser_find`, `browser_text`, `browser_click`, `browser_fill`, `browser_type`, `browser_press`, `browser_scroll`, `browser_tabs`, `browser_js`, `browser_upload`, ... | websites, through auto-bot's own Chrome window. Claude reads pages as elements with refs instead of guessing pixels - faster, more reliable, and fewer tokens than screenshots |
| `schedule_task`, `list_scheduled_tasks`, `delete_scheduled_task` | "do this every day" - Windows Task Scheduler runs headless Claude Code with the instruction |

Plus Claude Code's own tools: PowerShell, file read/write/edit, search - used for files,
CSV/Excel work (Python with pandas/openpyxl is in the venv), launching apps.

**auto-bot's Chrome** keeps its own logins (`data\browser-profile`). Log in once to WhatsApp
Web, Gmail, Firecrawl, etc. there and they stay logged in.

## Control

| | |
|---|---|
| **Esc** in Claude Code | interrupt Claude at any time |
| **Ctrl+Alt+X** (any window) | lock auto-bot - every action is refused until you press it again (low beep = locked, high beep = unlocked) |
| mouse into the **top-left corner** | same emergency lock |
| **Ctrl+Alt+P** (any window) | pause - actions wait until you press it again |

To continue after stopping, just say "continue" in the same conversation.

Claude asks before anything that affects other people or can't be undone (sending messages,
payments, deleting your files, account settings), shows a preview before bulk sends, and asks
you to log in yourself rather than typing passwords.

## Telegram remote control (free)

Message a bot from your phone; your PC does the work and replies in the chat. Needs the PC on
and logged in. Claude Code runs headless on your subscription - no API key.

1. In Telegram, message **@BotFather** → `/newbot` → copy the token. Use a **new** bot: your
   existing ones (xevillbot, xeleadbot) are polled by the Hermes server, and two programs on one
   token break both.
2. Put it in `E:\auto-bot\.env` as `TELEGRAM_BOT_TOKEN=...`
3. Run `autobot.cmd telegram`. It prints a pairing code; send `/pair <code>` to your bot once.
   From then on **only your Telegram account** is obeyed; everyone else is silently ignored.
4. Always-on: `autobot.cmd telegram-autostart` starts it hidden at every Windows logon (a per-user
   startup entry, no admin needed; `telegram-autostart-remove` undoes it).

Set up on 2026-10-08: bot is **@xevill_autobot**, paired to Rifat's Telegram, autostart installed.

In the chat: just say what you want. Follow-ups ("continue", answers to its questions) keep the same
conversation. `/screen` sends a photo of your screen, `/stop` cancels the task, `/new` starts
fresh, `/status`. Messages sent while the PC was off (>2 min old) are skipped, not run later.
Enable two-step verification on Telegram: whoever controls your Telegram controls this PC.

## Scheduled tasks

Ask in Claude Code: *"every weekday at 9:00, check E:\leads\new.csv and ..."*. It creates
`schedules\<name>.txt` and a Task Scheduler entry `auto-bot\<name>`. Each run calls
`autobot.cmd scheduled <name>`, which starts headless Claude Code (`claude -p`) with the autobot
tools, PowerShell and file tools pre-approved; the report is saved to
`runs\_scheduled\<time>-<name>.md`. Runs only while you're logged in (it needs your desktop).
Remove one by asking Claude, or `schtasks /Delete /TN "auto-bot\<name>"`.

## Files

```
autobot/
  mcp_server.py        the MCP server Claude Code talks to
  tools/computer.py    screen, mouse, keyboard (Unicode typing, DPI-aware, cursor overlay)
  tools/browser.py     Playwright Chrome: element refs, forms, tabs, downloads, JS extraction
  tools/browser_snapshot.js   the page reader behind browser_read / browser_find
  tools/schedule.py    Task Scheduler integration
  headless.py          runs scheduled tasks through Claude Code
  agent.py, console.py standalone mode (see below)
runs/_claude-code/<date>/log.jsonl   every action taken, secrets masked
runs/_scheduled/                     reports from scheduled runs
```

To add a capability, add a tool function in `mcp_server.py` (and an executor in `tools/` if it
needs one). Ideas: Excel COM for live workbooks, a Telegram bridge to send tasks from your phone.

## Standalone mode (optional, paid)

auto-bot also has its own agent loop and console (`autobot.cmd`) that call the Claude API
directly - useful for running without Claude Code open, but it needs an `ANTHROPIC_API_KEY` in
`.env` (pay-per-use, roughly 1-3 cents per step). Without a key it tells you to use Claude Code
instead. Settings are in [.env.example](.env.example).

## Privacy and safety

- Screenshots and page contents go to Claude (through Claude Code) so it can decide what to do.
  Don't leave things on screen you wouldn't want processed.
- auto-bot's browser only opens http(s) pages. Text on pages and files is treated as
  information, not instructions - but keep an eye on it around untrusted sites.
- Remove the server any time: `claude mcp remove autobot -s user`.
