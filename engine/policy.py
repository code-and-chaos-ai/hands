"""What the agent may touch: the hard deny list and the person's consent (once / always / no)."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

STATE = Path(__file__).resolve().parent.parent / "state"
CONSENT_FILE = STATE / "consent.json"

# exe basenames (lowercase) I never drive or launch
DENY_EXES = {
    # terminals, shells, remote shells
    "windowsterminal.exe", "openconsole.exe", "conhost.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
    "powershell_ise.exe", "mintty.exe", "bash.exe", "wsl.exe", "wt.exe", "alacritty.exe", "wezterm-gui.exe",
    "git-bash.exe", "ssh.exe", "putty.exe", "nu.exe", "kitty.exe", "hyper.exe", "tabby.exe",
    # auth, elevation, lock, credentials
    "consent.exe", "credentialuibroker.exe", "lockapp.exe", "logonui.exe", "useraccountcontrolsettings.exe",
    # security, process control
    "securityhealthui.exe", "securityhealthhost.exe", "msmpeng.exe", "mpcmdrun.exe", "taskmgr.exe",
    # password managers
    "1password.exe", "bitwarden.exe", "keepass.exe", "keepassxc.exe", "dashlane.exe", "lastpass.exe",
    "proton pass.exe", "nordpass.exe", "enpass.exe",
    # my own permission prompts live here: never self-approve
    "claude.exe",
    # the other agent's harness
    "codex.exe", "chatgpt.exe",
    # registry / policy consoles
    "regedit.exe", "mmc.exe",
}
# packaged-app ids, matched case-insensitively as prefixes
DENY_AUMID_PREFIX = tuple(p.casefold() for p in (
    "Microsoft.SecHealthUI", "Microsoft.Windows.SecHealthUI", "Microsoft.WindowsTerminal", "Microsoft.PowerShell",
    "Microsoft.CredDialogHost", "Microsoft.LockApp", "Microsoft.Windows.Shell.RunDialog", "Claude_", "Claude!",
    "OpenAI.Codex", "OpenAI.ChatGPT", "Microsoft.WindowsTerminalPreview", "Bitwarden", "AgileBits",
))
# launching these would run code, not open an app
SCRIPT_EXT = {".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".lnk", ".msi",
              ".msc", ".reg", ".scr", ".hta", ".com", ".pif", ".cpl", ".url", ".appref-ms"}


class Denied(Exception):
    pass


def denied_aumid(aumid: str) -> bool:
    a = (aumid or "").casefold()
    return any(a.startswith(p) for p in DENY_AUMID_PREFIX)


def check_launch_target(path: str) -> None:
    """Refuse before anything starts: the resolved target, never the words I was given."""
    low = (path or "").casefold()
    if "!" in path and denied_aumid(path):
        raise Denied(f"I never launch {path}: hard deny list.")
    # file checks run for every target, app ids included: a folder named "x!y" must not hide a script
    ext = os.path.splitext(low)[1]
    if ext in SCRIPT_EXT:
        raise Denied(f"I never launch {ext} files: that runs code instead of opening an app.")
    if os.path.basename(low) in DENY_EXES:
        raise Denied(f"I never launch {os.path.basename(low)}: hard deny list.")


def is_run_dialog(hwnd: int, exe: str, cls: str) -> bool:
    """The Run box in any language: an explorer-owned #32770 holding a combo box and the
    'Browse…' button row (title is localized, so the shape decides)."""
    if cls != "#32770" or os.path.basename(exe or "").lower() != "explorer.exe":
        return False
    try:
        import win32gui
        kids: list[str] = []
        win32gui.EnumChildWindows(hwnd, lambda h, _: kids.append(win32gui.GetClassName(h)) or True, None)
        return "ComboBox" in kids and kids.count("Button") >= 3 and "Static" in kids
    except Exception:
        return True  # unsure about an explorer dialog: refuse


def check_denied(exe: str, app: str, title: str, cls: str, hwnd: int = 0) -> None:
    base = os.path.basename(exe or "").lower()
    if base in DENY_EXES:
        raise Denied(f"I never drive {base}: it is on the hard deny list (terminals, auth, security, "
                     f"password managers, my own permission prompts).")
    if denied_aumid(app):
        raise Denied(f"I never drive {app}: hard deny list.")
    if hwnd and is_run_dialog(hwnd, exe, cls):
        raise Denied("I never use the Run dialog.")


def consent_key(exe: str, app: str) -> str:
    return app if "!" in app else os.path.basename(exe or app).lower()


class Consent:
    def __init__(self):
        self.session: set[str] = set()
        self.refused: set[str] = set()
        self.lock = threading.Lock()
        STATE.mkdir(exist_ok=True)
        try:
            data = json.loads(CONSENT_FILE.read_text(encoding="utf-8"))
            always = data.get("always", []) if isinstance(data, dict) else []
            # only a list holds grants, and only its non-empty strings are grants: a dict's keys or a string's letters
            # must never become one, and one bad entry must not cost the person their real ones
            self.always = {k for k in always if isinstance(k, str) and k} if isinstance(always, list) else set()
        except Exception:
            self.always = set()

    def allowed(self, key: str) -> bool:
        return key in self.always or key in self.session

    def grant(self, key: str, scope: str) -> None:
        with self.lock:
            if scope == "always":
                self.always.add(key)
                from .bouncer import _atomic_write, log  # a fresh temp name: a planted link or folder can't block it
                try:
                    _atomic_write(str(CONSENT_FILE), json.dumps({"always": sorted(self.always)}, ensure_ascii=False, indent=1))
                except Exception as e:  # unwritable: the grant still holds this session; it just isn't remembered
                    log(f"could not save an 'Always' consent ({type(e).__name__}); it holds for this session only")
            elif scope == "once":
                self.session.add(key)
            self.refused.discard(key)

    def refuse(self, key: str) -> None:
        with self.lock:
            self.refused.add(key)


# ---------------------------------------------------------------- the browser (slice 2)
# schemes I never look at or act in: the browser's own pages, extensions (the agent's and the person's), devtools
DENY_SCHEMES = ("chrome:", "chrome-extension:", "chrome-untrusted:", "chrome-search:", "devtools:", "edge:",
                "about:", "view-source:", "javascript:", "data:", "blob:", "filesystem:")
ABOUT_OK = ("about:blank",)
# password managers' web vaults (host or any subdomain)
PASSWORD_HOSTS = ("passwords.google.com", "passwords.google", "1password.com", "1password.ca", "1password.eu",
                  "bitwarden.com", "bitwarden.eu", "lastpass.com", "dashlane.com", "keepersecurity.com",
                  "keepersecurity.eu", "nordpass.com", "pass.proton.me", "roboform.com", "enpass.io",
                  "keeper.io", "app.keeweb.info", "vault.zoho.com", "vault.zoho.eu", "vault.zoho.in",
                  "vault.zoho.com.au", "vault.zoho.jp")
# Password managers are denied by HOST only. A path rule can't be a wall: a page on the same origin can
# reach any of its own paths (percent-encoding, //, framing and copying the DOM), while a page can't fake
# a host. Every vault on this list has its own host.


def _norm_host(h: str) -> str:
    """The host as Chrome will see it: NFKC-normalised (full-width letters and digits become plain), the ideographic
    and full-width dots read as dots, lowercase, every trailing dot gone, then IDNA. If IDNA refuses the name (a
    label too long, say) the normalised form is kept, so a deny-list suffix still matches it."""
    import unicodedata
    h = unicodedata.normalize("NFKC", (h or "").strip())
    for dot in ("。", "．", "｡"):
        h = h.replace(dot, ".")
    h = h.lower().rstrip(".")
    try:
        h = h.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        pass
    return h.lower()


def url_host(url: str) -> str:
    from urllib.parse import urlsplit
    try:
        return _norm_host(urlsplit(url).hostname or "")
    except ValueError:
        return ""


def _host_is(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def check_url(url: str, *, opening: bool = False) -> None:
    """Raise Denied for pages I never touch. opening=True also refuses about:blank as a destination."""
    u = (url or "").strip()
    low = u.lower()
    if not low:
        raise Denied("No URL.")
    if any(low.startswith(s) for s in DENY_SCHEMES):
        if not opening and low.split("#")[0] in ABOUT_OK:
            return
        raise Denied(f"I never drive browser-internal or script pages ({low.split(':')[0]}:).")
    if opening and not (low.startswith("http://") or low.startswith("https://")):
        raise Denied("I only open http(s) pages.")
    # close parser differentials with Chrome: it strips tabs/newlines, treats \ as /, honours userinfo and
    # percent-decodes hosts; anything that could read one way here and another way there is refused outright
    from urllib.parse import urlsplit
    if any(c in u for c in "\\\t\r\n ") or any(ord(c) < 0x20 or ord(c) == 0x7F for c in u):
        raise Denied("That URL has characters browsers read differently; give me a clean URL.")
    try:
        parts = urlsplit(u)
    except ValueError:
        raise Denied("That URL doesn't parse.")
    if low.startswith(("http://", "https://")):
        if "@" in parts.netloc or "%" in parts.netloc:
            raise Denied("That URL hides its real host (userinfo or escapes in the host); give me a clean URL.")
        if not parts.hostname:
            raise Denied("That URL has no host.")
    host = url_host(u)
    for p in PASSWORD_HOSTS:
        if _host_is(host, p):
            raise Denied(f"I never drive a password manager ({p}).")


def nav_policy() -> dict:
    """The lists the extension's navigation gate applies to every top-level load of my tabs."""
    return {"hosts": [h for h in PASSWORD_HOSTS]}


def site_key(url: str) -> str:
    return "site:" + url_host(url)
