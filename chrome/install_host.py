"""Register (or --uninstall) the native messaging host com.kincaid.hands for the current user.

Writes chrome\\com.kincaid.hands.json (path to host.cmd, allowed_origins = exactly our extension id) and
points HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts\\com.kincaid.hands at it (Chrome only).
Touches no other registry key. Exit 0 on success.
"""
from __future__ import annotations

import json
import sys
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.chrome_key import EXTENSION_ID, HOST_NAME  # noqa: E402

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / f"{HOST_NAME}.json"
KEYS = (rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}",)  # Chrome only


def registered() -> dict[str, str | None]:
    out = {}
    for k in KEYS:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, k) as h:
                out[k] = winreg.QueryValueEx(h, "")[0]
        except FileNotFoundError:
            out[k] = None
    return out


def _paths(name: str) -> tuple[Path, str]:
    return HERE / f"{name}.json", rf"Software\Google\Chrome\NativeMessagingHosts\{name}"


def install(name: str = HOST_NAME) -> Path:
    """Register a host under `name` (default: the real one). Tests pass their own name, so the person's real
    registration is never touched."""
    manifest, key = _paths(name)
    manifest.write_text(json.dumps({
        "name": name,
        "description": "Hands: Chrome <-> local named pipe",
        "path": str(HERE / "host.cmd"),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"],
    }, indent=2), encoding="utf-8")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as h:
        winreg.SetValueEx(h, "", 0, winreg.REG_SZ, str(manifest))
    return manifest


def uninstall(name: str = HOST_NAME) -> None:
    manifest, key = _paths(name)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
    except FileNotFoundError:
        pass
    if name != HOST_NAME:
        manifest.unlink(missing_ok=True)  # a test's own manifest goes with its key; the real one stays


if __name__ == "__main__":
    if "--uninstall" in sys.argv:
        uninstall()
        print("com.kincaid.hands unregistered")
    elif "--status" in sys.argv:
        print(json.dumps(registered(), indent=1))
    else:
        install()
        print(f"com.kincaid.hands registered -> {MANIFEST}")
