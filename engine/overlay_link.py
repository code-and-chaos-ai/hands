"""The line to overlay.exe: JSON lines out on its stdin, events back on its stdout.

ops  → target{hwnd,label} · mode{fg} · glide{x,y,kind,seq} · consent{seq,app,detail} · idle · quit
events ← ready · arrived{seq} · consent{seq,choice} · stop · her_input{tick}
Headless when overlay.exe is missing: everything still works, consent can't be asked.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

from . import identity

EXE = Path(__file__).resolve().parent.parent / "overlay" / "overlay.exe"


class OverlayLink:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.seq = 0
        self.waiters: dict[int, tuple[threading.Event, dict]] = {}
        self.stop_requested = False
        self.her_input_tick = 0
        self.ready = threading.Event()

    # -------------------------------------------------- lifecycle
    def ensure(self) -> bool:
        if self.proc and self.proc.poll() is None:
            return True
        if not EXE.exists():
            return False
        self.ready.clear()
        env = dict(os.environ)
        if env.get("HANDS_TEST_HOOKS") != "1":
            env.pop("HANDS_PROOF", None)  # proof shots (overlay visible to capture) are a test switch only
        me = identity.get()  # the overlay draws the agent's own look
        env.update(HANDS_LOOK_NAME=me["name"], HANDS_LOOK_INITIAL=me["initial"], HANDS_LOOK_COLOR=me["color"],
                   HANDS_LOOK_COLOR_HOT=me["color_hot"], HANDS_LOOK_LABEL=identity.label())
        self.proc = subprocess.Popen([str(EXE)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, creationflags=0x08000000, env=env)  # no console
        threading.Thread(target=self._reader, daemon=True).start()
        self.ready.wait(3.0)
        return self.proc.poll() is None

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            self._send({"op": "quit"})
            try:
                self.proc.wait(1.5)
            except Exception:
                self.proc.kill()
        self.proc = None

    @property
    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    # -------------------------------------------------- wire
    def _send(self, msg: dict) -> bool:
        if not self.alive:
            return False
        try:
            with self.lock:
                self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
                self.proc.stdin.flush()
            return True
        except Exception:
            return False

    def _reader(self) -> None:
        p = self.proc
        for raw in iter(p.stdout.readline, b""):
            try:
                ev = json.loads(raw.decode("utf-8"))
            except Exception:
                continue
            kind = ev.get("ev")
            if kind == "ready":
                self.ready.set()
            elif kind == "stop":
                self.stop_requested = True
            elif kind == "her_input":
                self.her_input_tick = int(ev.get("tick", 0))
            elif kind in ("arrived", "consent"):
                w = self.waiters.get(int(ev.get("seq", -1)))
                if w:
                    w[1].update(ev)
                    w[0].set()

    def _next(self) -> int:
        with self.lock:
            self.seq += 1
            return self.seq

    # -------------------------------------------------- ops
    def target(self, hwnd: int, label: str | None = None) -> None:
        label = label or identity.label()
        if self.ensure():
            self._send({"op": "target", "hwnd": hwnd, "label": label})

    def mode(self, fg: bool) -> None:
        self._send({"op": "mode", "fg": fg})

    def glide(self, x: int, y: int, kind: str = "click", wait: float = 0.6) -> None:
        # the overlay times the trip by distance (200–520 ms) so the person can follow it; the engine waits for it to land
        if not self.alive:
            return
        seq = self._next()
        ev = threading.Event()
        self.waiters[seq] = (ev, {})
        self._send({"op": "glide", "x": x, "y": y, "kind": kind, "seq": seq})
        ev.wait(wait)
        self.waiters.pop(seq, None)

    def ask_consent(self, app: str, detail: str, timeout: float = 60.0) -> str | None:
        """once | always | no | None (no answer in time) | "no_overlay" (no card could be shown at all)."""
        if not self.ensure():
            return "no_overlay"
        seq = self._next()
        ev = threading.Event()
        box: dict = {}
        self.waiters[seq] = (ev, box)
        self._send({"op": "consent", "seq": seq, "app": app, "detail": detail})
        ev.wait(timeout)
        self.waiters.pop(seq, None)
        if not ev.is_set():
            self._send({"op": "consent_cancel", "seq": seq})
            return None
        return box.get("choice")

    def idle(self) -> None:
        self._send({"op": "idle"})
