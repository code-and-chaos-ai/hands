"""Windows: enumeration, identity (exe / AUMID), capture, foreground and input.

All coordinates are physical pixels; the process is per-monitor-v2 DPI aware.
"""
from __future__ import annotations

import ctypes
import io
import os
import time
from ctypes import wintypes
from dataclasses import dataclass, field

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
gdi32 = ctypes.WinDLL("gdi32")

try:  # per-monitor v2; must run before any window call
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:  # pragma: no cover
    pass

import win32con  # noqa: E402
import win32gui  # noqa: E402
import win32process  # noqa: E402
import win32ui  # noqa: E402
from PIL import Image  # noqa: E402

GW_OWNER = 4
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
PW_RENDERFULLCONTENT = 2
SW_SHOWNOACTIVATE = 4
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MAX_LONG_EDGE = 1568


# ---------------------------------------------------------------- identity

def exe_of_pid(pid: int) -> str:
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


def aumid_of_hwnd(hwnd: int) -> str:
    try:
        from win32com.propsys import propsys, pscon
        store = propsys.SHGetPropertyStoreForWindow(hwnd, propsys.IID_IPropertyStore)
        v = store.GetValue(pscon.PKEY_AppUserModel_ID).GetValue()
        return v or ""
    except Exception:
        return ""


def pid_of(hwnd: int) -> int:
    return win32process.GetWindowThreadProcessId(hwnd)[1]


def real_pid(hwnd: int) -> int:
    """UWP frames are hosted by ApplicationFrameHost; the app is the CoreWindow child."""
    pid = pid_of(hwnd)
    if win32gui.GetClassName(hwnd) == "ApplicationFrameWindow":
        found = []

        def cb(ch, _):
            if win32gui.GetClassName(ch) == "Windows.UI.Core.CoreWindow":
                found.append(pid_of(ch))
            return True

        try:
            win32gui.EnumChildWindows(hwnd, cb, None)
        except Exception:
            pass
        if found:
            return found[0]
    return pid


# ---------------------------------------------------------------- geometry

def is_cloaked(hwnd: int) -> bool:
    v = ctypes.c_int(0)
    dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v))
    return v.value != 0


def frame_rect(hwnd: int) -> tuple[int, int, int, int]:
    """Visible frame (no invisible resize borders)."""
    r = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) == 0:
        return r.left, r.top, r.right, r.bottom
    return win32gui.GetWindowRect(hwnd)


@dataclass
class Win:
    id: int
    title: str
    exe: str
    app: str  # AUMID when packaged, else exe basename
    pid: int
    cls: str
    minimized: bool
    rect: tuple[int, int, int, int]
    foreground: bool = False

    def brief(self) -> dict:
        l, t, r, b = self.rect
        return {"id": self.id, "app": self.app, "exe": os.path.basename(self.exe), "title": self.title,
                "minimized": self.minimized, "size": [r - l, b - t], "foreground": self.foreground}


def clean(s) -> str:
    """Text from Windows can hold broken UTF-16 (a lone half of an emoji). Pairs are joined, lone halves become
    U+FFFD, so nothing read from a window can break encoding downstream."""
    if not isinstance(s, str):
        return s
    return s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def title_of(hwnd: int) -> str:
    return clean(win32gui.GetWindowText(hwnd))


def describe(hwnd: int) -> Win:
    pid = real_pid(hwnd)
    exe = exe_of_pid(pid)
    aumid = aumid_of_hwnd(hwnd)
    return Win(id=hwnd, title=title_of(hwnd), exe=exe,
               app=aumid or os.path.basename(exe).lower(), pid=pid, cls=win32gui.GetClassName(hwnd),
               minimized=bool(win32gui.IsIconic(hwnd)), rect=frame_rect(hwnd),
               foreground=hwnd == win32gui.GetForegroundWindow())


def top_windows() -> list[Win]:
    """Real, user-facing top-level windows, z-order (front first)."""
    out: list[int] = []

    def cb(h, _):
        if not win32gui.IsWindowVisible(h) or not win32gui.GetWindowText(h):
            return True
        if user32.GetWindow(h, GW_OWNER):
            return True
        if win32gui.GetWindowLong(h, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOOLWINDOW:
            return True
        if is_cloaked(h):
            return True
        l, t, r, b = frame_rect(h)
        if (r - l < 40 or b - t < 30) and not win32gui.IsIconic(h):
            return True
        if win32gui.GetClassName(h) in ("Progman", "Shell_TrayWnd", "WorkerW"):
            return True
        out.append(h)
        return True

    win32gui.EnumWindows(cb, None)
    res = []
    for h in out:
        try:
            res.append(describe(h))
        except Exception:  # the window closed between the enumeration and now
            continue
    return res


def _owned_by(h: int, hwnd: int) -> bool:
    """h is owned by hwnd, directly or through a chain of owners (a dialog of a dialog)."""
    seen = 0
    o = user32.GetWindow(h, GW_OWNER)
    while o and seen < 8:
        if o == hwnd:
            return True
        o = user32.GetWindow(o, GW_OWNER)
        seen += 1
    return False


def all_top_hwnds() -> set[int]:
    """Every top-level window that exists, in ANY state (hidden, cloaked, minimized). 'New' means not in here:
    a minimized window an app un-hides to add a tab is NOT new, and is NOT mine."""
    out: set[int] = set()
    win32gui.EnumWindows(lambda h, _: out.add(h) or True, None)
    return out


def popups_of(hwnd: int) -> list[int]:
    """This window's own popups: windows it owns (dialogs, through the owner chain) and menus/dropdown lists from its
    own UI thread. Never another window's dialog just because the app runs all its windows in one process."""
    tid = win32process.GetWindowThreadProcessId(hwnd)[0]
    res: list[int] = []

    def cb(h, _):
        if h == hwnd or not win32gui.IsWindowVisible(h) or is_cloaked(h):
            return True
        menu = (win32gui.GetClassName(h) in ("#32768", "ComboLBox")
                and win32process.GetWindowThreadProcessId(h)[0] == tid and not user32.GetWindow(h, GW_OWNER))
        if _owned_by(h, hwnd) or menu:
            l, t, r, b = frame_rect(h)
            if r - l > 4 and b - t > 4:
                res.append(h)
        return True

    win32gui.EnumWindows(cb, None)
    return res


# ---------------------------------------------------------------- capture

def restore_quietly(hwnd: int) -> None:
    if win32gui.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        time.sleep(0.25)


def capture(hwnd: int) -> Image.Image:
    """PrintWindow with full-content rendering: works when the window is covered."""
    wl, wt, wr, wb = win32gui.GetWindowRect(hwnd)
    w, h = max(1, wr - wl), max(1, wb - wt)
    hdc = win32gui.GetWindowDC(hwnd)
    mfc = win32ui.CreateDCFromHandle(hdc)
    sdc = mfc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc, w, h)
    sdc.SelectObject(bmp)
    try:
        user32.PrintWindow(hwnd, sdc.GetSafeHdc(), PW_RENDERFULLCONTENT)
        info = bmp.GetInfo()
        img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1)
    finally:
        win32gui.DeleteObject(bmp.GetHandle())
        sdc.DeleteDC()
        mfc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hdc)
    if win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & 0x00400000:  # WS_EX_LAYOUTRTL
        # a right-to-left window (right-to-left dialogs) comes out of PrintWindow mirrored; give it back as the person sees it
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    fl, ft, fr, fb = frame_rect(hwnd)
    box = (fl - wl, ft - wt, fl - wl + (fr - fl), ft - wt + (fb - ft))
    if box != (0, 0, w, h) and box[2] <= w and box[3] <= h and box[0] >= 0 and box[1] >= 0:
        img = img.crop(box)
    return img


def to_jpeg(img: Image.Image, quality: int = 78) -> tuple[bytes, float]:
    """Downscale to the long-edge cap. Returns (bytes, scale) where image_px = window_px * scale."""
    scale = 1.0
    long_edge = max(img.size)
    if long_edge > MAX_LONG_EDGE:
        scale = MAX_LONG_EDGE / long_edge
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue(), scale


def is_blank(img: Image.Image) -> bool:
    lo, hi = img.convert("L").resize((64, 64)).getextrema()
    return hi - lo < 4


# ---------------------------------------------------------------- foreground + input

class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def last_input_tick() -> int:
    li = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    user32.GetLastInputInfo(ctypes.byref(li))
    return li.dwTime


def tick() -> int:
    return kernel32.GetTickCount() & 0xFFFFFFFF


def foreground() -> int:
    return win32gui.GetForegroundWindow()


def hit_code(hwnd: int, x: int, y: int) -> int:
    """What this window says lies at a screen point (WM_NCHITTEST), whatever covers it: 20 close, 8 minimize,
    9 maximize, 3 window menu, 2 caption... 0 when it doesn't answer."""
    lp = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
    r = ctypes.c_size_t(0)
    ok = ctypes.windll.user32.SendMessageTimeoutW(wintypes.HWND(hwnd), 0x0084, wintypes.WPARAM(0),
                                                  wintypes.LPARAM(lp), 0x0002, 300, ctypes.byref(r))
    return int(r.value) if ok else 0


def close_hit(x: int, y: int, count: int = 1) -> bool:
    """True when a click here would close the window under it, in any language: its caption Close button
    (WM_NCHITTEST says HTCLOSE, or UI Automation names the element under the point AutomationId 'Close'), or its
    window-menu icon (HTSYSMENU: a click opens the menu Close lives in, a double click closes)."""
    root = root_at(x, y)
    if root and hit_code(root, x, y) in (20, 3):  # HTCLOSE / HTSYSMENU
        return True
    try:
        import uiautomation as auto
        c = auto.ControlFromPoint(x, y)
        if c and c.AutomationId == "Close" and c.ControlTypeName == "ButtonControl":
            return True
    except Exception:
        pass
    return False


def root_at(x: int, y: int) -> int:
    """The top-level window under a screen point."""
    h = user32.WindowFromPoint(wintypes.POINT(x, y))
    return user32.GetAncestor(h, 2) if h else 0  # GA_ROOT


def set_foreground(hwnd: int) -> bool:
    """Reliable foreground. A zero-distance injected mouse move makes us the last input source, which
    unlocks SetForegroundWindow; never an ALT tap (that opens menu mode in the person's app)."""
    if not hwnd or not win32gui.IsWindow(hwnd):
        return False
    if win32gui.GetForegroundWindow() == hwnd:
        return True
    if win32gui.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    _send([_mouse(MOUSEEVENTF_MOVE, 0, 0)])
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        user32.BringWindowToTop(hwnd)
    time.sleep(0.03)
    return win32gui.GetForegroundWindow() == hwnd


def cursor_pos() -> tuple[int, int]:
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


# SendInput structures
ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


HANDS_TAG = 0x524F5741  # "ROWA": marks our own injected input so the overlay can tell it from the person's
KEYEVENTF_KEYUP = 0x2
KEYEVENTF_UNICODE = 0x4
KEYEVENTF_EXTENDEDKEY = 0x1
MOUSEEVENTF_MOVE = 0x1
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_WHEEL = 0x800
MOUSEEVENTF_HWHEEL = 0x1000
BUTTON_FLAGS = {"left": (0x2, 0x4), "right": (0x8, 0x10), "middle": (0x20, 0x40)}
EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x6F, 0x90, 0xA3, 0xA5}


def _send(inputs: list[INPUT]) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    i = INPUT(type=1)
    i.u.ki = KEYBDINPUT(vk, scan, flags, 0, HANDS_TAG)
    return i


def send_keys_raw(seq: list[tuple[int, bool]]) -> None:
    _send([_key(vk, 0, (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_VKS else 0))
           for vk, up in seq])


TYPE_CHUNK = 6       # characters per SendInput burst: modern text stacks (TSF, WinUI) garble long bursts
TYPE_GAP_S = 0.012


def send_text(text: str) -> None:
    """Unicode typing (any script, emoji-safe), paced; newlines become Enter, tabs Tab. An emoji outside the BMP goes
    as its two UTF-16 halves pressed together (both downs, then both ups) so no app sees a lone half."""
    batch: list[INPUT] = []
    count = 0
    for ch in text:
        if ch == "\r":
            continue
        if ch == "\n":
            batch += [_key(0x0D), _key(0x0D, 0, KEYEVENTF_KEYUP)]
        elif ch == "\t":
            batch += [_key(0x09), _key(0x09, 0, KEYEVENTF_KEYUP)]
        else:
            data = ch.encode("utf-16-le", "surrogatepass")
            units = [data[k] | (data[k + 1] << 8) for k in range(0, len(data), 2)]
            batch += [_key(0, u, KEYEVENTF_UNICODE) for u in units]
            batch += [_key(0, u, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP) for u in units]
        count += 1
        if count % TYPE_CHUNK == 0:
            _send(batch)
            batch = []
            time.sleep(TYPE_GAP_S)
    if batch:
        _send(batch)


def _abs(x: int, y: int) -> tuple[int, int]:
    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    return round((x - vx) * 65535 / max(1, vw - 1)), round((y - vy) * 65535 / max(1, vh - 1))


def _mouse(flags: int, x: int = 0, y: int = 0, data: int = 0) -> INPUT:
    i = INPUT(type=0)
    i.u.mi = MOUSEINPUT(x, y, ctypes.c_uint32(data).value, flags, 0, HANDS_TAG)
    return i


def mouse_move(x: int, y: int) -> None:
    ax, ay = _abs(x, y)
    _send([_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, ax, ay)])


def mouse_click(x: int, y: int, button: str = "left", count: int = 1) -> None:
    down, up = BUTTON_FLAGS[button]
    ax, ay = _abs(x, y)
    seq = [_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, ax, ay)]
    for _ in range(count):
        seq += [_mouse(down), _mouse(up)]
    _send(seq)


def mouse_drag(x1: int, y1: int, x2: int, y2: int, steps: int = 18) -> None:
    down, up = BUTTON_FLAGS["left"]
    mouse_move(x1, y1)
    _send([_mouse(down)])
    for s in range(1, steps + 1):
        mouse_move(round(x1 + (x2 - x1) * s / steps), round(y1 + (y2 - y1) * s / steps))
        time.sleep(0.008)
    _send([_mouse(up)])


def mouse_wheel(x: int, y: int, dy: int = 0, dx: int = 0) -> None:
    mouse_move(x, y)
    seq = []
    if dy:
        seq.append(_mouse(MOUSEEVENTF_WHEEL, data=-dy))
    if dx:
        seq.append(_mouse(MOUSEEVENTF_HWHEEL, data=dx))
    if seq:
        _send(seq)


def set_cursor(x: int, y: int) -> None:
    user32.SetCursorPos(x, y)
