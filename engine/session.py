"""Hands: computer use on Windows. Background first; when the real mouse or keyboard is
unavoidable, it waits for the person's hands to rest, borrows, acts, and gives everything back.
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass

import uiautomation as auto
import win32gui

from . import identity, keys, policy, tree, win
from . import secrets as secretscan
from .common import (TEXT_CAP, HandsError, NeedsConsent, Stopped, Stops, check_action, check_then,  # noqa: F401
                     close_with_look, gate_consent, test_hooks)
from .overlay_link import OverlayLink

SCAN_CAP = 5000  # elements read to check a screenshot for secrets; a window with more gets no screenshot
IDLE_NEEDED_MS = 700
IDLE_WAIT_S = 8.0
ALIASES = {"calc": "calculator", "notepad": "notepad", "explorer": "file explorer", "settings": "settings",
           "paint": "paint", "word": "word", "excel": "excel", "chrome": "google chrome"}


CLOSING_NAMES = {"exit", "quit", "close", "close window", "close tab", "close all", "close all tabs", "exit app",
                 "יציאה", "סגור", "סגירה", "סגור חלון", "סגירת חלון", "סגור כרטיסייה", "סגירת כרטיסייה"}


def closing_menu_item(name: str, accel: str) -> bool:
    """A menu item that closes the window, a document or the app: by its shortcut (any language), else by its name
    (English and Hebrew; a trailing '...' or '&' mnemonic ignored)."""
    a = accel.strip()
    if a:
        try:
            if keys.closes(a):
                return True
        except Exception:
            pass
    nm = name.replace("&", "").split("\t")[0].strip().rstrip(".").strip().casefold()
    return nm in CLOSING_NAMES


@dataclass
class Look:
    hwnd: int
    nodes: list
    origin: tuple[int, int]
    rect: tuple[int, int, int, int]
    scale: float
    at: float
    id: int = 0


class Hands:
    def __init__(self, overlay: bool = True):
        self.tree = tree.Tree()
        self.consent = policy.Consent()
        self.ov = OverlayLink()
        self.use_overlay = overlay
        self.looks: dict[int, Look] = {}
        self.look_seq = 0
        self.launched: dict[int, str] = {}  # windows I opened (hwnd -> title), closed at done() unless kept
        self.made: set[int] = set()  # every window I opened, kept ones included: my full hands stay in them
        self.stops = Stops()
        self.poll_events = None  # the browser side plugs in here: a stop from Chrome reaches the desktop too
        self.injected_until = 0
        self.her_fg = 0
        self.apps_cache: list[dict] | None = None
        self.apps_cache_at = 0.0
        # the system-wide screen-reader flag makes Chromium/Electron apps expose their insides to UI
        # Automation (they read it when they start); it doesn't survive logoff, so it's asserted here, but only
        # when the owner turned it on ("screen_reader_flag": true in state/config.json): it changes the whole system
        try:
            if not identity.setting("screen_reader_flag", False):
                raise RuntimeError("off")
            import ctypes
            ctypes.windll.user32.SystemParametersInfoW(0x0047, 1, None, 0x2)  # SPI_SETSCREENREADER, SPIF_SENDCHANGE
        except Exception:
            pass
        # test-only: lets a test drive what it opens; honoured only with HANDS_TEST_HOOKS=1 (never in normal use)
        if test_hooks():
            for k in filter(None, os.environ.get("HANDS_TEST_ALLOW", "").split(";")):
                self.consent.session.add(k)

    # ================================================================ catalog
    def windows(self, query: str | None = None) -> list[dict]:
        out = [w.brief() for w in win.top_windows()]
        if query:
            q = query.casefold()
            out = [w for w in out if q in w["title"].casefold() or q in w["app"].casefold() or q in w["exe"].casefold()]
        return out

    def apps(self, query: str | None = None) -> list[dict]:
        if self.apps_cache is None or time.time() - self.apps_cache_at > 600:
            import win32com.client
            shell = win32com.client.Dispatch("Shell.Application")
            folder = shell.NameSpace("shell:AppsFolder")
            self.apps_cache = [{"name": it.Name, "id": it.Path} for it in folder.Items()]
            self.apps_cache_at = time.time()
        res = self.apps_cache
        if query:
            q = ALIASES.get(query.casefold(), query).casefold()
            exact = [a for a in res if a["name"].casefold() == q]
            starts = [a for a in res if a["name"].casefold().startswith(q) and a not in exact]
            contains = [a for a in res if q in a["name"].casefold() and a not in exact and a not in starts]
            # Windows may be in any language: ids (AUMIDs, exe paths) are language-proof
            by_id = [a for a in res if q.replace(" ", "") in a["id"].casefold().replace(" ", "")
                     and a not in exact and a not in starts and a not in contains]
            res = exact + starts + contains + by_id
        return res

    # ================================================================ gates
    def _her_hands_tick(self) -> int:
        if self.ov.alive and self.ov.her_input_tick:
            return self.ov.her_input_tick
        t = win.last_input_tick()
        return t if t > self.injected_until + 60 else 0

    def _gate(self, w: win.Win) -> None:
        policy.check_denied(w.exe, w.app, w.title, w.cls, w.id)
        name = w.title.split(" - ")[-1] if w.title else os.path.basename(w.exe)
        gate_consent(self, policy.consent_key(w.exe, w.app), name, f"{os.path.basename(w.exe)} · {w.title[:80]}")

    def _poll_stops(self) -> None:
        if self.ov.stop_requested:
            self.ov.stop_requested = False
            self.stops.stop_all("Esc×2 / Stop")
            self.ov.idle()
        if self.poll_events:
            try:
                self.poll_events()
            except Exception:
                pass

    def _check_stop(self, surface: str = "desktop") -> None:
        self._poll_stops()
        self.stops.check(surface)

    def resume(self, surface: str) -> None:
        """A fresh look of mine on that surface: the only way back after the person stopped it."""
        self._poll_stops()
        self.stops.resume(surface)

    @property
    def stopped(self) -> bool:
        return self.stops.desktop

    def _win(self, hwnd: int) -> win.Win:
        if not win32gui.IsWindow(hwnd):
            raise HandsError(f"Window {hwnd} is gone. Call hands_windows.")
        return win.describe(hwnd)

    # ================================================================ launch
    def launch(self, target: str, watch: bool = False) -> dict:
        self._check_stop()
        her_fg = win.foreground()
        before = win.all_top_hwnds()  # every window in any state: an un-hidden old window is never "new"
        path = target
        if "!" not in target and not target.lower().endswith(".exe") and not os.path.exists(target):
            hits = self.apps(target)
            if not hits:
                raise HandsError(f"No installed app matches {target!r}.")
            path = hits[0]["id"]
        if "!" not in path and path.lower().endswith(".exe") and not os.path.isabs(path):
            # a bare name like "foo.exe" would be found by the shell somewhere on PATH: resolve it to ONE absolute
            # file here, so the deny check, the consent key and the launch all see the same program
            import shutil
            found_exe = shutil.which(path)
            if not found_exe:
                raise HandsError(f"{path!r} isn't on this machine's PATH; give its full path or the app's name.")
            path = os.path.abspath(found_exe)
        # from here on, ONLY the path the check judged: consent, the launch and the window match all use it
        path = policy.check_launch_target(path)
        exe_hint = os.path.basename(path).lower() if path.lower().endswith(".exe") else ""
        aumid = path if "!" in path else ""
        if not aumid and not exe_hint:
            raise HandsError(f"{path!r} is neither an installed app nor an .exe; I only launch apps.")
        app_key = aumid or exe_hint
        gate_consent(self, policy.consent_key(path, app_key),
                     os.path.splitext(os.path.basename(path))[0] if not aumid else target, f"launch {path[:90]}")

        # a packaged app's window doesn't always carry its AUMID (Notepad doesn't); its exe then lives in the
        # package's own folder: ...\WindowsApps\<PackageName>_<version>_<arch>__<publisherId>\...
        pkg = aumid.split("!", 1)[0] if aumid else ""
        pkg_name, pkg_pub = (pkg.split("_", 1) + [""])[:2] if pkg else ("", "")

        def mine(w: win.Win) -> bool:
            if aumid and w.app.casefold() == aumid.casefold():
                return True
            if pkg_name:
                parts = w.exe.casefold().replace("/", "\\").split("\\")
                return any(p.startswith(pkg_name.casefold() + "_") and p.endswith("__" + pkg_pub.casefold())
                           for p in parts)
            return bool(exe_hint and os.path.basename(w.exe).lower() == exe_hint)

        if aumid:
            os.startfile(f"shell:AppsFolder\\{aumid}")
        else:
            os.startfile(path)
        t0 = time.time()
        found = None
        while time.time() - t0 < 8.0 and not found:
            time.sleep(0.12)
            match = [w for w in win.top_windows() if w.id not in before and mine(w)]
            found = match[0] if match else None
        reused = False
        if not found:  # single-instance apps reuse an existing window
            reuse = [w for w in win.top_windows() if mine(w)]
            found = reuse[0] if reuse else None
            reused = bool(found)
        if not found:
            raise HandsError(f"Launched {target}, but no window of it appeared in 8 s.")
        policy.check_denied(found.exe, found.app, found.title, found.cls, found.id)
        time.sleep(0.25)
        if not watch and her_fg and her_fg != found.id and win.foreground() == found.id:
            win.set_foreground(her_fg)
        if self.use_overlay:
            self.ov.target(found.id)
        res = {**win.describe(found.id).brief(), "launched_in_ms": round((time.time() - t0) * 1000)}
        holds = None if reused else self._holds_documents(found.id)
        if reused:
            res["note"] = "no new window appeared; this is an EXISTING window of the app and it may be the person's"
        elif holds:
            # session restore (Notepad does this) can open the person's saved tabs inside a genuinely new window
            res["note"] = (f"a NEW window, but it already holds {holds}: it belongs to the person, not to me; "
                           f"I touch nothing in it and won't close it")
        else:
            self.launched[found.id] = res["title"]
            self.made.add(found.id)
        res["mine"] = found.id in self.launched
        return res

    @staticmethod
    def _holds_documents(hwnd: int) -> str | None:
        """A window is mine only if it's new AND empty of anything of the person's. Restored content shows as a document
        name in the title or as more than one tab. Waits a moment: restore fills the window after it appears."""
        import re
        time.sleep(0.5)
        title = (win.title_of(hwnd) or "").lstrip("*").strip()
        head = title.split(" - ")[0]
        if re.search(r"\S\.[A-Za-z0-9]{1,8}$", head):
            return f"a document ({head[:60]!r})"
        try:
            root = auto.ControlFromHandle(hwnd)
            tabs = 0
            def walk(c, d=0):
                nonlocal tabs
                for ch in c.GetChildren():
                    if ch.ControlTypeName == "TabItemControl":
                        tabs += 1
                    if d < 7 and tabs < 2:
                        walk(ch, d + 1)
            walk(root)
            if tabs >= 2:
                return f"{tabs}+ tabs"
        except Exception:
            pass
        return None

    # ================================================================ look
    def _popups(self, hwnd: int) -> list[int]:
        """Popups of the window, each deny-checked by its OWN process (an owned dialog can belong to anyone)."""
        ok = []
        for ph in win.popups_of(hwnd)[:3]:
            try:
                pw = win.describe(ph)
                policy.check_denied(pw.exe, pw.app, pw.title, pw.cls, ph)
                if pw.pid != win.describe(hwnd).pid and not self.consent.allowed(policy.consent_key(pw.exe, pw.app)):
                    continue
                ok.append(ph)
            except policy.Denied:
                continue
        return ok

    def look(self, hwnd: int, shot: bool = True, show_tree: bool = True, find: str | None = None,
             tree_cap: int = 12000, clear_stop: bool = True) -> tuple[str, list[bytes]]:
        w = self._win(hwnd)
        self._gate(w)
        if clear_stop:  # a fresh look by me is how I come back after the person stopped it; act's own closing look never is
            self.resume("desktop")
        if w.minimized:
            win.restore_quietly(hwnd)
            w = self._win(hwnd)
        if self.use_overlay:
            self.ov.target(hwnd)
        t0 = time.perf_counter()
        fl, ft, fr, fb = w.rect
        images: list[bytes] = []
        scale = 1.0
        notes = []
        pops = self._popups(hwnd)
        shots = []  # (window, image, its screen origin): encoded only after the secrets on it are blacked out
        before: dict[int, list] = {}
        if shot:
            # the windows' text is read before AND after the capture: a secret in either is blacked out, so text that
            # changes while the picture is taken can't slip past
            for h in (hwnd, *pops):
                before[h] = self.tree.snapshot(h, (fl, ft), max_nodes=SCAN_CAP)
            self._hide_secrets([n for g in before.values() for n in g])
            img = win.capture(hwnd)
            if win.is_blank(img):
                notes.append("screenshot came back blank (GPU surface or protected content)")
            shots.append((hwnd, img, (fl, ft)))
            for ph in pops:
                pl, pt, _, _ = win.frame_rect(ph)
                shots.append((ph, win.capture(ph), (pl, pt)))
        nodes = []
        text = ""
        groups: dict[int, list] = {}
        if show_tree or find or shot:  # a screenshot is checked for secrets through the window's own text
            cap = SCAN_CAP if shot else 1500
            nodes = self.tree.snapshot(hwnd, (fl, ft), max_nodes=cap)
            groups[hwnd] = list(nodes)
            for ph in pops:
                more = self.tree.snapshot(ph, (fl, ft), max_nodes=cap)
                groups[ph] = more
                for n in more:
                    n.index = len(nodes)
                    nodes.append(n)
        hidden = self._hide_secrets(nodes)
        if hidden:
            notes.append(f"secrets: {hidden} hidden (in the text as {secretscan.HIDDEN}, blacked out on the "
                         f"screenshot); never guess what is under them")
        unchecked = shot and any(len(g) >= SCAN_CAP for g in [*groups.values(), *before.values()])
        if unchecked:  # too much text to check every element: no picture (failing closed)
            shots = []
            notes.append(f"no screenshot: withheld: over {SCAN_CAP} elements, too many to check every one for "
                         f"secrets; use the tree (or find) instead")
        if shots:
            for i, (h, img, origin) in enumerate(shots):
                self._black_out(img, origin, groups.get(h, []) + before.get(h, []))
                data, sc = win.to_jpeg(img)
                images.append(data)
                if i == 0:
                    scale = sc
                else:
                    pl, pt, pr, pb = win.frame_rect(h)
                    notes.append(f"popup {h} '{secretscan.hide(win.title_of(h))[0][:40]}' at window "
                                 f"({round((pl - fl) * scale)},{round((pt - ft) * scale)}) "
                                 f"size {round((pr - pl) * scale)}x{round((pb - pt) * scale)} (image {len(images)})")
        else:
            l, t, r, b = w.rect
            long_edge = max(r - l, b - t)
            scale = min(1.0, win.MAX_LONG_EDGE / long_edge) if long_edge else 1.0
        if show_tree or find:
            if find:
                hits = tree.find(nodes, find)
                text = "\n".join(tree.render([n for n in nodes if n in hits or n.index == 0], (fl, ft), scale, tree_cap)[0].splitlines())
            else:
                text, _ = tree.render(nodes, (fl, ft), scale, tree_cap)
            if len(nodes) <= 1:
                notes.append("no accessibility tree: act by screenshot")
        self.look_seq += 1
        self.looks[hwnd] = Look(hwnd, nodes, (fl, ft), w.rect, scale, time.time(), self.look_seq)
        ms = round((time.perf_counter() - t0) * 1000)
        head = (f"look {self.look_seq} · window {hwnd} · {w.app} · \"{secretscan.hide(w.title)[0][:90]}\" · {fr - fl}x{fb - ft}px"
                f"{f' · image scale {scale:.3f}' if scale != 1 else ''} · {len(nodes)} elements · {ms} ms\n"
                f"coordinates: screenshot pixels, origin = window top-left\n")
        if notes:
            head += "note: " + "; ".join(notes) + "\n"
        return head + text, images

    # ---------------------------------------------------------------- secrets in what I'm shown
    @staticmethod
    def _hide_secrets(nodes: list) -> int:
        """Every element's own text (name, value, help) is checked; a secret is replaced in it, and the element is
        marked to be blacked out on the screenshot. Returns how many distinct secrets were hidden."""
        seen: set[str] = set()
        for n in nodes:
            hit = False
            for get, put in ((lambda: n.name, lambda v: setattr(n, "name", v)),
                             (lambda: n.props.get("value"), lambda v: n.props.__setitem__("value", v)),
                             (lambda: n.props.get("help"), lambda v: n.props.__setitem__("help", v))):
                raw = get()
                if not isinstance(raw, str) or not raw:
                    continue
                for s, e, _ in secretscan.spans(raw):
                    seen.add(raw[s:e])
                    hit = True
                if hit:
                    put(secretscan.hide(raw)[0])
            n.props["secret"] = hit
        count = len(seen)
        seen.clear()
        return count

    @staticmethod
    def _black_out(img, origin: tuple[int, int], nodes: list) -> None:
        """Paint over every element holding a secret, on this window's own image (its screen origin given)."""
        boxes = []
        for n in nodes:
            l, t, r, b = n.rect
            if not n.props.get("secret") or r <= l or b <= t:
                continue
            # a window's name is its title: drawn in its caption strip, not across the whole window
            boxes.append((l, t, r, min(b, t + 48)) if n.ctype == "Window" else (l, t, r, b))
        if not boxes:
            return
        from PIL import ImageDraw
        d = ImageDraw.Draw(img)
        ox, oy = origin
        for l, t, r, b in boxes:
            d.rectangle((l - ox - 3, t - oy - 3, r - ox + 3, b - oy + 3), fill=(58, 42, 51))

    # ================================================================ act
    def act(self, hwnd: int, actions: list[dict], then: str = "both", look_id: int | None = None) -> tuple[str, list[bytes]]:
        self._check_stop()
        check_then(then)
        w = self._win(hwnd)
        self._gate(w)
        lk = self.looks.get(hwnd)
        if lk is None:
            raise HandsError("Look at the window first (hands_look): indexes and coordinates come from a look.")
        if look_id is not None and look_id != lk.id:
            raise HandsError(f"look {look_id} is stale; the latest look of this window is {lk.id}. Use its indexes.")
        self.her_fg = win.foreground()
        start_tick = win.tick()
        log: list[str] = []
        took_fg = False
        stopped = False
        try:
            for i, a in enumerate(actions):
                self._check_stop()
                hands = self._her_hands_tick()
                if took_fg and hands and hands > start_tick:
                    log.append(f"{i}: stopped — the person's hands moved, so the agent let go")
                    break
                if not win32gui.IsWindow(hwnd):
                    log.append(f"{i}: the window closed; nothing more done")
                    break
                t = time.perf_counter()
                msg, fg = self._do(w, lk, a)
                took_fg = took_fg or fg
                log.append(f"{i}: {msg} ({round((time.perf_counter() - t) * 1000)} ms)")
            self._check_stop()  # a stop during the last action still counts
        except Stopped as e:
            stopped = True
            log.append(f"STOPPED: {e}")
        except (HandsError, keys.KeyError_, policy.Denied) as e:  # what already happened stays in the log
            log.append(f"refused: {e}")
        except Exception as e:  # outcome unknown: say so, never pretend
            log.append(f"error ({type(e).__name__}): {e} — outcome unknown, look again before retrying")
        finally:
            if self.ov.alive:
                self.ov.mode(False)
            fg_now = win.foreground()
            if self.her_fg and fg_now != self.her_fg and self.her_fg != hwnd and win32gui.IsWindow(self.her_fg):
                if not (self._her_hands_tick() > start_tick):
                    win.set_foreground(self.her_fg)
                    log.append("gave the person's window back")
        out = "\n".join(log)
        alive = bool(win32gui.IsWindow(hwnd))
        if not alive:
            out += "\nwindow closed"
        elif then != "none" and not stopped:
            time.sleep(0.12)
        return close_with_look(out, then, stopped, alive, lambda: self.look(
            hwnd, shot=then in ("both", "shot"), show_tree=then in ("both", "tree"), clear_stop=False))

    # ---------------------------------------------------------------- helpers
    def _node(self, lk: Look, idx) -> tree.Node:
        if isinstance(idx, bool) or not isinstance(idx, int) or idx < 0 or idx >= len(lk.nodes):
            raise HandsError(f"No element [{idx}] in the latest look.")
        return lk.nodes[idx]

    def _inside(self, lk: Look, w: win.Win, sx: int, sy: int, what) -> tuple[int, int]:
        cur = win.frame_rect(w.id)
        if cur != lk.rect:
            raise HandsError("The window moved or resized since my look; look again before real input.")
        l, t, r, b = cur
        if not (l <= sx < r and t <= sy < b):
            raise HandsError(f"{what} is outside the window.")
        return sx, sy

    def _pt(self, lk: Look, w: win.Win, xy) -> tuple[int, int]:
        if not (isinstance(xy, (list, tuple)) and len(xy) == 2):
            raise HandsError("A point is [x, y] in screenshot pixels.")
        try:
            x, y = float(xy[0]), float(xy[1])
        except (TypeError, ValueError):
            raise HandsError("A point's x and y must be numbers.")
        if not (math.isfinite(x) and math.isfinite(y)):
            raise HandsError("A point's x and y must be finite numbers.")
        return self._inside(lk, w, lk.origin[0] + round(x / lk.scale), lk.origin[1] + round(y / lk.scale), f"Point {xy}")

    def _node_pt(self, lk: Look, w: win.Win, n: tree.Node) -> tuple[int, int]:
        return self._inside(lk, w, *n.center, f"Element [{n.index}]")

    def _wait_her_idle(self) -> None:
        t0 = time.time()
        while time.time() - t0 < IDLE_WAIT_S:
            hands = self._her_hands_tick()
            if not hands or win.tick() - hands >= IDLE_NEEDED_MS:
                return
            time.sleep(0.08)
        raise HandsError("The person's hands are busy on the keyboard/mouse; the agent won't grab them. Retry in a moment, or ask them.")

    def _foreground(self, w: win.Win, fn, point: tuple[int, int] | None = None) -> None:
        self._wait_her_idle()
        if self.ov.alive:
            self.ov.mode(True)
        front = w.id
        # only popups that passed the deny list and consent (each by its OWN process) may ever take real input: an
        # owned dialog can belong to anyone, a credential prompt included
        allowed = (w.id, *self._popups(w.id))
        if not win32gui.IsWindowEnabled(w.id):
            # a modal dialog (Save As, a message box) has disabled this window: keys sent to the window itself are
            # thrown away, so real input goes to the dialog in front of it, if it is one we may touch
            modal = [p for p in allowed[1:] if win32gui.IsWindowEnabled(p)]
            if not modal:
                raise HandsError("A dialog Hands may not touch is blocking this window; it's the person's to answer.")
            front = modal[-1]
        if not win.set_foreground(front) and win.foreground() not in allowed:
            raise HandsError("Couldn't bring the window forward for real input.")
        if point is not None and win.root_at(*point) not in allowed:
            raise HandsError("Another window covers that point; I won't click into something that isn't mine to touch.")
        her_cursor = win.cursor_pos()  # saved right before this action, given back right after
        try:
            fn()
        finally:
            self.injected_until = win.tick() + 40
            win.set_cursor(*her_cursor)

    def _glide(self, x: int, y: int, kind: str) -> None:
        if self.ov.alive:
            self.ov.glide(x, y, kind)

    def _ctl(self, n: tree.Node):
        return auto.Control.CreateControlFromElement(n.el)

    # ---------------------------------------------------------------- the person's windows
    # The close guard matches the known ways an action closes a window (keys, the caption's Close and window menu,
    # closing menu items by shortcut or name). It guards the agent's own actions; it is not a sandbox around the app.
    def _is_hers(self, hwnd: int) -> bool:
        """A window I didn't open (nor a dialog owned by one I opened) is the person's. Once its app has their yes I
        am joined in it automatically: I look and act there, but I never close it."""
        self.made = {h for h in self.made if win32gui.IsWindow(h)}
        h = hwnd
        for _ in range(8):
            if not h:
                return True
            if h in self.made or h in self.launched:
                return False
            h = win32gui.GetWindow(h, 4)  # GW_OWNER
        return True

    def _never_close(self, w: win.Win, what: str) -> None:
        raise HandsError(f"{what} would close {w.title[:50]!r}, and that window is the person's: I work in it with "
                         f"them, but I never close it. If it should close, they close it.")

    @staticmethod
    def _in_title_bar(lk: Look, n: tree.Node) -> bool:
        """The element sits in the window's own title bar (its Close button, its window menu)."""
        depth = n.depth
        for m in reversed(lk.nodes[:n.index]):
            if m.depth < depth:
                if m.ctype == "TitleBar":
                    return True
                depth = m.depth
        return False

    def _guard_node(self, w: win.Win, lk: Look, n: tree.Node, verb: str, button: str = "left") -> None:
        """Closing her window by an element: the title bar's Close (by its id, or because the window itself says its
        close button is there: any language, even with something in front of it) or its window menu. In her title
        bar only minimize and maximize stay open to me."""
        if not self._is_hers(w.id):
            return
        code = win.hit_code(w.id, *n.center)
        in_bar = self._in_title_bar(lk, n)
        if ((n.ctype == "Button" and n.aid == "Close") or code in (20, 3)  # wherever it sits: the close spot, the menu
                or (in_bar and (n.ctype in ("MenuBar", "MenuItem") or button != "left"   # a right click opens the menu
                                or (n.ctype == "Button" and code not in (8, 9))))):
            self._never_close(w, f"{verb} [{n.index}] {n.name[:30]!r}")
        if n.ctype == "MenuItem":
            accel = ""
            try:
                accel = str(auto.Control.CreateControlFromElement(n.el).AcceleratorKey or "")
            except Exception:
                pass
            if closing_menu_item(n.name, accel):
                self._never_close(w, f"{verb} [{n.index}] {n.name[:30]!r}")

    def _guard_click(self, w: win.Win, x: int, y: int, count: int = 1, button: str = "left") -> None:
        if not self._is_hers(w.id):
            return
        # her window itself is asked too: whatever covers the point now, it is raised before a real click lands
        if win.close_hit(x, y, count) or win.hit_code(w.id, x, y) in (20, 3):
            self._never_close(w, "that click")
        # a right click on her caption (or its buttons) opens the window menu, where Close lives
        if button != "left" and win.hit_code(w.id, x, y) in (2, 3, 8, 9, 20):
            self._never_close(w, f"a {button} click on the title bar")

    # ---------------------------------------------------------------- one action
    def _do(self, w: win.Win, lk: Look, a: dict) -> tuple[str, bool]:
        check_action(a)
        if "wait" in a:
            s = float(a["wait"])
            time.sleep(s)
            return f"waited {s}s", False

        if "click" in a:
            target, button, count = a["click"], a.get("button", "left"), a.get("count", 1)
            if button not in ("left", "right", "middle") or not 1 <= count <= 3:
                raise HandsError("button left|right|middle, count 1–3")
            if isinstance(target, int) and not isinstance(target, bool):
                n = self._node(lk, target)
                self._guard_node(w, lk, n, "clicking", button)
                self._glide(*n.center, "click")
                note = ""
                if button == "left" and count == 1 and not a.get("real"):
                    how = self._pattern_click(n)  # raises if a pattern exists but fails
                    if how:
                        return f"clicked [{n.index}] {n.name[:40]!r} via {how} (no cursor)", False
                    note = " — it has no clickable pattern, so real input"
                x, y = self._node_pt(lk, w, n)
                self._guard_click(w, x, y, count, button)
            else:
                x, y = self._pt(lk, w, target)
                self._guard_click(w, x, y, count, button)
                self._glide(x, y, "click")
                note = ""
            self._foreground(w, lambda: win.mouse_click(x, y, button, count), point=(x, y))
            return f"{button} click ×{count} at screen {x},{y} (real input, cursor returned){note}", True

        if "set" in a:
            n = self._node(lk, a["set"])
            val = a.get("value", "")
            self._glide(*n.center, "type")
            if n.ctype == "ComboBox":
                # an editable combo (a file dialog's name box) keeps its text in the Edit inside it; setting the
                # combo's own value doesn't reach that edit, so the dialog would save under its old name
                inner = next((m for m in lk.nodes[n.index + 1:n.index + 8]
                              if m.depth == n.depth + 1 and m.ctype == "Edit" and m.props.get("value_ok")
                              and not m.props.get("readonly")), None)
                if inner is not None:
                    n = inner
            c = self._ctl(n)
            if n.props.get("value_ok") and not n.props.get("readonly"):
                c.GetValuePattern().SetValue(val, waitTime=0)
                return f"set [{n.index}] value ({len(val)} chars, no cursor)", False
            if n.props.get("range_ok"):
                c.GetRangeValuePattern().SetValue(float(val), waitTime=0)
                return f"set [{n.index}] range to {val}", False
            raise HandsError(f"[{n.index}] has no settable value; click it and type instead.")

        if "type" in a:
            text = a["type"]
            into = a.get("into")
            if into is not None:
                n = self._node(lk, into)
                self._glide(*n.center, "type")
                hwnd_el = n.props.get("hwnd") or 0
                cls = (n.props.get("cls") or "")
                if hwnd_el and ("Edit" in cls or "RichEdit" in cls):
                    # classic and rich edit controls take text straight in: exact (any script, emoji, newlines), instant,
                    # no keystrokes, no focus
                    import ctypes
                    body = text.replace("\r\n", "\n").replace("\n", "\r\n")
                    ctypes.windll.user32.SendMessageW(hwnd_el, 0x00C2, 1, body)  # EM_REPLACESEL, undoable
                    return f"typed {len(text)} chars into [{n.index}] (background, no focus)", False

                pt = self._node_pt(lk, w, n)

                def go():
                    try:
                        self._ctl(n).SetFocus()
                    except Exception:
                        win.mouse_click(*pt)
                    time.sleep(0.03)
                    win.send_text(text)
                self._foreground(w, go, point=pt)
                return f"typed {len(text)} chars into [{n.index}]", True
            self._foreground(w, lambda: win.send_text(text))
            return f"typed {len(text)} chars at focus", True

        if "key" in a:
            seq = keys.parse(str(a["key"]))
            if keys.closes(str(a["key"])) and self._is_hers(w.id):
                self._never_close(w, f"pressing {a['key']}")
            self._foreground(w, lambda: win.send_keys_raw(seq))
            return f"pressed {a['key']}", True

        if "scroll" in a:
            dy, dx = a.get("dy", 3), a.get("dx", 0)
            tgt = a["scroll"]
            if isinstance(tgt, int) and not isinstance(tgt, bool):
                n = self._node(lk, tgt)
                if n.props.get("scroll"):
                    sp = self._ctl(n).GetScrollPattern()
                    v = 4 if dy > 0 else 1 if dy < 0 else 2  # LargeIncrement / LargeDecrement / NoAmount
                    h = 4 if dx > 0 else 1 if dx < 0 else 2
                    for _ in range(max(1, min(10, abs(dy) or abs(dx)))):
                        sp.Scroll(h, v, waitTime=0)
                    return f"scrolled [{n.index}] dy={dy} dx={dx} (no cursor)", False
                x, y = self._node_pt(lk, w, n)
            else:
                x, y = self._pt(lk, w, tgt)
            self._glide(x, y, "scroll")
            self._foreground(w, lambda: win.mouse_wheel(x, y, dy * 120, dx * 120), point=(x, y))
            return f"wheel dy={dy} dx={dx} at {x},{y}", True

        for verb in ("expand", "collapse", "toggle", "select", "focus"):
            if verb in a:
                n = self._node(lk, a[verb])
                if verb != "focus":
                    self._guard_node(w, lk, n, verb)
                self._glide(*n.center, "click")
                c = self._ctl(n)
                if verb == "expand":
                    c.GetExpandCollapsePattern().Expand(waitTime=0)
                elif verb == "collapse":
                    c.GetExpandCollapsePattern().Collapse(waitTime=0)
                elif verb == "toggle":
                    c.GetTogglePattern().Toggle(waitTime=0)
                elif verb == "select":
                    c.GetSelectionItemPattern().Select(waitTime=0)
                else:
                    c.SetFocus()
                return f"{verb} [{n.index}] {n.name[:40]!r}", verb == "focus"

        if "drag" in a:
            p = a["drag"]
            if not (isinstance(p, (list, tuple)) and len(p) == 4):
                raise HandsError("drag: [x1, y1, x2, y2]")
            x1, y1 = self._pt(lk, w, p[:2])
            x2, y2 = self._pt(lk, w, p[2:])
            self._glide(x1, y1, "drag")
            self._foreground(w, lambda: win.mouse_drag(x1, y1, x2, y2), point=(x1, y1))
            return f"dragged {x1},{y1} → {x2},{y2}", True

        if "activate" in a:
            win.set_foreground(w.id)
            self.her_fg = w.id  # the person asked to watch: don't give focus back
            return "brought the window to the front", False

        raise HandsError(f"Unknown action {list(a)[0]!r}")

    def _pattern_click(self, n: tree.Node) -> str | None:
        p = n.props
        c = self._ctl(n)
        try:
            if p.get("invoke"):
                c.GetInvokePattern().Invoke(waitTime=0)
                return "Invoke"
            if p.get("toggle"):
                c.GetTogglePattern().Toggle(waitTime=0)
                return "Toggle"
            if p.get("select"):
                c.GetSelectionItemPattern().Select(waitTime=0)
                return "Select"
            if p.get("expand"):
                ec = c.GetExpandCollapsePattern()
                (ec.Collapse if int(p.get("xstate") or 0) == 1 else ec.Expand)(waitTime=0)
                return "ExpandCollapse"
            if p.get("legacy") and p.get("default_action"):
                c.GetLegacyIAccessiblePattern().DoDefaultAction(waitTime=0)
                return "DefaultAction"
        except Exception as e:  # the element has a pattern and it failed: never fall through to a blind real click
            raise HandsError(f"[{n.index}] {n.name[:40]!r} didn't respond ({type(e).__name__}); look again") from e
        return None

    # ================================================================ done
    def screen(self) -> tuple[str, list[bytes]]:
        """The whole desktop as the person sees it (every monitor; the overlay never shows in it). It can show anything
        they have open, so it needs their consent once / always, like an app."""
        from .common import gate_consent
        import mss
        from PIL import Image
        gate_consent(self, "screen:all", "your whole screen", "a screenshot of every monitor, as you see it")
        with mss.mss() as s:
            mon = s.monitors[0]  # the virtual desktop spanning every monitor
            g = s.grab(mon)
        img = Image.frombytes("RGB", g.size, g.rgb)
        del g
        titles = "; ".join(f"{w.id} {secretscan.hide(w.title)[0][:40]!r}" for w in win.top_windows()[:12])
        head = (f"screen · {img.width}x{img.height} px across {len(s.monitors) - 1} monitor(s) · origin "
                f"{mon['left']},{mon['top']}")
        # no text tree for a whole screen: its secrets are found by reading the pixels (Windows' own OCR), and the
        # picture is shown only after they are blacked out. If the check fails, nothing from the screen is shown.
        try:
            hidden = self._ocr_redact(img)
        except Exception as e:
            img.close()
            return (head + f"\nno screenshot: withheld: the secret check failed ({type(e).__name__}); nothing from the "
                           f"screen was shown\nwindows (front first): {titles}"), []
        data, scale = win.to_jpeg(img)
        img.close()
        head += (f"{f' · image scale {scale:.3f}' if scale != 1 else ''}\n"
                 + (f"secrets: {hidden} hidden (blacked out); never guess what is under them\n" if hidden else "")
                 + f"windows (front first): {titles}")
        return head, [data]

    _ocr = None  # (module, worker, patterns), started on the first whole-screen look and kept warm

    def _ocr_redact(self, img) -> int:
        """Black out every secret the OCR reads on this picture, in place. Returns how many were hidden."""
        import numpy as np
        from PIL import Image
        if self._ocr is None:
            from . import ocr_redact
            Hands._ocr = (ocr_redact, ocr_redact.OcrWorker(), ocr_redact.load_patterns())
        mod, worker, pats = self._ocr
        rgb = np.array(img)  # a writable copy
        gray = (rgb[..., 0].astype("uint16") * 77 + rgb[..., 1].astype("uint16") * 150
                + rgb[..., 2].astype("uint16") * 29 >> 8).astype("uint8")
        r = mod.analyze(gray, worker, pats, (1, 2), 6.0)
        del gray
        if r.boxes:
            mod.apply_boxes(rgb, r.boxes)
            img.paste(Image.fromarray(rgb))
        del rgb
        return r.count + r.random

    def _close_mine(self, keep: set[int]) -> tuple[list[str], list[str]]:
        """Close the windows I launched. One that answers with a save prompt has unsaved changes: I cancel the
        prompt and leave it open (never throw text away silently) and say so."""
        closed, left = [], []
        for hwnd, title in list(self.launched.items()):
            if hwnd in keep or not win32gui.IsWindow(hwnd):
                self.launched.pop(hwnd, None)
                continue
            title = win.title_of(hwnd) or title
            try:
                auto.ControlFromHandle(hwnd).GetWindowPattern().Close(waitTime=0)
            except Exception:
                win32gui.PostMessage(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            for _ in range(15):
                time.sleep(0.1)
                if not win32gui.IsWindow(hwnd) or win.popups_of(hwnd):
                    break
            time.sleep(0.3)
            if not win32gui.IsWindow(hwnd):
                closed.append(title)
                self.launched.pop(hwnd, None)
                continue
            # still here = it asked to save: cancel that prompt (never discard), keep the window and its text.
            # Language-proof ids: a modern in-window dialog's Cancel is "CloseButton"; a classic dialog's is "2".
            for h in [hwnd, *win.popups_of(hwnd)]:
                try:
                    root = auto.ControlFromHandle(h)
                    for aid in ("CloseButton", "2"):
                        b = root.ButtonControl(searchDepth=8, AutomationId=aid)
                        if b.Exists(0.3, 0):
                            b.GetInvokePattern().Invoke(waitTime=0.3)
                            break
                except Exception:
                    pass
            left.append(title)
        return closed, left

    def done(self, keep: list[int] | None = None) -> str:
        closed, left = self._close_mine(set(keep or []))
        self.looks.clear()
        if self.ov.alive:
            self.ov.idle()
        msg = "released: overlay off, nothing of the agent's left on the screen"
        if closed:
            msg += f"; closed the windows I opened: {closed}"
        if left:
            msg += f"; LEFT OPEN (unsaved changes, I didn't throw them away): {left}"
        return msg
