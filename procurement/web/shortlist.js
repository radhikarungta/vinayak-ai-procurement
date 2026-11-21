// Step 3 (first half) · Vendor shortlist. Plain script, no build step.
// Usage: load /web/shortlist.js, then call window.mountShortlist(el).
// Uses the page's CSS classes (.panel .eyebrow .chip .btn .tbl .muted .small .mono .err .empty ul.plain).
// The agent ranks and explains; the ticks are the person's choice and are never sent anywhere from here.
(function () {
  "use strict";
  const API = "/api/shortlist";
  const ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"};
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ESC[c]);
  const pct = x => `${Math.round(x * 100)}%`;
  // Values made up by scripts/make_demo_vendor_data.py. Same tone as the page's "Demo data" chip.
  const DEMO = `<span class="chip warn" title="Made up for the demo, not real history">demo</span>`;

  async function getJSON(url) {
    const r = await fetch(url);
    let body = null;
    try { body = await r.json(); } catch { /* not JSON */ }
    if (!r.ok) throw new Error((body && body.detail) || `Request failed (${r.status}).`);
    return body;
  }

  function injectStyle() {
    if (document.getElementById("sl-style")) return;
    const st = document.createElement("style");
    st.id = "sl-style";
    st.textContent = `
.sl{display:flex;flex-direction:column;gap:14px}
.sl .bar{display:flex;flex-wrap:wrap;gap:12px;align-items:end}
.sl select{font:inherit;font-size:.88rem;padding:7px 9px;border-radius:6px;border:1px solid var(--line);background:var(--surface);color:var(--fg)}
.sl .codes{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:.85rem}
.sl .codes label{font-weight:400;display:inline-flex;gap:4px;align-items:center}
.sl .cov{display:inline-block;width:60px;height:6px;border-radius:3px;background:var(--sunk);vertical-align:middle;margin-right:6px;overflow:hidden}
.sl .cov span{display:block;height:100%;background:var(--accent)}
.sl .fx{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:3px;font-size:.82rem}
.sl .fx .k{display:inline-block;min-width:140px;color:var(--muted)}
.sl .fx .chip{font-size:.7rem;padding:1px 7px}
.sl .fx .nd{color:var(--muted);font-style:italic}
.sl tr.on td{background:var(--accent-soft)}
.sl input[type=checkbox]{width:16px;height:16px;accent-color:var(--accent)}`;
    document.head.appendChild(st);
  }

  function factorList(fs) {
    return `<ul class="fx">${fs.map(f => `<li><span class="k">${esc(f.label)} <span class="mono">${f.score == null ? "—" : Math.round(f.score * 100)}</span></span>
      <span class="${f.score == null ? "nd" : ""}">${f.score == null ? "No data. " : ""}${esc(f.reason)}</span>${f.demo ? " " + DEMO : ""}</li>`).join("")}</ul>`;
  }

  function row(v) {
    const rank = v.score == null ? "—" : (v.tied ? `=${v.rank}` : v.rank);
    return `<tr class="${v.selected ? "on" : ""}" data-id="${esc(v.vendor_id)}">
      <td><input type="checkbox" data-tick="${esc(v.vendor_id)}" ${v.selected ? "checked" : ""} aria-label="Send RFQ to ${esc(v.name)}"></td>
      <td class="n">${rank}</td>
      <td><b>${esc(v.name)}</b>${v.demo ? " " + DEMO : ""}${v.notes.length ? `<ul class="plain small muted" style="margin-top:4px">${v.notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}</td>
      <td class="n">${v.score == null ? `<span class="muted">no data</span>` : v.score.toFixed(1)}</td>
      <td class="n"><span class="cov"><span style="width:${pct(v.coverage)}"></span></span>${pct(v.coverage)}</td>
      <td style="min-width:320px">${factorList(v.factors)}</td>
    </tr>`;
  }

  function view(res) {
    const meds = Object.entries(res.medians);
    const w = Object.entries(res.weights).map(([k, x]) => `${esc(k.replace("_", " "))} ${pct(x)}`).join(" · ");
    return `
      <p>${esc(res.headline)}</p>
      ${res.notes.length ? `<div class="banner">${res.notes.map(esc).join("<br>")}</div>` : ""}
      <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center">
        <span class="chip acc" id="sl-count"></span>
        <span class="small muted">Weights (assumed, not from the SOP): ${w}. A factor with no data is left out and the rest re-weighted; coverage shows how much weight had data.</span>
      </div>
      <div class="tbl"><table><thead><tr><th>RFQ</th><th class="n">Rank</th><th>Vendor</th><th class="n">Score</th><th class="n">Coverage</th><th>Why</th></tr></thead>
        <tbody>${res.vendors.map(row).join("") || `<tr><td colspan="6" class="muted">No vendors in this category.</td></tr>`}</tbody></table></div>
      ${res.excluded.length ? `<div><h3>Left out</h3><ul class="plain small">${res.excluded.map(x => `<li><b>${esc(x.name)}</b>: ${esc(x.reason)}</li>`).join("")}</ul></div>` : ""}
      ${meds.length ? `<details class="small"><summary class="muted">Median rates used for the price comparison (${meds.length})</summary>
        <div class="tbl" style="margin-top:6px"><table><thead><tr><th>Item code</th><th class="n">Median</th><th>Unit</th><th class="n">Quotes</th></tr></thead>
        <tbody>${meds.map(([c, m]) => `<tr><td class="mono">${esc(c)}</td><td class="n">₹${esc(m.median)}</td><td>${esc(m.unit)}</td><td class="n">${esc(m.quotes)}</td></tr>`).join("")}</tbody></table></div></details>` : ""}`;
  }

  window.mountShortlist = async function (el) {
    injectStyle();
    el.innerHTML = `<div class="sl"><div class="panel">
        <div class="bar">
          <div><div class="eyebrow">Category</div><select id="sl-cat"></select></div>
          <div style="flex:1 1 300px"><div class="eyebrow">Item codes</div><div class="codes" id="sl-codes"></div></div>
        </div>
      </div>
      <div class="panel" id="sl-out"><div class="empty">Loading…</div></div></div>`;
    const out = el.querySelector("#sl-out"), cat = el.querySelector("#sl-cat"), codesEl = el.querySelector("#sl-codes");
    let reqs = [], res = null;

    const count = () => {
      const n = el.querySelectorAll("input[data-tick]:checked").length;
      const c = el.querySelector("#sl-count");
      if (!c) return;
      c.textContent = `${n} ticked for the RFQ${n < res.min_rfq ? ` · SOP 05 asks for at least ${res.min_rfq}` : ""}`;
      c.className = "chip " + (n < res.min_rfq ? "warn" : "acc");
    };
    async function load() {
      const codes = [...codesEl.querySelectorAll("input:checked")].map(i => i.value).join(",");
      out.innerHTML = `<div class="empty">Scoring…</div>`;
      try {
        res = await getJSON(`${API}?category=${encodeURIComponent(cat.value)}&codes=${encodeURIComponent(codes)}`);
        out.innerHTML = view(res);
        count();
      } catch (e) { out.innerHTML = `<div class="err">${esc(e.message)}</div>`; }
    }
    function drawCodes() {
      const r = reqs.find(x => x.category === cat.value);
      codesEl.innerHTML = r && r.item_codes.length
        ? r.item_codes.map(c => `<label><input type="checkbox" value="${esc(c.code)}" checked><span class="mono">${esc(c.code)}</span> ${esc(c.text)}</label>`).join("")
        : `<span class="small muted">No past quotes in this category, so price can't be compared.</span>`;
    }
    out.addEventListener("change", e => {
      const t = e.target.closest("input[data-tick]"); if (!t) return;
      t.closest("tr").classList.toggle("on", t.checked); count();
    });
    cat.addEventListener("change", () => { drawCodes(); load(); });
    codesEl.addEventListener("change", load);
    try {
      reqs = await getJSON(`${API}/requirements`);
      if (!reqs.length) { out.innerHTML = `<div class="empty">No vendors or past deals yet. Add vendors in the Vendors tab.</div>`; return; }
      cat.innerHTML = reqs.map(r => `<option value="${esc(r.category)}">${esc(r.category)} (${r.vendors} vendor${r.vendors === 1 ? "" : "s"})</option>`).join("");
      drawCodes();
      load();
    } catch (e) { out.innerHTML = `<div class="err">${esc(e.message)}</div>`; }
  };
})();
