"""Hands in Chrome: the client of the native host's pipe, and my looks and acts in browser tabs.

The road: engine (here) -> \\\\.\\pipe\\rowan-hands-chrome (authkey, JSON bytes) -> chrome\\host.py -> the
extension's service worker -> chrome.debugger -> ONE background tab I opened. Policy lives here (deny list,
consent per site AND per frame); the service worker re-verifies every frame against the exact URL I approved,
at the moment it reads or acts.
"""
from __future__ import annotations

import base64
import itertools
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from multiprocessing import connection as mpc

from . import policy
from .chrome_key import load_or_create_key, pipe_address
from .common import (TEXT_CAP, HandsError, NeedsConsent, Stopped, check_action, check_then,  # noqa: F401
                     close_with_look, gate_consent)

CALL_TIMEOUT = 10.0          # every pipe call is bounded
MAX_REPLY = 16 * 1024 * 1024
TREE_CAP = 12000
SETTLE_S = 2.5               # the closing look waits at most this long for a loading page


class BrowserError(HandsError):
    pass


def _ctime(pid: int) -> int | None:
    """A process's creation time (100 ns ticks), or None if there is no such process."""
    import ctypes
    from ctypes import wintypes as wt
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wt.HANDLE
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        c, e, k, u = (wt.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
            return None
        code = wt.DWORD()
        if k32.GetExitCodeProcess(h, ctypes.byref(code)) and code.value != 259:  # STILL_ACTIVE
            return None
        return (c.dwHighDateTime << 32) | c.dwLowDateTime
    finally:
        k32.CloseHandle(h)


def engine_alive(token: str) -> bool:
    """An engine token is "<pid>:<creation time>:<random>": alive only if that very process still runs."""
    try:
        pid, ct, _ = token.split(":", 2)
        return _ctime(int(pid)) == int(ct)
    except (ValueError, TypeError):
        return False


ENGINE = f"{os.getpid()}:{_ctime(os.getpid())}:{secrets.token_hex(4)}"


class BrowserStopped(BrowserError, Stopped):
    pass


class OutcomeUnknown(BrowserError):
    """My press wasn't confirmed by the browser's own (trusted) events on the target: the batch stops here."""


class Pipe:
    """One authenticated connection to the native host. Thread-safe; reconnects on the next call after a break."""

    def __init__(self):
        self.conn = None
        self.lock = threading.Lock()
        self.ids = itertools.count(1)
        self.events: list[dict] = []     # pushed by the extension through the host (e.g. the person cancelled the bar)
        self.last_hello: dict | None = None

    def _connect(self, wait: float = 3.0) -> None:
        addr = pipe_address()
        t0 = time.time()
        last = None
        while True:  # the host may be restarting (the service worker reconnects it)
            try:
                self.conn = mpc.Client(addr, family="AF_PIPE", authkey=load_or_create_key())
                try:  # every new connection says hello: my URL policy in, leftovers of dead engines released
                    hello = self._roundtrip("engine.hello", {"policy": policy.nav_policy(), "engine": ENGINE},
                                            CALL_TIMEOUT)
                    dead = [x for x in (hello or {}).get("leftovers") or [] if not engine_alive(str(x.get("owner")))]
                    self.last_hello = (self._roundtrip("orphans.release", {"tabs": dead}, CALL_TIMEOUT)
                                       if dead else {"closed": [], "kept": []})
                except BrowserError:
                    self.conn = None
                    raise
                return
            except mpc.AuthenticationError:
                raise BrowserError("the Chrome bridge refused my key (state\\chrome.key differs from the host's)")
            except (FileNotFoundError, ConnectionError, OSError) as e:
                last = e
            if time.time() - t0 >= wait:
                break
            time.sleep(0.15)
        raise BrowserError("Chrome isn't connected (no host pipe). If Chrome is open with the Hands extension, "
                           "it reconnects on its own within about 30 s: try again then. If Chrome is closed, it "
                           "needs opening first.")

    def close(self) -> None:
        with self.lock:
            if self.conn is not None:
                try:
                    self.conn.close()
                except Exception:
                    pass
            self.conn = None

    def call(self, method: str, params: dict | None = None, timeout: float = CALL_TIMEOUT,
             connect_wait: float = 3.0):
        timeout = min(CALL_TIMEOUT, max(0.1, timeout))
        with self.lock:
            for attempt in (0, 1):
                if self.conn is None:
                    self._connect(connect_wait)
                try:
                    return self._roundtrip(method, params, timeout)
                except _SendFailed:
                    self.conn = None
                    if attempt == 0:
                        continue  # the host restarted between calls: reconnect once; nothing was delivered
                    raise BrowserError("the Chrome bridge broke while sending")
        raise BrowserError("the Chrome bridge broke while sending")

    def drain_events(self) -> list[dict]:
        """Events that arrived between calls, without waiting (never blocks a caller holding the pipe)."""
        if self.lock.acquire(blocking=False):
            try:
                while self.conn is not None and self.conn.poll(0):
                    msg = self._parse(self.conn.recv_bytes(MAX_REPLY))
                    if isinstance(msg, dict) and "id" not in msg and isinstance(msg.get("event"), str):
                        self.events.append(msg)
            except (OSError, EOFError):
                self.conn = None
            finally:
                self.lock.release()
        out, self.events = self.events, []
        return out

    @staticmethod
    def _parse(raw: bytes):
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return None

    def _roundtrip(self, method: str, params: dict | None, timeout: float):
        """One request and its answer on the held connection (lock held by the caller)."""
        if True:
            if True:
                rid = next(self.ids)
                try:
                    self.conn.send_bytes(json.dumps({"id": rid, "method": method, "params": params or {}},
                                                    ensure_ascii=False).encode("utf-8"))
                except (OSError, EOFError):
                    raise _SendFailed()
                deadline = time.time() + timeout
                while True:
                    left = deadline - time.time()
                    try:
                        if left <= 0 or not self.conn.poll(left):
                            # a late answer is dropped by its id; the connection stays usable
                            raise BrowserError(f"Chrome didn't answer {method} within {timeout:.0f} s")
                        raw = self.conn.recv_bytes(MAX_REPLY)
                    except (OSError, EOFError):
                        self.conn = None
                        raise BrowserStopped("browser use stopped: the Chrome bridge closed mid-call (Chrome or the "
                                             "extension went away); look again")
                    msg = self._parse(raw)
                    if isinstance(msg, dict) and "id" not in msg and isinstance(msg.get("event"), str):
                        self.events.append(msg)
                        continue
                    if not isinstance(msg, dict) or msg.get("id") != rid:
                        continue  # a late answer to a call that already timed out
                    if "error" in msg:
                        err = msg["error"] if isinstance(msg["error"], dict) else {"message": str(msg["error"])}
                        code, text = str(err.get("code", "error")), str(err.get("message", ""))[:600]
                        if code in ("stopped", "gone"):
                            raise BrowserStopped(text if text.startswith("browser use stopped") else
                                                 f"browser use stopped: {text}")
                        if code == "denied":
                            raise policy.Denied(text)
                        raise BrowserError(f"{text} [{code}]")
                    return msg.get("result")


class _SendFailed(Exception):
    pass


# ---------------------------------------------------------------- keys (into the tab through CDP, never the OS)
_NAMED = {"enter": ("Enter", "Enter", 13, "\r"), "return": ("Enter", "Enter", 13, "\r"),
          "tab": ("Tab", "Tab", 9, None), "escape": ("Escape", "Escape", 27, None), "esc": ("Escape", "Escape", 27, None),
          "backspace": ("Backspace", "Backspace", 8, None), "delete": ("Delete", "Delete", 46, None),
          "del": ("Delete", "Delete", 46, None), "insert": ("Insert", "Insert", 45, None),
          "space": (" ", "Space", 32, " "), "home": ("Home", "Home", 36, None), "end": ("End", "End", 35, None),
          "pageup": ("PageUp", "PageUp", 33, None), "page_up": ("PageUp", "PageUp", 33, None),
          "pagedown": ("PageDown", "PageDown", 34, None), "page_down": ("PageDown", "PageDown", 34, None),
          "left": ("ArrowLeft", "ArrowLeft", 37, None), "up": ("ArrowUp", "ArrowUp", 38, None),
          "right": ("ArrowRight", "ArrowRight", 39, None), "down": ("ArrowDown", "ArrowDown", 40, None),
          "arrowleft": ("ArrowLeft", "ArrowLeft", 37, None), "arrowup": ("ArrowUp", "ArrowUp", 38, None),
          "arrowright": ("ArrowRight", "ArrowRight", 39, None), "arrowdown": ("ArrowDown", "ArrowDown", 40, None)}
for _i in range(1, 13):
    _NAMED[f"f{_i}"] = (f"F{_i}", f"F{_i}", 111 + _i, None)
_MODS = {"alt": 1, "ctrl": 2, "control": 2, "shift": 8}
_WIN = {"win", "windows", "meta", "super", "cmd", "command", "os", "lwin", "rwin"}


def cdp_key(chord) -> dict:
    if not isinstance(chord, str) or not chord.strip() or len(chord) > 40:
        raise BrowserError("key: a chord like 'Enter', 'ctrl+a', 'shift+Tab'")
    parts = ["+"] if chord.strip() == "+" else [p for p in chord.replace(" ", "").split("+") if p]
    mods = 0
    for m in parts[:-1]:
        ml = m.lower().removesuffix("_l").removesuffix("_r")
        if ml in _WIN:
            raise BrowserError("I never press the Windows/meta key.")
        if ml not in _MODS:
            raise BrowserError(f"Unknown modifier {m!r}")
        mods |= _MODS[ml]
    k = parts[-1]
    kl = k.lower().removesuffix("_l").removesuffix("_r")
    if kl in _WIN:
        raise BrowserError("I never press the Windows/meta key.")
    if k.lower() in _NAMED:
        key, code, vk, text = _NAMED[k.lower()]
    elif len(k) == 1:
        if k.isascii() and k.isalpha():
            up = k.upper()
            key, code, vk = (up if mods & 8 else k.lower()), f"Key{up}", ord(up)
        elif k.isascii() and k.isdigit():
            key, code, vk = k, f"Digit{k}", ord(k)
        else:
            key, code, vk = k, "", 0
        text = key
    else:
        raise BrowserError(f"Unknown key {k!r}; type text with {{\"type\": ...}}")
    if mods & 3 and key != "Enter":  # ctrl/alt chords insert no text
        text = None
    return {"key": key, "code": code, "vk": vk, "text": text, "modifiers": mods}


# ---------------------------------------------------------------- the tree
COLLAPSE = {"generic", "none", "presentation", "LineBreak", "paragraph", "Section", "GenericContainer", "Div",
            "group", "Abbr", "time", "Pre", "Ruby", "Mark", "LayoutTable", "LayoutTableRow", "LayoutTableCell"}
STATE_ORDER = ("focused", "disabled", "checked", "pressed", "expanded", "selected", "required", "invalid",
               "readonly", "level", "multiline", "modal")
MARK = "\U0001FA77 "  # the overlay's pink heart in the title: mine, not the page's


@dataclass
class BLook:
    id: int
    tab: int
    loader: str
    refs: dict           # ref -> (frameId, backendNodeId)
    lines: dict          # ref -> rendered line (no indent), for the next diff
    at: float


def _q(s, n: int = 160) -> str:
    s = " ".join(str(s).split())
    return json.dumps(s[:n] + ("…" if len(s) > n else ""), ensure_ascii=False)


def _line(n: dict) -> str:
    role = n.get("r") or "?"
    out = ["text" if role == "StaticText" else role]
    name = n.get("n")
    if role == "RootWebArea" and isinstance(name, str) and name.startswith(MARK):
        name = name[len(MARK):]
    if name:
        out.append(_q(name))
    if n.get("v") is not None and n.get("v") != n.get("n"):
        out.append("value=" + _q(n["v"], 200))
    st = n.get("s") or {}
    for k in STATE_ORDER:
        if k in st:
            v = st[k]
            out.append(k if v is True or v == "true" else f"{k}={v}")
    return " ".join(out)


class Browser:
    """My hands in Chrome. One per engine; every pipe call bounded; every frame gated."""

    def __init__(self, hands):
        self.h = hands
        self.pipe = Pipe()
        self.lock = threading.RLock()
        self.seq = 0
        self.looks: dict[int, BLook] = {}
        self.refmaps: dict[int, tuple[str, dict, itertools.count]] = {}  # tab -> (loader, (frame,node)->ref, counter)
        self.opened: set[int] = set()
        hands.poll_events = self.poll_events  # a stop from Chrome stops the desktop surface too

    def poll_events(self) -> None:
        for ev in self.pipe.drain_events():
            if ev.get("event") == "stopped":
                self.h.stops.stop_all(str(ev.get("reason") or "browser use stopped")[:120])

    @staticmethod
    def _grantee() -> str:
        """Who is asking, as the bouncer names a session: house + session id. A joined / reclaimed tab answers only
        the session the person said yes to."""
        from .bouncer import me_from_env
        me = me_from_env()
        return f"{me.house}:{me.id}"

    def _call(self, method: str, params: dict | None = None, **kw):
        params = dict(params or {})
        params["grantee"] = self._grantee()  # always this engine's own session, never a caller's value
        try:
            return self.pipe.call(method, params, **kw)
        except BrowserStopped as e:
            if "cancelled the debugging bar" in str(e):
                self.h.stops.stop_all("the person cancelled the debugging bar")
            raise
        finally:
            for ev in self.pipe.events[:]:
                if ev.get("event") == "stopped":
                    self.h.stops.stop_all(str(ev.get("reason") or "browser use stopped")[:120])
            self.pipe.events.clear()

    # ------------------------------------------------ policy
    def _gate_site(self, url: str, *, opening: bool = False) -> None:
        """The tab's own site: deny list, then the person's consent (the card, or needs_consent when headless)."""
        policy.check_url(url, opening=opening)
        if url.strip().lower().split("#")[0] == "about:blank":
            return
        gate_consent(self.h, policy.site_key(url), policy.url_host(url), f"Chrome · {url[:90]}")

    def _frame_verdict(self, url: str) -> str:
        """ok | denied | consent for a sub-frame (frames never raise cards: a page can't spam the person)."""
        if not url.lower().startswith(("http://", "https://")):
            return "denied"  # error pages, data:, blob:, file:, internal schemes: never mine to read
        try:
            policy.check_url(url)
        except policy.Denied:
            return "denied"
        key = policy.site_key(url)
        if key in self.h.consent.refused:
            return "denied"
        return "ok" if self.h.consent.allowed(key) else "consent"

    def _verdicts(self, frames: list) -> dict:
        out: dict[str, str] = {}
        for f in frames:  # tree order: a parent's verdict exists before its children's
            u = str(f.get("url") or "")
            if not f.get("parent"):
                if not u.lower().startswith(("http://", "https://")) and u.lower() != "about:blank":
                    raise policy.Denied(f"I only drive http(s) pages (this one is {u.split(':')[0]}:).")
                self._gate_site(u)  # raises Denied / NeedsConsent for the page itself
                out[f["id"]] = "ok"
            elif u in ("about:blank", "about:srcdoc"):
                out[f["id"]] = out.get(f["parent"], "denied")  # an empty/srcdoc frame is its creator's
            else:
                out[f["id"]] = self._frame_verdict(u)
        return out

    def _approved(self, tab: int, resume: bool) -> tuple[dict, dict, str]:
        fr = self._call("frames", {"tabId": tab, "resume": resume})
        frames = fr.get("frames") or []
        v = self._verdicts(frames)
        return {f["id"]: f["url"] for f in frames if v.get(f["id"]) == "ok"}, v, str(fr.get("loaderId") or "")

    # ------------------------------------------------ catalog
    def tabs(self) -> list:
        with self.lock:
            return self._call("tabs.list")

    def open(self, url: str, session: str | None = None, watch: bool = False) -> dict:
        from .identity import get as _identity
        session = session or _identity()["session"]
        with self.lock:
            self.h._check_stop("browser")
            if not isinstance(url, str) or len(url) > 4000:
                raise BrowserError("url: an http(s) URL")
            self._gate_site(url, opening=True)
            name = "".join(ch for ch in str(session) if ch.isprintable())[:24] or _identity()["session"]
            res = self._call("tabs.open", {"url": url, "session": name, "watch": bool(watch)})
            self.opened.add(int(res["id"]))
            return res

    def _enter(self, kind: str, tab: int, session: str | None) -> dict:
        """JOIN (a tab the person opened) or RECLAIM (one I opened and handed over): the tab's CURRENT page passes the
        deny list and its site consent, then the person answers a card for THIS tab (Once or No: never remembered),
        then the extension lets me in. The yes lasts until the tab closes or I leave it (browser_leave): across turns
        and hands_done, which only detaches the debugger."""
        from .identity import get as _identity
        with self.lock:
            self.h._check_stop("browser")
            tab = self._tab(tab)
            t = self._call("tabs.get", {"tabId": tab})
            url = str(t.get("url") or "")
            if not url.lower().startswith(("http://", "https://")):
                raise BrowserError(f"tab {tab} isn't on a web page (it's on {url.split(':')[0]}:); nothing to {kind}")
            self._gate_site(url)
            title = str(t.get("title") or "")[:60]
            what = "be in your tab with you" if kind == "join" else "take back the tab he handed you"
            key = f"{kind}:{tab}"
            try:
                gate_consent(self.h, key, f"{title or policy.url_host(url)}",
                             f"{_identity()['name']} wants to {what}: {url[:80]}")
                name = "".join(ch for ch in str(session or _identity()["session"]) if ch.isprintable())[:24] or "rowan"
                res = self._call(f"tabs.{kind}", {"tabId": tab, "session": name})
            finally:
                # the card's yes is spent on entering: what holds it from here is the joined/reclaimed tab itself
                # (until the tab closes or I leave). Coming back after a leave asks again.
                self.h.consent.session.discard(key)
            self.opened.add(tab)  # done lets go of it (joined) or hands it back (reclaimed)
            self.looks.pop(tab, None)
            self.refmaps.pop(tab, None)
            return res

    def leave(self, tab: int) -> dict:
        with self.lock:
            tab = self._tab(tab)
            res = self._call("tabs.leave", {"tabId": tab})
            self.opened.discard(tab)
            self.looks.pop(tab, None)
            self.refmaps.pop(tab, None)
            return res

    def join(self, tab: int, session: str | None = None) -> dict:
        return self._enter("join", tab, session)

    def reclaim(self, tab: int, session: str | None = None) -> dict:
        return self._enter("reclaim", tab, session)

    def close(self, tab: int) -> dict:
        with self.lock:
            tab = self._tab(tab)
            res = self._call("tabs.close", {"tabId": tab})
            self.opened.discard(tab)
            self.looks.pop(tab, None)
            self.refmaps.pop(tab, None)
            return res

    def done(self) -> dict | None:
        """Close the tabs THIS engine opened (unless kept); kept ones are handed to the person. Never anything else."""
        with self.lock:
            ids = sorted(self.opened)
            self.looks.clear()
            self.refmaps.clear()
            if not ids:
                return None
            try:
                res = self._call("done", {"tabIds": ids}, connect_wait=0.5)
            except HandsError as e:
                return {"error": str(e)}
            self.opened.clear()
            return res

    @staticmethod
    def _tab(tab) -> int:
        if isinstance(tab, bool) or not isinstance(tab, int):
            raise BrowserError("tab: the tab id from browser_tabs / browser_open")
        return tab

    # ------------------------------------------------ look
    def look(self, tab: int, screenshot: bool = True, tree: bool = True, find: str | None = None,
             diff: bool = True, tree_cap: int = TREE_CAP, clear_stop: bool = True) -> tuple[str, list]:
        with self.lock:
            tab = self._tab(tab)
            t0 = time.perf_counter()
            info = self._call("tabs.get", {"tabId": tab})
            if not (info.get("mine") or info.get("joined")):
                raise BrowserError(f"tab {tab} is not one the agent opened or joined; browser_join asks the person first")
            policy.check_url(info.get("url", ""))  # before anything attaches
            if clear_stop:  # a fresh look by me is how I come back after the person stopped it; act's closing look never is
                self.h.resume("browser")
            approved, verdicts, _ = self._approved(tab, resume=clear_stop)
            r = self._call("look", {"tabId": tab, "screenshot": bool(screenshot), "tree": bool(tree or find),
                                        "approved": approved, "resume": False})
            return self._render(tab, r, verdicts, find, bool(diff), max(1000, min(60000, int(tree_cap))), t0)

    def _render(self, tab, r, verdicts, find, diff, cap, t0) -> tuple[str, list]:
        loader = str(r.get("loaderId") or "")
        rm = self.refmaps.get(tab)
        if rm is None or rm[0] != loader:  # a new document: new refs
            rm = (loader, {}, itertools.count(1))
            self.refmaps[tab] = rm
        _, keymap, counter = rm
        prev = self.looks.get(tab)
        frames = r.get("frames") or []
        by_owner: dict = {}
        for f in frames:
            if f.get("parent") and isinstance(f.get("owner"), int):
                by_owner.setdefault((f["parent"], f["owner"]), []).append(f)
        refs: dict[int, tuple[str, int]] = {}
        out: list[tuple[int, int | None, str]] = []  # (depth, ref, text)
        placed: set[str] = set()
        count = {"read": 0, "not": 0}

        def frame(f, depth):
            placed.add(f["id"])
            u = str(f.get("url", ""))
            host = policy.url_host(u) if u.lower().startswith(("http://", "https://")) else (u.split(":", 1)[0] + ":")
            v = verdicts.get(f["id"], "denied")
            if v != "ok" or f.get("skipped"):
                count["not"] += 1
                out.append((depth, None, f"[frame needs consent: {host}]" if v == "consent" else f"[frame denied: {host}]"))
                return
            if f.get("error"):
                count["not"] += 1
                out.append((depth, None, f"[frame not readable: {host} ({f['error']})]"))
                return
            if "nodes" not in f:
                return
            count["read"] += 1
            nodes = {n["i"]: n for n in f["nodes"] if isinstance(n, dict) and "i" in n}
            root = next((n for n in f["nodes"] if isinstance(n, dict) and not n.get("p")), None)
            seen: set = set()

            def walk(n, depth, parent_name):
                if n.get("i") in seen or len(out) > 8000:
                    return
                seen.add(n.get("i"))
                role = n.get("r") or ""
                if role == "InlineTextBox":
                    return
                name = (n.get("n") or "").strip()
                emit = not (n.get("ig") or (role in COLLAPSE and not name and n.get("v") is None))
                if role == "StaticText" and (not name or (parent_name and name in parent_name)):
                    emit = False
                d, pname = depth, parent_name
                b = n.get("b")
                if emit:
                    ref = None
                    if isinstance(b, int):
                        k = (f["id"], b)
                        if k not in keymap:
                            keymap[k] = next(counter)
                        ref = keymap[k]
                        refs[ref] = k
                    out.append((depth, ref, _line(n)))
                    d, pname = depth + 1, (name or parent_name)
                for c in n.get("c") or []:
                    if c in nodes:
                        walk(nodes[c], d, pname)
                if isinstance(b, int):
                    for cf in by_owner.get((f["id"], b), []):
                        frame(cf, d)

            if root is not None:
                walk(root, depth, "")

        main = next((f for f in frames if not f.get("parent")), None)
        if main is not None:
            frame(main, 0)
        for f in frames:  # frames whose owner element never showed in the tree
            if f["id"] not in placed and (f.get("parent") in placed):
                frame(f, 1)

        lines: dict[int, str] = {}
        rendered = []
        for depth, ref, txt in out:
            rendered.append("  " * min(depth, 30) + (f"[{ref}] " if ref is not None else "") + txt)
            if ref is not None:
                lines[ref] = txt
        self.seq += 1
        lk = BLook(self.seq, tab, loader, refs, lines, time.time())
        self.looks[tab] = lk
        title = str(r.get("title") or "")[:120]
        ms = round((time.perf_counter() - t0) * 1000)
        head = (f"look {lk.id} · tab {tab} · \"{title}\" · {str(r.get('url') or '')[:200]} · {len(refs)} refs · "
                f"frames {count['read']} read/{count['not']} not · {ms} ms"
                f"{' · LOADING' if r.get('loading') else ''}{' · ACTIVE (the person can see it)' if r.get('active') else ''}\n")
        if isinstance(r.get("jpeg"), str):
            nr = int(r.get("not_rendered") or 0)
            head += (f"screenshot: {int(r.get('covered') or 0)} frame(s) covered"
                     f"{f', {nr} not rendered' if nr else ''}\n")
        if r.get("shot_error"):
            head += f"note: no screenshot: {r['shot_error']}\n"
        if find:
            q = find.casefold()
            hits = [l.strip() for l in rendered if q in l.casefold()]
            body = f"find {find!r}: " + ("\n" + "\n".join(hits) if hits else "no match")
        elif diff and prev is not None and prev.loader == loader:
            add = [f"+ [{k}] {v}" for k, v in lines.items() if k not in prev.lines]
            rem = [f"- [{k}] {v}" for k, v in prev.lines.items() if k not in lines]
            chg = [f"~ [{k}] {v}" for k, v in lines.items() if k in prev.lines and prev.lines[k] != v]
            same = len(lines) - len(add) - len(chg)
            body = (f"diff vs look {prev.id}: +{len(add)} -{len(rem)} ~{len(chg)} ({same} unchanged; "
                    f"diff=false for the full tree)\n" + "\n".join(chg + add + rem))
        else:
            body = "\n".join(rendered)
            if prev is not None and prev.loader != loader:
                head += "note: a new page since my last look: full tree, new refs\n"
        if len(body) > cap:
            cut = body[:cap].rsplit("\n", 1)[0]
            body = cut + f"\n… ({body.count(chr(10)) - cut.count(chr(10))} more lines; use find=\"…\")"
        imgs = [base64.b64decode(r["jpeg"])] if isinstance(r.get("jpeg"), str) else []
        return head + body, imgs

    # ------------------------------------------------ act
    def act(self, tab: int, actions: list, look_id, then: str = "both") -> tuple[str, list]:
        with self.lock:
            tab = self._tab(tab)
            check_then(then)
            try:
                self.h._check_stop("browser")
            except Stopped as e:  # a stop is the answer, not a failure of the call
                return f"STOPPED: {e}", []
            if not isinstance(actions, list) or not 1 <= len(actions) <= 60:
                raise BrowserError("actions: a list of 1–60 action objects")
            lk = self.looks.get(tab)
            if lk is None:
                raise BrowserError("Look at the tab first (browser_look): refs come from a look.")
            if isinstance(look_id, bool) or look_id != lk.id:
                raise BrowserError(f"look {look_id} is stale; the latest look of tab {tab} is {lk.id}. Use its refs.")
            log: list[str] = []
            stopped = False
            try:
                approved, _, loader = self._approved(tab, resume=False)
                if loader != lk.loader:
                    raise BrowserError("the page navigated since my look; its refs are stale; look again")
                for i, a in enumerate(actions):
                    self.h._check_stop("browser")
                    t = time.perf_counter()
                    try:
                        msg = self._do(tab, lk, approved, a)
                    except OutcomeUnknown as e:  # nothing after this may build on an unknown
                        log.append(f"{i}: {e}")
                        break
                    log.append(f"{i}: {msg} ({round((time.perf_counter() - t) * 1000)} ms)")
                self.h._check_stop("browser")
            except Stopped as e:
                stopped = True
                log.append(f"STOPPED: {e}")
            except (HandsError, policy.Denied) as e:  # what already happened stays in the log
                log.append(f"refused: {e}")
            except Exception as e:  # outcome unknown: say so, never pretend
                log.append(f"error ({type(e).__name__}): {str(e)[:300]} — outcome unknown, look again before retrying")
            if not stopped:
                self._settle(tab)
                try:  # loads my tab tried that the URL gate refused (a link, a script, a redirect)
                    gone = list(dict.fromkeys(self._call("nav.blocked", {"tabId": tab}).get("blocked") or []))
                    for g in gone[:6]:
                        log.append(f"blocked: my tab {str(g)[:200]}")
                except HandsError:
                    pass
            out = "\n".join(log)
            return close_with_look(out, then, stopped, True, lambda: self.look(
                tab, screenshot=then in ("both", "shot"), tree=then in ("both", "tree"), clear_stop=False))

    def _settle(self, tab: int) -> None:
        t0 = time.time()
        time.sleep(0.05)
        while time.time() - t0 < SETTLE_S:
            try:
                if not self._call("tabs.get", {"tabId": tab}, timeout=2).get("loading"):
                    return
            except HandsError:
                return
            time.sleep(0.1)

    @staticmethod
    def _ref(lk: BLook, ref) -> dict:
        if isinstance(ref, bool) or not isinstance(ref, int) or ref not in lk.refs:
            raise BrowserError(f"No ref [{ref}] in look {lk.id}.")
        frame, node = lk.refs[ref]
        return {"frame": frame, "node": node}

    def _op(self, tab, lk, approved, op, **kw) -> dict:
        return self._call("act", {"tabId": tab, "op": op, "approved": approved, "loaderId": lk.loader, **kw})

    def _do(self, tab: int, lk: BLook, approved: dict, a) -> str:
        check_action(a)
        if "wait" in a:
            s = float(a["wait"])
            time.sleep(s)
            return f"waited {s}s"
        if "keep" in a:
            keep = a["keep"] is not False
            self._call("tabs.keep", {"tabId": tab, "keep": keep})
            return "this tab stays open for the person at hands_done" if keep else "keep cleared"
        if "click" in a:
            button, count = a.get("button", "left"), a.get("count", 1)
            if button not in ("left", "right", "middle") or isinstance(count, bool) or count not in (1, 2, 3):
                raise BrowserError("button left|right|middle, count 1–3")
            r = self._op(tab, lk, approved, "click", ref=self._ref(lk, a["click"]), button=button, count=count)
            if r.get("reached") is not True:
                why = ("the page changed right after, so its own events couldn't confirm it" if r.get("reached") is None
                       else "the browser's own press and release didn't reach it (something else took them)")
                raise OutcomeUnknown(f"outcome unknown: the click didn't reach [{a['click']}] "
                                     f"{lk.lines.get(a['click'], '')[:50]} — {why}; look again before going on")
            return (f"clicked [{a['click']}] {lk.lines.get(a['click'], '')[:50]} at {r.get('x')},{r.get('y')} "
                    f"(in the tab; the person's cursor untouched)")
        if "type" in a:
            text = a["type"]
            if not isinstance(text, str) or len(text) > TEXT_CAP:
                raise BrowserError(f"type: text up to {TEXT_CAP} chars")
            ref = self._ref(lk, a["into"]) if a.get("into") is not None else None
            self._op(tab, lk, approved, "type", ref=ref, text=text)
            return f"typed {len(text)} chars" + (f" into [{a['into']}]" if ref else " at focus")
        if "set" in a:
            v = a.get("value", "")
            if not isinstance(v, str) or len(v) > TEXT_CAP:
                raise BrowserError(f"set: value is text up to {TEXT_CAP} chars")
            self._op(tab, lk, approved, "set", ref=self._ref(lk, a["set"]), value=v)
            return f"set [{a['set']}] ({len(v)} chars; input + change fired)"
        if "select" in a:
            v = a.get("value")
            if not isinstance(v, str) or len(v) > 400:
                raise BrowserError("select: value = the option's value or its text")
            r = self._op(tab, lk, approved, "select", ref=self._ref(lk, a["select"]), value=v)
            return f"selected {r.get('picked')!r} in [{a['select']}]"
        if "key" in a:
            k = cdp_key(a["key"])
            ref = self._ref(lk, a["into"]) if a.get("into") is not None else None
            self._op(tab, lk, approved, "key", key=k, ref=ref)
            return f"pressed {a['key']}"
        if "scroll" in a:
            try:
                dy, dx = int(a.get("dy", 3)), int(a.get("dx", 0))
            except (TypeError, ValueError, OverflowError):
                raise BrowserError("dy and dx are whole notches")
            if abs(dy) > 50 or abs(dx) > 50:
                raise BrowserError("Scroll at most 50 notches per action.")
            tgt = a["scroll"]
            ref = None if tgt is None or tgt is True else self._ref(lk, tgt)
            self._op(tab, lk, approved, "scroll", ref=ref, dy=dy, dx=dx)
            return f"scrolled dy={dy} dx={dx}"
        if "goto" in a:
            url = a["goto"]
            if not isinstance(url, str) or len(url) > 4000:
                raise BrowserError("goto: an http(s) URL")
            self._gate_site(url, opening=True)
            self._op(tab, lk, approved, "goto", value=url)
            return f"going to {url[:120]} (refs after this belong to the next look)"
        if "back" in a or "forward" in a:
            d = "forward" if "forward" in a else "back"
            h = self._op(tab, lk, approved, "history", value=d)
            self._gate_site(str(h.get("url") or ""), opening=True)  # the page I'd land on, before I move
            self._op(tab, lk, approved, d, entryId=h.get("entryId"))
            return f"went {d}"
        raise BrowserError(f"Unknown action {list(a)[0]!r}")
