// Step 2 · Match item codes. Plain script, no build step.
// Usage: load /web/itemcodes.js, then call window.mountItemCodes(el).
// Uses the page's CSS classes (.panel .eyebrow .chip .btn .tbl .muted .small .mono .err .empty .grid2 ul.plain).
(function () {
  const ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"};
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ESC[c]);
  const KIND = {
    exact: ["Matches", "good"],
    partial: ["Partly matches", "warn"],
    none: ["No code yet", "bad"],
  };
  const DUP = {
    same_code_different_text: ["Same code, different text", "bad", "One code means two things. A PO with this code is unclear."],
    near_duplicate: ["Different codes, near-identical text", "warn", "Likely the same item listed under more than one code."],
    repeated: ["Same row twice", "", "The same code and text appear more than once."],
  };
  const ISSUE = {
    same_code_different_text: ["Two meanings", "bad"],
    near_duplicate: ["Likely duplicate", "warn"],
    repeated: ["Listed twice", ""],
    misplaced: ["Looks shifted", "warn"],
  };

  async function getJSON(url, opts) {
    const r = await fetch(url, opts);
    let body = null;
    try { body = await r.json(); } catch { /* not JSON */ }
    if (!r.ok) throw new Error((body && body.detail) || `Request failed (${r.status}).`);
    return body;
  }

  const sl = (sls, rows) => rows.map((r, i) => (sls && sls[i] ? `Sl ${sls[i]}` : `row ${r}`)).join(", ");
  const qty = l => (l.quantity == null ? "" : `${Number(l.quantity).toLocaleString("en-IN", {maximumFractionDigits: 2})} ${l.uom || ""}`.trim());

  function lineRow(m) {
    const [label, tone] = KIND[m.kind];
    const flags = (m.flags || []).map(f => `<li><span class="chip ${esc(f.tone || "")}">${f.tone === "bad" ? "Check" : f.tone === "good" ? "OK" : "Note"}</span> ${esc(f.text)}</li>`).join("");
    const sugg = m.suggestion ? `<p class="small"><b>Suggested new code:</b> <span class="mono">${esc(m.suggestion.code)}</span>. ${esc(m.suggestion.why)}</p>` : "";
    const also = m.also && m.also.length ? `<p class="small"><b>Also use:</b> <span class="mono">${m.also.map(esc).join(", ")}</span></p>` : "";
    const others = (m.candidates || []).filter(c => c.code !== m.code);
    const cands = others.length ? `<details class="small"><summary class="muted">Other options (${others.length})</summary><ul class="plain">${others.map(c =>
      `<li><span class="mono">${esc(c.code)}</span> ${esc(c.text)} <span class="muted">· shares ${esc(c.common.map(w => w.toLowerCase()).join(", ") || "only common words")}</span></li>`).join("")}</ul></details>` : "";
    const src = [m.line.source, m.line.site_code ? `site code ${m.line.site_code}` : "", qty(m.line)].filter(Boolean).join(" · ");
    return `<tr>
      <td><div>${esc(m.line.text)}</div><div class="small muted">${esc(src)}</div>${m.line.detail ? `<div class="small muted">${esc(m.line.detail)}</div>` : ""}</td>
      <td class="mono">${m.code ? esc(m.code) : `<span class="muted">none</span>`}</td>
      <td><span class="chip ${tone}">${label}</span>${m.decided_by === "claude" ? `<div class="small muted" style="margin-top:4px">by Claude</div>` : ""}</td>
      <td>${m.erp_text ? esc(m.erp_text) : `<span class="muted">No ERP item</span>`}${m.erp_rows && m.erp_rows.length ? `<div class="small muted">sheet row ${m.erp_rows.map(esc).join(", ")}</div>` : ""}</td>
      <td style="min-width:260px"><p>${esc(m.reason)}</p>${also}${sugg}${flags ? `<ul class="plain small" style="list-style:none;padding:0;margin-top:6px">${flags}</ul>` : ""}${cands}</td>
    </tr>`;
  }

  function matchesView(res) {
    if (res.ready === false) return `<div class="err">${esc(res.message)}</div>`;
    if (!res.lines.length) return `<div class="empty">This email has no lines to match.</div>`;
    const mode = res.mode === "claude"
      ? `<span class="chip acc">Chosen by Claude${res.model ? " · " + esc(res.model) : ""}</span>`
      : `<span class="chip">Matched by rules</span>`;
    return `<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center">${mode}<span class="small muted">${esc(res.message || "")}</span></div>
      <p class="small muted">${res.from_sheet ? "Lines come from the attached quantity sheet." : "No quantity sheet, so lines come from the work type and scope in the email."}</p>
      <div class="tbl"><table><thead><tr><th>Requirement line</th><th>Code</th><th>Match</th><th>ERP text</th><th>Why</th></tr></thead>
      <tbody>${res.lines.map(lineRow).join("")}</tbody></table></div>`;
  }

  function dupView(d) {
    if (d.ready === false) return `<div class="err">${esc(d.message)}</div>`;
    const counts = Object.entries(DUP).map(([k, [label, tone]]) => `<span class="chip ${tone}">${esc(label)}: ${esc(d.counts[k])}</span>`).join(" ");
    const notes = (d.notes || []).map(n => `<li>${esc(n)}</li>`).join("");
    const misplaced = (d.misplaced || []).map(m =>
      `<li><span class="mono">${esc(m.code)}</span> (Sl ${esc(m.sl_no)}) reads “${esc(m.text)}”, like ${esc(m.reads_like.join(", "))}. Its base code ${esc(m.base_code)} means “${esc(m.base_text)}”.</li>`).join("");
    const sections = Object.entries(DUP).map(([kind, [label, tone, what]]) => {
      const gs = d.groups.filter(g => g.kind === kind);
      if (!gs.length) return "";
      return `<details ${kind === "same_code_different_text" ? "open" : ""}><summary><b>${esc(label)}</b> <span class="chip ${tone}">${gs.length}</span></summary>
        <p class="small muted" style="margin:6px 0">${esc(what)}</p>
        <ul class="plain small">${gs.map(g => `<li>${esc(g.note)}${kind === "repeated" ? "" : `<ul class="plain" style="margin-top:4px">${g.members.map(mm =>
          `<li><span class="mono">${esc(mm.code)}</span> ${esc(mm.text)} <span class="muted">(${esc(sl(mm.sl_nos, mm.rows))})</span></li>`).join("")}</ul>`}</li>`).join("")}</ul></details>`;
    }).join("");
    return `<div style="display:flex;flex-wrap:wrap;gap:6px">${counts}</div>
      ${notes ? `<div><h3>Shifted rows</h3><ul class="plain small">${notes}</ul></div>` : ""}
      ${misplaced ? `<details><summary><b>Codes that don't match their base code</b> <span class="chip warn">${d.misplaced.length}</span></summary><ul class="plain small" style="margin-top:6px">${misplaced}</ul></details>` : ""}
      ${sections}`;
  }

  function masterRows(entries, q) {
    q = q.trim().toLowerCase();
    const hit = entries.filter(e => !q || [e.code, e.description, e.normalised, e.sl_no].some(v => String(v || "").toLowerCase().includes(q)));
    if (!hit.length) return `<tr><td colspan="4" class="muted">No item matches “${esc(q)}”.</td></tr>`;
    return hit.map(e => `<tr>
      <td class="mono">${esc(e.sl_no || "")}</td>
      <td class="mono">${esc(e.code)}</td>
      <td>${esc(e.title)}${e.has_spec ? ` <span class="small muted" title="${esc(e.description)}">+ full spec</span>` : ""}</td>
      <td>${(e.issues || []).map(i => `<span class="chip ${ISSUE[i] ? ISSUE[i][1] : ""}">${esc(ISSUE[i] ? ISSUE[i][0] : i)}</span>`).join(" ")}</td></tr>`).join("");
  }

  window.mountItemCodes = async function (el) {
    el.innerHTML = `<div class="empty">Loading item codes…</div>`;
    let inbox, dup, master;
    try {
      [inbox, dup, master] = await Promise.all([getJSON("/api/demo/inbox"), getJSON("/api/itemcodes/duplicates"), getJSON("/api/itemcodes/master")]);
    } catch (e) {
      el.innerHTML = `<div class="err">${esc(e.message)}</div>`;
      return;
    }
    inbox = [...inbox].sort((a, b) => new Date(b.received) - new Date(a.received));
    const ready = master.ready !== false;

    el.innerHTML = `<div style="display:flex;flex-direction:column;gap:18px">
      <div class="panel">
        <div class="eyebrow">Step 2 · Match item codes</div>
        <h2>Which ERP code is each line?</h2>
        <p class="muted small">Each line of a requirement is compared with the ERP item code list. You see the code, how well it fits and why. Where nothing fits, a new code is suggested. You make the final call.</p>
        ${ready ? "" : `<div class="err">${esc(master.message)}</div>`}
        <div class="small muted">Pick an email</div>
        <div data-ic="emails" style="display:flex;flex-wrap:wrap;gap:6px">${inbox.map(r => `<button class="btn" data-uid="${esc(r.uid)}">${esc(r.result.email.subject || "Email " + r.uid)}</button>`).join("")}</div>
        <div data-ic="matches">${inbox.length ? "" : `<div class="empty">No demo emails yet.</div>`}</div>
      </div>
      <div class="grid2">
        <div class="panel"><div class="eyebrow">ERP list problems</div><h2>Duplicate and conflicting codes</h2>${dupView(dup)}</div>
        <div class="panel"><div class="eyebrow">ERP item code list</div>
          <h2>Search the list</h2>
          ${ready ? `<p class="small muted">${esc(master.rows)} rows, ${esc(master.codes)} codes, from <span class="mono">${esc(String(master.path).split("/").pop())}</span> (sheet “${esc(master.sheet)}”).</p>
          <input data-ic="q" type="search" placeholder="Code or words, e.g. toilet, WP.04" aria-label="Search item codes" style="font:inherit;padding:8px 10px;border-radius:6px;border:1px solid var(--line);background:var(--surface);color:var(--fg);width:100%">
          <div class="tbl" style="max-height:520px;overflow:auto"><table><thead><tr><th>Sl</th><th>Code</th><th>Description</th><th>Issues</th></tr></thead><tbody data-ic="rows">${masterRows(master.entries, "")}</tbody></table></div>`
          : `<div class="empty">The item list isn't loaded.</div>`}
        </div>
      </div>
    </div>`;

    const box = el.querySelector('[data-ic="matches"]');
    const cache = {};
    async function show(uid, mode) {
      el.querySelectorAll("[data-uid]").forEach(b => {
        const on = b.dataset.uid === uid;
        b.classList.toggle("primary", on);
        b.setAttribute("aria-current", on ? "true" : "false");
      });
      const key = uid + ":" + (mode || "auto");
      box.innerHTML = `<div class="empty">Matching…</div>`;
      try {
        cache[key] = cache[key] || await getJSON(`/api/itemcodes/match/${encodeURIComponent(uid)}${mode ? "?mode=" + mode : ""}`);
        const res = cache[key];
        box.innerHTML = matchesView(res) + (res.mode === "claude" ? `<div><button class="btn" data-ic="rules">Show the rule-based answer</button></div>` : mode === "rules" ? `<div><button class="btn" data-ic="auto">Back to Claude's answer</button></div>` : "");
        const r = box.querySelector('[data-ic="rules"]'), a = box.querySelector('[data-ic="auto"]');
        if (r) r.onclick = () => show(uid, "rules");
        if (a) a.onclick = () => show(uid, null);
      } catch (e) {
        box.innerHTML = `<div class="err">${esc(e.message)}</div>`;
      }
    }
    el.querySelectorAll("[data-uid]").forEach(b => b.addEventListener("click", () => show(b.dataset.uid, null)));
    if (inbox.length) show(String(inbox[0].uid), null);

    const q = el.querySelector('[data-ic="q"]'), rows = el.querySelector('[data-ic="rows"]');
    if (q && rows) q.addEventListener("input", () => { rows.innerHTML = masterRows(master.entries, q.value); });
  };
})();
