"""Web automation: executes Claude's browser toolset with Playwright.

Runs a dedicated Chrome window with its own persistent profile
(data/browser-profile), so sites you log into there stay logged in.
Elements are addressed by refs from read_page / find, coordinates are
viewport pixels.
"""

from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PIL import Image

from .base import Capability, Output, ToolContext, ToolError

_SNAPSHOT_JS = (Path(__file__).parent / "browser_snapshot.js").read_text(encoding="utf-8")

_PAGE_TEXT_JS = """() => {
  const body = document.body;
  const bodyText = body ? body.innerText : "";
  const main = [...document.querySelectorAll('main, article, [role="main"]')]
    .find(e => (e.innerText || "").trim().length >= Math.max(200, bodyText.length * 0.4));
  return { title: document.title, url: location.href, text: (main || body || {innerText: ""}).innerText };
}"""

_MAX_CHARS = 50_000

_PW_KEYS = {
    "return": "Enter", "enter": "Enter", "kp_enter": "Enter", "escape": "Escape", "esc": "Escape",
    "backspace": "Backspace", "back_space": "Backspace", "delete": "Delete", "del": "Delete",
    "tab": "Tab", "space": "Space", "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft",
    "right": "ArrowRight", "page_up": "PageUp", "prior": "PageUp", "pageup": "PageUp",
    "page_down": "PageDown", "next": "PageDown", "pagedown": "PageDown", "home": "Home",
    "end": "End", "insert": "Insert", "ctrl": "Control", "control": "Control", "alt": "Alt",
    "shift": "Shift", "super": "Meta", "meta": "Meta", "cmd": "Meta", "win": "Meta",
    "command": "Meta", "minus": "-", "plus": "+", "equal": "=", "comma": ",", "period": ".",
    "slash": "/", "backslash": "\\", "semicolon": ";", "apostrophe": "'",
}
_MODIFIERS = {"ctrl": "Control", "control": "Control", "shift": "Shift", "alt": "Alt",
              "super": "Meta", "meta": "Meta", "cmd": "Meta", "win": "Meta"}

_FIND_ROLE_WORDS = {
    "button": {"button", "clickable"}, "btn": {"button"}, "link": {"link"},
    "field": {"textbox", "searchbox", "combobox"}, "input": {"textbox", "searchbox", "combobox"},
    "box": {"textbox", "searchbox", "checkbox", "combobox"}, "textbox": {"textbox"},
    "search": {"searchbox"}, "checkbox": {"checkbox"}, "radio": {"radio"},
    "dropdown": {"combobox", "listbox"}, "select": {"combobox", "listbox"},
    "menu": {"menuitem", "combobox"}, "tab": {"tab"}, "image": {"img"}, "icon": {"img", "button"},
    "heading": {"heading"}, "title": {"heading"}, "toggle": {"switch", "checkbox"},
}
_STOPWORDS = {"the", "a", "an", "to", "of", "for", "on", "in", "with", "and", "or", "that", "this"}


def _sanitize(text: str, limit: int = 4096) -> str:
    """browser_state strings may not contain control or line-separator characters."""
    return re.sub(r"[\x00-\x1f\x7f  ]", " ", text or "")[:limit]


def _cap(text: str) -> str:
    if len(text) <= _MAX_CHARS:
        return text
    return text[:_MAX_CHARS] + (f"\n[Output truncated at {_MAX_CHARS} characters. Narrow it with a "
                                "smaller depth, a ref, or filter='interactive'.]")


def _pw_combo(combo: str) -> str:
    parts = [p for p in combo.split("+") if p != ""] or ["+"]
    keys = []
    for p in parts:
        low = p.lower()
        if low in _PW_KEYS:
            keys.append(_PW_KEYS[low])
        elif re.fullmatch(r"f\d{1,2}", low):
            keys.append(low.upper())
        elif len(p) == 1:
            keys.append(p.lower() if len(parts) > 1 else p)
        else:
            keys.append(p[0].upper() + p[1:])
    return "+".join(keys)


def _modifier_list(raw: Any) -> list[str]:
    if not raw:
        return []
    items = raw if isinstance(raw, list) else re.split(r"[+\s,]+", str(raw))
    mods = []
    for item in items:
        m = _MODIFIERS.get(str(item).strip().lower())
        if m:
            mods.append(m)
    return mods


class BrowserTools(Capability):
    toolset = "browser"

    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self._pw = None
        self._ctx = None
        self._tabs: dict[str, Any] = {}       # tab_id -> Page
        self._tab_seq = 0
        self._active: str | None = None
        self._frame_ids: dict[int, int] = {}  # id(Frame) -> small number for ref prefixes
        self._changes: list[dict] = []        # pending browser_state state_changes
        self._downloads: list[tuple[str, Any]] = []
        self._dl_seq = 0
        self._reported: list | None = None    # last tab inventory sent to Claude
        self._closed = False

    def definitions(self) -> list[dict]:
        configs = {"file_upload": {"enabled": True}}
        if self.cfg.browser_js:
            configs["javascript_exec"] = {"enabled": True}
        return [{"type": "browser_toolset_20260801", "configs": configs}]

    # --- lifecycle -----------------------------------------------------------
    def _ensure(self) -> None:
        if self._ctx is not None and not self._closed:
            return
        if self._ctx is not None:  # the user closed the window - start a fresh one
            self._ctx = None
            self._tabs.clear()
            self._active = None
            self._reported = None

        from playwright.sync_api import sync_playwright

        if self._pw is None:
            self._pw = sync_playwright().start()
        profile = self.cfg.browser_profile_dir
        profile.mkdir(parents=True, exist_ok=True)
        opts = dict(
            user_data_dir=str(profile),
            headless=self.cfg.browser_headless,
            viewport={"width": self.cfg.viewport_width, "height": self.cfg.viewport_height},
            device_scale_factor=1,
            accept_downloads=True,
        )
        errors = []
        for channel in dict.fromkeys([self.cfg.browser_channel or None, "msedge", None]):
            try:
                self._ctx = self._pw.chromium.launch_persistent_context(
                    channel=channel, **opts) if channel else \
                    self._pw.chromium.launch_persistent_context(**opts)
                break
            except Exception as e:  # channel not installed, profile locked, ...
                errors.append(f"{channel or 'bundled chromium'}: {str(e).splitlines()[0]}")
        if self._ctx is None:
            raise ToolError("Could not start the browser. " + " | ".join(errors) +
                            " (If the profile is in use, close the other auto-bot browser window.)")
        self._closed = False
        self._ctx.on("close", lambda _ctx: setattr(self, "_closed", True))
        self._ctx.on("page", self._on_page)
        for page in self._ctx.pages:
            self._register(page, announce=False)
        if not self._tabs:
            self._ctx.new_page()

    def close(self) -> None:
        try:
            if self._ctx:
                self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._ctx = self._pw = None

    # --- tabs ----------------------------------------------------------------
    def _register(self, page, announce: bool = True) -> str:
        for tid, p in self._tabs.items():
            if p is page:
                return tid
        self._tab_seq += 1
        tid = f"tab-{self._tab_seq}"
        self._tabs[tid] = page
        page.on("download", self._on_download)
        if announce:
            self._changes.append({"type": "tab_opened", "tab_id": tid})
        self._active = tid
        return tid

    def _on_page(self, page) -> None:
        self._register(page)

    def _on_download(self, download) -> None:
        self._dl_seq += 1
        did = f"dl-{self._dl_seq}"
        self._downloads.append((did, download))
        self._changes.append({"type": "download_started", "download_id": did,
                              "url": _sanitize(download.url)})

    def _open_tabs(self) -> dict[str, Any]:
        for tid in [t for t, p in self._tabs.items() if p.is_closed()]:
            del self._tabs[tid]
        if self._active not in self._tabs:
            self._active = next(reversed(self._tabs), None) if self._tabs else None
        return self._tabs

    def _page(self, tab_id: Any = None):
        self._ensure()
        tabs = self._open_tabs()
        if tab_id:
            if tab_id not in tabs:
                raise ToolError(f"Unknown or closed tab_id '{tab_id}'. Use list_tabs.")
            return tabs[tab_id]
        if not tabs:
            page = self._ctx.new_page()
            self._register(page, announce=False)
            return page
        return tabs[self._active]

    def _inventory(self) -> list[dict]:
        items = []
        for tid, page in self._open_tabs().items():
            try:
                title = page.title()
            except Exception:
                title = ""
            entry = {"tab_id": tid, "title": _sanitize(title), "url": _sanitize(page.url)}
            if tid == self._active:
                entry["active"] = True
            items.append(entry)
        return items[:100]

    def _state_block(self, force: bool) -> dict | None:
        self._finish_downloads()
        tabs = self._inventory()
        open_ids = {t["tab_id"] for t in tabs}
        changes = [c for c in self._changes
                   if c["type"] != "tab_opened" or c["tab_id"] in open_ids][:200]
        self._changes = []
        if not force and not changes and tabs == self._reported:
            return None
        self._reported = tabs
        block: dict = {"type": "browser_state", "tabs": tabs}
        if changes:
            block["state_changes"] = changes
        return block

    def _finish_downloads(self) -> None:
        while self._downloads:
            did, dl = self._downloads.pop(0)
            try:
                target_dir = self.cfg.downloads_dir
                target_dir.mkdir(parents=True, exist_ok=True)
                name = dl.suggested_filename or did
                target = target_dir / name
                n = 1
                while target.exists():
                    target = target_dir / f"{Path(name).stem} ({n}){Path(name).suffix}"
                    n += 1
                dl.save_as(str(target))
                self._changes.append({"type": "download_completed", "download_id": did,
                                      "url": _sanitize(dl.url), "path": _sanitize(str(target)),
                                      "size_bytes": target.stat().st_size})
            except Exception as e:
                self._changes.append({"type": "download_failed", "download_id": did,
                                      "url": _sanitize(dl.url), "error": _sanitize(str(e), 500)})

    # --- refs and targets ----------------------------------------------------
    def _frame_prefix(self, frame, page) -> str:
        if frame == page.main_frame:
            return "ref_"
        key = id(frame)
        if key not in self._frame_ids:
            self._frame_ids[key] = len(self._frame_ids) + 1
        return f"ref_f{self._frame_ids[key]}_"

    def _frame_for_ref(self, page, ref: str):
        m = re.match(r"ref_f(\d+)_", ref)
        if not m:
            return page.main_frame
        for frame in page.frames:
            if self._frame_ids.get(id(frame)) == int(m.group(1)):
                return frame
        raise ToolError(f"{ref} is stale or not found on the current page. "
                        "Re-read the page to get fresh references.")

    def _locator(self, page, ref: str):
        frame = self._frame_for_ref(page, ref)
        loc = frame.locator(f'[data-ab-ref="{ref}"]').first
        if loc.count() == 0:
            raise ToolError(f"{ref} is stale or not found on the current page. "
                            "Re-read the page to get fresh references.")
        return loc

    def _xy(self, target: Any) -> tuple[float, float]:
        if isinstance(target, dict) and "x" in target and "y" in target:
            x, y = float(target["x"]), float(target["y"])
        elif isinstance(target, (list, tuple)) and len(target) == 2:
            x, y = float(target[0]), float(target[1])
        else:
            raise ToolError("Expected a coordinate target {\"type\": \"coordinate\", \"x\": .., \"y\": ..}.")
        w, h = self.cfg.viewport_width, self.cfg.viewport_height
        if not (0 <= x <= w and 0 <= y <= h):
            raise ToolError(f"Coordinate ({x:.0f}, {y:.0f}) is outside the viewport ({w}x{h}).")
        return x, y

    def _resolve(self, page, target: Any):
        """('ref', locator) or ('xy', (x, y))"""
        if isinstance(target, dict) and target.get("type") == "ref" or \
                isinstance(target, dict) and "ref" in target:
            return "ref", self._locator(page, str(target["ref"]))
        if isinstance(target, str) and target.startswith("ref_"):
            return "ref", self._locator(page, target)
        return "xy", self._xy(target)

    # --- capture -------------------------------------------------------------
    def _png_block(self, png: bytes) -> dict:
        return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                            "data": base64.standard_b64encode(png).decode()}}

    def _screenshot(self, page) -> dict:
        return self._png_block(page.screenshot(type="png", timeout=15000))

    def _zoom(self, page, region: Any) -> dict:
        if not (isinstance(region, (list, tuple)) and len(region) == 4):
            raise ToolError("region must be [x0, y0, x1, y1].")
        x0, y0 = self._xy(region[:2])
        x1, y1 = self._xy(region[2:])
        if x1 <= x0 or y1 <= y0:
            raise ToolError("region must have x1 > x0 and y1 > y0.")
        png = page.screenshot(type="png", clip={"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0},
                              timeout=15000)
        img = Image.open(io.BytesIO(png))
        w, h = self.cfg.viewport_width, self.cfg.viewport_height
        ratio = min(w / img.width, h / img.height)
        img = img.resize((max(1, round(img.width * ratio)), max(1, round(img.height * ratio))),
                         Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return self._png_block(buf.getvalue())

    # --- reading -------------------------------------------------------------
    def _snapshot(self, page, filter_: str | None, depth: int, ref: str | None) -> list[tuple[str, list, dict]]:
        """[(frame header, nodes)] for the main frame and same-page iframes."""
        sections = []
        frames = [self._frame_for_ref(page, ref)] if ref else [page.main_frame] + page.frames[1:10]
        for frame in frames:
            if frame != page.main_frame and frame.is_detached():
                continue
            prefix = self._frame_prefix(frame, page)
            try:
                res = self._eval(frame, _SNAPSHOT_JS, {"filter": filter_, "depth": depth,
                                                       "ref": ref, "prefix": prefix})
            except Exception as e:
                if frame == page.main_frame:
                    raise ToolError(f"Could not read the page: {str(e).splitlines()[0]}")
                continue
            if res.get("error"):
                raise ToolError(res["error"])
            if res["nodes"] or frame == page.main_frame:
                header = "" if frame == page.main_frame else f"iframe \"{frame.url[:100]}\""
                sections.append((header, res["nodes"], res))
        return sections

    @staticmethod
    def _line(node: dict, indent: int, show_offscreen: bool) -> str:
        parts = [f'{"  " * indent}{node["role"]} "{node["name"]}" [{node["ref"]}]']
        for key in ("href", "type", "value"):
            if node.get(key):
                parts.append(f'{key}="{node[key]}"')
        if node.get("level"):
            parts.append(f"level={node['level']}")
        for flag in ("checked", "disabled"):
            if node.get(flag):
                parts.append(flag)
        if "expanded" in node:
            parts.append("expanded" if node["expanded"] else "collapsed")
        if show_offscreen and not node.get("inView"):
            parts.append("(offscreen)")
        return " ".join(parts)

    def _read_page(self, page, args: dict) -> str:
        filter_ = args.get("filter") or None
        depth = max(1, int(args.get("depth") or 15))
        sections = self._snapshot(page, filter_, depth, args.get("ref"))
        lines = []
        main_info = None
        for header, nodes, info in sections:
            main_info = main_info or info
            base = 0
            if header:
                lines.append(header)
                base = 1
            for node in nodes:
                lines.append(self._line(node, base + node["depth"], filter_ == "all"))
        if not lines:
            lines.append("(no matching visible elements)")
        if main_info and filter_ != "all" and not args.get("ref"):
            below = main_info["scrollHeight"] - main_info["scrollY"] - main_info["vh"]
            if below > 50:
                lines.append(f"[{below}px more below the viewport - scroll or use filter='all']")
        return _cap("\n".join(lines))

    def _find(self, page, query: str) -> str:
        words = [w for w in re.findall(r"[\w'-]+", query.lower()) if w not in _STOPWORDS]
        if not words:
            raise ToolError("query is empty.")
        roles = set().union(*(_FIND_ROLE_WORDS.get(w, set()) for w in words))
        scored = []
        for _header, nodes, _info in self._snapshot(page, "all", 60, None):
            for node in nodes:
                hay = " ".join(str(node.get(k, "")) for k in ("name", "value", "href", "type")).lower()
                score = 0.0
                for w in words:
                    if re.search(rf"\b{re.escape(w)}\b", hay):
                        score += 3
                    elif len(w) >= 4 and w[:4] in hay:
                        score += 1
                if node["role"] in roles:
                    score += 2
                if score and node.get("interactive"):
                    score += 0.5
                if score and node.get("inView"):
                    score += 0.25
                if score > 0:
                    scored.append((score, node))
        if not scored:
            return "No matching elements. Try read_page, or scroll and try again."
        scored.sort(key=lambda s: -s[0])
        return "\n".join(self._line(n, 0, True) for _, n in scored[:20])

    def _page_text(self, page) -> str:
        res = self._eval(page, _PAGE_TEXT_JS)
        text = re.sub(r"\n{3,}", "\n\n", res.get("text") or "").strip()
        return _cap(f"Title: {res.get('title', '')}\nURL: {res.get('url', '')}\n\n{text}")

    # --- dispatch ------------------------------------------------------------
    def execute(self, name: str, args: dict[str, Any], ctx: ToolContext) -> Output:
        if name in ("read_console", "read_network"):
            raise ToolError(f"{name} is not available in this browser.")
        page = None if name in ("new_tab", "list_tabs", "switch_tab", "close_tab") else \
            self._page(args.get("tab_id"))
        content: list[dict] = []
        force_state = False

        if name == "navigate":
            content.append({"type": "text", "text": self._navigate(page, str(args.get("url", "")))})
            force_state = True
        elif name == "screenshot":
            content.append(self._screenshot(page))
        elif name == "zoom":
            content.append(self._zoom(page, args.get("region")))
        elif name in ("left_click", "right_click", "middle_click", "double_click", "triple_click"):
            content.append({"type": "text", "text": self._click(page, name, args)})
        elif name == "hover":
            kind, t = self._resolve(page, args.get("target"))
            t.hover(timeout=5000) if kind == "ref" else page.mouse.move(*t)
            content.append({"type": "text", "text": "Hovered."})
        elif name == "left_click_drag":
            x0, y0 = self._xy(args.get("from"))
            x1, y1 = self._xy(args.get("target"))
            page.mouse.move(x0, y0)
            page.mouse.down()
            page.mouse.move(x1, y1, steps=12)
            page.mouse.up()
            content.append({"type": "text", "text": "Dragged."})
        elif name in ("left_mouse_down", "left_mouse_up", "mouse_move"):
            if args.get("target") is not None:
                page.mouse.move(*self._xy(args["target"]))
            if name == "left_mouse_down":
                page.mouse.down()
            elif name == "left_mouse_up":
                page.mouse.up()
            content.append({"type": "text", "text": "OK"})
        elif name == "scroll":
            content.append({"type": "text", "text": self._scroll(page, args)})
        elif name == "scroll_to":
            kind, t = self._resolve(page, args.get("target"))
            if kind != "ref":
                raise ToolError("scroll_to needs a ref target.")
            t.scroll_into_view_if_needed(timeout=5000)
            ref = args["target"].get("ref") if isinstance(args.get("target"), dict) else args.get("target")
            content.append({"type": "text", "text": f"Scrolled to element {ref}."})
        elif name == "type":
            text = str(args.get("text", ""))
            if len(text) > 300:
                page.keyboard.insert_text(text)
            else:
                page.keyboard.type(text, delay=8)
            content.append({"type": "text", "text": f"Typed {len(text)} characters."})
        elif name == "key":
            repeat = max(1, min(100, int(args.get("repeat") or 1)))
            for _ in range(repeat):
                for combo in str(args.get("text", "")).split():
                    ctx.control.checkpoint()
                    page.keyboard.press(_pw_combo(combo))
            self._settle(page)
            content.append({"type": "text", "text": f"Pressed {args.get('text')}."})
        elif name == "hold_key":
            keys = _pw_combo(str(args.get("text", ""))).split("+")
            for k in keys:
                page.keyboard.down(k)
            try:
                ctx.control.sleep(max(0.0, min(300.0, float(args.get("duration") or 1))))
            finally:
                for k in reversed(keys):
                    page.keyboard.up(k)
            content.append({"type": "text", "text": "OK"})
        elif name == "wait":
            ctx.control.sleep(max(0.0, min(300.0, float(args.get("duration") or 1))))
            content.append({"type": "text", "text": "Waited."})
        elif name == "read_page":
            content.append({"type": "text", "text": self._read_page(page, args)})
        elif name == "find":
            content.append({"type": "text", "text": self._find(page, str(args.get("query", "")))})
        elif name == "get_page_text":
            content.append({"type": "text", "text": self._page_text(page)})
        elif name == "form_input":
            content.append({"type": "text", "text": self._form_input(page, args)})
        elif name == "file_upload":
            content.append({"type": "text", "text": self._file_upload(page, args)})
        elif name == "javascript_exec":
            content.append({"type": "text", "text": self._js(page, str(args.get("text", "")))})
        elif name == "new_tab":
            self._ensure()
            new = self._ctx.new_page()
            tid = self._register(new)
            new.bring_to_front()
            content.append({"type": "text", "text": f"Opened {tid}."})
            force_state = True
        elif name == "list_tabs":
            self._ensure()
            content.append({"type": "text", "text": f"{len(self._open_tabs())} tab(s) open."})
            force_state = True
        elif name in ("switch_tab", "close_tab"):
            tid = args.get("tab_id")
            if not tid:
                raise ToolError("tab_id is required.")
            target = self._page(tid)
            if name == "switch_tab":
                self._active = tid
                target.bring_to_front()
            else:
                target.close()
                self._open_tabs()
            content.append({"type": "text", "text": f"{'Switched to' if name == 'switch_tab' else 'Closed'} {tid}."})
            force_state = True
        else:
            raise ToolError(f"Unsupported browser action '{name}'.")

        state = self._state_block(force_state)
        if state:
            content.append(state)
        return content

    # --- actions -------------------------------------------------------------
    def _settle(self, page) -> None:
        """Give a click/keypress time to start a navigation, then wait for it."""
        try:
            page.wait_for_timeout(250)
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass

    def _eval(self, target, js: str, arg: Any = None):
        """evaluate() that survives a navigation happening underneath it."""
        page = getattr(target, "page", None) or target
        for attempt in range(4):
            try:
                return target.evaluate(js, arg) if arg is not None else target.evaluate(js)
            except Exception as e:
                msg = str(e)
                transient = ("Execution context was destroyed" in msg or "navigat" in msg.lower()
                             or "Cannot find context" in msg)
                if not transient or attempt == 3:
                    raise
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=8000)
                    page.wait_for_timeout(300)
                except Exception:
                    pass

    def _navigate(self, page, url: str) -> str:
        url = url.strip()
        if url in ("back", "forward"):
            (page.go_back if url == "back" else page.go_forward)(wait_until="domcontentloaded", timeout=30000)
        else:
            if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
                url = "https://" + url
            scheme = urlparse(url).scheme.lower()
            if scheme not in ("http", "https", "about"):
                raise ToolError(f"Refusing to open a '{scheme}:' URL. Only http(s) pages are allowed.")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                msg = str(e).splitlines()[0]
                if "net::ERR_ABORTED" not in msg:  # aborted = it became a download
                    raise ToolError(f"Navigation failed: {msg}")
        try:
            page.wait_for_load_state("load", timeout=5000)
        except Exception:
            pass
        try:
            title = page.title()
        except Exception:
            title = ""
        return f"Navigated to {page.url} - title: {title}"

    def _click(self, page, name: str, args: dict) -> str:
        button = {"right_click": "right", "middle_click": "middle"}.get(name, "left")
        count = {"double_click": 2, "triple_click": 3}.get(name, 1)
        mods = _modifier_list(args.get("modifiers"))
        kind, t = self._resolve(page, args.get("target"))
        if kind == "ref":
            try:
                t.click(button=button, click_count=count, modifiers=mods or None, timeout=6000)
            except Exception as e:
                first = str(e).splitlines()[0]
                try:  # covered by an overlay / not "stable" - click it anyway
                    t.click(button=button, click_count=count, modifiers=mods or None,
                            timeout=3000, force=True)
                except Exception:
                    raise ToolError(f"Click failed: {first}")
            where = args["target"].get("ref") if isinstance(args.get("target"), dict) else t
        else:
            for m in mods:
                page.keyboard.down(m)
            try:
                page.mouse.click(t[0], t[1], button=button, click_count=count)
            finally:
                for m in reversed(mods):
                    page.keyboard.up(m)
            where = f"({t[0]:.0f}, {t[1]:.0f})"
        self._settle(page)
        return f"Clicked {where}."

    def _scroll(self, page, args: dict) -> str:
        direction = str(args.get("scroll_direction", "down")).lower()
        amount = max(1, min(30, int(args.get("scroll_amount") or 3)))
        target = args.get("target")
        if target is not None:
            kind, t = self._resolve(page, target)
            if kind == "ref":
                t.scroll_into_view_if_needed(timeout=5000)
                box = t.bounding_box()
                if box:
                    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            else:
                page.mouse.move(*t)
        else:
            page.mouse.move(self.cfg.viewport_width / 2, self.cfg.viewport_height / 2)
        step = 120 * amount
        dx = {"left": -step, "right": step}.get(direction, 0)
        dy = {"up": -step, "down": step}.get(direction, 0)
        page.mouse.wheel(dx, dy)
        page.wait_for_timeout(350)
        return f"Scrolled {direction} ×{amount}."

    def _form_input(self, page, args: dict) -> str:
        kind, loc = self._resolve(page, args.get("target"))
        if kind != "ref":
            raise ToolError("form_input needs a ref target.")
        value = args.get("value")
        info = loc.evaluate("e => [e.tagName, (e.type || '').toLowerCase()]")
        tag, typ = info[0], info[1]
        if tag == "SELECT":
            try:
                loc.select_option(label=str(value), timeout=5000)
            except Exception:
                loc.select_option(value=str(value), timeout=5000)
            return f"Selected '{value}'."
        if typ in ("checkbox", "radio"):
            checked = value if isinstance(value, bool) else str(value).strip().lower() in ("true", "1", "yes", "on")
            loc.set_checked(checked, timeout=5000)
            return f"Set {'checked' if checked else 'unchecked'}."
        loc.fill("" if value is None else str(value), timeout=5000)
        return "Value set."

    def _file_upload(self, page, args: dict) -> str:
        paths = args.get("paths") or []
        if not paths:
            raise ToolError("Provide local file paths in 'paths' (document_ids are not supported here).")
        missing = [p for p in paths if not Path(p).exists()]
        if missing:
            raise ToolError(f"File(s) not found: {', '.join(missing)}")
        kind, loc = self._resolve(page, args.get("target"))
        if kind != "ref":
            raise ToolError("file_upload needs a ref target (the file input).")
        loc.set_input_files(paths, timeout=10000)
        return f"Attached {len(paths)} file(s)."

    def _js(self, page, code: str) -> str:
        if not code.strip():
            raise ToolError("text (JavaScript code) is empty.")
        try:
            result = self._eval(page, code)
        except Exception as e:
            if "Illegal return" in str(e) or "await is only valid" in str(e):
                try:
                    result = self._eval(page, f"async () => {{\n{code}\n}}")
                except Exception as e2:
                    raise ToolError(f"JavaScript error: {str(e2).splitlines()[0]}")
            else:
                raise ToolError(f"JavaScript error: {str(e).splitlines()[0]}")
        if result is None:
            return "undefined"
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=1, default=str)
        return _cap(text)

    def describe(self, name: str, args: dict[str, Any]) -> str:
        target = args.get("target")
        if isinstance(target, dict):
            where = target.get("ref") or f"({target.get('x')}, {target.get('y')})"
        else:
            where = ""
        if name == "navigate":
            return f"browser: open {args.get('url')}"
        if name == "type":
            t = str(args.get("text", "")).replace("\n", "⏎")
            return f"browser: type \"{t[:60]}{'…' if len(t) > 60 else ''}\""
        if name == "key":
            return f"browser: key {args.get('text')}"
        if name == "find":
            return f"browser: find \"{args.get('query')}\""
        if name == "form_input":
            v = str(args.get("value"))
            return f"browser: set {where} = \"{v[:50]}\""
        if name == "javascript_exec":
            return f"browser: run JS ({len(str(args.get('text', '')))} chars)"
        if name == "scroll":
            return f"browser: scroll {args.get('scroll_direction')} ×{args.get('scroll_amount', 3)}"
        extra = f" {where}" if where else ""
        if args.get("tab_id"):
            extra += f" [{args['tab_id']}]"
        return f"browser: {name.replace('_', ' ')}{extra}"
