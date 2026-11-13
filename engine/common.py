"""What the desktop and the browser share: errors, the person's consent, stops, action checks, the closing look.
One copy of each rule, so the two surfaces can't drift apart."""
from __future__ import annotations

import math
import os

TEXT_CAP = 20000
THEN = ("both", "tree", "shot", "none")


class HandsError(Exception):
    pass


class NeedsConsent(HandsError):
    pass


class Stopped(HandsError):
    pass


def test_hooks() -> bool:
    """Test switches (HANDS_TEST_ALLOW, HANDS_PROOF, debug.*) only work when this is also set."""
    return os.environ.get("HANDS_TEST_HOOKS") == "1"


class Stops:
    """One stop (Esc×2, the pill's Stop, the debugging bar's Cancel) stops BOTH surfaces; each surface resumes
    only through its own fresh look (hands_look → desktop, browser_look → browser)."""

    SURFACES = ("desktop", "browser")

    def __init__(self):
        self.desktop = False
        self.browser = False
        self.reason = ""

    def stop_all(self, reason: str) -> None:
        self.desktop = self.browser = True
        self.reason = reason

    def check(self, surface: str) -> None:
        if getattr(self, surface):
            if surface == "browser":
                raise Stopped(f"browser use stopped: {self.reason}. Input is refused until I look again (browser_look) "
                              f"and the person wants the agent back.")
            raise Stopped(f"The person stopped the hands ({self.reason}). Input is refused until I look again (hands_look) "
                          f"and the person wants the agent back.")

    def resume(self, surface: str) -> None:
        setattr(self, surface, False)


def gate_consent(hands, key: str, label: str, detail: str) -> None:
    """The person's consent for `key`, after the caller's deny check: allowed → refused → no card (headless) → card."""
    if hands.consent.allowed(key):
        return
    if key in hands.consent.refused:
        raise HandsError(f"The person said no to {key} this session.")
    if not hands.use_overlay:
        raise NeedsConsent(f"needs_consent: {key}")
    choice = hands.ov.ask_consent(label, detail)
    if choice in ("once", "always"):
        hands.consent.grant(key, choice)
        return
    if choice == "no":
        hands.consent.refuse(key)
        raise HandsError(f"The person said no to {label}.")
    if choice == "no_overlay":
        raise NeedsConsent(f"needs_consent: {key} — no consent card could be shown: the overlay isn't running. Ask the person.")
    raise NeedsConsent(f"needs_consent: {label} ({key}) — the person didn't answer the card in 60 s. Ask them.")


def _int(v, what: str, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise HandsError(f"{what} must be a whole number {lo}–{hi}")
    return v


def check_action(a) -> dict:
    """The checks every action passes on both surfaces before anything happens. Refuses, never truncates."""
    if not isinstance(a, dict) or not a:
        raise HandsError(f"Bad action {a!r}")
    if "wait" in a:
        w = a["wait"]
        if isinstance(w, bool) or not isinstance(w, (int, float)) or not math.isfinite(w) or not 0 <= w <= 5:
            raise HandsError("wait: 0–5 seconds")
    if "count" in a:
        _int(a["count"], "count", 1, 3)
    if "button" in a and a["button"] not in ("left", "right", "middle"):
        raise HandsError("button: left|right|middle")
    for k in ("type", "value"):
        if k in a and (not isinstance(a[k], str) or len(a[k]) > TEXT_CAP):
            raise HandsError(f"{k}: text up to {TEXT_CAP} chars")
    for k in ("dy", "dx"):
        if k in a:
            _int(a[k], k, -50, 50)
    return a


def check_then(then) -> str:
    if then not in THEN:
        raise HandsError("then: both|tree|shot|none")
    return then


def close_with_look(out: str, then: str, stopped: bool, alive: bool, look) -> tuple[str, list]:
    """An act's closing look: never clears a stop, and a failure there is reported in the result, never raised."""
    if then == "none" or stopped or not alive:
        return out, []
    from . import policy
    try:
        text, imgs = look()
    except Stopped as e:
        return out + f"\nSTOPPED: {e}", []
    except (HandsError, policy.Denied) as e:
        return out + f"\n(after: {e})", []
    return out + "\n--- after ---\n" + text, imgs
