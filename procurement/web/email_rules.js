/* Email rules UI. Plain browser JS, no build step.
 *
 *   <script src="/web/email_rules.js"></script>
 *   window.mountEmailRules(document.getElementById("rules"));
 *
 * Talks to /api/email-rules (procurement/email_rules/api.py). Uses the intake page's CSS classes;
 * the only extra styles are for form fields, injected once below.
 */
(function () {
  "use strict";

  const API = "/api/email-rules";
  const HOW = "Block rules always win; if you have any allow rules, an email must match one of them or it is held; with no allow rules, every email that isn't blocked is read.";
  const FIELDS = [
    ["sender", "Sender address", "qs@site.example.com"],
    ["sender_domain", "Sender domain", "example.com"],
    ["subject", "Subject", "Requirement"],
    ["body", "Email text", "unsubscribe"],
    ["to", "Sent to", "requirements@"],
    ["has_attachment", "Has an attachment", ""],
    ["attachment_type", "Attachment type", "xlsx"],
  ];
  const MATCHES = [["contains", "contains"], ["equals", "is exactly"], ["starts_with", "starts with"], ["regex", "matches pattern"]];
  const DECISION = {
    process: ["good", "Read"],
    block: ["bad", "Blocked"],
    hold: ["warn", "Held"],
  };
  const BLANK = { action: "block", field: "sender_domain", match: "equals", value: "", note: "" };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }

  async function call(method, path, body) {
    const res = await fetch(API + path, {
      method,
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = null;
    try { data = await res.json(); } catch (_) { /* empty body */ }
    if (!res.ok) {
      let msg = data && data.detail;
      if (Array.isArray(msg)) msg = msg.map((d) => d.msg).join(" ");
      const err = new Error(msg || "Something went wrong (" + res.status + "). Try again.");
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function injectStyle() {
    if (document.getElementById("er-style")) return;
    const s = document.createElement("style");
    s.id = "er-style";
    s.textContent =
      ".er-form{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}" +
      ".er-form label{display:flex;flex-direction:column;gap:4px}" +
      ".er-form .wide{grid-column:1/-1}" +
      ".er-form select,.er-form input{font:inherit;font-size:.88rem;padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:var(--surface);color:var(--fg);min-width:0}" +
      ".er-form select:focus-visible,.er-form input:focus-visible{outline:2px solid var(--accent);outline-offset:2px}" +
      ".er-row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}" +
      ".er-off td{color:var(--muted)} .er-changed td{background:var(--accent-soft)}" +
      ".er-tbl td:last-child{white-space:nowrap}";
    document.head.appendChild(s);
  }

  function opt(list, sel) {
    return list.map(([v, label]) => '<option value="' + esc(v) + '"' + (v === sel ? " selected" : "") + ">" + esc(label) + "</option>").join("");
  }

  function decisionChip(d, count) {
    const [tone, label] = DECISION[d] || ["", d];
    return '<span class="chip ' + tone + '">' + esc(label) + (count === undefined ? "" : " " + esc(count)) + "</span>";
  }

  window.mountEmailRules = function mountEmailRules(el) {
    injectStyle();
    const st = { rules: [], env: [], how: HOW, editing: null, confirm: null, form: { ...BLANK }, listErr: "", formErr: "", saving: false, seq: 0 };

    el.innerHTML =
      '<div style="display:flex;flex-direction:column;gap:18px">' +
      '<section class="panel">' +
      '<div><p class="eyebrow">Email rules</p><h2>Choose which emails get read</h2>' +
      '<p class="muted small" style="margin-top:4px">Rules run before the model reads anything, so a skipped email costs nothing.</p></div>' +
      '<p class="small" data-r="how"></p>' +
      '<div data-r="list"><p class="muted small">Loading rules…</p></div>' +
      "</section>" +
      '<div class="grid2">' +
      '<section class="panel" data-r="form"></section>' +
      '<section class="panel" data-r="preview"><p class="muted small">Loading preview…</p></section>' +
      "</div></div>";
    const $ = (r) => el.querySelector('[data-r="' + r + '"]');

    // ── Rule list ────────────────────────────────────────────────────────────────────────────
    function renderList() {
      $("how").innerHTML = "<b>How rules combine:</b> " + esc(st.how);
      const rows = st.rules.map((r) => {
        const actions = st.confirm === r.id
          ? '<span class="er-row"><span class="small">Delete this rule?</span>' +
            '<button class="btn" data-act="del-yes" data-id="' + esc(r.id) + '">Yes, delete</button>' +
            '<button class="btn" data-act="del-no">Keep it</button></span>'
          : '<span class="er-row"><button class="btn" data-act="edit" data-id="' + esc(r.id) + '">Edit</button>' +
            '<button class="btn" data-act="del" data-id="' + esc(r.id) + '">Delete</button></span>';
        return '<tr class="' + (r.enabled ? "" : "er-off") + '">' +
          '<td><button class="btn" data-act="toggle" data-id="' + esc(r.id) + '" aria-pressed="' + r.enabled + '" title="Turn this rule ' + (r.enabled ? "off" : "on") + '">' + (r.enabled ? "On" : "Off") + "</button></td>" +
          '<td><span class="chip ' + (r.action === "allow" ? "good" : "bad") + '">' + (r.action === "allow" ? "Allow" : "Block") + "</span></td>" +
          "<td>" + esc(r.text) + (r.note ? '<div class="muted small">' + esc(r.note) + "</div>" : "") + "</td>" +
          "<td>" + actions + "</td></tr>";
      });
      const envRows = st.env.map((r) =>
        "<tr><td><span class=\"muted small\">On</span></td>" +
        '<td><span class="chip good">Allow</span></td>' +
        "<td>" + esc(r.text) + ' <span class="chip acc">from .env</span></td>' +
        '<td class="muted small">Change WATCH_FROM in .env</td></tr>');
      const all = rows.concat(envRows);
      $("list").innerHTML =
        (st.listErr ? '<p class="err">' + esc(st.listErr) + "</p>" : "") +
        (all.length
          ? '<div class="tbl"><table class="er-tbl"><thead><tr><th>On</th><th>Action</th><th>Rule</th><th></th></tr></thead><tbody>' + all.join("") + "</tbody></table></div>"
          : '<p class="empty">No rules yet. Every email is read.</p>');
    }

    async function load() {
      try {
        const data = await call("GET", "");
        st.rules = data.rules || [];
        st.env = data.env_rules || [];
        st.how = data.how_rules_combine || HOW;
        st.listErr = "";
      } catch (e) {
        st.listErr = e.message;
      }
      renderList();
    }

    $("list").addEventListener("click", async (ev) => {
      const b = ev.target.closest("button[data-act]");
      if (!b) return;
      const id = b.dataset.id;
      const act = b.dataset.act;
      if (act === "del") { st.confirm = id; return renderList(); }
      if (act === "del-no") { st.confirm = null; return renderList(); }
      if (act === "edit") {
        const r = st.rules.find((x) => x.id === id);
        if (!r) return;
        st.editing = id;
        st.form = { action: r.action, field: r.field, match: r.match, value: r.value, note: r.note || "" };
        st.formErr = "";
        renderForm();
        el.querySelector('[data-r="form"] input[name="value"], [data-r="form"] select[name="value"]').focus();
        return schedulePreview();
      }
      b.disabled = true;
      try {
        if (act === "toggle") await call("POST", "/" + encodeURIComponent(id) + "/toggle");
        if (act === "del-yes") {
          await call("DELETE", "/" + encodeURIComponent(id));
          st.confirm = null;
          if (st.editing === id) resetForm();
        }
        st.listErr = "";
      } catch (e) {
        st.listErr = e.message;
      }
      await load();
      schedulePreview(0);
    });

    // ── Add / edit form ──────────────────────────────────────────────────────────────────────
    function renderForm() {
      const f = st.form;
      const ph = (FIELDS.find((x) => x[0] === f.field) || [])[2] || "";
      const yesNo = f.field === "has_attachment";
      $("form").innerHTML =
        "<div><p class=\"eyebrow\">" + (st.editing ? "Edit rule" : "Add a rule") + "</p>" +
        '<p class="muted small" style="margin-top:4px">The preview on the right updates as you type. Nothing changes until you save.</p></div>' +
        '<form class="er-form" novalidate>' +
        '<label>Action<select name="action">' + opt([["block", "Block: skip these"], ["allow", "Allow: read these"]], f.action) + "</select></label>" +
        '<label>Look at<select name="field">' + opt(FIELDS.map(([v, l]) => [v, l]), f.field) + "</select></label>" +
        (yesNo ? "" : '<label>Test<select name="match">' + opt(MATCHES, f.match) + "</select></label>") +
        (yesNo
          ? '<label>Value<select name="value">' + opt([["yes", "Yes"], ["no", "No"]], f.value === "no" ? "no" : "yes") + "</select></label>"
          : '<label>Value<input name="value" autocomplete="off" spellcheck="false" placeholder="' + esc(ph) + '" value="' + esc(f.value) + '"></label>') +
        '<label class="wide">Note (optional)<input name="note" autocomplete="off" placeholder="Why this rule exists" value="' + esc(f.note) + '"></label>' +
        '<p class="err wide" data-r="ferr"' + (st.formErr ? "" : " hidden") + ">" + esc(st.formErr) + "</p>" +
        '<div class="er-row wide"><button class="btn primary" type="submit"' + (st.saving ? " disabled" : "") + ">" + (st.editing ? "Save changes" : "Add rule") + "</button>" +
        '<button class="btn" type="button" data-act="cancel">' + (st.editing ? "Cancel" : "Clear") + "</button>" +
        "</div></form>";
      if (yesNo) st.form.value = f.value === "no" ? "no" : "yes";
    }

    function showFormErr(msg) {
      st.formErr = msg || "";
      const p = $("form").querySelector('[data-r="ferr"]');
      if (p) { p.textContent = st.formErr; p.hidden = !st.formErr; }
    }

    function resetForm() {
      st.editing = null;
      st.form = { ...BLANK };
      st.formErr = "";
      renderForm();
    }

    function readForm() {
      const fd = new FormData($("form").querySelector("form"));
      st.form = {
        action: fd.get("action"),
        field: fd.get("field"),
        match: fd.get("match") || "equals",
        value: String(fd.get("value") || ""),
        note: String(fd.get("note") || ""),
      };
    }

    $("form").addEventListener("input", () => {
      const prev = st.form.field;
      readForm();
      if (st.form.field !== prev) {
        // "Has an attachment" takes yes/no; the other fields take typed text.
        if (st.form.field === "has_attachment") st.form.value = "yes";
        else if (prev === "has_attachment") st.form.value = "";
        renderForm();
        const sel = $("form").querySelector('select[name="field"]');
        if (sel) sel.focus();
      }
      schedulePreview();
    });

    $("form").addEventListener("click", (ev) => {
      if (ev.target.closest('[data-act="cancel"]')) { resetForm(); schedulePreview(0); }
    });

    $("form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      readForm();
      if (!st.form.value.trim()) return showFormErr("Type a value for the rule to look for.");
      st.saving = true;
      renderForm();
      try {
        if (st.editing) await call("PUT", "/" + encodeURIComponent(st.editing), st.form);
        else await call("POST", "", st.form);
        st.saving = false;
        resetForm();
        await load();
        schedulePreview(0);
      } catch (e) {
        st.saving = false;
        renderForm();
        showFormErr(e.message);
      }
    });

    // ── Live preview ─────────────────────────────────────────────────────────────────────────
    let timer = null;
    function schedulePreview(delay) {
      clearTimeout(timer);
      timer = setTimeout(runPreview, delay === undefined ? 250 : delay);
    }

    async function runPreview() {
      const seq = ++st.seq;
      const hasDraft = st.form.value.trim() !== "";
      const body = hasDraft ? { draft: st.form, draft_id: st.editing } : {};
      try {
        const data = await call("POST", "/preview", body);
        if (seq !== st.seq) return;
        if (hasDraft) showFormErr("");
        renderPreview(data);
      } catch (e) {
        if (seq !== st.seq) return;
        if (e.status === 422 && hasDraft) showFormErr("Not ready to save: " + e.message);
        else $("preview").innerHTML = '<p class="err">' + esc(e.message) + "</p>";
      }
    }

    function renderPreview(data) {
      const c = data.counts || {};
      const rows = (data.emails || []).map((e) => {
        const was = e.changed ? '<div class="muted small">Was: ' + esc((DECISION[e.before] || ["", e.before])[1]) + "</div>" : "";
        return '<tr class="' + (e.changed ? "er-changed" : "") + '">' +
          "<td><div>" + esc(e.subject || "(no subject)") + '</div><div class="muted small">' + esc(e.sender || "unknown sender") +
          " · " + (e.source === "demo" ? "demo email" : "your inbox") + "</div></td>" +
          "<td>" + decisionChip(e.decision) + was + "</td>" +
          '<td class="small">' + esc(e.reason) + "</td></tr>";
      });
      const changed = (data.emails || []).filter((e) => e.changed).length;
      $("preview").innerHTML =
        '<div><p class="eyebrow">Preview</p><p class="small" style="margin-top:4px">' +
        (data.proposed
          ? (st.editing ? "With your changes to this rule" : "With this rule added") + ": " +
            (changed ? changed + " email" + (changed === 1 ? "" : "s") + " would change." : "no email would change.")
          : "What the saved rules do now.") +
        "</p></div>" +
        '<div class="er-row">' + decisionChip("process", c.process || 0) + decisionChip("block", c.block || 0) + decisionChip("hold", c.hold || 0) + "</div>" +
        (rows.length
          ? '<div class="tbl"><table><thead><tr><th>Email</th><th>Result</th><th>Why</th></tr></thead><tbody>' + rows.join("") + "</tbody></table></div>"
          : '<p class="empty">No emails to preview yet.</p>');
    }

    renderForm();
    load();
    schedulePreview(0);
  };
})();
