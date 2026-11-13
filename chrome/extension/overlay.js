// Hands overlay: draws ONLY. The agent's cursor glides to the target and lands before the
// service worker sends the input; a pink heart leads the tab title while I drive. Invisible to the page's
// accessibility tree (aria-hidden) and to its pointer (pointer-events:none); closed shadow root.
(() => {
  if (window.__rowanHands) return;
  const MARK = "\u{1FA77} ";
  const host = document.createElement("rowan-hands");
  host.setAttribute("aria-hidden", "true");
  host.style.cssText = "position:fixed;inset:0;z-index:2147483646;pointer-events:none;display:block;contain:strict";
  const root = host.attachShadow({ mode: "closed" });
  root.innerHTML = `
    <style>
      :host { all: initial; }
      .c { position: fixed; left: 0; top: 0; width: 30px; height: 34px; transform: translate(-200px,-200px);
           filter: drop-shadow(0 0 6px rgba(255,46,147,.85)) drop-shadow(0 1px 1px rgba(0,0,0,.35)); opacity: 0;
           transition: opacity .2s; will-change: transform; }
      .c.on { opacity: 1; }
      .c.land svg { animation: land .22s ease-out; }
      @keyframes land { 0% { transform: scale(1) } 45% { transform: scale(.82) } 100% { transform: scale(1) } }
    </style>
    <div class="c"><svg width="30" height="34" viewBox="0 0 30 34">
      <path d="M2 2 L2 24 L8 18.5 L12.5 29 L16.5 27.2 L12 17 L20 17 Z" fill="#ff2e93" stroke="#fff" stroke-width="1.6"
            stroke-linejoin="round"/>
      <circle cx="22" cy="25" r="7.2" fill="#ff2e93" stroke="#fff" stroke-width="1.4"/>
      <text x="22" y="28.6" font-family="Segoe UI, Arial, sans-serif" font-size="10" font-weight="700"
            fill="#fff" text-anchor="middle">R</text></svg></div>`;
  const cur = root.querySelector(".c");
  let pos = null;
  let titleOn = false;

  function attach() { if (!host.isConnected) (document.documentElement || document).appendChild(host); }
  function markTitle() {
    if (!titleOn) return;
    if (!document.title.startsWith(MARK)) document.title = MARK + document.title;
  }
  const titleWatch = new MutationObserver(markTitle);

  function glide(x, y, done) {
    attach();
    cur.classList.add("on");
    const to = `translate(${x - 2}px, ${y - 2}px)`;
    if (document.visibilityState !== "visible" || !pos) {   // hidden tab: nothing to watch, land at once
      cur.style.transform = to;
      pos = [x, y];
      done();
      return;
    }
    const d = Math.hypot(x - pos[0], y - pos[1]);
    const ms = Math.max(140, Math.min(420, d * 0.6));
    const a = cur.animate([{ transform: cur.style.transform || to }, { transform: to }],
                          { duration: ms, easing: "cubic-bezier(.22,.8,.3,1)" });
    let finished = false;
    const fin = () => {
      if (finished) return;
      finished = true;
      cur.style.transform = to;
      pos = [x, y];
      cur.classList.remove("land"); void cur.offsetWidth; cur.classList.add("land");
      setTimeout(done, 60);
    };
    a.onfinish = fin;
    setTimeout(fin, ms + 120);   // never let a throttled frame hold the click
  }

  chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
    if (!msg || !msg.hands) return;
    if (msg.hands === "glide") { titleOn = true; markTitle(); glide(msg.x, msg.y, () => reply({ arrived: true })); return true; }
    if (msg.hands === "mark") {
      titleOn = true; markTitle(); attach();
      const t = document.querySelector("title");
      if (t) titleWatch.observe(t, { childList: true, characterData: true, subtree: true });
      reply({ ok: true });
      return;
    }
    if (msg.hands === "bye") {
      titleOn = false; titleWatch.disconnect();
      if (document.title.startsWith(MARK)) document.title = document.title.slice(MARK.length);
      host.remove(); pos = null; cur.classList.remove("on");
      reply({ ok: true });
      return;
    }
    if (msg.hands === "ping") { reply({ ok: true }); return; }
  });
  window.__rowanHands = true;
})();
