"""claude-sentry: sidebar TUI for the active Claude Code session.

Modes
-----
default   : sidebar for one Claude session. Top pane shows that session's file
            activity; bottom pane has Skills / Agents / Tools / Unconfirmed
            tabs (session, this-week, and all-time counts).

--inventory: full-window catalog of every installed skill and agent on this
            machine, with week + all-time count columns.

Storage (~/.claude/sentry/)
---------------------------
events.jsonl        append-only log of tool calls (written by the hooks)
config.json         divider position, theme, path-display mode
confirmations.json  confirm/deny decisions for the Unconfirmed tab
win-session/        active Claude session per Windows Terminal window
locks/              per-window auto-launch guards
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Canonical UUID (8-4-4-4-12 hex) — the shape Claude Code's /status reports.
_SESSION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def is_session_id(s: str) -> bool:
    return bool(_SESSION_ID_RE.match(s.strip()))

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

from functools import partial

from rich.text import Text

from .core import (
    SENTRY_DIR,
    LOG_FILE,
    CONFIG_FILE,
    CONFIRM_FILE,
    CLAUDE_DIR,
    HOME,
    NATIVE_COMMANDS,
    NATIVE_AGENTS,
    DOCS_COMMANDS,
    DOCS_AGENTS,
    confirm_keys,
    deny_keys,
    find_skill_or_agent_file,
    is_native,
    load_confirmations,
    load_events,
    native_doc_url,
    parse_ts,
    rename_key,
    save_confirmations,
)

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Provider
from textual.containers import Vertical, Horizontal
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, DataTable, Input, Static, TabbedContent, TabPane

# ---------------------------------------------------------------------------
# Paths & constants (sentry/claude paths now live in .core — re-imported above)

WIN_SESSION_DIR = SENTRY_DIR / "win-session"

VIEW_ALL_KEY = "__VIEW_ALL__"
CONFIRM_ALL_KEY = "__CONFIRM_ALL__"
DENY_ALL_KEY = "__DENY_ALL__"
PLACEHOLDER_KEY = "__PLACEHOLDER__"  # inert "nothing here yet" rows

KEY_DISPLAY = {
    "plus": "+",
    "equals_sign": "=",
    "minus": "-",
    "underscore": "_",
    "bracket_left": "[",
    "bracket_right": "]",
    "comma": ",",
    "period": ".",
    "space": "␣",
}


def display_key(key: str) -> str:
    return KEY_DISPLAY.get(key, key)


DEBUG_FILE = SENTRY_DIR / "debug.log"
_DEBUG_MAX_BYTES = 1 * 1024 * 1024


def debug_log(where: str, exc: BaseException) -> None:
    """Record a swallowed error so failures aren't completely invisible.
    Fail-silent and size-capped — never let logging break the UI."""
    try:
        SENTRY_DIR.mkdir(parents=True, exist_ok=True)
        if DEBUG_FILE.exists() and DEBUG_FILE.stat().st_size > _DEBUG_MAX_BYTES:
            DEBUG_FILE.write_text("", encoding="utf-8")
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with DEBUG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{ts} tui {where}: {type(exc).__name__}: {exc}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Event log helpers (load_events lives in .core; EventLog stays here because it
# carries TUI-specific incremental-read state).

class EventLog:
    """Incremental reader for events.jsonl: each call reads only the bytes
    appended since last time, instead of re-parsing the whole file every tick.

    Tracks a byte offset (binary mode, so it lines up with the file size) and
    buffers any trailing partial line until its newline arrives. If the file
    shrank (the hook trimmed it, or it was deleted), it transparently full-reloads."""

    def __init__(self) -> None:
        self._events: list[dict] = []
        self._offset = 0
        self._buf = ""

    def read(self) -> list[dict]:
        try:
            size = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
        except Exception as exc:
            debug_log("EventLog.stat", exc)
            return self._events
        if size < self._offset:  # trimmed / rotated / deleted → start over
            self._events, self._offset, self._buf = [], 0, ""
        if size == self._offset:
            return self._events
        try:
            with LOG_FILE.open("rb") as f:
                f.seek(self._offset)
                chunk = f.read()
                self._offset = f.tell()
        except Exception as exc:
            debug_log("EventLog.read", exc)
            return self._events
        data = self._buf + chunk.decode("utf-8", errors="replace")
        parts = data.split("\n")
        self._buf = parts.pop()  # trailing partial line (or "" if clean)
        for line in parts:
            line = line.strip()
            if not line:
                continue
            try:
                self._events.append(json.loads(line))
            except Exception:
                continue
        return self._events


def relative_time(ts: datetime) -> str:
    s = int((datetime.now(timezone.utc) - ts).total_seconds())
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def collapse_home(path: str) -> str:
    try:
        p = Path(path)
        try:
            return "~/" + str(p.relative_to(HOME)).replace("\\", "/")
        except ValueError:
            return str(p).replace("\\", "/")
    except Exception:
        return path


# How the Activity pane renders a path. Keying/dedup always uses the FULL path;
# only the displayed text changes.
PATH_MODES = ("name", "name1", "full")
PATH_MODE_LABELS = {
    "name": "Activity paths: filename only",
    "name1": "Activity paths: filename + 1 folder",
    "full": "Activity paths: full path",
}


def format_path(path: str, mode: str) -> str:
    """Display form of a path for the chosen mode. Dedup still keys on the full
    path elsewhere — this only affects what's shown."""
    full = collapse_home(path)  # already forward-slashed, ~ for home
    if mode == "full":
        return full
    parts = full.split("/")
    if mode == "name":
        return parts[-1]
    if mode == "name1":
        return "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    return full


# ---------------------------------------------------------------------------
# OS open/reveal

def open_with_default(path: str) -> None:
    try:
        if IS_WIN:
            os.startfile(path)  # type: ignore[attr-defined]
        elif IS_MAC:
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def reveal_in_explorer(path: str) -> None:
    """Reveal/highlight `path` in the OS file manager."""
    try:
        if IS_WIN:
            win = str(Path(path)).replace("/", "\\")
            subprocess.Popen(["explorer", f"/select,{win}"])
        elif IS_MAC:
            subprocess.Popen(["open", "-R", str(path)])
        else:
            # Most Linux file managers don't support "reveal one file"; open the
            # parent directory instead, which is the standard fallback.
            parent = str(Path(path).parent)
            subprocess.Popen(["xdg-open", parent])
    except Exception:
        pass


def copy_to_clipboard(text: str) -> bool:
    """OS-native clipboard set, no external deps. Returns True on success."""
    try:
        if IS_WIN:
            subprocess.run(["clip"], input=text, text=True, check=False)
            return True
        if IS_MAC:
            subprocess.run(["pbcopy"], input=text, text=True, check=False)
            return True
        for cmd in (
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "-b", "-i"],
        ):
            if shutil.which(cmd[0]):
                subprocess.run(cmd, input=text, text=True, check=False)
                return True
    except Exception:
        pass
    return False


_PATH_EXISTS_CACHE: dict[str, tuple[float, bool]] = {}


def _path_exists(path: str, ttl: float = 5.0) -> bool:
    """os.path.exists with a tiny TTL cache so the 2-second refresh doesn't
    do dozens of stat() calls each tick."""
    now = time.monotonic()
    cached = _PATH_EXISTS_CACHE.get(path)
    if cached and now - cached[0] < ttl:
        return cached[1]
    try:
        exists = os.path.exists(path)
    except Exception:
        exists = False
    _PATH_EXISTS_CACHE[path] = (now, exists)
    return exists


def truncate_left(s: str, width: int) -> str:
    """Truncate `s` to fit `width` chars, preserving the END (filename) with
    a leading … ellipsis."""
    if width <= 0:
        return ""
    if len(s) <= width:
        return s
    if width == 1:
        return "…"
    return "…" + s[-(width - 1):]


def truncate_right(s: str, width: int) -> str:
    """Truncate `s` to `width`, keeping the START (for sentences/messages)."""
    if width <= 0:
        return ""
    if len(s) <= width:
        return s
    if width == 1:
        return "…"
    return s[: width - 1] + "…"


def wrap_paragraphs(text: str, width: int) -> str:
    """Word-wrap each blank-line-separated paragraph to `width`, preserving the
    paragraph breaks. We wrap explicitly (rather than relying on CSS) because
    Textual's `width: auto` disables text wrapping (Textualize/textual#6014)."""
    out: list[str] = []
    for para in text.split("\n\n"):
        out.append(textwrap.fill(para, width=width) if para.strip() else "")
    return "\n\n".join(out)


def widest_line(text: str) -> int:
    return max((len(line) for line in text.splitlines()), default=0)


def in_windows_terminal() -> bool:
    return bool(os.environ.get("WT_SESSION")) and shutil.which("wt") is not None


def wt_resize_pane(direction: str) -> bool:
    """Shell out to `wt resize-pane`. Returns True if invoked, False otherwise.

    Uses the long form `--direction` because the short form `-d` collides with
    wt's global `-d` (starting directory) option and gets reinterpreted as
    a path.
    """
    if not in_windows_terminal():
        return False
    try:
        subprocess.Popen(
            ["wt", "-w", "0", "resize-pane", "--direction", direction],
            creationflags=0x00000008,  # DETACHED_PROCESS
        )
        return True
    except Exception:
        return False


def spawn_inventory_window(tab: str) -> None:
    """Open the inventory TUI in a new terminal window — best effort per OS.

    Windows Terminal : `wt -w new new-tab ... cmd /k claude-sentry --inventory`
    macOS            : open a new Terminal.app window via osascript
    Linux            : try common terminals in order, fall back to detached run
    """
    flags = 0x00000008 if IS_WIN else 0  # DETACHED_PROCESS on Windows only
    inv_args = ["claude-sentry", "--inventory", "--tab", tab]

    if in_windows_terminal():
        cmd = [
            "wt", "-w", "new", "new-tab",
            "--title", "claude-sentry inventory",
            "cmd", "/k", *inv_args,
        ]
        try:
            subprocess.Popen(cmd, creationflags=flags)
            return
        except Exception:
            pass

    if IS_MAC:
        script = (
            'tell application "Terminal" to do script '
            f'"{" ".join(inv_args)}"'
        )
        try:
            subprocess.Popen(["osascript", "-e", script])
            return
        except Exception:
            pass

    # Linux: try a few common terminals
    if not IS_WIN and not IS_MAC:
        for term in (
            ["gnome-terminal", "--"],
            ["konsole", "-e"],
            ["xterm", "-e"],
            ["alacritty", "-e"],
            ["kitty", "--"],
        ):
            if shutil.which(term[0]):
                try:
                    subprocess.Popen(term + inv_args)
                    return
                except Exception:
                    continue

    # Last resort: just run it without a new terminal window
    try:
        subprocess.Popen(inv_args, creationflags=flags)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Skill / agent discovery (for inventory + on-click open)

def _read_frontmatter(p: Path) -> dict:
    """Tiny YAML-frontmatter parser for `name:` and `description:` only."""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end]
    out: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _plugin_from_path(p: Path) -> str:
    """Best-effort: pull the plugin name out of a skill/agent path under
    ~/.claude/plugins/**. Handles both cache and marketplace layouts."""
    parts = list(p.parts)
    anchor = None
    for needle in ("skills", "agents", "commands"):
        if needle in parts:
            anchor = parts.index(needle)
            break
    if anchor is None:
        return ""
    j = anchor - 1
    while j > 0:
        seg = parts[j]
        is_version = seg == "unknown" or (
            "." in seg and seg.replace(".", "").replace("-", "").isdigit()
        )
        if is_version:
            j -= 1
            continue
        if seg in ("cache", "marketplaces", "external_plugins", "plugins"):
            if j + 1 < anchor:
                return parts[j + 1]
            return ""
        return seg
    return ""


def discover_skills() -> list[dict]:
    """All installed skills AND commands (Claude merged commands into skills, so
    a `~/.claude/commands/cc.md` is invokable as `/cc` just like a SKILL.md).
    Covers user + plugin locations so the inventory matches what the sidebar
    treats as a real skill."""
    seen: dict[str, dict] = {}

    def add(name: str, path: Path, source: str, plugin: str = "") -> None:
        if name and name not in seen:
            fm = _read_frontmatter(path)
            seen[name] = {
                "name": name,
                "description": fm.get("description", ""),
                "path": str(path),
                "source": source,
                "plugin": plugin,
            }

    # User + plugin skills (directory with SKILL.md)
    for skill_md in (CLAUDE_DIR / "skills").glob("*/SKILL.md"):
        add(skill_md.parent.name, skill_md, "user")
    for skill_md in CLAUDE_DIR.glob("plugins/**/skills/*/SKILL.md"):
        add(skill_md.parent.name, skill_md, "plugin", _plugin_from_path(skill_md))
    # User + plugin commands (a single .md file; stem is the /name)
    for cmd in (CLAUDE_DIR / "commands").glob("*.md"):
        add(cmd.stem, cmd, "user")
    for cmd in CLAUDE_DIR.glob("plugins/**/commands/*.md"):
        add(cmd.stem, cmd, "plugin", _plugin_from_path(cmd))
    return sorted(seen.values(), key=lambda d: d["name"].lower())


def discover_agents() -> list[dict]:
    seen: dict[str, dict] = {}
    for md in (CLAUDE_DIR / "agents").glob("*.md"):
        seen[md.stem] = {
            "name": md.stem,
            "description": _read_frontmatter(md).get("description", ""),
            "path": str(md),
            "source": "user",
            "plugin": "",
        }
    for md in CLAUDE_DIR.glob("plugins/**/agents/*.md"):
        if md.stem not in seen:
            seen[md.stem] = {
                "name": md.stem,
                "description": _read_frontmatter(md).get("description", ""),
                "path": str(md),
                "source": "plugin",
                "plugin": _plugin_from_path(md),
            }
    return sorted(seen.values(), key=lambda d: d["name"].lower())


# (find_skill_or_agent_file, NATIVE_COMMANDS/AGENTS, is_native, native_doc_url
# all live in .core — re-imported above so callers below see them unchanged.)


# ---------------------------------------------------------------------------
# Config (persists divider split)

def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        SENTRY_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


# (Confirmations live in .core — load/save/confirm/deny + CONFIRM_FILE are
# imported at the top.)


# ---------------------------------------------------------------------------
# Aggregation

def aggregate_counts(events_: list[dict], kind: str) -> dict[str, dict]:
    """Return {name: {'7d': int, 'all': int}} for the given event type."""
    cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)
    counts: dict[str, dict] = {}
    for e in events_:
        if e.get("type") != kind:
            continue
        name = e.get("target") or ""
        if not name:
            continue
        rec = counts.setdefault(name, {"7d": 0, "all": 0})
        rec["all"] += 1
        if parse_ts(e.get("ts", "")) >= cutoff_7d:
            rec["7d"] += 1
    return counts


def distinct_used_in_session(events_: list[dict], session_id: str, kind: str) -> int:
    if not session_id:
        return 0
    return len({
        e.get("target", "")
        for e in events_
        if e.get("type") == kind and e.get("session_id") == session_id and e.get("target")
    })


# ---------------------------------------------------------------------------
# Divider widget — thin draggable handle

class FooterBinding(Static):
    """One key+label cell inside WrappingFooter. Clickable, truncates with …."""

    DEFAULT_CSS = """
    FooterBinding {
        height: 1;
        padding: 0 1;
        color: $text;
    }
    FooterBinding:hover { background: $accent 40%; }
    .footer-key { color: $accent; text-style: bold; }
    """

    def __init__(self, key: str, label: str, action: str) -> None:
        # Markup: bold accent key, then label. Static auto-truncates with ….
        super().__init__(f"[$accent b]{key}[/] {label}", markup=True)
        self._action_id = action

    async def on_click(self, event: events.Click) -> None:
        event.stop()
        await self.app.run_action(self._action_id)


class WrappingFooter(Widget):
    """Replacement Footer: grid layout that wraps to multiple lines instead of
    dropping bindings off the end."""

    DEFAULT_CSS = """
    WrappingFooter {
        background: $surface-darken-2;
        height: auto;
        max-height: 4;
        layout: grid;
        grid-size: 2;
        grid-rows: 1;
        grid-columns: 1fr 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        for binding in self.app.BINDINGS:
            if not getattr(binding, "show", True):
                continue
            yield FooterBinding(display_key(binding.key), binding.description, binding.action)

    def on_resize(self, event: events.Resize) -> None:
        # 1 column per ~14 chars of width — keeps labels readable, wraps as needed.
        cols = max(1, min(4, event.size.width // 14))
        self.styles.grid_size_columns = cols
        # grid_columns string e.g. "1fr 1fr 1fr"
        self.styles.grid_columns = " ".join(["1fr"] * cols)


class FileMenu(ModalScreen):
    """Right-click context menu for a row. Small centred popup; the rest of
    the sidebar stays visible behind it (no full-screen dim)."""

    DEFAULT_CSS = """
    FileMenu {
        align: center middle;
        background: $surface 0%;
    }
    FileMenu > #wrap {
        background: $panel;
        border: round $accent;
        padding: 0 1;
        width: auto;     /* exact width computed in on_mount from content */
        height: auto;
    }
    FileMenu #titlebar { height: 1; width: 1fr; }
    FileMenu .path {
        width: 1fr;
        color: $text-muted;
        text-style: italic;
    }
    FileMenu #close {
        width: 3;
        content-align: center middle;
        color: $text-muted;
    }
    FileMenu #close:hover { color: $text; background: $error 60%; }
    FileMenu .warn { color: $error; text-style: bold; height: 1; }
    FileMenu .item {
        width: 100%;
        height: 1;
        padding: 0 1;
        color: $text;
    }
    FileMenu .item.-selected { background: $accent; }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("up", "move(-1)", "Up", show=False),
        Binding("down", "move(1)", "Down", show=False),
        Binding("enter", "activate", "Select", show=False),
    ]

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path
        self.missing = not _path_exists(path)
        # Action ids in display order — depends on whether the file is missing.
        self._item_ids = ["reveal", "copy"] if self.missing else ["open", "reveal", "copy"]
        self._sel = 0

    # Longest the path is allowed to make the box before it's left-truncated.
    MAX_PATH = 50

    def compose(self) -> ComposeResult:
        if self.missing:
            self._items = [("⚠  file no longer exists here", None, "warn"),
                           ("▸ Open containing folder", "reveal", "item"),
                           ("▸ Copy path", "copy", "item")]
        else:
            self._items = [("▸ Open file", "open", "item"),
                           ("▸ Show in file browser", "reveal", "item"),
                           ("▸ Copy path", "copy", "item")]
        with Vertical(id="wrap"):
            with Horizontal(id="titlebar"):
                yield Static("", classes="path")  # text set in on_mount
                yield Static("✕", id="close")
            for label, iid, cls in self._items:
                if iid is None:
                    yield Static(label, classes=cls)
                else:
                    yield Static(label, id=iid, classes=cls)

    def on_mount(self) -> None:
        # Auto-size: width fits the widest of {the menu rows, the path}. The
        # path is left-truncated so it never pushes the box past MAX_PATH.
        rows_w = max(len(label) for label, _, _ in self._items)
        full_path = collapse_home(self.path)
        # titlebar = path + 1 gap + ✕(1); reserve 2 for that beyond the path text
        path_budget = max(rows_w - 2, min(len(full_path), self.MAX_PATH))
        path_text = truncate_left(full_path, path_budget)
        self.query_one(".path", Static).update(path_text)
        content_w = max(rows_w, len(path_text) + 2)
        # width is border-box: +2 padding (0 1) +2 round border.
        self.query_one("#wrap").styles.width = content_w + 4
        # Open with the top option pre-selected (yellow).
        self._select(0)

    def _select(self, index: int) -> None:
        """Highlight item at `index`; index < 0 clears all selection."""
        n = len(self._item_ids)
        self._sel = index if (0 <= index < n) else -1
        for i, iid in enumerate(self._item_ids):
            try:
                w = self.query_one(f"#{iid}", Static)
            except Exception:
                continue
            w.set_class(i == self._sel, "-selected")

    def action_move(self, delta: int) -> None:
        n = len(self._item_ids)
        if n == 0:
            return
        # If nothing selected (mouse moved off), Down picks first / Up picks last.
        start = self._sel if self._sel >= 0 else (-1 if delta > 0 else 0)
        self._select((start + delta) % n)

    def action_activate(self) -> None:
        if 0 <= self._sel < len(self._item_ids):
            self._do(self._item_ids[self._sel])

    def on_mouse_move(self, event: events.MouseMove) -> None:
        # Mouse hover takes over selection; moving off the items clears it.
        x, y = event.screen_x, event.screen_y
        for i, iid in enumerate(self._item_ids):
            try:
                if self.query_one(f"#{iid}", Static).region.contains(x, y):
                    if self._sel != i:
                        self._select(i)
                    return
            except Exception:
                continue
        if self._sel != -1:
            self._select(-1)

    def _do(self, action: str) -> None:
        if action == "open" and not self.missing:
            open_with_default(self.path)
        elif action == "reveal":
            if self.missing:
                try:
                    open_with_default(str(Path(self.path).parent))
                except Exception:
                    pass
            else:
                reveal_in_explorer(self.path)
        elif action == "copy":
            if copy_to_clipboard(self.path):
                self.app.notify("Path copied to clipboard", timeout=1.5)
            else:
                self.app.notify("Copy failed — no clipboard tool found",
                                severity="warning", timeout=2)
        self.dismiss()

    def on_click(self, event: events.Click) -> None:
        node = event.widget
        nid = getattr(node, "id", None) if node is not None else None
        if nid in ("open", "reveal", "copy"):
            self._do(nid)
            return
        if nid == "close":
            self.dismiss()
            return
        # Click outside the #wrap box closes the menu.
        cur = node
        while cur is not None:
            if getattr(cur, "id", None) == "wrap":
                return  # inside the box — keep open
            cur = cur.parent
        self.dismiss()


class NativeInfo(ModalScreen):
    """Context menu for a Claude-native skill/agent (no file to open). Offers a
    deep link to the Claude docs. Same look/feel as FileMenu."""

    DEFAULT_CSS = """
    NativeInfo { align: center middle; background: $surface 0%; }
    NativeInfo > #wrap {
        background: $panel;
        border: round $accent;
        padding: 0 1;
        width: auto;
        height: auto;
    }
    NativeInfo #titlebar { height: 1; width: 1fr; }
    NativeInfo .name { width: 1fr; color: $text-muted; text-style: italic; }
    NativeInfo #close {
        width: 3; content-align: center middle; color: $text-muted;
    }
    NativeInfo #close:hover { color: $text; background: $error 60%; }
    NativeInfo .item { width: 100%; height: 1; padding: 0 1; color: $text; }
    NativeInfo .item.-selected { background: $accent; }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("up", "move(-1)", "Up", show=False),
        Binding("down", "move(1)", "Down", show=False),
        Binding("enter", "activate", "Select", show=False),
    ]

    def __init__(self, kind: str, name: str, url: str) -> None:
        super().__init__()
        self.item_kind = kind
        self.item_name = name
        self.url = url
        self._item_ids = ["docs", "copy"]
        self._sel = 0

    @property
    def _title(self) -> str:
        return f"{self.item_name} · Claude native {self.item_kind}"

    def compose(self) -> ComposeResult:
        self._items = [("▸ View on Claude docs", "docs"),
                       ("▸ Copy doc link", "copy")]
        with Vertical(id="wrap"):
            with Horizontal(id="titlebar"):
                yield Static(self._title, classes="name")
                yield Static("✕", id="close")
            for label, iid in self._items:
                yield Static(label, id=iid, classes="item")

    def on_mount(self) -> None:
        title_w = len(self._title) + 2  # + ✕ gap
        rows_w = max(len(label) for label, _ in self._items)
        content_w = max(rows_w, title_w)
        self.query_one("#wrap").styles.width = content_w + 4
        self._select(0)

    def _select(self, index: int) -> None:
        n = len(self._item_ids)
        self._sel = index if (0 <= index < n) else -1
        for i, iid in enumerate(self._item_ids):
            try:
                self.query_one(f"#{iid}", Static).set_class(i == self._sel, "-selected")
            except Exception:
                pass

    def action_move(self, delta: int) -> None:
        start = self._sel if self._sel >= 0 else (-1 if delta > 0 else 0)
        self._select((start + delta) % len(self._item_ids))

    def action_activate(self) -> None:
        if 0 <= self._sel < len(self._item_ids):
            self._do(self._item_ids[self._sel])

    def on_mouse_move(self, event: events.MouseMove) -> None:
        x, y = event.screen_x, event.screen_y
        for i, iid in enumerate(self._item_ids):
            try:
                if self.query_one(f"#{iid}", Static).region.contains(x, y):
                    if self._sel != i:
                        self._select(i)
                    return
            except Exception:
                continue
        if self._sel != -1:
            self._select(-1)

    def _do(self, action: str) -> None:
        if action == "docs":
            open_with_default(self.url)  # opens in the default browser
        elif action == "copy":
            if copy_to_clipboard(self.url):
                self.app.notify("Doc link copied", timeout=1.5)
        self.dismiss()

    def on_click(self, event: events.Click) -> None:
        node = event.widget
        nid = getattr(node, "id", None) if node is not None else None
        if nid in ("docs", "copy"):
            self._do(nid)
            return
        if nid == "close":
            self.dismiss()
            return
        cur = node
        while cur is not None:
            if getattr(cur, "id", None) == "wrap":
                return
            cur = cur.parent
        self.dismiss()


class ConfirmDialog(ModalScreen[bool]):
    """Small yes/no confirmation popup. Dismisses True (confirm) or False."""

    DEFAULT_CSS = """
    ConfirmDialog { align: center middle; background: $surface 0%; }
    ConfirmDialog > #wrap {
        background: $panel;
        border: round $accent;
        padding: 0 1;
        width: auto;     /* exact width is computed in on_mount from content */
        height: auto;
    }
    ConfirmDialog .msg { padding: 1 0; height: auto; }
    ConfirmDialog .item {
        width: 100%;
        height: 1;
        padding: 0 1;
        color: $text;
    }
    ConfirmDialog .item.-selected { background: $accent; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("up", "move(-1)", "Up", show=False),
        Binding("down", "move(1)", "Down", show=False),
        Binding("left", "move(-1)", "Up", show=False),
        Binding("right", "move(1)", "Down", show=False),
        Binding("enter", "activate", "Select", show=False),
    ]

    def __init__(self, message: str, confirm_label: str = "Yes",
                 cancel_label: str = "Cancel") -> None:
        super().__init__()
        self._message = message
        self._labels = {"yes": confirm_label, "no": cancel_label}
        self._ids = ["yes", "no"]
        self._sel = 0

    # Max content width before the message starts wrapping onto more lines.
    MAX_CONTENT = 48

    def compose(self) -> ComposeResult:
        self._wrapped = wrap_paragraphs(self._message, self.MAX_CONTENT)
        with Vertical(id="wrap"):
            yield Static(self._wrapped, classes="msg")
            yield Static(f"▸ {self._labels['yes']}", id="yes", classes="item")
            yield Static(f"▸ {self._labels['no']}",  id="no",  classes="item")

    def on_mount(self) -> None:
        # Auto-size the box to the actual content: the widest wrapped message
        # line, or the longest action row, whichever is bigger. `width: auto`
        # can't do this for wrapped text (Textualize/textual#6014), so we set
        # the exact width ourselves.
        items_w = max(len(f"▸ {self._labels[i]}") for i in self._ids)
        content_w = max(widest_line(self._wrapped), items_w)
        # width is border-box: +2 padding (0 1) +2 round border.
        self.query_one("#wrap").styles.width = content_w + 4
        self._select(0)

    def _select(self, index: int) -> None:
        n = len(self._ids)
        self._sel = index if 0 <= index < n else -1
        for i, iid in enumerate(self._ids):
            try:
                self.query_one(f"#{iid}", Static).set_class(i == self._sel, "-selected")
            except Exception:
                pass

    def action_move(self, delta: int) -> None:
        start = self._sel if self._sel >= 0 else (-1 if delta > 0 else 0)
        self._select((start + delta) % len(self._ids))

    def action_activate(self) -> None:
        self.dismiss(self._sel == 0)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_mouse_move(self, event: events.MouseMove) -> None:
        x, y = event.screen_x, event.screen_y
        for i, iid in enumerate(self._ids):
            try:
                if self.query_one(f"#{iid}", Static).region.contains(x, y):
                    if self._sel != i:
                        self._select(i)
                    return
            except Exception:
                continue
        if self._sel != -1:
            self._select(-1)

    def on_click(self, event: events.Click) -> None:
        nid = getattr(event.widget, "id", None) if event.widget is not None else None
        if nid == "yes":
            self.dismiss(True)
            return
        if nid == "no":
            self.dismiss(False)
            return
        cur = event.widget
        while cur is not None:
            if getattr(cur, "id", None) == "wrap":
                return  # inside the box
            cur = cur.parent
        self.dismiss(False)  # click outside cancels


class SentryTable(DataTable):
    """DataTable that emits a RightClicked message on right mouse-down.

    Why a subclass: DataTable._on_click handles the Click and calls event.stop()
    without ever checking which button was pressed — so a right-click reaches it
    as an ordinary 'select this row' and never bubbles to the App. We intercept
    earlier, at mouse-DOWN (which DataTable doesn't handle), so the right-click
    is caught before the Click is even synthesised.
    """

    class RightClicked(events.Message):
        def __init__(self, table: "SentryTable", row_key: str, column: int) -> None:
            super().__init__()
            self.table = table
            self.row_key = row_key
            self.column = column

    class LeftClicked(events.Message):
        def __init__(self, table: "SentryTable", row_key: str, column: int) -> None:
            super().__init__()
            self.table = table
            self.row_key = row_key
            self.column = column

    def _cell_at(self, event: events.MouseDown) -> tuple[str, int] | None:
        """Return (row_key, column_index) for the clicked cell, or None."""
        meta = getattr(event.style, "meta", None) or {}
        if "row" not in meta or "column" not in meta:
            return None
        try:
            coord = Coordinate(int(meta["row"]), int(meta["column"]))
        except Exception:
            return None
        if coord.row < 0:  # header
            return None
        try:
            row_key = self.coordinate_to_cell_key(coord).row_key.value
        except Exception:
            return None
        return (row_key, coord.column) if row_key else None

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 3:  # right-click → context menu
            event.stop()
            event.prevent_default()
            hit = self._cell_at(event)
            if hit:
                self.post_message(self.RightClicked(self, hit[0], hit[1]))
        elif event.button == 1:  # left-click
            hit = self._cell_at(event)
            if hit:
                # Clicking a real row (re)shows the highlight; don't stop the
                # event so DataTable still moves the cursor there.
                self.show_cursor = True
                self.post_message(self.LeftClicked(self, hit[0], hit[1]))
            else:
                # Clicked blank space below the rows (or the header) → clear the
                # default row selection.
                self.show_cursor = False


class LinkSession(ModalScreen[str]):
    """Paste-a-session-id modal. Dismisses with the entered id (or '').

    Matches the other modals: transparent backdrop, auto-sized to its content,
    flat height-1 action rows. The input field gives a fixed paste width so a
    full UUID is always visible.
    """

    INPUT_WIDTH = 40  # comfortably fits a 36-char UUID

    DEFAULT_CSS = """
    LinkSession { align: center middle; background: $surface 0%; }
    LinkSession > #wrap {
        background: $panel;
        border: round $accent;
        padding: 0 1;
        width: auto;   /* exact width computed in on_mount */
        height: auto;
    }
    LinkSession .head { text-style: bold; padding: 1 0 0 0; height: auto; }
    LinkSession .hint { color: $text-muted; padding: 1 0; height: auto; }
    LinkSession Input { margin: 0 0 1 0; }
    LinkSession .error { color: $error; height: auto; padding: 0 0 1 0; }
    LinkSession .item {
        width: 100%;
        height: 1;
        padding: 0 1;
        color: $text;
    }
    LinkSession .item.-selected { background: $accent; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("enter", "submit", "Link", show=False),
    ]

    def __init__(self, intro: str = "") -> None:
        super().__init__()
        self._intro = intro

    def compose(self) -> ComposeResult:
        self._intro_wrapped = (
            wrap_paragraphs(self._intro, self.INPUT_WIDTH) if self._intro else ""
        )
        self._hint_wrapped = wrap_paragraphs(
            "Run  /status  in Claude to see its session ID, then paste it below.",
            self.INPUT_WIDTH,
        )
        with Vertical(id="wrap"):
            if self._intro_wrapped:
                yield Static(self._intro_wrapped, classes="head")
            yield Static(self._hint_wrapped, classes="hint")
            yield Input(placeholder="paste session id (UUID)…", id="sid")
            yield Static("", id="error", classes="error")  # filled on bad input
            yield Static("▸ Link this session", id="ok", classes="item")
            yield Static("▸ Cancel",            id="cancel", classes="item")

    def on_mount(self) -> None:
        text_w = max(widest_line(self._intro_wrapped),
                     widest_line(self._hint_wrapped),
                     len("▸ Link this session"),
                     self.INPUT_WIDTH)
        self.query_one("#wrap").styles.width = text_w + 4  # border-box
        self.query_one("#sid", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss("")

    def _try_submit(self) -> None:
        """Validate before linking: empty cancels, a non-UUID shows an inline
        error and keeps the modal open, a valid UUID links."""
        val = self.query_one("#sid", Input).value.strip()
        if not val:
            self.dismiss("")
            return
        if not is_session_id(val):
            self.query_one("#error", Static).update(
                "⚠  That's not a valid session ID. Run /status in Claude "
                "and paste the full UUID (it looks like "
                "1234abcd-…-1234567890ab)."
            )
            self.query_one("#sid", Input).focus()
            return
        self.dismiss(val)

    def action_submit(self) -> None:
        self._try_submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._try_submit()

    def on_click(self, event: events.Click) -> None:
        nid = getattr(event.widget, "id", None) if event.widget is not None else None
        if nid == "ok":
            self._try_submit()
            return
        if nid == "cancel":
            self.dismiss("")
            return
        cur = event.widget
        while cur is not None:
            if getattr(cur, "id", None) == "wrap":
                return  # inside the box
            cur = cur.parent
        self.dismiss("")  # click outside cancels


class RenameModal(ModalScreen[str]):
    """Rename an unconfirmed skill/agent to its new canonical name.

    Validates that the new name exists on disk or is a native built-in before
    dismissing. Same visual style as LinkSession: transparent backdrop, round
    accent border, inline error, Esc/click-outside to cancel.
    """

    INPUT_WIDTH = 36

    DEFAULT_CSS = """
    RenameModal { align: center middle; background: $surface 0%; }
    RenameModal > #wrap {
        background: $panel;
        border: round $accent;
        padding: 0 1;
        width: auto;
        height: auto;
    }
    RenameModal .head  { text-style: bold; padding: 1 0 0 0; height: auto; }
    RenameModal .hint  { color: $text-muted; padding: 1 0; height: auto; }
    RenameModal Input  { margin: 0 0 1 0; }
    RenameModal .error { color: $error; height: auto; padding: 0 0 1 0; }
    RenameModal .item  { width: 100%; height: 1; padding: 0 1; color: $text; }
    RenameModal .item.-selected { background: $accent; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("enter",  "submit", "Rename", show=False),
    ]

    def __init__(self, kind: str, old_name: str) -> None:
        super().__init__()
        self._kind = kind
        self._old_name = old_name

    def compose(self) -> ComposeResult:
        label = "skill" if self._kind == "skill" else "agent"
        hint = wrap_paragraphs(
            f"Enter the new {label} name (without leading /).\n"
            "It must be installed on disk or be a native built-in.",
            self.INPUT_WIDTH,
        )
        with Vertical(id="wrap"):
            yield Static(f"↻  Rename  {self._old_name}", classes="head")
            yield Static(hint, classes="hint")
            yield Input(placeholder=f"new {label} name…", id="name")
            yield Static("", id="error", classes="error")
            yield Static("▸ Rename", id="ok",     classes="item")
            yield Static("▸ Cancel", id="cancel", classes="item")

    def on_mount(self) -> None:
        hint_lines = self.query_one(".hint", Static).renderable
        w = max(
            len(f"↻  Rename  {self._old_name}"),
            len("▸ Rename"),
            self.INPUT_WIDTH,
        )
        self.query_one("#wrap").styles.width = w + 4
        self.query_one("#name", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss("")

    def _try_submit(self) -> None:
        val = self.query_one("#name", Input).value.strip().lstrip("/")
        if not val:
            self.dismiss("")
            return
        if val == self._old_name:
            self.query_one("#error", Static).update("⚠  Same name — enter a different one.")
            self.query_one("#name", Input).focus()
            return
        exists = (find_skill_or_agent_file(self._kind, val) is not None
                  or is_native(self._kind, val))
        if not exists:
            kind_label = "skill" if self._kind == "skill" else "agent"
            self.query_one("#error", Static).update(
                f"⚠  {kind_label} \"{val}\" not found — check it matches "
                "a command installed in ~/.claude/commands/ or is a "
                "native built-in."
            )
            self.query_one("#name", Input).focus()
            return
        self.dismiss(val)

    def action_submit(self) -> None:
        self._try_submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._try_submit()

    def on_click(self, event: events.Click) -> None:
        nid = getattr(event.widget, "id", None) if event.widget is not None else None
        if nid == "ok":
            self._try_submit()
            return
        if nid == "cancel":
            self.dismiss("")
            return
        cur = event.widget
        while cur is not None:
            if getattr(cur, "id", None) == "wrap":
                return  # inside the box
            cur = cur.parent
        self.dismiss("")  # click outside cancels


class Divider(Static):
    """Single-line draggable separator. Updates app.top_height while dragged."""

    DEFAULT_CSS = """
    Divider {
        height: 1;
        background: $primary-darken-2;
        color: $text-muted;
        content-align: center middle;
        text-style: dim;
    }
    Divider:hover { background: $accent 40%; color: $text; }
    """

    def __init__(self) -> None:
        super().__init__("──── drag or +/- to resize ────")

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self.capture_mouse(True)
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        self.capture_mouse(False)
        self.app.persist_divider()  # type: ignore[attr-defined]
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        # Only act while we hold mouse capture
        if event.button == 0:
            return
        # event.screen_y is the absolute Y in screen coords
        self.app.set_top_height(event.screen_y)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Command-palette provider: the three Activity path-display modes show up as
# entries in the Settings palette (Ctrl+P / s).

class PathModeProvider(Provider):
    def _entries(self):
        cur = getattr(self.app, "_path_mode", "full")
        for mode in PATH_MODES:
            tick = "● " if mode == cur else "○ "
            yield mode, tick + PATH_MODE_LABELS[mode]

    async def discover(self):
        for mode, label in self._entries():
            yield DiscoveryHit(label, partial(self.app.set_path_mode, mode))

    async def search(self, query: str):
        matcher = self.matcher(query)
        for mode, label in self._entries():
            score = matcher.match(label)
            if score > 0:
                yield Hit(score, matcher.highlight(label),
                          partial(self.app.set_path_mode, mode))


# ---------------------------------------------------------------------------
# Main session app

class SentryApp(App):
    COMMANDS = App.COMMANDS | {PathModeProvider}
    CSS = """
    Screen { background: $surface; }
    /* Modals float over the panes — keep their screen background transparent
       (the app-level Screen rule would otherwise make them solid). */
    FileMenu, ConfirmDialog, LinkSession, NativeInfo { background: $surface 0%; }
    #top, #bottom { border: round $primary-darken-2; }
    #top { height: 50%; }
    #bottom { height: 1fr; }
    DataTable { height: 1fr; background: $surface; }
    .title {
        background: $primary 30%;
        color: $text;
        padding: 0 1;
        text-style: bold;
        height: 1;
    }
    TabbedContent { height: 1fr; }
    TabPane { padding: 0; }
    Footer { background: $surface-darken-2; }
    #hint {
        height: auto;
        background: $surface-darken-2;
        color: $text-muted;
        text-align: center;
    }
    """

    BINDINGS = [
        # App-level controls (shown in the footer)
        Binding("+", "grow_focused", "Taller"),
        Binding("=", "grow_focused", "Taller", show=False),
        Binding("-", "shrink_focused", "Shorter"),
        Binding("_", "shrink_focused", "Shorter", show=False),
        Binding("o", "open_selected", "Open"),
        Binding("enter", "open_selected", "Open", show=False),
        Binding("r", "reveal_selected", "Reveal"),
        Binding("c", "copy_selected", "Copy"),
        Binding("m", "row_menu", "Menu"),
        Binding("v", "view_all", "View all"),
        Binding("l", "link_session", "Link"),
        Binding("s", "command_palette", "Settings"),
        Binding("ctrl+p", "command_palette", "Settings", show=False),
        Binding("q", "quit", "Quit"),
        Binding("escape", "close_overlay", "Close", show=False),
    ]

    TITLE = "claude-sentry"

    def __init__(self, session_id: str = "") -> None:
        super().__init__()
        self.session_id = session_id
        cfg = load_config()
        self._top_height: int | None = cfg.get("top_height")
        saved_theme = cfg.get("theme")
        if saved_theme:
            self.theme = saved_theme
        mode = cfg.get("path_mode", "full")
        self._path_mode = mode if mode in PATH_MODES else "full"
        self._cols_ready = False  # set once on_mount has added columns
        self._eventlog = EventLog()  # incremental reader for the 2s refresh
        # Auto-resume support via claude-with-sentry launcher. The launcher
        # generates CLAUDE_SENTRY_LINK_ID, the SessionStart hook writes
        # session_id to a state file keyed by that UUID, and on SIGUSR1 we
        # re-read the file and re-scope this app.
        self._link_id = os.environ.get("CLAUDE_SENTRY_LINK_ID", "")
        self._link_state_file = (
            Path.home() / ".claude" / "state" / "sentry-links" / f"{self._link_id}.json"
            if self._link_id else None
        )

    def set_path_mode(self, mode: str) -> None:
        if mode not in PATH_MODES:
            return
        self._path_mode = mode
        cfg = load_config()
        cfg["path_mode"] = mode
        save_config(cfg)
        self.refresh_data(force=True)
        self.notify(f"{PATH_MODE_LABELS[mode]}", timeout=2)

    def watch_theme(self, theme: str) -> None:
        cfg = load_config()
        cfg["theme"] = theme
        save_config(cfg)

    # ---- compose --------------------------------------------------------

    @property
    def _activity_label(self) -> str:
        return "Activity (this session)" if self.session_id else "Activity (not linked)"

    def compose(self) -> ComposeResult:
        with Vertical(id="top"):
            yield Static(f"▼ {self._activity_label}", classes="title", id="top-title")
            yield SentryTable(id="edits-table", cursor_type="row",
                              zebra_stripes=True, cell_padding=1)
        yield Divider()
        with Vertical(id="bottom"):
            with TabbedContent(initial="skills-tab", id="bottom-tabs"):
                with TabPane("Skills", id="skills-tab"):
                    yield SentryTable(id="skills-table", cursor_type="row",
                                      zebra_stripes=True, cell_padding=1)
                with TabPane("Agents", id="agents-tab"):
                    yield SentryTable(id="agents-table", cursor_type="row",
                                      zebra_stripes=True, cell_padding=1)
                with TabPane("Tools", id="tools-tab"):
                    yield SentryTable(id="tools-table", cursor_type="row",
                                      zebra_stripes=True, cell_padding=1)
                with TabPane("Unconfirmed", id="unconfirmed-tab"):
                    yield SentryTable(id="unconfirmed-table", cursor_type="row",
                                      zebra_stripes=True, cell_padding=1)
        yield WrappingFooter()
        # Sidebar-width resize is a terminal feature: Windows Terminal has a key
        # binding; other terminals (iTerm2 etc.) resize by dragging the pane edge.
        # Hint text for the resize keybinding — varies by platform.
        resize = ("alt+shift+←→: widen / narrow" if IS_WIN
                  else "cmd+alt+←→: widen / narrow")
        yield Static(
            "left-click: select    right-click: options\n" + resize,
            id="hint",
        )

    # ---- lifecycle ------------------------------------------------------

    def on_mount(self) -> None:
        # Edit columns: act | file | -N | +N | when.
        # cell_padding=1 gives a 1-col gap between every column; the file column
        # is left-truncated at render time so the right-hand columns stay visible
        # and there's never a horizontal scrollbar.
        edits = self.query_one("#edits-table", DataTable)
        edits.add_column("act", key="act", width=3)
        edits.add_column("file", key="file")
        edits.add_column("−", key="rm", width=4)
        edits.add_column("+", key="ad", width=4)
        edits.add_column("when", key="when", width=4)

        # Skills/Agents: name on the LEFT (left-truncated if too long),
        # session/week/all counts pinned on the RIGHT and always visible.
        for sel in ("#skills-table", "#agents-table"):
            t = self.query_one(sel, DataTable)
            t.add_column("name", key="name")
            t.add_column("session", key="ses", width=7)
            t.add_column("week", key="7d", width=4)
            t.add_column("all", key="all", width=4)

        tools = self.query_one("#tools-table", DataTable)
        tools.add_column("tool", key="name")
        tools.add_column("uses", key="uses", width=5)

        # Unconfirmed review queue: name | kind | seen | ✓ | ✗ | ↻
        unconf = self.query_one("#unconfirmed-table", DataTable)
        unconf.add_column("name", key="name")
        unconf.add_column("kind", key="kind", width=5)
        unconf.add_column("seen", key="seen", width=4)
        unconf.add_column("✓", key="ok",     width=1)
        unconf.add_column("✗", key="no",     width=1)
        unconf.add_column("↻", key="rename", width=1)
        if self._top_height is None:
            self._top_height = max(3, self.screen.size.height // 2)
        self.query_one("#top").styles.height = self._top_height
        self._cols_ready = True
        self.refresh_data()
        self.set_interval(2.0, self.refresh_data)
        # Register this process in the launcher's state file. The state file's
        # session_id is the source of truth — if the hook updated it between
        # the launcher reading it and us mounting, adopt that newer value.
        self._register_resume_link()
        # Opened without a session → prompt to link the current Claude chat.
        # (Skipped when we're auto-linked by CLAUDE_SENTRY_LINK_ID — the hook
        # will have written a session_id either before or shortly after mount.)
        if not self.session_id and not self._link_id:
            self.call_after_refresh(lambda: self.action_link_session(self.GLOBAL_INTRO))

    def _register_resume_link(self) -> None:
        """Mark this app as the live sentry for its CLAUDE_SENTRY_LINK_ID by
        writing our PID into the state file. The hook also writes session_id
        there. The 2s refresh tick polls this file (_poll_link_state) to pick
        up any session change driven by /resume — no signal handler needed,
        which sidesteps asyncio/threading fragility and stale-PID issues."""
        if not self._link_state_file:
            return
        try:
            self._link_state_file.parent.mkdir(parents=True, exist_ok=True)
            data: dict = {}
            if self._link_state_file.exists():
                try:
                    data = json.loads(self._link_state_file.read_text())
                except Exception:
                    data = {}
            data["sentry_pid"] = os.getpid()
            self._link_state_file.write_text(json.dumps(data))
            # Seed with current session_id so the poll only reacts to changes.
            self._last_polled_sid = (data.get("session_id") or "").strip()
        except Exception:
            return

    def _poll_link_state(self) -> None:
        """Called from refresh_data each tick. If the state file's session_id
        has changed since we last looked, re-scope and force a refresh."""
        if not self._link_state_file or not self._link_state_file.exists():
            return
        try:
            data = json.loads(self._link_state_file.read_text())
        except Exception:
            return
        new_sid = (data.get("session_id") or "").strip()
        if not new_sid or new_sid == getattr(self, "_last_polled_sid", ""):
            return
        self._last_polled_sid = new_sid
        if new_sid != self.session_id:
            self.session_id = new_sid
            # refresh_data is the caller, so it will redraw with the new sid.
    # ---- actions --------------------------------------------------------

    def action_grow_focused(self) -> None:
        self._adjust_focused(+1)

    def action_shrink_focused(self) -> None:
        self._adjust_focused(-1)

    def _which_pane(self) -> str:
        """Return 'top' or 'bottom' based on what's currently focused."""
        node = self.focused
        while node is not None:
            nid = getattr(node, "id", None)
            if nid in ("top", "bottom"):
                return nid
            node = node.parent
        return "top"

    def _adjust_focused(self, delta: int) -> None:
        # Bottom is `1fr` (fills remainder), so growing bottom == shrinking top.
        if self._which_pane() == "bottom":
            self._adjust_top(-delta)
        else:
            self._adjust_top(delta)

    def action_view_all(self) -> None:
        active = self.query_one("#bottom-tabs", TabbedContent).active
        tab = "agents" if active == "agents-tab" else "skills"
        self._confirm_inventory(tab)

    def _confirm_inventory(self, tab: str) -> None:
        """Ask before spawning a new terminal window for the full inventory."""
        msg = ("Open the full inventory?\n\n"
               "This opens a NEW terminal window listing every installed skill "
               "and agent with their global 7-day and all-time usage counts.")

        def _done(ok: bool | None) -> None:
            if ok:
                spawn_inventory_window(tab)

        self.push_screen(ConfirmDialog(msg, confirm_label="Open inventory"), _done)

    GLOBAL_INTRO = (
        "claude-sentry is in global mode — it's showing activity across ALL "
        "Claude sessions. To track just your current chat, link it below."
    )

    def action_link_session(self, intro: str = "") -> None:
        """Open the paste-a-session-id modal; on confirm, re-scope to it."""
        def _done(sid: str | None) -> None:
            if not sid:
                return
            self.session_id = sid
            # Persist for this WT window so a future restart re-attaches.
            wt = os.environ.get("WT_SESSION")
            if wt:
                try:
                    WIN_SESSION_DIR.mkdir(parents=True, exist_ok=True)
                    (WIN_SESSION_DIR / f"{wt}.txt").write_text(sid, encoding="utf-8")
                except Exception:
                    pass
            # Update the header + force a re-render with the new filter.
            try:
                self.query_one("#top-title", Static).update(f"▼ {self._activity_label}")
            except Exception:
                pass
            self.refresh_data(force=True)
            self.notify(f"Linked to session …{sid[-8:]}", timeout=2)

        self.push_screen(LinkSession(intro=intro), _done)

    def action_close_overlay(self) -> None:
        """Dismiss the command palette / help panel / any modal screen on Escape."""
        # Modal screen at the top of the stack
        try:
            if len(self.screen_stack) > 1:
                self.pop_screen()
                return
        except Exception:
            pass
        # Help panel (opened from palette → 'show keys and help')
        for selector in ("HelpPanel", "CommandPalette"):
            try:
                node = self.query_one(selector)
                node.remove()
                return
            except Exception:
                continue

    def _adjust_top(self, delta: int) -> None:
        base = self._top_height if self._top_height is not None else self.query_one("#top").size.height or 10
        self._top_height = max(3, min(self.screen.size.height - 6, base + delta))
        self.query_one("#top").styles.height = self._top_height
        self.persist_divider()

    def set_top_height(self, lines: int) -> None:
        self._top_height = max(3, min(self.screen.size.height - 6, lines))
        self.query_one("#top").styles.height = self._top_height

    def persist_divider(self) -> None:
        if self._top_height is None:
            return
        try:
            cfg = load_config()
            cfg["top_height"] = self._top_height
            save_config(cfg)
        except Exception:
            pass

    # ---- data refresh ---------------------------------------------------

    _TABLES = ("#edits-table", "#skills-table", "#agents-table",
               "#tools-table", "#unconfirmed-table")

    def _capture_state(self, sel: str) -> dict:
        try:
            t = self.query_one(sel, DataTable)
        except Exception:
            return {}
        row_key = None
        try:
            coord = t.cursor_coordinate
            if coord is not None and 0 <= coord.row < t.row_count:
                row_key = t.coordinate_to_cell_key(coord).row_key.value
        except Exception:
            row_key = None
        return {
            "row_key": row_key,
            "scroll_y": float(getattr(t, "scroll_y", 0.0)),
            "focused": t.has_focus,
        }

    def _restore_state(self, sel: str, state: dict) -> None:
        if not state:
            return
        try:
            t = self.query_one(sel, DataTable)
        except Exception:
            return
        rk = state.get("row_key")
        if rk:
            for i, key in enumerate(t.rows.keys()):
                kv = getattr(key, "value", key)
                if kv == rk:
                    try:
                        t.move_cursor(row=i, animate=False)
                    except Exception:
                        pass
                    break
        sy = state.get("scroll_y", 0.0)
        if sy:
            try:
                t.scroll_to(y=sy, animate=False)
            except Exception:
                pass
        if state.get("focused"):
            try:
                t.focus()
            except Exception:
                pass

    def _avail_width(self, table: DataTable, fixed: int, ncols: int) -> int:
        """Width to give the one variable-width column so the whole row fits
        the viewport with NO horizontal scroll.

        With cell_padding=1, every column is rendered `2` cols wider than its
        content (1 each side). So the row width is:
            sum(content widths) + 2 * ncols
        `fixed` is the summed *content* width of the other columns; `ncols` is
        the total number of columns (including the variable one). We solve for
        the variable column's content width.
        """
        tw = int(getattr(table.size, "width", 0) or 0)
        if tw <= 0:
            aw = int(getattr(self.size, "width", 0) or 0)
            # Inner table width = app width − round border (2) − tab/pane chrome
            # (2). Used only before a hidden tab has been laid out.
            tw = max(0, aw - 4)
        if tw <= 0:
            tw = 56
        padding = 2 * ncols
        # -3 safety: covers the column gutter the cursor reserves plus a margin,
        # so a row never tips the table into a horizontal scrollbar. Floor of 4
        # keeps it fitting even in a very narrow pane (names just truncate hard).
        return max(4, tw - fixed - padding - 3)

    def _fix_col_width(self, table: DataTable, key: str, width: int) -> None:
        """Pin a column to an exact width so DataTable can't auto-expand it
        past the viewport."""
        try:
            col = table.columns[key]
            col.auto_width = False
            col.width = width
        except Exception:
            pass

    def on_resize(self, event: events.Resize) -> None:
        if not self._cols_ready:
            return
        # Force a re-render: widths might have changed even if data didn't.
        self.refresh_data(force=True)

    def refresh_data(self, force: bool = False) -> None:
        # Re-link to the latest session_id if the launcher's state file changed
        # (driven by /resume in the paired Claude pane). May set self.session_id.
        prev_sid = self.session_id
        self._poll_link_state()
        if self.session_id != prev_sid:
            force = True  # session changed → bypass the "nothing changed" gate
        evts = self._eventlog.read()  # incremental: only parses new lines
        # Cheap signature so we don't redraw (and flash) when nothing changed.
        sig = (len(evts), evts[-1].get("ts") if evts else "", self.size.width)
        if not force and sig == getattr(self, "_last_sig", None):
            return
        self._last_sig = sig

        # Load the user's confirm/deny decisions fresh each tick (tiny file).
        self._confirmations = load_confirmations()

        states = {sel: self._capture_state(sel) for sel in self._TABLES}
        self._render_edits(evts)
        self._render_counts(evts, "skill", "#skills-table", "skills-tab", "Skills")
        self._render_counts(evts, "agent", "#agents-table", "agents-tab", "Agents")
        self._render_tools(evts)
        self._render_unconfirmed(evts)
        for sel, st in states.items():
            self._restore_state(sel, st)

    def _status_of(self, kind: str, name: str) -> str:
        """One of: verified (on disk) | native (Claude built-in) | confirmed |
        denied | unconfirmed. An on-disk file always wins, even over 'denied'."""
        if find_skill_or_agent_file(kind, name):
            return "verified"
        if is_native(kind, name):
            return "native"
        key = f"{kind}::{name}"
        conf = getattr(self, "_confirmations", {"confirmed": set(), "denied": set()})
        if key in conf["denied"]:
            return "denied"
        if key in conf["confirmed"]:
            return "confirmed"
        return "unconfirmed"

    def _render_tools(self, evts: list[dict]) -> None:
        if not self.session_id:
            tools = [e for e in evts if e.get("type") == "tool"]
        else:
            tools = [e for e in evts if e.get("type") == "tool" and e.get("session_id") == self.session_id]
        counts: dict[str, int] = {}
        for e in tools:
            name = e.get("target") or e.get("name") or ""
            if not name:
                continue
            counts[name] = counts.get(name, 0) + 1
        rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        table = self.query_one("#tools-table", DataTable)
        table.clear()
        name_w = self._avail_width(table, fixed=5, ncols=2)  # name + uses(5)
        self._fix_col_width(table, "name", name_w)
        for name, n in rows:
            table.add_row(truncate_left(name, name_w), str(n), key=f"tool::{name}")
        if not rows:
            table.add_row(
                Text(truncate_right("no tools used this session yet", name_w),
                     style="dim italic"),
                "", key=PLACEHOLDER_KEY)
        try:
            pane = self.query_one("#tools-tab", TabPane)
            pane.label = f"Tools ({len(counts)})"  # type: ignore[assignment]
        except Exception:
            pass

    @staticmethod
    def _norm_path(p: str) -> str:
        """Key used to dedup the same file across slash styles (and case on
        Windows). `C:\\a\\b.py` and `C:/a/b.py` collapse to one row."""
        n = p.replace("\\", "/")
        return n.lower() if IS_WIN else n

    def _render_edits(self, evts: list[dict]) -> None:
        edits = [e for e in evts if e.get("type") == "edit"]
        if self.session_id:
            edits = [e for e in edits if e.get("session_id") == self.session_id]
        agg: dict[str, dict] = {}
        for e in edits:
            target = e.get("target") or ""
            if not target:
                continue
            nk = self._norm_path(target)
            ts = parse_ts(e.get("ts", ""))
            rec = agg.setdefault(nk, {
                "ts": ts, "added": 0, "removed": 0, "action": "edited",
                "path": target,
            })
            # Latest event wins for the displayed action, time, and path form.
            if ts >= rec["ts"]:
                rec["ts"] = ts
                rec["action"] = e.get("action", "edited") or "edited"
                rec["path"] = target
            rec["added"] += int(e.get("added", 0) or 0)
            rec["removed"] += int(e.get("removed", 0) or 0)

        rows = sorted(agg.values(), key=lambda r: r["ts"], reverse=True)[:100]
        table = self.query_one("#edits-table", DataTable)
        table.clear()

        action_glyph = {
            "created": Text("cre", style="bold green"),
            "edited":  Text("edt", style="bold cyan"),
            "deleted": Text("del", style="bold red"),
        }

        # Fixed columns: act=3, rm=4, ad=4, when=4 → 15. 5 columns total.
        file_w = self._avail_width(table, fixed=15, ncols=5)
        self._fix_col_width(table, "file", file_w)

        if not rows:
            msg = ("No activity yet — keep using Claude"
                   if self.session_id else
                   "No activity yet. Press l to link a session.")
            table.add_row("", Text(truncate_right(msg, file_w), style="dim italic"),
                          "", "", "", key=PLACEHOLDER_KEY)
            return

        for rec in rows:
            path = rec["path"]
            act = action_glyph.get(rec["action"], Text(rec["action"][:3]))
            rm = Text(f"-{rec['removed']}", style="red") if rec["removed"] else Text("")
            ad = Text(f"+{rec['added']}", style="green") if rec["added"] else Text("")
            display_str = truncate_left(format_path(path, self._path_mode), file_w)
            # Red filename = file is NOT on disk *right now*. The action glyph
            # ("del") records the last logged action; a file deleted then
            # recreated keeps the glyph but is no longer red.
            display = (
                Text(display_str, style="red")
                if not _path_exists(path)
                else display_str
            )
            table.add_row(
                act,
                display,
                rm,
                ad,
                relative_time(rec["ts"]),
                key=path,
            )

    def _render_counts(
        self, evts: list[dict], kind: str, table_sel: str, tab_id: str, label: str
    ) -> None:
        counts = aggregate_counts(evts, kind)
        # We need sess_counts *before* sorting, but it's built after this
        # block in the original flow — recompute here cheaply.
        _sess_for_sort: dict[str, int] = {}
        if self.session_id:
            for e in evts:
                if e.get("type") == kind and e.get("session_id") == self.session_id:
                    n = e.get("target") or ""
                    if n:
                        _sess_for_sort[n] = _sess_for_sort.get(n, 0) + 1

        rows = sorted(
            counts.items(),
            key=lambda kv: (
                -_sess_for_sort.get(kv[0], 0),
                -kv[1]["7d"],
                -kv[1]["all"],
                kv[0],
            ),
        )[:300]
        table = self.query_one(table_sel, DataTable)
        table.clear()
        # Per-session counts: how often each item was used in *this* session
        if self.session_id:
            sess_counts: dict[str, int] = {}
            for e in evts:
                if e.get("type") == kind and e.get("session_id") == self.session_id:
                    n = e.get("target") or ""
                    if n:
                        sess_counts[n] = sess_counts.get(n, 0) + 1
        else:
            sess_counts = {}

        # First row: "view all" pseudo-row
        table.add_row("▶ View all installed…", "", "", "", key=VIEW_ALL_KEY)
        name_w = self._avail_width(table, fixed=15, ncols=4)  # name + session(7)+week(4)+all(4)
        self._fix_col_width(table, "name", name_w)
        shown = 0
        for name, rec in rows:
            s = sess_counts.get(name, 0)
            # Sidebar tabs only list items actually used THIS session. The full
            # catalogue lives in the inventory view (▶ View all installed).
            if s == 0:
                continue
            # Verified (on disk), Claude-native, or user-confirmed items appear
            # here; unconfirmed/denied are quarantined in the Unconfirmed tab.
            status = self._status_of(kind, name)
            if status not in ("verified", "confirmed", "native"):
                continue
            shown += 1
            disp = f"{name} (native)" if status == "native" else name
            # Counts centred under their (wider) headers so they look balanced.
            table.add_row(
                truncate_left(disp, name_w),
                str(s).center(7),
                str(rec["7d"]).center(4) if rec["7d"] else "",
                str(rec["all"]).center(4) if rec["all"] else "",
                key=f"{kind}::{name}",
            )

        if shown == 0:
            none = f"no {label.lower()} used this session yet"
            table.add_row(Text(truncate_right(none, name_w), style="dim italic"),
                          "", "", "", key=PLACEHOLDER_KEY)

        try:
            pane = self.query_one(f"#{tab_id}", TabPane)
            pane.label = f"{label} ({shown})"  # type: ignore[assignment]
        except Exception:
            pass

    def _render_unconfirmed(self, evts: list[dict]) -> None:
        """Review queue: skill/agent names *this session* used that aren't backed
        by a file and aren't a known built-in, skipping ones already confirmed or
        denied. Scoped to the current session like the other tabs (in global mode
        it shows all). The confirm/deny decision it writes is still global."""
        seen: dict[tuple[str, str], int] = {}
        for e in evts:
            kind = e.get("type", "")
            if kind not in ("skill", "agent"):
                continue
            if self.session_id and e.get("session_id") != self.session_id:
                continue
            name = e.get("target") or ""
            if not name:
                continue
            if self._status_of(kind, name) not in ("unconfirmed",):
                continue
            seen[(kind, name)] = seen.get((kind, name), 0) + 1

        # Most-seen first — those are most likely real and worth confirming.
        rows = sorted(seen.items(), key=lambda kv: (-kv[1], kv[0][1]))
        self._unconfirmed_keys = {f"{k}::{n}" for (k, n), _ in rows}

        table = self.query_one("#unconfirmed-table", DataTable)
        table.clear()
        # fixed: kind=5, seen=4, ok=1, no=1, rename=1 → 12; 6 columns.
        name_w = self._avail_width(table, fixed=12, ncols=6)
        self._fix_col_width(table, "name", name_w)

        if rows:
            table.add_row(Text("✓ Confirm all", style="green"), "", "", "", "", "",
                          key=CONFIRM_ALL_KEY)
            table.add_row(Text("✗ Deny all", style="red"), "", "", "", "", "",
                          key=DENY_ALL_KEY)
        for (kind, name), n in rows:
            table.add_row(
                truncate_left(name, name_w),
                kind,                           # "skill" / "agent" (col width 5)
                str(n),
                Text("✓", style="bold green"),
                Text("✗", style="bold red"),
                Text("↻", style="bold yellow"),
                key=f"{kind}::{name}",
            )

        # Update the tab header to show the count. In Textual 8.x the label
        # lives on the Tab widget (the header), not on TabPane — get it via
        # TabbedContent.get_tab(pane_id). The Text wrapper is important;
        # passing a bare string can re-render as plain text and lose styling.
        try:
            tabbed = self.query_one(TabbedContent)
            tab = tabbed.get_tab("unconfirmed-tab")
            tab.label = Text(f"Unconfirmed ({len(rows)})") if rows else Text("Unconfirmed")
        except Exception:
            pass

    # ---- click handling -------------------------------------------------

    def _path_for_row(self, table_id: str, row_key: str) -> str | None:
        """Resolve a row to an on-disk path (or None if there isn't one)."""
        if row_key == VIEW_ALL_KEY:
            return None
        if table_id == "edits-table":
            return row_key
        if "::" not in row_key:
            return None
        kind, name = row_key.split("::", 1)
        md = find_skill_or_agent_file(kind, name)
        return str(md) if md else None

    def _handle_row(self, table_id: str, row_key: str, right_click: bool) -> None:
        if row_key == VIEW_ALL_KEY:
            tab = "agents" if table_id == "agents-table" else "skills"
            self._confirm_inventory(tab)
            return
        # Claude-native skill/agent → offer the docs link instead of a file menu.
        if "::" in row_key:
            kind, name = row_key.split("::", 1)
            if not find_skill_or_agent_file(kind, name) and is_native(kind, name):
                self.push_screen(NativeInfo(kind, name, native_doc_url(kind, name)))
                return
        path = self._path_for_row(table_id, row_key)
        if path is None:
            return
        if right_click:
            self.push_screen(FileMenu(path))
        else:
            open_with_default(path)

    def on_click(self, event: events.Click) -> None:
        """Left-click on a row only moves the cursor (DataTable's default).
        Right-clicks arrive via SentryTable.RightClicked, not here, because
        DataTable swallows the Click. We only need the section-title toggle."""
        node = event.widget
        if node is not None and getattr(node, "id", None) == "top-title":
            self._toggle("#top", "#top-title", self._activity_label)

    def on_sentry_table_right_clicked(self, message: SentryTable.RightClicked) -> None:
        self._handle_row(message.table.id or "", message.row_key, right_click=True)

    def on_sentry_table_left_clicked(self, message: SentryTable.LeftClicked) -> None:
        rk = message.row_key
        # Unconfirmed tab: ✓ column confirms, ✗ column denies; bulk rows act on all.
        if message.table.id == "unconfirmed-table":
            self._handle_unconfirmed_click(rk, message.column)
            return
        if rk == VIEW_ALL_KEY:
            tab = "agents" if message.table.id == "agents-table" else "skills"
            self._confirm_inventory(tab)
            return
        # Left-click a native skill/agent → open the docs-link menu (it has no
        # file to open, so the docs link is its only action).
        if message.table.id in ("skills-table", "agents-table") and "::" in rk:
            kind, name = rk.split("::", 1)
            if not find_skill_or_agent_file(kind, name) and is_native(kind, name):
                self.push_screen(NativeInfo(kind, name, native_doc_url(kind, name)))

    def _handle_unconfirmed_click(self, row_key: str, column: int) -> None:
        keys = getattr(self, "_unconfirmed_keys", set())
        if row_key == CONFIRM_ALL_KEY:
            if keys:
                confirm_keys(set(keys))
                self.notify(f"Confirmed {len(keys)}", timeout=2)
                self.refresh_data(force=True)
            return
        if row_key == DENY_ALL_KEY:
            if keys:
                deny_keys(set(keys))
                self.notify(f"Denied {len(keys)}", timeout=2)
                self.refresh_data(force=True)
            return
        if "::" not in row_key:
            return
        # Columns: 0 name, 1 kind, 2 seen, 3 ✓, 4 ✗, 5 ↻.
        if column == 3:
            confirm_keys({row_key})
            self.refresh_data(force=True)
        elif column == 4:
            deny_keys({row_key})
            self.refresh_data(force=True)
        elif column == 5:
            self._open_rename_modal(row_key)

    def _open_rename_modal(self, row_key: str) -> None:
        if "::" not in row_key:
            return
        kind, old_name = row_key.split("::", 1)

        def _on_rename(new_name: str) -> None:
            if not new_name:
                return
            rename_key(row_key, new_name)
            self.notify(f"↻ {old_name} → {new_name}", timeout=2)
            self.refresh_data(force=True)

        self.push_screen(RenameModal(kind, old_name), _on_rename)

    def _focused_row(self) -> tuple[str, str] | None:
        for sel in ("#edits-table", "#skills-table", "#agents-table", "#tools-table"):
            try:
                t = self.query_one(sel, DataTable)
            except Exception:
                continue
            if not t.has_focus:
                continue
            coord = t.cursor_coordinate
            if coord is None or coord.row < 0:
                return None
            try:
                row_key = t.coordinate_to_cell_key(coord).row_key.value
            except Exception:
                return None
            if not row_key:
                return None
            return (t.id or "", row_key)
        return None

    def action_open_selected(self) -> None:
        hit = self._focused_row()
        if hit:
            self._handle_row(hit[0], hit[1], right_click=False)

    def action_reveal_selected(self) -> None:
        hit = self._focused_row()
        if not hit:
            return
        path = self._path_for_row(*hit)
        if path is None:
            return
        # If file is gone, reveal the parent dir instead of explorer /select-ing
        # a missing path (which errors out on Windows).
        if not os.path.exists(path):
            try:
                open_with_default(str(Path(path).parent))
            except Exception:
                pass
        else:
            reveal_in_explorer(path)

    def action_copy_selected(self) -> None:
        hit = self._focused_row()
        if not hit:
            return
        path = self._path_for_row(*hit)
        if path and copy_to_clipboard(path):
            self.notify("Path copied", timeout=1.5)

    def action_row_menu(self) -> None:
        hit = self._focused_row()
        if not hit:
            return
        path = self._path_for_row(*hit)
        if path:
            self.push_screen(FileMenu(path))


# ---------------------------------------------------------------------------
# Inventory app — full-window list of every installed skill / agent

class InventoryApp(App):
    CSS = """
    Screen { background: $surface; }
    FileMenu { background: $surface 0%; }
    DataTable { height: 1fr; background: $surface; }
    /* Only the hovered column's header cell highlights (per-column, crisp). */
    DataTable > .datatable--header-hover { background: $accent; color: $text; }
    TabbedContent { height: 1fr; }
    Footer { background: $surface-darken-2; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
    ]

    TITLE = "claude-sentry · inventory"

    def __init__(self, initial_tab: str = "skills") -> None:
        super().__init__()
        self._initial_tab = "agents-tab" if initial_tab == "agents" else "skills-tab"
        saved_theme = load_config().get("theme")
        if saved_theme:
            self.theme = saved_theme
        # Per-tab sort state: (column_key, direction). +1 = asc, -1 = desc.
        # Default: name A→Z, with the ▲ shown on the name column.
        self._sort: dict[str, tuple[str, int]] = {
            "skills-table": ("name", 1),
            "agents-table": ("name", 1),
        }

    def watch_theme(self, theme: str) -> None:
        cfg = load_config()
        cfg["theme"] = theme
        save_config(cfg)

    def compose(self) -> ComposeResult:
        with TabbedContent(initial=self._initial_tab):
            with TabPane("Skills", id="skills-tab"):
                yield SentryTable(id="skills-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Agents", id="agents-tab"):
                yield SentryTable(id="agents-table", cursor_type="row", zebra_stripes=True)
        yield WrappingFooter()

    def on_mount(self) -> None:
        for sel, name_label in (("#skills-table", "skill"), ("#agents-table", "agent")):
            t = self.query_one(sel, DataTable)
            t.add_column(name_label, key="name")
            t.add_column("plugin", key="plugin", width=24)
            # width 6 so the header word + sort arrow ("week ▼") isn't clipped.
            t.add_column("week", key="7d", width=6)
            t.add_column("all", key="all", width=5)
        self.refresh_data()
        self.set_interval(5.0, self.refresh_data)

    def action_refresh(self) -> None:
        self.refresh_data()

    def _sort_rows(self, rows: list[dict], table_id: str) -> list[dict]:
        col, direction = self._sort.get(table_id, ("name", 1))
        def keyfn(r: dict):
            v = r.get(col, "")
            if col in ("7d", "all"):
                try:
                    return int(v)
                except Exception:
                    return 0
            return str(v).lower()
        return sorted(rows, key=keyfn, reverse=(direction == -1))

    def _update_header_arrows(self, table_id: str, name_label: str) -> None:
        sort_col, sort_dir = self._sort.get(table_id, ("name", 1))
        t = self.query_one(f"#{table_id}", DataTable)
        for ck, label in (("name", name_label), ("plugin", "plugin"),
                          ("7d", "week"), ("all", "all")):
            arrow = (" ▼" if sort_dir == -1 else " ▲") if ck == sort_col else ""
            try:
                t.columns[ck].label = Text(f"{label}{arrow}")
            except Exception:
                pass

    def refresh_data(self) -> None:
        evts = load_events()
        skill_counts = aggregate_counts(evts, "skill")
        agent_counts = aggregate_counts(evts, "agent")

        def lookup(name: str, counts: dict[str, dict]) -> tuple[int, int]:
            if name in counts:
                rec = counts[name]
                return int(rec["7d"]), int(rec["all"])
            for k, rec in counts.items():
                if k.endswith(":" + name) or k == name:
                    return int(rec["7d"]), int(rec["all"])
            return 0, 0

        for table_id, name_label, items, counts, kind in (
            ("skills-table", "skill",  discover_skills(),  skill_counts,  "skill"),
            ("agents-table", "agent",  discover_agents(),  agent_counts,  "agent"),
        ):
            rows = []
            for it in items:
                d7, da = lookup(it["name"], counts)
                rows.append({
                    "name": it["name"],
                    "plugin": it.get("plugin", "") or "",
                    "7d": d7,
                    "all": da,
                    "path": it["path"],
                })
            rows = self._sort_rows(rows, table_id)
            t = self.query_one(f"#{table_id}", DataTable)
            t.clear()
            for r in rows:
                t.add_row(
                    r["name"],
                    r["plugin"] or "—",
                    str(r["7d"]) if r["7d"] else "",
                    str(r["all"]) if r["all"] else "",
                    key=f"{kind}::{r['path']}",
                )
            self._update_header_arrows(table_id, name_label)

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        tid = event.data_table.id
        if tid not in self._sort:
            return
        col_key = event.column_key.value
        cur_col, cur_dir = self._sort[tid]
        if col_key == cur_col:
            self._sort[tid] = (col_key, -cur_dir)
        else:
            # Counts default to descending, text columns to ascending.
            self._sort[tid] = (col_key, -1 if col_key in ("7d", "all") else 1)
        self.refresh_data()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Left-click (re-click of highlighted row) → open the source file.
        if event.row_key is None or event.row_key.value is None:
            return
        _, _, path = event.row_key.value.partition("::")
        if path:
            open_with_default(path)

    def on_sentry_table_right_clicked(self, message: SentryTable.RightClicked) -> None:
        _, _, path = message.row_key.partition("::")
        if path:
            self.push_screen(FileMenu(path))


# ---------------------------------------------------------------------------
# Entry point

def _session_for_this_window() -> str:
    """When --session isn't given, fall back to the Claude session the
    SessionStart hook recorded for this Windows Terminal window. Lets a manual
    `claude-sentry` restart re-attach to the same conversation."""
    wt = os.environ.get("WT_SESSION")
    if not wt:
        return ""
    f = SENTRY_DIR / "win-session" / f"{wt}.txt"
    try:
        return f.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def main() -> None:
    parser = argparse.ArgumentParser(prog="claude-sentry")
    parser.add_argument("--session", default="", help="Claude session_id to filter edits by")
    parser.add_argument("--all-sessions", action="store_true",
                        help="Show activity across all sessions (don't auto-attach)")
    parser.add_argument("--inventory", action="store_true", help="Run inventory mode")
    parser.add_argument("--tab", default="skills", choices=("skills", "agents"))
    args = parser.parse_args()

    if args.inventory:
        InventoryApp(initial_tab=args.tab).run()
        return

    session = args.session
    if not session and not args.all_sessions:
        session = _session_for_this_window()
    SentryApp(session_id=session).run()


if __name__ == "__main__":
    main()
