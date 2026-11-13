"""Shared by the native host and engine/browser.py: the extension id, the pipe name, the authkey file.
The key's bytes never leave these two processes: never printed, never logged."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

STATE = Path(__file__).resolve().parent.parent / "state"
KEY_FILE = STATE / "chrome.key"
EXTENSION_ID = "okjcipplkighkejacjpajiflnkkdmfcf"  # pinned by the manifest's "key"
from .identity import get as _identity  # noqa: E402
HOST_NAME = _identity()["chrome_host"]
PIPE_NAME = _identity()["chrome_pipe"]


def pipe_address() -> str:
    name = os.environ.get("HANDS_CHROME_PIPE") or PIPE_NAME
    if not name.replace("-", "").replace("_", "").isalnum():
        raise ValueError("HANDS_CHROME_PIPE: letters, digits, - and _ only")
    return "\\\\.\\pipe\\" + name


# What protects the key: the file's Windows ACL, inherited from the repo folder (SYSTEM, Administrators, the user).
# The 0o600 passed to os.open below is a POSIX mode Windows ignores; it is not the protection.
def load_or_create_key() -> bytes:
    STATE.mkdir(exist_ok=True)
    try:
        fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    except FileExistsError:
        data = KEY_FILE.read_bytes()
        if len(data) < 32:
            raise RuntimeError("state\\chrome.key is damaged; delete it and restart Chrome")
        return data
    with os.fdopen(fd, "wb") as f:
        f.write(secrets.token_bytes(32))
    return KEY_FILE.read_bytes()
