"""Hands MCP door: computer use on Windows for an agent, beside the person rather than instead of them.

All engine work runs on ONE warm worker thread (COM/UIA initialised once): no cold starts.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Literal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP, Image  # noqa: E402

LOG = Path(__file__).resolve().parent / "state" / "log.jsonl"
_state: dict = {}


def _init():
    import uiautomation as auto
    _state["com"] = auto.UIAutomationInitializerInThread()
    from engine import session
    _state["hands"] = session.Hands(overlay=os.environ.get("HANDS_HEADLESS") != "1")
    if _state["hands"].use_overlay:
        _state["hands"].ov.ensure()  # warm: the first launch never pays the overlay's spin-up
    from engine import browser
    _state["browser"] = browser.Browser(_state["hands"])


POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1, initializer=_init, thread_name_prefix="hands")


def _safe(x):
    """Every string leaving the door is valid UTF-8: broken UTF-16 from an app (a lone half of an emoji in a window
    title) would otherwise fail the response's encoding and leave the caller waiting forever."""
    if isinstance(x, str):
        return x.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    if isinstance(x, tuple):
        return tuple(_safe(v) for v in x)
    if isinstance(x, list):
        return [_safe(v) for v in x]
    if isinstance(x, dict):
        return {_safe(k): _safe(v) for k, v in x.items()}
    return x


_BOUNCER: dict = {}


def _bouncer():
    """The bouncer, as this server's exact self (re-read each call: the session id can arrive after startup)."""
    from engine import bouncer
    me = bouncer.me_from_env()
    b = _BOUNCER.get("b")
    if b is None or b.me != me:
        b = _BOUNCER["b"] = bouncer.Bouncer(me)
    return b


async def _gate(purpose: str) -> None:
    """One holder of the hands at a time, across every door: take or renew the lease, or say who has it."""
    ok, why = await asyncio.to_thread(lambda: _bouncer().acquire(purpose))
    if not ok:
        from engine.common import HandsError
        raise HandsError(why)


async def _run(fn, *a, free: bool = False, purpose: str = "", **k):
    if not free:
        await _gate(purpose or sys._getframe(1).f_code.co_name)
    return _safe(await asyncio.get_running_loop().run_in_executor(POOL, lambda: fn(_state["hands"], *a, **k)))


async def _runb(fn, *a, free: bool = False, purpose: str = "", **k):
    if not free:
        await _gate(purpose or sys._getframe(1).f_code.co_name)
    return _safe(await asyncio.get_running_loop().run_in_executor(POOL, lambda: fn(_state["browser"], *a, **k)))


_SECRET_KEYS = {"type", "value", "set", "text", "find", "key"}  # what I typed, set, searched or pressed: length only
_SPECIAL = ("http", "https", "ws", "wss", "ftp", "file")  # WHATWG special schemes: Chrome reads \ as /
_URLISH = re.compile(r"(?i)\b(?:https?|wss?|ftp|file):[^\s'\"<>]+")  # Chrome also reads "http:u:p@h" (no slashes)


def _clean_url(u) -> str:
    """A URL as Chrome would read it, rebuilt from its parsed parts: scheme, host, port and path only (no userinfo,
    query, fragment, path parameters or escaped ?/#). Any other scheme: just its name and length."""
    from urllib.parse import unquote, urlsplit, urlunsplit
    if not isinstance(u, str):
        return f"<redacted {type(u).__name__}>"
    s = re.sub(r"[\t\r\n]", "", u).strip()
    m = re.match(r"([a-zA-Z][a-zA-Z0-9+.-]*):", s)
    if not m:
        return f"<unparseable url: {len(s)} chars>"
    scheme = m.group(1).lower()
    if scheme not in _SPECIAL:
        return f"<{scheme}: {len(s)} chars>"
    s = s.replace("\\", "/")
    try:
        p = urlsplit(s)
        host = p.hostname or ""
        port = f":{p.port}" if p.port else ""
    except ValueError:
        return "<unparseable url>"
    if scheme != "file" and not host:
        return "<unparseable url>"
    if "[" in host or ":" in host:
        host = f"[{host}]"
    path = p.path.split(";", 1)[0]
    cut = re.split(r"[?#]", unquote(path), maxsplit=1)[0]
    return urlunsplit((scheme, host + port, cut, "", ""))


def _redact(x, key: str = ""):
    """The audit log keeps what I did, never what I typed, and never a URL's query or fragment."""
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            if k in _SECRET_KEYS:
                out[k] = f"<{len(v)} chars>" if isinstance(v, str) else f"<redacted {type(v).__name__}>"
            else:
                out[k] = _redact(v, k)
        return out
    if isinstance(x, list):
        return [_redact(v, key) for v in x]
    if key in ("url", "goto", "href"):
        return _clean_url(x)
    return x


def _scrub(note: str) -> str:
    """URLs inside free text (error notes) lose their query and fragment too."""
    return _URLISH.sub(lambda m: _clean_url(m.group(0)), note or "")


def _log(tool: str, args: dict, ok: bool, ms: int, note: str = "") -> None:
    try:
        LOG.parent.mkdir(exist_ok=True)
        args = _redact(args)
        note = _scrub(note)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "tool": tool, "args": args, "ok": ok,
                                "ms": ms, "note": note[:300]}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _images(imgs: list[bytes]) -> list:
    return [Image(data=b, format="jpeg") for b in imgs]


mcp = FastMCP("hands", instructions=(
    "Hands: computer use on Windows, beside the person. Background first: clicks on an element index go through UI "
    "Automation and never touch the person's cursor or focus. Real mouse/keyboard only when unavoidable, and then "
    "only after their hands have rested; their cursor and window are given back. Loop: hands_windows → hands_look → "
    "hands_act (a batch; the fresh look comes back in the same call) → verify → hands_done. For web pages use the "
    "browser tools (browser_open → browser_look → browser_act, background tabs in Chrome, their focus untouched); "
    "for files prefer file tools. One agent session holds the hands at a time: a 'Hold up' refusal means wait for "
    "the nudge (hands_who shows the line)."))


@mcp.tool()
async def hands_windows(query: str = "", installed: bool = False) -> str:
    """List open windows (id, app, exe, title, minimized, size, foreground), front to back.
    query filters by title/app/exe. installed=true also searches installed apps (name + launch id) for hands_launch."""
    t = time.perf_counter()
    res = await _run(lambda h: {"windows": h.windows(query or None),
                                **({"installed": h.apps(query or None)[:15]} if installed else {})}, free=True)
    _log("windows", {"query": query}, True, round((time.perf_counter() - t) * 1000))
    return json.dumps(res, ensure_ascii=False, indent=1)


@mcp.tool()
async def hands_launch(app: str, watch: bool = False) -> str:
    """Open an app and return its NEW window. app = a name ("calc", "notepad", "paint", names in any language work),
    an AUMID (Publisher.App_hash!App) or an .exe path. watch=false keeps the person's own window in front."""
    t = time.perf_counter()
    try:
        res = await _run(lambda h: h.launch(app, watch=watch))
        _log("launch", {"app": app}, True, round((time.perf_counter() - t) * 1000))
        return json.dumps(res, ensure_ascii=False)
    except Exception as e:
        _log("launch", {"app": app}, False, round((time.perf_counter() - t) * 1000), str(e))
        raise


@mcp.tool()
async def hands_look(window: int, screenshot: bool = True, tree: bool = True, find: str = "",
                     tree_cap: int = 12000) -> list:
    """See a window (works while it's covered by other windows). Returns a screenshot and an indexed element
    tree: `[index] role "name" #automationId value=… state (actions) @x,y`. Indexes and @x,y are valid only
    until the next look; @x,y and click points are screenshot pixels. find="text" filters the tree.
    Popups/menus of the window come as extra images and extra elements."""
    t = time.perf_counter()
    try:
        text, imgs = await _run(lambda h: h.look(window, shot=screenshot, show_tree=tree, find=find or None,
                                                 tree_cap=max(1000, min(60000, tree_cap))))
        _log("look", {"window": window, "find": find}, True, round((time.perf_counter() - t) * 1000))
        return [text, *_images(imgs)]
    except Exception as e:
        _log("look", {"window": window}, False, round((time.perf_counter() - t) * 1000), str(e))
        raise


@mcp.tool()
async def hands_act(window: int, look: int, actions: list[dict],
                    then: Literal["both", "tree", "shot", "none"] = "both") -> list:
    """Do a batch of actions in a window, in order, then look again (then = both|tree|shot|none).
    look = the id of the latest look of this window (its header says `look N`); an older id is refused,
    because indexes and points belong to the look that produced them. The closing look has a new id.
    Actions (indexes from that look; points [x,y] in screenshot pixels):
      {"click": 12} — background via UI Automation (no cursor) | {"click": [x,y]} — real click, polite
      {"click": 12, "button": "right"|"middle", "count": 2} — real click on the element's center
      {"set": 12, "value": "text"} — replace a field's value in the background (it doesn't move keyboard focus).
        A file dialog's name box (Save As / Open) ignores a background set: name it as a person does,
        [{"focus": 12}, {"key": "ctrl+a"}, {"type": "C:\\path\\name.txt"}, {"key": "Return"}], then verify on disk
      {"type": "text", "into": 12} — type at that field (background for classic edits) | {"type": "text"} — at focus
      {"key": "ctrl+s"} — also Return, Tab, Escape, alt+F4, Control_L+a, F5, page_down (never the Windows key)
      {"scroll": 12, "dy": 3} | {"scroll": [x,y], "dy": -5, "dx": 0} — dy>0 is down, in notches
      {"expand": 12} {"collapse": 12} {"toggle": 12} {"select": 12} {"focus": 12}
      {"drag": [x1,y1,x2,y2]} | {"wait": 0.5} (≤5 s) | {"activate": true} — bring it forward for the person to watch
    Real input waits until the person's hands rest (≥0.7 s), returns their cursor at once and gives their window
    back. If they move during real input, the batch stops. Esc Esc / Stop on the pill stops the hands."""
    t = time.perf_counter()
    if not isinstance(actions, list) or not actions or len(actions) > 60:
        raise ValueError("actions: a list of 1–60 action objects")
    try:
        text, imgs = await _run(lambda h: h.act(window, actions, then=then, look_id=look))
        _log("act", {"window": window, "actions": actions}, True, round((time.perf_counter() - t) * 1000),
             text.split("--- after ---")[0])
        return [text, *_images(imgs)]
    except Exception as e:
        _log("act", {"window": window, "actions": actions}, False, round((time.perf_counter() - t) * 1000), str(e))
        raise


@mcp.tool()
async def hands_screen() -> list:
    """See the WHOLE desktop as the person sees it: one screenshot across every monitor (the overlay never appears in
    it), plus the open windows front to back. It can show anything they have open, so it needs their consent.
    To act, pick a window and use hands_look / hands_act on it."""
    t = time.perf_counter()
    try:
        text, imgs = await _run(lambda h: h.screen())
        _log("screen", {}, True, round((time.perf_counter() - t) * 1000))
        return [text, *_images(imgs)]
    except Exception as e:
        _log("screen", {}, False, round((time.perf_counter() - t) * 1000), str(e))
        raise


@mcp.tool()
async def hands_claude_app(action: str, dry_run: bool = False) -> str:
    """The ONE narrow door into the Claude desktop app (which is otherwise hard-denied): bring a session back online
    when an update knocked Remote Control out. action = "remote_control_on" (turns the Remote Control switch of the
    session in view ON, never off) or "new_session" (presses New). Nothing else in that app, ever. Every real use
    texts the owner; with no notify hook configured the door stays shut. dry_run = find the control, touch nothing."""
    from engine import claude_door
    t = time.perf_counter()
    try:
        res = await _run(lambda h: claude_door.run(action, dry_run))
        _log("claude_app", {"action": action, "dry_run": dry_run}, True, round((time.perf_counter() - t) * 1000))
        return res
    except Exception as e:
        _log("claude_app", {"action": action, "dry_run": dry_run}, False, round((time.perf_counter() - t) * 1000), str(e))
        raise


@mcp.tool()
async def hands_done(keep_windows: list[int] | None = None) -> str:
    """Finish and clean up after myself: the app windows I opened with hands_launch close (keep_windows = ids to
    leave open for the person, e.g. a document they should see; a window with unsaved changes is never discarded: its save
    prompt is cancelled and it's reported as left open), the Chrome tabs I opened close (a tab I marked
    {"keep": true} stays and becomes theirs), and the border, pill and cursor leave the screen.
    Call when the task's screen work is over: it also hands the hands back (the bouncer nudges whoever is waiting)."""
    res = await _run(lambda h: h.done(keep_windows), free=True)
    b = await _runb(lambda br: br.done(), free=True)
    if b:
        res += ("; Chrome: " + (b["error"] if "error" in b else
                                f"closed my tabs {b.get('closed', [])}, left open for the person {b.get('kept', [])}"))
    if await asyncio.to_thread(lambda: _bouncer().release()):
        res += "; handed the hands back"
    _log("done", {}, True, 0, json.dumps(b or {}))
    return res


@mcp.tool()
async def hands_who() -> str:
    """Who holds the hands right now (house + exact session id, since, last move), who waits in line, and whether a
    place is being held for someone. Never needs the lease."""
    st = await asyncio.to_thread(lambda: _bouncer().status())
    me = _bouncer().me
    return json.dumps({"me": {"house": me.house, "id": me.id}, **st}, ensure_ascii=False, indent=1)


# ================================================================ Chrome (slice 2)
async def _browser_call(tool: str, args: dict, fn):
    t = time.perf_counter()
    try:
        res = await _runb(fn, free=(tool == "browser_tabs"), purpose=tool)
        _log(tool, args, True, round((time.perf_counter() - t) * 1000))
        return res
    except Exception as e:
        _log(tool, args, False, round((time.perf_counter() - t) * 1000), str(e))
        raise


@mcp.tool()
async def browser_tabs() -> str:
    """List the tabs of the person's Chrome (through the Hands extension): id, title, url, active, mine, session.
    Listing never attaches to anything; I only ever drive tabs I opened (mine=true)."""
    res = await _browser_call("browser_tabs", {}, lambda b: b.tabs())
    return json.dumps(res, ensure_ascii=False, indent=1)


@mcp.tool()
async def browser_open(url: str, session: str = "", watch: bool = False) -> str:
    """Open an http(s) page in a NEW BACKGROUND tab in the agent's own tab group: the person's active tab, mouse
    and keyboard stay theirs. watch=true opens it in front for them to watch. First use of a site asks them (consent
    card); browser-internal pages and password managers are refused."""
    res = await _browser_call("browser_open", {"url": url, "session": session},
                              lambda b: b.open(url, session=session, watch=watch))
    return json.dumps(res, ensure_ascii=False)


@mcp.tool()
async def browser_look(tab: int, screenshot: bool = True, tree: bool = True, find: str = "", diff: bool = True,
                       tree_cap: int = 12000) -> list:
    """See one of my tabs (it stays in the background): a JPEG screenshot and the accessibility tree as
    `[ref] role "name" value=… state`, pruned. The header says `look N`; refs belong to that look.
    diff=true (default): after the first look of a page, only what changed (+ added, - removed, ~ changed).
    find="text" filters lines. Frames I may not touch show as one line `[frame denied: host]` /
    `[frame needs consent: host]`, unread. A fresh look is also how the agent resumes after the person stopped it."""
    return await _browser_call("browser_look", {"tab": tab, "find": find}, lambda b: (
        lambda text_imgs: [text_imgs[0], *_images(text_imgs[1])])(
        b.look(tab, screenshot=screenshot, tree=tree, find=find or None, diff=diff, tree_cap=tree_cap)))


@mcp.tool()
async def browser_act(tab: int, look: int, actions: list[dict],
                      then: Literal["both", "tree", "shot", "none"] = "both") -> list:
    """Do a batch of actions in one of my background tabs, in order, then look again (diffed) in the same call
    (then = both|tree|shot|none). look = the latest look id of this tab; an older id is refused. All input goes
    into the tab through the debugger: the person's OS cursor, keyboard and active tab are never touched. The agent's cursor
    lands on each target first. A refusal or a stop ends the batch and says so.
      {"click": ref} | {"click": ref, "button": "right", "count": 2}
      {"type": "text", "into": ref} | {"type": "text"} — at the focused field (any language)
      {"set": ref, "value": "text"} — replace the value; fires input + change
      {"select": ref, "value": "option value or text"}
      {"key": "Enter"} | {"key": "ctrl+a", "into": ref} — Tab, Escape, Backspace, arrows, F1–F12 (never meta/Windows)
      {"scroll": ref|null, "dy": 3, "dx": 0} — notches, dy>0 is down
      {"goto": "https://…"} | {"back": true} | {"wait": 0.5} (≤5 s)
      {"keep": true} — this tab stays open (handed to the person) at hands_done"""
    if not isinstance(actions, list) or not actions or len(actions) > 60:
        raise ValueError("actions: a list of 1–60 action objects")
    return await _browser_call("browser_act", {"tab": tab, "actions": actions}, lambda b: (
        lambda text_imgs: [text_imgs[0], *_images(text_imgs[1])])(b.act(tab, actions, look_id=look, then=then)))


@mcp.tool()
async def browser_close(tab: int) -> str:
    """Close one of the agent's tabs now (never one of the person's)."""
    res = await _browser_call("browser_close", {"tab": tab}, lambda b: b.close(tab))
    return json.dumps(res, ensure_ascii=False)


def _sweeper() -> None:
    """Every 15 s: silence frees the hands even when nobody calls (the line gets nudged), and the person's Stop frees them
    if I'm the one holding."""
    import threading

    def her_stop_check():
        h = _state.get("hands")
        if not h:
            return
        h._poll_stops()
        if h.stopped and _bouncer()._is_me(_bouncer().status().get("holder")):
            _bouncer().stopped()

    def loop():
        while True:
            time.sleep(15)
            try:
                _bouncer().sweep()
                POOL.submit(her_stop_check)
            except Exception:
                pass
    threading.Thread(target=loop, name="bouncer-sweep", daemon=True).start()


if __name__ == "__main__":
    _sweeper()
    try:
        mcp.run()
    finally:
        try:  # leaving: the hands go back first, so whoever waits is nudged at once
            _bouncer().release("its server closed")
        except Exception:
            pass
        # leaving: my Chrome tabs close (kept ones are handed to the person, debugger off), then my overlay goes
        try:
            POOL.submit(lambda: _state["browser"].done()).result(12)
        except Exception:
            pass
        try:  # the server leaving also cleans up the app windows I opened
            POOL.submit(lambda: _state["hands"].done()).result(8)
        except Exception:
            pass
        try:
            POOL.submit(lambda: _state["hands"].ov.close()).result(3)
        except Exception:
            pass
