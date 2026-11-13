"""The bouncer: one holder of the hands at a time, across every door (each door runs its own Hands process).

State lives in state/bouncer.json, read and written only under a named mutex. A holder is an exact identity
(house + session id + server pid). The first screen-touching call takes the lease, every call renews it. The
lease frees on done, on the holder's process dying, or on silence. When it frees, the first in line is nudged and
the lease is held for it a short while. The person's Stop frees it and nudges nobody."""
import json
import math
import os
import re
import subprocess
import time
from dataclasses import dataclass

import win32api
import win32con
import win32event

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEST = os.environ.get("HANDS_TEST_HOOKS") == "1" and os.environ.get("HANDS_BOUNCER_DIR")
STATE = os.environ["HANDS_BOUNCER_DIR"] if _TEST else os.path.join(ROOT, "state")  # tests: their own world
FILE = os.path.join(STATE, "bouncer.json")
CONFIG = os.path.join(STATE, "config.json")
MUTEX = "Local\\hands-bouncer" + ("-test" if _TEST else "")
SILENT_S = 180      # a holder this quiet has let go
RESERVE_S = 60      # how long a nudged waiter's place is kept


@dataclass(frozen=True)
class Who:
    house: str
    id: str
    pid: int

    def key(self) -> str:
        return f"{self.house}:{self.id}"

    def label(self) -> str:
        return f"{self.house.upper()} session {self.id}"


def me_from_env() -> Who:
    """This server's exact identity. CC: the PreToolUse hook writes the session id to state/sessions/<cc pid>.json
    and the server finds it through its own parent pid; any other host: HANDS_SESSION."""
    house = os.environ.get("HANDS_HOUSE", "cc").strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,16}", house):
        house = "cc"
    sid = os.environ.get("HANDS_SESSION", "").strip()
    if not sid:
        try:
            sid = json.load(open(os.path.join(STATE, "sessions", f"{os.getppid()}.json"), encoding="utf-8"))["session_id"]
            if not isinstance(sid, str) or not sid.strip():
                raise ValueError
        except Exception:  # missing, unreadable, wrong shape: fall back, never fail the call
            sid = f"pid{os.getppid()}"
    sid = "".join(ch for ch in sid if ch.isprintable())[:64] or f"pid{os.getppid()}"
    return Who(house, sid, os.getpid())


def _entry_ok(e, times: tuple) -> bool:
    """One holder / waiter / reservation as the bouncer writes it, and nothing else."""
    if not isinstance(e, dict):
        return False
    if not (isinstance(e.get("house"), str) and isinstance(e.get("id"), str)):
        return False
    pid = e.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return False
    if "purpose" in e and not isinstance(e["purpose"], str):
        return False
    for k in times:
        v = e.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return False
    return True


CLOCK_SLACK = 120  # a clock correction can move time back this far without freeing a working holder


def _sane(t: float, now: float, ahead: float = CLOCK_SLACK) -> bool:
    """A time the bouncer itself could have written: not from the far past, not from beyond the clock's slack."""
    return now - 86400 <= t <= now + ahead


def _alive(pid: int) -> bool:
    try:
        h = win32api.OpenProcess(win32con.SYNCHRONIZE | 0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    except Exception:
        return False
    try:
        return win32event.WaitForSingleObject(h, 0) == win32event.WAIT_TIMEOUT
    finally:
        win32api.CloseHandle(h)


def _hhmm(t: float) -> str:
    try:
        return time.strftime("%H:%M", time.localtime(t))
    except (OverflowError, OSError, ValueError):
        return "?"


class Bouncer:
    def __init__(self, me: Who, now=time.time):
        self.me = me
        self.now = now
        self.mx = win32event.CreateMutex(None, False, MUTEX)

    # ------------------------------------------------------------ state, under the mutex
    def _locked(self, fn):
        r = win32event.WaitForSingleObject(self.mx, 5000)
        if r not in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
            raise RuntimeError("the bouncer's lock is stuck")
        try:
            st = self._load()
            out, nudges = fn(st)
            try:
                self._save(st)
            except Exception as e:  # unwritable (a folder or a locked file at its name): never stuck, it fails OPEN
                log(f"could not save the lease ({type(e).__name__}): the bouncer is not coordinating until fixed")
        finally:
            win32event.ReleaseMutex(self.mx)
        for n in nudges:  # outside the lock: a slow nudge never holds the door
            _nudge(n)
        return out

    @staticmethod
    def _load() -> dict:
        """The state, or (missing, unreadable, or ANY wrong shape) an empty one: the hands are free, never stuck."""
        try:
            raw = json.load(open(FILE, encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError
            holder, line, res = raw.get("holder"), raw.get("line", []), raw.get("reserved")
            if not (holder is None or _entry_ok(holder, ("since", "last"))):
                raise ValueError
            if not (res is None or _entry_ok(res, ("until",))):
                raise ValueError
            if not (isinstance(line, list) and all(_entry_ok(w, ("since",)) for w in line)):
                raise ValueError
            return {"holder": holder, "line": line, "reserved": res}
        except Exception:
            return {"holder": None, "line": [], "reserved": None}

    @staticmethod
    def _save(st: dict) -> None:
        _atomic_write(FILE, json.dumps(st, ensure_ascii=False, indent=1))

    # ------------------------------------------------------------ the rules
    def _sweep(self, st: dict) -> list[dict]:
        """Free a dead or silent holder, expire an unclaimed reservation, drop dead waiters; return the nudges owed."""
        now = self.now()
        nudges = []
        st["line"] = [w for w in st["line"] if _alive(w.get("pid", 0))]
        h = st["holder"]
        # `since` only has to be possible (not in the future): a holder working for days is still working
        if h and (not _alive(h["pid"]) or not _sane(h["last"], now) or not (0 < h["since"] <= now + CLOCK_SLACK)
                  or now - h["last"] > SILENT_S):
            why = "its process ended" if not _alive(h["pid"]) else f"went quiet for {SILENT_S // 60} min"
            st["holder"] = None
            nudges += self._hand_on(st, f"{h['house'].upper()} session {h['id']} {why}")
        r = st["reserved"]
        if r and (not _sane(r["until"], now, RESERVE_S + CLOCK_SLACK) or now > r["until"]) and not st["holder"]:
            st["reserved"] = None
            nudges += self._hand_on(st, f"{r['house'].upper()} session {r['id']} didn't come for them")
        return nudges

    def _hand_on(self, st: dict, why: str) -> list[dict]:
        if not st["line"]:
            st["reserved"] = None
            return []
        nxt = st["line"].pop(0)
        st["reserved"] = {**nxt, "until": self.now() + RESERVE_S}
        return [{"house": nxt["house"], "id": nxt["id"],
                 "message": f"There you go, have your hands back ({why}). They're held for you for {RESERVE_S} s."}]

    def _is_me(self, e: dict | None) -> bool:
        """Me = my own server process. The id is a label others can read in a refusal and copy, so it never proves
        who someone is; the pid does (and lets my id sharpen from 'pid…' to the session id mid-hold)."""
        return bool(e) and e.get("pid") == self.me.pid

    def acquire(self, purpose: str = "") -> tuple[bool, str]:
        """Take or renew the lease. (True, "") = go ahead; (False, why) = wait, with who holds it and my place."""
        me = self.me

        def fn(st):
            nudges = self._sweep(st)
            now = self.now()
            h = st["holder"]
            if self._is_me(h):
                h.update(id=me.id, last=now, pid=me.pid, purpose=purpose or h.get("purpose", ""))
                return (True, ""), nudges
            r = st["reserved"]
            if not h and (not r or self._is_me(r)):
                st["holder"] = {"house": me.house, "id": me.id, "pid": me.pid, "since": now, "last": now,
                                "purpose": purpose}
                st["reserved"] = None
                st["line"] = [w for w in st["line"] if not self._is_me(w)]
                return (True, ""), nudges
            if not any(self._is_me(w) for w in st["line"]):
                st["line"].append({"house": me.house, "id": me.id, "pid": me.pid, "since": now})
            place = 1 + [i for i, w in enumerate(st["line"]) if self._is_me(w)][0]
            if h:
                who = (f"{h['house'].upper()} session {h['id']} is holding the hands since {_hhmm(h['since'])} "
                       f"(last move {int(now - h['last'])} s ago{', ' + h['purpose'] if h.get('purpose') else ''})")
            else:
                who = f"the hands are held {int(r['until'] - now)} s more for {r['house'].upper()} session {r['id']}, who was first in line"
            why = (f"Hold up: {who}. You're #{place} in line; I'll nudge you the moment they're free "
                   f"(a silent holder lets go after {SILENT_S // 60} min).")
            return (False, why), nudges

        return self._locked(fn)

    def release(self, why: str = "") -> bool:
        me = self.me

        def fn(st):
            mine = self._is_me(st["holder"])
            st["line"] = [w for w in st["line"] if not self._is_me(w)]
            if not mine:
                return False, self._sweep(st)
            st["holder"] = None
            return True, self._hand_on(st, why or f"{me.label()} is done")

        return self._locked(fn)

    def stopped(self) -> None:
        """The person's Stop: the hands are freed and nobody is nudged. The line stays; each asks again in its own time."""
        def fn(st):
            st["holder"] = None
            st["reserved"] = None
            return None, []
        self._locked(fn)

    def sweep(self) -> None:
        self._locked(lambda st: (None, self._sweep(st)))

    def status(self) -> dict:
        return self._locked(lambda st: (json.loads(json.dumps(st)), self._sweep(st)))


SHELLS = {"cmd.exe", "powershell.exe", "pwsh.exe", "wscript.exe", "cscript.exe", "mshta.exe", "bash.exe", "wsl.exe",
          "sh.exe", "conhost.exe", "rundll32.exe", "regsvr32.exe", "forfiles.exe", "explorer.exe"}


def exe_cmd(cmd) -> list[str] | None:
    """A configured command Hands may run: a list of strings whose first item is an existing .exe that is not a
    shell or script host. A .bat / .cmd, or cmd.exe /c, would re-parse the arguments, and the nudge's argument
    carries text from another session: it must reach a program as one plain argument, never as shell input."""
    if not (isinstance(cmd, list) and cmd and all(isinstance(x, str) for x in cmd)):
        return None
    exe = os.path.expandvars(cmd[0])
    if not (exe.lower().endswith(".exe") and os.path.isfile(exe)):
        return None
    if os.path.basename(exe).lower() in SHELLS:
        return None
    return [exe, *cmd[1:]]


def _atomic_write(path: str, text: str) -> None:
    """Write via a fresh, uniquely named temp file (a planted file, link or folder at a fixed name can't block or
    redirect it), then an atomic replace. Raises if the target can't be replaced; callers decide what that means."""
    import secrets
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, f".{os.path.basename(path)}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    with open(tmp, "x", encoding="utf-8") as fh:
        fh.write(text)
    try:
        # no attribute changes on the target: a hard link would carry them to a file outside state/
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        finally:
            raise


def log(line: str) -> None:
    """One line in state/bouncer.log: what went wrong where nobody was watching."""
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(os.path.join(STATE, "bouncer.log"), "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except Exception:
        pass


def _nudge(n: dict) -> None:
    try:
        raw = json.load(open(CONFIG, encoding="utf-8-sig")).get("nudge_cmd")
    except Exception:
        raw = None
    cmd = exe_cmd(raw)
    if raw is not None and cmd is None:
        log(f"nudge for {n.get('house')}:{n.get('id')} not sent: nudge_cmd must start with an existing .exe")
        return
    if cmd is None:
        return
    # The nudge's text was chosen by another session (its id), so it NEVER travels on a command line, where a shell
    # (even a renamed one) would read it as commands. It goes into a file Hands names itself; the command gets only
    # that path, which is hex and backslashes. The receiver reads the JSON from the file and deletes it.
    import secrets
    out = os.path.join(STATE, "nudges-out")
    path = os.path.join(out, f"nudge-{secrets.token_hex(8)}.json")
    try:
        os.makedirs(out, exist_ok=True)
        with open(path, "x", encoding="utf-8") as fh:
            json.dump(n, fh, ensure_ascii=False)
        # stdin from nothing: inside the MCP server stdin IS the protocol pipe, and a child must never inherit it
        r = subprocess.run([*cmd, path], stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=20, creationflags=0x08000000)
        if r.returncode != 0:
            log(f"nudge for {n.get('house')}:{n.get('id')} failed: exit {r.returncode}")
    except Exception as e:
        log(f"nudge for {n.get('house')}:{n.get('id')} failed: {type(e).__name__}")
