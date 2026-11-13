"""The bouncer through the real doors: two Hands servers over stdio, one as a coding host ('cc') and one as a chat host ('den'), in the test's own
bouncer world. Asserts: CC takes the hands; the Den is refused by CC's exact session id and joins the line; CC's
hands_done hands them back and the Den is NUDGED by its exact id; the Den then goes ahead.
Opens only a Calculator of its own (closed by done)."""
import asyncio
import json
import os
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALC = "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
WORLD = tempfile.mkdtemp(prefix="hands-bouncer-door-")
NUDGES = os.path.join(WORLD, "nudges.jsonl")
json.dump({"nudge_cmd": [sys.executable, "-c",
                         f"import sys; open(r'{NUDGES}', 'a', encoding='utf-8').write(open(sys.argv[1], encoding='utf-8').read() + '\\n')"]},
          open(os.path.join(WORLD, "config.json"), "w"))
RES = {}


def params(house: str, sid: str) -> StdioServerParameters:
    env = dict(os.environ, HANDS_TEST_ALLOW=CALC, HANDS_TEST_HOOKS="1", HANDS_BOUNCER_DIR=WORLD,
               HANDS_HOUSE=house, HANDS_SESSION=sid, HANDS_HEADLESS="1")
    return StdioServerParameters(command=sys.executable, args=["-X", "utf8", os.path.join(HERE, "mcp_server.py")], env=env)


def text(r) -> str:
    return r.content[0].text if r.content else ""


async def main() -> int:
    async with stdio_client(params("cc", "cc-test-0001")) as (r1, w1), stdio_client(params("den", "den-test-0002")) as (r2, w2):
        async with ClientSession(r1, w1) as cc, ClientSession(r2, w2) as den:
            await cc.initialize(); await den.initialize()
            a = await cc.call_tool("hands_launch", {"app": "calc"})
            RES["BD1 cc takes the hands"] = not a.isError
            d = await den.call_tool("hands_screen", {})
            msg = text(d)
            RES["BD2 den refused by cc's exact id, joins the line"] = (d.isError and "CC session cc-test-0001" in msg
                                                                        and "hands_launch" in msg and "#1 in line" in msg)
            print("   refusal:", msg[:200])
            free = await den.call_tool("hands_windows", {})
            RES["BD3 listing never needs the lease"] = not free.isError
            who = json.loads(text(await den.call_tool("hands_who", {})))
            RES["BD4 hands_who names holder + line"] = (who["holder"]["id"] == "cc-test-0001"
                                                       and who["line"][0]["id"] == "den-test-0002")
            done = await cc.call_tool("hands_done", {})
            print("   done:", text(done)[:200])
            await asyncio.sleep(1.0)
            n = [json.loads(l) for l in open(NUDGES, encoding="utf-8")] if os.path.exists(NUDGES) else []
            RES["BD5 done nudges the den by its exact id"] = (len(n) == 1 and n[0]["house"] == "den"
                                                             and n[0]["id"] == "den-test-0002")
            after = await den.call_tool("hands_windows", {"query": "zzz-nothing"})
            go = await den.call_tool("hands_who", {})
            st = json.loads(text(go))
            RES["BD6 the den's place is held for it"] = st["reserved"] and st["reserved"]["id"] == "den-test-0002"
            await den.call_tool("hands_done", {})
    for k, v in RES.items():
        print(f"{k}: {'PASS' if v else 'FAIL'}")
    print("SUMMARY " + " ".join(f"{k.split()[0]}:{'PASS' if v else 'FAIL'}" for k, v in RES.items()))
    return 0 if all(RES.values()) else 1


sys.exit(asyncio.run(main()))
