"""Cleanup and ownership, on the tests' OWN scratch app only (never an app the person uses).
CL0: a new window that already holds a document is the person's: not counted as mine, never closed by done().
CL1: done() closes the windows I launched.
CL2: a launched window with unsaved changes is never discarded: its save prompt is cancelled, it stays open with its
     text, and done() says so."""
import os, subprocess, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["HANDS_TEST_HOOKS"] = "1"
os.environ["HANDS_TEST_ALLOW"] = "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App;hands_scratch.exe"
from engine import session, win
import uiautomation as auto
import win32gui

FX = os.path.join(ROOT, "tests", "fixtures")
EXE = os.path.join(FX, "hands_scratch.exe")
if not os.path.exists(EXE) or os.path.getmtime(EXE) < os.path.getmtime(os.path.join(FX, "scratch.cs")):
    csc = os.path.join(os.environ["WINDIR"], r"Microsoft.NET\Framework64\v4.0.30319\csc.exe")
    # same flags as the overlay: Smart App Control lets this build run (a plain AnyCPU build was blocked)
    subprocess.run([csc, "/nologo", "/optimize+", "/target:winexe", "/platform:x64", f"/out:{EXE}", "/r:System.Windows.Forms.dll",
                    os.path.join(FX, "scratch.cs")], check=True, capture_output=True)


def scratch_windows():
    return [w for w in win.top_windows() if os.path.basename(w.exe).lower() == "hands_scratch.exe"]


def force_close(hwnd):
    """The test's own leftover: close it and answer No (don't save): the text is the test's."""
    if not win32gui.IsWindow(hwnd):
        return
    win32gui.PostMessage(hwnd, 0x0010, 0, 0)
    for _ in range(30):
        time.sleep(0.1)
        for p in win.popups_of(hwnd):
            b = auto.ControlFromHandle(p).ButtonControl(searchDepth=4, AutomationId="7")  # IDNO
            if b.Exists(0.2, 0):
                b.GetInvokePattern().Invoke(waitTime=0.2)
        if not win32gui.IsWindow(hwnd):
            return


for w in scratch_windows():  # a previous run's leftovers
    force_close(w.id)
H = session.Hands(overlay=False)

# CL0: a new window whose title shows a document is the person's
os.environ["HANDS_SCRATCH_TITLE"] = "notes.txt - Hands scratch"
r0 = H.launch(EXE)
cl0 = (not r0["mine"]) and "belongs to the person" in r0.get("note", "") and r0["id"] not in H.launched
os.environ["HANDS_SCRATCH_TITLE"] = "Hands scratch"

calc = H.launch("calc")["id"]
mine = H.launch(EXE)
note = mine["id"]
H.look(note, shot=False)
lk = H.looks[note]
ed = [n for n in lk.nodes if n.ctype in ("Edit", "Document")][0]
H.act(note, [{"type": "CL2 unsaved text", "into": ed.index}], then="none", look_id=lk.id)
res = H.done()
time.sleep(0.3)
cl0 = cl0 and win32gui.IsWindow(r0["id"])  # done() left the person's window alone
cl1 = not win32gui.IsWindow(calc)
alive = win32gui.IsWindow(note)
val = ""
if alive:
    try:
        val = auto.ControlFromHandle(note).EditControl(searchDepth=4).GetValuePattern().Value
    except Exception:
        pass
cl2 = alive and "CL2 unsaved text" in val and "LEFT OPEN" in res and not win.popups_of(note)
print(f"CL0: {'PASS' if cl0 else 'FAIL'} — a new window holding a document: mine={r0['mine']}, note={r0.get('note', '')[:70]!r}, survived done()={win32gui.IsWindow(r0['id'])}")
print(f"CL1: {'PASS' if cl1 else 'FAIL'} — launched Calculator closed by done(): {cl1}")
print(f"CL2: {'PASS' if cl2 else 'FAIL'} — unsaved scratch kept with its text={'CL2' in val}, no prompt left={not win.popups_of(note) if alive else None}, reported={'LEFT OPEN' in res}")
print("   done() said:", res[:220])
for w in scratch_windows():
    force_close(w.id)
left = [w.title for w in scratch_windows()]
print("   scratch windows left behind:", left)
ok = cl0 and cl1 and cl2 and not left
print("SUMMARY CL0:%s CL1:%s CL2:%s" % tuple("PASS" if x else "FAIL" for x in (cl0, cl1, cl2)))
sys.exit(0 if ok else 1)
