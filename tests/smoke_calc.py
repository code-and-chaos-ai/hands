"""Criteria B, C, D, J on a Calculator this test opens itself (never the person's windows)."""
import sys, time
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from engine import session, win, policy
import uiautomation as auto

H = session.Hands(overlay="--overlay" in sys.argv)
if not H.use_overlay:
    H.consent.session.add("Microsoft.WindowsCalculator_8wekyb3d8bbwe!App")  # test-scoped, never persisted
fg0, cur0 = win.foreground(), win.cursor_pos()
ok = True

t = time.perf_counter(); w = H.launch("calc"); tl = time.perf_counter() - t
print("B launch:", w["title"], w["app"], f"{tl*1000:.0f} ms", "| their fg kept:", win.foreground() == fg0)
ok &= "Calculator" in w["app"] and tl < 5
hwnd = w["id"]

times = []
for k in range(3):
    t = time.perf_counter(); text, imgs = H.look(hwnd); times.append(time.perf_counter() - t)
print(f"C/J look: {[round(x*1000) for x in times]} ms, image {len(imgs[0])//1024} KB, {len(text)} chars")
ok &= times[1] < 0.25 and len(imgs) >= 1

lk = H.looks[hwnd]
idx = {n.aid: n.index for n in lk.nodes if n.aid}
plan = [{"click": idx[a]} for a in ("clearButton", "num7Button", "multiplyButton", "num6Button", "equalButton")]
t = time.perf_counter(); out, imgs = H.act(hwnd, plan, then="tree"); ta = time.perf_counter() - t
print(out.split("--- after ---")[0])
res = [n for n in H.looks[hwnd].nodes if n.aid == "CalculatorResults"][0].name
print("D result:", res, f"| batch+look {ta*1000:.0f} ms | cursor kept:", win.cursor_pos() == cur0, "| their fg kept:", win.foreground() == fg0)
ok &= res.endswith("42") and win.cursor_pos() == cur0 and win.foreground() == fg0

auto.ControlFromHandle(hwnd).GetWindowPattern().Close()
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)

