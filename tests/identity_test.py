"""ID1: with no config, the identity is the defaults. ID2: valid overrides apply, each key on its own. ID3: invalid
values are ignored (a bad colour, a host name Chrome would refuse, a pipe with a path in it), never half-applied.
ID4: the pipe and host the Chrome side uses come from the identity."""
import json, os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from engine import identity

res = {}
tmp = tempfile.mkdtemp(prefix="hands-identity-")
identity.CONFIG = os.path.join(tmp, "config.json")
res["ID1"] = identity.get() == identity.DEFAULTS and identity.label() == "Rowan is working here"

json.dump({"identity": {"name": "Nova", "initial": "N", "color": "#00C2A8"}}, open(identity.CONFIG, "w"))
g = identity.get()
res["ID2"] = (g["name"] == "Nova" and g["initial"] == "N" and g["color"] == "#00C2A8"
              and g["color_hot"] == identity.DEFAULTS["color_hot"] and identity.label() == "Nova is working here")

json.dump({"identity": {"color": "pink", "chrome_host": "Com.Bad Host", "chrome_pipe": "..\\evil",
                        "initial": "", "name": "x" * 99}}, open(identity.CONFIG, "w"))
res["ID3"] = identity.get() == identity.DEFAULTS

identity.CONFIG = os.path.join(ROOT, "state", "config.json")  # the real one, read only
from engine import chrome_key
res["ID4"] = (chrome_key.HOST_NAME == identity.get()["chrome_host"]
              and chrome_key.pipe_address().endswith(identity.get()["chrome_pipe"]))

for k, v in res.items():
    print(f"{k}: {'PASS' if v else 'FAIL'}")
print("SUMMARY " + " ".join(f"{k}:{'PASS' if v else 'FAIL'}" for k, v in res.items()))
sys.exit(0 if all(res.values()) else 1)
