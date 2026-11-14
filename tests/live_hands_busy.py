"""Criterion E, live, with the person's real hands, on the tests' OWN scratch app. The scratch window's TITLE tells
them what to do; every phase waits for what their hands actually do, never a clock.
E1: while they keep moving the mouse, a real-input action is refused ("hands are busy"), nothing typed.
E2: once they rest, a long real-input batch starts; the moment they move the mouse it stops ("let go").
E3: the scratch window is closed after (nothing left behind)."""
import os, subprocess, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FX = os.path.join(ROOT, "tests", "fixtures")
EXE = os.path.join(FX, "hands_scratch.exe")
os.environ["HANDS_TEST_HOOKS"] = "1"
os.environ["HANDS_TEST_ALLOW"] = os.path.normcase(EXE)
os.environ["HANDS_SCRATCH_TITLE"] = "Hands test: get ready"
from engine import session, win
import win32gui


def title(t):
    win32gui.SetWindowText(sid, t)


def wait_for(pred, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


def idle_ms():
    return win.tick() - win.last_input_tick()


H = session.Hands(overlay=False)
sid = H.launch(EXE)["id"]
os.environ.pop("HANDS_SCRATCH_TITLE", None)
l, t, r, b = win.frame_rect(sid)
win32gui.SetWindowPos(sid, 0, l, t, 900, 260, 0x0004)  # wider, so the title is easy to read
res = {}
try:
    # E1
    title("Hands test 1 of 2: MOVE YOUR MOUSE IN CIRCLES and keep going until this title changes")
    def steady(seconds=2.0):
        """their hand has been moving without a pause over 300 ms for this long"""
        start = None
        t_end = time.time() + 120
        while time.time() < t_end:
            if idle_ms() < 300:
                start = start or time.time()
                if time.time() - start >= seconds:
                    return True
            else:
                start = None
            time.sleep(0.05)
        return False
    moving = steady()
    H.look(sid, shot=False)
    idle_at = idle_ms()
    out1, _ = H.act(sid, [{"type": "E1 should never appear"}], then="none", look_id=H.looks[sid].id)
    print(f"   E1: your mouse had been still for {idle_at} ms when I tried")
    import uiautomation as auto
    text1 = auto.ControlFromHandle(sid).EditControl(searchDepth=4).GetValuePattern().Value or ""
    res["E1"] = moving and "busy" in out1 and "E1" not in text1
    print(f"E1: {'PASS' if res['E1'] else 'FAIL'} — saw you moving={moving}; act -> {out1.strip()[:90]!r}")

    # E2
    title("Hands test 2 of 2: STOP. Take your hand off the mouse and wait...")
    rested = wait_for(lambda: idle_ms() > 2000, 120)
    title("Hands test 2 of 2: I'm typing now. MOVE YOUR MOUSE to stop me!")
    batch = []
    for i in range(40):
        batch += [{"type": f"{i % 10}"}, {"wait": 0.4}]
    H.look(sid, shot=False)
    t0 = time.time()
    out2, _ = H.act(sid, batch, then="none", look_id=H.looks[sid].id)
    secs = time.time() - t0
    res["E2"] = rested and "let go" in out2 and secs < 30
    print(f"E2: {'PASS' if res['E2'] else 'FAIL'} — you rested={rested}; stopped after {secs:.1f}s: "
          f"{[ln for ln in out2.splitlines() if 'let go' in ln or 'refused' in ln][:1]}")
    title("Hands test: done, thank you! Closing...")
    time.sleep(1.2)
finally:
    H.done()
    if win32gui.IsWindow(sid):  # it may hold the test's own typed digits: close it without saving
        win32gui.PostMessage(sid, 0x0010, 0, 0)
        time.sleep(0.5)
        for p in win.popups_of(sid):
            import uiautomation as auto
            bno = auto.ControlFromHandle(p).ButtonControl(searchDepth=4, AutomationId="7")
            if bno.Exists(0.3, 0):
                bno.GetInvokePattern().Invoke(waitTime=0.2)
        time.sleep(0.5)
res["E3"] = not win32gui.IsWindow(sid)
print(f"E3: {'PASS' if res['E3'] else 'FAIL'} — scratch window closed={not win32gui.IsWindow(sid)}")
print("SUMMARY " + " ".join(f"{k}:{'PASS' if v else 'FAIL'}" for k, v in res.items()))
sys.exit(0 if len(res) == 3 and all(res.values()) else 1)
