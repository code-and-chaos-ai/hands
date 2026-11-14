"""The person's desktop windows: joined automatically, never closed. On the tests' OWN scratch app only.
DJ1: a window Hands didn't open (the test starts it itself) can be looked at and acted in once its app has consent.
DJ2: closing chords on it are refused (alt+F4, ctrl+w, ctrl+q, alt+space); it stays open.
DJ3: its title-bar Close is refused both ways: by element index and by a real click on the button's point.
DJ4: a window Hands launched keeps full hands: its Close by index closes it.
DJ5: the chord table itself (pure): closing chords flagged, ordinary ones not."""
import os, subprocess, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FX = os.path.join(ROOT, "tests", "fixtures")
EXE = os.path.join(FX, "hands_scratch.exe")
os.environ["HANDS_TEST_HOOKS"] = "1"
os.environ["HANDS_TEST_ALLOW"] = os.path.normcase(EXE)
from engine import session, win, keys
import uiautomation as auto
import win32gui

if not os.path.exists(EXE) or os.path.getmtime(EXE) < os.path.getmtime(os.path.join(FX, "scratch.cs")):
    csc = os.path.join(os.environ["WINDIR"], r"Microsoft.NET\Framework64\v4.0.30319\csc.exe")
    subprocess.run([csc, "/nologo", "/optimize+", "/target:winexe", "/platform:x64", f"/out:{EXE}",
                    "/r:System.Windows.Forms.dll", os.path.join(FX, "scratch.cs")], check=True, capture_output=True)


def scratch(title):
    return [w for w in win.top_windows() if os.path.basename(w.exe).lower() == "hands_scratch.exe" and w.title == title]


def force_close(hwnd):
    if not win32gui.IsWindow(hwnd):
        return
    win32gui.PostMessage(hwnd, 0x0010, 0, 0)
    for _ in range(30):
        time.sleep(0.1)
        for p in win.popups_of(hwnd):
            b = auto.ControlFromHandle(p).ButtonControl(searchDepth=4, AutomationId="7")  # IDNO: the text is the test's
            if b.Exists(0.2, 0):
                b.GetInvokePattern().Invoke(waitTime=0.2)
        if not win32gui.IsWindow(hwnd):
            return


def closers(lk, hwnd):
    """The title bar's Close button, found the language-proof way: the point under its center is the close button."""
    return [n for n in lk.nodes if n.ctype == "Button" and win.hit_code(hwnd, *n.center) == 20]


res = {}
for w in [w for w in win.top_windows() if os.path.basename(w.exe).lower() == "hands_scratch.exe"]:
    force_close(w.id)

# the person's window: started by the test, not by Hands
proc = subprocess.Popen([EXE], env=dict(os.environ, HANDS_SCRATCH_TITLE="Her scratch DJ"))
her = None
for _ in range(80):
    time.sleep(0.1)
    found = scratch("Her scratch DJ")
    if found:
        her = found[0].id
        break
H = session.Hands(overlay=False)
Hands_in_title = session.Hands._in_title_bar
try:
    # DJ1: joined automatically: look and act work
    H.look(her, shot=False)
    lk = H.looks[her]
    ed = [n for n in lk.nodes if n.ctype in ("Edit", "Document")][0]
    out1, _ = H.act(her, [{"set": ed.index, "value": "joined"}], then="none", look_id=lk.id)
    val = auto.ControlFromHandle(her).EditControl(searchDepth=4).GetValuePattern().Value
    res["DJ1"] = H._is_hers(her) and "refused" not in out1 and val == "joined"
    print(f"DJ1: {'PASS' if res['DJ1'] else 'FAIL'} — hers={H._is_hers(her)}; act -> {out1.strip()[:70]!r}; text={val!r}")

    # DJ2: closing chords refused before anything is pressed
    outs = []
    for chord in ("alt+F4", "ctrl+w", "ctrl+q", "alt+space"):
        H.look(her, shot=False)
        o, _ = H.act(her, [{"key": chord}], then="none", look_id=H.looks[her].id)
        outs.append(o)
    res["DJ2"] = all("never close" in o for o in outs) and win32gui.IsWindow(her) and not win.popups_of(her)
    print(f"DJ2: {'PASS' if res['DJ2'] else 'FAIL'} — refused={[('never close' in o) for o in outs]}; still open={win32gui.IsWindow(her)}")

    # DJ3: the title-bar Close, by index and by its point
    H.look(her, shot=False)
    lk = H.looks[her]
    close = closers(lk, her)
    o_idx = o_pt = ""
    if close:
        c = close[0]
        o_idx, _ = H.act(her, [{"click": c.index}], then="none", look_id=lk.id)
        H.look(her, shot=False)
        lk = H.looks[her]
        c = closers(lk, her)[0]
        sx, sy = c.center
        px, py = (sx - lk.origin[0]) * lk.scale, (sy - lk.origin[1]) * lk.scale
        o_pt, _ = H.act(her, [{"click": [px, py]}], then="none", look_id=lk.id)
    # and the window menu (where Close lives): expanding or clicking it is refused
    H.look(her, shot=False)
    lk = H.looks[her]
    sysm = [n for n in lk.nodes if n.ctype == "MenuItem" and Hands_in_title(lk, n)]
    o_menu = ""
    if sysm:
        o_menu, _ = H.act(her, [{"click": sysm[0].index}], then="none", look_id=lk.id)
    res["DJ3"] = (bool(close) and "never close" in o_idx and "never close" in o_pt and bool(sysm)
                  and "never close" in o_menu and win32gui.IsWindow(her) and not win.popups_of(her))
    print(f"DJ3: {'PASS' if res['DJ3'] else 'FAIL'} — close button found={bool(close)}; by index -> {o_idx.strip()[:60]!r}; "
          f"by point -> {o_pt.strip()[:60]!r}; window menu -> {o_menu.strip()[:40]!r}; still open={win32gui.IsWindow(her)}")

    # DJ7: a right click on her title bar (it opens the window menu, where Close lives) is refused, by point and index
    H.look(her, shot=False)
    lk = H.looks[her]
    l, t, r, b = win.frame_rect(her)
    cx, cy = (l + r) // 2, t + 12
    bar = [n for n in lk.nodes if n.ctype == "TitleBar"]
    o7p = H.act(her, [{"click": [(cx - lk.origin[0]) * lk.scale, (cy - lk.origin[1]) * lk.scale], "button": "right"}],
                then="none", look_id=lk.id)[0]
    H.look(her, shot=False)
    lk = H.looks[her]
    bar = [n for n in lk.nodes if n.ctype == "TitleBar"]
    o7i = H.act(her, [{"click": bar[0].index, "button": "right"}], then="none", look_id=lk.id)[0] if bar else ""
    res["DJ7"] = (win.hit_code(her, cx, cy) == 2 and "never close" in o7p and "never close" in o7i
                  and win32gui.IsWindow(her) and not win.popups_of(her))
    print(f"DJ7: {'PASS' if res['DJ7'] else 'FAIL'} — caption hit={win.hit_code(her, cx, cy)}; right click by point -> "
          f"{o7p.strip()[:50]!r}; by index -> {o7i.strip()[:50]!r}")

    # DJ4: a window Hands launched: full hands, its Close closes it
    os.environ["HANDS_SCRATCH_TITLE"] = "Mine scratch DJ"
    mine = H.launch(EXE)["id"]
    os.environ.pop("HANDS_SCRATCH_TITLE", None)
    # DJ6: her window covered by mine: her Close by index is still refused (the window itself is asked, not the top)
    l, t, r, b = win.frame_rect(her)
    win32gui.SetWindowPos(mine, 0, l, t, r - l, b - t, 0x0040)  # exactly over hers (SWP_SHOWWINDOW)
    win.set_foreground(mine)
    time.sleep(0.4)
    H.look(her, shot=False)
    lk = H.looks[her]
    hc = closers(lk, her)
    covered = bool(hc) and win.root_at(*hc[0].center) != her
    o6 = H.act(her, [{"click": hc[0].index}], then="none", look_id=lk.id)[0] if hc else ""
    H.look(her, shot=False)
    lk = H.looks[her]
    hc = closers(lk, her)
    sx, sy = hc[0].center
    o6p = H.act(her, [{"click": [(sx - lk.origin[0]) * lk.scale, (sy - lk.origin[1]) * lk.scale]}], then="none",
                look_id=lk.id)[0]
    res["DJ6"] = covered and "never close" in o6 and "never close" in o6p and win32gui.IsWindow(her)
    print(f"   DJ6 covered point click -> {o6p.strip()[:60]!r}")
    print(f"DJ6: {'PASS' if res['DJ6'] else 'FAIL'} — her Close covered by another window={covered}; by index -> {o6.strip()[:60]!r}; still open={win32gui.IsWindow(her)}")
    H.look(mine, shot=False)
    lk = H.looks[mine]
    mc = closers(lk, mine)
    o4 = ""
    if mc:
        o4, _ = H.act(mine, [{"click": mc[0].index}], then="none", look_id=lk.id)
    for _ in range(20):
        if not win32gui.IsWindow(mine):
            break
        time.sleep(0.1)
    res["DJ4"] = (not H._is_hers(mine) or not win32gui.IsWindow(mine)) and bool(mc) and not win32gui.IsWindow(mine)
    print(f"DJ4: {'PASS' if res['DJ4'] else 'FAIL'} — launched window closed by its own Close: {not win32gui.IsWindow(mine)} ({o4.strip()[:60]!r})")
finally:
    H.done()
    for w in [w for w in win.top_windows() if os.path.basename(w.exe).lower() == "hands_scratch.exe"]:
        force_close(w.id)

cm = session.closing_menu_item
menu_ok = [cm("E&xit", ""), cm("Exit", ""), cm("Close window", ""), cm("יציאה", ""), cm("Quit...", ""),
           cm("Something", "Alt+F4"), cm("Whatever", "Ctrl+W"), cm("Close tab\tCtrl+W", "")]
menu_no = [cm("Save", "Ctrl+S"), cm("Close-up mode", ""), cm("Open", ""), cm("Exit fullscreen", "F11")]
res["DJ8"] = all(menu_ok) and not any(menu_no)
print(f"DJ8: {'PASS' if res['DJ8'] else 'FAIL'} — closing items flagged={menu_ok}; ordinary flagged={menu_no}")
flag = {c: keys.closes(c) for c in ("alt+F4", "ctrl+F4", "ctrl+w", "ctrl+W", "ctrl+shift+w", "ctrl+q", "alt+space",
                                     "ctrl+s", "ctrl+a", "Return", "alt+f", "ctrl+z", "F4")}
res["DJ5"] = all(flag[c] for c in ("alt+F4", "ctrl+F4", "ctrl+w", "ctrl+W", "ctrl+shift+w", "ctrl+q", "alt+space")) and \
    not any(flag[c] for c in ("ctrl+s", "ctrl+a", "Return", "alt+f", "ctrl+z", "F4"))
print(f"DJ5: {'PASS' if res['DJ5'] else 'FAIL'} — {flag}")
left = [w.title for w in win.top_windows() if os.path.basename(w.exe).lower() == "hands_scratch.exe"]
print("   scratch windows left behind:", left)
print("SUMMARY " + " ".join(f"{k}:{'PASS' if v else 'FAIL'}" for k, v in res.items()))
sys.exit(0 if all(res.values()) and len(res) == 8 and not left else 1)
