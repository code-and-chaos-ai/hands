"""K8: the person's cursor is saved right before EACH real-input action (not once per batch). Input is stubbed: nothing real moves."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine import session, win
import uiautomation as auto

CALC = "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
H = session.Hands(overlay=False)
H.consent.session.add(CALC)
w = H.launch("calc")
hwnd = w["id"]
ok = False
try:
    H.look(hwnd)
    lk = H.looks[hwnd]
    reads = iter([(100, 100), (900, 700)] + [(5, 5)] * 40)
    put = []
    real = dict(cursor_pos=win.cursor_pos, set_cursor=win.set_cursor, mouse_click=win.mouse_click,
                set_foreground=win.set_foreground, root_at=win.root_at)
    win.cursor_pos = lambda: next(reads)
    win.set_cursor = lambda x, y: put.append((x, y))
    win.mouse_click = lambda *a, **k: None
    win.set_foreground = lambda h: True
    win.root_at = lambda x, y: hwnd
    session.IDLE_NEEDED_MS = 0
    l, t, r, b = lk.rect
    pts = [[(r - l) // 2, (b - t) // 2], [(r - l) // 3, (b - t) // 2]]
    out, _ = H.act(hwnd, [{"click": pts[0]}, {"click": pts[1]}], then="none", look_id=lk.id)
    for k, v in real.items():
        setattr(win, k, v)
    print(out)
    print("restored to:", put)
    ok = len(put) == 2 and put[0] != put[1]
    print("K8", "PASS (saved per action)" if ok else "FAIL")
finally:
    auto.ControlFromHandle(hwnd).GetWindowPattern().Close()
sys.exit(0 if ok else 1)
