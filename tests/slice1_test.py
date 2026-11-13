"""Slice 1 criteria A, F, G, H, I as asserting rows (each prints PASS/FAIL; exit 1 if any fails).
Own windows only: a Calculator this test opens and a console window it starts itself; never the person's.
E (foreground fallback while the person types) needs a person's real hands: a manual check, reported by tests\\run.py.

    python -X utf8 tests\\slice1_test.py
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ["HANDS_TEST_HOOKS"] = "1"

import uiautomation as auto  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402

from engine import policy, session, win  # noqa: E402

CALC = "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
RESULTS: dict[str, tuple[bool, str]] = {}


def row(k: str, ok: bool, evidence: str) -> None:
    RESULTS[k] = (bool(ok), evidence)
    print(f"{k}: {'PASS' if ok else 'FAIL'} — {evidence}", flush=True)


def cloaked(hwnd: int) -> bool:
    v = ctypes.c_int(0)
    ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(v), ctypes.sizeof(v))  # DWMWA_CLOAKED
    return v.value != 0


def pink_pixels(region) -> int:
    import mss
    with mss.mss() as m:
        shot = m.grab(region)
    n = 0
    for (b, g, r, _a) in zip(*[iter(shot.raw)] * 4):
        if r > 200 and g < 110 and 90 <= b <= 200 and r - g > 120:
            n += 1
    return n


def raises(fn, exc) -> tuple[bool, str]:
    try:
        fn()
    except exc as e:
        return True, str(e)[:200]
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:70]}"
    return False, "no refusal"


def shared_rules() -> None:
    """X: the rules both surfaces share live once (engine/common.py) and behave the same. No windows involved."""
    from engine import browser, common
    bad = {"wait 'x'": {"wait": "x"}, "wait inf": {"wait": float("inf")}, "wait 9": {"wait": 9},
           "set 20001": {"set": 1, "value": "x" * 20001}, "count True": {"click": 1, "count": True},
           "count 2.0": {"click": 1, "count": 2.0}, "count 4": {"click": 1, "count": 4}, "dy 51": {"scroll": 1, "dy": 51}}
    refused = {k: raises(lambda a=a: common.check_action(a), common.HandsError)[0] for k, a in bad.items()}
    then_r = raises(lambda: common.check_then("everything"), common.HandsError)[0]
    same = session.check_action is common.check_action and browser.check_action is common.check_action         and session.TEXT_CAP == browser.TEXT_CAP == common.TEXT_CAP

    class _Ov:
        def ask_consent(self, *a):
            return "no_overlay"

    class _H:
        use_overlay = True
        ov = _Ov()
        consent = policy.Consent()
    _H.consent.session.clear()
    _H.consent.always.discard("x-key")
    dead = raises(lambda: common.gate_consent(_H, "x-key", "x", "x"), common.NeedsConsent)
    env = {k: v for k, v in os.environ.items() if k != "HANDS_TEST_HOOKS"}
    never = "Hands.TestOnly.NeverGranted_0000!App"  # a key nobody can have granted for real (Calculator may be)
    env["HANDS_TEST_ALLOW"] = never
    gated = subprocess.run([sys.executable, "-X", "utf8", "-c",
                            "import sys; sys.path.insert(0, r'%s'); from engine import session; "
                            "print(session.Hands(overlay=False).consent.allowed(%r))" % (ROOT, never)],
                           capture_output=True, text=True, env=env, timeout=60).stdout.strip()
    sys.path.insert(0, ROOT)
    import mcp_server as door  # the audit log's redaction: URLs rebuilt from parsed parts; typed/searched/pressed = length
    bs = chr(92)  # Sköll's redact_probe cases, pinned: Chrome reads \ as / in special schemes, strips tab/CR/LF
    probe_urls = ["http://a:b@c@host/x?q#f", "http://user:pass@host/p", "HTTPS://u:p@[::1]:8/x?q", "http://host/x;jsessionid=SECRETSEMI",
                  "http://host/%3Fq=SECRETPCT", "javascript:alert(SECRETJS)", "http://host:99999/x?SECRETPORT", "//u:SECRETREL@host/x",
                  "http://host/x" + bs + "?SECRETBS", "http:/" + bs + "u:SECRETBS2@host/", "http://u:SECRETTAB	@host/",
                  "data:text/html,SECRETDATA", "http://host/x#SECRETFRAG", "  http://u:SECRETSP@host/"]
    logged = json.dumps([door._redact({"url": u, "goto": u}) for u in probe_urls + [
                          "https://user:pass@site.com:8443/p?t=1#x", "http://user:pass@/x", "ht!tp://x"]]
                        + [door._redact({"actions": [{"key": "K", "into": 3}, {"type": {"x": "SECRETNESTED"}}]}),
                           door._scrub("note http://u:SECRETNOTE@h/x?q=1 and HTTP://h/y?SECRETUP and https://h/z#SECRETF2 and "
                                       "http:/" + bs + "u:SECRETBS3@h/")]
                        + [door._redact({"find": "SKFIND3", "key": "S", "actions": [{"type": "SKTYPE", "goto": "http://u:SKPASS7@h/p?q"}]}),
                           door._scrub("failed at http://a:b@c@host/x?q#f")])
    leaks = [w for w in ("a:b", "user:pass", "SKPASS7", "SKFIND3", "SKTYPE", "?q", "#f", "t=1", "SECRET") if w in logged]
    redact_ok = not leaks and "http://host/x" in logged and "https://site.com:8443/p" in logged and "<unparseable url>" in logged
    row("X", all(refused.values()) and then_r and same and dead[0] and "overlay isn't running" in dead[1] and gated == "False"
        and redact_ok,
        f"refused {sum(refused.values())}/{len(refused)} bad actions, unknown then refused={then_r}; one helper + one "
        f"TEXT_CAP on both surfaces={same}; dead overlay -> {dead[1][:60]!r}; HANDS_TEST_ALLOW without test hooks "
        f"allows calc={gated}; log redaction leaks={leaks or 'none'}: {logged[:160]}")


def main() -> int:
    shared_rules()
    H = session.Hands(overlay=False)
    H.consent.session.add(CALC)
    calc = H.launch("calc")["id"]
    tmp = tempfile.mkdtemp(prefix="hands-s1-")
    con = None
    try:
        # ---------------- A: the real windows, filtered; queries; warm timing
        H.windows()
        t = time.perf_counter()
        ws = H.windows()
        ms = (time.perf_counter() - t) * 1000
        bad = [w for w in ws if not win32gui.IsWindowVisible(w["id"]) or cloaked(w["id"])
               or win32gui.GetWindowLong(w["id"], win32con.GWL_EXSTYLE) & win32con.WS_EX_TOOLWINDOW
               or (not w.get("minimized") and (lambda r: r[2] - r[0] <= 0 or r[3] - r[1] <= 0)(win32gui.GetWindowRect(w["id"])))]
        fields = all(all(k in w for k in ("id", "exe", "app", "title", "minimized")) for w in ws)
        q = [w["id"] for w in H.windows("Microsoft.WindowsCalculator")]
        none = H.windows("zz-no-such-window-zz")
        row("A", ms < 150 and not bad and fields and calc in q and none == [],
            f"{len(ws)} windows in {ms:.0f} ms (warm); cloaked/tool/0-size listed: {len(bad)}; fields ok={fields}; "
            f"query by app id finds my Calculator={calc in q}; nonsense query -> {none}")

        # ---------------- F: refusals at engine level (launch + look/act), never a silent no-op
        bat = os.path.join(tmp, "x.bat")
        ps1 = os.path.join(tmp, "x.ps1")
        for f in (bat, ps1):
            open(f, "w").write("rem never run\n")
        sysdir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
        launches = {name: raises(lambda n=name: H.launch(n), policy.Denied) for name in (
            os.path.join(sysdir, "cmd.exe"), os.path.join(sysdir, "conhost.exe"),
            os.path.join(sysdir, "WindowsPowerShell", "v1.0", "powershell.exe"), bat, ps1,
            "microsoft.windowsterminal_8wekyb3d8bbwe!App", "Claude_pzs8sxrjxfjjc!Claude", "OPENAI.CODEX_abc!App")}
        units = {exe: raises(lambda e=exe: policy.check_denied(e, "", "", ""), policy.Denied) for exe in (
            r"C:\x\claude.exe", r"C:\x\WindowsTerminal.exe", r"C:\x\pwsh.exe", r"C:\x\mintty.exe",
            r"C:\Windows\System32\consent.exe", r"C:\x\1Password.exe", r"C:\x\CredentialUIBroker.exe")}
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 7  # minimized, never activated: it is my window, not something on the person's screen
        con = subprocess.Popen([os.path.join(sysdir, "conhost.exe"), "cmd.exe", "/k", "title hands-deny-test"],
                               startupinfo=si, creationflags=subprocess.CREATE_NEW_CONSOLE)
        hwnd = 0
        for _ in range(50):
            hwnd = win32gui.FindWindow(None, "hands-deny-test")
            if hwnd:
                break
            time.sleep(0.1)
        look_r = raises(lambda: H.look(hwnd), policy.Denied) if hwnd else (False, "my console never appeared")
        H.looks[hwnd] = H.looks.get(calc)  # even with a look in hand, act on it refuses
        act_r = raises(lambda: H.act(hwnd, [{"key": "a"}], then="none", look_id=None), policy.Denied) if hwnd else (False, "-")
        okF = all(v[0] for v in launches.values()) and all(v[0] for v in units.values()) and look_r[0] and act_r[0]
        row("F", okF, f"launch refusals {sum(v[0] for v in launches.values())}/{len(launches)}; deny-list units "
                      f"{sum(v[0] for v in units.values())}/{len(units)}; look on my console -> {look_r}; act -> {act_r}"
                      + ("" if okF else f"; misses: {[k for k, v in {**launches, **units}.items() if not v[0]]}"))

        # ---------------- G: headless needs_consent; "always" survives a new Consent()
        H2 = session.Hands(overlay=False)
        H2.consent.always.discard(CALC)
        H2.consent.session.discard(CALC)
        g1 = raises(lambda: H2.look(calc), session.NeedsConsent)
        cf = policy.CONSENT_FILE
        before = cf.read_bytes() if cf.exists() else None
        try:
            c = policy.Consent()
            c.grant("hands-test-always-key", "always")
            survives = "hands-test-always-key" in policy.Consent().always
        finally:
            if before is None:
                cf.unlink(missing_ok=True)
            else:
                cf.write_bytes(before)
        restored = (cf.read_bytes() if cf.exists() else None) == before
        row("G", g1[0] and "needs_consent" in g1[1] and survives and restored,
            f"headless look -> {g1}; 'always' seen by a new Consent()={survives}; consent.json restored byte-identical={restored}")

        # ---------------- H: a stop mid-batch and in the last action; refused until hands_look; overlay gone on exit
        H.look(calc)
        idx = {n.aid: n.index for n in H.looks[calc].nodes if n.aid}
        plan = [{"click": idx[a]} for a in ("clearButton", "num1Button", "num2Button", "num3Button")]
        h_ok = []
        for stop_at in (2, len(plan)):
            H.look(calc)
            orig = H._do
            n = [0]

            def stopping(w, lk, a, orig=orig, n=n, stop_at=stop_at):
                r = orig(w, lk, a)
                n[0] += 1
                if n[0] == stop_at:
                    H.ov.stop_requested = True  # as the overlay reports Esc×2 / the pill's Stop
                return r
            H._do = stopping
            try:
                out, _ = H.act(calc, plan, then="none", look_id=H.looks[calc].id)
            finally:
                H._do = orig
            refused_next = raises(lambda: H.act(calc, [{"click": idx["num4Button"]}], then="none", look_id=H.looks[calc].id),
                                  session.Stopped)
            H.look(calc)
            back = "clicked" in H.act(calc, [{"click": idx["clearButton"]}], then="none", look_id=H.looks[calc].id)[0]
            h_ok.append((stop_at, "STOPPED" in out and out.count("clicked") == stop_at, refused_next[0], back))
        exit_ms = asyncio.run(overlay_exit_ms())
        row("H", all(a and b and c for (_, a, b, c) in h_ok) and exit_ms is not None and exit_ms <= 1500,
            f"(stop at action, STOPPED with only the actions before it, next act refused, hands_look resumes) = {h_ok}; "
            f"overlay process gone {exit_ms} ms after the server's stdin closed")

        # ---------------- I: with the overlay up, a real screen grab of my window's region has none of its pink
        def grab_with(proof: bool) -> int:
            if proof:
                os.environ["HANDS_PROOF"] = "1"
            try:
                HI = session.Hands(overlay=True)
                HI.consent.session.add(CALC)
                HI.ov.ensure()
                HI.look(calc)
                time.sleep(0.8)
                l, t_, r, b = win.frame_rect(calc)
                n = pink_pixels({"left": l - 14, "top": t_ - 14, "width": r - l + 28, "height": b - t_ + 28})
                HI.ov.close()
                return n
            finally:
                os.environ.pop("HANDS_PROOF", None)
        normal = grab_with(False)
        proof = grab_with(True)
        row("I", normal == 0 and proof > 50,
            f"pink px in a real grab: overlay as it normally runs = {normal}; same with the proof switch (visible to capture) = {proof}")
    finally:
        try:
            auto.ControlFromHandle(calc).GetWindowPattern().Close()
        except Exception:
            pass
        if con is not None:
            subprocess.run(["taskkill", "/PID", str(con.pid), "/T", "/F"], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
    order = ["A", "F", "G", "H", "I", "X"]
    print("\nSUMMARY " + " ".join(f"{k}:{'PASS' if RESULTS.get(k, (False,))[0] else 'FAIL'}" for k in order))
    return 0 if all(RESULTS.get(k, (False,))[0] for k in order) else 1


async def overlay_exit_ms():
    """Start the real door with the overlay, find its overlay.exe, close the door, time until that process is gone."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    import win32com.client
    exe = os.path.join(ROOT, "overlay", "overlay.exe").lower()

    def overlay_pids():
        wmi = win32com.client.GetObject("winmgmts:")
        return {int(p.ProcessId) for p in wmi.ExecQuery("select ProcessId, ExecutablePath from Win32_Process where Name='overlay.exe'")
                if (p.ExecutablePath or "").lower() == exe}
    before = overlay_pids()
    env = {k: v for k, v in os.environ.items() if k not in ("HANDS_HEADLESS",)}
    params = StdioServerParameters(command=sys.executable, args=["-X", "utf8", os.path.join(ROOT, "mcp_server.py")], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            await s.call_tool("hands_windows", {})
            mine = overlay_pids() - before
    t = time.perf_counter()
    while time.perf_counter() - t < 5:
        if mine and not (mine & overlay_pids()):
            return round((time.perf_counter() - t) * 1000)
        time.sleep(0.05)
    return None


if __name__ == "__main__":
    sys.exit(main())
