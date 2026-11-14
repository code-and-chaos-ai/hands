"""THE gate for Hands: runs every asserting test file (slice 1 + browser), prints ONE SUMMARY line, exits 1 if any
fails. Criterion E (foreground fallback while the person types) needs a person's real hands: reported as SKIPPED-MANUAL, never PASS.
proof_overlay.py makes proof screenshots for a human; it asserts nothing, so it isn't part of the gate.

    python -X utf8 tests\\run.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SUITE = [  # (label, file, timeout s)
    ("smoke_calc(B,C,D,J)", "smoke_calc.py", 120),
    ("door_test", "door_test.py", 180),
    ("k8_cursor", "k8_cursor.py", 120),
    ("slice1(A,F,G,H,I)", "slice1_test.py", 300),
    ("policy(PW1-PW3)", "policy_test.py", 60),
    ("cleanup(CL0-CL2)", "cleanup_test.py", 120),
    ("identity(ID1-ID4)", "identity_test.py", 60),
    ("hardening(HD1-HD10)", "hardening_test.py", 60),
    ("bouncer(BC1-BC22)", "bouncer_test.py", 120),
    ("bouncer_door(BD1-BD6)", "bouncer_door_test.py", 180),
    ("browser(K-W,J1-J7,E1-E5)", "browser_test.py", 900),
]


def main() -> int:
    results = []
    for label, fname, limit in SUITE:
        t0 = time.time()
        print(f"\n===== {label}: {fname}", flush=True)
        try:
            p = subprocess.run([sys.executable, "-X", "utf8", os.path.join(HERE, fname)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=limit)
            out, code = p.stdout, p.returncode
        except subprocess.TimeoutExpired as e:
            out, code = (e.stdout or "") if isinstance(e.stdout, str) else "", -1
            out += f"\nTIMEOUT after {limit} s"
        rows = [l for l in out.splitlines() if l.startswith("SUMMARY") or ": PASS" in l or ": FAIL" in l
                or l.strip() in ("PASS", "FAIL") or l.startswith("K8 ") or "TIMEOUT" in l or l.startswith("ABORTED")]
        print("\n".join(rows), flush=True)
        results.append((label, code == 0, round(time.time() - t0)))
    line = " ".join(f"{label}:{'PASS' if ok else 'FAIL'}" for label, ok, _ in results)
    print("\nSUMMARY " + line + " E(foreground fallback):SKIPPED-MANUAL")
    print("times: " + ", ".join(f"{label} {s}s" for label, _, s in results))
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
