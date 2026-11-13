"""Hardening found by the pre-release review, each pinned.
HD1: consent.json grants only from a list of strings: a dict's keys, a string's letters, non-strings are never
     grants, and a later "always" grant still saves (no TypeError in sorted()).
HD2: config.json nested deep enough to overflow the parser leaves identity and settings at their defaults.
HD3: the Claude-app door's notify command must be an existing .exe (a .bat is refused, the door stays shut)."""
import json, os, sys, tempfile
from pathlib import Path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from engine import identity, policy

res = {}
tmp = Path(tempfile.mkdtemp(prefix="hands-hardening-"))

# HD1
orig_state, orig_file = policy.STATE, policy.CONSENT_FILE
policy.STATE, policy.CONSENT_FILE = tmp, tmp / "consent.json"
hd1 = []
for bad in ({"always": {"screen:all": False}}, {"always": "screen:all"}, {"always": [1, None, ""]}, ["screen:all"]):
    policy.CONSENT_FILE.write_text(json.dumps(bad), encoding="utf-8")
    c = policy.Consent()
    hd1.append(not c.allowed("screen:all") and not c.allowed("s") and c.always == set())
policy.CONSENT_FILE.write_text(json.dumps({"always": [1, "site:example.com"]}), encoding="utf-8")
c = policy.Consent()
try:  # one bad entry costs only itself: the real grant survives and the next "always" still saves
    c.grant("app:x", "always")
    saved = json.loads(policy.CONSENT_FILE.read_text(encoding="utf-8"))["always"]
    hd1.append(saved == ["app:x", "site:example.com"])
except Exception:
    hd1.append(False)
# a planted hard link at consent.tmp never carries the write outside state/ (Sköll K4)
outside = tmp / "outside.txt"
outside.write_text("untouched", encoding="utf-8")
link = policy.CONSENT_FILE.with_suffix(".tmp")
if link.exists():
    link.unlink()
os.link(outside, link)
c.grant("app:y", "always")
hd1.append(outside.read_text(encoding="utf-8") == "untouched")
# and a planted FOLDER at that name never jams an "Always" grant (Sköll N3)
link.unlink()
link.mkdir()
try:
    c.grant("app:z", "always")
    hd1.append("app:z" in json.loads(policy.CONSENT_FILE.read_text(encoding="utf-8"))["always"])
except Exception:
    hd1.append(False)
policy.STATE, policy.CONSENT_FILE = orig_state, orig_file
res["HD1"] = all(hd1)

# HD2
orig_cfg = identity.CONFIG
identity.CONFIG = str(tmp / "config.json")
Path(identity.CONFIG).write_text("{\"identity\": " + "[" * 20000 + "]" * 20000 + "}", encoding="utf-8")
try:
    res["HD2"] = identity.get() == identity.DEFAULTS and identity.setting("screen_reader_flag", False) is False
except Exception:
    res["HD2"] = False
identity.CONFIG = orig_cfg

# HD3
from engine import claude_door
orig_door_cfg = claude_door.CONFIG
claude_door.CONFIG = str(tmp / "door.json")
bat = tmp / "notify.bat"
bat.write_text("@echo off\r\n", encoding="utf-8")
Path(claude_door.CONFIG).write_text(json.dumps({"notify_cmd": [str(bat), "x"]}), encoding="utf-8")
shut_bat = claude_door.notify_cmd() is None
Path(claude_door.CONFIG).write_text(json.dumps({"notify_cmd": [sys.executable, "-c", "pass"]}), encoding="utf-8")
open_exe = claude_door.notify_cmd() == [sys.executable, "-c", "pass"]
claude_door.CONFIG = orig_door_cfg
res["HD3"] = shut_bat and open_exe

# HD4: a password-manager host can't slip past the fence with trailing / ideographic / full-width dots or full-width
# digits, nor with an over-long label that makes IDNA fail (Sköll K5)
spellings = ["https://vault.bitwarden.com。。/", "https://1password.com../", "https://1password.com.。/",
             "https://1password.com。/", "https://1password.com｡/", "https://1password.com．/",
             "https://" + "a" * 64 + ".１password.com/", "https://VAULT.BITWARDEN.COM./"]
hd4 = []
for u in spellings:
    try:
        policy.check_url(u, opening=True)
        hd4.append(False)
    except Exception as e:
        hd4.append(type(e).__name__ == "Denied")
try:
    policy.check_url("https://en.wikipedia.org/wiki/Wolf", opening=True)
except Exception:
    hd4.append(False)  # an ordinary site must stay open
res["HD4"] = all(hd4)

# HD5: the Claude-app door presses nothing when the notice to the owner fails (Sköll K7)
pressed = []
class FakeBtn:
    def GetInvokePattern(self):
        class P:
            def Invoke(self, waitTime=0):
                pressed.append(1)
        return P()
orig = (claude_door._app_window, claude_door._find, claude_door.auto.ControlFromHandle, claude_door.notify_cmd)
claude_door._app_window = lambda: 0
claude_door.auto.ControlFromHandle = lambda h: None
claude_door._find = lambda root, want: [FakeBtn()]
claude_door.notify_cmd = lambda: [sys.executable, "-c", "import sys; sys.exit(3)"]
try:
    claude_door.run("new_session")
    hd5 = False
except Exception as e:
    hd5 = "stayed shut" in str(e) and not pressed
claude_door.notify_cmd = lambda: [sys.executable, "-c", "pass"]
try:
    out = claude_door.run("new_session")
    hd5 = hd5 and pressed == [1] and "pressed New" in out
except Exception:
    hd5 = False
claude_door._app_window, claude_door._find, claude_door.auto.ControlFromHandle, claude_door.notify_cmd = orig
res["HD5"] = hd5

# HD6: consent is for one program, not a file name; a renamed shell is still a shell; network paths never launch
import shutil as _sh
fake_dir = tmp / "elsewhere"
fake_dir.mkdir()
renamed = fake_dir / "notashell.exe"
_sh.copy2(os.path.join(os.environ["WINDIR"], "System32", "cmd.exe"), renamed)
k1 = policy.consent_key(r"C:\Windows\System32\notepad.exe", "")
k2 = policy.consent_key(str(fake_dir / "notepad.exe"), "")
kpkg1 = policy.consent_key(r"C:\Program Files\WindowsApps\Microsoft.WindowsNotepad_11.2510.6.0_x64__8wekyb3d8bbwe\Notepad\Notepad.exe", "")
kpkg2 = policy.consent_key(r"C:\Program Files\WindowsApps\Microsoft.WindowsNotepad_11.2601.1.0_x64__8wekyb3d8bbwe\Notepad\Notepad.exe", "")
hd6 = [k1 != k2, kpkg1 == kpkg2 == "pkg:microsoft.windowsnotepad__8wekyb3d8bbwe",
       policy.consent_key("", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App") == "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"]
for call in (lambda: policy.check_launch_target(str(renamed)),
             lambda: policy.check_denied(str(renamed), "", "x", "x"),
             lambda: policy.check_launch_target(r"\\server\share\tool.exe"),
             lambda: policy.check_launch_target("//server/share/tool.exe")):
    try:
        call()
        hd6.append(False)
    except Exception as e:
        hd6.append(type(e).__name__ == "Denied")
try:
    policy.check_launch_target(r"C:\Windows\System32\notepad.exe")
    hd6.append(True)  # an ordinary program still launches
except Exception:
    hd6.append(False)
# (round 2 of the review) a fake WindowsApps folder gets no package identity; every network spelling, a URL and an
# alternate data stream are refused; a swapped file is re-read, never served from the cache
fake_pkg = tmp / "WindowsApps" / "Microsoft.WindowsNotepad_1.0.0.0_x64__8wekyb3d8bbwe"
fake_pkg.mkdir(parents=True)
hd6.append(policy.consent_key(str(fake_pkg / "Notepad.exe"), "").startswith("pkg:") is False)
for bad in ("\\/server/share/t.exe", "/\\server\\share\\t.exe", "\\\\?\\UNC\\server\\share\\t.exe",
            "\\\\?\\C:\\Windows\\System32\\notepad.exe", "file:///C:/Windows/System32/notepad.exe",
            "C:\\Windows\\System32\\notepad.exe:evil.exe", "\\??\\C:\\Windows\\System32\\notepad.exe",
            "\\\\.\\C:\\Windows\\System32\\notepad.exe", "\\Windows\\System32\\notepad.exe", "tool.exe",
            "C:\\does\\not\\exist.exe"):
    try:
        policy.check_launch_target(bad)
        hd6.append(False)
    except Exception as e:
        hd6.append(type(e).__name__ == "Denied")
# a junction on a local drive that leads somewhere else is judged by where it leads (realpath), and a normal
# local file still launches
jdir = tmp / "jdir"
try:  # no shell: the junction is made through the Windows API directly
    import _winapi
    _winapi.CreateJunction(os.path.join(os.environ["WINDIR"], "System32"), str(jdir))
    junction_ok = True
except OSError:
    junction_ok = False
if junction_ok:
    hd6.append(policy.local_path_problem(str(jdir / "notepad.exe")) is None)  # local target: fine
hd6.append(policy.local_path_problem(os.path.join(os.environ["WINDIR"], "System32", "notepad.exe")) is None)
hd6.append(policy.local_path_problem("Microsoft.WindowsCalculator_8wekyb3d8bbwe!App") is None)
swap = tmp / "swap.exe"
_sh.copy2(os.path.join(os.environ["WINDIR"], "System32", "notepad.exe"), swap)
first = policy.original_name(str(swap))
import time as _t
_t.sleep(0.05)
_sh.copy2(os.path.join(os.environ["WINDIR"], "System32", "cmd.exe"), swap)
os.utime(swap, None)
hd6.append(first == "notepad.exe" and policy.original_name(str(swap)) == "cmd.exe")
res["HD6"] = all(hd6)

# HD8: a bare "name.exe" is resolved to ONE absolute file before the deny check, consent and launch
from engine import session as _s8
calls = []
H8 = _s8.Hands(overlay=False)
orig8 = (_s8.policy.check_launch_target, _s8.gate_consent)
_s8.policy.check_launch_target = lambda p: (calls.append(("check", p)), (_ for _ in ()).throw(policy.Denied("stop")))[0]
try:
    H8.launch("notepad.exe")
except Exception:
    pass
try:
    H8.launch("definitely-not-on-path-1234.exe")
    unresolved_refused = False
except Exception as e:
    unresolved_refused = "isn't on this machine's PATH" in str(e)
_s8.policy.check_launch_target, _s8.gate_consent = orig8
res["HD8"] = (bool(calls) and os.path.isabs(calls[0][1]) and calls[0][1].lower().endswith("notepad.exe")
              and unresolved_refused)

# HD7: real input never goes to a blocking dialog that failed the deny list / consent (it's the person's)
from engine import session as _s
import win32gui as _wg
H = _s.Hands(overlay=False)
fired = []
class _W:
    id = 100
saved = (_wg.IsWindowEnabled, _s.win.popups_of, H._popups, H._wait_her_idle)
_wg.IsWindowEnabled = lambda h: h != 100          # the window is disabled by a modal dialog...
_s.win.popups_of = lambda h: [200]                  # ...owned popup 200 exists...
H._popups = lambda h: []                            # ...but it did NOT pass deny / consent
H._wait_her_idle = lambda: None
try:
    H._foreground(_W(), lambda: fired.append(1))
    hd7 = False
except Exception as e:
    hd7 = "may not touch" in str(e) and not fired
finally:
    _wg.IsWindowEnabled, _s.win.popups_of, H._popups, H._wait_her_idle = saved
res["HD7"] = hd7

for k, v in res.items():
    print(f"{k}: {'PASS' if v else 'FAIL'}")
print("SUMMARY " + " ".join(f"{k}:{'PASS' if v else 'FAIL'}" for k, v in res.items()))
sys.exit(0 if all(res.values()) else 1)
