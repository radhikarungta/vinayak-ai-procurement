/* Vendor directory UI. Plain script, no build step.
   Load:  <script src="/web/vendors.js"></script>
   Mount: window.mountVendors(document.querySelector("#vendors"))
   Uses the page's CSS (.panel .chip .btn .tbl …) from intake.html, plus a few scoped input styles. */
(function () {
  "use strict";
  const API = "/api/vendors";
  const STATUSES = ["approved", "on list, not vetted", "blocked"];
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const statusTone = s => s === "approved" ? "good" : s === "blocked" ? "bad" : "warn";
  const list = s => String(s || "").split(",").map(x => x.trim()).filter(Boolean);
  const today = () => new Date().toISOString().slice(0, 10);
  // Values made up by scripts/make_demo_vendor_data.py. Same tone as the page's "Demo data" chip.
  const DEMO = `<span class="chip warn" title="Made up for the demo, not real history. Remove with scripts/make_demo_vendor_data.py --remove">demo</span>`;
  const creditDemo = v => (v.demo_fields || []).some(k => k === "credit_days" || k === "advance_pct");
  const creditCell = v => v.credit_days == null ? `<span class="muted">Not known</span>` : `${esc(v.credit_label)}${creditDemo(v) ? " " + DEMO : ""}`;
  const numOrNull = x => { const t = String(x ?? "").trim(); return t === "" ? null : Number(t); };

  function injectStyle() {
    if (document.getElementById("vd-style")) return;
    const st = document.createElement("style");
    st.id = "vd-style";
    st.textContent = `
.vd{display:flex;flex-direction:column;gap:14px}
.vd .bar{display:flex;flex-wrap:wrap;gap:8px;align-items:end}
.vd .bar>*{flex:0 0 auto} .vd .bar .grow{flex:1 1 220px}
.vd input:not([type=file]),.vd select,.vd textarea.short{width:100%;font:inherit;font-size:.88rem;padding:7px 9px;border-radius:6px;border:1px solid var(--line);background:var(--surface);color:var(--fg)}
.vd textarea.short{min-height:64px;font:13px/1.5 var(--body)}
.vd select:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.vd .f{display:flex;flex-direction:column;gap:4px}
.vd .row-actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.vd .chips{display:flex;flex-wrap:wrap;gap:4px}
.vd button.chip{border:0;cursor:pointer;font:inherit;font-size:.78rem}
.vd .head{display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px;align-items:start}`;
    document.head.appendChild(st);
  }

  async function api(path, opts = {}) {
    const r = await fetch(API + path, {headers: {"Content-Type": "application/json"}, ...opts});
    if (r.status === 204) return null;
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const d = body.detail;
      const msg = Array.isArray(d) ? d.map(e => `${(e.loc || []).slice(-1)[0] || "field"}: ${String(e.msg || "").replace(/^Value error, /, "")}`).join(" ") : d;
      throw new Error(msg || `Request failed (${r.status})`);
    }
    return body;
  }

  function catChips(v) {
    const conf = new Set(v.categories_to_confirm || []), demo = new Set(v.demo_categories || []);
    if (!v.categories.length) return `<span class="muted small">No category</span>`;
    return `<span class="chips">${v.categories.map(c => demo.has(c)
      ? `<span class="chip warn" title="Category added for the demo only.">${esc(c)} · demo</span>`
      : conf.has(c)
      ? `<span class="chip warn" title="Guessed from the name. Please confirm.">${esc(c)} · to confirm</span>`
      : `<span class="chip acc">${esc(c)}</span>`).join("")}</span>`;
  }

  function perfCell(p) {
    if (!p.count) return `<span class="muted">No history</span>`;
    return `<span class="mono">Q ${esc(p.quality)} · D ${esc(p.delivery)}</span> <span class="muted small">(${esc(p.count)})</span>${p.disputes.major ? ` <span class="chip bad">${esc(p.disputes.major)} major</span>` : ""}${p.demo ? " " + DEMO : ""}`;
  }

  window.mountVendors = function mountVendors(el) {
    injectStyle();
    const state = {items: [], cats: [], q: "", category: "", status: "", selected: null, draft: null, mode: "view", confirmDelete: false, error: "", flash: ""};

    el.innerHTML = `<div class="vd">
      <div class="panel">
        <div class="head"><div><p class="eyebrow">Vendor directory</p><h2>Who we can ask for quotes</h2>
          <p class="muted small">Categories marked "to confirm" were guessed from the vendor's name. Past performance is recorded by people after each job. A vendor with no records shows "no history", not a low score.</p></div>
          <button class="btn primary" data-act="new">Add a vendor</button></div>
        <div class="bar">
          <div class="f grow"><label for="vd-q">Search</label><input id="vd-q" type="search" placeholder="Name, city, project, GSTIN…"></div>
          <div class="f"><label for="vd-cat">Category</label><select id="vd-cat"></select></div>
          <div class="f"><label for="vd-st">Status</label><select id="vd-st"><option value="">Any status</option>${STATUSES.map(s => `<option>${esc(s)}</option>`).join("")}</select></div>
        </div>
        <div data-slot="msg"></div>
      </div>
      <div data-slot="side"></div>
      <div class="panel"><div data-slot="table"></div></div>
    </div>`;
    const $ = s => el.querySelector(s);
    const slot = n => el.querySelector(`[data-slot="${n}"]`);

    async function refresh() {
      const p = new URLSearchParams();
      if (state.q) p.set("q", state.q);
      if (state.category) p.set("category", state.category);
      if (state.status) p.set("status", state.status);
      try {
        const [res, cats] = await Promise.all([api("?" + p), api("/categories")]);
        state.items = res.items; state.cats = cats;
        if (state.selected) state.selected = res.items.find(v => v.id === state.selected.id) || state.selected;
        state.error = "";
      } catch (e) { state.error = e.message; }
      draw();
    }

    function drawCats() {
      const sel = $("#vd-cat");
      sel.innerHTML = `<option value="">All categories</option>` + state.cats.map(c => `<option value="${esc(c.name)}">${esc(c.name)} (${esc(c.vendors)})</option>`).join("");
      sel.value = state.category;
    }

    function drawTable() {
      if (!state.items.length) {
        slot("table").innerHTML = `<div class="empty">${state.q || state.category || state.status ? "No vendors match. Try a different search or filter." : "No vendors yet. Add one, or run the seed."}</div>`;
        return;
      }
      slot("table").innerHTML = `<p class="muted small" style="margin-bottom:8px">${esc(state.items.length)} vendor${state.items.length === 1 ? "" : "s"}</p>
      <div class="tbl"><table><thead><tr><th>Name</th><th>Categories</th><th>Status</th><th>Contact</th><th>Credit terms</th><th>Past performance</th></tr></thead><tbody>
      ${state.items.map(v => `<tr class="row" tabindex="0" data-id="${esc(v.id)}"${state.selected && state.selected.id === v.id ? ' aria-current="true"' : ""}>
        <td><b>${esc(v.name)}</b>${v.city ? `<div class="muted small">${esc(v.city)}</div>` : ""}</td>
        <td>${catChips(v)}</td>
        <td><span class="chip ${statusTone(v.status)}">${esc(v.status)}</span></td>
        <td class="small">${v.has_contact ? esc([v.contact_person, v.phone, v.email].filter(Boolean).join(" · ")) : `<span class="muted">None yet</span>`}</td>
        <td class="small">${creditCell(v)}</td>
        <td class="small">${perfCell(v.performance_summary)}</td></tr>`).join("")}
      </tbody></table></div>`;
    }

    function formHtml(v) {
      v = v || {categories: [], categories_to_confirm: [], projects: [], sources: [], status: "on list, not vetted"};
      const inp = (k, label, val, extra = "") => `<div class="f"><label for="vd-f-${k}">${label}</label><input id="vd-f-${k}" name="${k}" value="${esc(val)}" ${extra}></div>`;
      return `<form class="panel" data-form="vendor" novalidate>
        <div class="head"><h2>${v.id ? `Edit ${esc(v.name)}` : "Add a vendor"}</h2><button type="button" class="btn" data-act="cancel">Cancel</button></div>
        <div class="grid2">
          ${inp("name", "Name", v.name || "", "required")}
          <div class="f"><label for="vd-f-status">Status</label><select id="vd-f-status" name="status">${STATUSES.map(s => `<option${s === v.status ? " selected" : ""}>${esc(s)}</option>`).join("")}</select></div>
          ${inp("categories", "Categories (comma separated)", (v.categories || []).join(", "), 'placeholder="Waterproofing, Electrical"')}
          ${inp("projects", "Projects (comma separated)", (v.projects || []).join(", "))}
          ${inp("contact_person", "Contact person", v.contact_person || "")}
          ${inp("phone", "Phone", v.phone || "", 'type="tel"')}
          ${inp("email", "Email", v.email || "", 'type="email"')}
          ${inp("gstin", "GSTIN", v.gstin || "", 'placeholder="15 characters, e.g. 19ABCDE1234F1Z5" maxlength="15"')}
          ${inp("city", "City", v.city || "")}
          ${inp("sources", "Where we know them from", (v.sources || []).join(", "))}
          ${inp("credit_days", "Credit on bills, days (0 = advance / on delivery; blank = not known)", v.credit_days ?? "", 'type="number" min="0" max="365" step="1"')}
          ${inp("advance_pct", "Advance asked, % of order (blank = not known)", v.advance_pct ?? "", 'type="number" min="0" max="100" step="any"')}
        </div>
        <div class="f"><label for="vd-f-notes">Notes</label><textarea class="short" id="vd-f-notes" name="notes">${esc(v.notes || "")}</textarea></div>
        ${(v.categories_to_confirm || []).length ? `<p class="small muted">Still to confirm: ${esc(v.categories_to_confirm.join(", "))}. Saving keeps them marked; use "Confirm" on the vendor page to clear the mark.</p>` : ""}
        <div class="row-actions"><button class="btn primary" type="submit">${v.id ? "Save changes" : "Add vendor"}</button></div>
      </form>`;
    }

    function detailHtml(v) {
      const p = v.performance_summary;
      const conf = v.categories_to_confirm || [];
      const dd = (x, empty = "Not recorded") => x ? esc(x) : `<span class="none">${empty}</span>`;
      return `<div class="panel">
        <div class="head"><div><p class="eyebrow">Vendor</p><h2>${esc(v.name)}</h2></div>
          <div class="row-actions">
            <button class="btn" data-act="edit">Edit</button>
            ${state.confirmDelete
              ? `<span class="small">Delete this vendor and its records?</span><button class="btn" data-act="del-yes" style="color:var(--bad);border-color:var(--bad)">Yes, delete</button><button class="btn" data-act="del-no">Keep</button>`
              : `<button class="btn" data-act="del">Delete</button>`}
            <button class="btn" data-act="close">Close</button>
          </div></div>
        <div class="grid2">
          <dl class="fields">
            <dt>Status</dt><dd><span class="chip ${statusTone(v.status)}">${esc(v.status)}</span></dd>
            <dt>Categories</dt><dd>${v.categories.length ? `<span class="chips">${v.categories.map(c => conf.includes(c)
              ? `<span class="chip warn">${esc(c)} · to confirm</span><button class="chip good" data-act="confirm-cat" data-cat="${esc(c)}">Confirm</button><button class="chip" data-act="drop-cat" data-cat="${esc(c)}">Remove</button>`
              : `<span class="chip acc">${esc(c)}</span>`).join(" ")}</span>` : `<span class="none">None yet</span>`}</dd>
            <dt>Contact</dt><dd>${dd(v.contact_person)}</dd>
            <dt>Phone</dt><dd>${v.phone ? `<span class="mono">${esc(v.phone)}</span>` : dd("")}</dd>
            <dt>Email</dt><dd>${v.email ? `<span class="mono">${esc(v.email)}</span>` : dd("")}</dd>
            <dt>GSTIN</dt><dd>${v.gstin ? `<span class="mono">${esc(v.gstin)}</span>` : dd("")}</dd>
            <dt>City</dt><dd>${dd(v.city)}</dd>
            <dt>Projects</dt><dd>${dd(v.projects.join(", "), "None")}</dd>
            <dt>Credit terms</dt><dd>${creditCell(v)}</dd>
            <dt>Known from</dt><dd>${v.sources.length ? `<ul class="plain">${v.sources.map(s => `<li>${esc(s)}</li>`).join("")}</ul>` : dd("")}</dd>
            ${v.notes ? `<dt>Notes</dt><dd>${esc(v.notes)}</dd>` : ""}
          </dl>
          <div style="display:flex;flex-direction:column;gap:10px">
            <h3>Past performance</h3>
            <p>${p.count ? esc(p.label) : `<span class="muted">No history yet. This vendor isn't scored until someone records a job.</span>`}${p.demo ? ` ${DEMO} <span class="small muted">${esc(p.demo)} of ${esc(p.count)} records are demo data.</span>` : ""}</p>
            ${p.count ? `<div class="tbl"><table><thead><tr><th>Date</th><th>Project · work</th><th class="n">Quality</th><th class="n">Delivery</th><th>Disputes</th></tr></thead><tbody>
              ${v.performance.map(r => `<tr><td class="mono">${esc(r.date)}</td><td>${esc(r.project)} · ${esc(r.work)}${r.note ? `<div class="muted small">${esc(r.note)}</div>` : ""}<div class="muted small">By ${esc(r.recorded_by)}</div></td>
                <td class="n">${esc(r.quality)}/5</td><td class="n">${esc(r.delivery)}/5</td>
                <td><span class="chip ${r.disputes === "none" ? "" : r.disputes === "minor" ? "warn" : "bad"}">${esc(r.disputes)}</span>${r.source === "demo" ? " " + DEMO : ""}</td></tr>`).join("")}
            </tbody></table></div>` : ""}
            <form data-form="perf" class="panel" style="background:var(--sunk)" novalidate>
              <h3>Add a record</h3>
              <div class="grid2">
                <div class="f"><label for="vd-p-date">Date</label><input id="vd-p-date" name="date" type="date" value="${today()}" required></div>
                <div class="f"><label for="vd-p-project">Project</label><input id="vd-p-project" name="project" value="${esc(v.projects[0] || "")}" required></div>
                <div class="f"><label for="vd-p-work">Work or item</label><input id="vd-p-work" name="work" placeholder="e.g. toilet waterproofing, floors 1–10" required></div>
                <div class="f"><label for="vd-p-by">Your role</label><input id="vd-p-by" name="recorded_by" placeholder="e.g. Site engineer" required></div>
                <div class="f"><label for="vd-p-q">Quality (1 poor – 5 excellent)</label><select id="vd-p-q" name="quality">${[5, 4, 3, 2, 1].map(n => `<option${n === 3 ? " selected" : ""}>${n}</option>`).join("")}</select></div>
                <div class="f"><label for="vd-p-d">Delivery on time (1–5)</label><select id="vd-p-d" name="delivery">${[5, 4, 3, 2, 1].map(n => `<option${n === 3 ? " selected" : ""}>${n}</option>`).join("")}</select></div>
                <div class="f"><label for="vd-p-x">Disputes</label><select id="vd-p-x" name="disputes"><option>none</option><option>minor</option><option>major</option></select></div>
                <div class="f"><label for="vd-p-note">Note</label><input id="vd-p-note" name="note" placeholder="What went well or wrong"></div>
              </div>
              <div class="row-actions"><button class="btn primary" type="submit">Save record</button></div>
            </form>
          </div>
        </div>
      </div>`;
    }

    function draw() {
      drawCats();
      slot("msg").innerHTML = state.error ? `<div class="err">${esc(state.error)}</div>` : state.flash ? `<p class="small" style="color:var(--good)">${esc(state.flash)}</p>` : "";
      const side = slot("side");
      if (state.mode === "new") side.innerHTML = formHtml(state.draft);
      else if (state.mode === "edit" && state.selected) side.innerHTML = formHtml(state.draft ? {...state.selected, ...state.draft} : state.selected);
      else if (state.selected) side.innerHTML = detailHtml(state.selected);
      else side.innerHTML = "";
      drawTable();
    }

    async function act(fn, flash) {
      try { await fn(); state.error = ""; state.flash = flash || ""; } catch (e) { state.error = e.message; state.flash = ""; }
      await refresh();
    }

    async function select(id) {
      try { state.selected = await api("/" + encodeURIComponent(id)); state.mode = "view"; state.confirmDelete = false; state.error = state.flash = ""; }
      catch (e) { state.error = e.message; }
      draw();
      slot("side").scrollIntoView({block: "nearest", behavior: "smooth"});
    }

    let t;
    $("#vd-q").addEventListener("input", e => { clearTimeout(t); t = setTimeout(() => { state.q = e.target.value.trim(); refresh(); }, 200); });
    $("#vd-cat").addEventListener("change", e => { state.category = e.target.value; refresh(); });
    $("#vd-st").addEventListener("change", e => { state.status = e.target.value; refresh(); });

    el.addEventListener("click", e => {
      const row = e.target.closest("tr.row");
      if (row) return select(row.dataset.id);
      const b = e.target.closest("[data-act]");
      if (!b) return;
      const v = state.selected;
      switch (b.dataset.act) {
        case "new": state.mode = "new"; state.selected = null; state.draft = null; state.flash = state.error = ""; return draw();
        case "edit": state.mode = "edit"; state.draft = null; return draw();
        case "cancel": state.mode = "view"; state.draft = null; state.error = ""; return draw();
        case "close": state.selected = null; state.mode = "view"; return draw();
        case "del": state.confirmDelete = true; return draw();
        case "del-no": state.confirmDelete = false; return draw();
        case "del-yes": return act(async () => { await api("/" + encodeURIComponent(v.id), {method: "DELETE"}); state.selected = null; state.confirmDelete = false; }, `Deleted ${v.name}.`);
        case "confirm-cat": return act(() => api("/" + encodeURIComponent(v.id), {method: "PATCH", body: JSON.stringify({categories_to_confirm: v.categories_to_confirm.filter(c => c !== b.dataset.cat)})}), `Confirmed ${b.dataset.cat}.`);
        case "drop-cat": return act(() => api("/" + encodeURIComponent(v.id), {method: "PATCH", body: JSON.stringify({categories: v.categories.filter(c => c !== b.dataset.cat)})}), `Removed ${b.dataset.cat}.`);
      }
    });
    el.addEventListener("keydown", e => {
      const row = e.target.closest && e.target.closest("tr.row");
      if (row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); select(row.dataset.id); }
    });

    el.addEventListener("submit", e => {
      e.preventDefault();
      const f = e.target, fd = new FormData(f);
      if (f.dataset.form === "vendor") {
        const body = {};
        for (const k of ["name", "status", "contact_person", "phone", "email", "gstin", "city", "notes"]) body[k] = String(fd.get(k) || "").trim();
        for (const k of ["categories", "projects", "sources"]) body[k] = list(fd.get(k));
        // Send credit terms only when changed, so saving other fields keeps them (and their demo tag) as they are.
        const was = state.mode === "edit" && state.selected ? state.selected : {};
        for (const k of ["credit_days", "advance_pct"]) {
          const x = numOrNull(fd.get(k));
          if (x !== null && !Number.isFinite(x)) { state.error = "Credit days and advance % must be numbers."; return draw(); }
          if (x !== (was[k] ?? null)) body[k] = x;
        }
        state.draft = body;
        if (!body.name) { state.error = "A vendor needs a name."; return draw(); }
        const editing = state.mode === "edit" && state.selected;
        if (editing) body.categories_to_confirm = (state.selected.categories_to_confirm || []).filter(c => body.categories.includes(c));
        return act(async () => {
          const saved = editing
            ? await api("/" + encodeURIComponent(state.selected.id), {method: "PATCH", body: JSON.stringify(body)})
            : await api("", {method: "POST", body: JSON.stringify(body)});
          state.selected = saved; state.mode = "view"; state.draft = null;
        }, editing ? "Saved." : `Added ${body.name}.`);
      }
      if (f.dataset.form === "perf") {
        const body = Object.fromEntries(fd.entries());
        body.quality = Number(body.quality); body.delivery = Number(body.delivery);
        for (const k of ["project", "work", "recorded_by"]) if (!String(body[k] || "").trim()) { state.error = "Fill in the project, the work and your role."; return draw(); }
        return act(() => api(`/${encodeURIComponent(state.selected.id)}/performance`, {method: "POST", body: JSON.stringify(body)}), "Record saved.");
      }
    });

    refresh();
  };
})();
