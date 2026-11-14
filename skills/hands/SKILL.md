---
name: hands
description: How to use Hands, a Windows computer-use MCP server for AI agents that works BESIDE the person instead of taking over their machine. Desktop apps are driven in the background through UI Automation, Chrome through a companion extension in background tabs, and a lease ("the bouncer") lets only one agent session hold the hands at a time. Load before calling any hands_* or browser_* tool, when a call is refused or stopped, when a "Hold up" refusal or a "[HANDS · the bouncer]" nudge arrives, and when setting Hands up.
---

# Hands

Hands gives an agent its own hands on a Windows PC while the person keeps working. It is an MCP server
(`mcp_server.py`, stdio) plus a small overlay that shows the person what the agent is doing.

> **Read before installing.** Hands can operate the apps on your computer and, with the Chrome extension, the
> pages in your browser. The extension uses Chrome's debugger permission, which gives it full control of the tabs
> it opens. Use it only on a machine and with an agent you trust, review the deny list and consent settings below,
> and never leave sensitive work unattended while an agent holds the hands. It is provided as-is, with no warranty
> (see LICENSE). You are responsible for what you let an agent do.

## Principles
1. **The person works while the agent works.** Background first: element actions go through UI Automation
   (Invoke, Value, Toggle, Expand…) with no cursor movement and no focus change, even on a covered window. Real
   mouse/keyboard input is used only when an app exposes nothing but pixels; Hands waits until the person's hands
   have rested 0.7 s, borrows input for the shortest time, and gives their cursor and window back. If the person
   moves during real input, the batch stops.
2. **The agent is always visible and stoppable.** While working: a soft glow round every screen, a brighter border
   and a pill with a Stop button round the agent's window, and the agent's own cursor. None of this appears in
   screenshots. **Esc Esc** or **Stop** stops desktop and browser at once; the agent resumes only by looking again.
3. **What the agent opens, it closes.** Unsaved work is never thrown away.

## The loop
1. `hands_windows(query?)`: list windows (no lease needed). Or `hands_launch(app)`: open an app by name, AUMID or
   `.exe`; returns its NEW window and says if the window is not the agent's (see Ownership).
2. `hands_look(window)`: a screenshot plus an indexed element tree:
   `[index] role "name" #automationId value=… state (actions) @x,y`. The header says `look N`.
3. `hands_act(window, look=N, actions=[…], then="both"|"tree"|"shot"|"none")`: a batch of actions in order, then
   a fresh look (new id) in the same call. Indexes and points belong to look N only; a stale id is refused.
4. Verify the outcome (read the value back, check the file on disk).
5. `hands_done(keep_windows?)`: always, when the screen work is over.

Browser: `browser_tabs()` (free) · `browser_open(url, session?, watch?)` opens a background tab in the agent's
own tab group · `browser_look(tab)` (accessibility tree as `[ref]`, diffed after the first look) ·
`browser_act(tab, look, actions)` · `browser_close(tab)`. A tab marked `{"keep": true}` is handed to the person
at `hands_done`; all others close.

Working in the same tab as the person:
- `browser_join(tab)`: a tab THEY opened and want you in. They answer a card for that tab (once or no, never
  remembered). Then look and act in it as usual. It stays theirs: you can't close it, their focus never moves, and
  hands_done lets go of it and leaves it open. If it wanders to a page you may not touch, you step out of it.
- `browser_reclaim(tab)`: a tab YOU opened and handed to them earlier (`browser_tabs` shows `reclaimable: true`).
  Card too. Full hands again, closing included; at hands_done it goes back to them unless you closed it on purpose.

Whole screen: `hands_screen()` returns one screenshot of every monitor plus the window list (needs consent).

## Actions
Desktop (`hands_act`): `{"click": i}` (background) · `{"click": [x,y]}` (real) · `{"click": i, "button":
"right", "count": 2}` · `{"set": i, "value": "…"}` · `{"type": "…", "into": i}` · `{"type": "…"}` at focus ·
`{"key": "ctrl+s"}` (the Windows key is never sent) · `{"scroll": i, "dy": 3}` (dy>0 = down, notches) ·
`{"expand"|"collapse"|"toggle"|"select"|"focus": i}` · `{"drag": [x1,y1,x2,y2]}` · `{"wait": 0.5}` (≤5 s) ·
`{"activate": true}` (bring the window forward so the person can watch).

Browser (`browser_act`): `{"click": ref}` · `{"type": "…", "into": ref}` · `{"set": ref, "value": "…"}` ·
`{"select": ref, "value": "…"}` · `{"key": "Enter"}` · `{"scroll": ref|null, "dy": 3}` · `{"goto": "https://…"}` ·
`{"back": true}` · `{"wait": 0.5}` · `{"keep": true}`.

Batch what you can predict: one look, one batch, one verifying look.

## Consent and deny
- The first use of an app or a site shows the person a card: **Once** (this session), **Always** (saved in
  `state/consent.json`), **No**. With no overlay running the call fails with `needs_consent`: ask the person.
- **Hard deny** (no consent can open it): terminals and shells, password managers (matched by host in the
  browser), security and authentication apps, the Run box, browser-internal pages, and the agent host app itself.
- Never type passwords, payment details or identity numbers. Ask the person before anything that sends, posts,
  buys or deletes in their name.

## Ownership and cleanup
- `hands_done` closes every window the agent launched and every tab it opened. A window with unsaved changes is
  never discarded: its save prompt is cancelled and it is reported as **LEFT OPEN**. `keep_windows=[id]` leaves
  chosen windows for the person.
- A window counts as the agent's only if it did not exist before the launch AND holds nothing of the person's.
  Apps that restore sessions (Windows 11 Notepad) can open the person's documents in a brand-new window: such a
  launch returns `mine: false` and a note ("already holds a document / 2+ tabs: it belongs to the person"), and
  the agent must not edit it.

## The bouncer: one agent session at a time
Several agent sessions (for example a coding agent and a chat agent) can each run their own Hands server. A lease
across all of them decides who may touch the screen.
- Every screen tool takes or renews the lease. `hands_windows`, `browser_tabs`, `hands_done` and `hands_who` don't.
- A refused call says exactly who holds the hands, since when, their last action, and your place in line. You are
  now in the line: do not retry in a loop. `hands_who()` shows holder, line and any held place.
- When the hands free, the first in line is **nudged** by its exact id and its place is held for 60 s; take the
  hands with any screen tool, or do nothing and they pass on.
- The lease frees on `hands_done`, when the holder's server exits or its process dies, after 3 minutes of silence,
  and when the person presses Stop (which nudges nobody).

## Setup and configuration
- Requirements: Windows 10/11, Python 3.11+ with `pip install -r requirements.txt`, Chrome for browser work.
- Register the MCP server with your agent host: command `python -X utf8 <repo>/mcp_server.py` (stdio).
- Identity for the bouncer: `HANDS_HOUSE` (e.g. `cc`, `den`) and `HANDS_SESSION` (the session or thread id) in the
  server's environment. Without them a session is named by its parent process id.
- `state/config.json` (optional):
  - `"notify_cmd": ["<exe>", "<script>"]`: run with one message argument whenever the host-app door is used.
  - `"nudge_cmd": ["<exe>", "<script>"]`: run with one JSON argument `{house, id, message}` when the hands free
    for a waiting session; route it to that session however your hosts receive messages.
- Chrome: load the extension from `chrome/extension` (unpacked), then run `python chrome/install_host.py` to
  register the native messaging host for that extension id.
- Some Electron apps expose their accessibility tree only while the system screen-reader flag is on. It is
  system-wide, so it is opt-in: `"screen_reader_flag": true` in `state/config.json`.

## Troubleshooting
- `look N is stale`: look again and use the new id. · `Window … is gone`: `hands_windows`.
- `Stopped`: the person pressed Stop. Explain what you were doing; resume only with a fresh look if still wanted.
- `Chrome isn't connected`: Chrome must be open with the extension loaded and the native host registered.
- A file dialog ignores a background `set` on its name box: name it as a person does,
  `[{"focus": i}, {"key": "ctrl+a"}, {"type": "C:\\path\\name.txt"}, {"key": "Return"}]`, then check the file.
- On a non-English Windows, target elements by `#automationId`, not by visible names.
- Gate: `python -X utf8 tests/run.py` (some tests open windows on the real screen: run them when the machine is
  free).
