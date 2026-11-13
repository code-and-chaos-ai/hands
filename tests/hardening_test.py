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

for k, v in res.items():
    print(f"{k}: {'PASS' if v else 'FAIL'}")
print("SUMMARY " + " ".join(f"{k}:{'PASS' if v else 'FAIL'}" for k, v in res.items()))
sys.exit(0 if all(res.values()) else 1)
