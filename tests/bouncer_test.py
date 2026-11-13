"""The bouncer, criterion by criterion (contract BC1-BC8), in its own test world (never the live lease).
Holders are real child processes (so liveness and cross-process locking are real); time is driven by a fake clock
where the rule is about time."""
import json, os, subprocess, sys, tempfile, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORLD = tempfile.mkdtemp(prefix="hands-bouncer-")
os.environ["HANDS_TEST_HOOKS"] = "1"
os.environ["HANDS_BOUNCER_DIR"] = WORLD
sys.path.insert(0, ROOT)
from engine import bouncer as B

NUDGES = os.path.join(WORLD, "nudges.jsonl")
json.dump({"nudge_cmd": [sys.executable, "-c",
                         f"import sys; open(r'{NUDGES}', 'a', encoding='utf-8').write(open(sys.argv[1], encoding='utf-8').read() + '\\n')"]},
          open(os.path.join(WORLD, "config.json"), "w"))


def nudges():
    try:
        return [json.loads(l) for l in open(NUDGES, encoding="utf-8")]
    except OSError:
        return []


def sleeper():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])


class Clock:
    t = time.time()
    def __call__(self):
        return self.t


procs = [sleeper() for _ in range(4)]
import atexit
atexit.register(lambda: [p.kill() for p in procs])  # even when a row throws: an orphan sleeper holds the output pipe
clock = Clock()
A = B.Bouncer(B.Who("cc", "aaaa1111", procs[0].pid), clock)
D = B.Bouncer(B.Who("den", "dddd2222", procs[1].pid), clock)
C = B.Bouncer(B.Who("cc", "cccc3333", procs[2].pid), clock)
res = {}

# BC1 + BC2: the first call takes it, every call renews it; the holder is the exact id
ok1, _ = A.acquire("hands_look")
st = A.status()
res["BC1"] = ok1 and st["holder"]["house"] == "cc" and st["holder"]["id"] == "aaaa1111" and st["holder"]["pid"] == procs[0].pid
clock.t += 100
ok2, _ = A.acquire("hands_act")
res["BC2"] = ok2 and A.status()["holder"]["last"] == clock.t and A.status()["holder"]["purpose"] == "hands_act"

# BC3: the refusal names the holder exactly, its purpose, and my place in line
okd, why = D.acquire("browser_open")
okc, whyc = C.acquire("hands_launch")
res["BC3"] = (not okd and "CC session aaaa1111" in why and "hands_act" in why and "#1 in line" in why
              and not okc and "#2 in line" in whyc)

# BC5: done frees it, the first in line is nudged and its place is held; nobody else cuts in
A.release()
n = nudges()
okc2, whyc2 = C.acquire("hands_launch")       # #2 tries to cut in while D's place is held
okd2, _ = D.acquire("browser_open")            # D claims it
res["BC5"] = (len(n) == 1 and n[0]["house"] == "den" and n[0]["id"] == "dddd2222" and "have your hands back" in n[0]["message"]
              and not okc2 and "held" in whyc2 and okd2)

# BC5b: an unclaimed reservation passes on to the next in line
D.release()                                    # C is next: nudged, held for it
C2 = B.Bouncer(B.Who("cc", "eeee5555", procs[3].pid), clock)
okc2held, _ = C2.acquire("hands_look")         # refused while C's place is held: joins the line
clock.t += B.RESERVE_S + 1                     # C never comes
C2.sweep()                                     # the reservation passes on to C2, nudged
n = nudges()
st = A.status()
res["BC5b"] = (not okc2held and len(n) == 3 and n[1]["id"] == "cccc3333" and n[2]["id"] == "eeee5555"
               and st["reserved"]["id"] == "eeee5555" and st["holder"] is None)

# BC4: silence frees it (and the next waiter is nudged); a dead holder frees it at once
for b in (A, D, C, C2):
    b.release()
B.Bouncer._save({"holder": None, "line": [], "reserved": None})
open(NUDGES, "w").close()
A.acquire("x"); D.acquire("y")
clock.t += B.SILENT_S + 1
D.sweep()
silent = A.status()
res_silent = silent["holder"] is None and silent["reserved"] and silent["reserved"]["id"] == "dddd2222" and nudges()
B.Bouncer._save({"holder": None, "line": [], "reserved": None})
dead = sleeper()
Z = B.Bouncer(B.Who("cc", "zzzz9999", dead.pid), clock)
Z.acquire("z")
dead.kill(); dead.wait()
okA, _ = A.acquire("after the dead one")
res["BC4"] = bool(res_silent) and okA

# BC6: the person's Stop frees it and nudges nobody
B.Bouncer._save({"holder": None, "line": [], "reserved": None})
open(NUDGES, "w").close()
A.acquire("x"); D.acquire("y")
A.stopped()
st = A.status()
res["BC6"] = st["holder"] is None and st["reserved"] is None and nudges() == []

# BC7: atomic across processes, and a corrupt file means free, never stuck
B.Bouncer._save({"holder": None, "line": [], "reserved": None})
racer = ("import os, sys, json; sys.path.insert(0, r'%s'); from engine import bouncer as B; "
         "b = B.Bouncer(B.Who('cc', sys.argv[1], os.getpid())); print(json.dumps(b.acquire('race')[0]), flush=True); "
         "import time; time.sleep(4)" % ROOT)  # alive through the race: a dead winner is rightly replaced
env = dict(os.environ)
ps = [subprocess.Popen([sys.executable, "-c", racer, f"racer{i}"], stdout=subprocess.PIPE, text=True, env=env) for i in range(8)]
wins = sum(json.loads(p.communicate()[0].strip() or "false") for p in ps)
open(B.FILE, "w").write("{not json")
okcorrupt, _ = A.acquire("after corruption")
res["BC7"] = wins == 1 and okcorrupt

# BC8: the sweep alone (nobody calling acquire) frees a silent holder and nudges the line
B.Bouncer._save({"holder": None, "line": [], "reserved": None})
open(NUDGES, "w").close()
A.acquire("x"); D.acquire("y")
clock.t += B.SILENT_S + 1
C.sweep()
res["BC8"] = A.status()["reserved"]["id"] == "dddd2222" and len(nudges()) == 1

# BC9: any wrong shape of bouncer.json means free, never stuck (Freki F1), including deep nesting
bad_files = ['{"holder": {"house": "cc"}}', '{"line": [1]}', '{"holder": {"house":"cc","id":"x","pid":1,"since":1,"last":"x"}}',
             '{"reserved": {"house":"cc","id":"x","pid":1,"until":"x"}}', '[' * 5000 + ']' * 5000, '{"holder": true}',
             '{"line": {"a": 1}}']
bc9 = []
for txt in bad_files:
    open(B.FILE, "w").write(txt)
    try:
        ok9, _ = A.acquire("after a bad file")
        bc9.append(ok9 and A.status()["holder"]["id"] == "aaaa1111")
    except Exception as e:
        bc9.append(False)
    A.release()
res["BC9"] = all(bc9)

# BC10: a forged holder with a live pid and a future / infinite / NaN time is freed, not eternal (Freki F2)
bc10 = []
for last in ("1e308", "Infinity", "NaN", str(clock.t + 3600)):
    open(B.FILE, "w").write('{"holder": {"house":"cc","id":"forged","pid":%d,"since":%s,"last":%s,"purpose":"x"},'
                            '"line": [], "reserved": null}' % (procs[3].pid, last, last))
    okf, why = D.acquire("after a forged holder")
    bc10.append(okf)
    D.release()
open(B.FILE, "w").write('{"holder": null, "line": [], "reserved": {"house":"cc","id":"forged","pid":%d,"until":1e308}}'
                        % procs[3].pid)
okr, _ = D.acquire("after a forged reservation")
bc10.append(okr)
D.release()
res["BC10"] = all(bc10)

# BC11: only an existing .exe may be run as a configured command (Freki F3); a refused nudge is logged (F9)
bat = os.path.join(WORLD, "evil.bat")
open(bat, "w").write("@echo off\r\n")
res_exe = (B.exe_cmd([sys.executable, "-c", "x"]) is not None and B.exe_cmd([bat, "x"]) is None
           and B.exe_cmd(["C:\\nope\\missing.exe"]) is None and B.exe_cmd("python") is None and B.exe_cmd([]) is None)
json.dump({"nudge_cmd": [bat]}, open(os.path.join(WORLD, "config.json"), "w"))
B._nudge({"house": "den", "id": "x", "message": "& calc"})
logged = os.path.exists(os.path.join(WORLD, "bouncer.log")) and "must start with an existing .exe" in open(
    os.path.join(WORLD, "bouncer.log"), encoding="utf-8").read()
res["BC11"] = res_exe and logged

# BC12: the house name is bounded; a session file of the wrong shape never fails the call (Freki F8)
os.environ["HANDS_HOUSE"] = "x" * 200 + ";&|"
w12 = B.me_from_env()
os.environ["HANDS_HOUSE"] = "den"
res["BC12"] = w12.house == "cc" and B.me_from_env().house == "den"
os.environ.pop("HANDS_HOUSE", None)

# BC13: a session copying the holder's id (it is shown to every waiter) can't take or free the lease (Sköll K3)
B.Bouncer._save({"holder": None, "line": [], "reserved": None})
victim = B.Bouncer(B.Who("cc", "victim01", procs[0].pid), clock)
impostor = B.Bouncer(B.Who("cc", "victim01", procs[1].pid), clock)
victim.acquire("v")
oki, _ = impostor.acquire("i")
freed = impostor.release()
st13 = victim.status()
res["BC13"] = (not oki) and (not freed) and st13["holder"]["pid"] == procs[0].pid
victim.release(); impostor.release()

# BC14: a planted hard link at bouncer.json.tmp never carries the write outside the state folder (Sköll K4)
outside = os.path.join(tempfile.mkdtemp(prefix="hands-outside-"), "outside.txt")
open(outside, "w").write("untouched")
tmpf = B.FILE + ".tmp"
if os.path.lexists(tmpf):
    os.remove(tmpf)
os.link(outside, tmpf)
A.acquire("after a planted link"); A.release()
res["BC14"] = open(outside).read() == "untouched"

# BC15: a holder with a corrupt START time is freed, never an endless error (Sköll N1)
bc15 = []
for since in ("1e300", "-1e20", "1e18"):
    B.Bouncer._save({"holder": None, "line": [], "reserved": None})
    open(B.FILE, "w").write('{"holder": {"house":"den","id":"f","pid":%d,"since":%s,"last":%r},"line":[],"reserved":null}'
                            % (procs[3].pid, since, clock.t))
    try:
        ok15, _ = A.acquire("after a corrupt since")
        bc15.append(ok15)
    except Exception:
        bc15.append(False)
    A.release()
res["BC15"] = all(bc15)

# BC16: a shell or script host is never a configured command, however it is spelled (Sköll N2)
res["BC16"] = all(B.exe_cmd(c) is None for c in (
    ["%ComSpec%", "/c", "x.cmd"], [os.path.join(os.environ["WINDIR"], "System32", "cmd.exe"), "/c", "x"],
    [os.path.join(os.environ["WINDIR"], "System32", "WindowsPowerShell", "v1.0", "powershell.exe"), "-c", "x"],
    [os.path.join(os.environ["WINDIR"], "System32", "wscript.exe"), "x.vbs"])) and B.exe_cmd([sys.executable, "x"]) is not None

# BC17: a planted folder at the old temp name, or a read-only bouncer.json, never jams the writes (Sköll N3)
B.Bouncer._save({"holder": None, "line": [], "reserved": None})
if os.path.isfile(B.FILE + ".tmp"):
    os.remove(B.FILE + ".tmp")  # BC14's planted link
os.makedirs(B.FILE + ".tmp", exist_ok=True)
import stat as _stat
try:  # a folder at the old temp name changes nothing: writes use fresh names
    ok17a = A.acquire("after a planted folder")[0] and A.status()["holder"]["id"] == "aaaa1111"
except Exception:
    ok17a = False
A.release()
os.chmod(B.FILE, _stat.S_IREAD)
try:  # a read-only lease file is never unlocked (that would reach a hard-linked file outside): it fails OPEN, logged
    ok17b = A.acquire("after a read-only file")[0] and D.acquire("another")[0]
    ok17b = ok17b and "could not save the lease" in open(os.path.join(WORLD, "bouncer.log"), encoding="utf-8").read()
except Exception:
    ok17b = False
os.chmod(B.FILE, _stat.S_IWRITE | _stat.S_IREAD)
A.release(); D.release()
res["BC17"] = ok17a and ok17b

# BC18: the clock stepping back (an NTP correction) never frees a working holder (Sköll N4)
B.Bouncer._save({"holder": None, "line": [], "reserved": None})
A.acquire("working")
clock.t -= 10
ok18, _ = D.acquire("during a clock step back")
res["BC18"] = (not ok18) and A.status()["holder"]["id"] == "aaaa1111"
A.release(); D.release()
clock.t += 10

# BC19: a holder renewing all along keeps the hands past a day (Sköll R3b)
B.Bouncer._save({"holder": None, "line": [], "reserved": None})
A.acquire("long job")
D.acquire("waiting")
ok19 = True
for _ in range(25 * 30):  # renewing every 2 min (inside the 3 min silence rule) for 25 hours
    clock.t += 120
    ok19 = ok19 and A.acquire("still working")[0]
res["BC19"] = ok19 and A.status()["holder"]["id"] == "aaaa1111"
A.release(); D.release()

# BC20: a folder planted at bouncer.json itself fails OPEN and is logged, never stuck (Sköll R3c)
os.remove(B.FILE)
os.makedirs(B.FILE)
try:
    ok20 = A.acquire("with a folder at the lease file")[0] and D.acquire("and another")[0]
except Exception:
    ok20 = False
res["BC20"] = ok20 and "could not save the lease" in open(os.path.join(WORLD, "bouncer.log"), encoding="utf-8").read()
os.rmdir(B.FILE)

# BC21: a read-only file outside, hard-linked at bouncer.json, keeps its read-only flag and its bytes (Sköll R3d)
outside21 = os.path.join(tempfile.mkdtemp(prefix="hands-outside-"), "precious.txt")
open(outside21, "w").write("PRECIOUS")
os.chmod(outside21, _stat.S_IREAD)
if os.path.exists(B.FILE):
    os.remove(B.FILE)
os.link(outside21, B.FILE)
A.acquire("over a planted link"); A.release()
res["BC21"] = (not os.access(outside21, os.W_OK)) and open(outside21).read() == "PRECIOUS"
os.chmod(outside21, _stat.S_IWRITE | _stat.S_IREAD)

# BC22: another session's text never reaches a command line, even through a renamed shell (Sköll R3e)
import shutil as _sh
fake = os.path.join(WORLD, "notashell.exe")
_sh.copy2(os.path.join(os.environ["WINDIR"], "System32", "cmd.exe"), fake)
script = os.path.join(WORLD, "n.cmd")
open(script, "w").write("@echo off\r\n")
pwn = os.path.join(WORLD, "PWN.txt")
json.dump({"nudge_cmd": [fake, "/c", script]}, open(os.path.join(WORLD, "config.json"), "w"))
B._nudge({"house": "cc", "id": f'w&echo INJ>"{pwn}"&', "message": "& echo INJ2"})
res["BC22"] = not os.path.exists(pwn)

for p in procs:
    p.kill()
for k in res:
    print(f"{k}: {'PASS' if res.get(k) else 'FAIL'}")
print("SUMMARY " + " ".join(f"{k}:{'PASS' if v else 'FAIL'}" for k, v in res.items()))
sys.exit(0 if all(res.values()) else 1)
