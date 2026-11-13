"""The real door: a stdio MCP client drives mcp_server.py exactly as Claude Code will, and ASSERTS each claim.
Own window only (a Calculator it opens). Exit 1 on any miss.

    python -X utf8 tests\\door_test.py
"""
import asyncio
import json
import os
import sys
import time

import win32gui
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALC = "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, evidence: str) -> None:
    CHECKS.append((name, bool(ok), evidence))
    print(f"{name}: {'PASS' if ok else 'FAIL'} — {evidence}", flush=True)


def look_id(text: str) -> int:
    for line in text.split("--- after ---")[-1].splitlines():
        if line.startswith("look "):
            return int(line.split()[1])
    raise ValueError("no look id")


async def main() -> int:
    env = dict(os.environ, HANDS_TEST_ALLOW=CALC, HANDS_TEST_HOOKS="1")
    params = StdioServerParameters(command=sys.executable, args=["-X", "utf8", os.path.join(HERE, "mcp_server.py")], env=env)
    hwnd = None
    try:
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = [x.name for x in (await s.list_tools()).tools]
                check("tools", {"hands_windows", "hands_launch", "hands_look", "hands_act", "hands_done", "browser_tabs",
                                "browser_open", "browser_look", "browser_act", "browser_close"} <= set(tools), f"{tools}")
                res = await s.call_tool("hands_launch", {"app": "calc"})
                info = json.loads(res.content[0].text)
                hwnd = info["id"]
                check("launch", not res.isError and win32gui.IsWindow(hwnd), f"{info.get('title')!r} in {info.get('launched_in_ms')} ms")
                res = await s.call_tool("hands_look", {"window": hwnd})
                text = res.content[0].text
                check("look", not res.isError and any(c.type == "image" for c in res.content), text.splitlines()[0][:90])
                idx = {}
                for line in text.splitlines():
                    for aid in ("clearButton", "num8Button", "plusButton", "num4Button", "equalButton"):
                        if f"#{aid} " in line + " ":
                            idx[aid] = int(line.strip().split("]")[0][1:])
                acts = [{"click": idx[a]} for a in ("clearButton", "num8Button", "plusButton", "num4Button", "equalButton")]
                t = time.perf_counter()
                res = await s.call_tool("hands_act", {"window": hwnd, "look": look_id(text), "actions": acts, "then": "tree"})
                body = res.content[0].text
                result = [l.strip() for l in body.splitlines() if "CalculatorResults" in l]
                check("8 + 4 = 12", not res.isError and bool(result) and '12"' in result[0],
                      f"{result[:1]} in {(time.perf_counter() - t) * 1000:.0f} ms")
                fg = win32gui.GetForegroundWindow()
                res = await s.call_tool("hands_act", {"window": hwnd, "look": look_id(body), "actions": [{"key": "Super_L+r"}],
                                                      "then": "tree"})
                wk = res.content[0].text
                check("Windows key refused, nothing pressed",
                      "refused: I never press the Windows key" in wk and "pressed" not in wk.split("--- after ---")[0]
                      and win32gui.GetForegroundWindow() == fg,
                      wk.strip().splitlines()[0][:90])
                res = await s.call_tool("hands_act", {"window": hwnd, "look": look_id(wk), "actions": [{"key": "alt+F4"}], "then": "none"})
                closed = False
                for _ in range(30):
                    if not win32gui.IsWindow(hwnd):
                        closed = True
                        break
                    await asyncio.sleep(0.1)
                check("alt+F4 closed it", closed, res.content[0].text.strip().splitlines()[0][:90])
                res = await s.call_tool("hands_done", {})
                check("done released", "released" in res.content[0].text, res.content[0].text[:90])
    finally:
        if hwnd and win32gui.IsWindow(hwnd):
            import uiautomation as auto
            auto.ControlFromHandle(hwnd).GetWindowPattern().Close()
    ok = all(c[1] for c in CHECKS) and len(CHECKS) == 7
    print("\nSUMMARY " + " ".join(f"{n.replace(' ', '_')}:{'PASS' if o else 'FAIL'}" for n, o, _ in CHECKS))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
