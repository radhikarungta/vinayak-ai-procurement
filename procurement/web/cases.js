// Requirements workflow, one email at a time:
//   1 what was asked → 2 item codes (reply to site for conflicts / partial / no match) → 3 approve items
//   → 4 vendor picks and the RFQ drafts → 5 compare quotes, choose the vendor → 6 email the HOD.
// Colours mean one thing each: green done · amber needs you · teal in progress / info · grey waiting on others.
// Plain script, no build step. Usage: load /web/cases.js, then call window.mountCases(el).
// Uses the page's CSS (.panel .eyebrow .chip .btn .tbl .muted .small .mono .err .empty .mail .checks .fields .q .body).
(function () {
  "use strict";
  const ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"};
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ESC[c]);
  const STAGE = {
    item_codes: ["Item codes", "warn"],
    waiting_on_site: ["Waiting on site", ""],
    approve: ["To approve", "warn"],
    vendors: ["Pick vendors", "warn"],
    rfq: ["Send RFQs", "warn"],
    rfq_sent: ["Waiting for quotes", ""],
    quotes: ["Compare quotes", "warn"],
    hod: ["With HOD", "warn"],
    done: ["HOD approved", "good"],
    error: ["Can't open", "bad"],
  };
  const ORDER = ["item_codes", "waiting_on_site", "approve", "vendors", "rfq", "rfq_sent", "quotes", "hod", "done"];
  const LINE = {ready: ["Matched", "good"], decided: ["Code picked", "good"], skipped: ["Left out", ""], to_reply: ["Needs a decision", "warn"]};
  const ISSUE = {conflict: ["Code conflict", "bad"], partial: ["Partial match", "warn"], no_match: ["No item code", "bad"]};
  const FIELDS = [["project", "Project"], ["block", "Tower / block"], ["work_type", "What is needed"], ["scope_areas", "Details"],
    ["floors", "Floors"], ["start", "Needed by / start"], ["finish", "Finish"], ["requested_by", "Raised by"], ["requested_action", "Asks for"],
    ["vendor_named", "Vendor named"], ["attachments_referenced", "Attachments mentioned"]];

  async function getJSON(url, opts) {
    const r = await fetch(url, opts);
    let body = null;
    try { body = await r.json(); } catch { /* not JSON */ }
    if (!r.ok) throw new Error((body && body.detail) || `Request failed (${r.status}).`);
    return body;
  }
  const post = (url, data) => getJSON(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(data)});
  const qty = l => (l.quantity == null ? "" : `${Number(l.quantity).toLocaleString("en-IN", {maximumFractionDigits: 2})} ${l.uom || ""}`.trim());
  const addr = s => ((s || "").match(/<([^>]+)>/) || [null, s || ""])[1].trim();
  const when = s => (s || "").replace("T", " ").slice(0, 16);
  const num = (x, d = 2) => (x == null ? "–" : Number(x).toLocaleString("en-IN", {maximumFractionDigits: d}));
  const inr = x => (x == null ? "–" : "₹" + Math.round(x).toLocaleString("en-IN"));

  let root, current = null, list = [], vendorData = null;
  const edits = {};  // unsent edits to the reply / RFQ drafts, kept across redraws: key -> {to, subject, body}

  function style() {
    if (document.getElementById("wf-style")) return;
    const st = document.createElement("style");
    st.id = "wf-style";
    st.textContent = `
.wf{display:flex;flex-direction:column;gap:14px;min-width:0}
.wf{height:calc(100vh - 140px)}
.wf .head{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.wf .head h2{font-size:1.2rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wf .pane{flex:1;min-height:0;overflow:auto;padding-right:4px}
.wf .pane .body{max-height:52vh;overflow:auto}
.wf .stepper{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:6px}
.wf .stepper button.on{outline:2px solid var(--accent);outline-offset:-1px}
.wf .stepper button{all:unset;cursor:pointer;display:flex;flex-direction:column;gap:2px;padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:var(--surface);font-size:.8rem;min-width:0}
.wf .stepper button b{font:700 .85rem var(--display);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wf .stepper button.done{border-color:var(--good)} .wf .stepper button.done span{color:var(--good)}
.wf .stepper button.now{border-color:var(--accent);background:var(--accent-soft)} .wf .stepper button.now span{color:var(--accent)}
.wf .stepper button.locked{opacity:.6}
.wf .stepper button:focus-visible{outline:2px solid var(--accent)}
.wf .subh{font:700 .95rem var(--display);margin:14px 0 4px}
.wf section.sec{display:flex;flex-direction:column;gap:10px;scroll-margin-top:190px}
.wf section.sec.locked > :not(.sh){opacity:.55;pointer-events:none}
.wf .sh{display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap}
.wf .sh h3{font-size:1.05rem}
.wf .line{display:flex;flex-direction:column;gap:6px}
.wf .line .top{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap}
.wf input.txt{width:100%;box-sizing:border-box;font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--surface);color:var(--fg)}
.wf details summary{cursor:pointer;font-size:.82rem;color:var(--muted)}
.wf .why{margin:6px 0 0;padding-left:18px;font-size:.82rem}
.wf td.l1{background:var(--good-soft);color:var(--good);font-weight:600}
.wf td.nc{color:var(--muted)}
.wf input.rate{width:90px;font:inherit;padding:3px 6px;border:1px solid var(--line);border-radius:6px;background:var(--surface);color:var(--fg);text-align:right}
.wf ul.sum{margin:0;padding-left:18px;display:flex;flex-direction:column;gap:4px;font-size:.88rem}
.wf label.btn input[type=file]{display:none}
.wf .views{display:flex;gap:6px;align-items:center}
.wf .views button.on{outline:2px solid var(--accent);outline-offset:-1px}
.spin{display:inline-block;width:10px;height:10px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:wfspin .8s linear infinite;vertical-align:-1px}
@keyframes wfspin{to{transform:rotate(360deg)}}
.wf .thread{display:flex;flex-direction:column;gap:10px}
.wf .mail{max-width:78%;border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:var(--surface);display:flex;flex-direction:column;gap:4px}
.wf .mail.out{align-self:flex-end;background:var(--accent-soft);border-color:var(--accent)}
.wf .mail.old{opacity:.55}
.wf .mail .mh{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;font-size:.8rem}
.wf .mail pre{white-space:pre-wrap;font:inherit;font-size:.85rem;margin:6px 0 0;max-height:240px;overflow:auto}
.wf .hist{list-style:none;margin:0;padding:0;display:flex;flex-direction:column}
.wf .hist li{display:grid;grid-template-columns:150px 80px 1fr;gap:10px;padding:7px 4px;border-bottom:1px solid var(--line);font-size:.85rem}
.wf .who{font-size:.72rem;text-transform:uppercase;letter-spacing:.04em;padding:1px 6px;border-radius:6px;border:1px solid var(--line);justify-self:start}
.wf .who.agent{border-color:var(--accent);color:var(--accent)} .wf .who.site{border-color:var(--warn);color:var(--warn)}
.wf .who.vendor{border-color:var(--good);color:var(--good)} .wf .who.system{color:var(--muted)}
.needs{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;padding:10px 14px;margin-bottom:14px;border-radius:8px;border:1px solid var(--line);background:var(--surface);font-size:.9rem}
.needs.has{border-color:var(--warn)} .needs b{margin-right:2px}
.needs button{all:unset;cursor:pointer;padding:2px 9px;border-radius:999px;background:var(--sunk);font-size:.84rem}
.needs button:hover,.needs button[aria-pressed="true"]{background:var(--accent-soft);color:var(--accent)}
.needs .w{color:var(--muted);font-size:.84rem}
.wf .next{display:flex;gap:10px 14px;align-items:center;flex-wrap:wrap;padding:12px 14px;border-radius:10px;border:1px solid var(--line);background:var(--surface)}
.wf .next.warn{border-color:var(--warn);background:linear-gradient(90deg,var(--warn-soft),var(--surface) 55%)}
.wf .next.good{border-color:var(--good)} .wf .next.running{border-color:var(--accent)}
.wf .next .what{flex:1;min-width:240px;display:flex;flex-direction:column;gap:2px}
.wf .next .what b{font-size:.98rem} .wf .next details{flex-basis:100%}
.wf .next ol{margin:6px 0 0;padding-left:20px;display:flex;flex-direction:column;gap:3px;font-size:.85rem}
.wf details.panel > summary{font-size:.88rem;color:var(--fg);display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.wf details.panel[open] > summary{margin-bottom:4px}
.wf .settled td{vertical-align:middle}
.wf .err.wf-err{margin-top:6px}
.trk{display:flex;align-items:center;list-style:none;margin:0;padding:0}
.trk li{display:flex;align-items:center}
.trk li+li::before{content:"";width:9px;height:2px;background:var(--line)}
.trk li.done+li::before{background:var(--good)}
.trk span{width:21px;height:21px;border-radius:50%;display:grid;place-items:center;font:600 .68rem var(--mono);border:1.5px solid var(--line);color:var(--muted);background:var(--surface);box-sizing:border-box}
.trk .done span{background:var(--good);border-color:var(--good);color:var(--surface)}
.trk .now span{background:var(--warn-soft);border-color:var(--warn);color:var(--warn)}
.trk .wait span{border:1.5px dashed var(--muted);color:var(--fg)}
.ovl{display:flex;gap:16px;flex-wrap:wrap;font-size:.76rem;color:var(--muted);margin-top:8px;align-items:center}
.ovl .trk span{width:13px;height:13px;font-size:0}
.simmenu{position:relative} .simmenu > summary{list-style:none;display:inline-block} .simmenu > summary::-webkit-details-marker{display:none}
.simmenu .menu{position:absolute;right:0;top:calc(100% + 6px);z-index:20;width:320px;background:var(--surface);border:1px solid var(--line);border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,.18);padding:6px;display:flex;flex-direction:column}
.simmenu .menu button{all:unset;cursor:pointer;display:flex;flex-direction:column;gap:2px;padding:9px 10px;border-radius:7px;font-size:.88rem}
.simmenu .menu button span{font-size:.78rem;color:var(--muted)} .simmenu .menu button:hover,.simmenu .menu button:focus-visible{background:var(--accent-soft)}
.ovt{overflow:visible} .ovt table{table-layout:fixed;font-size:.8rem} .ovt th{white-space:normal;padding:6px}
.ovt td{padding:6px;overflow-wrap:anywhere} .ovt td .chip{white-space:normal;overflow-wrap:break-word;padding:2px 7px;font-size:.74rem;border-radius:6px;line-height:1.35}
.ovt th:first-child{width:40%} .ovt th:nth-child(2){width:180px} .ovt th:last-child{width:110px} .ovt td{vertical-align:middle}
.ovt td .btn{padding:4px 8px;font-size:.76rem;white-space:nowrap}
@media (max-width:900px){.wf .stepper{grid-template-columns:repeat(3,minmax(0,1fr))}}`;
    document.head.appendChild(st);
  }

  // ── step state ──
  function steps(c) {
    const at = ORDER.indexOf(c.stage);
    const itemsDone = at >= ORDER.indexOf("approve");
    const aw = Object.values(c.award || {}).map(a => a.vendor);
    const rec = (c.quotes.categories || []).map(g => g.recommended && g.recommended.vendor).filter(Boolean);
    return [
      {id: "s1", n: 1, title: "Requirement", state: "done", note: "Read from the email"},
      {id: "s2", n: 2, title: "Item codes", state: itemsDone ? "done" : "now",
        note: itemsDone ? `${c.counts.ready + c.counts.decided} matched` : c.stage === "waiting_on_site" ? "Waiting on site" : c.counts.to_reply ? `${c.counts.to_reply} to decide` : "Email problem"},
      {id: "s3", n: 3, title: "Approve items", state: c.approved_at ? "done" : c.stage === "approve" ? "now" : "locked",
        note: c.approved_at ? "Approved" : c.stage === "approve" ? "Ready to approve" : "After item codes"},
      {id: "s4", n: 4, title: "Vendors & RFQ", state: at >= ORDER.indexOf("rfq_sent") ? "done" : c.stage === "vendors" || c.stage === "rfq" ? "now" : "locked",
        note: at >= ORDER.indexOf("rfq_sent") ? `${c.rfq_drafts.length} RFQs sent` : c.stage === "vendors" ? "Pick vendors" : c.stage === "rfq" ? `${c.rfq_drafts.filter(d => !d.sent).length} RFQs to send` : "After approval"},
      {id: "s6", n: 5, title: "Compare quotes", state: aw.length ? "done" : c.quotes.open ? "now" : "locked",
        note: aw.length ? `${aw.join(", ")} chosen` : rec.length ? `${rec.join(", ")} recommended` : c.quotes_in ? `${c.quotes_in} quote${c.quotes_in === 1 ? "" : "s"}` : c.quotes.open ? "Waiting for quotes" : "After RFQs"},
      {id: "s7", n: 6, title: "HOD approval", state: c.stage === "done" ? "done" : c.stage === "hod" ? "now" : "locked",
        note: c.stage === "done" ? "Approved" : c.stage === "hod" ? (c.hod ? "Sent to HOD" : "Email to send") : "After choosing"},
    ];
  }

  // ── 1 · requirement ──
  function field(f) {
    if (!f) return null;
    const v = f.values ? (f.values.length ? f.values.map(esc).join(" · ") : null) : (f.value != null && f.value !== "" ? esc(f.value) : null);
    return v == null ? null : `${v}${f.quote ? `<span class="q">“${esc(f.quote)}”</span>` : ""}`;
  }
  function secRequirement(c) {
    const req = c.requirement || {};
    const rows = FIELDS.map(([k, l]) => [l, field(req[k])]).filter(([, v]) => v);
    const att = (c.email.attachments || []).map(a => `<span class="chip">${esc(a.filename)}${a.used ? " · read" : ""}</span>`).join(" ");
    const checks = c.email_checks.length ? `<div class="eyebrow">Problems with the email</div>
      <ul class="checks">${c.email_checks.map(k => `<li class="${k.resolved ? "ok" : esc(k.severity)}">
        <span class="sev">${k.resolved ? "sorted" : k.severity === "block" ? "stop" : "check"}</span>
        <div><b>${esc(k.title)}.</b> <span class="small">${esc(k.detail)}</span>
        <div class="small muted">${k.resolved ? "Marked sorted." : "Included in the reply to the site (step 2)."} <button class="btn" style="padding:2px 8px;margin-left:6px" data-check="${esc(k.id)}" data-resolved="${k.resolved ? "0" : "1"}">${k.resolved ? "Reopen" : "Mark sorted"}</button></div></div></li>`).join("")}</ul>` : "";
    return `<section class="sec" id="s1"><div class="sh"><h3>1 · What was asked</h3><span class="small muted">${esc(c.sender || "")} · ${esc(c.date || "")}</span></div>
      <div class="panel"><div class="grid2">
        <div style="display:flex;flex-direction:column;gap:8px;min-width:0"><div class="eyebrow">The email</div><p class="body">${esc(c.email.body)}</p>${att ? `<div>${att}</div>` : ""}</div>
        <div style="display:flex;flex-direction:column;gap:8px;min-width:0"><div class="eyebrow">Requirement read from it</div>
          ${rows.length ? `<dl class="fields">${rows.map(([l, v]) => `<dt>${l}</dt><dd>${v}</dd>`).join("")}</dl>` : `<span class="none">Nothing could be read.</span>`}
          ${c.quantity_sheet ? `<p class="small muted">Quantity sheet ${esc(c.quantity_sheet.filename)}: ${c.quantity_sheet.lines} lines.</p>` : ""}</div>
      </div>${checks}</div></section>`;
  }

  // ── 2 · item codes + reply ──
  function choices(ln) {
    const m = ln.match, out = [];
    if (m.code) out.push({codes: [m.code, ...(m.also || [])], label: [m.code, ...(m.also || [])].join(" + "), title: m.erp_text});
    for (const k of m.candidates || []) if (k.code !== m.code && out.length < 4) out.push({codes: [k.code], label: k.code, title: k.text});
    if (m.suggestion) out.push({codes: [m.suggestion.code], label: `new code ${m.suggestion.code}`, title: m.suggestion.why});
    return out;
  }
  function lineCard(ln) {
    const [label, tone] = LINE[ln.status];
    const issues = ln.issues.map(i => `<li><span class="chip ${ISSUE[i.kind][1]}">${ISSUE[i.kind][0]}</span> ${esc(i.text)}</li>`).join("");
    const codes = ln.codes.length ? `<span class="mono">${ln.codes.map(esc).join(" + ")}</span>${ln.status === "ready" && ln.match.erp_text ? ` <span class="muted">“${esc(ln.match.erp_text)}”</span>` : ""}` : `<span class="muted">none yet</span>`;
    // Only undecided lines get a card (settled ones are rows in settledLines).
    const acts = `<div class="acts">${choices(ln).map(o => `<button class="btn" data-use="${esc(o.codes.join(","))}" data-line="${esc(ln.id)}" title="${esc(o.title || "")}">Use ${esc(o.label)}</button>`).join("")}
        <button class="btn" data-act="skip" data-line="${esc(ln.id)}">Leave this line out</button></div>
        <p class="small muted">Need more specification from the site? Leave it undecided: it goes in the reply below.</p>`;
    return `<div class="panel line"><div class="top"><b>${esc(ln.text)}</b><span class="chip ${tone}">${label}</span></div>
      <div class="small muted">${esc([qty(ln), ln.source, ln.category].filter(Boolean).join(" · "))}</div>
      <div class="small">Item code: ${codes}</div>
      ${issues ? `<ul style="list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:4px" class="small">${issues}</ul>` : ""}
      ${acts}</div>`;
  }
  // Lines with an item code (or left out): one compact row each, so the open ones stand out.
  function settledLines(c) {
    const done = c.lines.filter(l => l.status !== "to_reply");
    if (!done.length) return "";
    const rows = done.map(ln => { const [label, tone] = LINE[ln.status];
      const btn = ln.decision ? `<button class="btn" style="padding:2px 10px" data-act="clear" data-line="${esc(ln.id)}">Undo</button>`
        : `<button class="btn" style="padding:2px 10px" data-act="ask_site" data-line="${esc(ln.id)}" title="Put this line in the reply to the site">Ask the site</button>`;
      return `<tr><td><b>${esc(ln.text)}</b><div class="small muted">${esc([qty(ln), ln.category].filter(Boolean).join(" · "))}</div>
          ${ln.issues.map(i => `<div class="small"><span class="chip ${ISSUE[i.kind][1]}" style="padding:0 7px">${ISSUE[i.kind][0]}</span> ${esc(i.text)}</div>`).join("")}</td>
        <td><span class="mono">${ln.codes.map(esc).join(" + ") || "—"}</span>${ln.status === "ready" && ln.match.erp_text ? `<div class="small muted">“${esc(ln.match.erp_text)}”</div>` : ""}</td>
        <td><span class="chip ${tone}">${label}</span></td><td style="text-align:right">${btn}</td></tr>`; }).join("");
    return `<div class="panel"><div class="eyebrow">Settled · ${done.length} line${done.length === 1 ? "" : "s"}</div>
      <div class="tbl"><table class="settled"><thead><tr><th>Line</th><th>Item code</th><th>Status</th><th></th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
  }
  function mailForm(key, d, sendNote, {disabled, sent, label}) {
    const e = edits[key] || {};
    return `<label class="small">To <input class="txt" data-k="${key}" data-f="to" value="${esc(e.to ?? d.to)}" placeholder="email address (test inboxes only)"></label>
      <label class="small">Subject <input class="txt" data-k="${key}" data-f="subject" value="${esc(e.subject ?? d.subject)}"></label>
      <textarea data-k="${key}" data-f="body">${esc(e.body ?? d.body)}</textarea>
      <div class="acts"><button class="btn primary" data-send="${key}" ${disabled ? "disabled" : ""}>${sent ? "Send again" : label}</button>
        <button class="btn" data-copy="${key}">Copy</button><button class="btn" data-mark="${key}">Mark as sent</button></div>
      <p class="small muted" data-msg="${key}">${sendNote}</p>`;
  }
  function sendNote(s) {
    return !s.smtp ? "Sending isn't set up (GMAIL_ADDRESS / GMAIL_APP_PASSWORD in .env). Copy it, or mark it as sent."
      : !s.allowed_to.length ? "Sending is limited to test inboxes and REPLY_ALLOWED_TO is empty. Copy it, or mark it as sent."
      : `Sends only to test inboxes: ${s.allowed_to.map(esc).join(", ")}.`;
  }
  function secItemCodes(c) {
    const d = c.draft, s = c.send;
    const sent = c.replies.map(r => `<li>${esc(when(r.at))} · ${r.via === "smtp" ? "sent" : "marked as sent"} to ${esc(r.to)}</li>`).join("");
    const reply = d.needed ? `<div class="panel agent"><div class="eyebrow">Reply to the site: more specification or a fix for the item match</div>
        <p class="small muted">Drafted from the lines still undecided and the email problems. Edit before sending.</p>
        ${mailForm("reply", {...d, to: addr(d.to)}, sendNote(s), {disabled: !(s.smtp && s.allowed_to.length), sent: c.replies.length, label: "Send reply"})}
        ${sent ? `<div class="small"><b>Sent</b><ul style="margin:4px 0 0;padding-left:18px">${sent}</ul></div>` : ""}</div>`
      : sent ? `<div class="panel small"><b>Replies to the site</b><ul style="margin:4px 0 0;padding-left:18px">${sent}</ul></div>` : "";
    return `<section class="sec" id="s2"><div class="sh"><h3>2 · Item code mapping</h3><span class="small muted">${esc(c.headline)}</span></div>
      ${siteAnswers(c)}${c.stage === "waiting_on_site" ? "" : simBox(c)}
      ${c.lines.filter(l => l.status === "to_reply").map(lineCard).join("")}
      ${settledLines(c)}
      ${c.lines.length ? "" : `<div class="empty">No requirement lines were read from this email.</div>`}
      ${reply}${pasteBox(c)}</section>`;
  }
  // The site's latest answer, read into one suggestion per question. A person accepts each one.
  const SUGG = {use_code: ["Use", "good"], skip: ["Leave out", "warn"], resolved: ["Sorted", "good"], unclear: ["Unclear", ""]};
  function siteAnswers(c) {
    const all = c.site_replies || [];
    if (!all.length) return "";
    const i = all.length - 1, r = all[i];
    const lines = Object.fromEntries(c.lines.map(l => [l.id, l]));
    const checks = Object.fromEntries(c.email_checks.map(k => ["check:" + k.id, k]));
    const rows = r.suggestions.map(sg => {
      const ln = lines[sg.ref], ck = checks[sg.ref];
      const what = ln ? ln.text : ck ? ck.title : sg.ref;
      const open = ln ? ln.status === "to_reply" : ck ? !ck.resolved : false;
      const [label, tone] = SUGG[sg.action];
      const act = sg.action === "unclear" || !open ? ""
        : `<button class="btn primary" style="padding:2px 10px" data-accept="${esc(sg.ref)}" data-sa="${esc(sg.action)}" data-sc="${esc(sg.codes.join(","))}" data-sn="${esc(sg.note || "")}">Accept</button>`;
      return `<li><div class="top" style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap">
          <b>${esc(what)}</b><span><span class="chip ${tone}">${label}${sg.codes.length ? " " + sg.codes.map(esc).join(" + ") : ""}</span> ${open ? act : `<span class="chip">done</span>`}</span></div>
        ${sg.quote ? `<span class="q">“${esc(sg.quote)}”</span>` : ""}
        ${sg.note ? `<div class="small">${esc(sg.note)}</div>` : ""}
        ${sg.why_unclear ? `<div class="small muted">Not used: ${esc(sg.why_unclear)}</div>` : sg.action === "unclear" ? `<div class="small muted">The reply doesn't settle this: decide below or ask again.</div>` : ""}</li>`;
    }).join("");
    const status = r.read === "read" ? "" : `<p class="small muted">${esc(r.error || (r.read === "nothing_asked" ? "Nothing was open when it came in." : ""))}
        ${r.read === "needs_key" || r.read === "failed" ? `<button class="btn" style="padding:2px 8px;margin-left:6px" data-reread="${i}">Read again</button>` : ""}</p>`;
    return `<div class="panel ${c.site_replied ? "agent" : ""}"><div class="eyebrow">The site's answer${all.length > 1 ? ` (latest of ${all.length})` : ""}</div>
      <div class="small muted">${esc(when(r.at))} · ${r.via === "email" ? "by email" : r.via === "simulated" ? "simulated (demo)" : "pasted in"}${r.sender ? " · " + esc(r.sender) : ""}
        ${r.read === "read" ? `<button class="btn" style="padding:2px 8px;margin-left:6px" data-reread="${i}" title="Read it against the questions open now">Read again</button>` : ""}</div>
      <details><summary>The reply</summary><p class="body">${esc(r.body)}</p></details>
      ${rows ? `<p class="small muted">Suggested from the reply. Check each one; nothing changes until you accept.</p><ul class="sum" style="list-style:none;padding:0;gap:10px">${rows}</ul>` : ""}
      ${status}</div>`;
  }
  // Demo emails: once the reply has gone, the site's answer can be made to arrive.
  function simBox(c) {
    if (!c.demo || !c.replies.length || c.site_replied || !(c.counts.to_reply || c.counts.blocking)) return "";
    return `<div class="panel" style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap">
      <div><b>Waiting on the site.</b> <span class="small muted">Demo email: no real site inbox to answer it.</span></div>
      <button class="btn primary" data-simulate="1">Simulate the site's reply</button></div>`;
  }
  function pasteBox(c) {
    if (!(c.replies.length || c.draft.needed)) return "";
    return `<details class="panel"><summary>The site answered by phone or WhatsApp? Paste its answer</summary>
      <textarea data-paste placeholder="Paste the site's answer here" style="margin-top:8px"></textarea>
      <div class="acts"><button class="btn" data-pastesend="1">Add the site's answer</button></div>
      <p class="small muted">Answers by email are attached here on their own when the mailbox is synced.</p></details>`;
  }

  // ── 3 · approve ──
  function secApprove(c) {
    const can = c.stage === "approve";
    const rows = c.lines.map(ln => `<tr class="${ln.status === "skipped" || ln.status === "to_reply" ? "unm" : ""}"><td>${esc(ln.text)}</td>
      <td class="mono">${ln.codes.map(esc).join(" + ") || "—"}</td><td class="n">${esc(qty(ln)) || "—"}</td><td>${esc(ln.category || "—")}</td>
      <td>${ln.status === "skipped" ? "Left out" : ln.status === "to_reply" ? "Undecided" : "Goes to vendors"}</td></tr>`).join("");
    const why = c.approved_at ? "" : c.counts.blocking ? "A problem with the email stops the tender (step 1)." : c.counts.to_reply ? `${c.counts.to_reply} line(s) still need an item code (step 2).` : "";
    return `<section class="sec ${c.approved_at || can ? "" : "locked"}" id="s3"><div class="sh"><h3>3 · Approve the items</h3>
      ${c.approved_at ? `<span class="chip good">Approved ${esc(when(c.approved_at))}</span>` : ""}</div>
      <div class="panel"><div class="tbl"><table><thead><tr><th>Line</th><th>Item code</th><th class="n">Qty</th><th>Category</th><th>Decision</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="acts">${c.approved_at ? `<button class="btn" data-approve="0">Withdraw approval</button>` : `<button class="btn primary" data-approve="1" ${can ? "" : "disabled"}>Approve these items</button>`}</div>
      ${why ? `<p class="small muted">${esc(why)}</p>` : `<p class="small muted">Vendors are scored only on approved items. Changing an item later withdraws the approval and the vendor picks.</p>`}</div></section>`;
  }

  // ── 4 · vendors & RFQ ──
  function vendorsTable(v, c) {
    if (!v.ran) return `<p class="small muted">${esc(v.reason)}</p>`;
    return v.groups.map(g => `<div class="panel" data-cat="${esc(g.category)}">
      <div class="sh"><b>${esc(g.category)}</b><span class="small muted">item codes <span class="mono">${g.item_codes.map(esc).join(", ")}</span> · ${g.confirmed ? `<span class="chip good">picks confirmed</span>` : `${g.selected} pre-ticked`}</span></div>
      ${g.vendors.length ? `<div class="tbl"><table><thead><tr><th>RFQ</th><th class="n">#</th><th>Vendor</th><th class="n">Score</th><th class="n">Data</th><th>Why</th></tr></thead><tbody>
        ${g.vendors.map(x => `<tr><td><input type="checkbox" data-vid="${esc(x.vendor_id)}" ${x.picked ? "checked" : ""} aria-label="Send RFQ to ${esc(x.name)}"></td>
          <td class="n">${x.rank}${x.tied ? "=" : ""}</td><td>${esc(x.name)}${x.notes.length ? `<div class="small muted">${x.notes.map(esc).join(" ")}</div>` : ""}</td>
          <td class="n">${x.score == null ? "–" : x.score.toFixed(1)}</td><td class="n">${Math.round(x.coverage * 100)}%</td>
          <td><details><summary>${x.factors.filter(f => f.score != null).length} of ${x.factors.length} factors</summary>
            <ul class="why">${x.factors.map(f => `<li><b>${esc(f.label)}</b> ${f.score == null ? "(no data)" : `${Math.round(f.score * 100)}/100`}: ${esc(f.reason)}</li>`).join("")}</ul></details></td></tr>`).join("")}
        </tbody></table></div>` : `<p class="small">No vendors in the directory for ${esc(g.category)}. Add them in Admin › Vendors.</p>`}
      ${g.excluded.length ? `<p class="small muted">Excluded: ${g.excluded.map(e => `${esc(e.name)} (${esc(e.reason)})`).join("; ")}</p>` : ""}
      ${g.notes.map(n => `<p class="small muted">${esc(n)}</p>`).join("")}
      ${g.vendors.length ? `<div class="acts"><button class="btn primary" data-pick="${esc(g.category)}">${g.confirmed ? "Update picks" : "Confirm these vendors"}</button></div>` : ""}
    </div>`).join("") + v.notes.map(n => `<p class="small">${esc(n)}</p>`).join("");
  }
  function secVendors(c) {
    const open = !!c.approved_at;
    // Once the RFQs are drafted, the scoring folds away so the drafts come first.
    const picked = ORDER.indexOf(c.stage) >= ORDER.indexOf("rfq") ? [...new Set(c.rfq_drafts.map(d => d.vendor))].join(", ") : "";
    return `<section class="sec ${open ? "" : "locked"}" id="s4"><div class="sh"><h3>4 · Vendors & RFQ</h3>
      <span class="small muted">Pick vendors per category; each one's RFQ is drafted below.</span></div>
      ${picked ? `<details class="panel"><summary><b>Vendors picked:</b> ${esc(picked)} <span class="small muted">(open to change)</span></summary><div id="v-out" style="margin-top:8px"><div class="small muted">Scoring vendors…</div></div></details>`
        : `<h4 class="subh">Pick the vendors</h4><span class="small muted">Price vs median, past performance, lead time, response time, experience. Missing data is left out, not scored zero.</span>
      <div id="v-out">${open ? `<div class="small muted">Scoring vendors…</div>` : `<div class="panel small muted">Unlocks after the items are approved.</div>`}</div>`}
      ${open ? secRfq(c) : ""}</section>`;
  }

  // ── 4 (cont.) · the RFQ drafts, under the vendor picks ──
  // Which of the category's approved items go to this vendor. Locked once the RFQ is sent.
  function rfqItems(key, d) {
    const boxes = d.available.map(i => `<label title="${esc(i.text)}"><input type="checkbox" data-item="${esc(key)}" value="${esc(i.code)}"
      ${d.selected.includes(i.code) ? "checked" : ""} ${d.sent ? "disabled" : ""}> <span class="mono">${esc(i.code)}</span> <span class="muted">${esc(i.text)}</span></label>`).join("");
    return `<div class="small" style="display:flex;flex-wrap:wrap;gap:4px 14px"><span class="muted">Items on this RFQ:</span>${boxes}</div>
      ${d.attachment ? `<p class="small muted">Goes with the site's quantity sheet ${esc(d.attachment)} attached.</p>` : ""}`;
  }
  function secRfq(c) {
    const open = ORDER.indexOf(c.stage) >= ORDER.indexOf("rfq");
    const ds = c.rfq_drafts;
    const body = !open ? `<div class="panel small muted">The RFQ drafts appear here once you confirm the vendors above.</div>`
      : ds.map(d => { const key = "rfq:" + d.vendor_id + "|" + d.category;
        const inner = `${!d.to ? `<p class="small muted">No email address for this vendor in the directory.</p>` : ""}
          ${rfqItems(key, d)}
          ${mailForm(key, d, sendNote(c.send), {disabled: !(c.send.smtp && c.send.allowed_to.length), sent: d.sent, label: "Send RFQ"})}`;
        if (d.sent) return `<details class="panel"><summary><span class="chip good">✓ ${d.sent.via === "smtp" ? "Sent" : "Marked as sent"}</span><b>${esc(d.vendor)}</b>
          <span class="small muted">${esc(d.category)} · ${esc(when(d.sent.at))} · ${esc(d.sent.to)}</span></summary>${inner}</details>`;
        return `<div class="panel agent"><div class="sh"><b>${esc(d.vendor)}</b><span class="chip warn">Draft · ${esc(d.category)}</span></div>${inner}</div>`; }).join("");
    return `<h4 class="subh">RFQs</h4>${body}`;
  }

  // ── 5 · quotes, comparison, award ──
  const TERM = [["gst", "GST"], ["validity_days", "Validity"], ["lead_time_days", "Start"], ["warranty", "Warranty"], ["payment", "Payment"]];
  const termText = (k, v) => k === "validity_days" ? `${v} days` : k === "lead_time_days" ? `within ${v} days` : v;
  const upBtn = (g, vid, label) => `<label class="btn" style="padding:2px 10px">${label}<input type="file" accept="application/pdf,.pdf" data-up="${esc(vid)}" data-cat="${esc(g.category)}"></label>`;
  function quoteRow(g, q) {
    const Q = q.quote, terms = TERM.map(([k, l]) => { const t = (Q.terms || []).find(x => x.key === k); return t ? `${l}: ${esc(termText(k, t.value))}` : null; }).filter(Boolean);
    const src = q.source === "upload" ? `<span class="chip acc">uploaded ${esc(when(q.at).slice(0, 10))}</span>` : `<span class="chip">from the tender folder</span>`;
    const read = Q.needs_vision ? `<span class="chip bad">needs a person to read the rates</span>` : `<span class="chip ${Q.read_confidence === "high" ? "good" : "warn"}">read · ${esc(Q.read_confidence)} confidence</span>`;
    const canTick = !Q.needs_vision;
    return `<tr><td><input type="checkbox" data-cmpv="${esc(q.vendor_id)}" ${q.selected ? "checked" : ""} ${canTick ? "" : "disabled"} aria-label="Compare ${esc(q.vendor)}"></td>
      <td><b>${esc(q.vendor)}</b><div class="small muted">${esc(q.file)}${Q.date ? " · dated " + esc(Q.date) : ""}</div></td><td>${src}</td><td>${read}
      ${Q.notes.length ? `<details><summary>${Q.notes.length} note${Q.notes.length === 1 ? "" : "s"}</summary><ul class="why">${Q.notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul></details>` : ""}</td>
      <td class="small">${terms.join("<br>") || `<span class="muted">no terms read</span>`}</td>
      <td><div class="acts">${upBtn(g, q.vendor_id, q.source === "upload" ? "Replace PDF" : "Upload PDF")}
        ${q.source === "pack" ? `<button class="btn" style="padding:2px 10px" data-link="0" data-vid="${esc(q.vendor_id)}" data-cat="${esc(g.category)}">Don't use</button>` : ""}</div></td></tr>`;
  }
  function ratesForm(g, q) {
    const Q = q.quote, mine = {};
    for (const l of Q.lines) mine[l.sl_no] = l;
    const keyOf = t => t.code || "#" + t.sl_no;
    const rows = g.basis.lines.map(t => { const l = mine[t.sl_no], v = q.manual_rates[keyOf(t)];
      return `<tr><td class="mono">${esc(t.code || t.sl_no)}</td><td>${esc(t.description)}</td><td class="n">${l ? `${num(l.qty)} ${esc(l.unit_as_written)}` : "–"}</td>
        <td class="n"><input class="rate" type="number" min="0" step="0.01" data-rate="${esc(keyOf(t))}" value="${v == null ? "" : v}" placeholder="per ${esc(l ? l.unit_as_written : t.unit)}"></td></tr>`; }).join("");
    return `<details ${Q.needs_vision ? "open" : ""} data-rv="${esc(q.vendor_id)}"><summary>${esc(q.vendor)}: type in the rates from the PDF${Object.keys(q.manual_rates).length ? " (saved)" : ""}</summary>
      <div class="tbl" style="margin-top:6px"><table><thead><tr><th>Code</th><th>Item</th><th class="n">Qty read</th><th class="n">Rate (₹, excl. GST)</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="acts" style="margin-top:6px"><button class="btn" data-rates="${esc(q.vendor_id)}" data-cat="${esc(g.category)}">Save rates</button></div></details>`;
  }
  function compareTable(g) {
    const cmp = g.comparison;
    if (!cmp) return `<p class="small muted">Tick at least one readable quote to compare.</p>`;
    const vs = cmp.vendors, ids = cmp.vendor_ids;
    const head = `<tr><th>Code</th><th>Item</th><th class="n">Qty</th><th>Unit</th>${vs.map(v => `<th class="n">${esc(v.vendor)}<br><span class="small muted" style="text-transform:none">rate · amount</span></th>`).join("")}</tr>`;
    const rows = cmp.tender.map(t => {
      const low = cmp.lowest[String(t.sl_no)] || [];
      return `<tr class="${t.measured ? "" : "unm"}"><td class="mono">${esc(t.code || t.sl_no)}</td><td>${esc(t.description)}${t.measured ? "" : `<div class="small">${esc(t.note || "not measured")}: not comparable</div>`}</td>
        <td class="n">${num(t.qty)}</td><td>${esc((t.unit || "").toUpperCase())}</td>
        ${vs.map((v, i) => { const l = v.lines.find(x => x.sl_no === t.sl_no);
          const fl = l.flags.filter(f => !f.startsWith("Not measured"));
          return `<td class="n ${low.includes(ids[i]) ? "l1" : l.comparable ? "" : "nc"}" title="${esc(fl.join(" "))}">${l.rate == null ? "–" : num(l.rate_per_tender_unit ?? l.rate)}${fl.length ? ` <span class="chip ${l.comparable ? "warn" : "bad"}" style="padding:0 6px">!</span>` : ""}
            <br><span class="muted">${l.comparable ? inr(l.repriced) : l.as_quoted != null ? `as quoted ${inr(l.as_quoted)}` : "–"}</span></td>`; }).join("")}</tr>`; }).join("");
    const tot = (label, f, cls = "") => `<tr class="${cls}"><td></td><td colspan="3">${label}</td>${vs.map(v => `<td class="n">${f(v)}</td>`).join("")}</tr>`;
    return `<div class="tbl"><table><thead>${head}</thead><tbody>${rows}
      ${tot("Like for like, same quantities (excl. GST)", v => inr(v.repriced_total) + (v.complete ? "" : " *"), "gt")}
      ${tot("Rank like for like", v => v.rank_repriced ? `#${v.rank_repriced}${v.pct_above_lowest ? ` <span class="muted">+${v.pct_above_lowest}%</span>` : ""}` : "–")}
      ${tot("As quoted (own PDF total)", v => inr(v.as_quoted_total))}
      ${tot("Rank as quoted", v => v.rank_as_quoted ? `#${v.rank_as_quoted}` : "–")}
      ${tot("Quantity basis", v => esc(v.quantity_basis === "tender" ? "same as basis" : v.quantity_basis === "different" ? "different set" : "–"))}
    </tbody></table></div>
    <p class="small muted">Green: lowest on that line. Amounts are the vendor's rate × the basis quantity. ! = a note on that cell (hover). * = not every line priced, so not ranked.</p>`;
  }
  function awardBox(g) {
    const r = g.recommended, a = g.award;
    if (a) return `<div class="panel"><div class="sh"><b>Chosen: ${esc(a.vendor)}</b><span class="chip good">${a.override ? "Chosen over the recommendation" : "Recommended vendor"} · ${esc(when(a.at))}</span></div>
      <p class="small">${esc(a.reason)}</p><div class="acts"><button class="btn" data-unaward="${esc(g.category)}">Change the vendor</button></div></div>`;
    if (!g.comparison) return "";
    const opts = g.comparison.vendor_ids.map((id, i) => `<option value="${esc(id)}" ${r && r.vendor_id === id ? "selected" : ""}>${esc(g.comparison.vendors[i].vendor)}${r && r.vendor_id === id ? " (recommended)" : ""}</option>`).join("");
    return `<div class="panel agent"><div class="eyebrow">Recommendation</div>
      ${r ? `<p><b>${esc(r.vendor)}</b>. ${esc(r.reason)}</p>` : `<p class="small">No vendor can be recommended from these quotes. You can still choose one, with a reason.</p>`}
      <div class="acts" style="align-items:center"><select data-award-v>${opts}</select>
        <input class="txt" style="flex:1;min-width:220px;width:auto" data-award-r placeholder="Reason (needed if you choose another vendor)">
        <button class="btn primary" data-award="${esc(g.category)}">Choose this vendor</button></div></div>`;
  }
  function secQuotes(c) {
    const Q = c.quotes;
    if (!Q.open) return `<section class="sec locked" id="s6"><div class="sh"><h3>5 · Compare quotes</h3></div>
      <div class="panel small muted">Unlocks once an RFQ is sent (or marked as sent).</div></section>`;
    const body = Q.categories.map(g => {
      const waiting = g.waiting.map(w => { const off = g.offers.find(o => o.vendor_id === w.vendor_id);
        return `<tr class="unm"><td></td><td><b>${esc(w.vendor)}</b></td><td><span class="chip">no quote yet</span></td>
          <td colspan="2" class="small">${off ? `A quote from this vendor is in the tender folder (${esc(off.file)}), priced on a different tender.` : ""}</td>
          <td><div class="acts">${upBtn(g, w.vendor_id, "Upload PDF")}${off ? `<button class="btn" style="padding:2px 10px" data-link="1" data-vid="${esc(w.vendor_id)}" data-cat="${esc(g.category)}">Use the tender-folder quote</button>` : ""}</div></td></tr>`; }).join("");
      const manual = g.quotes.filter(q => q.quote.needs_vision || Object.keys(q.manual_rates).length);
      return `<div class="panel" data-qcat="${esc(g.category)}">
        <div class="sh"><b>${esc(g.category)}</b><span class="small muted">${g.quotes.length} of ${g.quotes.length + g.waiting.length} quotes in · basis: ${g.basis.kind === "tender" ? "the tender BOQ in the tender folder" : "this requirement's quantities"}</span></div>
        <div class="tbl"><table><thead><tr><th>Compare</th><th>Vendor</th><th>Source</th><th>Read</th><th>Terms</th><th></th></tr></thead><tbody>${g.quotes.map(q => quoteRow(g, q)).join("")}${waiting}</tbody></table></div>
        ${g.quotes.length ? "" : `<p class="small muted">No quotes in yet. Upload each vendor's PDF on its row as it comes in.</p>`}
        ${manual.map(q => ratesForm(g, q)).join("")}
        ${g.quotes.length ? `<div class="acts"><button class="btn" data-cmp="${esc(g.category)}">Compare the ticked quotes</button></div>` : ""}
        ${g.quotes.length ? compareTable(g) : ""}
        ${g.summary.length ? `<div class="panel"><div class="eyebrow">Summary</div><ul class="sum">${g.summary.map(x => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
        ${awardBox(g)}</div>`; }).join("");
    return `<section class="sec" id="s6"><div class="sh"><h3>5 · Compare quotes</h3>
      <span class="small muted">Every rate on the same quantities, so totals compare like for like.</span></div>${body || `<div class="panel small muted">No categories with picked vendors.</div>`}</section>`;
  }

  // ── 6 · HOD email ──
  function secHod(c) {
    const d = c.quotes.hod_draft, h = c.hod;
    if (!d) return `<section class="sec locked" id="s7"><div class="sh"><h3>6 · HOD approval</h3></div>
      <div class="panel small muted">Unlocks after you choose the vendor in step 5.</div></section>`;
    const status = !h ? "" : `<div class="panel"><div class="sh"><b>${h.approved_by_hod_at ? "Approved by the HOD" : "Sent to HOD, awaiting approval"}</b>
        <span class="chip ${h.approved_by_hod_at ? "good" : "warn"}">${h.via === "smtp" ? "Sent" : "Marked as sent"} ${esc(when(h.at))} to ${esc(h.to)}${h.approved_by_hod_at ? ` · approved ${esc(when(h.approved_by_hod_at))}` : ""}</span></div>
      <div class="acts">${h.approved_by_hod_at ? `<button class="btn" data-hodok="0">Undo approval</button>` : `<button class="btn primary" data-hodok="1">Record HOD approval</button>`}</div></div>`;
    return `<section class="sec" id="s7"><div class="sh"><h3>6 · HOD approval</h3>
      <span class="small muted">For the HOD to approve or send back.</span></div>${status}
      ${h ? `<details class="panel"><summary><b>The email to the HOD</b> <span class="small muted">open to read or send again</span></summary>` : `<div class="panel agent"><div class="eyebrow">Email to the HOD</div>`}
        ${!d.to && !(edits.hod && edits.hod.to) ? `<p class="small muted">No HOD address set (HOD_EMAIL in .env). Type it in, or copy the email.</p>` : ""}
        ${mailForm("hod", d, sendNote(c.send), {disabled: !(c.send.smtp && c.send.allowed_to.length), sent: h, label: "Send to HOD"})}${h ? "</details>" : "</div>"}</section>`;
  }

  // ── drawing: one requirement as a full page, its steps as tabs ──
  const STEP_FOR = {item_codes: "s2", waiting_on_site: "s2", approve: "s3", vendors: "s4", rfq: "s4", rfq_sent: "s4", quotes: "s6", hod: "s7", done: "s7"};
  const SECTIONS = {s1: secRequirement, s2: secItemCodes, s3: secApprove, s4: secVendors, s5: secVendors, s6: secQuotes, s7: secHod,
    thread: secThread, history: secHistory};

  // ── Email thread and activity history ──
  const PARTY = {site: "Site", vendor: "Vendor", hod: "HOD"};
  const VIA = {smtp: "sent", marked: "marked as sent", simulated: "simulated", pasted: "pasted in", email: "by email", upload: "uploaded"};
  const firstLine = t => (t.split("\n").find(l => l.trim()) || "Show").slice(0, 90);
  function secThread(c) {
    const ms = c.thread.map(m => `<div class="mail ${m.direction}${m.superseded ? " old" : ""}">
      <div class="mh"><b>${esc(m.label)}</b><span class="muted">${esc(when(m.at) || "")}${m.via ? " · " + esc(VIA[m.via] || m.via) : ""}${m.superseded ? " · superseded" : ""}</span></div>
      <div class="small muted">${m.direction === "in" ? "From " + esc(m.from) : "To " + esc(m.to || PARTY[m.party])}${m.subject ? " · " + esc(m.subject) : ""}</div>
      ${m.body ? `<details><summary class="small">${esc(firstLine(m.body))}</summary><pre>${esc(m.body)}</pre></details>` : ""}
      ${m.note ? `<div class="small muted">Attached: ${esc(m.note)}</div>` : ""}</div>`).join("");
    return `<section class="sec"><div class="sh"><h3>Email thread</h3><span class="small muted">${c.thread.length} email${c.thread.length === 1 ? "" : "s"} on this requirement · ours on the right</span></div>
      <div class="thread">${ms}</div></section>`;
  }
  const WHO = {person: "you", agent: "agent", site: "site", vendor: "vendor", system: "system"};
  function secHistory(c) {
    const rows = c.history.slice().reverse().map(e => `<li><span class="mono small muted">${esc(when(e.at) || "")}</span>
      <span class="who ${esc(e.actor)}">${esc(WHO[e.actor] || e.actor)}</span><span>${esc(e.text)}</span></li>`).join("");
    return `<section class="sec"><div class="sh"><h3>History</h3><span class="small muted">Every action on this requirement, newest first</span></div>
      <ul class="hist">${rows}</ul></section>`;
  }
  // The card on top of every requirement: what needs doing now, one main button for it, and the agent.
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  function nextStep(c) {
    const home = STEP_FOR[c.stage], here = active === home, go = n => `<button class="btn primary" data-jump="${home}">Go to step ${n} →</button>`;
    const unsent = c.rfq_drafts.filter(d => !d.sent).length, can = c.send.smtp && c.send.allowed_to.length;
    const rec = (c.quotes.categories || []).map(g => !g.award && g.recommended && g.recommended.vendor).filter(Boolean);
    switch (c.stage) {
      case "item_codes": return c.counts.to_reply
        ? {tone: "warn", text: `${plural(c.counts.to_reply, "line")} need${c.counts.to_reply === 1 ? "s" : ""} an item code`, sub: "Pick a code for each one, or leave it undecided and send the reply to ask the site.", btn: here ? "" : go(2)}
        : {tone: "warn", text: "The email has a problem that stops the tender", sub: "Mark it sorted in step 1, or ask the site in the reply.", btn: here ? "" : go(2)};
      case "waiting_on_site": return {tone: "", text: "Waiting for the site's answer", sub: "Their answer is read into suggestions here when it comes in.",
        btn: c.demo ? `<button class="btn primary" data-simulate="1">Simulate the site's reply</button>` : here ? "" : go(2)};
      case "approve": return {tone: "warn", text: "Every line has an item code", sub: "Approve the items so vendors can be scored and picked.", btn: `<button class="btn primary" data-approve="1">Approve the items</button>`};
      case "vendors": return {tone: "warn", text: "Pick the vendors", sub: "Top-scored vendors are pre-ticked. Confirm them for each category.", btn: here ? "" : go(4)};
      case "rfq": return {tone: "warn", text: `${plural(unsent, "RFQ")} ready to send`, sub: "Each goes with its own To, subject and text below.",
        btn: here ? `<button class="btn primary" data-sendall="send" ${can ? "" : "disabled"} title="${can ? "" : esc(sendNote(c.send))}">Send ${unsent > 1 ? "all " + unsent : "it"}</button>
          <button class="btn" data-sendall="mark">Mark ${unsent > 1 ? "all " : ""}as sent</button><span class="small muted" id="sendall-msg"></span>` : go(4)};
      case "rfq_sent": { const n = c.rfq_drafts.length - c.quotes_in;
        return {tone: "", text: `Waiting for ${plural(Math.max(n, 0), "quote")}`, sub: "Upload each vendor's PDF in step 5 as it comes in.", btn: active === "s6" ? "" : `<button class="btn primary" data-jump="s6">Go to step 5 →</button>`}; }
      case "quotes": return {tone: "warn", text: rec.length ? `${rec.join(", ")} recommended` : "Choose the vendor", sub: "Check the comparison and confirm the vendor.", btn: here ? "" : go(5)};
      case "hod": return c.hod
        ? {tone: "", text: "Waiting for the HOD's approval", sub: `Sent ${when(c.hod.at)} to ${c.hod.to}.`, btn: `<button class="btn primary" data-hodok="1">Record HOD approval</button>`}
        : {tone: "warn", text: "The email to the HOD is ready", sub: "Review it and send it.", btn: here
          ? (can ? `<button class="btn primary" data-send="hod">Send to HOD</button>` : `<button class="btn primary" data-mark="hod">Mark as sent</button>`) : go(6)};
      case "done": return {tone: "good", text: "The HOD approved", sub: "Nothing left to do on this requirement.", btn: ""};
    }
    return {tone: "", text: STAGE[c.stage] ? STAGE[c.stage][0] : c.stage, sub: "", btn: ""};
  }
  const STOP = {needs_you: "stopped: needs you", ready_for_hod: "finished its part", done: "finished", error: "stopped on an error"};
  function agentBar(c) {
    const a = c.agent;
    const done = a && a.steps.map(s => `<li>${esc(s.text)}</li>`).join("");
    if (a && a.running) return `<div class="next running"><div class="what"><b><span class="spin" style="color:var(--accent)"></span> The agent is working</b>
      <span class="small muted">${a.steps.length ? `Step ${a.steps.length}: ${esc(a.steps[a.steps.length - 1].text)}` : "Starting: reading the requirement…"}</span></div>
      ${done ? `<details open><summary class="small">So far: ${plural(a.steps.length, "step")}</summary><ol>${done}</ol></details>` : ""}</div>`;
    const n = nextStep(c);
    const canAgent = c.stage !== "done" && !(a && a.stopped === "ready_for_hod");
    const agentBtn = canAgent ? `<button class="btn" data-agent="1" title="Takes it as far as it can; stops where you're needed">${a ? "Run the agent again" : "Let the agent do it"}</button>` : "";
    const last = a && a.reason && (a.stopped === "needs_you" || a.stopped === "error")
      ? `<span class="small" style="color:var(--${a.stopped === "error" ? "bad" : "warn"})">Agent ${STOP[a.stopped]}: ${esc(a.reason)}</span>` : "";
    return `<div class="next ${n.tone}"><div class="what"><b>${esc(n.text)}</b><span class="small muted">${esc(n.sub)}</span>${last}</div>
      ${n.btn}${agentBtn}
      ${done ? `<details><summary class="small">What the agent did: ${plural(a.steps.length, "step")}, ${esc(when(a.at))}</summary><ol>${done}</ol></details>` : ""}</div>`;
  }
  let active = null, pollTimer = null, listTimer = null;

  function fit() {
    const page = root && root.querySelector(".wf");
    if (!page || root.hidden) return;
    page.style.height = Math.max(360, window.innerHeight - page.getBoundingClientRect().top - 16) + "px";
  }
  function drawCase(c) {
    // Moved on: show the next step. Not from RFQs to quotes: step 4 offers "Next: compare quotes".
    if (current && current.uid === c.uid && current.stage !== c.stage && !(current.stage === "rfq" && (c.stage === "quotes" || c.stage === "rfq_sent"))) active = STEP_FOR[c.stage];
    current = c;
    if (!active) active = STEP_FOR[c.stage];
    if (active === "s5") active = "s4";  // RFQs used to be their own step (older agent runs point there)
    root.innerHTML = `<div class="wf">
      <div class="head"><button class="btn" data-back="1">← All requirements</button>
        <div style="min-width:0;flex:1"><h2>${esc(c.subject || "(no subject)")}</h2><div class="small muted">${esc([c.project, c.sender, c.date].filter(Boolean).join(" · "))}</div></div>
        <div class="views"><button class="btn ${active === "thread" ? "on" : ""}" data-jump="thread">Email thread (${c.thread.length})</button>
          <button class="btn ${active === "history" ? "on" : ""}" data-jump="history">History</button></div></div>
      <div class="stepper" role="tablist">${steps(c).map(s => `<button role="tab" aria-selected="${s.id === active}" class="${s.state}${s.id === active ? " on" : ""}" data-jump="${s.id}"><span class="small">STEP ${s.n}</span><b>${esc(s.title)}</b><span class="small">${esc(s.note)}</span></button>`).join("")}</div>
      ${agentBar(c)}
      <div class="pane">${SECTIONS[active](c)}</div></div>`;
    fit();
    if (active === "s4" && c.approved_at) loadVendors();
    schedulePoll(c);
  }
  // While the agent works: fetch the case every 1.5 s and redraw. If the person is typing in a field,
  // only the agent bar is updated, so nothing they're writing is lost.
  function schedulePoll(c) {
    clearTimeout(pollTimer);
    if (!(c.agent && c.agent.running)) return;
    pollTimer = setTimeout(async () => {
      if (!current || current.uid !== c.uid || !document.body.contains(root)) return;
      let n;
      try { n = await getJSON(`/api/cases/${c.uid}`); } catch (e) { return schedulePoll(c); }
      if (!current || current.uid !== n.uid) return;
      const typing = root.contains(document.activeElement) && /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
      if (!typing) return drawCase(n);
      const bar = root.querySelector(".next");
      if (bar) bar.outerHTML = agentBar(n);
      schedulePoll(n);
    }, 1500);
  }
  async function loadVendors() {
    const out = root.querySelector("#v-out");
    if (!out) return;
    try { vendorData = await getJSON(`/api/cases/${current.uid}/vendors`); out.innerHTML = vendorsTable(vendorData, current); }
    catch (e) { out.innerHTML = `<div class="err">${esc(e.message)}</div>`; }
  }
  async function open(uid) {
    for (const k of Object.keys(edits)) delete edits[k];
    if (!current || current.uid !== uid) { current = null; active = null; }
    root.innerHTML = `<div class="empty">Loading…</div>`;
    try { drawCase(await getJSON(`/api/cases/${encodeURIComponent(uid)}`)); }
    catch (e) { root.innerHTML = `<div class="err">${esc(e.message)}</div><button class="btn" data-back="1">← All requirements</button>`; }
  }
  function flash(btn, msg) {
    root.querySelectorAll(".wf-err").forEach(e => e.remove());
    const anchor = btn && root.contains(btn) ? (btn.closest(".acts") || btn) : null;
    if (!anchor) return window.toast ? window.toast(msg) : alert(msg);
    anchor.insertAdjacentHTML("afterend", `<div class="err wf-err" role="alert">${esc(msg)}</div>`);
    anchor.nextElementSibling.scrollIntoView({block: "nearest", behavior: "smooth"});
  }
  async function act(btn, fn, keep) {
    btn.disabled = true;
    try {
      const c = await fn();
      if (keep) delete edits[keep];
      const pane = root.querySelector(".pane"), y = pane ? pane.scrollTop : 0;
      const before = active;
      drawCase(c);
      const p2 = root.querySelector(".pane");
      if (p2 && active === before) p2.scrollTop = y;
    } catch (e) {
      btn.disabled = false;
      if (btn.dataset && btn.dataset.agent) {  // the run may have started anyway: reload and keep following it
        try { drawCase(await getJSON(`/api/cases/${current.uid}`)); } catch { /* shown below */ }
        btn = root.querySelector("[data-agent]") || null;
      }
      flash(btn, e.message);
    }
  }
  const mail = key => { const g = f => root.querySelector(`[data-k="${CSS.escape(key)}"][data-f="${f}"]`).value; return {to: g("to"), subject: g("subject"), body: g("body")}; };
  const mailUrl = key => key === "reply" ? `/api/cases/${current.uid}/reply` : key === "hod" ? `/api/cases/${current.uid}/hod` : `/api/cases/${current.uid}/rfq/${encodeURIComponent(key.slice(4).split("|")[0])}?category=${encodeURIComponent(key.slice(4).split("|").slice(1).join("|"))}`;

  function onClick(e) {
    const b = e.target.closest("button");
    if (!b || !root.contains(b)) return;
    if (b.dataset.back) { location.hash = "#overview"; return; }
    if (!current) return;
    const base = `/api/cases/${current.uid}`;
    if (b.dataset.jump) { active = b.dataset.jump; return drawCase(current); }
    if (b.dataset.use) return act(b, () => post(`${base}/lines/${encodeURIComponent(b.dataset.line)}`, {action: "use_code", codes: b.dataset.use.split(",")}));
    if (b.dataset.act) return act(b, () => post(`${base}/lines/${encodeURIComponent(b.dataset.line)}`, {action: b.dataset.act}));
    if (b.dataset.accept) {
      const ref = b.dataset.accept, a = b.dataset.sa;
      if (a === "resolved") return act(b, () => post(`${base}/checks/${encodeURIComponent(ref.slice(6))}`, {resolved: true}));
      return act(b, () => post(`${base}/lines/${encodeURIComponent(ref)}`, {action: a, codes: b.dataset.sc ? b.dataset.sc.split(",") : [], note: b.dataset.sn || null}));
    }
    if (b.dataset.agent) { b.textContent = "Starting the agent…"; return act(b, () => post(`${base}/agent`, {})); }
    if (b.dataset.simulate) { b.textContent = "Reading the site's reply…"; return act(b, () => post(`${base}/site-reply/simulate`, {})); }
    if (b.dataset.reread) return act(b, () => post(`${base}/site-reply/${b.dataset.reread}/read`, {}));
    if (b.dataset.pastesend) {
      const t = root.querySelector("textarea[data-paste]").value;
      if (!t.trim()) return flash(b, "Paste the site's answer first.");
      return act(b, () => post(`${base}/site-reply`, {body: t}));
    }
    if (b.dataset.check) return act(b, () => post(`${base}/checks/${encodeURIComponent(b.dataset.check)}`, {resolved: b.dataset.resolved === "1"}));
    if (b.dataset.approve) return act(b, () => post(`${base}/approve`, {approved: b.dataset.approve === "1"}));
    if (b.dataset.pick) {
      const box = b.closest("[data-cat]");
      const ids = [...box.querySelectorAll("input[data-vid]:checked")].map(i => i.dataset.vid);
      if (!ids.length) return flash(b, "Tick at least one vendor.");
      return act(b, () => post(`${base}/vendors`, {category: b.dataset.pick, vendor_ids: ids}));
    }
    if (b.dataset.sendall) return sendAll(b, b.dataset.sendall === "mark");
    const cat = b.dataset.cat;
    if (b.dataset.link) return act(b, () => post(`${base}/quotes/${encodeURIComponent(b.dataset.vid)}/link?category=${encodeURIComponent(cat)}`, {use: b.dataset.link === "1"}));
    if (b.dataset.rates) {
      const rates = {};
      for (const i of b.closest("details").querySelectorAll("input[data-rate]")) rates[i.dataset.rate] = i.value === "" ? null : Number(i.value);
      return act(b, () => post(`${base}/quotes/${encodeURIComponent(b.dataset.rates)}/rates?category=${encodeURIComponent(cat)}`, {rates}));
    }
    if (b.dataset.cmp) {
      const ids = [...b.closest("[data-qcat]").querySelectorAll("input[data-cmpv]:checked")].map(i => i.dataset.cmpv);
      if (!ids.length) return flash(b, "Tick at least one quote.");
      return act(b, () => post(`${base}/compare`, {category: b.dataset.cmp, vendor_ids: ids}));
    }
    if (b.dataset.award) {
      const box = b.closest("[data-qcat]");
      return act(b, () => post(`${base}/award`, {category: b.dataset.award, vendor_id: box.querySelector("[data-award-v]").value, reason: box.querySelector("[data-award-r]").value}));
    }
    if (b.dataset.unaward) return act(b, () => post(`${base}/award`, {category: b.dataset.unaward, vendor_id: null}));
    if (b.dataset.hodok) return act(b, () => post(`${base}/hod/approve`, {approved: b.dataset.hodok === "1"}));
    if (b.dataset.send || b.dataset.mark) {
      const key = b.dataset.send || b.dataset.mark;
      return act(b, () => post(mailUrl(key), {...mail(key), mark_only: !!b.dataset.mark}), key);
    }
    if (b.dataset.copy) {
      const m = mail(b.dataset.copy);
      navigator.clipboard.writeText(`To: ${m.to}\nSubject: ${m.subject}\n\n${m.body}`);
      root.querySelector(`[data-msg="${CSS.escape(b.dataset.copy)}"]`).textContent = "Copied.";
    }
  }
  // Send (or mark) every unsent RFQ one by one, each with its own form values. Stops on nothing:
  // a vendor that fails (no address, not a test inbox) is listed and the rest still go.
  async function sendAll(btn, markOnly) {
    const keys = current.rfq_drafts.filter(d => !d.sent).map(d => "rfq:" + d.vendor_id + "|" + d.category);
    const msg = root.querySelector("#sendall-msg") || {};
    btn.disabled = true;
    const failed = [];
    let last = null;
    for (const [i, key] of keys.entries()) {
      msg.textContent = `${markOnly ? "Marking" : "Sending"} ${i + 1} of ${keys.length}…`;
      try { last = await post(mailUrl(key), {...mail(key), mark_only: markOnly}); delete edits[key]; }
      catch (e) { failed.push(`${current.rfq_drafts.find(d => "rfq:" + d.vendor_id + "|" + d.category === key).vendor}: ${e.message}`); }
    }
    if (last) drawCase(last);
    if (failed.length) flash(root.querySelector("[data-sendall]") || btn, `Not sent: ${failed.join(" · ")}`);
  }
  // Ticking an RFQ item saves the selection and redraws that draft's text (a hand edit to the text is replaced).
  function onChange(e) {
    const t = e.target;
    if (t.dataset && t.dataset.up && current && t.files.length) {
      const fd = new FormData();
      fd.append("file", t.files[0]);
      return act(t, () => getJSON(`/api/cases/${current.uid}/quotes/${encodeURIComponent(t.dataset.up)}?category=${encodeURIComponent(t.dataset.cat)}`, {method: "POST", body: fd}));
    }
    if (!t.dataset || !t.dataset.item || !current) return;
    const key = t.dataset.item, [vid, ...cat] = key.slice(4).split("|");
    const codes = [...root.querySelectorAll(`input[data-item="${CSS.escape(key)}"]:checked`)].map(i => i.value);
    if (!codes.length) { t.checked = true; return flash(t, "An RFQ needs at least one item."); }
    if (edits[key]) delete edits[key].body;
    return act(t, () => post(`/api/cases/${current.uid}/rfq/${encodeURIComponent(vid)}/items?category=${encodeURIComponent(cat.join("|"))}`, {codes}));
  }
  function onInput(e) {
    const t = e.target;
    if (t.dataset && t.dataset.k) (edits[t.dataset.k] ||= {})[t.dataset.f] = t.value;
  }

  // ── Overview: one row per requirement email, its progress through the steps ──
  // Six dots, one per step: green done · amber needs you · dashed waiting on others · grey not yet. Hover for detail.
  const DOT = ["Requirement", "Item codes", "Approve", "Vendors & RFQ", "Quotes", "HOD"];
  function track(x) {
    const at = ORDER.indexOf(x.stage), c = x.counts;
    const settled = c.ready + c.decided, lines = settled + c.to_reply + c.skipped;
    const st = [
      ["done", "read from the email"],
      at >= ORDER.indexOf("approve") ? ["done", `${settled}/${lines} coded${x.replies ? `, ${x.replies} repl${x.replies === 1 ? "y" : "ies"} to the site` : ""}`]
        : x.stage === "waiting_on_site" ? ["wait", "waiting on the site's answer"] : ["now", c.to_reply ? `${c.to_reply} to decide` : "email problem"],
      x.approved ? ["done", "approved"] : x.stage === "approve" ? ["now", "to approve"] : ["", "not yet"],
      at >= ORDER.indexOf("rfq_sent") ? ["done", `${x.picked} picked, ${x.rfqs_sent} RFQs sent`] : x.stage === "rfq" ? ["now", `${x.rfqs_sent}/${x.picked} RFQs sent`]
        : x.stage === "vendors" ? ["now", "vendors to pick"] : ["", "not yet"],
      x.awarded.length ? ["done", `${x.awarded.join(", ")} chosen`] : x.stage === "quotes" ? ["now", `${x.quotes_in} in, vendor to choose`]
        : x.stage === "rfq_sent" ? ["wait", `waiting for quotes (${x.quotes_in} in)`] : ["", "not yet"],
      x.stage === "done" ? ["done", "approved"] : x.stage === "hod" ? (x.hod_sent ? ["wait", "waiting on the HOD"] : ["now", "email to send"]) : ["", "not yet"],
    ];
    return `<ol class="trk" aria-label="Progress">${st.map(([k, t], i) => `<li class="${k}" title="${i + 1} · ${DOT[i]}: ${esc(t)}"><span>${i + 1}</span></li>`).join("")}</ol>`;
  }
  // What it's on now, in words. Amber when it needs a person, grey when it waits on someone else.
  function statusOf(x) {
    const c = x.counts, n = Math.max(x.picked - x.rfqs_sent, 0);
    return {
      item_codes: [c.to_reply ? `${c.to_reply} item code${c.to_reply === 1 ? "" : "s"} to decide` : "Email problem to sort", "warn"],
      waiting_on_site: ["Waiting on the site", ""],
      approve: ["Items to approve", "warn"],
      vendors: ["Vendors to pick", "warn"],
      rfq: [n ? `${n} RFQ${n === 1 ? "" : "s"} to send` : "RFQs to send", "warn"],
      rfq_sent: [`Waiting for quotes · ${x.quotes_in} in`, ""],
      quotes: ["Vendor to choose", "warn"],
      hod: x.hod_sent ? ["Waiting on the HOD", ""] : ["HOD email to send", "warn"],
      done: ["HOD approved", "good"],
    }[x.stage] || [x.stage, ""];
  }
  const recv = x => (x.date || "").replace(/^\w+, /, "").slice(0, 11);
  // Run the agent on one requirement, from the overview (not once it's finished or already working).
  function runCell(x) {
    const a = x.agent;
    if (a && a.running) return `<span class="small" style="color:var(--accent)">Working…</span>`;
    if (x.stage === "done" || (a && a.stopped === "ready_for_hod")) return `<span class="small muted" title="${esc((a && a.reason) || "")}">${a ? "✓ Done its part" : "—"}</span>`;
    return `<button class="btn" data-runone="${esc(x.uid)}" title="Take this requirement as far as the agent can; it stops where you're needed">${a ? "Run again" : "Run agent"}</button>`;
  }
  // The line on top: what needs a person now (each part filters the table), then what's waiting on others.
  let ovFilter = null;  // {key, uids}
  const NEEDS = [
    ["hod", x => x.stage === "hod" && !x.hod_sent, n => `${n} HOD email${n === 1 ? "" : "s"} to send`],
    ["quotes", x => x.stage === "quotes", n => `${n} to choose a vendor`],
    ["rfq", x => x.stage === "rfq", n => `${n} with RFQs to send`],
    ["vendors", x => x.stage === "vendors", n => `${n} to pick vendors`],
    ["approve", x => x.stage === "approve", n => `${n} to approve`],
    ["problem", x => x.stage === "item_codes" && x.counts.blocking, (n, xs) => n === 1 ? `1 with an email problem (${short(xs[0].subject)})` : `${n} with an email problem`],
    ["codes", x => x.stage === "item_codes" && !x.counts.blocking, n => `${n} with item codes to decide`],
    ["error", x => x.stage === "error", n => `${n} that couldn't be opened`],
  ];
  const WAITING = [
    [x => x.stage === "waiting_on_site", n => `${n} on the site's answer`],
    [x => x.stage === "rfq_sent", n => `${n} on vendors' quotes`],
    [x => x.stage === "hod" && x.hod_sent, n => `${n} on the HOD`],
  ];
  const short = t => { t = (t || "").replace(/^Requirement:\s*/i, "").split(",")[0]; return t.length > 28 ? t.slice(0, 27) + "…" : t; };
  const canRun = x => !(x.agent && x.agent.running) && x.stage !== "done" && x.stage !== "error" && !(x.agent && x.agent.stopped === "ready_for_hod");
  function needsLine(items) {
    const parts = NEEDS.map(([key, f, label]) => { const xs = items.filter(f); return xs.length ? {key, uids: xs.map(x => x.uid), text: label(xs.length, xs)} : null; }).filter(Boolean);
    const untried = items.filter(x => canRun(x) && !x.agent);
    if (untried.length) parts.push({key: "agent", uids: untried.map(x => x.uid), text: `agent not run yet on ${untried.length}`});
    if (ovFilter && !parts.some(p => p.key === ovFilter.key)) ovFilter = null;  // that group emptied
    else if (ovFilter) ovFilter = parts.find(p => p.key === ovFilter.key);
    const wait = WAITING.map(([f, label]) => { const n = items.filter(f).length; return n ? label(n) : null; }).filter(Boolean);
    const waitTxt = wait.length ? `<span class="w">Waiting: ${wait.join(" · ")}</span>` : "";
    if (!parts.length) return `<div class="needs"><b>Nothing needs you.</b>${waitTxt}</div>`;
    return `<div class="needs has"><b>Needs you:</b>${parts.map(p => `<button data-need="${p.key}" aria-pressed="${ovFilter && ovFilter.key === p.key}" title="Show only these">${esc(p.text)}</button>`).join("")}
      ${ovFilter ? `<button data-need="" title="Show every requirement">Show all</button>` : ""}${waitTxt}</div>`;
  }
  window.mountOverview = async function (el, quiet) {
    style();
    clearTimeout(listTimer);
    if (!quiet) el.innerHTML = `<div class="empty">Loading…</div>`;
    try {
      const d = await getJSON("/api/cases");
      // While the agent works on any requirement, refresh the list every 2 s (quietly) to show it move.
      if (d.items && d.items.some(x => x.agent && x.agent.running))
        listTimer = setTimeout(() => { if (!el.hidden && document.body.contains(el) && el.offsetParent !== null) window.mountOverview(el, true); }, 2000);
      if (!d.ready) { el.innerHTML = `<div class="err">${esc(d.message)}</div>`; return; }
      const items = d.items;
      const needs = needsLine(items), shown = ovFilter ? items.filter(x => ovFilter.uids.includes(x.uid)) : items;
      el.innerHTML = needs + `<section class="panel"><div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
          <div class="eyebrow">${ovFilter ? `${shown.length} of ${items.length}` : items.length} requirement emails · newest first</div>
          <div class="acts"><details class="simmenu"><summary class="btn">Simulate a new email ▾</summary><div class="menu">
            <button data-arrive="auto"><b>Agent runs it end to end</b><span>Sends, the site's answer and the quotes are simulated. Stops at the HOD email.</span></button>
            <button data-arrive="checkpoints"><b>Agent runs it, asks you twice</b><span>Stops for you to approve the items and to choose the vendor, then carries on.</span></button>
            <button data-arrive="manual"><b>You work it yourself</b><span>The agent doesn't start. Opens the new requirement.</span></button></div></details>
          <button class="btn primary" data-runall="1" title="Takes every unfinished requirement as far as it can; stops where you're needed">Run agent on all</button></div></div>
        <div class="tbl ovt"><table><thead><tr><th>Requirement</th><th>Progress</th><th>Status</th><th>Agent</th></tr></thead><tbody>
        ${shown.map(x => {
          if (x.stage === "error") return `<tr><td><b>${esc(x.subject || "Email " + x.uid)}</b><div class="small muted">${esc(recv(x))}</div></td><td></td><td colspan="2"><span class="chip bad">Can't open</span> <span class="small muted">${esc(x.headline)}</span></td></tr>`;
          const [txt, tone] = statusOf(x), a = x.agent;
          const sub = a && a.running ? `<div class="small" style="color:var(--accent)" title="${esc(a.last || "")}"><span class="spin"></span> Agent working…</div>`
            : x.awarded.length && x.stage !== "quotes" ? `<div class="small muted">${esc(x.awarded.join(", "))} chosen</div>`
            : a && a.stopped === "needs_you" ? `<div class="small muted" title="${esc(a.reason || "")}">Agent stopped here</div>` : "";
          return `<tr class="row" tabindex="0" data-case="${esc(x.uid)}">
            <td><b>${esc(x.subject)}</b><div class="small muted">${esc([x.project, recv(x)].filter(Boolean).join(" · "))}</div></td>
            <td>${track(x)}</td><td><span class="chip ${tone}">${esc(txt)}</span>${sub}</td><td>${runCell(x)}</td></tr>`; }).join("")}
        </tbody></table></div>
        <div class="ovl"><span>Steps: 1 requirement · 2 item codes · 3 approve · 4 vendors & RFQ · 5 quotes · 6 HOD</span>
          <span><ol class="trk" style="display:inline-flex;vertical-align:-2px"><li class="done"><span></span></li></ol> done</span>
          <span><ol class="trk" style="display:inline-flex;vertical-align:-2px"><li class="now"><span></span></li></ol> needs you</span>
          <span><ol class="trk" style="display:inline-flex;vertical-align:-2px"><li class="wait"><span></span></li></ol> waiting on others</span></div></section>`;
      if (!el.dataset.wired) {
        el.dataset.wired = "1";
        el.addEventListener("click", e => {
          const sm = el.querySelector(".simmenu[open]");
          if (sm && !sm.contains(e.target)) sm.open = false;  // a click elsewhere closes the menu
          const b = e.target.closest("button[data-runall],button[data-arrive],button[data-runone]");
          if (b) {
            b.disabled = true;
            const mode = b.dataset.arrive;
            const url = b.dataset.runone ? `/api/cases/${encodeURIComponent(b.dataset.runone)}/agent` : mode ? `/api/cases/arrive?mode=${encodeURIComponent(mode)}` : "/api/cases/agent/run-all";
            post(url, {}).then(r => mode === "manual" ? window.openCase(r.uid) : window.mountOverview(el))
              .catch(err => { b.disabled = false; window.toast ? window.toast(err.message) : alert(err.message); });
            return;
          }
          const nb = e.target.closest("button[data-need]");
          if (nb) { const k = nb.dataset.need; ovFilter = k && !(ovFilter && ovFilter.key === k) ? {key: k, uids: []} : null; return window.mountOverview(el, true); }
          const r = e.target.closest("tr[data-case]"); if (r) window.openCase(r.dataset.case);
        });
        el.addEventListener("keydown", e => { if (e.key === "Enter" && e.target.matches("tr[data-case]")) window.openCase(e.target.dataset.case); });
      }
    } catch (e) { el.innerHTML = `<div class="err">${esc(e.message)}</div>`; }
  };

  // A requirement page: #cases/<uid>. The overview opens it; "All requirements" goes back.
  window.openCase = function (uid) { location.hash = "#cases/" + uid; };

  window.mountCases = function (el, uid) {
    if (root !== el) {
      root = el;
      style();
      el.addEventListener("click", onClick);
      el.addEventListener("input", onInput);
      el.addEventListener("change", onChange);
      window.addEventListener("resize", fit);
    }
    uid = uid || window.PENDING_CASE;
    window.PENDING_CASE = null;
    if (uid) open(uid); else location.hash = "#overview";
  };
})();
