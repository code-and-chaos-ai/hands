"""Who the agent is, on screen and on the wire: one place. state/config.json "identity" overrides any of these;
missing keys keep the defaults.

  name        shown on the pill ("<name> is working here") and in the tab group
  initial     the letter in the cursor badge (one character)
  color       the main colour of the glow, border, cursor fill (#RRGGBB)
  color_hot   the brighter accent: cursor edge glow, click ripple (#RRGGBB)
  session     the default tab-group session name in Chrome
  chrome_host the native messaging host name (must match HOST in chrome/extension/sw.js)
  chrome_pipe the local named pipe between the host and the engine (letters, digits, - and _)
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "state", "config.json")

DEFAULTS = {
    "name": "Rowan",
    "initial": "R",
    "color": "#E4007C",
    "color_hot": "#FF4DA6",
    "session": "rowan",
    "chrome_host": "com.kincaid.hands",
    "chrome_pipe": "rowan-hands-chrome",
}
_CHECK = {
    "name": r"[^\x00-\x1f]{1,32}",
    "initial": r"\S",
    "color": r"#[0-9A-Fa-f]{6}",
    "color_hot": r"#[0-9A-Fa-f]{6}",
    "session": r"[^\x00-\x1f]{1,24}",
    "chrome_host": r"[a-z0-9_]+(\.[a-z0-9_]+)*",   # Chrome's own rule for native host names
    "chrome_pipe": r"[A-Za-z0-9_-]{1,64}",
}


def get() -> dict:
    """The identity: defaults, with each valid override from state/config.json on top (an invalid one is ignored)."""
    out = dict(DEFAULTS)
    try:
        over = json.load(open(CONFIG, encoding="utf-8-sig")).get("identity") or {}
    except Exception:  # unreadable, wrong shape, or nested deep enough to overflow: the defaults stand
        over = {}
    if isinstance(over, dict):
        for k, v in over.items():
            if k in _CHECK and isinstance(v, str) and re.fullmatch(_CHECK[k], v):
                out[k] = v
    return out


def setting(key: str, default):
    """A top-level setting from state/config.json, only if it has the default's type (else the default)."""
    try:
        v = json.load(open(CONFIG, encoding="utf-8-sig")).get(key, default)
    except Exception:
        return default
    return v if type(v) is type(default) else default


def label() -> str:
    return f"{get()['name']} is working here"
