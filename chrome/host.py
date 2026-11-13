"""Native messaging host: Chrome (stdio, 4-byte LE length + JSON) <-> a per-user named pipe.

Chrome starts this when the extension calls connectNative("com.kincaid.hands"). It owns the pipe
\\\\.\\pipe\\rowan-hands-chrome (override: HANDS_CHROME_PIPE, tests only). Clients (engine/browser.py) must
prove the shared authkey in state\\chrome.key (HMAC challenge both ways) before a byte of JSON is read.
Never a TCP port. Never pickle: JSON bytes only, typed at the seam. Remote (SMB) pipe clients are rejected.
The authkey is never logged.
"""
from __future__ import annotations

import itertools
import json
import os
import struct
import sys
import threading
import time
from multiprocessing import connection as mpc
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.chrome_key import EXTENSION_ID, PIPE_NAME, load_or_create_key, pipe_address  # noqa: E402

LOG = ROOT / "state" / "host.log"
# test hooks (debug.*) live only on a test pipe whose Chrome was started with HANDS_TEST_HOOKS=1: never on the person's
TEST_HOOKS = (os.environ.get("HANDS_TEST_HOOKS") == "1"
              and os.environ.get("HANDS_CHROME_PIPE", PIPE_NAME) not in ("", PIPE_NAME))
MAX_TO_CHROME = 1024 * 1024        # Chrome's cap for host -> extension messages
MAX_FROM_CLIENT = 1024 * 1024
AUTH_TIMEOUT = 5.0
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008


def log(msg: str) -> None:
    try:
        LOG.parent.mkdir(exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{os.getpid()}] {msg}\n")
    except Exception:
        pass


class LocalPipeListener(mpc.PipeListener):
    """multiprocessing's pipe listener, but local clients only."""

    def _new_handle(self, first=False):
        import _winapi
        flags = _winapi.PIPE_ACCESS_DUPLEX | _winapi.FILE_FLAG_OVERLAPPED
        if first:
            flags |= _winapi.FILE_FLAG_FIRST_PIPE_INSTANCE
        return _winapi.CreateNamedPipe(
            self._address, flags,
            _winapi.PIPE_TYPE_MESSAGE | _winapi.PIPE_READMODE_MESSAGE | _winapi.PIPE_WAIT
            | PIPE_REJECT_REMOTE_CLIENTS,
            _winapi.PIPE_UNLIMITED_INSTANCES, mpc.BUFSIZE, mpc.BUFSIZE,
            _winapi.NMPWAIT_WAIT_FOREVER, _winapi.NULL)


class Host:
    def __init__(self):
        self.key = load_or_create_key()
        self.out_lock = threading.Lock()
        self.ids = itertools.count(1)
        self.pending: dict[int, tuple[object, object]] = {}  # host id -> (conn, client id)
        self.clients: dict[int, object] = {}  # authenticated pipe clients (events from the extension reach all of them)
        self.cids = itertools.count(1)
        self.plock = threading.Lock()
        self.stdout = sys.stdout.buffer
        self.alive = True

    # ------------------------------------------------ Chrome side
    def to_chrome(self, msg: dict) -> None:
        data = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        if len(data) > MAX_TO_CHROME:
            raise ValueError("message to Chrome over 1 MB")
        with self.out_lock:
            self.stdout.write(struct.pack("<I", len(data)) + data)
            self.stdout.flush()

    def chrome_reader(self) -> None:
        inp = sys.stdin.buffer
        while True:
            head = inp.read(4)
            if len(head) < 4:
                break
            (n,) = struct.unpack("<I", head)
            body = inp.read(n)
            if len(body) < n:
                break
            try:
                msg = json.loads(body.decode("utf-8"))
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue
            if "id" not in msg and isinstance(msg.get("event"), str):  # e.g. the person cancelled the debugging bar
                ev = json.dumps({"event": msg["event"], "reason": str(msg.get("reason", ""))[:200]}).encode("utf-8")
                with self.plock:
                    targets = list(self.clients.values())
                for c in targets:
                    try:
                        c.send_bytes(ev)
                    except Exception:
                        pass
                continue
            hid = msg.get("id")
            if not isinstance(hid, int):
                continue
            with self.plock:
                route = self.pending.pop(hid, None)
            if not route:
                continue
            conn, cid = route
            reply = {"id": cid}
            if "error" in msg:
                reply["error"] = msg["error"]
            else:
                reply["result"] = msg.get("result")
            try:
                conn.send_bytes(json.dumps(reply, ensure_ascii=False).encode("utf-8"))
            except Exception:
                pass
        log("chrome closed the port; host exiting")
        self.alive = False
        os._exit(0)  # Chrome is gone or the extension let go: the pipe goes with us

    # ------------------------------------------------ pipe side
    def serve(self) -> None:
        addr = pipe_address()
        try:
            listener = LocalPipeListener(addr)
        except OSError as e:
            log(f"pipe busy ({e.winerror}): another host already serves; exiting")
            os._exit(3)
        log("pipe up")
        while self.alive:
            try:
                conn = listener.accept()
            except Exception as e:
                log(f"accept failed: {type(e).__name__}")
                time.sleep(0.2)
                continue
            threading.Thread(target=self.client, args=(conn,), daemon=True).start()

    def client(self, conn) -> None:
        authed = threading.Event()

        def watchdog():
            if not authed.wait(AUTH_TIMEOUT):
                try:
                    conn.close()
                except Exception:
                    pass
        threading.Thread(target=watchdog, daemon=True).start()
        try:
            mpc.deliver_challenge(conn, self.key)
            mpc.answer_challenge(conn, self.key)
        except Exception:
            log("client failed the authkey challenge; dropped")
            try:
                conn.close()
            except Exception:
                pass
            return
        authed.set()
        cid = next(self.cids)
        with self.plock:
            self.clients[cid] = conn
        try:
            while True:
                raw = conn.recv_bytes(MAX_FROM_CLIENT)
                try:
                    req = json.loads(raw.decode("utf-8"))
                except Exception:
                    continue
                if not (isinstance(req, dict) and isinstance(req.get("id"), int)
                        and isinstance(req.get("method"), str) and isinstance(req.get("params", {}), dict)):
                    continue
                if req["method"].startswith("debug.") and not TEST_HOOKS:
                    conn.send_bytes(json.dumps({"id": req["id"], "error": {
                        "code": "denied", "message": "test hooks are off"}}).encode())
                    continue
                if req["method"] == "host.ping":
                    conn.send_bytes(json.dumps({"id": req["id"], "result": {"pid": os.getpid()}}).encode())
                    continue
                hid = next(self.ids)
                with self.plock:
                    self.pending[hid] = (conn, req["id"])
                try:
                    self.to_chrome({"id": hid, "method": req["method"], "params": req.get("params", {}), "client": cid})
                except Exception as e:
                    with self.plock:
                        self.pending.pop(hid, None)
                    conn.send_bytes(json.dumps({"id": req["id"], "error": {
                        "code": "host", "message": str(e)}}).encode())
        except (EOFError, OSError):
            pass
        finally:
            with self.plock:
                for k in [k for k, v in self.pending.items() if v[0] is conn]:
                    self.pending.pop(k, None)
                self.clients.pop(cid, None)
            try:
                self.to_chrome({"gone": cid})  # its tabs become orphans; the next engine's hello releases them
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass


def main() -> None:
    origin = sys.argv[1] if len(sys.argv) > 1 else ""
    if origin.rstrip("/") != f"chrome-extension://{EXTENSION_ID}":
        log("refused: caller is not the Hands extension")
        sys.exit(2)
    log("started by the extension")
    h = Host()
    h.to_chrome({"hello": {"test": TEST_HOOKS, "host": os.getpid()}})  # the worker's test mode follows this alone
    threading.Thread(target=h.serve, daemon=True).start()
    h.chrome_reader()


if __name__ == "__main__":
    main()
