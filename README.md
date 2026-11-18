# Hands

**Computer use for AI agents on Windows that works beside you, not instead of you.**
Your agent drives desktop apps and Chrome in the background while you keep your mouse, your keyboard and your
window. You can always see what it is doing, and one keypress stops it.

> ## ⚠️ Read this before installing
> Hands lets an AI agent operate the apps on your computer and, with the Chrome extension, the web pages it opens.
> The extension uses Chrome's **debugger** permission, which gives it **full control of the tabs it opens**
> (it never attaches to your own tabs, but the permission itself is powerful).
> - Install it only on a machine you control, for an agent you trust.
> - Review the [security model](#security-model) and the deny list before first use.
> - Do not leave sensitive work (banking, email, admin consoles) open and unattended while an agent holds the hands.
> - Hands is provided **as is, without warranty of any kind** (see [LICENSE](LICENSE)). You are responsible for
>   what you allow an agent to do on your computer.

## What it does
- **Sees any window, even a covered one**: a screenshot (full window content, JPEG ≤1568 px) plus an indexed
  accessibility tree from one cached UI Automation round trip (typically 25–80 ms).
- **Acts in the background first**: clicking an element goes through UI Automation patterns (Invoke, Toggle,
  Select, Expand, default action), values through ValuePattern, typing into classic edit boxes without focus. Your
  cursor and focus are not touched.
- **Real input only when unavoidable, politely**: it waits until your hands have rested ≥0.7 s, acts, returns your
  cursor at once and gives your window back. If you move during it, the batch stops.
- **Always visible**: a soft glow round every screen whenever the agent works, in any window or none (a background
  Chrome tab or a whole-screen look lights it too; it goes off when the agent is done), a brighter border and a pill
  ("<name> is working here · Esc Esc to stop · Stop") round the window it is in, and its own cursor that travels to
  each target before the action lands. None of it appears in screenshots (WDA_EXCLUDEFROMCAPTURE). The name,
  initial and colours are [configurable](#configuration).
- **Asks first**: the first use of each app or site shows a card (No / Allow once / Always).
- **Cleans up**: windows and tabs the agent opened close when it is done; anything with unsaved changes is left open
  and reported, never discarded. A window that holds your documents is never treated as the agent's.
- **Hides secrets from the agent**: in every look, text that matches a secret (database connection strings with a
  password, API keys and tokens from the common providers, JWTs, bearer tokens, private keys, labelled passwords)
  is replaced with `[secret hidden]` and its element is blacked out on the screenshot, in Chrome (using the page's
  own text) and on the desktop (using each window's own accessibility text). The whole-screen look has no text to go
  by, so it reads the pixels with Windows' built-in OCR first; if that check fails, the picture is withheld. Titles
  and URLs are scrubbed too. The
  header says how many were hidden. The pattern list is `engine/secret_patterns.json`. It catches text the page or
  window exposes as text; a secret drawn as an image, on a canvas, or inside a frame Hands can't read is not caught.
- **Never closes your windows**: once you allow an app, the agent can look and act in your own windows of it, but
  closing them is refused: closing keys (alt+F4, ctrl+F4, ctrl+W, ctrl+Q, alt+space), the title bar's Close button
  (found by asking the window itself, so it works in any language and even when something covers it), a right click
  on the title bar, the window menu, and menu items that close (by their shortcut, or named Exit / Quit / Close).
  Windows the agent opened keep its full hands. This guards the agent's own actions; it is not a sandbox around
  the app.
- **One agent at a time**: several agent sessions can each run Hands; a lease (the "bouncer") lets one hold the
  hands, tells the others exactly who holds them, keeps a line, and nudges the next one when they free.
- **Chrome in background tabs**: the agent opens its own tabs, in its own tab group, and drives them through the
  debugger with focus emulation; your active tab, mouse and keyboard stay yours.

Windows 10/11 only. Built clean-room after studying how other agents' computer use behaves; it uses no code or
binaries from any other computer-use product.

## Install
Requirements: Windows 10 or 11, Python 3.11+, Google Chrome (for the browser part), and the .NET Framework 4.x
compiler that ships with Windows (for the overlay).

```bat
git clone <this repository> hands
cd hands
pip install -r requirements.txt
overlay\build.cmd
```

Register the MCP server with your agent host. For Claude Code:

```bat
claude mcp add hands -- python -X utf8 C:\path\to\hands\mcp_server.py
```

Any MCP client that speaks stdio works the same way: command `python`, arguments `-X utf8 <repo>\mcp_server.py`.

### Chrome (optional)
1. `python -X utf8 chrome\install_host.py` registers the native messaging host for the current user
   (`--status` shows it, `--uninstall` removes it).
2. In Chrome: `chrome://extensions` → Developer mode → **Load unpacked** → the `chrome\extension` folder. The id is
   pinned by the manifest's key, so it matches what the host allows.
3. Restart the MCP server. The host and the server must run from the same folder (they share `state\chrome.key`).

### Electron apps
Some Electron and Chromium apps only expose their accessibility tree while Windows' screen-reader flag is on. That
flag is system-wide, so Hands leaves it alone unless you opt in with `"screen_reader_flag": true` in
`state\config.json`; it is then set each time Hands starts (apps read it when they launch). It resets at sign-out.

## Use
The loop an agent follows:
1. `hands_windows` (list) or `hands_launch` (open an app; returns its new window)
2. `hands_look(window)`: screenshot + tree, e.g. `[12] button "Save" #saveButton (invoke) @410,288`
3. `hands_act(window, look, actions)`: a batch of actions, then a fresh look in the same call
4. verify the outcome
5. `hands_done`: closes what the agent opened, hands the hands back

Browser: `browser_tabs`, `browser_open`, `browser_look`, `browser_act`, `browser_close`. Whole screen:
`hands_screen`. Who holds the hands: `hands_who`.

The full guide for agents (every action, consent, cleanup, the bouncer, troubleshooting) is
[skills/hands/SKILL.md](skills/hands/SKILL.md). Drop it into your agent's skills so it loads before the tools are
used.

## Configuration
Everything is optional. `state\config.json`:

```json
{
  "identity": {
    "name": "Nova",
    "initial": "N",
    "color": "#00A88F",
    "color_hot": "#5CFFE0",
    "session": "nova"
  },
  "screen_reader_flag": false,
  "notify_cmd": ["C:\\Python311\\python.exe", "C:\\path\\to\\notify.py"],
  "nudge_cmd": ["C:\\Python311\\python.exe", "C:\\path\\to\\nudge.py"]
}
```

- `identity`: how the agent looks and is named on screen. Invalid values are ignored. `chrome_host` and
  `chrome_pipe` can be changed too, but the host name must then also be changed in `chrome\extension\sw.js`.
- `notify_cmd`: run with one message argument whenever the host-app door (below) is used. With none configured,
  that door stays shut.
- `grant_scope`: `"session"` (default) or `"agent"`. When the person lets the agent join or reclaim a tab, that
  yes belongs to the asking session; with `"agent"` it covers every session of this agent on this machine.
- `nudge_cmd`: run with one JSON argument `{"house", "id", "message"}` when the hands free for a waiting session.
  Route it to that session however your agent host receives messages.
- Both commands must start with the full path of an existing `.exe` that is not a shell (`cmd.exe`, PowerShell,
  script hosts are refused) and a `.bat` / `.cmd` never runs: the nudge's argument carries text another session
  chose, so it must arrive at your program as one plain argument. Treat it as data in your script too.
- Environment: `HANDS_HOUSE` (which kind of host, e.g. `cc`) and `HANDS_SESSION` (that host's session or thread id)
  name the session exactly for the bouncer. Without them a session is named by its parent process id.

## The bouncer
Every screen-touching tool takes or renews a lease shared by all Hands servers on the machine (a named mutex and
`state\bouncer.json`). A refused call reads like:

> Hold up: CC session 7f3a… is holding the hands since 21:25 (last move 4 s ago, hands_act). You're #1 in line;
> I'll nudge you the moment they're free (a silent holder lets go after 3 min).

The lease frees on `hands_done`, when the holder's server exits or its process dies, after 3 minutes of silence, or
when you press Stop (which nudges nobody). The first in line is nudged by its exact id and its place is held for
60 s. Listing tools and `hands_who` never need the lease.

## Security model
- **Background first, visible always, stoppable always.** Esc Esc, the pill's Stop, or Cancel on Chrome's debugging
  bar stops both desktop and browser; the agent resumes only by looking again.
- **Consent per app and per site**, stored in `state\consent.json` ("Always") or kept for the session ("Once").
- **Hard deny list** (no consent can open it): terminals and shells, the Run dialog, UAC / credential / lock
  screens, Windows Security, password managers (by host in the browser), registry and management consoles, other
  agent hosts, and browser-internal pages. The Windows key is never pressed.
- **Host-app door**: the one exception to the agent host being denied is a narrow, optional door
  (`hands_claude_app`) that can only turn a session's Remote Control switch on or press New, and reports every use
  through `notify_cmd`.
- **Chrome**: the agent drives only tabs it opened (a tab your page opens is never its), the page and every frame
  pass the deny list and consent, frames it may not read are painted over in screenshots (or the screenshot is
  withheld), only trusted presses confirm a click, and a tab that lands on a denied page is blanked or closed.
- **Local only**: the extension talks to a native host, the host to the engine over a named pipe with a shared
  key challenge both ways (`state\chrome.key`, never printed or logged). No TCP port is opened.
- Never type passwords, payment details or identity numbers through Hands; sign-ins are yours to do.

## Layout
| path | what |
|---|---|
| `mcp_server.py` | the MCP server (stdio): every tool, the bouncer gate, cleanup on exit |
| `engine\session.py` | desktop: windows, apps, launch, look, act, done, ownership |
| `engine\tree.py` | cached UI Automation subtree → indexed text |
| `engine\win.py` | windows, identity (exe / AUMID), capture, foreground, tagged input |
| `engine\keys.py` | key chords |
| `engine\policy.py` | deny list, consent, URL gate |
| `engine\common.py` | shared errors, the consent gate, stops, action checks |
| `engine\bouncer.py` | the lease, the line, nudges |
| `engine\identity.py` | the agent's name, look and Chrome wiring |
| `engine\claude_door.py` | the optional host-app door |
| `engine\browser.py` | Chrome: pipe client, per-frame policy and consent, refs, diffs, actions |
| `engine\chrome_key.py` | extension id, pipe name, the shared key file |
| `engine\overlay_link.py` | the line to the overlay |
| `overlay\Overlay.cs` | the overlay (glow, border, pill, cursor, consent card); `overlay\build.cmd` builds it |
| `chrome\extension\` | the MV3 extension: `sw.js` drives the agent's tabs through `chrome.debugger`; `overlay.js` only draws |
| `chrome\host.py`, `chrome\host.cmd` | the native messaging host (Chrome stdio ↔ named pipe) |
| `chrome\install_host.py` | registers the host for the current user |
| `skills\hands\SKILL.md` | the guide for agents |
| `tests\` | the gate |

## Tests
`python -X utf8 tests\run.py` runs every asserting test and exits non-zero on any failure. Desktop tests open their
own Calculator, console and scratch windows; browser tests start their own Chrome with a temporary profile and their
own pipe. They do appear on screen: run them when you are not using the machine. One criterion (real-input fallback
while you are typing) needs a person's hands and is reported SKIPPED-MANUAL.

Test switches only work together with `HANDS_TEST_HOOKS=1`: `HANDS_TEST_ALLOW=<key;key>` lets a test drive what it
opens, `HANDS_PROOF=1` makes the overlay visible to screenshots. Never set them in normal use.

## Security notes

**18 November 2026.** In a whole-screen screenshot, a secret shown in large text (about 26 px letters and up) could reach the agent without being blacked out, about one time in seven in our tests. Fixed in the commit that adds this note. Window and browser looks were not affected: they hide secrets from the exact text, not from pixels. Known limits that remain: a random string of 24 to 32 characters with no label next to it, and the last line of a key that wraps over several lines in large text.

**Same day, a follow-up.** A partly covered string in large text could keep a few pixel rows visible: fixed in this commit. Two lines of large text set close together can be judged as one line, and a secret on one of them shown when the other line is readable: this commit catches the clear cases, not all of them (in our tests the key stayed visible in 1 of 1,440 such cards, before and after).

**Same day, last follow-up.** A string in large text that the check had seen only in part could keep its remaining characters readable in one case (a box kept from the second look). Fixed in this commit.

## License
Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Copyright 2026 Code & Chaos.
