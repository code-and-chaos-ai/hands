"""Test-only: start a SEPARATE Chrome (temp profile) with our unpacked extension, through the one road that
works on branded Chrome 137+: --remote-debugging-pipe + --enable-unsafe-extension-debugging + CDP
Extensions.loadUnpacked. The pipe is two anonymous OS pipes handed to Chrome as CRT fds 3 and 4 (the
lpReserved2 handoff), so no TCP port is ever opened. Never touches the person's Chrome: own --user-data-dir, own PID.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import msvcrt
import os
import shutil
import subprocess
import tempfile
import threading
import time

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
TEMP_PREFIX = "hands-test-"

k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
                ("dwX", wt.DWORD), ("dwY", wt.DWORD), ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
                ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
                ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE),
                ("hStdError", wt.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE), ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


k32.CreateProcessW.argtypes = [wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL, wt.DWORD,
                               ctypes.c_void_p, wt.LPCWSTR, ctypes.POINTER(STARTUPINFOW),
                               ctypes.POINTER(PROCESS_INFORMATION)]
k32.CreateProcessW.restype = wt.BOOL


class PipeChrome:
    """A Chrome this test owns: its PID, its temp profile, its CDP pipe."""

    def __init__(self, extra_args: list[str] | None = None, exe: str = CHROME, env: dict | None = None,
                 show: int | None = 7):
        """show = the first window's ShowWindow state; 7 = SW_SHOWMINNOACTIVE (never takes the person's focus)."""
        self.user_data_dir = tempfile.mkdtemp(prefix=TEMP_PREFIX)
        r_in, w_in = os.pipe()    # we write commands -> Chrome reads fd 3
        r_out, w_out = os.pipe()  # Chrome writes fd 4 -> we read
        h3 = msvcrt.get_osfhandle(r_in)
        h4 = msvcrt.get_osfhandle(w_out)
        os.set_handle_inheritable(h3, True)
        os.set_handle_inheritable(h4, True)
        n = 5
        FOPEN, FPIPE = 0x01, 0x08
        ptr = ctypes.sizeof(ctypes.c_void_p)
        buf = ctypes.create_string_buffer(4 + n + n * ptr)
        ctypes.memmove(buf, ctypes.byref(ctypes.c_int(n)), 4)
        ctypes.memmove(ctypes.addressof(buf) + 4, bytes([0, 0, 0, FOPEN | FPIPE, FOPEN | FPIPE]), n)
        inval = ctypes.c_void_p(-1).value
        handles = (ctypes.c_void_p * n)(inval, inval, inval, h3, h4)
        ctypes.memmove(ctypes.addressof(buf) + 4 + n, handles, n * ptr)
        si = STARTUPINFOW()
        si.cb = ctypes.sizeof(si)
        if show is not None:
            si.dwFlags |= 0x1  # STARTF_USESHOWWINDOW
            si.wShowWindow = show
        si.cbReserved2 = len(buf)
        si.lpReserved2 = ctypes.addressof(buf)
        pi = PROCESS_INFORMATION()
        args = [exe, f"--user-data-dir={self.user_data_dir}", "--remote-debugging-pipe",
                "--enable-unsafe-extension-debugging", "--no-first-run", "--no-default-browser-check",
                "--disable-sync", "--disable-background-networking", "--disable-component-update",
                "--disable-default-apps", *(extra_args or [])]
        cmd = subprocess.list2cmdline(args)
        env_block = None
        if env is not None:
            block = "".join(f"{k}={v}\0" for k, v in sorted(env.items())) + "\0"
            env_block = ctypes.create_unicode_buffer(block)
        CREATE_UNICODE_ENVIRONMENT = 0x400
        ok = k32.CreateProcessW(None, cmd, None, None, True, CREATE_UNICODE_ENVIRONMENT if env_block else 0,
                                ctypes.addressof(env_block) if env_block else None, None,
                                ctypes.byref(si), ctypes.byref(pi))
        os.set_handle_inheritable(h3, False)
        os.set_handle_inheritable(h4, False)
        if not ok:
            raise OSError(ctypes.get_last_error(), "CreateProcessW failed")
        k32.CloseHandle(pi.hThread)
        k32.CloseHandle(pi.hProcess)
        self.pid = pi.dwProcessId
        os.close(r_in)   # Chrome holds its own copies now
        os.close(w_out)
        self._w = os.fdopen(w_in, "wb", buffering=0)
        self._r = os.fdopen(r_out, "rb", buffering=0)
        self._id = 0
        self._lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, dict]] = {}
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        buf = b""
        while True:
            try:
                chunk = self._r.read(65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\0" in buf:
                raw, buf = buf.split(b"\0", 1)
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                w = self._pending.get(msg.get("id", -1))
                if w:
                    w[1].update(msg)
                    w[0].set()

    def send(self, method: str, params: dict | None = None, timeout: float = 10.0, session: str | None = None) -> dict:
        with self._lock:
            self._id += 1
            i = self._id
        ev, box = threading.Event(), {}
        self._pending[i] = (ev, box)
        msg = {"id": i, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        with self._lock:
            self._w.write(json.dumps(msg).encode() + b"\0")
        if not ev.wait(timeout):
            raise TimeoutError(f"CDP {method} timed out")
        self._pending.pop(i, None)
        if "error" in box:
            raise RuntimeError(f"CDP {method}: {box['error']}")
        return box.get("result", {})

    def page_session(self, url_part: str) -> str:
        """A flat CDP session on the page target whose URL contains url_part (the test's own eyes)."""
        for t in self.send("Target.getTargets")["targetInfos"]:
            if t["type"] == "page" and url_part in t["url"]:
                return self.send("Target.attachToTarget", {"targetId": t["targetId"], "flatten": True})["sessionId"]
        raise LookupError(url_part)

    def evaluate(self, session: str, expr: str):
        r = self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True}, session=session)
        return r.get("result", {}).get("value")

    def shutdown(self) -> tuple[bool, bool]:
        """Close OUR browser (CDP Browser.close, then a kill of OUR pid's tree if it lingers), wait for it,
        remove OUR temp profile. Returns (gone, removed)."""
        try:
            self.send("Browser.close", timeout=3)
        except Exception:
            pass
        if not wait_gone(self.pid, 5.0):
            subprocess.run(["taskkill", "/PID", str(self.pid), "/T", "/F"], capture_output=True)
        for f in (self._w, self._r):
            try:
                f.close()
            except Exception:
                pass
        gone = wait_gone(self.pid)
        removed = False
        for _ in range(20):
            shutil.rmtree(self.user_data_dir, ignore_errors=True)
            if not os.path.exists(self.user_data_dir):
                removed = True
                break
            time.sleep(0.3)
        return gone, removed


def wait_gone(pid: int, timeout: float = 8.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        if f" {pid} " not in r.stdout:
            return True
        time.sleep(0.25)
    return False
