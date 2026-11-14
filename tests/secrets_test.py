"""Secrets in what the agent is shown, on the desktop, on the tests' OWN scratch app (never an app the person uses).
SC1: the shared matcher catches every fake secret type and leaves ordinary text, ids and URLs alone.
SC2: a window holding fake secrets: none of them appears in the look's text, the header counts them, and the
     screenshot is blacked out over the text box (the secret's element) but not over the title area's ordinary text.
SC3: the same window with ordinary text only: no note, nothing blacked out.
SC4: a fake secret in the window's TITLE is hidden in the header and blacked out in the caption strip only.
SC5: a window with more elements than can all be checked gets no screenshot (failing closed).
SC6: the whole-screen look finds a fake key by reading the pixels and blacks it out; a broken check withholds it."""
import io, os, subprocess, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FX = os.path.join(ROOT, "tests", "fixtures")
EXE = os.path.join(FX, "hands_scratch.exe")
os.environ["HANDS_TEST_HOOKS"] = "1"
os.environ["HANDS_TEST_ALLOW"] = os.path.normcase(EXE)
from engine import session, win, secrets as S
import uiautomation as auto
import win32gui
from PIL import Image

FAKES = {
    "pg": "postgresql://neondb_owner:npg_Zx9Qw4Lm2Rt8Yv5Kp@ep-fake-meadow-00000.us-east-2.aws.neon.tech/neondb",
    "ant": "sk-" + "ant-api03-FAKEkeyQw8Zt4Lm2Rx9Yv5Kp7Hj3Nb6Gc1Df0Sa",  # assembled: no scanner sees a whole fake key
    "ghp": "gh" + "p_FaKe9Qw4Lm2Rt8Yv5Kp7Hj3Nb6Gc1Df0SaXz",
    "aws": "AKIA" + "FAKE0EXAMPLE7KEY",
    "jwt": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlIn0.Zm9vYmFyYmF6cXV4ZmFrZXNpZw",
    "slack": "xo" + "xb-0000000000-0000000000000-FaKeToKeN0000000000000",
    "pk": "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----",
}
PLAIN = ["Dinner reservation in 7 days.", "Branch overview production br-calm-lake-0000abcd",
         "commit 7846b00 redact: each word", "https://console.neon.tech/app/projects/quiet-river-00000000",
         "Created 2026-11-14 13:13:01", "Connection pooling", "Reset password", "Order #4471 shipped"]
res = {}
caught = {k: S.hide(v)[1] > 0 and not any(len(p) > 8 and p in S.hide(v)[0] for p in v.split("_")[1:2]) for k, v in FAKES.items()}
plain_ok = [S.hide(p) == (p, 0) for p in PLAIN]
res["SC1"] = all(caught.values()) and all(plain_ok)
print(f"SC1: {'PASS' if res['SC1'] else 'FAIL'} — caught={caught}; plain untouched={plain_ok}")


def scratch(title):
    return [w for w in win.top_windows() if os.path.basename(w.exe).lower() == "hands_scratch.exe" and w.title == title]


def force_close(hwnd):
    if not win32gui.IsWindow(hwnd):
        return
    win32gui.PostMessage(hwnd, 0x0010, 0, 0)
    for _ in range(30):
        time.sleep(0.1)
        for p in win.popups_of(hwnd):
            b = auto.ControlFromHandle(p).ButtonControl(searchDepth=4, AutomationId="7")
            if b.Exists(0.2, 0):
                b.GetInvokePattern().Invoke(waitTime=0.2)
        if not win32gui.IsWindow(hwnd):
            return


def fill_share(img, box):
    px = img.convert("RGB").load()
    l, t, r, b = box
    tot = hit = 0
    for y in range(max(0, t), min(img.height, b), 3):
        for x in range(max(0, l), min(img.width, r), 3):
            tot += 1
            p = px[x, y]
            hit += abs(p[0] - 58) < 22 and abs(p[1] - 42) < 22 and abs(p[2] - 51) < 22
    return hit / tot if tot else 0.0


for w in [w for w in win.top_windows() if os.path.basename(w.exe).lower() == "hands_scratch.exe"]:
    force_close(w.id)
H = session.Hands(overlay=False)
try:
    os.environ["HANDS_SCRATCH_TITLE"] = "Secrets scratch"
    sid = H.launch(EXE)["id"]
    edit = auto.ControlFromHandle(sid).EditControl(searchDepth=4)
    # SC3 first: ordinary text only
    edit.GetValuePattern().SetValue("\r\n".join(PLAIN), waitTime=0)
    time.sleep(0.3)
    txt3, imgs3 = H.look(sid, shot=True)
    im3 = Image.open(io.BytesIO(imgs3[0]))
    res["SC3"] = "secrets:" not in txt3 and fill_share(im3, (0, 0, im3.width, im3.height)) < 0.01
    print(f"SC3: {'PASS' if res['SC3'] else 'FAIL'} — note={'secrets:' in txt3}; blacked share={fill_share(im3, (0, 0, im3.width, im3.height)):.3f}")

    # SC2: fake secrets in the text box
    edit.GetValuePattern().SetValue("\r\n".join(FAKES.values()) + "\r\nPlain words stay readable", waitTime=0)
    time.sleep(0.3)
    txt2, imgs2 = H.look(sid, shot=True)
    im2 = Image.open(io.BytesIO(imgs2[0]))
    lk = H.looks[sid]
    ed = [n for n in lk.nodes if n.ctype in ("Edit", "Document")][0]
    l, t, r, b = ed.rect
    ox, oy = lk.origin
    sc = lk.scale
    box = (int((l - ox) * sc) + 6, int((t - oy) * sc) + 6, int((r - ox) * sc) - 6, int((b - oy) * sc) - 6)
    leaked = [k for k, v in FAKES.items() if any(part in txt2 for part in v.split("\n") if len(part) > 12)]
    edit_black = fill_share(im2, box)
    title_black = fill_share(im2, (0, 0, im2.width, max(1, box[1] - 8)))
    res["SC2"] = not leaked and "secrets:" in txt2 and edit_black > 0.9 and title_black < 0.2
    print(f"SC2: {'PASS' if res['SC2'] else 'FAIL'} — leaked={leaked}; note={[l for l in txt2.splitlines() if 'secrets:' in l][:1]}; "
          f"text box blacked {edit_black:.2f}; caption blacked {title_black:.2f}")

    # SC4: a secret in the title
    win32gui.SetWindowText(sid, "deploy " + FAKES["ghp"])
    edit.GetValuePattern().SetValue("only plain words here", waitTime=0)
    time.sleep(0.3)
    txt4, imgs4 = H.look(sid, shot=True)
    im4 = Image.open(io.BytesIO(imgs4[0]))
    head4 = txt4.splitlines()[0]
    lk = H.looks[sid]
    ed = [n for n in lk.nodes if n.ctype in ("Edit", "Document")][0]
    l, t, r, b = ed.rect
    body = (int((l - lk.origin[0]) * lk.scale) + 6, int((t - lk.origin[1]) * lk.scale) + 40,
            int((r - lk.origin[0]) * lk.scale) - 6, int((b - lk.origin[1]) * lk.scale) - 6)
    cap = (0, 0, im4.width, int(30 * lk.scale))
    res["SC4"] = ("ghp_FaKe" not in txt4 and "[secret hidden]" in head4 and fill_share(im4, cap) > 0.5
                  and fill_share(im4, body) < 0.05)
    print(f"SC4: {'PASS' if res['SC4'] else 'FAIL'} — header={head4[:90]!r}; caption blacked {fill_share(im4, cap):.2f}; "
          f"body blacked {fill_share(im4, body):.2f}")
    # SC6: the whole-screen look: the scratch window, in front, big text holding a fake key → the key is found by
    #      reading the pixels and blacked out; a broken check withholds the picture
    win32gui.SetWindowText(sid, "Secrets scratch")
    edit.GetValuePattern().SetValue("deploy key: " + FAKES["ghp"], waitTime=0)
    import ctypes
    hf = ctypes.windll.gdi32.CreateFontW(-28, 0, 0, 0, 400, 0, 0, 0, 0, 0, 0, 0, 0, "Consolas")
    ctypes.windll.user32.SendMessageW(win32gui.FindWindowEx(sid, 0, None, None), 0x0030, hf, 1)  # WM_SETFONT
    l0, t0, _, _ = win.frame_rect(sid)
    win32gui.SetWindowPos(sid, 0, l0, t0, 1100, 300, 0x0004)
    win.set_foreground(sid)
    time.sleep(0.6)
    H.consent.session.add("screen:all")  # the test's own yes for the whole-screen look
    txt6, imgs6 = H.screen()
    shown = bool(imgs6)
    import numpy as np
    im6 = Image.open(io.BytesIO(imgs6[0])).convert("RGB") if shown else None
    hidden6 = "secrets:" in txt6
    # the box must sit on the key itself: the text box's first line, in the screen image (origin + scale from the header)
    on_key = 0.0
    if shown:
        import re as _re
        mo = _re.search(r"origin (-?\d+),(-?\d+)", txt6)
        ms = _re.search(r"image scale ([0-9.]+)", txt6)
        ox6, oy6 = (int(mo.group(1)), int(mo.group(2))) if mo else (0, 0)
        s6 = float(ms.group(1)) if ms else 1.0
        er = [n for n in H.tree.snapshot(sid, (0, 0)) if n.ctype in ("Edit", "Document")][0].rect
        key_box = (int((er[0] - ox6 + 200) * s6), int((er[1] - oy6 + 4) * s6), int((er[0] - ox6 + 700) * s6),
                   int((er[1] - oy6 + 30) * s6))
        px6 = im6.load()
        pts = [(x, y) for y in range(key_box[1], key_box[3], 2) for x in range(key_box[0], key_box[2], 3)]
        on_key = sum(1 for x, y in pts if max(px6[x, y]) < 30) / max(1, len(pts))
    session.Hands._ocr = (session.Hands._ocr[0], None, session.Hands._ocr[2]) if session.Hands._ocr else None
    broken_txt, broken_imgs = H.screen()  # a dead OCR worker: the picture must be withheld
    session.Hands._ocr = None
    res["SC6"] = shown and hidden6 and on_key > 0.6 and not broken_imgs and "withheld" in broken_txt
    print(f"SC6: {'PASS' if res['SC6'] else 'FAIL'} — whole screen shown={shown}; key blacked {on_key:.2f}; "
          f"note={[l for l in txt6.splitlines() if 'secrets:' in l][:1]}; broken check -> images={len(broken_imgs)}, "
          f"{[l for l in broken_txt.splitlines() if 'withheld' in l][:1]}")

    # SC5: a window with more elements than can all be checked gets no screenshot (failing closed), with a note
    real_cap = session.SCAN_CAP
    session.SCAN_CAP = 3
    try:
        txt5, imgs5 = H.look(sid, shot=True)
    finally:
        session.SCAN_CAP = real_cap
    res["SC5"] = not imgs5 and "withheld" in txt5 and "too many to check" in txt5
    print(f"SC5: {'PASS' if res['SC5'] else 'FAIL'} — images={len(imgs5)}; note={[l for l in txt5.splitlines() if 'withheld' in l][:1]}")
finally:
    os.environ.pop("HANDS_SCRATCH_TITLE", None)
    H.done()
    for w in [w for w in win.top_windows() if os.path.basename(w.exe).lower() == "hands_scratch.exe"]:
        force_close(w.id)
left = [w.title for w in win.top_windows() if os.path.basename(w.exe).lower() == "hands_scratch.exe"]
print("   scratch windows left behind:", left)
print("SUMMARY " + " ".join(f"{k}:{'PASS' if v else 'FAIL'}" for k, v in res.items()))
sys.exit(0 if len(res) == 6 and all(res.values()) and not left else 1)
