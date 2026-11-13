"""The one narrow door into the Claude desktop app. The app stays on the hard deny list for everything else.

Exactly two controls, found fresh each time and required to be unique, never by a generated id:
  remote_control_on  the "Remote Control" switch of the session in view: turned ON only, never off
  new_session        the "New" button: a new session

Every use (dry runs aside) tells the owner through the notify hook (state/config.json "notify_cmd": an argv list,
the message appended as its last argument). With no notify hook configured the door stays shut: the owner hears
about every use or none happens."""
import json
import os
import subprocess
import time

import uiautomation as auto

from . import win
from .common import HandsError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "state", "config.json")
ACTIONS = ("remote_control_on", "new_session")


def notify_cmd() -> list[str] | None:
    from .bouncer import exe_cmd  # the same rule as the nudge: only an existing .exe, never a .bat / .cmd
    try:
        cmd = json.load(open(CONFIG, encoding="utf-8-sig")).get("notify_cmd")
    except Exception:
        return None
    return exe_cmd(cmd)


def _notify(cmd: list[str], msg: str) -> bool:
    try:
        # stdin from nothing: inside the MCP server stdin IS the protocol pipe, and a child must never inherit it
        return subprocess.run([*cmd, msg], stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                              creationflags=0x08000000).returncode == 0
    except Exception:
        return False


def _app_window() -> int:
    ws = [w for w in win.top_windows() if os.path.basename(w.exe).lower() == "claude.exe"]
    if not ws:
        raise HandsError("The Claude app isn't open (no claude.exe window).")
    return ws[0].id


def _find(root, want: str) -> list:
    """Buttons named exactly `want` (the Remote Control one must also be a switch)."""
    out = []

    def walk(c, d=0):
        for ch in c.GetChildren():
            if ch.ControlTypeName == "ButtonControl" and (ch.Name or "").strip() == want:
                if want != "Remote Control" or _toggle_state(ch) is not None:
                    out.append(ch)
            if d < 45:
                walk(ch, d + 1)
    walk(root)
    return out


def _toggle_state(c):
    try:
        return c.GetTogglePattern().ToggleState  # 1 = on
    except Exception:
        return None


def run(action: str, dry_run: bool = False) -> str:
    if action not in ACTIONS:
        raise HandsError(f"The Claude-app door does only {ACTIONS}; nothing else in that app, ever.")
    cmd = notify_cmd()
    if not cmd and not dry_run:
        raise HandsError("The Claude-app door is shut: no notify hook in state/config.json, and every use must reach the owner.")
    root = auto.ControlFromHandle(_app_window())
    want = "Remote Control" if action == "remote_control_on" else "New"
    hits = _find(root, want)
    if len(hits) != 1:
        raise HandsError(f"Found {len(hits)} '{want}' controls in the Claude app (need exactly one); touched nothing.")
    c = hits[0]
    what = "turn Remote Control ON" if action == "remote_control_on" else "press New (a new session)"
    if action == "remote_control_on":
        before = _toggle_state(c)
        if before == 1:
            return "Remote Control is already ON for the session in view; touched nothing."
        if dry_run:
            return "dry run: found the Remote Control switch (OFF); would turn it ON."
    elif dry_run:
        return "dry run: found the New button; would start a new session."
    # the owner hears BEFORE anything is pressed: if the notice can't reach them, nothing happens
    if not _notify(cmd, f"🐺 Hands, Claude-app door: about to {what}."):
        raise HandsError("The Claude-app door stayed shut: the notice to the owner failed, so nothing was pressed.")
    if action == "remote_control_on":
        try:
            c.GetTogglePattern().Toggle(waitTime=0)
        except Exception:
            c.GetInvokePattern().Invoke(waitTime=0)
        for _ in range(20):
            time.sleep(0.15)
            if _toggle_state(c) == 1:
                break
        on = _toggle_state(c) == 1
        result = "Remote Control turned ON" if on else "tried to turn Remote Control ON, but it still reads OFF"
    else:
        c.GetInvokePattern().Invoke(waitTime=0)
        time.sleep(0.6)
        result = "pressed New: a new session in the Claude app"
    told = _notify(cmd, f"🐺 Hands, Claude-app door: {result}.")
    return result + ("; the owner was told" if told else "; ⚠ the notice to the owner FAILED: tell them yourself")
