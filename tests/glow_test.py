"""The person always sees when the agent works: the whole-screen glow.
GL1: the overlay's 'awake' lights the glow with NO target window (a background Chrome tab, a whole-screen look).
GL2: idle takes it off.
GL3: every gated tool call lights it (mcp_server._gate -> _glow_on), so browser work shows too; _glow_off clears it.
No window of the person's is touched: the glow is click-through and never in a capture."""
import asyncio, ctypes, os, sys, time
from ctypes import wintypes
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["HANDS_TEST_HOOKS"] = "1"
from engine import session

user32 = ctypes.windll.user32
SM_CXSCREEN, SM_CYSCREEN = 0, 1


def glow_layers(pid: int) -> int:
    """Visible windows of the overlay process that frame a whole monitor (the glow is one per monitor)."""
    found = []
    sw, sh = user32.GetSystemMetrics(SM_CXSCREEN), user32.GetSystemMetrics(SM_CYSCREEN)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(h):
            r = wintypes.RECT()
            user32.GetWindowRect(h, ctypes.byref(r))
            if r.right - r.left >= sw * 0.9 and r.bottom - r.top >= sh * 0.9:
                found.append(h)
        return True
    user32.EnumWindows(cb, 0)
    return len(found)


def wait_for(pred, t=3.0):
    end = time.time() + t
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    return pred()


res = {}
H = session.Hands(overlay=True)
try:
    assert H.ov.ensure(), "overlay didn't start"
    pid = H.ov.proc.pid
    before = glow_layers(pid)
    H.ov.awake()
    on = wait_for(lambda: glow_layers(pid) > 0)
    res["GL1"] = before == 0 and on
    print(f"GL1: {'PASS' if res['GL1'] else 'FAIL'} — glow layers before={before}, after awake (no target)={glow_layers(pid)}")
    H.ov.idle()
    off = wait_for(lambda: glow_layers(pid) == 0)
    res["GL2"] = off
    print(f"GL2: {'PASS' if res['GL2'] else 'FAIL'} — after idle={glow_layers(pid)}")

    import mcp_server
    mcp_server.POOL.submit(lambda: None).result(20)  # the worker's own start-up runs first (it builds its Hands)
    spare = mcp_server._state.get("hands")
    if spare is not None and spare is not H:
        spare.ov.close()
    mcp_server._state["hands"] = H

    class FakeBouncer:  # the lease is granted; only the glow is under test
        def acquire(self, purpose):
            return True, ""
    mcp_server._bouncer = lambda: FakeBouncer()
    asyncio.run(mcp_server._gate("browser_look"))
    lit = wait_for(lambda: glow_layers(pid) > 0)
    mcp_server._glow_off()
    dark = wait_for(lambda: glow_layers(pid) == 0)
    res["GL3"] = lit and dark and "glow" not in mcp_server._state
    print(f"GL3: {'PASS' if res['GL3'] else 'FAIL'} — a gated browser call lit it={lit}; glow_off cleared it={dark}")
finally:
    H.ov.close()
print("SUMMARY " + " ".join(f"{k}:{'PASS' if v else 'FAIL'}" for k, v in res.items()))
sys.exit(0 if len(res) == 3 and all(res.values()) else 1)
