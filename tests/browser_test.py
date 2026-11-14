"""Slice 2 acceptance, K–T (+ U, the per-frame gate ruled 12 Nov, + edge rows marked E*).

Drives the REAL door (mcp_server.py over stdio, as Claude Code does) against a SEPARATE Chrome 157 this test
starts: temp --user-data-dir, our unpacked extension loaded through CDP Extensions.loadUnpacked over
--remote-debugging-pipe (no TCP port), its own http.servers on 127.0.0.1 / 127.0.0.2. The test's own CDP pipe is
used only as independent eyes (what the page recorded) and to play the person (closing a tab, focusing a frame).
Never touches the person's Chrome: own profile, own PIDs, everything removed in finally. The authkey is never printed.

    python -X utf8 tests\\browser_test.py
"""
from __future__ import annotations

import asyncio
import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.environ["HANDS_CHROME_PIPE"] = f"rowan-hands-chrome-test-{os.getpid()}"  # never the real pipe
os.environ["HANDS_TEST_HOOKS"] = "1"  # debug.* seams: honoured only on a non-default pipe (see chrome\\host.py)
os.environ["HANDS_BOUNCER_DIR"] = tempfile.mkdtemp(prefix="hands-bt-bouncer-")  # never the live lease
os.environ["HANDS_TEST_GRANT_SCOPE"] = "session"  # J7 tests the default, whatever the owner's config says

import win32api  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from chrome import install_host  # noqa: E402
from chrome_launch import PipeChrome  # noqa: E402
from engine.browser import Pipe  # noqa: E402
from engine.chrome_key import EXTENSION_ID  # noqa: E402

RESULTS: dict[str, tuple[bool, str]] = {}
STOP = threading.Event()
HITS: dict[str, int] = {}

PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8><title>Hands test</title></head><body>
<h1>Hands test page</h1>
<button id=press onclick="rec('click', {trusted: event.isTrusted, focus: document.hasFocus()})">Press me</button>
<button id=add onclick="addItem()">Add item</button>
<ul id=list><li>First item</li></ul>
<form id=f onsubmit="event.preventDefault(); rec('submit', {q: document.getElementById('q').value})">
<label>Query <input id=q name=q></label>
<label>Name <input id=nm value="old value"></label>
<label>Color <select id=color><option value=r>Red</option><option value=g>Green</option><option value=b>Blue</option></select></label>
<button type=submit>Go</button></form>
<script>
window.log = []; function rec(k, d) { window.log.push(Object.assign({k}, d || {})); }
const $ = (i) => document.getElementById(i);
$('q').addEventListener('input', e => rec('input-q', {v: e.target.value, t: e.inputType}));
$('nm').addEventListener('input', e => rec('input-nm', {v: e.target.value}));
$('nm').addEventListener('change', e => rec('change-nm', {v: e.target.value}));
$('color').addEventListener('change', e => rec('change-color', {v: e.target.value}));
let n = 1; function addItem() { const li = document.createElement('li'); li.textContent = 'Item ' + (++n); $('list').appendChild(li); }
</script></body></html>"""
HOME = "<!doctype html><meta charset=utf-8><title>Her tab</title><p>The person's own page</p>"
# fake secrets (none real), one per line at the top; ordinary text far below
FAKE_GHP = "gh" + "p_FaKe9Qw4Lm2Rt8Yv5Kp7Hj3Nb6Gc1Df0SaXz"  # assembled: no scanner sees a whole fake key
FAKE_ANT = "sk-" + "ant-api03-FAKEkeyQw8Zt4Lm2Rx9Yv5Kp7Hj3Nb6Gc1Df0Sa"
FAKE_AWS = "AKIA" + "FAKE0EXAMPLE7KEY"
SECRETS_PAGE = """<!doctype html><meta charset=utf-8><title>keys {ghp}</title>
<body style="margin:0;background:#fff;color:#000;font:20px monospace">
<div style="height:40px">postgresql://neondb_owner:npg_Zx9Qw4Lm2Rt8Yv5Kp@ep-fake-meadow-00000.us-east-2.aws.neon.tech/neondb</div>
<div style="height:40px">{ant}</div>
<div style="height:40px">{aws}</div>
<div style="height:40px"><input aria-label="token" style="font:20px monospace;width:600px" value="{ghp}"></div>
<p style="margin-top:260px">Plain words stay readable</p></body>""".replace("{ghp}", FAKE_GHP).replace("{ant}", FAKE_ANT).replace("{aws}", FAKE_AWS)
FAKE_SECRETS = ["npg_Zx9Qw4Lm2Rt8Yv5Kp", FAKE_ANT, FAKE_AWS, FAKE_GHP]
INNER = """<!doctype html><meta charset=utf-8><title>inner</title><style>html,body{margin:0;background:#ff00ff}</style><button id=ib onclick="window.hit=1">Inner button</button>
<input id=ii aria-label="Inner field">"""
FRAMES = """<!doctype html><meta charset=utf-8><title>Frames</title><h1>Frames page</h1>
<iframe id=fa title="same origin" src="/inner.html" width=300 height=80></iframe>
<iframe id=fb title="other origin" src="http://127.0.0.2:{pb}/b.html" width=300 height=80></iframe>
<iframe id=fc title="vault" src="http://app.keeweb.info:{pa}/pm.html" width=300 height=80></iframe>
<iframe id=fd title="data" src="data:text/html,<body style='margin:0;background:%23ff00ff'><button>Data secret</button><input aria-label=DataField>" width=300 height=80></iframe>"""
B_PAGE = """<!doctype html><meta charset=utf-8><title>B</title><style>html,body{margin:0;background:#ff00ff}</style><button id=bb onclick="window.hit=1">Secret B button</button>
<input id=bi aria-label="Secret B field">"""
PM_PAGE = """<!doctype html><meta charset=utf-8><title>PM</title><style>html,body{margin:0;background:#ff00ff}</style><p>Vault secret text</p><input id=pi aria-label="Vault field">"""
NEST_OUTER = """<!doctype html><meta charset=utf-8><title>Nest</title><h1>Nest page</h1>
<iframe id=nc title="consented other site" src="http://127.0.0.3:{pc}/nest.html" width=360 height=160></iframe>"""
NEST = """<!doctype html><meta charset=utf-8><title>nest</title><p>consented frame</p>
<iframe id=nv title="vault inside" src="http://app.keeweb.info:{pa}/pm.html" width=300 height=90></iframe>"""
FLICKER = """<!doctype html><meta charset=utf-8><title>Flicker</title><style>body{background:#fff}</style>
<h1>Flicker page</h1><div id=slot style="height:130px"></div>
<script>let on = false; (function tick() { const s = document.getElementById('slot');
  if (window.__pin) { if (!on) { s.innerHTML = '<iframe src="http://app.keeweb.info:{pa}/pm.html" width=300 height=120 style="border:0"></iframe>'; on = true; }
    setTimeout(tick, 200); return; }
  s.innerHTML = on ? '' : '<iframe src="http://app.keeweb.info:{pa}/pm.html" width=300 height=120 style="border:0"></iframe>';
  on = !on; setTimeout(tick, on ? 1500 : 500); })();</script>"""
HIDDEN = """<!doctype html><meta charset=utf-8><title>Hidden</title><style>body{background:#fff}</style><p>Visible text</p>
<iframe id=hv src="http://app.keeweb.info:{pa}/pm.html" style="display:none"></iframe>"""
BLANK = """<!doctype html><meta charset=utf-8><title>Blank</title><h1>links</h1>
<a id=l href="/second.html?blank" target="_blank">open in new tab</a><br><br>
<button id=p onclick="window.open('/second.html?pop','pw','popup,width=400,height=300')">popup</button>"""
JB = """<!doctype html><meta charset=utf-8><title>JB</title><style>html,body{margin:0;background:#ff00ff;height:100%}</style>
<script>for (const ev of ['mousedown','click','pointerdown','focus','input','keydown'])
 addEventListener(ev, e => fetch('/hit-b-'+ev, {mode:'no-cors'}), true);</script><input id=bi aria-label="B field" style="width:280px">"""
JACK = """<!doctype html><meta charset=utf-8><title>Jack</title><style>body{margin:0}</style><h1>jack page</h1>
<button id=t style="position:absolute;top:150px;left:40px;width:220px;height:70px" onclick="window.clicked=(window.clicked||0)+1">Press here</button>
<iframe id=x src="http://127.0.0.2:{pb}/jb.html" style="position:absolute;top:110px;left:-3000px;width:320px;height:160px;border:0"></iframe>
<script>const f=document.getElementById('x');
if (location.hash === '#move') addEventListener('mousemove', () => { f.style.left = '0px'; }, true);</script>"""
NAVP = """<!doctype html><meta charset=utf-8><title>Nav</title><h1>nav page</h1>
<button id=g onclick="location='http://app.keeweb.info:{pa}/pm.html?x=1'">go vault</button>
<a id=lh href="http://localhost:{pa}/second.html?lh">to localhost</a>"""
SHADOW = """<!doctype html><meta charset=utf-8><title>Shadow</title><h1>shadow page</h1><div id=host></div>
<script>const r = document.getElementById('host').attachShadow({mode: 'closed'});
r.innerHTML = '<iframe id=sf src="http://127.0.0.2:{pb}/b.html" width=300 height=80></iframe>';
window.__sf = r.querySelector('iframe');</script>"""
VLINK = """<!doctype html><meta charset=utf-8><title>Vlink</title><h1>vault links</h1>
<a href="http://app.keeweb.info:{pa}/pm.html?blank" target="_blank">vault new tab</a><br><br>
<button onclick="location='http://app.keeweb.info:{pa}/swpage.html'">sw nav</button>"""
VSW = """self.addEventListener('install', e => self.skipWaiting()); self.addEventListener('activate', e => e.waitUntil(clients.claim()));
self.addEventListener('fetch', e => { if (e.request.mode === 'navigate' && e.request.url.includes('swpage')) e.respondWith(new Response('<!doctype html><title>SWVAULT</title><p>vault served by sw</p><input aria-label="Vault field">', {headers: {'content-type': 'text/html'}})); });"""
SWREG = """<!doctype html><title>reg</title><script>navigator.serviceWorker.register('/vsw.js').then(() => navigator.serviceWorker.ready).then(() => { document.title = 'REGOK'; }).catch(e => { document.title = 'REGFAIL ' + e; });</script>"""
JACKL = """<!doctype html><meta charset=utf-8><title>JackL</title><style>body{margin:0}</style><h1>jack late</h1>
<button id=t style="position:absolute;top:150px;left:40px;width:220px;height:70px" onclick="window.clicked=(window.clicked||0)+1">Press here</button>
<iframe id=x src="http://127.0.0.2:{pb}/jb.html" style="position:absolute;top:110px;left:-3000px;width:320px;height:160px;border:0"></iframe>
<script>const f=document.getElementById('x'), b=document.getElementById('t'); const D=+location.hash.slice(1)||0; let armed=true;
addEventListener('mousemove', () => { if (!armed) return; armed = false; const t0 = performance.now(); const mc = new MessageChannel();
 mc.port1.onmessage = () => { if (performance.now() - t0 < D) { mc.port2.postMessage(0); return; } f.style.left = '0px';
   b.dispatchEvent(new MouseEvent('mousedown', {bubbles: true})); b.dispatchEvent(new MouseEvent('mouseup', {bubbles: true})); };
 mc.port2.postMessage(0); }, true);</script>"""
NAV2 = "<!doctype html><meta charset=utf-8><title>Second</title><p>Second page</p>"


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        HITS[path] = HITS.get(path, 0) + 1
        if path == "/redir":
            self.send_response(302)
            self.send_header("Location", self.path.split("to=", 1)[1])
            self.end_headers()
            return
        if path.startswith("/hit"):
            self.send_response(204)
            self.end_headers()
            return
        if path == "/hang":  # a page that never finishes loading
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<!doctype html><title>Hanging</title><button>Still loading</button>")
            self.wfile.flush()
            STOP.wait(40)
            return
        return super().do_GET()


def serve(host: str, web: str):
    srv = http.server.ThreadingHTTPServer((host, 0), functools.partial(Handler, directory=web))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def row(k: str, ok: bool, evidence: str) -> None:
    RESULTS[k] = (ok, evidence)
    print(f"{k}: {'PASS' if ok else 'FAIL'} — {evidence}", flush=True)


def text_of(res) -> str:
    return "\n".join(c.text for c in res.content if c.type == "text")


def look_id(text: str) -> int:
    for part in text.split("--- after ---")[-1].split("\n"):
        if part.startswith("look "):
            return int(part.split()[1])
    raise ValueError("no look id in: " + text[:200])


def ref_of(text: str, needle: str) -> int:
    body = text.split("--- after ---")[-1]
    for line in body.splitlines():
        s = line.strip().lstrip("+~- ").strip()
        if needle in s and s.startswith("["):
            return int(s[1:s.index("]")])
    raise LookupError(f"{needle!r} not in tree")


async def main() -> int:
    web = tempfile.mkdtemp(prefix="hands-test-web-")
    srv_a = srv_b = srv_c = ch = None
    created_key = False
    prev_key = prev_manifest = None
    try:
        srv_a = serve("127.0.0.1", web)
        srv_b = serve("127.0.0.2", web)
        srv_c = serve("127.0.0.3", web)
        pa, pb, pc = srv_a.server_address[1], srv_b.server_address[1], srv_c.server_address[1]
        files = {"page.html": PAGE, "home.html": HOME, "inner.html": INNER, "frames.html": FRAMES.format(pa=pa, pb=pb),
                 "b.html": B_PAGE, "pm.html": PM_PAGE, "second.html": NAV2,
                 "nestouter.html": NEST_OUTER.format(pc=pc), "nest.html": NEST.format(pa=pa),
                 "flicker.html": FLICKER.replace("{pa}", str(pa)), "hidden.html": HIDDEN.replace("{pa}", str(pa)),
                 "blank.html": BLANK, "jb.html": JB, "jack.html": JACK.replace("{pb}", str(pb)),
                 "nav.html": NAVP.replace("{pa}", str(pa)), "shadow.html": SHADOW.replace("{pb}", str(pb)),
                 "vlink.html": VLINK.replace("{pa}", str(pa)), "vsw.js": VSW, "swreg.html": SWREG,
                 "jackl.html": JACKL.replace("{pb}", str(pb)), "secrets.html": SECRETS_PAGE}
        for name, body in files.items():
            with open(os.path.join(web, name), "w", encoding="utf-8") as f:
                f.write(body)
        A = f"http://127.0.0.1:{pa}"
        # The real registration is never touched: this run registers its OWN host name and loads a copy of the
        # extension that calls that name, so no other Chrome on this account can start this run's host.
        real_before = install_host.registered()
        real_manifest_before = install_host.MANIFEST.read_bytes() if install_host.MANIFEST.exists() else None
        test_host = f"com.kincaid.hands_test_{os.getpid()}"
        ext_copy = tempfile.mkdtemp(prefix="hands-ext-")
        shutil.copytree(os.path.join(ROOT, "chrome", "extension"), ext_copy, dirs_exist_ok=True)
        sw_path = os.path.join(ext_copy, "sw.js")
        sw_src = open(sw_path, encoding="utf-8").read()
        assert sw_src.count('const HOST = "com.kincaid.hands";') == 1, "sw.js HOST line moved: update the test"
        open(sw_path, "w", encoding="utf-8", newline="").write(
            sw_src.replace('const HOST = "com.kincaid.hands";', f'const HOST = "{test_host}";'))
        install_host.install(test_host)
        created_key = True
        ch = PipeChrome(env=dict(os.environ), extra_args=[
            "--host-resolver-rules=MAP passwords.google.com 127.0.0.1, MAP app.keeweb.info 127.0.0.1",
            "--disable-features=HttpsUpgrades",
            f"--unsafely-treat-insecure-origin-as-secure=http://app.keeweb.info:{pa}", f"{A}/home.html"])
        loaded = ch.send("Extensions.loadUnpacked", {"path": ext_copy})
        print(f"setup: test Chrome pid {ch.pid}, extension {loaded.get('id')} (pinned {EXTENSION_ID}), servers :{pa} :{pb}")
        assert loaded.get("id") == EXTENSION_ID, "extension id differs from the pinned id"
        probe = Pipe()  # the test's own client: readiness + the two test seams (cancel, host pid)
        t0 = time.time()
        while True:
            try:
                probe.call("hello", connect_wait=1.0)
                break
            except Exception:
                if time.time() - t0 > 15:
                    raise
                time.sleep(0.3)

        env = dict(os.environ, HANDS_TEST_ALLOW="site:127.0.0.1;site:127.0.0.3;join:*;reclaim:*", HANDS_HEADLESS="1")
        params = StdioServerParameters(command=sys.executable, args=["-X", "utf8", os.path.join(ROOT, "mcp_server.py")],
                                       env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()

                async def call(tool, args):
                    t = time.perf_counter()
                    res = await s.call_tool(tool, args)
                    return res, (time.perf_counter() - t) * 1000

                # ---------------- K
                await call("browser_tabs", {})  # warm the door's worker (COM init) — K is the steady-state call
                res, ms = await call("browser_tabs", {})
                tabs = json.loads(text_of(res))
                her = next(t for t in tabs if t["url"].endswith("/home.html"))
                fields = all(all(k in t for k in ("id", "title", "url", "active", "mine")) for t in tabs)
                row("K", (not res.isError) and fields and her["active"] and not her["mine"] and ms < 300,
                    f"browser_tabs {ms:.0f} ms, {len(tabs)} tab(s), the person's tab {her['title']!r} active={her['active']} mine={her['mine']}")

                # ---------------- L
                cur0 = win32api.GetCursorPos()
                res, ms = await call("browser_open", {"url": f"{A}/page.html", "session": "test"})
                o = json.loads(text_of(res))
                tab = o["id"]
                tabs = json.loads(text_of((await call("browser_tabs", {}))[0]))
                her_now = next(t for t in tabs if t["id"] == her["id"])
                cur1 = win32api.GetCursorPos()
                g = o.get("group") or {}
                row("L", o["active"] is False and g.get("title") == "\U0001F43A test" and g.get("color") == "pink"
                    and her_now["active"] and cur0 == cur1 and ms < 3000,
                    f"open {ms:.0f} ms, active={o['active']}, group {g.get('title')!r}/{g.get('color')}, the person's tab active="
                    f"{her_now['active']}, OS cursor {cur0}->{cur1}")
                await asyncio.sleep(0.6)

                # ---------------- M
                res, ms = await call("browser_look", {"tab": tab})
                full = text_of(res)
                imgs = [c for c in res.content if c.type == "image"]
                import base64
                jpeg_ok = bool(imgs) and base64.b64decode(imgs[0].data)[:3] == b"\xff\xd8\xff"
                has_btn = '] button "Press me"' in full and '] textbox "Query"' in full
                pruned = ("InlineTextBox" not in full) and ("generic\n" not in full) and not any(
                    l.strip().split("] ", 1)[-1] == "generic" for l in full.splitlines())
                still_bg = not next(t for t in json.loads(text_of((await call("browser_tabs", {}))[0])) if t["id"] == tab)["active"]
                row("M", (not res.isError) and has_btn and pruned and jpeg_ok and still_bg,
                    f"look {ms:.0f} ms, {len(full)} chars, button+textbox lines={has_btn}, pruned={pruned}, "
                    f"JPEG {len(base64.b64decode(imgs[0].data)) if imgs else 0} B, tab still background={still_bg}")
                print("   sample:", " | ".join(full.splitlines()[:6]))
                sess = ch.page_session("/page.html")  # the test's own eyes on that page

                # ---------------- N
                lid = look_id(full)
                cur0 = win32api.GetCursorPos()
                res, ms = await call("browser_act", {"tab": tab, "look": lid, "actions": [{"click": ref_of(full, 'button "Press me"')}],
                                                     "then": "tree"})
                after = text_of(res)
                cur1 = win32api.GetCursorPos()
                log = ch.evaluate(sess, "JSON.stringify(window.log)")
                clicks = [e for e in json.loads(log) if e["k"] == "click"]
                tabs = json.loads(text_of((await call("browser_tabs", {}))[0]))
                act_ok = next(t for t in tabs if t["id"] == her["id"])["active"] and not next(t for t in tabs if t["id"] == tab)["active"]
                row("N", len(clicks) == 1 and clicks[0]["trusted"] and clicks[0]["focus"] and cur0 == cur1 and act_ok
                    and "--- after ---" in after,
                    f"act {ms:.0f} ms, page recorded {clicks}, OS cursor {cur0}->{cur1}, the person's tab still active={act_ok}")

                # ---------------- O
                lid = look_id(after)
                body = full  # refs are stable within a document; the closing look was a diff
                q, nm, color = ref_of(body, 'textbox "Query"'), ref_of(body, 'textbox "Name"'), ref_of(body, 'combobox "Color"')
                res, ms = await call("browser_act", {"tab": tab, "look": lid, "then": "tree", "actions": [
                    {"type": "مرحبا world", "into": q}, {"set": nm, "value": "new value"},
                    {"select": color, "value": "Green"}, {"key": "Enter", "into": q}]})
                after = text_of(res)
                log = json.loads(ch.evaluate(sess, "JSON.stringify(window.log)"))
                vals = ch.evaluate(sess, "JSON.stringify([q.value, nm.value, color.value])")
                kinds = [e["k"] for e in log]
                ok_type = any(e["k"] == "input-q" and e["v"] == "مرحبا world" for e in log)
                ok_set = "input-nm" in kinds and "change-nm" in kinds and json.loads(vals)[1] == "new value"
                ok_sel = any(e["k"] == "change-color" and e["v"] == "g" for e in log)
                ok_sub = any(e["k"] == "submit" and e["q"] == "مرحبا world" for e in log)
                row("O", ok_type and ok_set and ok_sel and ok_sub and "refused" not in after.split("--- after ---")[0],
                    f"act {ms:.0f} ms; values {vals}; events {kinds[1:]}")
                print("   act log:", after.split("--- after ---")[0].replace("\n", " | "))

                # ---------------- P
                lid = look_id(after)
                res, _ = await call("browser_look", {"tab": tab, "diff": False, "screenshot": False})
                full2 = text_of(res)
                lid = look_id(full2)
                res, _ = await call("browser_act", {"tab": tab, "look": lid, "actions": [{"click": ref_of(full2, 'button "Add item"')}],
                                                    "then": "none"})
                res, _ = await call("browser_look", {"tab": tab, "screenshot": False})
                d = text_of(res)
                dbody = d.split("\n", 1)[1]
                dlines = [l for l in dbody.splitlines()[1:] if l.strip()]
                row("P", dbody.startswith("diff vs look") and any(l.startswith("+ ") and "Item 2" in l for l in dlines)
                    and all(l[:2] in ("+ ", "- ", "~ ") for l in dlines) and len(d) < len(full2),
                    f"diff {len(d)} chars vs full {len(full2)}; {dbody.splitlines()[0]!r}; lines {dlines}")
                last = d

                # ---------------- T (right-to-left and non-Latin text through the whole chain: type, then read back
                # through a look)
                lid = look_id(last)
                heb = "مرحبا بالعالم · Grüße · мир · 世界"
                res, _ = await call("browser_act", {"tab": tab, "look": lid, "then": "tree",
                                                    "actions": [{"set": ref_of(full, 'textbox "Query"'), "value": ""},
                                                                {"type": heb, "into": ref_of(full, 'textbox "Query"')}]})
                after = text_of(res)
                page_val = ch.evaluate(sess, "document.getElementById('q').value")
                res, _ = await call("browser_look", {"tab": tab, "find": "Query", "screenshot": False})
                found = text_of(res)
                row("T", page_val == heb and f'value="{heb}"' in found and f'value="{heb}"' in after,
                    f"page value {page_val!r}; look reads back: {[l for l in found.splitlines() if 'Query' in l][:1]}")

                # ---------------- R (deny + consent, before anything opens or attaches)
                n_tabs = len(json.loads(text_of((await call("browser_tabs", {}))[0])))
                denied = {}
                for u in ("chrome://settings", "chrome-extension://abcdefghijklmnopabcdefghijklmnop/x.html", "edge://settings",
                          "about:config", "https://passwords.google.com/", f"http://passwords.google.com:{pa}/pm.html",
                          "https://vault.bitwarden.com/", "javascript:alert(1)", "file:///C:/Windows/win.ini"):
                    res, _ = await call("browser_open", {"url": u, "session": "test"})
                    denied[u] = res.isError
                res, _ = await call("browser_open", {"url": f"http://localhost:{pa}/page.html", "session": "test"})
                consent_msg = text_of(res)
                lid = look_id(last if "look " in last else full)
                res, _ = await call("browser_look", {"tab": tab, "screenshot": False})
                lid = look_id(text_of(res))
                res, _ = await call("browser_act", {"tab": tab, "look": lid, "actions": [{"goto": "chrome://version"}], "then": "none"})
                goto_denied = res.isError or "never" in text_of(res)
                n_after = len(json.loads(text_of((await call("browser_tabs", {}))[0])))
                row("R", all(denied.values()) and res.isError is not None and "needs_consent" in consent_msg
                    and goto_denied and n_after == n_tabs and HITS.get("/pm.html", 0) == 0,
                    f"refused {sum(denied.values())}/{len(denied)} opens; localhost -> {consent_msg.strip()[:60]!r}; "
                    f"goto chrome:// refused={goto_denied}; tabs {n_tabs}->{n_after}; vault page fetched {HITS.get('/pm.html', 0)}x")

                # ---------------- U (per-frame gate, coordinator ruling)
                res, _ = await call("browser_open", {"url": f"{A}/frames.html", "session": "test"})
                ftab = json.loads(text_of(res))["id"]
                await asyncio.sleep(1.5)
                res, _ = await call("browser_look", {"tab": ftab, "screenshot": False})
                fl = text_of(res)
                fsess = ch.page_session("/frames.html")
                print("   frames look:", " | ".join(l.strip() for l in fl.splitlines() if "frame" in l.lower() or "Iframe" in l))
                read_same = 'button "Inner button"' in fl
                unread = "Secret B" not in fl and "Vault" not in fl and "Data secret" not in fl
                no_leak_in_refusals = True  # filled after the acts below
                lines_ok = ("[frame needs consent: 127.0.0.2]" in fl and "[frame denied: app.keeweb.info]" in fl and HITS.get("/pm.html", 0) > 0
                            and fl.count("[frame denied:") >= 2)
                # play the person/the page: focus lands inside the unconsented frame, then inside the vault frame
                targets = ch.send("Target.getTargets")["targetInfos"]
                bt = next(t for t in targets if t["type"] == "iframe" and "/b.html" in t["url"])
                pt = next(t for t in targets if t["type"] == "iframe" and "/pm.html" in t["url"])
                bs = ch.send("Target.attachToTarget", {"targetId": bt["targetId"], "flatten": True})["sessionId"]
                ps = ch.send("Target.attachToTarget", {"targetId": pt["targetId"], "flatten": True})["sessionId"]
                ch.evaluate(fsess, "document.getElementById('fb').focus()")
                ch.evaluate(bs, "document.getElementById('bi').focus()")
                lid = look_id(fl)
                r1, _ = await call("browser_act", {"tab": ftab, "look": lid, "actions": [{"type": "leak"}], "then": "none"})
                r2, _ = await call("browser_act", {"tab": ftab, "look": lid, "actions": [{"key": "a"}], "then": "none"})
                ch.evaluate(fsess, "document.getElementById('fc').focus()")
                ch.evaluate(ps, "document.getElementById('pi').focus()")
                r3, _ = await call("browser_act", {"tab": ftab, "look": lid, "actions": [{"type": "leak"}], "then": "none"})
                # clicking the <iframe> element of the other origin = clicking into it: refused by the hit test
                frame_ref = ref_of(fl, "Iframe")
                ifr = [l for l in fl.splitlines() if "Iframe" in l]
                refs_b = [int(l.strip()[1:l.strip().index("]")]) for l in ifr if l.strip().startswith("[")]
                r4s = []
                for rf in refs_b:
                    r4, _ = await call("browser_act", {"tab": ftab, "look": lid, "actions": [{"click": rf}], "then": "none"})
                    r4s.append(text_of(r4))
                print(f"   vault frame target url {pt['url']!r}, fetched {HITS.get('/pm.html', 0)}x; B fetched {HITS.get('/b.html', 0)}x")
                bval = ch.evaluate(bs, "document.getElementById('bi').value + '|' + (window.hit||0)")
                pval = ch.evaluate(ps, "document.getElementById('pi').value")
                refused = [("refused" in text_of(x) and "frame" in text_of(x)) for x in (r1, r2, r3)]
                no_leak_in_refusals = not any(w in t for t in r4s + [text_of(x) for x in (r1, r2, r3)]
                                              for w in ("Data secret", "Secret B", "Vault"))
                clicked_into = sum(1 for t in r4s if "clicked" in t)
                # same-origin iframe: allowed, and typing there works
                ii = ref_of(fl, 'textbox "Inner field"')
                r5, _ = await call("browser_act", {"tab": ftab, "look": lid, "actions": [{"type": "ok", "into": ii}], "then": "none"})
                print("   same-origin type:", text_of(r5).strip()[:120], "| B:", text_of(r1).strip()[:90], "| vault:", text_of(r3).strip()[:60])
                isess_val = ch.evaluate(fsess, "document.getElementById('fa').contentDocument.getElementById('ii').value")
                row("U", read_same and unread and no_leak_in_refusals and lines_ok and all(refused) and bval == "|0" and pval == ""
                    and clicked_into <= 1 and isess_val == "ok" and frame_ref > 0,
                    f"same-origin frame read={read_same}; B/vault unread={unread}; frame lines={lines_ok}; "
                    f"type/key into B, type into vault refused={refused}; B after={bval!r} vault after={pval!r}; "
                    f"iframe-element clicks: {[t.split(chr(10))[0][:70] for t in r4s]}; same-origin type -> {isess_val!r}")

                # ---------------- V (F1: a screenshot never shows a frame I may not read)
                import io
                from PIL import Image as PILImage
                res, _ = await call("browser_look", {"tab": ftab, "screenshot": True, "diff": False})
                vtext = text_of(res)
                vimgs = [c for c in res.content if c.type == "image"]
                geo = json.loads(ch.evaluate(fsess, "JSON.stringify({dpr: devicePixelRatio, r: ['fa','fb','fc','fd'].map(i => {"
                                                    " const b = document.getElementById(i).getBoundingClientRect();"
                                                    " return [b.left, b.top, b.right, b.bottom]; })})"))

                def magenta(img, rect, dpr):
                    x0, y0, x1, y1 = (int(round(v * dpr)) for v in rect)
                    box = img.convert("RGB").crop((x0 + 4, y0 + 4, x1 - 4, y1 - 4))
                    return sum(1 for (r_, g_, b_) in box.getdata() if r_ > 190 and g_ < 90 and b_ > 190)

                got = PILImage.open(io.BytesIO(base64.b64decode(vimgs[0].data))) if vimgs else None
                counts = [magenta(got, rc, geo["dpr"]) for rc in geo["r"]] if got else None
                # the test's own eyes, unredacted: those frames really do paint magenta in this hidden tab
                rawshot = ch.send("Page.captureScreenshot", {"format": "png"}, session=fsess, timeout=10)
                rawimg = PILImage.open(io.BytesIO(base64.b64decode(rawshot["data"])))
                rawcounts = [magenta(rawimg, rc, geo["dpr"]) for rc in geo["r"]]
                header_ok = "screenshot: 3 frame(s) covered" in vtext
                row("V", bool(got) and header_ok and counts[0] > 0 and counts[1:] == [0, 0, 0] and all(c > 0 for c in rawcounts),
                    f"{'header ok' if header_ok else 'HEADER: ' + vtext.splitlines()[1][:80]}; magenta px in my screenshot "
                    f"[same-origin, 127.0.0.2, vault, data:] = {counts}; in the test's unredacted capture = {rawcounts}; "
                    f"image {got.size if got else None} dpr {geo['dpr']}")

                # ---------------- V2 (F1 fail-closed: a denied frame I can't place = no screenshot at all)
                res, _ = await call("browser_open", {"url": f"{A}/nestouter.html", "session": "test"})
                ntab = json.loads(text_of(res))["id"]
                await asyncio.sleep(2.0)
                res, _ = await call("browser_look", {"tab": ntab, "screenshot": True, "diff": False})
                ntext = text_of(res)
                nimgs = [c for c in res.content if c.type == "image"]
                nsess = ch.page_session("/nestouter.html")
                ngeo = json.loads(ch.evaluate(nsess, "JSON.stringify({dpr: devicePixelRatio, r: (b => [b.left, b.top, b.right, b.bottom])"
                                                     "(document.getElementById('nc').getBoundingClientRect())})"))
                print('   V2 look header:', ' | '.join(ntext.splitlines()[:3]))
                shot = None
                for _ in range(3):  # the test's own capture of a fresh hidden tab: its first frame can be slow
                    try:
                        shot = ch.send("Page.captureScreenshot", {"format": "png"}, session=nsess, timeout=10)
                        break
                    except TimeoutError:
                        continue
                nraw = PILImage.open(io.BytesIO(base64.b64decode(shot["data"])))
                nested_paints = magenta(nraw, ngeo["r"], ngeo["dpr"])
                withheld = "no screenshot: withheld" in ntext
                row("V2", not nimgs and withheld and nested_paints > 0 and "[frame denied: app.keeweb.info]" in ntext,
                    f"images returned {len(nimgs)}; note {[l for l in ntext.splitlines() if 'screenshot' in l][:1]}; nested vault "
                    f"paints {nested_paints} magenta px in the test's own capture; listed={'[frame denied: app.keeweb.info]' in ntext}")

                def all_magenta(img):
                    return sum(1 for (r_, g_, b_) in img.convert("RGB").getdata() if r_ > 190 and g_ < 90 and b_ > 190)

                # ---------------- V3 (F2.1: a cross-site denied frame toggling in and out never shows)
                res, _ = await call("browser_open", {"url": f"{A}/flicker.html", "session": "test"})
                vtab = json.loads(text_of(res))["id"]
                await asyncio.sleep(1.5)
                outcomes = {"covered": 0, "changed": 0, "clean": 0, "other_withheld": 0}
                leaks = []
                import random
                for i in range(16):
                    res, _ = await call("browser_look", {"tab": vtab, "screenshot": True})
                    t3 = text_of(res)
                    im = [c for c in res.content if c.type == "image"]
                    if im:
                        n = all_magenta(PILImage.open(io.BytesIO(base64.b64decode(im[0].data))))
                        if n > 30:
                            leaks.append((i, n))
                        outcomes["covered" if "screenshot: 1 frame(s) covered" in t3 else "clean"] += 1
                    elif "frames changed during capture" in t3:
                        outcomes["changed"] += 1
                    else:
                        outcomes["other_withheld"] += 1
                        print("   V3 other:", [l[:140] for l in t3.splitlines() if "screenshot" in l])
                    await asyncio.sleep(random.uniform(0.05, 0.45))
                fsess3 = ch.page_session("/flicker.html")
                ch.evaluate(fsess3, "window.__pin = true")  # hold the frame in place, then prove it paints
                painted, t_end = 0, time.time() + 25
                while painted <= 1000 and time.time() < t_end:  # the test's own eyes, sampled until seen (bounded)
                    try:
                        sh = ch.send("Page.captureScreenshot", {"format": "png"}, session=fsess3, timeout=10)
                        painted = max(painted, all_magenta(PILImage.open(io.BytesIO(base64.b64decode(sh["data"])))))
                    except TimeoutError:
                        pass
                    await asyncio.sleep(0.3)
                # the contract's V3 is safety: every look covers the frame or withholds for "frames changed", none
                # leaks; how many land "covered" on a page that flickers every 2 s is timing, not correctness (V/V4 prove liveness)
                row("V3", not leaks and outcomes["covered"] + outcomes["changed"] == 16 and outcomes["other_withheld"] == 0 and painted > 1000,
                    f"16 looks -> {outcomes}; screenshots with magenta: {leaks or 'none'}; the frame paints {painted} px in the test's own capture")

                # ---------------- V4 (F2.2: a box I can't read = no screenshot; a frame proven not rendered = counted, not blocking)
                res, _ = await call("browser_open", {"url": f"{A}/hidden.html", "session": "test"})
                htab4 = json.loads(text_of(res))["id"]
                await asyncio.sleep(1.5)
                res, _ = await call("browser_look", {"tab": htab4, "screenshot": True, "diff": False})
                t4 = text_of(res)
                img4 = [c for c in res.content if c.type == "image"]
                hidden_ok = bool(img4) and "screenshot: 0 frame(s) covered, 1 not rendered" in t4 and "[frame denied: app.keeweb.info]" in t4
                probe.call("debug.faults", {"boxError": True})
                res, _ = await call("browser_look", {"tab": ftab, "screenshot": True, "diff": False})
                t4b = text_of(res)
                img4b = [c for c in res.content if c.type == "image"]
                probe.call("debug.faults", {})
                res, _ = await call("browser_look", {"tab": ftab, "screenshot": True, "diff": False})
                back4 = bool([c for c in res.content if c.type == "image"])
                row("V4", hidden_ok and not img4b and "withheld" in t4b and back4,
                    f"display:none denied frame -> image={bool(img4)} {[l for l in t4.splitlines() if l.startswith('screenshot')]}; "
                    f"box-query fault -> images {len(img4b)} {[l[:110] for l in t4b.splitlines() if 'no screenshot' in l]}; "
                    f"fault cleared -> image again={back4}")

                # ---------------- V5 (F2.3: a cross-site frame whose own auto-attach failed = no screenshot)
                probe.call("debug.faults", {"autoAttachFail": True})
                res, _ = await call("browser_open", {"url": f"{A}/frames.html?v5", "session": "test"})
                t5tab = json.loads(text_of(res))["id"]
                await asyncio.sleep(1.5)
                res, _ = await call("browser_look", {"tab": t5tab, "screenshot": True, "diff": False})
                t5 = text_of(res)
                img5 = [c for c in res.content if c.type == "image"]
                probe.call("debug.faults", {})
                row("V5", not img5 and "auto-attach" in t5,
                    f"images {len(img5)}; {[l[:120] for l in t5.splitlines() if 'no screenshot' in l]}")

                # ---------------- E5 the test-hook gate: off on the real pipe name, off without HANDS_TEST_HOOKS
                gates = {}
                for label, extra in (("default pipe + hooks=1", {"HANDS_TEST_HOOKS": "1", "HANDS_CHROME_PIPE": ""}),
                                     ("test pipe, no hooks", {"HANDS_TEST_HOOKS": "", "HANDS_CHROME_PIPE": "x-test"}),
                                     ("test pipe + hooks=1", {"HANDS_TEST_HOOKS": "1", "HANDS_CHROME_PIPE": "x-test"})):
                    e5 = {k: v for k, v in os.environ.items() if k not in ("HANDS_TEST_HOOKS", "HANDS_CHROME_PIPE")}
                    e5.update({k: v for k, v in extra.items() if v})
                    out5 = subprocess.run([sys.executable, "-X", "utf8", "-c",
                                           "import sys; sys.path.insert(0, r'%s'); sys.argv = ['x']; "
                                           "import chrome.host as h; print(h.TEST_HOOKS)" % ROOT],
                                          capture_output=True, text=True, env=e5, timeout=30)
                    gates[label] = out5.stdout.strip()
                row("E5", gates == {"default pipe + hooks=1": "False", "test pipe, no hooks": "False", "test pipe + hooks=1": "True"},
                    f"TEST_HOOKS by env: {gates}")

                async def tabs_now():
                    return {t["id"]: t for t in json.loads(text_of((await call("browser_tabs", {}))[0]))}

                async def fresh_look(tb):
                    r_, _ = await call("browser_look", {"tab": tb, "screenshot": False, "diff": False})
                    return text_of(r_)

                # ---------------- W1 (S1: what my input opens is mine, grouped, and never takes the person's active tab)
                res, _ = await call("browser_open", {"url": f"{A}/blank.html", "session": "test"})
                btab = json.loads(text_of(res))["id"]
                await asyncio.sleep(1.0)
                mygroup = (await tabs_now())[btab]["group"]
                w1, adopted = {}, []
                for label, act in (("target=_blank click", lambda L: {"click": ref_of(L, 'link "open in new tab"')}),
                                   ("Enter on the link", lambda L: {"key": "Enter", "into": ref_of(L, 'link "open in new tab"')}),
                                   ("middle-click", lambda L: {"click": ref_of(L, 'link "open in new tab"'), "button": "middle"}),
                                   ("window.open popup", lambda L: {"click": ref_of(L, 'button "popup"')})):
                    before = await tabs_now()
                    L = await fresh_look(btab)
                    await call("browser_act", {"tab": btab, "look": look_id(L), "actions": [act(L)], "then": "none"})
                    await asyncio.sleep(1.2)
                    after = await tabs_now()
                    new = [t for i, t in after.items() if i not in before]
                    # a popup window's only tab is active within ITS window and can't join a group: the person's tab is what counts
                    popup = label == "window.open popup"
                    ok = (len(new) == 1 and after[her["id"]]["active"] and new[0]["mine"]
                          and (popup or (not new[0]["active"] and new[0]["group"] == mygroup)))
                    w1[label] = (ok, [(t["url"].split("/")[-1], t["active"], t["mine"], t["group"] == mygroup) for t in new])
                    adopted += [t["id"] for t in new]
                if not all(v[0] for v in w1.values()):
                    print("   W1 trace:", probe.call("debug.trace")["trace"][-12:])
                row("W1", all(v[0] for v in w1.values()), f"the person's tab stayed active; new tab (url, active, mine, in my group): {w1}")

                # ---------------- W9 (S8.2: the person's page opening a tab, even the same URL during my act, never becomes mine)
                hsess = ch.page_session("/home.html")
                L = await fresh_look(btab)
                before9 = await tabs_now()
                ch.send("Runtime.evaluate", {"expression": "setTimeout(() => window.open('/second.html?blank'), 250); 1",
                                             "userGesture": True}, session=hsess)
                await call("browser_act", {"tab": btab, "look": look_id(L), "actions": [{"click": ref_of(L, 'link "open in new tab"')}],
                                           "then": "none"})
                await asyncio.sleep(1.5)
                after9 = await tabs_now()
                new9 = [t for i, t in after9.items() if i not in before9]
                hers9 = [t for t in new9 if not t["mine"]]
                mine9 = [t for t in new9 if t["mine"]]
                adopted += [t["id"] for t in mine9]
                row("W9", len(new9) == 2 and len(hers9) == 1 and len(mine9) == 1 and not after9[her["id"]]["mine"],
                    f"new tabs (url, mine): {[(t['url'].split('/')[-1], t['mine']) for t in new9]}; the person's tab mine={after9[her['id']]['mine']}")

                # ---------------- W2 (S2: the page moves a frame under my click on mousemove)
                res, _ = await call("browser_open", {"url": f"{A}/jack.html#move", "session": "test"})
                jtab = json.loads(text_of(res))["id"]
                await asyncio.sleep(1.5)
                L = await fresh_look(jtab)
                hits0 = {k: v for k, v in HITS.items() if k.startswith("/hit-b-")}
                res, _ = await call("browser_act", {"tab": jtab, "look": look_id(L), "actions": [{"click": ref_of(L, 'button "Press here"')}],
                                                    "then": "none"})
                t2 = text_of(res)
                jsess = ch.page_session("/jack.html")
                clicked = ch.evaluate(jsess, "window.clicked || 0")
                await asyncio.sleep(0.5)
                hits1 = {k: v for k, v in HITS.items() if k.startswith("/hit-b-")}
                pressed_in_b = {k: hits1[k] - hits0.get(k, 0) for k in hits1 if k in ("/hit-b-mousedown", "/hit-b-click", "/hit-b-pointerdown") and hits1[k] - hits0.get(k, 0)}
                row("W2", "moved something under my click" in t2 and "clicked [" not in t2 and clicked == 0 and not pressed_in_b,
                    f"act -> {t2.strip()[:110]!r}; button clicks={clicked}; presses reaching the denied frame={pressed_in_b or 'none'}")

                # ---------------- W3 (S3 + F3.5 back/forward + F3.2: navigation onto a denied page is blocked before it loads)
                vault0 = HITS.get("/pm.html", 0)
                res, _ = await call("browser_open", {"url": f"{A}/nav.html", "session": "test"})
                ntab3 = json.loads(text_of(res))["id"]
                await asyncio.sleep(1.0)
                L = await fresh_look(ntab3)
                res, _ = await call("browser_act", {"tab": ntab3, "look": look_id(L), "actions": [{"click": ref_of(L, 'button "go vault"')}],
                                                    "then": "none"})
                t3a = text_of(res)
                await asyncio.sleep(0.8)
                url3a = (await tabs_now())[ntab3]["url"]

                def doc_of(url_part):  # the test's own eyes on what that tab really shows
                    tg = [t for t in ch.send("Target.getTargets")["targetInfos"] if t["type"] == "page" and url_part in t["url"]]
                    if not tg:
                        return "no such page"
                    ss = ch.send("Target.attachToTarget", {"targetId": tg[0]["targetId"], "flatten": True})["sessionId"]
                    v = ch.evaluate(ss, "location.href + ' | ' + document.documentElement.innerHTML.includes('ERR_BLOCKED_BY_CLIENT')")
                    ch.send("Target.detachFromTarget", {"sessionId": ss})
                    return str(v)
                shown3a = url3a
                stayed3a = url3a.startswith("about:blank")
                res, _ = await call("browser_open", {"url": f"{A}/redir?to=http://app.keeweb.info:{pa}/pm.html", "session": "test"})
                rtab = json.loads(text_of(res)).get("id")
                await asyncio.sleep(1.5)
                url3b = (await tabs_now()).get(rtab, {}).get("url", "")
                shown3b = url3b
                # forward onto an unconsented site: refused before moving; the batch's earlier action stays in the log
                res, _ = await call("browser_open", {"url": f"{A}/nav.html?fwd", "session": "test"})
                ntab3 = json.loads(text_of(res))["id"]
                await asyncio.sleep(1.0)
                L = await fresh_look(ntab3)
                await call("browser_act", {"tab": ntab3, "look": look_id(L), "actions": [{"click": ref_of(L, 'link "to localhost"')}], "then": "none"})
                await asyncio.sleep(1.0)
                nsess3 = ch.page_session("localhost")
                ch.evaluate(nsess3, "history.back()")
                await asyncio.sleep(1.0)
                L = await fresh_look(ntab3)
                res, _ = await call("browser_act", {"tab": ntab3, "look": look_id(L), "actions": [{"wait": 0}, {"forward": True}], "then": "none"})
                t3c = text_of(res)
                await asyncio.sleep(0.5)
                url3c = (await tabs_now())[ntab3]["url"]
                row("W3", HITS.get("/pm.html", 0) == vault0 and "blocked: my tab tried to load app.keeweb.info" in t3a
                    and stayed3a and url3b.startswith("about:blank") and not res.isError and t3c.startswith("0: waited")
                    and "refused: needs_consent" in t3c and "nav.html" in url3c,
                    f"script nav -> {t3a.strip()[:90]!r}, tab shows {shown3a[:70]!r}; redirect open -> {shown3b[:70]!r}; "
                    f"vault fetched {HITS.get('/pm.html', 0) - vault0}x; forward to unconsented -> {t3c.replace(chr(10), ' | ')[:120]!r}, tab at {url3c.split('/')[-1]!r}")

                # ---------------- W6 (F3.4: focus in a frame inside a CLOSED shadow root is found; typing there refused)
                res, _ = await call("browser_open", {"url": f"{A}/shadow.html", "session": "test"})
                stab = json.loads(text_of(res))["id"]
                await asyncio.sleep(1.5)
                L = await fresh_look(stab)
                ssess = ch.page_session("/shadow.html")
                ch.evaluate(ssess, "window.__sf.focus()")
                bt6 = next(t for t in ch.send("Target.getTargets")["targetInfos"] if t["type"] == "iframe" and "/b.html" in t["url"]
                           and t.get("openerId") is None and t["targetId"] not in ())
                bs6 = ch.send("Target.attachToTarget", {"targetId": bt6["targetId"], "flatten": True})["sessionId"]
                ch.evaluate(bs6, "document.getElementById('bi').focus()")
                r6a, _ = await call("browser_act", {"tab": stab, "look": look_id(L), "actions": [{"type": "x"}], "then": "none"})
                r6b, _ = await call("browser_act", {"tab": stab, "look": look_id(L), "actions": [{"key": "a"}], "then": "none"})
                vals6 = [ch.evaluate(bs6, "document.getElementById('bi').value")]
                row("W6", "refused" in text_of(r6a) and "frame" in text_of(r6a) and "refused" in text_of(r6b) and vals6 == [""],
                    f"type -> {text_of(r6a).strip()[:90]!r}; key -> {text_of(r6b).strip()[:60]!r}; field in the shadow frame {vals6}")

                # ---------------- W8 (S7: an unknown tab id says so)
                res, _ = await call("browser_look", {"tab": 2 ** 40})
                row("W8", res.isError and "no such tab" in text_of(res), text_of(res).strip()[:90])

                # ---------------- W5 (F3.3: an engine that leaves releases its tabs; one that dies is released at the next hello)
                # a second engine is a second holder to the bouncer: W5 is about tab cleanup, so it gets its own lease
                params2 = StdioServerParameters(command=sys.executable, args=["-X", "utf8", os.path.join(ROOT, "mcp_server.py")],
                                                env=dict(env, HANDS_BOUNCER_DIR=tempfile.mkdtemp(prefix="hands-bt-bouncer2-")))
                async with stdio_client(params2) as (r2, w2):
                    async with ClientSession(r2, w2) as s2:
                        await s2.initialize()
                        g_res = text_of(await s2.call_tool("browser_open", {"url": f"{A}/second.html?graceful", "session": "other"}))
                        print("   W5 second engine open:", g_res[:160])
                        g_tab = json.loads(g_res)["id"]
                await asyncio.sleep(1.0)
                graceful_gone = g_tab not in await tabs_now()
                killer = subprocess.Popen([sys.executable, "-X", "utf8", "-c",
                                           "import sys, time, json; sys.path.insert(0, r'%s')\n"
                                           "from engine import session, browser\n"
                                           "h = session.Hands(overlay=False); b = browser.Browser(h)\n"
                                           "print(json.dumps(b.open(sys.argv[1], 'dying')['id']), flush=True); time.sleep(120)" % ROOT,
                                           f"{A}/second.html?dying"], stdout=subprocess.PIPE, text=True,
                                          env=dict(env, HANDS_TEST_ALLOW="site:127.0.0.1"))
                d_tab = json.loads(killer.stdout.readline())
                await asyncio.sleep(0.8)
                was_there = d_tab in await tabs_now()
                killer.kill()
                killer.wait(10)
                await asyncio.sleep(0.5)
                still_after_kill = d_tab in await tabs_now()
                newcomer = Pipe()
                newcomer.call("hello")  # a new engine connects: its hello releases the dead engine's tab
                released = newcomer.last_hello
                newcomer.close()
                await asyncio.sleep(0.5)
                hard_gone = d_tab not in await tabs_now()
                row("W5", graceful_gone and was_there and still_after_kill and hard_gone,
                    f"graceful exit closed its tab={graceful_gone}; killed engine's tab open after the kill={still_after_kill}, "
                    f"released at the next hello={hard_gone} ({released})")

                # ---------------- W4 (S4: the worker comes back by itself after Chrome closes it)
                sws = [x for x in ch.send("Target.getTargets")["targetInfos"] if x["type"] == "service_worker" and EXTENSION_ID in x["url"]]
                for x in sws:
                    ch.send("Target.closeTarget", {"targetId": x["targetId"]})
                t4 = time.time()
                back4 = None
                await asyncio.sleep(2)
                while time.time() - t4 < 50:
                    r4, _ = await call("browser_tabs", {})
                    if not r4.isError:
                        back4 = time.time() - t4
                        break
                    msg4 = text_of(r4)
                    await asyncio.sleep(2)
                row("W4", bool(sws) and back4 is not None and back4 < 45,
                    f"closed {len(sws)} worker target(s); tools back after {back4 and round(back4, 1)} s"
                    + ("" if back4 else f"; last error {msg4[:120]!r}"))

                # ---------------- W7 (F3.5: one stop stops both surfaces; each resumes only through its own look)
                from engine import session as hsession, browser as hbrowser
                hs = hsession.Hands(overlay=False)
                hs.consent.session.update({"site:127.0.0.1", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"})
                hb = hbrowser.Browser(hs)
                st = hb.open(f"{A}/page.html?stops", "stops")["id"]
                await asyncio.sleep(1.0)
                calc = hs.launch("calc")["id"]
                w7 = {}
                try:
                    def refused(fn):
                        try:
                            fn()
                            return False
                        except hsession.Stopped:
                            return True

                    def b_act():
                        txt = hb.act(st, [{"wait": 0}], hb.looks[st].id if st in hb.looks else hb.look(st, screenshot=False)[0] and hb.looks[st].id, "none")[0]
                        if "STOPPED" in txt:
                            raise hsession.Stopped(txt)
                    hb.look(st, screenshot=False)
                    hs.look(calc, shot=False)
                    hs.ov.stop_requested = True  # Esc×2 / the pill's Stop
                    w7["overlay stop: desktop refused"] = refused(lambda: hs._check_stop("desktop"))
                    w7["overlay stop: browser refused"] = refused(b_act)
                    hb.look(st, screenshot=False)
                    w7["browser_look resumed browser"] = not refused(b_act)
                    w7["...but not desktop"] = refused(lambda: hs._check_stop("desktop"))
                    hs.look(calc, shot=False)
                    w7["hands_look resumed desktop"] = not refused(lambda: hs._check_stop("desktop"))
                    probe.call("debug.simulateCancel", {"tabId": st})  # the person cancels the debugging bar
                    await asyncio.sleep(0.4)
                    w7["bar cancel: desktop refused (no browser call made)"] = refused(lambda: hs._check_stop("desktop"))
                    hs.look(calc, shot=False)
                    w7["hands_look resumed desktop only"] = not refused(lambda: hs._check_stop("desktop")) and refused(b_act)
                    hb.look(st, screenshot=False)
                    w7["browser_look resumed browser"] = not refused(b_act)
                finally:
                    hb.done()
                    import uiautomation as auto
                    auto.ControlFromHandle(calc).GetWindowPattern().Close()
                # the bar-cancel reached THIS door's engine too (one stop stops every engine); a fresh look resumes it
                r7, _ = await call("browser_open", {"url": f"{A}/second.html?w7", "session": "test"})
                w7["the door's own engine was stopped too"] = "browser use stopped" in text_of(r7)
                await fresh_look(btab)
                row("W7", all(w7.values()), f"{w7}")

                def navlog(since):
                    return [x["text"] for x in probe.call("nav.log")["log"] if x["at"] >= since]

                async def ends_blank_or_closed(tb, within=1.5):
                    t_end = time.time() + within
                    while time.time() < t_end:
                        now = await tabs_now()
                        if tb not in now or now[tb]["url"].startswith("about:blank"):
                            return "closed" if tb not in now else "about:blank"
                        await asyncio.sleep(0.1)
                    return (await tabs_now()).get(tb, {}).get("url", "closed")

                async def look_shows_no_vault(tb):
                    r_, _ = await call("browser_look", {"tab": tb, "screenshot": False, "diff": False})
                    t_ = text_of(r_)
                    return not any(w in t_ for w in ("Vault", "SWVAULT", "vault served"))

                # ---------------- D1 (R4.1: an ADOPTED tab loads the vault: armed at adoption, the commit owner closes it)
                res, _ = await call("browser_open", {"url": f"{A}/vlink.html", "session": "test"})
                vtab = json.loads(text_of(res))["id"]
                await asyncio.sleep(1.0)
                L = await fresh_look(vtab)
                before1, t1 = await tabs_now(), time.time() * 1000 - 50
                v0 = HITS.get("/pm.html", 0)
                res, _ = await call("browser_act", {"tab": vtab, "look": look_id(L), "actions": [{"click": ref_of(L, 'link "vault new tab"')}],
                                                    "then": "none"})
                await asyncio.sleep(0.3)
                log1 = [x for x in probe.call("nav.log")["log"] if x["at"] >= t1 and "keeweb" in x["text"]]
                rec1 = [x["text"] for x in log1]
                adopted1 = [x["tab"] for x in log1 if x["tab"] != vtab]
                end1 = await ends_blank_or_closed(adopted1[0]) if adopted1 else "no record names the new tab"
                row("D1", end1 in ("closed", "about:blank") and rec1,
                    f"vault fetched {HITS.get('/pm.html', 0) - v0}x; the adopted tab ended {end1}; records {rec1[:2]}; act -> {text_of(res).strip()[:90]!r}")

                # ---------------- D3 (script location= onto the vault) and D6 (an explicit goto that passed the gate, then redirected)
                res, _ = await call("browser_open", {"url": f"{A}/nav.html?d3", "session": "test"})
                d3 = json.loads(text_of(res))["id"]
                await asyncio.sleep(1.0)
                L = await fresh_look(d3)
                t3 = time.time() * 1000 - 50
                res, _ = await call("browser_act", {"tab": d3, "look": look_id(L), "actions": [{"click": ref_of(L, 'button "go vault"')}], "then": "none"})
                end3 = await ends_blank_or_closed(d3)
                rec3 = [x for x in navlog(t3) if "keeweb" in x]
                row("D3", end3 == "about:blank" and rec3 and "blocked: my tab" in text_of(res) and await look_shows_no_vault(d3),
                    f"tab ended {end3}; records {rec3[:2]}; act -> {text_of(res).strip().splitlines()[-1][:90]!r}")
                res, _ = await call("browser_open", {"url": f"{A}/second.html?d6", "session": "test"})
                d6 = json.loads(text_of(res))["id"]
                await asyncio.sleep(1.0)
                L = await fresh_look(d6)
                t6 = time.time() * 1000 - 50
                res, _ = await call("browser_act", {"tab": d6, "look": look_id(L), "actions": [{"goto": f"{A}/redir?to=http://app.keeweb.info:{pa}/pm.html?d6"}],
                                                    "then": "none"})
                end6 = await ends_blank_or_closed(d6)
                rec6 = [x for x in navlog(t6) if "keeweb" in x]
                row("D6", end6 == "about:blank" and rec6 and "blocked: my tab" in text_of(res) and await look_shows_no_vault(d6),
                    f"goto passed the gate, redirect -> tab ended {end6}; records {rec6[:2]}; act -> {text_of(res).strip().splitlines()[-1][:90]!r}")

                # ---------------- D4 (a server redirect at open)
                t4d = time.time() * 1000 - 50
                res, _ = await call("browser_open", {"url": f"{A}/redir?to=http://app.keeweb.info:{pa}/pm.html?d4", "session": "test"})
                d4 = json.loads(text_of(res))["id"]
                end4 = await ends_blank_or_closed(d4, 2.5)
                rec4 = [x for x in navlog(t4d) if "keeweb" in x]
                row("D4", end4 == "about:blank" and rec4 and await look_shows_no_vault(d4), f"tab ended {end4}; records {rec4[:2]}")

                # ---------------- J (R4.2: only the browser's own events confirm my click; jackl#D for D = 0,1,2,3,5 ms)
                jr = {}
                for D in (0, 1, 2, 3, 5):
                    res, _ = await call("browser_open", {"url": f"{A}/jackl.html#{D}", "session": "test"})
                    jt = json.loads(text_of(res))["id"]
                    await asyncio.sleep(1.2)
                    L = await fresh_look(jt)
                    res, _ = await call("browser_act", {"tab": jt, "look": look_id(L), "actions": [{"click": ref_of(L, 'button "Press here"')}, {"wait": 0}],
                                                        "then": "none"})
                    tj = text_of(res)
                    js_ = ch.page_session(f"jackl.html#{D}")
                    cnt = ch.evaluate(js_, "window.clicked || 0")
                    kind = ("refused" if "nothing pressed" in tj else "unknown" if "outcome unknown" in tj
                            else "clicked" if "clicked [" in tj else "other")
                    stopped_after = "1: waited" not in tj if kind == "unknown" else True
                    jr[D] = (kind, cnt, stopped_after)
                okJ = all((k in ("refused", "unknown") and st) or (k == "clicked" and c == 1) for k, c, st in jr.values())
                row("J", okJ and not any(k == "clicked" and c == 0 for k, c, _ in jr.values()),
                    f"D -> (outcome, the page's real counter, batch stopped after an unknown) = {jr}")

                # ---------------- D2 (a SERVICE WORKER serves the vault page: no network for my gate; the commit owner leaves it)
                tid2 = ch.send("Target.createTarget", {"url": f"http://app.keeweb.info:{pa}/swreg.html", "background": True})["targetId"]
                ss2 = ch.send("Target.attachToTarget", {"targetId": tid2, "flatten": True})["sessionId"]
                reg = ""
                for _ in range(40):
                    reg = ch.evaluate(ss2, "document.title") or ""
                    if reg.startswith("REG"):
                        break
                    await asyncio.sleep(0.25)
                ch.send("Target.closeTarget", {"targetId": tid2})
                res, _ = await call("browser_open", {"url": f"{A}/vlink.html?d2", "session": "test"})
                d2 = json.loads(text_of(res))["id"]
                await asyncio.sleep(1.0)
                tid2t = next(t["targetId"] for t in ch.send("Target.getTargets")["targetInfos"] if "vlink.html?d2" in t["url"])
                L = await fresh_look(d2)
                t2d = time.time() * 1000 - 50
                res, _ = await call("browser_act", {"tab": d2, "look": look_id(L), "actions": [{"click": ref_of(L, 'button "sw nav"')}], "then": "none"})
                end2 = await ends_blank_or_closed(d2)
                rec2 = [x for x in navlog(t2d) if "keeweb" in x]
                row("D2", reg == "REGOK" and end2 == "about:blank" and rec2 and await look_shows_no_vault(d2),
                    f"service worker on the vault origin: {reg}; tab ended {end2}; records {rec2[:2]}; act -> {text_of(res).strip().splitlines()[-1][:90]!r}")

                # ---------------- D5 (back into a vault entry that DID commit (D2's service-worker page): the page's own
                #                    history.back(), no gate of mine on the way; bfcache or the worker serves it)
                await asyncio.sleep(0.5)
                t5 = time.time() * 1000 - 50
                s5 = ch.send("Target.attachToTarget", {"targetId": tid2t, "flatten": True})["sessionId"]
                try:
                    ch.evaluate(s5, "history.back()")
                except RuntimeError as e:  # the back itself navigates the page out from under the evaluate
                    if "navigated or closed" not in str(e):
                        raise
                seq5 = []
                for _ in range(30):  # where the tab really goes, every 50 ms
                    u5 = (await tabs_now()).get(d2, {}).get("url", "closed")
                    if not seq5 or seq5[-1] != u5:
                        seq5.append(u5)
                    await asyncio.sleep(0.05)
                print("   D5 tab urls:", [x.split("/")[-1][:40] for x in seq5], "| all records:", [x["text"][:50] for x in probe.call("nav.log")["log"] if x["tab"] == d2])
                end5 = await ends_blank_or_closed(d2)
                rec5 = [x for x in navlog(t5) if "keeweb" in x]
                row("D5", end5 == "about:blank" and rec5 and await look_shows_no_vault(d2),
                    f"history.back() into the committed vault entry -> tab ended {end5}; records {rec5[:2]}")

                # ---------------- E1 two looks racing; then the older id is refused
                ra, rb = await asyncio.gather(s.call_tool("browser_look", {"tab": tab, "screenshot": False}),
                                              s.call_tool("browser_look", {"tab": tab, "screenshot": False}))
                ids = sorted([look_id(text_of(ra)), look_id(text_of(rb))])
                res, _ = await call("browser_act", {"tab": tab, "look": ids[0], "actions": [{"wait": 0}], "then": "none"})
                row("E1", not ra.isError and not rb.isError and res.isError and "stale" in text_of(res),
                    f"racing looks {ids}; act with {ids[0]} -> {text_of(res).strip()[:80]!r}")

                # ---------------- E2 navigated between look and act: stale refs refused
                res, _ = await call("browser_look", {"tab": tab, "screenshot": False})
                old = text_of(res)
                lid = look_id(old)
                ch.evaluate(sess, f"location.href = '{A}/second.html'")
                await asyncio.sleep(0.8)
                res, _ = await call("browser_act", {"tab": tab, "look": lid, "actions": [{"click": ref_of(full, 'button "Press me"')}],
                                                    "then": "none"})
                row("E2", "stale" in text_of(res) or "navigated" in text_of(res), text_of(res).strip()[:100])

                # ---------------- E3 a page that never finishes loading: look and act stay bounded
                res, _ = await call("browser_open", {"url": f"{A}/hang", "session": "test"})
                htab = json.loads(text_of(res))["id"]
                await asyncio.sleep(0.8)
                res, ms1 = await call("browser_look", {"tab": htab, "screenshot": True})
                hl = text_of(res)
                ok_l = not res.isError and "Still loading" in hl
                ms2 = 0.0
                if ok_l:
                    res, ms2 = await call("browser_act", {"tab": htab, "look": look_id(hl), "actions": [
                        {"click": ref_of(hl, 'button "Still loading"')}], "then": "both"})
                row("E3", ok_l and ms1 < 10000 and ms2 < 10000 and not res.isError,
                    f"look {ms1:.0f} ms ({'LOADING' in hl and 'page still loading' or 'loaded'}), act+look {ms2:.0f} ms")

                # ---------------- E4 the host dies (service worker reconnects it); the door comes back by itself
                hostpid = probe.call("host.ping")["pid"]
                subprocess.run(["taskkill", "/PID", str(hostpid), "/F"], capture_output=True)
                t = time.perf_counter()
                back = False
                while time.perf_counter() - t < 12:
                    res, _ = await call("browser_tabs", {})
                    if not res.isError:
                        back = True
                        break
                    await asyncio.sleep(0.3)
                probe.close()
                newpid = probe.call("host.ping", connect_wait=3)["pid"]
                row("E4", back and newpid != hostpid, f"host {hostpid} killed; door back in {(time.perf_counter() - t) * 1000:.0f} ms "
                    f"on host {newpid}")

                # ---------------- S (debugger detached: the person cancels the bar / the tab closes)
                res, _ = await call("browser_look", {"tab": htab, "screenshot": False})
                lid = look_id(text_of(res))
                probe.call("debug.simulateCancel", {"tabId": htab})
                res, ms_a = await call("browser_act", {"tab": htab, "look": lid, "actions": [{"wait": 0}], "then": "none"})
                s1 = text_of(res)
                res, ms_b = await call("browser_act", {"tab": htab, "look": lid, "actions": [{"wait": 0}], "then": "none"})
                s2 = text_of(res)
                res, _ = await call("browser_look", {"tab": htab, "screenshot": False})
                resumed = not res.isError
                tid = next(t["targetId"] for t in ch.send("Target.getTargets")["targetInfos"] if t["type"] == "page" and t["url"].endswith("/hang"))
                lid = look_id(text_of(res))
                ch.send("Target.closeTarget", {"targetId": tid})
                await asyncio.sleep(0.4)
                res, ms_c = await call("browser_act", {"tab": htab, "look": lid, "actions": [{"wait": 0}], "then": "both"})
                s3 = text_of(res)
                row("S", s1.startswith("STOPPED: browser use stopped") and s2.startswith("STOPPED: browser use stopped")
                    and resumed and s3.startswith("STOPPED: browser use stopped")
                    and max(ms_a, ms_b, ms_c) < 10000,
                    f"cancel -> {s1.strip()[:70]!r} ({ms_a:.0f} ms), again ({ms_b:.0f} ms) still stopped, fresh look resumes={resumed}; "
                    f"tab closed -> {s3.strip()[:60]!r} ({ms_c:.0f} ms)")

                # ---------------- Q (done closes mine unless kept; never the person's)
                res, _ = await call("browser_open", {"url": f"{A}/page.html?keep", "session": "test"})
                ktab = json.loads(text_of(res))["id"]
                await asyncio.sleep(0.5)
                res, _ = await call("browser_look", {"tab": ktab, "screenshot": False})
                await call("browser_act", {"tab": ktab, "look": look_id(text_of(res)), "actions": [{"keep": True}], "then": "none"})
                before = {t["id"]: t for t in json.loads(text_of((await call("browser_tabs", {}))[0]))}
                res, _ = await call("hands_done", {})
                done_msg = text_of(res)
                await asyncio.sleep(0.3)
                aft = {t["id"]: t for t in json.loads(text_of((await call("browser_tabs", {}))[0]))}
                mine_before = [i for i, t in before.items() if t["mine"]]
                row("Q", her["id"] in aft and ktab in aft and not aft[ktab]["mine"] and tab not in aft and ftab not in aft
                    and not any(t["mine"] for t in aft.values()) and not any(a in aft for a in adopted)
                    and all(t["id"] in aft for t in hers9),
                    f"mine before {mine_before}; after: the person's tab kept={her['id'] in aft}, kept tab open={ktab in aft} "
                    f"(now the person's), others closed={tab not in aft and ftab not in aft}; {done_msg.strip()[:120]!r}")

                # ---------------- J1-J5 (JOIN a tab the person opened, RECLAIM one I handed over)
                async def tabmap():
                    return {t["id"]: t for t in json.loads(text_of((await call("browser_tabs", {}))[0]))}
                hid = her["id"]
                # J1: join the person's tab: it becomes joined (never 'mine'), and which tab is active doesn't change
                active_before = {i for i, t in (await tabmap()).items() if t["active"]}
                res, _ = await call("browser_join", {"tab": hid})
                m1 = await tabmap()
                active_after = {i for i, t in m1.items() if t["active"]}
                row("J1", (not res.isError) and m1[hid]["joined"] and not m1[hid]["mine"] and active_after == active_before,
                    f"join -> {text_of(res).strip()[:80]!r}; joined={m1[hid].get('joined')} mine={m1[hid]['mine']} "
                    f"active tabs unchanged={active_after == active_before}")
                # J2: I can look in a joined tab, but closing it is refused
                res, _ = await call("browser_look", {"tab": hid, "screenshot": False})
                looked = not res.isError and "The person's own page" in text_of(res)
                if not looked:
                    print("   J2 look:", text_of(res).strip()[:200])
                cres, _ = await call("browser_close", {"tab": hid})
                row("J2", looked and cres.isError and "never close" in text_of(cres) and hid in await tabmap(),
                    f"look ok={looked}; close -> {text_of(cres).strip()[:90]!r}")
                # J7: another session (its own HANDS_SESSION) can't use the yes given to this one: look and leave refused
                params7 = StdioServerParameters(command=sys.executable, args=["-X", "utf8", os.path.join(ROOT, "mcp_server.py")],
                                                env=dict(env, HANDS_SESSION="another-session-7",
                                                         HANDS_BOUNCER_DIR=tempfile.mkdtemp(prefix="hands-bt-bouncer7-")))
                async with stdio_client(params7) as (r7, w7):
                    async with ClientSession(r7, w7) as s7:
                        await s7.initialize()
                        o_look = await s7.call_tool("browser_look", {"tab": hid, "screenshot": False})
                        o_leave = await s7.call_tool("browser_leave", {"tab": hid})
                still = (await tabmap()).get(hid, {}).get("joined")
                row("J7", o_look.isError and o_leave.isError and "another session" in text_of(o_leave) and still,
                    f"other session look -> {text_of(o_look).strip()[:60]!r}; leave -> {text_of(o_leave).strip()[:70]!r}; still joined={still}")

                # J3: reclaim needs proof it was mine: the person's own tab and a forged id are refused; a handed-over
                #     tab can't be JOINED (that's reclaim)
                r_her, _ = await call("browser_reclaim", {"tab": hid})
                r_fake, _ = await call("browser_reclaim", {"tab": 2 ** 40})
                j_kept, _ = await call("browser_join", {"tab": ktab})
                row("J3", r_her.isError and r_fake.isError and j_kept.isError and "reclaim" in text_of(j_kept),
                    f"reclaim hers -> {text_of(r_her).strip()[:60]!r}; forged -> {text_of(r_fake).strip()[:40]!r}; "
                    f"join handed -> {text_of(j_kept).strip()[:60]!r}")
                # J4: reclaim the tab I handed over; then hands_done. The person's yes lasts until the tab closes: after
                #     done both tabs are still open, still joined / reclaimed, and a look works with no new card
                m0 = await tabmap()
                r_ok, _ = await call("browser_reclaim", {"tab": ktab})
                await call("hands_done", {})
                await asyncio.sleep(0.3)
                m3 = await tabmap()
                l_her, _ = await call("browser_look", {"tab": hid, "screenshot": False})
                l_k, _ = await call("browser_look", {"tab": ktab, "screenshot": False})
                row("J4", m0[ktab].get("reclaimable") and (not r_ok.isError)
                    and ktab in m3 and m3[ktab]["mine"] and hid in m3 and m3[hid].get("joined")
                    and not l_her.isError and not l_k.isError,
                    f"after done: joined still={m3.get(hid, {}).get('joined')}, reclaimed still mine={m3.get(ktab, {}).get('mine')}; "
                    f"looks with no new card: hers={not l_her.isError} reclaimed={not l_k.isError}")
                # J5: leave steps out: the joined tab stays open and is the person's again; the reclaimed one is handed
                #     back (reclaimable); coming back needs a new yes
                lv1, _ = await call("browser_leave", {"tab": hid})
                lv2, _ = await call("browser_leave", {"tab": ktab})
                m5 = await tabmap()
                after_look, _ = await call("browser_look", {"tab": hid, "screenshot": False})
                row("J5", (not lv1.isError) and (not lv2.isError) and hid in m5 and not m5[hid].get("joined")
                    and ktab in m5 and m5[ktab].get("reclaimable") and after_look.isError,
                    f"leave -> {text_of(lv1).strip()[:40]!r} / {text_of(lv2).strip()[:40]!r}; her tab open & hers again="
                    f"{hid in m5 and not m5[hid].get('joined')}; handed back reclaimable={m5.get(ktab, {}).get('reclaimable')}; "
                    f"look after leave refused={after_look.isError}")
                # J6: a reclaimed tab can be closed on purpose (full hands)
                await call("browser_reclaim", {"tab": ktab})
                c6, _ = await call("browser_close", {"tab": ktab})
                m6 = await tabmap()
                row("J6", (not c6.isError) and ktab not in m6, f"close reclaimed -> {text_of(c6).strip()[:60]!r}; gone={ktab not in m6}")
                await call("hands_done", {})

                # ---------------- S1-S3 (secrets in what I'm shown: hidden in the text, blacked out on the screenshot)
                res, _ = await call("browser_open", {"url": f"{A}/secrets.html", "session": "test"})
                stab = json.loads(text_of(res))["id"]
                opened_txt = text_of(res)
                await asyncio.sleep(0.8)
                res, _ = await call("browser_look", {"tab": stab, "screenshot": True})
                stxt = text_of(res)
                simgs = [c for c in res.content if c.type == "image"]
                tabs_txt = text_of((await call("browser_tabs", {}))[0])
                leaked = [s for s in FAKE_SECRETS if s in stxt or s in tabs_txt or s in opened_txt]
                row("S1", not res.isError and not leaked and stxt.count("[secret hidden]") >= 4 and "secrets: " in stxt
                    and "Plain words stay readable" in stxt,
                    f"leaked={leaked}; hidden markers={stxt.count('[secret hidden]')}; "
                    f"header={[l for l in stxt.splitlines() if l.startswith('secrets:')][:1]}")
                fill_rows = plain_fill = 0
                if simgs:
                    sim = PILImage.open(io.BytesIO(base64.b64decode(simgs[0].data))).convert("RGB")
                    W, H = sim.size
                    px = sim.load()

                    def fill(x, y):
                        r_, g_, b_ = px[x, y]
                        return abs(r_ - 58) < 22 and abs(g_ - 42) < 22 and abs(b_ - 51) < 22
                    band = lambda y: sum(fill(x, y) for x in range(0, W, 4)) / (W / 4)
                    # the four secret lines sit in the top 160 CSS px: find the scale from the image's own height
                    for y in range(0, H, 2):
                        share = band(y)
                        if share > 0.15:
                            if y < H * 0.45:
                                fill_rows += 1
                            else:
                                plain_fill += 1
                row("S2", bool(simgs) and fill_rows > 40 and plain_fill == 0,
                    f"screenshot={bool(simgs)}; blacked rows in the secret band={fill_rows}; blacked rows below it={plain_fill}")
                # S3: the same page with the secret list missing from the call is NOT what the engine sends: every look
                # carries it (the engine adds it itself), so a plain look of the person's page has no secrets line
                res, _ = await call("browser_look", {"tab": stab, "screenshot": False, "find": "Plain words"})
                row("S3", not res.isError and "Plain words stay readable" in text_of(res)
                    and not any(s in text_of(res) for s in FAKE_SECRETS),
                    f"find on the page -> {text_of(res).strip().splitlines()[-1][:60]!r}")
                await call("browser_close", {"tab": stab})
                await call("hands_done", {})
        probe.close()
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"ABORTED: {type(e).__name__}: {str(e)[:300]}")
    finally:
        STOP.set()
        gone = removed = None
        if ch:
            gone, removed = ch.shutdown()
        for srv in (srv_a, srv_b, srv_c):
            if srv:
                srv.shutdown()
                srv.server_close()
        shutil.rmtree(web, ignore_errors=True)
        key_restored = None
        if created_key:
            install_host.uninstall(test_host)
            shutil.rmtree(ext_copy, ignore_errors=True)
            real_manifest_after = install_host.MANIFEST.read_bytes() if install_host.MANIFEST.exists() else None
            key_restored = (install_host.registered() == real_before and real_manifest_after == real_manifest_before
                            and not install_host._paths(test_host)[0].exists())
        print(f"teardown: chrome gone={gone}, temp profile removed={removed}, web dir removed={not os.path.exists(web)}, "
              f"own host key removed and the real registration untouched: {key_restored}")
    order = ["K", "L", "M", "N", "O", "P", "Q", "R", "S", "T", "U", "V", "V2", "V3", "V4", "V5", "W1", "W2", "W3", "W4", "W5", "W6", "W7", "W8", "W9", "D1", "D2", "D3", "D4", "D5", "D6", "J", "J1", "J2", "J3", "J4", "J5", "J6", "J7", "S1", "S2", "S3", "E1", "E2", "E3", "E4", "E5"]
    print("\nSUMMARY " + " ".join(f"{k}:{'PASS' if RESULTS.get(k, (False,))[0] else 'FAIL'}" for k in order))
    return 0 if all(RESULTS.get(k, (False,))[0] for k in order) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
