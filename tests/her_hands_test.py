"""HH: only REAL movement by a person counts as their hands (the Paint bug, 15 Nov 2026: over Remote Desktop the
client echoed the agent's own cursor moves back as fresh input, and the agent let go every time it moved).

HH1-HH9 drive the overlay's own compiled judge (`overlay.exe --judge`): the exact logic the hooks run.
HH10-HH13 drive the engine's side with stubs: nothing real moves, no window opens.
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from engine import session, win  # noqa: E402

EXE = os.path.join(ROOT, "overlay", "overlay.exe")
SRC = os.path.join(ROOT, "overlay", "Overlay.cs")
if not os.path.exists(EXE) or os.path.getmtime(EXE) < os.path.getmtime(SRC):  # the same build as overlay/build.cmd
    csc = os.path.join(os.environ["WINDIR"], "Microsoft.NET", "Framework64", "v4.0.30319", "csc.exe")
    subprocess.run([csc, "/nologo", "/optimize+", "/target:winexe", "/platform:x64", f"/out:{EXE}",
                    "/r:System.Windows.Forms.dll", "/r:System.Drawing.dll", "/r:System.Web.Extensions.dll", SRC],
                   check=True, capture_output=True)
MOVE, LDOWN, LUP, WHEEL, KDOWN, KUP, SYSKDOWN = "200", "201", "202", "20A", "100", "101", "104"
results: list[tuple[str, bool, str]] = []


def judge(lines: list[str]) -> list[str]:
    """One fresh judge per scenario: its memory (the agent's points, her path) starts empty."""
    p = subprocess.run([EXE, "--judge"], input="\n".join(lines) + "\n", capture_output=True, text=True, timeout=20)
    return p.stdout.split("\n")[: len(lines)]


def mouse(msg, x, y, cx, cy, t, remote=1):
    return f"mouse {msg} {x} {y} {cx} {cy} {t} {remote}"


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"{name}: {'PASS' if ok else 'FAIL'}  {detail}")


# ---------------------------------------------------------------- the overlay's judge
# HH1 Remote Desktop echoes the agent's move back where the cursor already is: zero distance, nobody moved
out = judge([mouse(MOVE, 640, 400, 640, 400, 1000)])
check("HH1 zero-distance echo is not her", out == ["no"], str(out))

# HH2 a drag's earlier points come back late, while the cursor is already further along (and off by a few px when the
# client scales): echoes of the agent's own path, not her
drag = [f"mine {100 + 20 * i} 300 {1000 + 10 * i}" for i in range(18)]
out = judge(drag + [mouse(MOVE, 100, 300, 440, 300, 1250), mouse(MOVE, 163, 297, 440, 300, 1300),
                    mouse(MOVE, 245, 305, 440, 300, 1400)])
check("HH2 late echoes of the agent's drag are not her", out[-3:] == ["no", "no", "no"], str(out[-3:]))

# HH3 the hand-back: the agent returned her cursor to (900,700), then moved on to draw; the echo of the hand-back arrives
out = judge(["mine 900 700 2000", "mine 120 340 2050", mouse(MOVE, 900, 700, 120, 340, 2300)])
check("HH3 echo of the cursor hand-back is not her", out[-1] == "no", str(out[-1]))

# HH4 she really moves her mouse during the agent's drag (her client reports where HER cursor is): she wins at once
out = judge(drag + [mouse(MOVE, 900, 650, 440, 300, 1200)])
check("HH4 her real move during a drag stops the agent", out[-1].startswith("hers mouse moved"), out[-1])

# HH5 a one-pixel bump, then another after a pause: never a hand on the mouse
out = judge([mouse(MOVE, 501, 500, 500, 500, 1000, 0), mouse(MOVE, 502, 500, 501, 500, 1500, 0),
             mouse(MOVE, 503, 501, 502, 500, 2100, 0)])
check("HH5 one-pixel bumps with pauses are not her", out == ["no", "no", "no"], str(out))

# HH6 a slow, steady real move adds up: 2 px every 20 ms counts once it passes 8 px
out = judge([mouse(MOVE, 500 + 2 * (i + 1), 500, 500 + 2 * i, 500, 1000 + 20 * i, 0) for i in range(6)])
check("HH6 a slow steady move adds up to her", out[:3] == ["no", "no", "no"] and out[3].startswith("hers mouse moved"),
      str(out))

# HH7 keys: a press is hers (plain and Alt-held), a key-up alone (a remote client's resync) is not
out = judge([f"key {KDOWN}", f"key {KUP}", f"key {SYSKDOWN}"])
check("HH7 key press counts, key-up alone doesn't", out == ["hers key pressed", "no", "hers key pressed"], str(out))

# HH8 mouse buttons and the wheel: a press and a wheel turn are hers, a button-up alone is not
out = judge([mouse(LDOWN, 5, 5, 5, 5, 1000), mouse(LUP, 5, 5, 5, 5, 1100), mouse(WHEEL, 5, 5, 5, 5, 1200)])
check("HH8 button press and wheel count, button-up alone doesn't", out == ["hers mouse button", "no", "hers mouse wheel"],
      str(out))

# HH9 an echo window doesn't last forever: the agent's point from long ago is hers again if she goes back to it, and
# locally (no Remote Desktop) the window is short
out = judge(["mine 100 100 1000", mouse(MOVE, 100, 100, 600, 600, 4500, 1),
             "mine 100 100 5000", mouse(MOVE, 100, 100, 600, 600, 5700, 0)])
check("HH9 echo memory expires (remote 3 s, local 0.5 s)",
      out[1].startswith("hers mouse moved") and out[3].startswith("hers mouse moved"), str(out))


# ---------------------------------------------------------------- the engine's side
class FakeOv:
    def __init__(self, alive, tick=0, why=""):
        self.alive, self.her_input_tick, self.her_input_why = alive, tick, why


H = session.Hands.__new__(session.Hands)  # no overlay, no tree: just the gate under test
H.injected_until = 0
real = dict(last_input_tick=win.last_input_tick, tick=win.tick, remote_session=win.remote_session)
try:
    # HH10 the overlay is watching and hasn't seen her: 0, even though the system's last input is fresh (an echo)
    H.ov = FakeOv(alive=True, tick=0)
    win.last_input_tick = lambda: 50_000
    H.injected_until = 49_000
    check("HH10 overlay alive and quiet = nobody touched (no fallback)", H._her_hands_tick() == 0, str(H._her_hands_tick()))

    # HH11 headless in a remote session: input within 1.5 s of the agent's own is its echo; later input is hers
    H.ov = FakeOv(alive=False)
    win.remote_session = lambda: True
    win.last_input_tick = lambda: 49_000 + 900
    a = H._her_hands_tick()
    win.last_input_tick = lambda: 49_000 + session.REMOTE_ECHO_GRACE_MS + 50
    b = H._her_hands_tick()
    check("HH11 headless + remote: echo grace", a == 0 and b == 49_000 + session.REMOTE_ECHO_GRACE_MS + 50, f"{a} {b}")

    # HH12 headless on the console: the short grace stays (her hands still stop the agent quickly)
    win.remote_session = lambda: False
    win.last_input_tick = lambda: 49_000 + 200
    check("HH12 headless + local: short grace", H._her_hands_tick() == 49_200, str(H._her_hands_tick()))

    # HH13 the stop says what her hands did, so a wrong stop can be traced
    H.ov = FakeOv(alive=True, tick=60_000, why="mouse moved 42 px")
    check("HH13 the stop names what her hands did", H._her_hands_why() == " (mouse moved 42 px)", H._her_hands_why())
finally:
    for k, v in real.items():
        setattr(win, k, v)

# HH14 the hand-back is announced as the agent's own move (a tagged move) before the exact SetCursorPos
calls = []
real_move, real_u32 = win.mouse_move, win.user32
try:
    win.mouse_move = lambda x, y: calls.append(("tagged", x, y))

    class U32:
        def __getattr__(self, n):
            if n == "SetCursorPos":
                return lambda x, y: calls.append(("set", x, y))
            return getattr(real_u32, n)

    win.user32 = U32()
    win.set_cursor(321, 654)
finally:
    win.mouse_move, win.user32 = real_move, real_u32
check("HH14 cursor hand-back is a tagged move first", calls == [("tagged", 321, 654), ("set", 321, 654)], str(calls))

# HH15: with a non-Latin keyboard layout active (Hebrew, Russian, Greek...) the layout has no 'w', and ctrl+w / ctrl+s
# failed to map. Latin letters and digits in a chord are their keys on any layout. Forced here, whatever layout runs.
import ctypes  # noqa: E402
from engine import keys  # noqa: E402
u32 = ctypes.windll.user32
real_scan = u32.VkKeyScanW
try:
    u32.VkKeyScanW = lambda c: -1  # a layout with no Latin letters at all
    got = [keys._vk(t) for t in ("w", "S", "7")]
    try:
        keys._vk("ש")
        unmapped = "mapped"
    except keys.KeyError_:
        unmapped = "refused"
finally:
    u32.VkKeyScanW = real_scan
check("HH15 Latin chords map on a non-Latin layout", got == [(0x57, False), (0x53, True), (0x37, False)]
      and unmapped == "refused", f"{got} {unmapped}")

ok = all(r[1] for r in results)
print("SUMMARY", "PASS" if ok else "FAIL", f"{sum(r[1] for r in results)}/{len(results)}")
sys.exit(0 if ok else 1)
