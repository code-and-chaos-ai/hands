// Hands: the service worker. The native host is its only door; every request is typed JSON
// {id, method, params} and every answer is {id, result} or {id, error:{code, message}}.
// I drive ONLY tabs I opened (mine), through chrome.debugger, in the background, with focus emulation on.
// Policy (deny list, consent per site) is decided by the engine; here every frame I read or act in must carry
// the engine's approval for its EXACT current URL, re-checked against the live frame tree at the moment of use.
// Content scripts only DRAW (overlay.js); they automate nothing. Page functions run in my own isolated world.

const HOST = "com.kincaid.hands";
const TITLE_MARK = "\u{1FA77} ";       // pink heart before the title while I drive
const CDP_TIMEOUT = 8000;
const GLIDE_TIMEOUT = 900;
const GROUP_COLOR = "pink";
const MAX_FRAMES = 12;
const MAX_AX_NODES = 20000;
const DENY = /^(chrome|chrome-extension|chrome-untrusted|chrome-search|devtools|edge|about|view-source|javascript|data|blob|filesystem):/i;

let port = null;
let retryMs = 500;
let retryTimer = null;
// mine: {tabId: {session, keep, owner, root, adopted}}; owner = the token of the engine that opened it
const S = { mine: {}, groups: {}, policy: null, loaded: false };
const live = {};                                      // "<host pid>:<pipe client>" -> engine token, while connected
const attached = new Set();
const stopped = {};                                   // tabId -> why the debugger let go (held until a fresh look)
const worlds = new Map();                             // `${tabId}|${frameId}|${loaderId}` -> executionContextId
const sessions = {};                                  // tabId -> {sessionId|"root": "pending"|"ok"|"failed"}
const changes = {};                                   // tabId -> count of frame events (attach/detach/navigate)
const remote = {};                                    // tabId -> {frameId: {url, session, parent}}: frames in other processes
const mainFrame = {};                                 // tabId -> its main frame id (kept for reference only)
const childFrames = {};                               // tabId -> ids PROVEN to be subframes (a child id never becomes a main frame)
const blocked = {};                                   // tabId -> what my tab was kept from / left, not yet reported
const navLog = [];                                    // the same records, kept (last 50) for anyone checking afterwards
const refusedNet = {};                                // tabId -> URLs the Fetch gate refused (their error page is no news)
const leaving = {};                                   // tabId -> true while I'm taking it away from a denied page
const closedTabs = new Set();                         // recently closed tab ids ("closed" vs "no such tab")
const herActive = {};                                 // windowId -> the last active tab there that isn't mine
let herWindow = null;                                 // the last focused window that isn't a popup of mine
const herPrev = {};                                   // windowId -> the person's active tab before that
const trace = [];                                     // recent adoption decisions (read only through the test hook)
function note(x) { trace.push({ at: Date.now(), ...x }); if (trace.length > 40) trace.shift(); }
let openSeq = 0;                                      // tabs.open marks its own new tab: about:blank#rowan-open-N
let hostId = null;                                    // the native host's pid (from its hello)
let testMode = false;                                 // only the host can turn it on (test pipe + HANDS_TEST_HOOKS=1)
const fault = {};                                     // test-only faults, honoured only in testMode
const FRAME_EVENTS = new Set(["Page.frameAttached", "Page.frameDetached", "Page.frameNavigated", "Page.navigatedWithinDocument",
  "Target.attachedToTarget", "Target.detachedFromTarget", "Target.targetInfoChanged"]);

// ------------------------------------------------------------------ state
async function load() {
  if (S.loaded) return;
  const d = await chrome.storage.session.get(["mine", "groups", "stopped", "policy", "handed"]);
  S.mine = d.mine || {};
  S.handed = d.handed || {};   // tabs I opened and handed over (kept at done): the only ones I may reclaim
  S.groups = d.groups || {};
  S.policy = d.policy || null;
  Object.assign(stopped, d.stopped || {});
  S.loaded = true;
}
function save() { return chrome.storage.session.set({ mine: S.mine, groups: S.groups, stopped, policy: S.policy, handed: S.handed || {} }); }
// The person's tab: one Hands didn't open, or one it only JOINED (it stays theirs: their focus, never closed, never
// blanked, its popups never adopted).
function isHers(tabId) { const m = S.mine[String(tabId)]; return !m || m.mode === "joined"; }
function isJoined(tabId) { const m = S.mine[String(tabId)]; return !!m && m.mode === "joined"; }

// ------------------------------------------------------------------ native port
function connect() {
  if (port) return;
  retryTimer = null;
  try {
    port = chrome.runtime.connectNative(HOST);
  } catch (e) {
    port = null;
    schedule();
    return;
  }
  port.onMessage.addListener((msg) => { retryMs = 500; handle(msg); });
  port.onDisconnect.addListener(() => {
    void chrome.runtime.lastError;
    port = null;
    schedule();
  });
}
function schedule() {
  if (retryTimer) return;
  retryTimer = setTimeout(connect, retryMs);
  retryMs = Math.min(retryMs * 2, 10000);
}

function E(code, message) { const e = new Error(message); e.code = code; return e; }

function emit(ev) { try { port && port.postMessage(ev); } catch (e) { /* no host: nobody to tell */ } }

async function handle(msg) {
  if (msg && msg.hello && typeof msg.hello === "object") {
    testMode = msg.hello.test === true;
    hostId = String(msg.hello.host || "");
    for (const k of Object.keys(live)) delete live[k];   // a new host: every engine reconnects and says hello again
    return;
  }
  if (msg && Number.isInteger(msg.gone)) {          // an engine's pipe closed: its tabs wait for the next hello
    delete live[`${hostId}:${msg.gone}`];
    return;
  }
  if (!msg || typeof msg.id !== "number" || typeof msg.method !== "string") return;
  const fn = Object.prototype.hasOwnProperty.call(METHODS, msg.method) ? METHODS[msg.method] : null;
  let reply;
  try {
    if (!fn) throw E("unknown", `unknown method ${msg.method}`);
    const params = msg.params && typeof msg.params === "object" && !Array.isArray(msg.params) ? { ...msg.params } : {};
    params.__owner = `${hostId}:${Number.isInteger(msg.client) ? msg.client : "?"}`;   // set here, never by a client
    await load();
    reply = { id: msg.id, result: await fn(params) };
  } catch (e) {
    reply = { id: msg.id, error: { code: e.code || "error", message: String(e.message || e).slice(0, 600) } };
  }
  try { port && port.postMessage(reply); } catch (e) { /* port gone: the engine's call times out (bounded) */ }
}

// ------------------------------------------------------------------ helpers
function stripMark(t) { return (t || "").startsWith(TITLE_MARK) ? t.slice(TITLE_MARK.length) : (t || ""); }
function tabBrief(t) {
  const m = S.mine[String(t.id)];
  return { id: t.id, window: t.windowId, title: stripMark(t.title), url: t.url || t.pendingUrl || "",
           active: !!t.active, mine: !!m && m.mode !== "joined", joined: !!m && m.mode === "joined",
           reclaimable: !m && !!(S.handed || {})[String(t.id)], session: m ? m.session : null, keep: m ? !!m.keep : false,
           loading: t.status === "loading", group: t.groupId };
}
function int(v, what) {
  if (typeof v !== "number" || !Number.isSafeInteger(v)) throw E("bad", `${what} must be an integer`);
  return v;
}
async function getTab(tabId) {
  if (typeof tabId !== "number" || !Number.isSafeInteger(tabId) || tabId < 0) throw E("no_tab", `no such tab ${tabId}`);
  try { return await chrome.tabs.get(tabId); }
  catch (e) {
    if (closedTabs.has(tabId) || S.mine[String(tabId)]) throw E("gone", `browser use stopped: tab ${tabId} is closed`);
    throw E("no_tab", `no such tab ${tabId}`);
  }
}
// The engine's URL policy (engine/policy.py sends its lists); a top-level load in my tab must pass it.
function navAllowed(url) {
  const pol = S.policy;
  if (!pol) return false;                               // no policy yet: nothing loads in my tabs
  let u;
  try { u = new URL(url); } catch (e) { return false; }
  if (u.protocol !== "http:" && u.protocol !== "https:") return false;
  if (u.username || u.password) return false;
  const host = u.hostname.toLowerCase().replace(/\.+$/, "");   // every trailing dot: "vault.example.com.." too
  const is = (d) => host === d || host.endsWith("." + d);
  if ((pol.hosts || []).some(is)) return false;
  return true; // denial is by host only: a page can reach any path of its own origin, never fake a host
}
function mustBeMine(tabId) {
  if (!S.mine[String(tabId)]) throw E("not_mine", `tab ${tabId} is not one the agent opened; it never drives the person's tabs`);
}
function guardUrl(url) {
  const u = (url || "").toLowerCase();
  if (DENY.test(u) && u.split("#")[0] !== "about:blank") throw E("denied", "browser-internal page: I never drive it");
}

function cdp(tabId, method, params = {}, ms = CDP_TIMEOUT) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(E("timeout", `${method} took over ${ms} ms`)), ms);
    chrome.debugger.sendCommand({ tabId }, method, params, (r) => {
      clearTimeout(timer);
      const le = chrome.runtime.lastError;
      if (le) {
        if (stopped[tabId] || !attached.has(tabId)) reject(E("stopped", `browser use stopped: ${stopped[tabId] || "debugger detached"}`));
        else reject(E("cdp", `${method}: ${le.message}`));
      } else resolve(r || {});
    });
  });
}

// resume=true only from a fresh look of mine: that is how I come back after the person stopped it. Acts never resume.
async function ensureAttached(tabId, resume) {
  if (stopped[tabId]) {
    if (!resume) throw E("stopped", `browser use stopped: ${stopped[tabId]}. Look again (browser_look) only if the person wants the agent back.`);
    delete stopped[tabId];
    await save();
  }
  if (attached.has(tabId)) return;
  const tab = await getTab(tabId);
  guardUrl(tab.url || tab.pendingUrl);
  await new Promise((resolve, reject) => {
    chrome.debugger.attach({ tabId }, "1.3", () => {
      const le = chrome.runtime.lastError;
      if (le && !/already attached/i.test(le.message)) reject(E("attach", `could not attach: ${le.message}`));
      else resolve();
    });
  });
  attached.add(tabId);
  await cdp(tabId, "Emulation.setFocusEmulationEnabled", { enabled: true });
  await cdp(tabId, "DOM.enable").catch(() => {});
  await cdp(tabId, "Page.enable").catch(() => {});
  const ft0 = (await cdp(tabId, "Page.getFrameTree")).frameTree;
  mainFrame[tabId] = ft0.frame.id;
  childFrames[tabId] = new Set();
  (function walk(n) { for (const c of n.childFrames || []) { childFrames[tabId].add(c.frame.id); walk(c); } })(ft0);
  try {   // every top-level document load in my tab (redirect hops included) passes the URL gate first
    await cdp(tabId, "Fetch.enable", { patterns: [{ resourceType: "Document", requestStage: "Request" }] });
  } catch (e) {
    if (e.code === "stopped") throw e;
    await new Promise((r) => chrome.debugger.detach({ tabId }, () => { void chrome.runtime.lastError; r(); }));
    attached.delete(tabId);
    throw E("attach", "couldn't arm the navigation gate on that tab; not driving it");
  }
  // frames in other processes (cross-site iframes) are invisible to Page.getFrameTree: learn them from the browser
  remote[tabId] = {};
  sessions[tabId] = { root: "pending" };
  try {
    await cdp(tabId, "Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });
    sessions[tabId].root = "ok";
  } catch (e) {
    if (e.code === "stopped") throw e;
    sessions[tabId].root = "failed";   // looks will withhold screenshots: cross-site frames could be unknown
  }
}
function sendS(tabId, sessionId, method, params) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(E("timeout", `${method} took over ${CDP_TIMEOUT} ms`)), CDP_TIMEOUT);
    chrome.debugger.sendCommand({ tabId, sessionId }, method, params || {}, (r) => {
      clearTimeout(timer);
      const le = chrome.runtime.lastError;
      if (le) reject(E("cdp", `${method}: ${le.message}`)); else resolve(r || {});
    });
  });
}
chrome.debugger.onEvent.addListener((src, method, params) => {
  if (src.tabId == null || !params) return;
  if (method === "Fetch.requestPaused") return gateRequest(src, params);
  if (method === "Page.frameAttached" && !src.sessionId && params.parentFrameId && params.frameId)
    (childFrames[src.tabId] || (childFrames[src.tabId] = new Set())).add(params.frameId);
  if (method === "Page.frameNavigated" && !src.sessionId && params.frame && !params.frame.parentId) {
    mainFrame[src.tabId] = params.frame.id;
    commitOwner(src.tabId, params.frame.url, params.type === "BackForwardCacheRestore" ? "the back/forward cache" : "a navigation");
  }
  if (!remote[src.tabId]) return;
  if (FRAME_EVENTS.has(method)) changes[src.tabId] = (changes[src.tabId] || 0) + 1;
  const m = remote[src.tabId], ti = params.targetInfo, tab = src.tabId;
  if (method === "Target.attachedToTarget" && ti && ti.type === "iframe") {
    const sid = params.sessionId;
    m[ti.targetId] = { url: ti.url, session: sid, parent: ti.parentFrameId || null };
    // the frames inside it must be known too: its own frame events, and auto-attach for ITS cross-site children.
    // Until both are acknowledged, looks withhold screenshots.
    (sessions[tab] || (sessions[tab] = {}))[sid] = "pending";
    (async () => {
      try {
        if (testMode && fault.autoAttachFail) throw E("cdp", "simulated auto-attach failure (test hook)");
        await sendS(tab, sid, "Page.enable");
        await sendS(tab, sid, "Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });
        if (sessions[tab] && sessions[tab][sid] === "pending") sessions[tab][sid] = "ok";
      } catch (e) {
        if (sessions[tab] && sid in sessions[tab]) sessions[tab][sid] = "failed";
      }
    })();
  }
  else if (method === "Target.targetInfoChanged" && ti && m[ti.targetId]) m[ti.targetId].url = ti.url;
  else if (method === "Target.detachedFromTarget") {
    for (const [k, v] of Object.entries(m)) if (v.session === params.sessionId) delete m[k];
    if (sessions[tab]) delete sessions[tab][params.sessionId];
  }
});

function gateRequest(src, params) {
  const tab = src.tabId, reply = (method, extra) => chrome.debugger.sendCommand(
    src.sessionId ? { tabId: tab, sessionId: src.sessionId } : { tabId: tab }, method,
    { requestId: params.requestId, ...extra }, () => { void chrome.runtime.lastError; });
  // top-level unless the request's frame is PROVEN a subframe (seen attached under a parent); anything else,
  // unknown frames and prerendered pages included, is gated as top-level. Nothing is asked of the page while it waits.
  const top = !(params.frameId && childFrames[tab] && childFrames[tab].has(params.frameId));
  if (params.resourceType === "Document" && top && !navAllowed(params.request && params.request.url)) {
    reply("Fetch.failRequest", { errorReason: "BlockedByClient" });
    const u = String((params.request && params.request.url) || "").split("#")[0];
    (refusedNet[tab] || (refusedNet[tab] = new Set())).add(u);
    record(tab, `tried to load ${hostOf(u)} (refused before any byte arrived)`);
    return;
  }
  reply("Fetch.continueRequest", {});
}

chrome.debugger.onDetach.addListener(async (src, reason) => {
  if (src.tabId == null) return;
  await load();
  attached.delete(src.tabId);
  for (const k of [...worlds.keys()]) if (k.startsWith(`${src.tabId}|`)) worlds.delete(k);
  delete remote[src.tabId];
  delete sessions[src.tabId];
  if (!S.mine[String(src.tabId)]) return;
  stopped[src.tabId] = reason === "canceled_by_user" ? "the person cancelled the debugging bar" :
                       reason === "target_closed" ? "the tab closed" : `debugger detached (${reason})`;
  await save();
  if (reason === "canceled_by_user") emit({ event: "stopped", reason: stopped[src.tabId] });   // stops every surface
  overlay(src.tabId, { hands: "bye" }).catch(() => {});
});
chrome.tabs.onRemoved.addListener(async (tabId) => {
  await load();
  closedTabs.add(tabId);
  if (closedTabs.size > 2000) closedTabs.delete(closedTabs.values().next().value);
  attached.delete(tabId);
  let changed = false;
  if (S.handed && S.handed[String(tabId)]) { delete S.handed[String(tabId)]; changed = true; }
  if (S.mine[String(tabId)]) { delete S.mine[String(tabId)]; delete stopped[tabId]; changed = true; }
  if (changed) save();
});

// ------------------------------------------------------------------ what my tabs open is mine; the person's focus stays theirs
chrome.tabs.onActivated.addListener(({ tabId, windowId }) => {
  if (S.loaded && isHers(tabId) && herActive[windowId] !== tabId) {
    herPrev[windowId] = herActive[windowId];
    herActive[windowId] = tabId;
  }
});
chrome.windows.onFocusChanged.addListener(async (windowId) => {
  if (windowId === chrome.windows.WINDOW_ID_NONE) return;
  await new Promise((r) => setTimeout(r, 250));   // a popup my tab just opened is adopted first, so it never counts as the person's
  try {
    const w = await chrome.windows.get(windowId, { populate: true });
    if (!(w.tabs || []).every((t) => !isHers(t.id))) herWindow = windowId;
  } catch (e) { /* gone */ }
});
async function seedHerFocus() {
  try {
    for (const t of await chrome.tabs.query({ active: true })) if (isHers(t.id)) herActive[t.windowId] = t.id;
    const w = await chrome.windows.getLastFocused();
    if (w && w.id != null) herWindow = w.id;
  } catch (e) { /* no windows yet */ }
}
// ------------------------------------------------------------------ ONE owner: what my tab has committed to
function record(tabId, text) {
  (blocked[tabId] || (blocked[tabId] = [])).push(text);
  const root = S.mine[String(tabId)] && S.mine[String(tabId)].root;
  if (root != null && root !== tabId) (blocked[root] || (blocked[root] = [])).push(text);
  navLog.push({ tab: tabId, text, at: Date.now() });
  if (navLog.length > 50) navLog.shift();
}
// Called on EVERY main-frame commit of a tab of mine, whatever served it (network, service worker, back/forward
// cache, prerender). A page I may not touch: debugger off at once, the tab leaves it (adopted: closed), and I'm
// stopped on that tab until a fresh look.
async function commitOwner(tabId, url, how, again = false) {
  await load();
  const m = S.mine[String(tabId)];
  if (!m) return;
  const denied = (x) => typeof x === "string" && x && !x.startsWith("about:blank") && !navAllowed(x.split("#")[0]);
  let cur;
  try { cur = await chrome.tabs.get(tabId); } catch (e) { return; }
  const now = (cur.url || "").split("#")[0];
  if ((cur.pendingUrl || "").startsWith("about:blank")) return;   // already on its way out (my leave in flight)
  if (!denied(now)) {                           // decided by where the tab IS, not by which event said what
    if (denied(url) && !again) setTimeout(() => commitOwner(tabId, url, how, true), 300);
    return;
  }
  if (leaving[tabId]) return;                   // checked and set with no await between: one leave per landing
  leaving[tabId] = true;
  try {
    const host = hostOf(now);
    // an error page (left after the Fetch gate refused a load) holds nothing of the denied site: leave it quietly
    const quiet = now.startsWith("chrome-error:") || (refusedNet[tabId] && refusedNet[tabId].has(now));
    // what served it, from what I know (not from which event spoke first): no request of mine passed my gate
    const via = !attached.has(tabId) ? (m.adopted ? "a load that began before I could arm the tab" : "a load while my debugger was off")
              : m.adopted ? "no request through my gate: a load begun before I armed this adopted tab, or a service worker/cache"
              : "no request through my gate (service worker, back/forward cache or prerender)";
    if (!quiet) record(tabId, `landed on ${host} (served by ${via}); I left it`);
    stopped[tabId] = `my tab ended up on ${host}, a page I never touch; I left it`;
    if (m.mode === "joined") {
      // the person's own tab went somewhere I may not be: I step out of it; their tab is never moved or closed
      stopped[tabId] = `the person's tab went to ${host}, a page I never touch; I stepped out of it`;
      delete S.mine[String(tabId)];
    }
    await save();
    if (attached.has(tabId)) {
      attached.delete(tabId);
      await new Promise((r) => chrome.debugger.detach({ tabId }, () => { void chrome.runtime.lastError; r(); }));
    }
    if (m.mode === "joined") return;
    if (m.adopted) await chrome.tabs.remove(tabId);
    else await chrome.tabs.update(tabId, { url: "about:blank" });
  } catch (e) { /* already gone */ }
  finally { delete leaving[tabId]; }
}
chrome.webNavigation.onCommitted.addListener((d) => {
  if (d.frameId !== 0) return;
  const q = d.transitionQualifiers || [];
  commitOwner(d.tabId, d.url, q.includes("forward_back") ? "back/forward" : "a commit with no network request of mine seen (service worker, cache or prerender)");
});
chrome.tabs.onUpdated.addListener((tabId, info) => { if (info.url) commitOwner(tabId, info.url, "a page change"); });

// What my tab opens is mine, by proof only (never by URL or timing):
//  - Chrome's openerTabId is one of my tabs, or
//  - webNavigation says the new tab was created to host a navigation whose SOURCE is one of my tabs
//    (window.open, target=_blank, middle-click, Enter on a link). A tab the person's page opens names THEIR tab: never mine.
chrome.tabs.onCreated.addListener(async (t) => {
  await load();
  if (/^about:blank#rowan-open-/.test(t.pendingUrl || t.url || "")) return;   // my own tabs.open
  note({ created: t.id, opener: t.openerTabId });
  if (t.openerTabId != null && !isHers(t.openerTabId)) await adopt(t.id, t.openerTabId);
});
chrome.webNavigation.onCreatedNavigationTarget.addListener(async (d) => {
  await load();
  note({ navTarget: d.tabId, source: d.sourceTabId });
  if (!isHers(d.sourceTabId)) await adopt(d.tabId, d.sourceTabId);   // a joined tab is theirs: what it opens is theirs
});
async function adopt(tabId, fromId) {
  if (S.mine[String(tabId)]) return;   // already mine (both proofs can name the same tab)
  const src = S.mine[String(fromId)];
  let t;
  try { t = await chrome.tabs.get(tabId); } catch (e) { return; }
  S.mine[String(tabId)] = { session: src.session, keep: false, owner: src.owner, root: src.root != null ? src.root : fromId, adopted: true };
  await save();
  note({ adopted: tabId, from: fromId });
  ensureAttached(tabId, false).catch(() => {});   // Fetch gate + events before its load continues, where it still can
  commitOwner(tabId, t.url || t.pendingUrl || "", "a load that began before I could arm the tab");
  try {
    const w = await chrome.windows.get(t.windowId);
    if (w.type === "normal") {
      const her = herActive[t.windowId] === t.id ? herPrev[t.windowId] : herActive[t.windowId];
      if (her != null && her !== t.id) {
        try { await chrome.tabs.update(her, { active: true }); } catch (e) { /* the person's tab closed meanwhile */ }
      }
      let gid = await groupFor(src.session, t.windowId);
      if (gid != null) await chrome.tabs.group({ tabIds: [t.id], groupId: gid });
      else {
        gid = await chrome.tabs.group({ tabIds: [t.id], createProperties: { windowId: t.windowId } });
        S.groups[src.session] = gid;
        await chrome.tabGroups.update(gid, { title: `\u{1F43A} ${src.session}`, color: GROUP_COLOR, collapsed: false });
        await save();
      }
    } else if (herWindow != null && herWindow !== w.id) {
      await chrome.windows.update(herWindow, { focused: true });   // a popup of mine took focus: give it back
    }
    await chrome.tabs.update(t.id, { muted: true });
  } catch (e) { /* the tab went away while I adopted it */ }
}

// ------------------------------------------------------------------ overlay (draws only)
async function overlay(tabId, msg) {
  const send = () => new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(E("overlay", "overlay did not answer")), GLIDE_TIMEOUT);
    chrome.tabs.sendMessage(tabId, msg, { frameId: 0 }, (r) => {
      clearTimeout(timer);
      if (chrome.runtime.lastError) reject(E("overlay", chrome.runtime.lastError.message)); else resolve(r);
    });
  });
  const work = (async () => {
    try { return await send(); }
    catch (e) {
      if (msg.hands === "bye") return null;
      await chrome.scripting.executeScript({ target: { tabId, frameIds: [0] }, files: ["overlay.js"], injectImmediately: true });
      return await send();
    }
  })();
  // drawing never holds the work: a page that never finishes loading can't stall me here
  return await Promise.race([work, new Promise((_, rej) => setTimeout(() => rej(E("overlay", "overlay too slow")), 2 * GLIDE_TIMEOUT))]);
}
async function glide(tabId, x, y, kind) {
  try { await overlay(tabId, { hands: "glide", x, y, kind }); } catch (e) { /* drawing never blocks the work */ }
}

// ------------------------------------------------------------------ frames: the live tree, checked against approvals
async function frameTree(tabId) {
  const ft = await cdp(tabId, "Page.getFrameTree");
  const list = [];
  (function walk(n, parent) {
    list.push({ id: n.frame.id, url: n.frame.url, parent, loaderId: n.frame.loaderId });
    (n.childFrames || []).forEach((c) => walk(c, n.frame.id));
  })(ft.frameTree, null);
  const byId = Object.fromEntries(list.map((f) => [f.id, f]));
  let incomplete = false;
  for (const [id, r] of Object.entries(remote[tabId] || {})) {
    if (byId[id]) continue;
    const f = { id, url: r.url, parent: r.parent && byId[r.parent] ? r.parent : ft.frameTree.frame.id, loaderId: null, remote: true };
    list.push(f);
    byId[id] = f;
    // frames inside that frame's own process: known only through its session
    try {
      const sub = await sendS(tabId, r.session, "Page.getFrameTree");
      (function walk(n, parent) {
        for (const c of n.childFrames || []) {
          if (!byId[c.frame.id]) {
            const g = { id: c.frame.id, url: c.frame.url, parent, loaderId: c.frame.loaderId, remote: true };
            list.push(g);
            byId[g.id] = g;
          }
          walk(c, c.frame.id);
        }
      })(sub.frameTree, id);
    } catch (e) { incomplete = true; }
  }
  return { main: ft.frameTree.frame, list, byId, incomplete };
}
function approvedFrame(ft, approved, frameId) {
  const f = ft.byId[frameId];
  if (!f) throw E("stale", "that element's frame is gone (the page changed); look again");
  if (!approved || approved[frameId] !== f.url)
    throw E("frame_denied", `that frame (${hostOf(f.url)}) is not one I may touch now (denied, no consent, or it navigated since my look)`);
  return f;
}
function hostOf(u) {   // never echo a denied frame's URL beyond its host (or just its scheme)
  try { const x = new URL(u); return /^https?:$/.test(x.protocol) ? x.host : x.protocol; } catch (e) { return "unknown"; }
}

async function world(tabId, f) {
  const k = `${tabId}|${f.id}|${f.loaderId}`;
  if (worlds.has(k)) return worlds.get(k);
  const r = await cdp(tabId, "Page.createIsolatedWorld", { frameId: f.id, worldName: "rowan-hands", grantUniveralAccess: false });
  worlds.set(k, r.executionContextId);
  return r.executionContextId;
}
// The node, resolved into MY isolated world of the frame it must belong to; refused if it lives anywhere else.
async function nodeIn(tabId, f, backendNodeId) {
  const ctx = await world(tabId, f);
  let obj;
  try { obj = await cdp(tabId, "DOM.resolveNode", { backendNodeId: int(backendNodeId, "node"), executionContextId: ctx }); }
  catch (e) {
    if (e.code === "stopped") throw e;
    worlds.delete(`${tabId}|${f.id}|${f.loaderId}`);
    throw E("stale", "that element is gone; look again");
  }
  const id = obj.object.objectId;
  const own = await callObj(tabId, id, "function() { return this.ownerDocument === document; }");
  if (own !== true) throw E("frame_denied", "that element does not belong to the frame my look saw it in; look again");
  return id;
}
async function callObj(tabId, objectId, fn, args = []) {
  const r = await cdp(tabId, "Runtime.callFunctionOn", {
    objectId, functionDeclaration: fn, returnByValue: true, awaitPromise: true,
    arguments: args.map((a) => (a && typeof a === "object" && a.objectId ? { objectId: a.objectId } : { value: a })) });
  if (r.exceptionDetails) throw E("page", (r.exceptionDetails.exception && r.exceptionDetails.exception.description) || r.exceptionDetails.text);
  return r.result ? r.result.value : undefined;
}
// Which frame holds focus right now (walked from the top in my own world; cross-origin frames stop the walk at their owner).
// ------------------------------------------------------------------ screenshots never show a frame I may not read
// Every frame without my approval (denied, no consent) whose ancestors I may read gets its owner element's box
// painted over, here in the worker: the unredacted bytes never leave it. A box I can't place = no screenshot.
function framesToCover(ft, approved) {
  return ft.list.filter((f) => {
    if (!f.parent || approved[f.id] === f.url) return false;
    for (let p = ft.byId[f.parent]; p && p.parent; p = ft.byId[p.parent])
      if (approved[p.id] !== p.url) return false;   // an ancestor is covered already: so is this
    return true;
  });
}
async function frameBoxes(tabId, ft, frames) {
  const rects = [];
  let notRendered = 0;
  for (const f of frames) {
    let owner;
    try { owner = (await cdp(tabId, "DOM.getFrameOwner", { frameId: f.id })).backendNodeId; }
    catch (e) { if (e.code === "stopped") throw e; throw E("cover", `can't find the element holding a frame from ${hostOf(f.url)}`); }
    let quads = null, qerr = null;
    try {
      if (testMode && fault.boxError) throw E("timeout", "DOM.getContentQuads took over 8000 ms (simulated, test hook)");
      ({ quads } = await cdp(tabId, "DOM.getContentQuads", { backendNodeId: owner }));
    } catch (e) { if (e.code === "stopped") throw e; qerr = e; }
    if (!quads || !quads.length) {
      // skip ONLY the no-layout case, and only once the owner is proven not rendered (no client rects), read in
      // my isolated world of the frame that holds it. Every other failure: no screenshot.
      const noLayout = qerr ? (qerr.code === "cdp" && /could not compute content quads/i.test(qerr.message)) : Array.isArray(quads);
      if (!noLayout) throw E("cover", `the box of the frame from ${hostOf(f.url)} can't be read (${qerr ? qerr.message : "no quads"})`);
      const holder = ft.byId[f.parent];
      if (!holder || holder.remote) throw E("cover", `can't check whether the frame from ${hostOf(f.url)} is rendered`);
      let r;
      try {
        const obj = await nodeIn(tabId, holder, owner);
        r = await callObj(tabId, obj, "function() { return { n: this.getClientRects().length, d: getComputedStyle(this).display }; }");
      } catch (e) {
        if (e.code === "stopped") throw e;
        throw E("cover", `can't check whether the frame from ${hostOf(f.url)} is rendered`);
      }
      if (!r || r.n !== 0) throw E("cover", `the frame from ${hostOf(f.url)} has no box yet seems rendered`);
      notRendered++;
      continue;
    }
    for (const q of quads) {
      const xs = [q[0], q[2], q[4], q[6]], ys = [q[1], q[3], q[5], q[7]];
      rects.push({ x0: Math.min(...xs), y0: Math.min(...ys), x1: Math.max(...xs), y1: Math.max(...ys) });
    }
  }
  return { rects, notRendered };
}
// every session that could announce frames has acknowledged auto-attach (waits briefly for the pending ones)
async function attachAcked(tabId) {
  const t0 = Date.now();
  for (;;) {
    const st = Object.values(sessions[tabId] || { root: "failed" });
    if (st.includes("failed")) return false;
    if (!st.includes("pending")) return true;
    if (Date.now() - t0 > 800) return false;
    await new Promise((r) => setTimeout(r, 40));
  }
}
function frameKey(ft) { return ft.list.map((f) => `${f.id} ${f.url}`).sort().join("\n"); }
async function shotScale(tabId, ft) {
  const m = await cdp(tabId, "Page.getLayoutMetrics");
  const vv = m.cssVisualViewport || {};
  if ((vv.scale && Math.abs(vv.scale - 1) > 1e-3) || vv.offsetX || vv.offsetY)
    throw E("cover", "the page is pinch-zoomed, so I can't place frames exactly");
  const ctx = await world(tabId, ft.byId[ft.main.id]);
  const r = await cdp(tabId, "Runtime.evaluate", { contextId: ctx, expression: "window.devicePixelRatio", returnByValue: true });
  const dpr = r.result && typeof r.result.value === "number" ? r.result.value : 0;
  if (!(dpr > 0 && dpr < 10)) throw E("cover", "no device pixel ratio");
  return { dpr, w: vv.clientWidth, h: vv.clientHeight };
}
function b64ToBytes(b64) { const s = atob(b64); const u = new Uint8Array(s.length); for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i); return u; }
function bytesToB64(u) { let s = ""; for (let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode.apply(null, u.subarray(i, i + 0x8000)); return btoa(s); }
async function redact(b64, rects, sc) {
  const bmp = await createImageBitmap(new Blob([b64ToBytes(b64)], { type: "image/jpeg" }));
  // the capture must be the viewport at the device pixel ratio (a scrollbar may add up to ~40 px)
  if (bmp.width < sc.w * sc.dpr - 2 || bmp.width > (sc.w + 40) * sc.dpr + 2 || bmp.height < sc.h * sc.dpr - 2 ||
      bmp.height > (sc.h + 40) * sc.dpr + 2) {
    bmp.close();
    throw E("cover", `the screenshot isn't the size I expect (${bmp.width}x${bmp.height} for ${sc.w}x${sc.h}@${sc.dpr})`);
  }
  const c = new OffscreenCanvas(bmp.width, bmp.height);
  const g = c.getContext("2d");
  g.drawImage(bmp, 0, 0);
  bmp.close();
  g.fillStyle = "#3a2a33";
  for (const r of rects) {
    const x = Math.floor(r.x0 * sc.dpr) - 2, y = Math.floor(r.y0 * sc.dpr) - 2;
    g.fillRect(x, y, Math.ceil(r.x1 * sc.dpr) + 2 - x, Math.ceil(r.y1 * sc.dpr) + 2 - y);
  }
  const blob = await c.convertToBlob({ type: "image/jpeg", quality: 0.7 });
  return bytesToB64(new Uint8Array(await blob.arrayBuffer()));
}

// Which frame will receive typed text: walk activeElement down through shadow roots (open AND closed: the DOM agent
// sees both) and frame owners. A frame I can't look into (another process, unknown) is where the text goes.
async function focusedFrame(tabId, ft) {
  let frame = ft.main.id;
  let r = await cdp(tabId, "Runtime.evaluate", { expression: "document.activeElement", contextId: await world(tabId, ft.byId[frame]), returnByValue: false });
  for (let i = 0; i < 64; i++) {
    const oid = r.result && r.result.objectId;
    if (!oid) return frame;
    const n = (await cdp(tabId, "DOM.describeNode", { objectId: oid, depth: 1, pierce: true })).node || {};
    if (n.frameId) {
      const child = ft.byId[n.frameId];
      if (!child || child.remote) return n.frameId;
      frame = n.frameId;
      r = await cdp(tabId, "Runtime.evaluate", { expression: "document.activeElement", contextId: await world(tabId, child), returnByValue: false });
      continue;
    }
    const sr = (n.shadowRoots || [])[0];
    if (!sr) return frame;
    const so = await cdp(tabId, "DOM.resolveNode", { backendNodeId: sr.backendNodeId, executionContextId: await world(tabId, ft.byId[frame]) });
    r = await cdp(tabId, "Runtime.callFunctionOn", { objectId: so.object.objectId, returnByValue: false,
                                                     functionDeclaration: "function() { return this.activeElement; }" });
  }
  throw E("frame_denied", "can't tell where focus is; not typing blind");
}

// ------------------------------------------------------------------ tab groups
async function groupFor(session, windowId) {
  const gid = S.groups[session];
  if (gid != null) {
    try { const g = await chrome.tabGroups.get(gid); if (g.windowId === windowId) return gid; } catch (e) { /* gone */ }
  }
  return null;
}

// ------------------------------------------------------------------ geometry + input
async function viewport(tabId) {
  const m = await cdp(tabId, "Page.getLayoutMetrics");
  const v = m.cssLayoutViewport || m.layoutViewport;
  return { w: v.clientWidth, h: v.clientHeight };
}
async function center(tabId, backendNodeId) {
  await cdp(tabId, "DOM.scrollIntoViewIfNeeded", { backendNodeId }).catch(() => {});
  let quads;
  try { ({ quads } = await cdp(tabId, "DOM.getContentQuads", { backendNodeId })); }
  catch (e) { if (e.code === "stopped") throw e; throw E("no_box", "that element is gone or has no box; look again"); }
  if (!quads || !quads.length) throw E("no_box", "that element is not visible (no box); look again");
  const q = quads[0];
  const x = (q[0] + q[2] + q[4] + q[6]) / 4, y = (q[1] + q[3] + q[5] + q[7]) / 4;
  const v = await viewport(tabId);
  if (!(x >= 0 && y >= 0 && x < v.w && y < v.h)) throw E("no_box", "that element is outside the viewport even after scrolling");
  return { x, y };
}
// What would really receive a click at (x,y) must be the target (or inside it / around it) in an approved frame.
async function hitCheck(tabId, ft, approved, f, targetObj, x, y) {
  let hit;
  try { hit = await cdp(tabId, "DOM.getNodeForLocation", { x: Math.round(x), y: Math.round(y), includeUserAgentShadowDOM: false }); }
  catch (e) { if (e.code === "stopped") throw e; throw E("covered", "nothing hittable at that element's center; look again"); }
  await notIntoFrame(tabId, hit.backendNodeId);
  if (hit.frameId && hit.frameId !== f.id) {
    approvedFrame(ft, approved, hit.frameId);
    throw E("covered", "another frame covers that element; I won't click into something I didn't mean to");
  }
  const hobj = await nodeIn(tabId, f, hit.backendNodeId);
  const ok = await callObj(tabId, targetObj, `function(h) {
    const up = (n) => n.parentNode || (n instanceof ShadowRoot ? n.host : null);
    for (let n = h; n; n = up(n)) if (n === this) return true;
    for (let n = this; n; n = up(n)) if (n === h) return true;
    return false; }`, [{ objectId: hobj }]);
  if (!ok) throw E("covered", "something else covers that element at its center; look again before clicking");
}
// A hit that stops at a frame's owner element (<iframe>, <frame>, <object>, <embed>) means the input would be routed
// INTO a frame whose content I can't see from here (another process): never, whatever the frame is.
async function notIntoFrame(tabId, backendNodeId) {
  let d;
  try { d = await cdp(tabId, "DOM.describeNode", { backendNodeId }); }
  catch (e) { if (e.code === "stopped") throw e; throw E("covered", "can't tell what is at that point; look again"); }
  const n = d.node || {};
  if (n.frameId || /^(IFRAME|FRAME|OBJECT|EMBED|PORTAL|FENCEDFRAME)$/i.test(n.nodeName || ""))
    throw E("frame_denied", "that point is inside a frame I can't see into (another process); I don't send input there");
}
// Put focus in the field so typed text lands THERE. If another frame keeps focus (a page can hold it in a frame of
// another process), click the field like a person would (hit-tested); then the focused frame must be the field's own.
async function focusInto(tabId, ft, approved, f, obj, b, draw) {
  const p = await center(tabId, b);
  if (draw) await glide(tabId, p.x, p.y, "type");
  await cdp(tabId, "DOM.focus", { backendNodeId: b }).catch((e) => { if (e.code === "stopped") throw e; });
  let has = await callObj(tabId, obj, FOCUS_END);
  let ff = await focusedFrame(tabId, ft);
  if (has && ff !== f.id) {
    // another frame holds focus and Chrome won't let a frame steal it: take it back to the top document first
    const ctx = await world(tabId, ft.byId[ft.main.id]);
    await cdp(tabId, "Runtime.evaluate", { contextId: ctx, returnByValue: true,
      expression: "(() => { const a = document.activeElement; if (a && a !== document.body) a.blur(); return true; })()" });
    await cdp(tabId, "DOM.focus", { backendNodeId: b }).catch((e) => { if (e.code === "stopped") throw e; });
    has = await callObj(tabId, obj, FOCUS_END);
    ff = await focusedFrame(tabId, ft);
  }
  if (!has || ff !== f.id) {
    await hitCheck(tabId, ft, approved, f, obj, p.x, p.y);
    await mouseClick(tabId, p.x, p.y);
    has = await callObj(tabId, obj, FOCUS_END);
    ff = await focusedFrame(tabId, ft);
  }
  if (!has) throw E("page", "that element won't take focus; it can't be typed into");
  if (ff !== f.id) {
    const holder = ft.byId[ff];
    throw E("frame_denied", `focus won't move into that field (${holder ? hostOf(holder.url) : "another frame"} holds it); not typing`);
  }
}
// The page sees mouseMoved before the press and may move something under the point in its handler: the hit is
// checked again after the move, and the press only happens if it still lands on the target.
// The element that receives the press (a text node's parent) and the backend ids under it, frames excluded.
async function pressTarget(tabId, obj) {
  const r = await cdp(tabId, "Runtime.callFunctionOn", { objectId: obj, returnByValue: false,
    functionDeclaration: "function() { return this.nodeType === 1 ? this : this.parentElement; }" });
  if (!r.result || !r.result.objectId) throw E("no_box", "that element can't take a press; look again");
  const d = await cdp(tabId, "DOM.describeNode", { objectId: r.result.objectId, depth: -1, pierce: true });
  const ids = new Set();
  (function walk(n) {
    if (!n || n.frameId || /^(IFRAME|FRAME|OBJECT|EMBED|PORTAL|FENCEDFRAME)$/i.test(n.nodeName || "")) return;
    ids.add(n.backendNodeId);
    for (const c of n.children || []) walk(c);
    for (const c of n.shadowRoots || []) walk(c);
  })(d.node);
  return { anchor: r.result.objectId, ids };
}
async function guardedClick(tabId, ft, approved, f, obj, x, y, button, count) {
  const token = `c${Date.now()}${Math.random().toString(36).slice(2)}`;
  const { anchor, ids } = await pressTarget(tabId, obj);
  obj = anchor;
  // only presses the browser itself delivers count: a page's own dispatchEvent is never mine
  await callObj(tabId, obj, `function(t) { const box = (window.__rowanClicks = window.__rowanClicks || {}); box[t] = 0;
    const h = (e) => { if (e.isTrusted) box[t]++; }; (window.__rowanH = window.__rowanH || {})[t] = h;
    this.addEventListener("mousedown", h, true); this.addEventListener("mouseup", h, true); return true; }`, [token]);
  try {
    await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
    // the last check, one round trip before the press: still my element, in my frame, under the point
    let hit;
    try { hit = await cdp(tabId, "DOM.getNodeForLocation", { x: Math.round(x), y: Math.round(y), includeUserAgentShadowDOM: false }); }
    catch (e) { if (e.code === "stopped") throw e; hit = {}; }
    if (hit.frameId !== f.id || !ids.has(hit.backendNodeId))
      throw E("covered", "the page moved something under my click; nothing pressed");
    for (let i = 1; i <= count; i++) {
      await cdp(tabId, "Input.dispatchMouseEvent", { type: "mousePressed", x, y, button, clickCount: i });
      await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button, clickCount: i });
    }
  } finally {
    var got = await callObj(tabId, obj, `function(t) { const n = (window.__rowanClicks || {})[t] || 0; const h = (window.__rowanH || {})[t];
      if (h) { this.removeEventListener("mousedown", h, true); this.removeEventListener("mouseup", h, true); }
      delete (window.__rowanClicks || {})[t]; delete (window.__rowanH || {})[t]; return n; }`, [token]).catch(() => -1);
  }
  if (got < 0) return null;   // the document went away right after (the click navigated): can't confirm
  return got >= 2 * count;   // press and release both reached the element I meant
}
async function mouseClick(tabId, x, y, button = "left", count = 1) {
  await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
  for (let i = 1; i <= count; i++) {
    await cdp(tabId, "Input.dispatchMouseEvent", { type: "mousePressed", x, y, button, clickCount: i });
    await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button, clickCount: i });
  }
}

const SET_VALUE = `function(v) {
  if (this.isContentEditable) { this.focus(); this.textContent = v;
    this.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "insertReplacementText", data: v})); return this.textContent; }
  if (!(this instanceof HTMLInputElement || this instanceof HTMLTextAreaElement)) throw new Error("not a text field (use select for <select>)");
  if (this.readOnly || this.disabled) throw new Error("field is read-only or disabled");
  const proto = this instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  this.focus(); Object.getOwnPropertyDescriptor(proto, "value").set.call(this, v);
  this.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "insertReplacementText", data: v}));
  this.dispatchEvent(new Event("change", {bubbles: true}));
  return this.value;
}`;
const SELECT = `function(v) {
  if (!(this instanceof HTMLSelectElement)) throw new Error("not a <select>");
  if (this.disabled) throw new Error("select is disabled");
  const o = [...this.options].find(o => o.value === v) || [...this.options].find(o => o.label.trim() === v || o.text.trim() === v);
  if (!o) throw new Error("no option " + JSON.stringify(v) + "; options: " + [...this.options].map(o => o.text.trim()).slice(0, 20).join(" | "));
  if (o.disabled) throw new Error("that option is disabled");
  this.focus(); this.value = o.value;
  this.dispatchEvent(new Event("input", {bubbles: true}));
  this.dispatchEvent(new Event("change", {bubbles: true}));
  return o.text.trim();
}`;
const FOCUS_END = `function() { this.focus(); try { if (typeof this.value === "string" && this.setSelectionRange) {
  const n = this.value.length; this.setSelectionRange(n, n); } } catch (e) {}
  return this.ownerDocument.activeElement === this || this.contains(this.ownerDocument.activeElement); }`;

// ------------------------------------------------------------------ methods
const METHODS = {
  async hello() {
    return { version: chrome.runtime.getManifest().version, id: chrome.runtime.id };
  },

  async "tabs.list"() {
    const tabs = await chrome.tabs.query({});
    return tabs.map(tabBrief);
  },

  async "tabs.get"({ tabId }) {        // no attach: the engine gates the URL before I touch the page
    return tabBrief(await getTab(tabId));
  },

  async "tabs.open"(params) {
    const { url, session, watch } = params;
    if (typeof url !== "string") throw E("bad", "url must be a string");
    guardUrl(url);
    if (!/^https?:\/\//i.test(url)) throw E("denied", "I only open http(s) pages");
    const name = String(session || "rowan").slice(0, 24);
    if (!navAllowed(url)) throw E("denied", "that address fails the URL gate");
    let win;
    try { win = await chrome.windows.getLastFocused({ windowTypes: ["normal"] }); } catch (e) { win = null; }
    let tab;
    const blank = `about:blank#rowan-open-${++openSeq}`;
    if (!win) {
      const w = await chrome.windows.create({ url: blank, focused: !!watch });
      tab = w.tabs[0];
    } else {
      const [act] = await chrome.tabs.query({ active: true, windowId: win.id });
      tab = await chrome.tabs.create({ url: blank, active: !!watch, windowId: win.id, index: act ? act.index + 1 : undefined });
    }
    S.mine[String(tab.id)] = { session: name, keep: false, owner: live[params.__owner] || "unknown", root: tab.id };
    await save();
    let gid = await groupFor(name, tab.windowId);
    if (gid != null) await chrome.tabs.group({ tabIds: [tab.id], groupId: gid });
    else {
      gid = await chrome.tabs.group({ tabIds: [tab.id], createProperties: { windowId: tab.windowId } });
      S.groups[name] = gid;
      await save();
    }
    await chrome.tabGroups.update(gid, { title: `\u{1F43A} ${name}`, color: GROUP_COLOR, collapsed: false });
    if (!watch) await chrome.tabs.update(tab.id, { muted: true }).catch(() => {});
    // the gate is armed before the page is asked for: redirects and scripts can't reach a denied page
    await ensureAttached(tab.id, false);
    await cdp(tab.id, "Page.navigate", { url }, 2500).catch((e) => { if (e.code === "stopped") throw e; });
    // a hidden tab's first frame can take seconds: prime one capture, only once the committed URL passes the gate
    (async () => {
      for (let i = 0; i < 40; i++) {
        const t = await chrome.tabs.get(tab.id).catch(() => null);
        if (!t) return;
        if (t.url && !t.url.startsWith("about:blank")) {
          if (navAllowed(t.url)) await cdp(tab.id, "Page.captureScreenshot", { format: "jpeg", quality: 30, optimizeForSpeed: true }, 8000);
          return;
        }
        await new Promise((r) => setTimeout(r, 100));
      }
    })().catch(() => {});
    const g = await chrome.tabGroups.get(gid);
    return { ...tabBrief(await chrome.tabs.get(tab.id)), group: { id: gid, title: g.title, color: g.color } };
  },

  // JOIN: a tab the person opened and wants me in. Never one I opened or handed over (that's reclaim).
  // The engine has already gated the tab's current URL and shown the person the card.
  async "tabs.join"(params) {
    const tabId = int(params.tabId, "tab");
    const t = await getTab(tabId);
    if (S.mine[String(tabId)]) throw E("bad", `tab ${tabId} is already one I'm in`);
    if ((S.handed || {})[String(tabId)]) throw E("bad", `tab ${tabId} is one I opened and handed over: reclaim it instead`);
    const url = t.url || t.pendingUrl || "";
    guardUrl(url);
    if (!navAllowed(url)) throw E("denied", "that tab is on a page I never touch");
    S.mine[String(tabId)] = { session: String(params.session || "rowan").slice(0, 24), keep: true, mode: "joined",
                              owner: live[params.__owner] || "unknown", root: tabId };
    await save();
    return tabBrief(await chrome.tabs.get(tabId));
  },

  // RECLAIM: a tab I opened and handed over; my full hands come back (closing included). Proof it was
  // mine = the handed-over set, never a URL.
  async "tabs.reclaim"(params) {
    const tabId = int(params.tabId, "tab");
    const t = await getTab(tabId);
    if (S.mine[String(tabId)]) throw E("bad", `tab ${tabId} is already one I'm in`);
    if (!(S.handed || {})[String(tabId)]) throw E("not_mine", `tab ${tabId} was never one I opened and handed over`);
    const url = t.url || t.pendingUrl || "";
    guardUrl(url);
    if (!navAllowed(url)) throw E("denied", "that tab is on a page I never touch");
    delete S.handed[String(tabId)];
    // keep: true, so at done it is handed back again, not closed, unless I close it on purpose
    S.mine[String(tabId)] = { session: String(params.session || "rowan").slice(0, 24), keep: true, mode: "reclaimed",
                              owner: live[params.__owner] || "unknown", root: tabId };
    await save();
    return tabBrief(await chrome.tabs.get(tabId));
  },

  // LEAVE: I step out of a tab I joined (it stays the person's, untouched) or reclaimed (handed back, reclaimable).
  async "tabs.leave"(params) {
    const tabId = int(params.tabId, "tab");
    const m = S.mine[String(tabId)];
    if (!m || (m.mode !== "joined" && m.mode !== "reclaimed")) throw E("bad", `tab ${tabId} isn't one I joined or reclaimed`);
    if (attached.has(tabId)) {
      await overlay(tabId, { hands: "bye" }).catch(() => {});
      await new Promise((r) => chrome.debugger.detach({ tabId }, () => { void chrome.runtime.lastError; r(); }));
      attached.delete(tabId);
    }
    if (m.mode === "reclaimed") (S.handed || (S.handed = {}))[String(tabId)] = true;
    delete S.mine[String(tabId)];
    delete stopped[tabId];
    await save();
    return { left: tabId, was: m.mode };
  },

  async "tabs.close"({ tabId }) {
    mustBeMine(tabId);
    if (isJoined(tabId)) throw E("joined", `tab ${tabId} is the person's: I joined it, and I never close it`);
    await getTab(tabId);
    await chrome.tabs.remove(tabId);
    delete S.mine[String(tabId)];
    await save();
    return { closed: tabId };
  },

  async "tabs.keep"({ tabId, keep }) {
    mustBeMine(tabId);
    if (isJoined(tabId) && keep === false) throw E("joined", `tab ${tabId} is the person's: it always stays open`);
    S.mine[String(tabId)].keep = keep !== false;
    await save();
    return { tabId, keep: S.mine[String(tabId)].keep };
  },

  // End of my turn: close what I opened; a kept tab is handed to the person (debugger off, no longer mine, never closed by me).
  async done({ tabIds }) {
    if (!Array.isArray(tabIds)) throw E("bad", "tabIds: the tabs this engine opened");
    const roots = new Set(tabIds.map((t) => int(t, "tab")));
    return release([...new Set([...roots, ...Object.entries(S.mine).filter(([, v]) => roots.has(v.root)).map(([k]) => Number(k))])]);
  },

  // An engine says hello: its URL policy and its token. I report the tabs whose engine isn't connected; the engine
  // checks whether that engine's process is really gone and releases those with "orphans.release".
  async "engine.hello"({ policy, engine, __owner }) {
    if (!policy || !Array.isArray(policy.hosts) || !policy.hosts.length) throw E("bad", "policy: {hosts}");
    if (typeof engine !== "string" || !engine || engine.length > 100) throw E("bad", "engine: its token");
    S.policy = { hosts: policy.hosts.map(String).slice(0, 500) };
    live[__owner] = engine;
    await save();
    const connected = new Set(Object.values(live));
    // joined / reclaimed tabs aren't leftovers: the person's yes outlives the engine that asked
    return { leftovers: Object.entries(S.mine).filter(([, v]) => !connected.has(v.owner) && !v.mode).map(([k, v]) => ({ tab: Number(k), owner: v.owner })) };
  },

  // Release tabs of a dead engine (only while that engine is still not connected and still owns them).
  async "orphans.release"({ tabs }) {
    if (!Array.isArray(tabs)) throw E("bad", "tabs: [{tab, owner}]");
    const connected = new Set(Object.values(live));
    const ids = tabs.filter((x) => x && Number.isSafeInteger(x.tab) && S.mine[String(x.tab)] &&
      S.mine[String(x.tab)].owner === x.owner && !connected.has(x.owner)).map((x) => x.tab);
    return release(ids);
  },

  // The last 50 records (tab, text), for checking afterwards; nothing is consumed.
  async "nav.log"() {
    return { log: navLog.slice() };
  },

  // What my tab was kept from or left since the last ask.
  async "nav.blocked"({ tabId }) {
    const id = int(tabId, "tab");
    const b = blocked[id] || [];
    delete blocked[id];
    return { blocked: b };
  },
};

async function release(ids) {
    const closed = [], kept = [];
    for (const id of ids) {
      const k = String(id), v = S.mine[k];
      if (!v) continue;                       // not mine (or already gone): never touched
      if (attached.has(id)) {
        await overlay(id, { hands: "bye" }).catch(() => {});
        await new Promise((r) => chrome.debugger.detach({ tabId: id }, () => { void chrome.runtime.lastError; r(); }));
        attached.delete(id);
      }
      if (v.mode === "joined" || v.mode === "reclaimed") {
        // the person's yes lasts until the tab closes (or I leave it): only the debugger let go above
        delete stopped[id];
        continue;
      }
      if (v.keep) { kept.push(id); (S.handed || (S.handed = {}))[k] = true; }   // handed over: reclaimable
      else { try { await chrome.tabs.remove(id); closed.push(id); } catch (e) { /* already gone */ } }
      delete S.mine[k];
      delete stopped[id];
    }
    await save();
    return { closed, kept };
}

const METHODS2 = {

  // The live frame tree of my tab (for the engine's per-frame verdicts). Never resumes a stop.
  async frames({ tabId, resume }) {
    const tab = await getTab(tabId);
    mustBeMine(tabId);
    guardUrl(tab.url || tab.pendingUrl);
    await ensureAttached(tabId, resume === true);
    const ft = await frameTree(tabId);
    return { loaderId: ft.main.loaderId, frames: ft.list.map(({ id, url, parent }) => ({ id, url, parent })) };
  },

  // Read only frames whose CURRENT url equals the engine's approval for that frame id.
  async look({ tabId, screenshot, tree, approved, resume }) {
    let tab = await getTab(tabId);
    mustBeMine(tabId);
    guardUrl(tab.url || tab.pendingUrl);
    await ensureAttached(tabId, resume === true);
    const t0 = Date.now(), ms = {};
    const ft = await frameTree(tabId);
    if (!approved || approved[ft.main.id] !== ft.main.url)
      throw E("frame_denied", "the page changed since I checked it; look again");
    overlay(tabId, { hands: "mark" }).catch(() => {});
    const out = { loaderId: ft.main.loaderId, frames: [] };
    for (const f of ft.list) {
      const fr = { id: f.id, url: f.url, parent: f.parent };
      if (f.parent) {
        try { fr.owner = (await cdp(tabId, "DOM.getFrameOwner", { frameId: f.id })).backendNodeId; }
        catch (e) { if (e.code === "stopped") throw e; }
      }
      if (approved[f.id] !== f.url) fr.skipped = true;
      else if (f.remote) fr.error = "another process; open it as its own tab to read it";
      else if (tree && out.frames.filter((x) => x.nodes).length < MAX_FRAMES) {
        try {
          const { nodes } = await cdp(tabId, "Accessibility.getFullAXTree", { frameId: f.id });
          fr.nodes = nodes.slice(0, MAX_AX_NODES).map(slim);
        } catch (e) {
          if (e.code === "stopped") throw e;
          fr.error = /frame|not found|no.*node/i.test(e.message) ? "cross-origin frame in another process" : e.message.slice(0, 120);
        }
      }
      out.frames.push(fr);
    }
    ms.tree = Date.now() - t0;
    if (screenshot) {
      const t1 = Date.now();
      let raw = null;
      const cStart = changes[tabId] || 0;
      try {
        if (!(await attachAcked(tabId))) throw E("cover", "I can't confirm every nested frame is known (auto-attach unacknowledged or failed)");
        const ft0 = await frameTree(tabId);
        if (ft0.incomplete) throw E("cover", "I couldn't list the frames inside a cross-site frame");
        const c0 = changes[tabId] || 0;
        const cover = framesToCover(ft0, approved);
        const sc = cover.length ? await shotScale(tabId, ft0) : null;
        const b0 = cover.length ? await frameBoxes(tabId, ft0, cover) : { rects: [], notRendered: 0 };
        for (let attempt = 0; attempt < 2 && !raw; attempt++) {   // hidden tabs: a second ask usually lands at once
          try {
            raw = (await cdp(tabId, "Page.captureScreenshot", { format: "jpeg", quality: 70, optimizeForSpeed: true }, 3000)).data;
          } catch (e) {
            if (e.code === "stopped") throw e;
            out.shot_error = `Chrome produced no frame for the hidden tab (${e.message})`;
          }
        }
        if (raw) {
          // the frame set after the capture must be the one I covered for (ids AND urls), with no frame event
          // in between and every session still acknowledged; boxes from both sides are covered
          const ft1 = await frameTree(tabId);
          const b1 = cover.length ? await frameBoxes(tabId, ft1, cover) : { rects: [], notRendered: 0 };
          if (ft1.incomplete || frameKey(ft0) !== frameKey(ft1) || (changes[tabId] || 0) !== c0)
            throw E("cover", "frames changed during capture");
          if (!(await attachAcked(tabId))) throw E("cover", "a frame appeared whose insides I can't confirm");
          out.jpeg = cover.length ? await redact(raw, b0.rects.concat(b1.rects), sc) : raw;
          out.covered = cover.length - Math.max(b0.notRendered, b1.notRendered);
          out.not_rendered = Math.max(b0.notRendered, b1.notRendered);
          delete out.shot_error;
        }
      } catch (e) {
        if (e.code === "stopped") throw e;
        delete out.jpeg;
        delete out.covered;
        // when frames moved while I was making it, that IS the reason, whichever check tripped first
        const why = (changes[tabId] || 0) !== cStart ? `frames changed during capture (${e.message})` : e.message;
        out.shot_error = `withheld: ${why}; failing closed (a frame I may not read could show)`;
      }
      raw = null;
      ms.shot = Date.now() - t1;
    }
    try { out.viewport = await viewport(tabId); } catch (e) { if (e.code === "stopped") throw e; }
    tab = await getTab(tabId);
    Object.assign(out, tabBrief(tab), { ms });
    return out;
  },

  // One action. ref = {frame, node} from my look; approved = {frameId: url} the engine allows right now.
  async act({ tabId, op, ref, approved, loaderId, text, value, key, dy, dx, button, count, entryId }) {
    const tab = await getTab(tabId);
    mustBeMine(tabId);
    guardUrl(tab.url || tab.pendingUrl);
    await ensureAttached(tabId, false);
    const ft = await frameTree(tabId);
    if (loaderId && ft.main.loaderId !== loaderId)
      throw E("stale", "the page navigated since my look; its refs are stale; look again");
    approvedFrame(ft, approved, ft.main.id);
    let f = null, obj = null, b = null;
    if (ref) {
      f = approvedFrame(ft, approved, ref.frame);
      b = int(ref.node, "node");
      obj = await nodeIn(tabId, f, b);
    }
    if (op === "click") {
      if (!ref) throw E("bad", "click needs a ref");
      const p = await center(tabId, b);
      await hitCheck(tabId, ft, approved, f, obj, p.x, p.y);
      await glide(tabId, p.x, p.y, "click");
      const btn = ["left", "right", "middle"].includes(button) ? button : "left";
      const reached = await guardedClick(tabId, ft, approved, f, obj, p.x, p.y, btn, Math.max(1, Math.min(3, count || 1)));
      return { x: Math.round(p.x), y: Math.round(p.y), reached };
    }
    if (op === "type") {
      if (typeof text !== "string") throw E("bad", "text must be a string");
      if (ref) await focusInto(tabId, ft, approved, f, obj, b, true);
      approvedFrame(ft, approved, await focusedFrame(tabId, ft));   // the frame that will receive the text
      await cdp(tabId, "Input.insertText", { text });
      return { typed: text.length };
    }
    if (op === "set" || op === "select") {
      if (!ref) throw E("bad", `${op} needs a ref`);
      if (typeof value !== "string") throw E("bad", "value must be a string");
      const p = await center(tabId, b).catch((e) => { if (e.code === "stopped") throw e; return null; });
      if (p) await glide(tabId, p.x, p.y, op === "set" ? "type" : "click");
      return op === "set" ? { value: await callObj(tabId, obj, SET_VALUE, [value]) }
                          : { picked: await callObj(tabId, obj, SELECT, [value]) };
    }
    if (op === "key") {
      if (!key || typeof key.key !== "string") throw E("bad", "key must be built by the engine");
      if (ref) await focusInto(tabId, ft, approved, f, obj, b, false);
      approvedFrame(ft, approved, await focusedFrame(tabId, ft));
      const base = { key: key.key, code: key.code, windowsVirtualKeyCode: key.vk, nativeVirtualKeyCode: key.vk,
                     modifiers: key.modifiers || 0 };
      await cdp(tabId, "Input.dispatchKeyEvent", { ...base, type: key.text ? "keyDown" : "rawKeyDown",
                                                   ...(key.text ? { text: key.text, unmodifiedText: key.text } : {}) });
      await cdp(tabId, "Input.dispatchKeyEvent", { ...base, type: "keyUp" });
      return { pressed: key.key };
    }
    if (op === "scroll") {
      let p;
      if (ref) p = await center(tabId, b);
      else { const v = await viewport(tabId); p = { x: v.w / 2, y: v.h / 2 }; }
      const hit = await cdp(tabId, "DOM.getNodeForLocation", { x: Math.round(p.x), y: Math.round(p.y) }).catch((e) => {
        if (e.code === "stopped") throw e; return {}; });
      if (hit.backendNodeId) await notIntoFrame(tabId, hit.backendNodeId);
      if (hit.frameId) approvedFrame(ft, approved, hit.frameId);
      await glide(tabId, p.x, p.y, "scroll");
      await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseWheel", x: p.x, y: p.y, deltaX: (dx || 0) * 100, deltaY: (dy || 0) * 100 });
      return { scrolled: [dx || 0, dy || 0] };
    }
    if (op === "goto") {
      if (typeof value !== "string") throw E("bad", "url must be a string");
      guardUrl(value);
      if (!/^https?:\/\//i.test(value)) throw E("denied", "I only open http(s) pages");
      await cdp(tabId, "Page.navigate", { url: value });
      return { navigating: value };
    }
    if (op === "history") {       // where back/forward would go, for the engine's gate BEFORE I move
      const h = await cdp(tabId, "Page.getNavigationHistory");
      const i = h.currentIndex + (value === "forward" ? 1 : -1);
      if (i < 0 || i >= h.entries.length) throw E("page", `no page to go ${value === "forward" ? "forward" : "back"} to`);
      return { entryId: h.entries[i].id, url: h.entries[i].url };
    }
    if (op === "back" || op === "forward") {
      const h = await cdp(tabId, "Page.getNavigationHistory");
      const i = h.currentIndex + (op === "forward" ? 1 : -1);
      if (i < 0 || i >= h.entries.length || h.entries[i].id !== entryId) throw E("stale", "the history changed since I checked it; look again");
      if (!navAllowed(h.entries[i].url)) throw E("denied", "that history entry fails the URL gate");
      await cdp(tabId, "Page.navigateToHistoryEntry", { entryId });
      return { [op]: true };
    }
    throw E("unknown", `unknown act op ${op}`);
  },

  // Test seams below are refused unless the host announced test mode (test pipe + HANDS_TEST_HOOKS=1).
  // Faults on my side of the screenshot cover path.
  async "debug.faults"({ boxError, autoAttachFail }) {
    if (!testMode) throw E("denied", "test hooks are off");
    fault.boxError = boxError === true;
    fault.autoAttachFail = autoAttachFail === true;
    return { ...fault };
  },

  async "debug.trace"() {
    if (!testMode) throw E("denied", "test hooks are off");
    return { trace };
  },

  // The state Chrome leaves when the person presses Cancel on the debugging bar (that bar can't be clicked from a test).
  async "debug.simulateCancel"({ tabId }) {
    if (!testMode) throw E("denied", "test hooks are off");
    mustBeMine(tabId);
    await new Promise((r) => chrome.debugger.detach({ tabId }, () => { void chrome.runtime.lastError; r(); }));
    attached.delete(tabId);
    stopped[tabId] = "the person cancelled the debugging bar";
    await save();
    overlay(tabId, { hands: "bye" }).catch(() => {});
    emit({ event: "stopped", reason: stopped[tabId] });
    return { simulated: tabId };
  },
};
Object.assign(METHODS, METHODS2);

const PROPS = new Set(["focused", "disabled", "checked", "expanded", "selected", "required", "invalid", "readonly",
                       "pressed", "level", "multiline", "modal", "editable"]);
function slim(n) {
  const o = { i: n.nodeId, r: n.role ? String(n.role.value || "") : "", b: n.backendDOMNodeId };
  if (n.ignored) o.ig = 1;
  if (n.name && n.name.value) o.n = String(n.name.value).slice(0, 400);
  if (n.value && n.value.value !== undefined && n.value.value !== "") o.v = String(n.value.value).slice(0, 400);
  if (n.childIds && n.childIds.length) o.c = n.childIds;
  if (n.parentId) o.p = n.parentId;
  if (n.properties) {
    const pr = {};
    for (const p of n.properties) if (PROPS.has(p.name) && p.value && p.value.value !== false && p.value.value !== "false")
      pr[p.name] = p.value.value;
    if (Object.keys(pr).length) o.s = pr;
  }
  return o;
}

// ------------------------------------------------------------------ wake-ups: any of these reconnects the host
// The alarm wakes a worker Chrome put to sleep (or that died) so the host comes back without the person doing anything.
chrome.alarms.create("hands-wake", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => connect());
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
chrome.tabs.onActivated.addListener(connect);
chrome.tabs.onUpdated.addListener(connect);
chrome.windows.onFocusChanged.addListener(connect);
connect();
load().then(seedHerFocus);
