"""Key chords: 'ctrl+s', 'Control_L+Shift_L+period', 'Return', 'alt+F4', 'KP_0'. Never the Windows key."""
from __future__ import annotations

MODS = {"ctrl": 0x11, "control": 0x11, "control_l": 0xA2, "control_r": 0xA3, "shift": 0x10, "shift_l": 0xA0,
        "shift_r": 0xA1, "alt": 0x12, "alt_l": 0xA4, "alt_r": 0xA5, "menu": 0x12}
WIN = {"win", "windows", "meta", "super", "cmd", "command", "os", "super_l", "super_r", "meta_l", "meta_r", "lwin", "rwin"}
NAMED = {
    "return": 0x0D, "enter": 0x0D, "kp_enter": 0x0D, "numpad_enter": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B,
    "backspace": 0x08, "back": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "space": 0x20,
    "home": 0x24, "end": 0x23, "page_up": 0x21, "pageup": 0x21, "prior": 0x21, "page_down": 0x22,
    "pagedown": 0x22, "next": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "caps_lock": 0x14, "menu_key": 0x5D, "apps": 0x5D, "print": 0x2C,
    "period": 0xBE, "comma": 0xBC, "minus": 0xBD, "plus": 0xBB, "equal": 0xBB, "slash": 0xBF,
    "backslash": 0xDC, "semicolon": 0xBA, "apostrophe": 0xDE, "quote": 0xDE, "grave": 0xC0,
    "bracketleft": 0xDB, "bracketright": 0xDD,
    "numpad_add": 0x6B, "kp_add": 0x6B, "numpad_subtract": 0x6D, "kp_subtract": 0x6D,
    "numpad_multiply": 0x6A, "kp_multiply": 0x6A, "numpad_divide": 0x6F, "kp_divide": 0x6F,
    "numpad_decimal": 0x6E, "kp_decimal": 0x6E,
}
SHIFTED = {"greater": 0xBE, "less": 0xBC, "question": 0xBF, "colon": 0xBA, "underscore": 0xBD,
           "exclam": 0x31, "at": 0x32, "numbersign": 0x33, "dollar": 0x34, "percent": 0x35}
for _i in range(10):
    NAMED[f"kp_{_i}"] = 0x60 + _i
    NAMED[f"numpad_{_i}"] = 0x60 + _i
for _i in range(1, 25):
    NAMED[f"f{_i}"] = 0x6F + _i


class KeyError_(ValueError):
    pass


def _vk(token: str) -> tuple[int, bool]:
    t = token.strip()
    low = t.lower()
    if low in WIN:
        raise KeyError_("I never press the Windows key.")
    if low in NAMED:
        return NAMED[low], False
    if low in SHIFTED:
        return SHIFTED[low], True
    if len(t) == 1:
        import ctypes
        r = ctypes.windll.user32.VkKeyScanW(ord(t))
        if r == -1:
            raise KeyError_(f"Can't map key {t!r}; type it as text instead.")
        return r & 0xFF, bool(r & 0x100)
    raise KeyError_(f"Unknown key {token!r}")


def parse(chord: str) -> list[tuple[int, bool]]:
    parts = [p for p in chord.replace(" ", "").split("+") if p] if chord.strip() != "+" else ["plus"]
    if not parts:
        raise KeyError_("Empty key.")
    mods, key = parts[:-1], parts[-1]
    downs: list[int] = []
    for m in mods:
        ml = m.lower()
        if ml in WIN:
            raise KeyError_("I never press the Windows key.")
        if ml not in MODS:
            raise KeyError_(f"Unknown modifier {m!r}")
        downs.append(MODS[ml])
    vk, need_shift = _vk(key)
    if need_shift and 0x10 not in downs and 0xA0 not in downs:
        downs.append(0x10)
    ctrl = bool({0x11, 0xA2, 0xA3} & set(downs))
    alt = bool({0x12, 0xA4, 0xA5} & set(downs))
    shift = bool({0x10, 0xA0, 0xA1} & set(downs))
    if (ctrl and vk == 0x1B) or (alt and vk in (0x09, 0x1B)) or (ctrl and alt and vk == 0x2E) or (ctrl and shift and vk == 0x1B):
        raise KeyError_(f"I never press {chord}: it reaches the system (Start, task switching, task manager, "
                        f"security screen), not the window I'm in.")
    seq = [(d, False) for d in downs] + [(vk, False), (vk, True)] + [(d, True) for d in reversed(downs)]
    return seq
