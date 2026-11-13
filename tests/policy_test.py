"""Password managers are denied by HOST, in every spelling; a path can never be a wall, so no path rule exists.
Pure policy: opens nothing. Pins the doors Sköll used (percent-encoded and doubled paths) and the log scrub."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine import policy  # noqa: E402
import mcp_server as m  # noqa: E402

ok = True


def denied(u):
    try:
        policy.check_url(u, opening=True)
        return False
    except policy.Denied:
        return True


hosts = ["https://vault.zoho.com/x", "https://VAULT.ZOHO.COM./", "https://x.vault.zoho.eu/", "https://１password.com/",
         "https://vault.bitwarden.com/", "https://passwords.google.com/", "http://app.keeweb.info:1234/pm.html"]
h_ok = all(denied(u) for u in hosts)
ok &= h_ok
print(f"PW1: {'PASS' if h_ok else 'FAIL'} — every vault host form denied: {[(u, denied(u)) for u in hosts if not denied(u)] or 'all'}")

no_paths = "paths" not in policy.nav_policy() and not hasattr(policy, "PASSWORD_PATHS")
sw = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chrome", "extension", "sw.js"),
          encoding="utf-8").read()
no_paths &= "pol.paths" not in sw
ok &= no_paths
print(f"PW2: {'PASS' if no_paths else 'FAIL'} — no path-based deny rule exists in the engine or the extension")

notes = m._scrub("err at http:u:SECRET@h/x and https://a:SECRET@host/p?q=SECRET and http:/\\u:SECRET@h/")
s_ok = "SECRET" not in notes
ok &= s_ok
print(f"PW3: {'PASS' if s_ok else 'FAIL'} — free-text scrub: {notes!r}")

print("SUMMARY PW1:%s PW2:%s PW3:%s" % tuple("PASS" if x else "FAIL" for x in (h_ok, no_paths, s_ok)))
sys.exit(0 if ok else 1)
