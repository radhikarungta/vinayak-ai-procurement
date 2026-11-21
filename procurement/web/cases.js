// Requirements workflow, one email at a time:
//   1 what was asked → 2 item codes (reply to site for conflicts / partial / no match) → 3 approve items
//   → 4 vendor scoring and picks → 5 RFQ drafts → 6 compare quotes, choose the vendor → 7 email the HOD.
// Plain script, no build step. Usage: load /web/cases.js, then call window.mountCases(el).
// Uses the page's CSS (.panel .eyebrow .chip .btn .tbl .muted .small .mono .err .empty .mail .checks .fields .q .body).
(function () {
  "use strict";
  const ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"};
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ESC[c]);
  const STAGE = {
    item_codes: ["Item codes", "warn"],
    waiting_on_site: ["Waiting on site", ""],
    approve: ["To approve", "acc"],
    vendors: ["Pick vendors", "acc"],
    rfq: ["Send RFQs", "acc"],
    rfq_sent: ["Waiting for quotes", ""],
    quotes: ["Compare quotes", "acc"],
    hod: ["With HOD", "acc"],
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
.wf .stepper{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:6px}
.wf .stepper button.on{outline:2px solid var(--accent);outline-offset:-1px}
.wf .stepper button{all:unset;cursor:pointer;display:flex;flex-direction:column;gap:2px;padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:var(--surface);font-size:.8rem;min-width:0}
.wf .stepper button b{font:700 .85rem var(--display);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wf .stepper button.done{border-color:var(--good)} .wf .stepper button.done span{color:var(--good)}
.wf .stepper button.now{border-color:var(--accent);background:var(--accent-soft)} .wf .stepper button.now span{color:var(--accent)}
.wf .stepper button.locked{opacity:.6}
.wf .stepper button:focus-visible{outline:2px solid var(--accent)}
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
.kpis.k6{grid-template-columns:repeat(6,minmax(0,1fr))}
@media (max-width:900px){.wf .stepper{grid-template-columns:repeat(4,minmax(0,1fr))}.kpis.k6{grid-template-columns:repeat(2,minmax(0,1fr))}}`;
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
        note: itemsDone ? `${c.counts.ready + c.counts.decided} matched` : c.stage === "waiting_on_site" ? "Waiting on site" : `${c.counts.to_reply} to decide`},
      {id: "s3", n: 3, title: "Approve items", state: c.approved_at ? "done" : c.stage === "approve" ? "now" : "locked",
        note: c.approved_at ? "Approved" : c.stage === "approve" ? "Ready to approve" : "After item codes"},
      {id: "s4", n: 4, title: "Vendors", state: at > ORDER.indexOf("vendors") ? "done" : c.stage === "vendors" ? "now" : "locked",
        note: at > ORDER.indexOf("vendors") ? `${Object.values(c.vendor_picks).flat().length} picked` : c.stage === "vendors" ? "Score and pick" : "After approval"},
      {id: "s5", n: 5, title: "Draft RFQ", state: at >= ORDER.indexOf("rfq_sent") ? "done" : c.stage === "rfq" ? "now" : "locked",
        note: at >= ORDER.indexOf("rfq_sent") ? "All sent" : c.stage === "rfq" ? `${c.rfq_drafts.filter(d => !d.sent).length} to send` : "After vendors"},
      {id: "s6", n: 6, title: "Compare quotes", state: aw.length ? "done" : c.quotes.open ? "now" : "locked",
        note: aw.length ? `${aw.join(", ")} chosen` : rec.length ? `${rec.join(", ")} recommended` : c.quotes_in ? `${c.quotes_in} quote${c.quotes_in === 1 ? "" : "s"}` : c.quotes.open ? "Waiting for quotes" : "After RFQs"},
      {id: "s7", n: 7, title: "HOD approval", state: c.stage === "done" ? "done" : c.stage === "hod" ? "now" : "locked",
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
    let acts = "";
    if (ln.status === "to_reply") {
      acts = `<div class="acts">${choices(ln).map(o => `<button class="btn" data-use="${esc(o.codes.join(","))}" data-line="${esc(ln.id)}" title="${esc(o.title || "")}">Use ${esc(o.label)}</button>`).join("")}
        <button class="btn" data-act="skip" data-line="${esc(ln.id)}">Leave this line out</button></div>
        <p class="small muted">Need more specification from the site? Leave it undecided: it goes in the reply below.</p>`;
    } else if (ln.decision) {
      acts = `<div class="acts"><button class="btn" data-act="clear" data-line="${esc(ln.id)}">Undo</button></div>`;
    } else {
      acts = `<div class="acts"><button class="btn" data-act="ask_site" data-line="${esc(ln.id)}">Ask the site about this line</button></div>`;
    }
    return `<div class="panel line"><div class="top"><b>${esc(ln.text)}</b><span class="chip ${tone}">${label}</span></div>
      <div class="small muted">${esc([qty(ln), ln.source, ln.category].filter(Boolean).join(" · "))}</div>
      <div class="small">Item code: ${codes}</div>
      ${issues ? `<ul style="list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:4px" class="small">${issues}</ul>` : ""}
      ${acts}</div>`;
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
      ${c.lines.map(lineCard).join("") || `<div class="empty">No requirement lines were read from this email.</div>`}
      ${reply}</section>`;
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

  // ── 4 · vendors ──
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
    return `<section class="sec ${open ? "" : "locked"}" id="s4"><div class="sh"><h3>4 · Vendor scoring</h3>
      <span class="small muted">Price vs median, past performance, lead time, response time, experience. Missing data is left out, not scored zero.</span></div>
      <div id="v-out">${open ? `<div class="small muted">Scoring vendors…</div>` : `<div class="panel small muted">Unlocks after the items are approved.</div>`}</div></section>`;
  }

  // ── 5 · RFQ ──
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
    const body = !open ? `<div class="panel small muted">Unlocks after you confirm the vendors.</div>`
      : ds.map(d => { const key = "rfq:" + d.vendor_id + "|" + d.category;
        return `<div class="panel ${d.sent ? "" : "agent"}"><div class="sh"><b>${esc(d.vendor)}</b>
          ${d.sent ? `<span class="chip good">${d.sent.via === "smtp" ? "Sent" : "Marked as sent"} ${esc(when(d.sent.at))} to ${esc(d.sent.to)}</span>` : `<span class="chip warn">Draft</span>`}</div>
          ${!d.to ? `<p class="small muted">No email address for this vendor in the directory.</p>` : ""}
          ${rfqItems(key, d)}
          ${mailForm(key, d, sendNote(c.send), {disabled: !(c.send.smtp && c.send.allowed_to.length), sent: d.sent, label: "Send RFQ"})}</div>`; }).join("");
    const unsent = open ? ds.filter(d => !d.sent).length : 0;
    const can = c.send.smtp && c.send.allowed_to.length;
    const all = unsent > 1 ? `<div class="panel" style="flex-direction:row;align-items:center;flex-wrap:wrap;gap:10px">
      <span class="small"><b>${unsent} RFQs not sent yet.</b> Each goes with its own To, subject and text below.</span>
      <button class="btn primary" data-sendall="send" ${can ? "" : "disabled"}>Send all ${unsent}</button>
      <button class="btn" data-sendall="mark">Mark all as sent</button><span class="small muted" id="sendall-msg"></span></div>` : "";
    const next = open && !unsent && c.quotes.open ? `<div class="panel" style="flex-direction:row;align-items:center;flex-wrap:wrap;gap:10px">
      <span class="small"><b>All RFQs sent.</b> ${c.quotes_in ? `${c.quotes_in} quote${c.quotes_in === 1 ? "" : "s"} in.` : "Upload the quotes as they come in."}</span>
      <button class="btn primary" data-jump="s6">Next: compare quotes →</button></div>` : "";
    return `<section class="sec ${open ? "" : "locked"}" id="s5"><div class="sh"><h3>5 · Draft RFQ</h3>
      <span class="small muted">One per picked vendor, with the approved items and the six standard terms.</span></div>${next}${all}${body}</section>`;
  }

  // ── 6 · quotes, comparison, award ──
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
      <p class="small">${esc(a.reason)}</p><div class="acts"><button class="btn primary" data-jump="s7">Next: email the HOD →</button>
      <button class="btn" data-unaward="${esc(g.category)}">Change the vendor</button></div></div>`;
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
    if (!Q.open) return `<section class="sec locked" id="s6"><div class="sh"><h3>6 · Compare quotes</h3></div>
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
    return `<section class="sec" id="s6"><div class="sh"><h3>6 · Compare quotes</h3>
      <span class="small muted">Every rate on the same quantities, so totals compare like for like. Plain arithmetic, no AI.</span></div>${body || `<div class="panel small muted">No categories with picked vendors.</div>`}</section>`;
  }

  // ── 7 · HOD email ──
  function secHod(c) {
    const d = c.quotes.hod_draft, h = c.hod;
    if (!d) return `<section class="sec locked" id="s7"><div class="sh"><h3>7 · HOD approval</h3></div>
      <div class="panel small muted">Unlocks after you choose the vendor in step 6.</div></section>`;
    const status = !h ? "" : `<div class="panel"><div class="sh"><b>${h.approved_by_hod_at ? "Approved by the HOD" : "Sent to HOD, awaiting approval"}</b>
        <span class="chip ${h.approved_by_hod_at ? "good" : "warn"}">${h.via === "smtp" ? "Sent" : "Marked as sent"} ${esc(when(h.at))} to ${esc(h.to)}${h.approved_by_hod_at ? ` · approved ${esc(when(h.approved_by_hod_at))}` : ""}</span></div>
      <div class="acts">${h.approved_by_hod_at ? `<button class="btn" data-hodok="0">Undo approval</button>` : `<button class="btn primary" data-hodok="1">Record HOD approval</button>`}</div></div>`;
    return `<section class="sec" id="s7"><div class="sh"><h3>7 · HOD approval</h3>
      <span class="small muted">The recommendation, the comparison and the caveats, for the HOD to approve or send back.</span></div>${status}
      <div class="panel ${h ? "" : "agent"}"><div class="eyebrow">Email to the HOD</div>
        ${!d.to && !(edits.hod && edits.hod.to) ? `<p class="small muted">No HOD address set (HOD_EMAIL in .env). Type it in, or copy the email.</p>` : ""}
        ${mailForm("hod", d, sendNote(c.send), {disabled: !(c.send.smtp && c.send.allowed_to.length), sent: h, label: "Send to HOD"})}</div></section>`;
  }

  // ── drawing: one requirement as a full page, its steps as tabs ──
  const STEP_FOR = {item_codes: "s2", waiting_on_site: "s2", approve: "s3", vendors: "s4", rfq: "s5", rfq_sent: "s5", quotes: "s6", hod: "s7", done: "s7"};
  const SECTIONS = {s1: secRequirement, s2: secItemCodes, s3: secApprove, s4: secVendors, s5: secRfq, s6: secQuotes, s7: secHod};
  let active = null;

  function fit() {
    const page = root && root.querySelector(".wf");
    if (!page || root.hidden) return;
    page.style.height = Math.max(360, window.innerHeight - page.getBoundingClientRect().top - 16) + "px";
  }
  function drawCase(c) {
    // Moved on: show the next step. Not from RFQs to quotes: step 5 offers "Next: compare quotes".
    if (current && current.uid === c.uid && current.stage !== c.stage && !(current.stage === "rfq" && c.stage === "quotes")) active = STEP_FOR[c.stage];
    current = c;
    if (!active) active = STEP_FOR[c.stage];
    const [label, tone] = STAGE[c.stage];
    root.innerHTML = `<div class="wf">
      <div class="head"><button class="btn" data-back="1">← All requirements</button>
        <div style="min-width:0;flex:1"><h2>${esc(c.subject || "(no subject)")}</h2><div class="small muted">${esc([c.project, c.sender, c.date].filter(Boolean).join(" · "))}</div></div>
        <span class="chip ${tone}">${label}</span></div>
      <div class="stepper" role="tablist">${steps(c).map(s => `<button role="tab" aria-selected="${s.id === active}" class="${s.state}${s.id === active ? " on" : ""}" data-jump="${s.id}"><span class="small">STEP ${s.n}</span><b>${esc(s.title)}</b><span class="small">${esc(s.note)}</span></button>`).join("")}</div>
      <div class="pane">${SECTIONS[active](c)}</div></div>`;
    fit();
    if (active === "s4" && c.approved_at) loadVendors();
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
    const host = btn.closest(".panel") || root.querySelector(".pane") || root;
    host.querySelector(".wf-err")?.remove();
    host.insertAdjacentHTML("beforeend", `<div class="err wf-err">${esc(msg)}</div>`);
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
    } catch (e) { btn.disabled = false; flash(btn, e.message); }
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
    const msg = root.querySelector("#sendall-msg");
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
  function track(x) {
    const at = ORDER.indexOf(x.stage), c = x.counts;
    const cell = (state, text) => `<td><span class="chip ${state === "done" ? "good" : state === "now" ? "warn" : ""}">${esc(text)}</span></td>`;
    const settled = c.ready + c.decided, lines = settled + c.to_reply + c.skipped;
    const codes = at >= ORDER.indexOf("approve") ? cell("done", `${settled}/${lines} coded`)
      : cell("now", x.stage === "waiting_on_site" ? "Waiting on site" : `${c.to_reply} to decide${c.blocking ? ", email problem" : ""}`);
    const reply = x.replies ? cell("done", `${x.replies} sent`) : (c.to_reply || c.blocking) ? cell("now", "Not sent") : cell("", "Not needed");
    const appr = x.approved ? cell("done", "Approved") : x.stage === "approve" ? cell("now", "To approve") : cell("", "—");
    const vend = x.picked ? cell("done", `${x.picked} picked`) : x.stage === "vendors" ? cell("now", "To pick") : cell("", "—");
    const rfq = at >= ORDER.indexOf("rfq_sent") ? cell("done", `${x.rfqs_sent} sent`) : x.stage === "rfq" ? cell("now", `${x.rfqs_sent}/${x.picked} sent`) : cell("", "—");
    const quotes = x.awarded.length ? cell("done", `${x.awarded.join(", ")} chosen`) : x.quotes_in ? cell("now", `${x.quotes_in} in`)
      : x.stage === "rfq_sent" ? cell("now", "Waiting") : cell("", "—");
    const hod = x.stage === "done" ? cell("done", "Approved") : x.stage === "hod" ? cell("now", x.hod_sent ? "Awaiting HOD" : "To send") : cell("", "—");
    return codes + reply + appr + vend + rfq + quotes + hod;
  }
  window.mountOverview = async function (el) {
    style();
    el.innerHTML = `<div class="empty">Loading…</div>`;
    try {
      const d = await getJSON("/api/cases");
      if (!d.ready) { el.innerHTML = `<div class="err">${esc(d.message)}</div>`; return; }
      const items = d.items, n = s => items.filter(x => s.includes(x.stage)).length;
      const kpi = [["Requirement emails", items.length, ""], ["On item codes", n(["item_codes", "waiting_on_site"]), "clar"],
        ["To approve", n(["approve"]), "clar"], ["Vendors / RFQ", n(["vendors", "rfq"]), ""],
        ["Quotes / HOD", n(["rfq_sent", "quotes", "hod"]), "clar"], ["HOD approved", n(["done"]), "clear"]];
      el.innerHTML = `<div class="kpis k6">${kpi.map(([l, v, t]) => `<div class="kpi ${t}"><span class="v">${v}</span><span class="l">${l}</span></div>`).join("")}</div>
        <section class="panel"><div class="eyebrow">Requirement emails · newest first · click a row to work on it</div>
        <div class="tbl"><table><thead><tr><th>Received</th><th>Requirement</th><th>Now</th><th>2 · Item codes</th><th>Reply to site</th><th>3 · Approve</th><th>4 · Vendors</th><th>5 · RFQ</th><th>6 · Quotes</th><th>7 · HOD</th></tr></thead><tbody>
        ${items.map(x => { const [l, t] = STAGE[x.stage] || [x.stage, ""];
          if (x.stage === "error") return `<tr><td class="mono" style="white-space:nowrap">${esc((x.date || "").replace(/^\w+, /, "").slice(0, 11))}</td>
            <td><b>${esc(x.subject || "Email " + x.uid)}</b></td><td><span class="chip ${t}">${l}</span></td><td colspan="7" class="small muted">${esc(x.headline)}</td></tr>`;
          return `<tr class="row" tabindex="0" data-case="${esc(x.uid)}"><td class="mono" style="white-space:nowrap">${esc((x.date || "").replace(/^\w+, /, "").slice(0, 11))}</td>
            <td><b>${esc(x.subject)}</b><div class="small muted">${esc(x.project || "")}</div></td><td><span class="chip ${t}">${l}</span></td>${track(x)}</tr>`; }).join("")}
        </tbody></table></div></section>`;
      if (!el.dataset.wired) {
        el.dataset.wired = "1";
        el.addEventListener("click", e => { const r = e.target.closest("tr[data-case]"); if (r) window.openCase(r.dataset.case); });
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
