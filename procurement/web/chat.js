/* Ask: a chat panel that answers questions from the app's data (POST /api/chat).
   Plain script, no build step. Self-mounting:
     <div id="ask-root"></div>
     <script src="/web/chat.js" defer></script>
   Uses the page's CSS tokens (--accent, --line …) and .btn/.chip/.tbl from intake.html.
   History lives in sessionStorage; the server is stateless. Model text is escaped before the small
   markdown renderer adds any tag, so nothing from the model reaches innerHTML unescaped. */
(function () {
  "use strict";
  const KEY = "ask.history.v1";
  const SEND_TURNS = 20;
  const STARTERS = [
    "Which requirements are waiting on the site, and since when?",
    "Which item codes in the ERP master are duplicated?",
    "Which waterproofing vendors have no contact details?",
    "Compare the GST and payment terms across the quotes.",
  ];

  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

  // ── Tiny markdown: tables, lists, headings, bold, italic, code. Input is escaped first. ──────
  function inline(s) {
    return s
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/(^|[\s(])_([^_]+)_(?=[\s).,;:!?]|$)/g, "$1<i>$2</i>")
      .replace(/(^|[\s(])\*([^*\s][^*]*)\*(?=[\s).,;:!?]|$)/g, "$1<i>$2</i>");
  }
  const cells = row => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(c => c.trim());
  const isSep = row => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(row);
  const numeric = c => /^[-–₹+]?\s*[₹]?[\d,]+(\.\d+)?\s*(%|lakh|cr)?$/i.test(c.replace(/<[^>]+>/g, "").trim());

  function md(text) {
    const lines = esc(text).replace(/\r/g, "").split("\n");
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const ln = lines[i];
      if (/^\s*\|.*\|\s*$/.test(ln) && i + 1 < lines.length && isSep(lines[i + 1])) {
        const head = cells(ln);
        i += 2;
        const rows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(cells(lines[i++]));
        const numCol = head.map((_, k) => rows.length > 0 && rows.every(r => !r[k] || numeric(r[k])));
        out.push(`<div class="tbl"><table><thead><tr>${head.map((h, k) => `<th${numCol[k] ? ' class="n"' : ""}>${inline(h)}</th>`).join("")}</tr></thead><tbody>${
          rows.map(r => `<tr>${head.map((_, k) => `<td${numCol[k] ? ' class="n"' : ""}>${inline(r[k] ?? "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
        continue;
      }
      const ul = /^\s*[-*•]\s+(.*)$/, ol = /^\s*\d+[.)]\s+(.*)$/;
      if (ul.test(ln) || ol.test(ln)) {
        const re = ul.test(ln) ? ul : ol, tag = re === ul ? "ul" : "ol";
        const items = [];
        while (i < lines.length && re.test(lines[i])) items.push(`<li>${inline(lines[i++].match(re)[1])}</li>`);
        out.push(`<${tag}>${items.join("")}</${tag}>`);
        continue;
      }
      const h = ln.match(/^\s*#{1,4}\s+(.*)$/);
      if (h) { out.push(`<p class="h">${inline(h[1])}</p>`); i++; continue; }
      if (!ln.trim()) { i++; continue; }
      const para = [];
      while (i < lines.length && lines[i].trim() && !/^\s*(\||[-*•]\s|\d+[.)]\s|#{1,4}\s)/.test(lines[i])) para.push(inline(lines[i++]));
      if (para.length) out.push(`<p>${para.join("<br>")}</p>`);
      else out.push(`<p>${inline(lines[i++])}</p>`);
    }
    return out.join("");
  }

  // ── State ───────────────────────────────────────────────────────────────────────────────────
  let history = [];
  try { history = JSON.parse(sessionStorage.getItem(KEY) || "[]"); } catch { history = []; }
  const save = () => { try { sessionStorage.setItem(KEY, JSON.stringify(history.slice(-60))); } catch { /* full or blocked */ } };
  let busy = false, status = null;

  function page() {
    const hash = location.hash || "#overview";
    const m = hash.match(/^#cases\/(.+)$/);
    return {hash, uid: m ? decodeURIComponent(m[1]) : null};
  }

  // ── UI ──────────────────────────────────────────────────────────────────────────────────────
  function injectStyle() {
    if (document.getElementById("ask-style")) return;
    const st = document.createElement("style");
    st.id = "ask-style";
    st.textContent = `
.ask-fab{position:fixed;right:max(18px,env(safe-area-inset-right));bottom:max(18px,env(safe-area-inset-bottom));z-index:20;border-radius:999px;padding:10px 18px;box-shadow:0 4px 14px rgba(0,0,0,.18)}
.ask-panel{position:fixed;right:max(18px,env(safe-area-inset-right));bottom:max(18px,env(safe-area-inset-bottom));width:min(380px,calc(100vw - 24px));height:min(540px,calc(100vh - 100px));z-index:30;background:var(--surface);border:1px solid var(--line);border-radius:14px;overflow:hidden;display:flex;flex-direction:column;box-shadow:0 12px 36px rgba(0,0,0,.22)}
.ask-panel.wide{width:min(640px,calc(100vw - 24px));height:min(720px,calc(100vh - 60px))}
@media (max-width:520px){.ask-panel{right:12px;bottom:12px}}
.ask-head{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:12px 14px;border-bottom:1px solid var(--line)}
.ask-head h2{font-size:1rem} .ask-head .acts{gap:6px} .ask-head .btn{padding:5px 10px;font-size:.8rem}
.ask-log{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:12px}
.ask-msg{max-width:100%;font-size:.9rem;line-height:1.5;overflow-wrap:anywhere}
.ask-msg.user{align-self:flex-end;background:var(--accent-soft);padding:8px 12px;border-radius:10px 10px 2px 10px;max-width:88%;white-space:pre-wrap}
.ask-msg.bot{align-self:stretch;display:flex;flex-direction:column;gap:8px}
.ask-msg.bot p{margin:0} .ask-msg.bot p.h{font-weight:600} .ask-msg.bot ul,.ask-msg.bot ol{margin:0;padding-left:20px;display:flex;flex-direction:column;gap:3px}
.ask-msg.bot code{font:12px var(--mono);background:var(--sunk);padding:1px 4px;border-radius:4px}
.ask-msg.bot table{font-size:.8rem} .ask-msg.bot th,.ask-msg.bot td{padding:5px 8px}
.ask-msg.err{color:var(--bad);background:var(--bad-soft);padding:8px 12px;border-radius:8px}
.ask-looked{font-size:.74rem;color:var(--muted);font-family:var(--mono)}
.ask-thinking{color:var(--muted);font-size:.86rem;font-style:italic}
.ask-starters{display:flex;flex-direction:column;gap:6px}
.ask-starters button{all:unset;cursor:pointer;font-size:.86rem;padding:8px 10px;border:1px solid var(--line);border-radius:8px}
.ask-starters button:hover{border-color:var(--accent);background:var(--accent-soft)} .ask-starters button:focus-visible{outline:2px solid var(--accent)}
.ask-form{display:flex;gap:8px;padding:12px 14px;border-top:1px solid var(--line);align-items:flex-end}
.ask-form textarea{flex:1;min-height:42px;max-height:140px;font:14px/1.4 var(--body);padding:9px 10px;resize:none}
.ask-note{padding:10px 14px;font-size:.85rem;background:var(--warn-soft);border-bottom:1px solid var(--warn)}`;
    document.head.appendChild(st);
  }

  let root, panel, log, input, sendBtn, note;

  function build() {
    injectStyle();
    root = document.getElementById("ask-root") || document.body.appendChild(Object.assign(document.createElement("div"), {id: "ask-root"}));
    root.innerHTML = `
      <button class="btn primary ask-fab" type="button" aria-haspopup="dialog" aria-controls="ask-panel">Ask</button>
      <aside class="ask-panel" id="ask-panel" role="dialog" aria-label="Ask about the procurement data" hidden>
        <div class="ask-head"><div><h2>Ask</h2><div class="small muted">Answers from this app's data only</div></div>
          <div class="acts"><button class="btn" type="button" data-act="wide" aria-label="Make the chat bigger" title="Bigger / smaller">⤢</button><button class="btn" type="button" data-act="clear">Clear</button><button class="btn" type="button" data-act="close" aria-label="Close">✕</button></div></div>
        <div class="ask-note" hidden></div>
        <div class="ask-log" aria-live="polite"></div>
        <form class="ask-form"><textarea rows="1" placeholder="Ask about requirements, item codes, vendors, quotes…" aria-label="Your question"></textarea>
          <button class="btn primary" type="submit">Send</button></form>
      </aside>`;
    panel = root.querySelector(".ask-panel");
    log = root.querySelector(".ask-log");
    input = root.querySelector("textarea");
    sendBtn = root.querySelector(".ask-form .btn");
    note = root.querySelector(".ask-note");
    root.querySelector(".ask-fab").addEventListener("click", () => toggle(true));
    root.querySelector('[data-act="wide"]').addEventListener("click", () => panel.classList.toggle("wide"));
    root.querySelector('[data-act="close"]').addEventListener("click", () => toggle(false));
    root.querySelector('[data-act="clear"]').addEventListener("click", () => { history = []; save(); draw(); input.focus(); });
    root.querySelector(".ask-form").addEventListener("submit", e => { e.preventDefault(); send(input.value); });
    input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(input.value); } });
    input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 140) + "px"; });
    panel.addEventListener("keydown", e => { if (e.key === "Escape") toggle(false); });
  }

  async function toggle(open) {
    panel.hidden = !open;
    root.querySelector(".ask-fab").hidden = open;
    if (!open) { root.querySelector(".ask-fab").focus(); return; }
    draw();
    input.focus();
    if (!status) {
      try { status = await (await fetch("/api/chat/status")).json(); } catch { status = {ready: false, message: "Couldn't reach the server."}; }
    }
    note.hidden = !!status.ready;
    note.textContent = status.ready ? "" : status.message;
  }

  function looked(calls) {
    if (!calls || !calls.length) return "";
    const uniq = [...new Set(calls.map(c => c.input_summary || c.name))];
    return `<div class="ask-looked">looked at: ${esc(uniq.join(", "))}</div>`;
  }

  function draw() {
    if (!history.length) {
      log.innerHTML = `<div class="small muted">Ask about requirements, item codes, vendors or quotes. For example:</div>
        <div class="ask-starters">${STARTERS.map(q => `<button type="button">${esc(q)}</button>`).join("")}</div>`;
      log.querySelectorAll(".ask-starters button").forEach(b => b.addEventListener("click", () => send(b.textContent)));
    } else {
      log.innerHTML = history.map(m => m.role === "user"
        ? `<div class="ask-msg user">${esc(m.content)}</div>`
        : m.error ? `<div class="ask-msg err">${esc(m.content)}</div>`
        : `<div class="ask-msg bot">${md(m.content)}${looked(m.tools)}</div>`).join("");
    }
    if (busy) log.insertAdjacentHTML("beforeend", `<div class="ask-thinking">Thinking…</div>`);
    log.scrollTop = log.scrollHeight;
    sendBtn.disabled = busy;
  }

  async function send(text) {
    text = String(text || "").trim();
    if (!text || busy) return;
    history.push({role: "user", content: text});
    input.value = "";
    input.style.height = "auto";
    busy = true;
    save(); draw();
    const turns = history.filter(m => !m.error).slice(-SEND_TURNS).map(m => ({role: m.role, content: m.content}));
    try {
      const res = await fetch("/api/chat", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({messages: turns, page: page()})});
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const msg = typeof data.detail === "string" ? data.detail : `Something went wrong (${res.status}).`;
        history.push({role: "assistant", content: msg, error: true});
      } else {
        history.push({role: "assistant", content: data.reply || "", tools: data.tool_calls || []});
      }
    } catch {
      history.push({role: "assistant", content: "Couldn't reach the server.", error: true});
    }
    busy = false;
    save(); draw();
    input.focus();
  }

  window.askMarkdown = md; // for tests
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", build);
  else build();
})();
