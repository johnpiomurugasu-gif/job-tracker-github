/* Live wiring for the job-tracker dashboard v15 (a copy of live_v14.js, extended; v14's file is not changed).
 * Fetches /api/data (built from the database by dashboard/server_v15.py) and renders
 * every section from real rows — no sample data. Ports the v13 mock's chart /
 * timeline / calendar mechanics so the look and interactions are unchanged.
 * v14 adds: the header (last good run, v14 and copy tags), the Discarded tab, Delete, review categories,
 * the content checks badge and Confirm all. Text from the database goes into the page only through esc() or textContent.
 * v15 adds: the edit panel (right click, Control click, double click or Edit on an application row), the role or a faint "add role"
 * under every company, a dot on John's own values, his steps in Timeline, and the status filter from the server's status list.
 * v16 (a copy of live_v15.js; v15's file is not changed) adds the Normal / Kanban switch and the board in Pipeline: see "the board (v16)".
 * v17 (a copy of live_v16.js; v16's file is not changed) adds the List / Calendar switch, the calendar and the Coming up strip in Timeline, and
 * Upcoming dates in the panel: see "the calendar (v17)".
 * v18 (a copy of live_v17.js; v17's file is not changed) adds where an automatic role or industry came from: the line under Role and Industry
 * in the panel and the same words as the tooltip in the lists (autoWhy). The values themselves come from the server, as before.
 * v19 (a copy of live_v18.js; v18's file is not changed): esc() also escapes the single quote, and post() sends the X-Job-Tracker header
 * the v19 server asks for on every POST.
 */
"use strict";

const OUTCOME_COLOR = {
  Applied: "#9DB0AF", Assessment: "#d9a441", Interview: "#5CC8CD",
  Offer: "#5CC8CD", Rejected: "#d67a6c", Withdrawn: "#9EA7BA", "Not recorded": "#5f6e6d",
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pill = (cat, label) => `<span class="pill pill-${cat}">${esc(label) || "—"}</span>`;

let DATA = null;

/* ------------------------------------------------- pure functions (v14; tested with Node) */
const own = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
// review_category -> plain English. This is the one table: reword the labels here. A category that is not in it is shown as its name with spaces.
const NO_CATEGORY = "Filed by the old path";
const CAT_LABELS = {
  not_about_application: "Does not read like an application email",
  not_employer_name: "Name looks like a platform or service, not an employer",
  shared_parent_or_ambiguous: "Sender is shared by more than one company",
  recruiter_sender: "Sent by a recruiter or graduate service",
  name_not_in_email: "Company name is not in the email",
  duplicate_name: "Looks like a company you already track",
  sender_names_other_employer: "Sender names a different employer",
  address_like: "Name looks like an address",
  discarded_company_replied: "Replied after you discarded it",
  short_name: "Company name is very short",
  job_alert: "Looks like a job alert",
  text_unavailable: "The email text could not be read",
  low_confidence: "Llama was not sure what this email is",
  parse_failure: "Llama's answer could not be read",
  other: "Needs a look",
};
// the eight content checks (companies.content_checks_passed), in plain words
const CHECK_LABELS = {
  status: "The email is an application update",
  name_in_email: "The company name is in the email",
  name_shape: "The name looks like a company name",
  not_duplicate: "Not a copy of a company you track",
  addressed_to_you: "Addressed to you",
  about_application: "Reads like an application email",
  not_alert: "Not a job alert",
  sender_not_other: "The sender does not name another employer",
};
function catLabel(cat) {
  if (!cat) return NO_CATEGORY;
  return own(CAT_LABELS, cat) ? CAT_LABELS[cat] : cat.charAt(0).toUpperCase() + cat.slice(1).replace(/_/g, " ");
}
// [{cat, label, rows}]: categories in the order of CAT_LABELS, any other category after them, rows with no category last
function groupReview(rows) {
  const by = new Map();
  rows.forEach((r) => { const k = r.category || ""; if (!by.has(k)) by.set(k, []); by.get(k).push(r); });
  const known = Object.keys(CAT_LABELS).filter((k) => by.has(k));
  const others = [...by.keys()].filter((k) => k && !own(CAT_LABELS, k)).sort();
  return [...known, ...others, ...(by.has("") ? [""] : [])].map((k) => ({ cat: k, label: catLabel(k), rows: by.get(k) }));
}
function ageWords(s) {
  if (s < 60) return "less than a minute ago";
  const m = Math.floor(s / 60);
  if (m < 120) return `${plural(m, "minute")} ago`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${plural(h, "hour")} ago`;
  return `${plural(Math.floor(h / 24), "day")} ago`;
}
// the header text for the heartbeat the server sent (red comes from the server: one constant there)
function hbView(hb) {
  if (!hb || hb.state === "noserver") return { text: "Dashboard server not answering", red: true };
  if (hb.state === "missing" || hb.state === "unreadable") return { text: "No good run recorded", red: true };
  if (hb.state === "future") return { text: "Last good run: time is in the future (check the clock)", red: true };
  return { text: `Last good run: ${ageWords(hb.age_s)}`, red: !!hb.red };
}
function deleteText(r) {
  return { title: `Delete ${r.company}`,
    desc: `This removes ${r.company} and everything under it: ${plural(r.n_apps, "application")} and ${plural(r.n_events, "event")}.`,
    label: "Ignore future emails from this company",
    on: "Ticked: the company moves to the Discarded tab and can be restored; later emails from it go quietly to the quiet log.",
    off: "Unticked: it is deleted for good; later emails from it are filed as normal." };
}
function restoreText(d) {
  if (d.seed) return { title: `Take ${d.name} off the list`, ok: "Take off the list", blocked: "",
    desc: `${d.name} has no stored company: it is a name you discarded from the review queue. Restore only takes the name off this list, so later emails from it are filed as normal again.` };
  const n = d.emails_since;
  return { title: `Restore ${d.name}`, ok: "Restore",
    blocked: d.blocked ? "A company with this name already exists, so it cannot be restored. Delete or rename that one first." : "",
    desc: `Brings ${d.name} back exactly as it was when you deleted it (${plural(d.events_removed || 0, "event")}), ${d.was_confirmed ? "confirmed" : "unconfirmed"}. ` +
          `${plural(n, "email")} that arrived meanwhile ${n === 1 ? "was" : "were"} ignored and ${n === 1 ? "is" : "are"} not refiled.` };
}
function confirmAllText(info) {
  const n = info.count, m = info.from_content, cos = n === 1 ? "1 company" : `${n} companies`;
  return { title: `Confirm all ${cos.replace("company", "unconfirmed company").replace("companies", "unconfirmed companies")}?`,
    desc: `${cos} will be confirmed, ${m} of them added from email content (content checks passed). ` +
          `It is the same as pressing Confirm on each one, and it covers every unconfirmed company, not only the rows you can see.` };
}
// the Confirm dialog for a review row of category discarded_company_replied (build stage 4); r.replied comes from the server (null when the name is no longer on the Discarded list)
function repliedText(r) {
  const p = r && r.replied;
  if (!p) return null;
  if (p.seed) return { restore: false, blocked: "",
    desc: `${p.name} is on the Discarded list as a name only (no stored company). Confirm creates a confirmed company from this email, as usual, and takes ${p.name} off that list.` };
  const st = String(r.status || "applied").replace(/_/g, " ");
  return { restore: true, title: `Restore ${p.name}`, ok: "Restore company and add this email",
    desc: `Brings ${p.name} back exactly as it was when you deleted it (${plural(p.events_removed || 0, "event")}), ${p.was_confirmed ? "confirmed" : "unconfirmed"}, ` +
          `then adds this email to its latest application as ${st}. This item leaves the review queue.`,
    blocked: p.blocked ? `A company with this name already exists, so ${p.name} cannot be restored. Delete or rename that one first.`
      : p.no_application ? `The stored copy of ${p.name} has no application to add this email to, so it cannot be restored from here. Discard this item, or use Restore on the Discarded tab.` : "" };
}
// the dialog of "Add to existing company" (build stage 5); r.attach comes from the server: the one existing company this review row points at (null: no button)
function attachText(r) {
  const a = r && r.attach;
  if (!a) return null;
  const st = r.status_label || String(r.status || "applied").replace(/_/g, " "), when = r.date || "an unknown date";
  return { title: `Add this email to ${a.name}`, ok: "Add to existing company",
    desc: `Adds the ${st} email of ${when} to ${a.name} as an event on its latest application and removes this item from the queue; the company itself is not changed (not renamed, confirmed or restored).`,
    blocked: a.apps ? "" : `${a.name} has no application to add this email to.` };
}
function checksBadge(checks) {
  if (!checks || !checks.length) return "";
  const items = checks.map((c) => (own(CHECK_LABELS, c) ? CHECK_LABELS[c] : String(c).replace(/_/g, " ")));
  return `<span class="badge-checks" tabindex="0" title="${esc("Added from the email text. Passed: " + items.join("; "))}">content checks<span class="pop">` +
    `<b>Added from the email text. Checks passed:</b><ul>${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul></span></span>`;
}
// v15: the role under the company name, or a faint "add role" that opens the panel on the Role field
function roleLine(role, appId) {
  return role ? `<span class="role"${tipAttr(rowOf(appId), "role")}>${esc(role)}</span>`   // v18: tooltip
    : `<button type="button" class="add-role" data-app-open="${Number(appId)}" data-field="role">add role</button>`;
}
const mineDot = (a, f) => (a.v15 && a.v15.mine.includes(f) ? `<span class="mine-dot" title="Your edit"></span>` : "");
// v18: where an automatic role or industry came from, in plain words. The server sends row.v18 only for values from the automatic tables
// (never with a dot: the dot stays John's). autoTip: the same words, only where the automatic value is the one shown (not under John's own).
function autoWhy(r, f) {
  const a = r && r.v18 && r.v18[f];
  if (!a) return "";
  const when = a.date ? dayShort(a.date) : "";
  return `Automatic: ${a.value}. ` + (a.from === "email" ? (when ? `Read from an email dated ${when}.` : "Read from an email.")
    : `Guessed from a web search of the company name${when ? " on " + when : ""}.`);
}
const autoTip = (r, f) => (r && !(r.v15 && r.v15.mine.includes(f)) ? autoWhy(r, f) : "");
const tipAttr = (r, f) => { const t = autoTip(r, f); return t ? ` title="${esc(t)}"` : ""; };
const rowOf = (id) => (DATA ? DATA.pipeline.find((x) => x.id === Number(id)) || null : null);

/* ------------------------------------------------------------------ fetch */
async function load() {
  const r = await fetch("/api/data");
  DATA = await r.json();
  await loadUpcoming();                                   // v17: John's upcoming dates (their own route: /api/data stays v16's)
  renderAll();
}

function renderAll() {
  renderHeader();
  renderSidebar();
  renderSummary();
  renderReview();
  renderMoved();
  renderPipeline();
  renderTimeline();
  renderMetrics();
  renderStale();
  renderCalendar();
  renderFilterTab();
  renderDiscarded();
  renderTlCal();                                          // v17
  document.getElementById("synced").innerHTML =
    `<span class="dot"></span>synced ${esc(DATA.today)}`;
}

/* ----------------------------------------------------------------- header (v14) */
function renderHeader() {
  const v = hbView(DATA.heartbeat), el = document.getElementById("hb");
  el.className = "hb" + (v.red ? " red" : "");
  el.innerHTML = `<span class="dot"></span>${esc(v.text)}`;
  el.title = DATA.heartbeat && DATA.heartbeat.state === "unreadable" ? "The heartbeat file exists but cannot be read" : "";
  document.getElementById("tag-v14").textContent = DATA.meta.version;
  const copy = document.getElementById("tag-copy");
  copy.style.display = DATA.meta.copy ? "" : "none";
  copy.title = `reading ${DATA.meta.db}, not db/tracker.db`;
  document.getElementById("footer").textContent = `reading ${DATA.meta.db} — every figure is a live query`;
}
// v13 has no periodic refresh, so the header asks for the heartbeat alone every 30 seconds (it ages, and turns red, while the page stays open)
async function pollHeartbeat() {
  if (!DATA) return;
  try { DATA.heartbeat = await (await fetch("/api/heartbeat")).json(); } catch (e) { DATA.heartbeat = { state: "noserver" }; }
  renderHeader();
}

/* ---------------------------------------------------------------- sidebar */
function renderSidebar() {
  const s = DATA.summary;
  document.getElementById("brand-sub").textContent = `${s.total} applications`;
  document.getElementById("nav-review").textContent = DATA.review.length;
  document.getElementById("nav-pipeline").textContent = s.total;
  document.getElementById("nav-stale").textContent = DATA.stale.length;
}

/* --------------------------------------------------------------- priority */
function renderSummary() {
  const s = DATA.summary, k = DATA.metrics.kpis;
  const cell = (label, num, cls, head, sub) => `
    <div class="cell"><div class="label">${label}</div>
      <div class="num-row"><div class="num ${cls}">${num}</div></div>
      <div class="footer"><div class="footer-headline">${head}</div><div class="footer-sub">${sub}</div></div>
    </div>`;
  document.getElementById("summary").innerHTML =
    cell("Total applications", s.total, "", `${k.n} tracked`, "from your Gmail sync") +
    cell("Awaiting response", s.awaiting, "", "No reply yet", "First responses take 1–3 weeks") +
    cell("Interview / assessment", s.interview_assessment, "accent", "Currently in process",
         `${k.reach} ever reached assessment or interview`) +
    cell("Rejected", s.rejected, "red", `${Math.round(s.rejected / s.total * 100)}% of all applications`,
         "across the pipeline") +
    cell("Unconfirmed, new", s.unconfirmed, "amber", "Auto-added from inbox", "Confirm/edit in Pipeline");
}

// build stage 5: "Add to existing company" when the server found the one company the row points at. On a reply from a discarded company it replaces Confirm (Confirm there would
// create a second company of the same name); on a duplicate_name row Confirm stays, because it can be a genuinely new company.
function reviewRow(r) {
  const attach = !!r.attach, noConfirm = attach && r.category === "discarded_company_replied";
  return `<tr class="review-item">
      <td><div class="company">${esc(r.company)}<span class="via">${esc(r.sender)}</span></div></td>
      <td><div class="evidence"><span class="subj">${esc(r.subject) || esc(r.reason)}</span><span class="cat">${esc(catLabel(r.category))}</span></div></td>
      <td><div class="actions">
        ${noConfirm ? "" : `<button class="btn btn-keep" data-id="${r.id}" data-act="confirm">Confirm</button>`}
        ${attach ? `<button class="btn btn-keep" data-id="${r.id}" data-act="attach">Add to existing company</button>` : ""}
        <button class="btn btn-drop" data-id="${r.id}" data-act="discard">Discard</button>
      </div></td></tr>`;
}

function renderReview() {
  const rows = DATA.review;
  document.getElementById("review-hint").textContent =
    `${rows.length} item(s) the sync could not auto-file · not yet confirmed`;
  const box = document.getElementById("review-container");
  if (!rows.length) {
    box.innerHTML = `<div class="empty-state">Nothing in the review queue. Emails the pipeline
      can't confidently auto-file (no company match, low confidence, parse failure) land here for
      one-click triage.</div>`;
    return;
  }
  box.innerHTML = `<table class="review-table"><thead><tr>
      <th>Company</th><th>Detail</th><th class="review-th-actions">Actions</th></tr></thead><tbody>${
    groupReview(rows).map((g) => `<tr class="review-group"><td colspan="3">${esc(g.label)}<span class="count">${g.rows.length}</span></td></tr>${
      g.rows.map(reviewRow).join("")}`).join("")}</tbody></table>`;
  box.querySelectorAll(".btn").forEach((b) =>
    b.addEventListener("click", () => {
      const row = rows.find((x) => x.id == b.dataset.id);
      if (b.dataset.act === "attach") openAttachDialog(row); else openDialog(row, b.dataset.act);
    }));
}

function renderMoved() {
  const el = document.getElementById("moved-list");
  if (!DATA.just_moved.length) { el.innerHTML = `<div class="empty-state">No events yet.</div>`; return; }
  el.innerHTML = DATA.just_moved.map((e) => `<div class="moved-row" data-app="${Number(e.app_id)}">
      <div class="company">${esc(e.company)}${roleLine(e.role, e.app_id)}</div>
      ${pill(e.cat, e.status)}<div class="when">${esc(e.date)}</div></div>`).join("");
}

/* --------------------------------------------------------------- pipeline */
function pipelineActions(a) {
  const btns = [];
  if (a.unconfirmed) {
    btns.push(`<button class="btn btn-sm btn-keep" data-id="${a.id}" data-act="confirm">Confirm</button>`);
  }
  btns.push(`<button class="btn btn-sm btn-keep" type="button" data-app-open="${a.id}">Edit</button>`);
  btns.push(`<button class="btn btn-sm btn-drop" data-id="${a.id}" data-act="delete">Delete</button>`);
  if (a.cat === "assessment") {
    btns.push(a.submitted
      ? `<span class="submitted-badge">Submitted ✓</span>`
      : `<button class="btn btn-sm btn-mark" data-id="${a.id}" data-act="submit">Mark submitted</button>`);
  }
  return btns.join("");
}

function renderPipeline() {
  const rows = DATA.pipeline;
  const inds = [...new Set(rows.map((r) => r.industry))].sort();
  const sel = document.getElementById("pipeline-industry-filter"), keepInd = sel.value;
  sel.innerHTML = `<option value="all">All industries</option>` +
    inds.map((i) => `<option value="${esc(i)}">${esc(i)}</option>`).join("");
  sel.value = inds.includes(keepInd) ? keepInd : "all";
  // v15: the status filter comes from the server's status list (by label, so "Interview" covers both interview statuses, and Offer is there)
  const sf = document.getElementById("pipeline-status-filter"), keepSt = sf.value;
  sf.innerHTML = `<option value="all">All statuses</option>` + DATA.status_filter.map((l) => `<option value="${esc(l)}">${esc(l)}</option>`).join("") +
    `<option value="unconfirmed">Unconfirmed only</option>`;
  sf.value = [...sf.options].some((o) => o.value === keepSt) ? keepSt : "all";

  document.getElementById("pipeline-tbody").innerHTML = rows.map((a) => `
    <tr data-app="${a.id}" data-id="${a.id}" data-status-label="${esc(a.status_label)}" data-industry="${esc(a.industry)}"
        data-unconfirmed="${a.unconfirmed}"
        data-search="${esc((a.company + " " + a.role + " " + a.industry).toLowerCase())}">
      <td class="company-cell"><span class="name">${esc(a.company)}${mineDot(a, "company")}${
        a.unconfirmed ? '<span class="tag-unconfirmed">unconfirmed</span>' : ""}${checksBadge(a.checks)}</span>
        ${a.role ? `<span class="role"${tipAttr(a, "role")}>${esc(a.role)}${mineDot(a, "role")}</span>` : roleLine("", a.id)}</td>
      <td class="industry-tag"${tipAttr(a, "industry")}>${esc(a.industry)}${mineDot(a, "industry")}</td>
      <td>${pill(a.cat, a.status_label)}${mineDot(a, "status")}</td>
      <td class="pipe-stage">${esc(a.stage)}${mineDot(a, "stage")}</td>
      <td>${esc(a.last)}</td>
      <td class="pipe-actions">${pipelineActions(a)}</td></tr>`).join("");

  document.querySelectorAll('#pipeline-tbody .btn[data-act]')
    .forEach((b) => b.addEventListener("click", onPipelineAction));
  applyPipelineFilters();
}

async function post(url, body) {
  try {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "X-Job-Tracker": "1" },
                                body: JSON.stringify(body) });
    return await r.json();
  } catch (e) { return { ok: false, error: "Could not reach the dashboard server. Nothing was changed." }; }
}

async function reloadKeepScroll() { const y = window.scrollY; await load(); window.scrollTo(0, y); }
const STALE = ["gone", "done", "count", "exists", "no_target", "changed"];   // answers that mean "the page was out of date": nothing changed, so refresh it

// Confirm and Mark submitted post at once; Delete opens its dialog. A refusal (gone, done, busy) is said in a short dialog.
async function onPipelineAction(e) {
  const b = e.currentTarget, id = +b.dataset.id, act = b.dataset.act;
  if (act === "delete") return openDeleteDialog(DATA.pipeline.find((x) => x.id === id));
  b.disabled = true;
  const res = await post(act === "confirm" ? "/api/app/confirm" : "/api/app/mark_submitted", { id });
  if (!res.ok) { b.disabled = false; showNote(res.busy ? "Try again" : "Nothing was changed", res.error || "unknown error"); if (STALE.includes(res.code)) reloadKeepScroll(); return; }
  await reloadKeepScroll();
}

function applyPipelineFilters() {
  const q = document.getElementById("pipeline-search").value.trim().toLowerCase();
  const status = document.getElementById("pipeline-status-filter").value;
  const industry = document.getElementById("pipeline-industry-filter").value;
  const rows = [...document.querySelectorAll("#pipeline-tbody tr")];
  let vis = 0;
  rows.forEach((row) => {
    const okQ = !q || row.dataset.search.includes(q);
    const okS = status === "all" ||
      (status === "unconfirmed" ? row.dataset.unconfirmed === "true" : row.dataset.statusLabel === status);
    const okI = industry === "all" || row.dataset.industry === industry;
    const show = okQ && okS && okI;
    row.classList.toggle("hidden-row", !show);
    if (show) vis++;
  });
  document.getElementById("pipeline-count").textContent = `showing ${vis} of ${rows.length}`;
  document.getElementById("pipeline-no-results").style.display = vis ? "none" : "block";
  updateConfirmAll();
  renderBoard();                                          // v16: the board shows the rows these filters left visible
}

// Confirm all shows on the unconfirmed filter only, and works only while nothing narrows the list further (it confirms every unconfirmed company)
function updateConfirmAll() {
  const b = document.getElementById("confirm-all-btn"), n = DATA.confirm_all.count;
  b.style.display = document.getElementById("pipeline-status-filter").value === "unconfirmed" && n > 0 ? "" : "none";
  const narrowed = document.getElementById("pipeline-search").value.trim() !== "" || document.getElementById("pipeline-industry-filter").value !== "all";
  b.disabled = narrowed;
  b.title = narrowed ? "Clear the search box and the industry filter first: Confirm all confirms every unconfirmed company, not only the rows shown." : "";
  b.textContent = `Confirm all ${n}…`;
}

/* --------------------------------------------------------------- timeline */
const CHIP_TO_CAT = { assessment: "assessment", interview: "interview", rejected: "rejected",
  withdrawn: "withdrawn", applied: "applied" };
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function renderTimeline() {
  const ev = DATA.timeline;
  const axis = document.getElementById("timeline-axis");
  axis.innerHTML = "";
  if (!ev.length) { document.getElementById("timeline-empty-hint").textContent = "No dated events yet."; return; }
  const times = ev.map((e) => Date.parse(e.date));
  const min = Math.min(...times), max = Math.max(...times), span = (max - min) || 1;
  const at = (t) => 5 + ((t - min) / span) * 90;

  // month labels across the span
  const d0 = new Date(min), d1 = new Date(max);
  for (let y = d0.getFullYear(), m = d0.getMonth(); y < d1.getFullYear() || (y === d1.getFullYear() && m <= d1.getMonth());) {
    const t = new Date(y, m, 1).getTime();
    if (t >= min - 2.6e9) {
      const lab = document.createElement("div");
      lab.className = "timeline-month-label";
      lab.style.left = `${Math.max(0, Math.min(100, at(t)))}%`;
      lab.textContent = MONTHS[m];
      axis.appendChild(lab);
    }
    if (++m > 11) { m = 0; y++; }
  }

  ev.forEach((e) => {
    const left = at(Date.parse(e.date));
    const p = document.createElement("div");
    p.className = `timeline-point t-${e.cat}${e.source === "you" ? " manual" : ""}`;   // v15: John's steps have a dashed ring
    p.style.left = `${left}%`;
    p.dataset.cat = e.cat;
    const anchor = left < 16 ? "anchor-left" : left > 84 ? "anchor-right" : "";
    p.innerHTML = `<div class="hover-card ${anchor}">
        <div class="hc-title">${esc(e.company)}</div>
        <div class="hc-desc">${esc(e.status)}</div>
        <div class="hc-meta">${esc(e.date)} · ${esc(e.industry)}</div></div>`;
    p.addEventListener("click", () => selectPoint(e, p));
    axis.appendChild(p);
  });
}

function selectPoint(e, el) {
  document.querySelectorAll(".timeline-point").forEach((p) => p.classList.remove("selected"));
  el.classList.add("selected");
  document.getElementById("td-co").textContent = e.company;
  document.getElementById("td-date").textContent = e.date;
  document.getElementById("td-role").textContent = e.role || "—";
  document.getElementById("td-industry").textContent = e.industry;
  document.getElementById("td-status").textContent = e.status;
  document.getElementById("td-source").textContent = e.source === "you" ? "Recorded by you" : "Gmail sync";
  document.getElementById("td-detail").textContent = e.detail || (e.source === "you" ? "No note." : "No further detail recorded.");
  document.getElementById("timeline-detail").classList.add("visible");
  document.getElementById("timeline-empty-hint").style.display = "none";
}

/* ---------------------------------------------------------------- metrics */
function renderMetrics() {
  const k = DATA.metrics.kpis;
  document.getElementById("metrics-sub").textContent = `based on ${k.n} tracked applications`;
  const cell = (label, num, cls, delta) => `<div class="kpi-cell">
      <div class="kpi-label">${label}</div><div class="kpi-num ${cls}">${num}</div>
      <div class="kpi-delta">${delta}</div></div>`;
  document.getElementById("kpi-row").innerHTML =
    cell("Applications / week (avg)", k.apps_per_week ?? "—", "", "over the dated span") +
    cell("Reach assessment or interview", `${k.reach_pct}%`, "accent", `${k.reach} of ${k.n} applications`) +
    cell("Interview → offer", `${k.i2o_pct}%`, "", `${k.offers} of ${k.interviewed} interviews`) +
    cell("Rejection rate", `${k.rej_pct}%`, "red", `${k.rejected} of ${k.n}`) +
    cell("Avg. time to first response", k.avg_response == null ? "—" : `${k.avg_response} days`, "", "where a response came") +
    cell("Gone quiet, 3+ weeks", k.gone_quiet, "amber", "see Gone quiet tab");

  renderChart(DATA.metrics.weekly);
  renderDonut(DATA.metrics.outcome);
  renderBars("industry-list", DATA.metrics.industry);
  renderBars("source-list", DATA.metrics.source, "#5f6e6d");
  const rj = DATA.metrics.rej_stage;
  document.getElementById("rejstage-sub").textContent =
    `stage reached before the rejection came, of ${Object.values(rj).reduce((a, b) => a + b, 0)}`;
  renderBars("rejstage-list", rj, "var(--red)");
}

const SVGNS = "http://www.w3.org/2000/svg";
function renderChart(weekly) {
  const W = 720, H = 220, padL = 8, padR = 32, padT = 14, padB = 28;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const gEl = document.getElementById("chart-gridlines");
  const dotsEl = document.getElementById("chart-dots");
  const labelsEl = document.getElementById("chart-labels");
  gEl.innerHTML = dotsEl.innerHTML = labelsEl.innerHTML = "";
  if (!weekly.length) return;
  const maxVal = Math.max(...weekly.map((d) => Math.max(d.applied, d.rejected))) + 1;
  const n = weekly.length;
  const xFor = (i) => padL + (n === 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const yFor = (v) => padT + plotH - (v / maxVal) * plotH;

  [0, 0.25, 0.5, 0.75, 1].forEach((f) => {
    const y = padT + f * plotH;
    const ln = document.createElementNS(SVGNS, "line");
    ln.setAttribute("x1", padL); ln.setAttribute("x2", W - padR);
    ln.setAttribute("y1", y); ln.setAttribute("y2", y); ln.setAttribute("class", "chart-gridline");
    gEl.appendChild(ln);
  });
  document.getElementById("line-applied").setAttribute("points",
    weekly.map((d, i) => `${xFor(i)},${yFor(d.applied)}`).join(" "));
  document.getElementById("line-rejected").setAttribute("points",
    weekly.map((d, i) => `${xFor(i)},${yFor(d.rejected)}`).join(" "));

  const tooltip = document.getElementById("chart-tooltip");
  const svg = document.getElementById("line-chart-svg");
  weekly.forEach((d, i) => {
    const x = xFor(i);
    if (i % 2 === 0 || i === n - 1) {
      const t = document.createElementNS(SVGNS, "text");
      t.setAttribute("x", x); t.setAttribute("y", H - 8); t.setAttribute("text-anchor", "middle");
      t.setAttribute("class", "chart-axis-label"); t.textContent = d.label;
      labelsEl.appendChild(t);
    }
    [["", d.applied, "var(--ink-soft)"], [" chart-dot-rejected", d.rejected, "var(--red)"]].forEach(([cls, val]) => {
      const dot = document.createElementNS(SVGNS, "circle");
      dot.setAttribute("cx", x); dot.setAttribute("cy", yFor(val)); dot.setAttribute("r", 3.5);
      dot.setAttribute("class", "chart-dot" + cls);
      dot.addEventListener("mouseenter", () => {
        const total = d.applied + d.rejected;
        tooltip.innerHTML = `<div style="font-family:var(--mono);font-size:11px;color:var(--ink-faint);margin-bottom:4px;">${d.label}</div>
          <div class="tt-row"><span class="tt-swatch" style="background:var(--ink-soft)"></span><span class="tt-label">Applied</span><span class="tt-val">${d.applied}</span></div>
          <div class="tt-row"><span class="tt-swatch" style="background:var(--red)"></span><span class="tt-label">Rejected</span><span class="tt-val">${d.rejected}</span></div>
          <div class="tt-total"><span>Total</span><span class="tt-val" style="margin-left:auto">${total}</span></div>`;
        const rect = svg.getBoundingClientRect(), scaleX = rect.width / W;
        tooltip.style.left = Math.min(x * scaleX + 12, rect.width - 160) + "px";
        tooltip.style.top = "4px";
        tooltip.classList.add("visible");
      });
      dot.addEventListener("mouseleave", () => tooltip.classList.remove("visible"));
      dotsEl.appendChild(dot);
    });
  });
}

function renderDonut(outcome) {
  const svg = document.getElementById("donut-svg");
  const total = Object.values(outcome).reduce((a, b) => a + b, 0) || 1;
  let off = 0;
  const circles = [`<circle cx="21" cy="21" r="15.9" fill="transparent" stroke="var(--line)" stroke-width="6"></circle>`];
  Object.entries(outcome).forEach(([name, count]) => {
    const frac = (count / total) * 100;
    circles.push(`<circle cx="21" cy="21" r="15.9" fill="transparent" stroke="${OUTCOME_COLOR[name] || "#9DB0AF"}"
      stroke-width="6" stroke-dasharray="${frac} ${100 - frac}" stroke-dashoffset="${-off}"
      transform="rotate(-90 21 21)"></circle>`);
    off += frac;
  });
  circles.push(`<text x="21" y="19.5" text-anchor="middle" font-family="var(--mono)" font-size="6.5" fill="var(--ink)" font-weight="600">${total}</text>
    <text x="21" y="25" text-anchor="middle" font-family="var(--mono)" font-size="3.3" fill="var(--ink-faint)">total</text>`);
  svg.innerHTML = circles.join("");
  document.getElementById("donut-legend").innerHTML = Object.entries(outcome)
    .sort((a, b) => b[1] - a[1]).map(([name, count]) => `<div class="donut-legend-item">
      <div class="donut-legend-left"><span class="donut-dot" style="background:${OUTCOME_COLOR[name] || "#9DB0AF"}"></span>${esc(name)}</div>
      <span class="donut-count">${count}</span></div>`).join("");
}

function renderBars(id, map, color) {
  const entries = Object.entries(map).sort((a, b) => b[1] - a[1]);
  const el = document.getElementById(id);
  if (!entries.length) { el.innerHTML = `<div class="hint">No data yet.</div>`; return; }
  const mx = Math.max(...entries.map((e) => e[1])) || 1;
  el.innerHTML = entries.map(([label, count]) => `<div class="industry-item">
      <span class="iname">${esc(label)}</span>
      <div class="industry-bar-track"><div class="industry-bar-fill" style="width:${Math.round(100 * count / mx)}%${color ? ";background:" + color : ""}"></div></div>
      <span class="icount">${count}</span></div>`).join("");
}

/* ------------------------------------------------------------- gone quiet */
function renderStale() {
  const el = document.getElementById("stale-list");
  if (!DATA.stale.length) {
    el.innerHTML = `<div class="empty-state">Nothing has gone quiet — every open application has activity within 3 weeks.</div>`;
    return;
  }
  el.innerHTML = DATA.stale.map((s) => `<div class="stale-row" data-app="${Number(s.app_id)}">
      <div class="company">${esc(s.company)}${roleLine(s.role, s.app_id)}</div>
      <div class="lastsignal">${esc(s.stage)}</div>
      <div class="since">${s.days} days since ${esc(s.last)}</div></div>`).join("");
}

/* Calendar: today marker + suggested follow-ups (last signal + 1 week) */
let calY, calM;
function flaggedDates() {
  return DATA.stale.map((s) => { const d = new Date(s.last); d.setDate(d.getDate() + 7); return d; })
    .filter((d) => !isNaN(d));
}
function renderCalendar() {
  const today = new Date(DATA.today);
  if (calY === undefined) { calY = today.getFullYear(); calM = today.getMonth(); }
  const flags = flaggedDates();
  const grid = document.getElementById("calendar-grid");
  const monthNames = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  document.getElementById("cal-month-label").textContent = `${monthNames[calM]} ${calY}`;
  const same = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  let html = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"].map((d) => `<div class="cal-dow">${d}</div>`).join("");
  const start = new Date(calY, calM, 1).getDay();
  const days = new Date(calY, calM + 1, 0).getDate();
  const prevDays = new Date(calY, calM, 0).getDate();
  for (let i = start - 1; i >= 0; i--) html += `<div class="cal-day other-month">${prevDays - i}</div>`;
  for (let d = 1; d <= days; d++) {
    const date = new Date(calY, calM, d);
    let cls = "cal-day";
    if (same(date, today)) cls += " today";
    if (flags.some((f) => same(f, date))) cls += " flagged";
    html += `<div class="${cls}">${d}</div>`;
  }
  const trailing = (7 - ((start + days) % 7)) % 7;
  for (let d = 1; d <= trailing; d++) html += `<div class="cal-day other-month">${d}</div>`;
  grid.innerHTML = html;
}

/* --------------------------------------------------------- by industry/role */
function renderFilterTab() {
  const rows = DATA.pipeline;
  const inds = ["All", ...new Set(rows.map((r) => r.industry))];
  const roles = ["All", ...new Set(rows.map((r) => r.role_type))];
  const chipHTML = (v, i) => `<span class="chip${i === 0 ? " active" : ""}" data-v="${esc(v)}">${esc(v)}</span>`;
  document.getElementById("filter-industry-chips").innerHTML = inds.map(chipHTML).join("");
  document.getElementById("filter-role-chips").innerHTML = roles.map(chipHTML).join("");
  let fi = "All", fr = "All";
  const draw = () => {
    document.getElementById("filter-tbody").innerHTML = rows
      .filter((a) => (fi === "All" || a.industry === fi) && (fr === "All" || a.role_type === fr))
      .map((a) => `<tr data-app="${a.id}"><td class="company-cell"><span class="name">${esc(a.company)}</span></td>
        <td${tipAttr(a, "role")}>${a.role ? esc(a.role) : roleLine("", a.id)}</td><td class="industry-tag"${tipAttr(a, "industry")}>${esc(a.industry)}</td>
        <td class="industry-tag">${esc(a.role_type)}</td><td>${pill(a.cat, a.status_label)}</td></tr>`).join("");
  };
  const wire = (containerId, set) => document.querySelectorAll(`#${containerId} .chip`).forEach((c) =>
    c.addEventListener("click", () => {
      document.querySelectorAll(`#${containerId} .chip`).forEach((x) => x.classList.remove("active"));
      c.classList.add("active"); set(c.dataset.v); draw();
    }));
  wire("filter-industry-chips", (v) => { fi = v; });
  wire("filter-role-chips", (v) => { fr = v; });
  draw();
}

/* ----------------------------------------------------- confirm/discard dialog */
let dlgItem = null, dlgAct = null, dlgBlocked = false;
function openDialog(item, act) {
  dlgItem = item; dlgAct = act;
  const confirm = act === "confirm", rep = confirm ? repliedText(item) : null, restore = !!(rep && rep.restore);
  document.getElementById("dlg-title").textContent = restore ? rep.title : (confirm ? "Confirm " : "Discard ") + item.company;
  const base = confirm
    ? "Creates a confirmed company + application from this email, then clears it from the queue. Edit anything first."
    : "Removes this from the review queue without creating an application.";
  document.getElementById("dlg-desc").textContent = restore ? rep.desc : rep ? base + " " + rep.desc : base;
  document.getElementById("dlg-fields").style.display = restore ? "none" : "";     // a restore has nothing to edit: the company comes back as it was
  document.getElementById("dlg-company").value = item.company === "—" ? "" : item.company;
  document.getElementById("dlg-role").value = "";
  document.getElementById("dlg-industry").value = "";
  document.getElementById("dlg-status").value = item.status || "applied";
  const save = document.getElementById("dlg-save");
  save.textContent = restore ? rep.ok : confirm ? "Confirm and add" : "Discard";
  save.className = confirm ? "btn btn-primary" : "btn btn-drop";
  dlgBlocked = restore && !!rep.blocked; save.disabled = dlgBlocked;
  // v14: Discard remembers the name unless the box is cleared (a review item with no company name has nothing to remember)
  const named = item.company !== "—", box = document.getElementById("dlg-remember");
  document.getElementById("dlg-remember-row").style.display = confirm ? "none" : "";
  box.checked = named; box.disabled = !named;
  document.getElementById("dlg-msg").textContent = restore ? rep.blocked : "";
  document.getElementById("dialog-overlay").classList.add("visible");
}
async function commitDialog() {
  const url = dlgAct === "confirm" ? "/api/confirm" : "/api/discard";
  const body = dlgAct === "confirm"
    ? { id: dlgItem.id, company: val("dlg-company"), role: val("dlg-role"),
        industry: val("dlg-industry"), status: val("dlg-status"), stage: dlgItem.stage }
    : { id: dlgItem.id, remember: document.getElementById("dlg-remember").checked };
  const save = document.getElementById("dlg-save");
  save.disabled = true;                                   // a double click posts once
  const res = await post(url, body);
  save.disabled = dlgBlocked;
  if (!res.ok) {
    document.getElementById("dlg-msg").textContent = res.error || "unknown error";
    if (STALE.includes(res.code)) reloadKeepScroll();
    return;
  }
  document.getElementById("dialog-overlay").classList.remove("visible");
  await load(); // re-fetch so counts and the queue reflect the write
}
const val = (id) => document.getElementById(id).value.trim();

/* one more dialog (v14): Delete, Restore, Confirm all, and short notices */
let dlg2Ok = null;
function syncCheckLines() {
  const on = document.getElementById("dlg2-check").checked;
  document.getElementById("dlg2-on").classList.toggle("on", on);
  document.getElementById("dlg2-off").classList.toggle("on", !on);
}
function openDialog2(o) {   // {title, desc, check, on, off, blocked, ok, okClass, onOk(checked) -> the server's answer}; no ok = a notice with Close only
  const $ = (id) => document.getElementById(id);
  $("dlg2-title").textContent = o.title;
  $("dlg2-desc").textContent = o.desc;
  $("dlg2-check-row").style.display = o.check ? "" : "none";
  if (o.check) { $("dlg2-check").checked = true; $("dlg2-check-label").textContent = o.check; $("dlg2-on").textContent = o.on; $("dlg2-off").textContent = o.off; syncCheckLines(); }
  $("dlg2-msg").textContent = o.blocked || "";
  const ok = $("dlg2-ok");
  ok.textContent = o.ok || ""; ok.className = "btn " + (o.okClass || "btn-primary"); ok.disabled = !!o.blocked; ok.style.display = o.ok ? "" : "none";
  $("dlg2-cancel").textContent = o.ok ? "Cancel" : "Close";
  dlg2Ok = o.onOk;
  $("dialog2-overlay").classList.add("visible");
}
const closeDialog2 = () => document.getElementById("dialog2-overlay").classList.remove("visible");
const showNote = (title, message) => openDialog2({ title, desc: message });
async function commitDialog2() {
  const ok = document.getElementById("dlg2-ok");
  ok.disabled = true;                                     // a double click posts once
  const res = await dlg2Ok(document.getElementById("dlg2-check").checked);
  if (res.ok) { closeDialog2(); await reloadKeepScroll(); return; }
  document.getElementById("dlg2-msg").textContent = res.error || "unknown error";
  if (STALE.includes(res.code)) {                         // the page was out of date: nothing changed; say so, refresh behind the dialog
    ok.style.display = "none"; document.getElementById("dlg2-cancel").textContent = "Close"; reloadKeepScroll();
  } else ok.disabled = false;                             // busy or another error: the button stays, so it can be pressed again
}
function openDeleteDialog(r) {
  const t = deleteText(r);
  openDialog2({ title: t.title, desc: t.desc, check: t.label, on: t.on, off: t.off, ok: "Delete", okClass: "btn-drop",
    onOk: (remember) => post("/api/app/delete", { id: r.company_id, remember }) });
}
function openAttachDialog(r) {                           // the request carries the review row's id only: the server decides the company again
  const t = attachText(r);
  openDialog2({ title: t.title, desc: t.desc, blocked: t.blocked, ok: t.ok, okClass: "btn-primary", onOk: () => post("/api/attach", { id: r.id }) });
}
function openRestoreDialog(d) {
  const t = restoreText(d);
  openDialog2({ title: t.title, desc: t.desc, blocked: t.blocked, ok: t.ok, okClass: "btn-primary",
    onOk: () => post("/api/restore", { name_key: d.name_key }) });
}
function openConfirmAllDialog() {
  const t = confirmAllText(DATA.confirm_all), shown = DATA.confirm_all.count;   // the request carries the count this dialog shows
  openDialog2({ title: t.title, desc: t.desc, ok: "Yes, confirm all", okClass: "btn-primary", onOk: () => post("/api/confirm_all", { count: shown }) });
}

/* ------------------------------------------------------------- discarded tab */
function renderDiscarded() {
  const rows = DATA.discarded;
  document.getElementById("nav-discarded").textContent = rows.length;
  document.getElementById("discarded-hint").textContent = `${plural(rows.length, "name")} ignored · later emails from them go quietly to the quiet log`;
  document.getElementById("discarded-empty").style.display = rows.length ? "none" : "block";
  document.getElementById("discarded-table").style.display = rows.length ? "" : "none";
  const tb = document.getElementById("discarded-tbody");
  tb.innerHTML = rows.map((d) => `<tr>
      <td class="company-cell"><span class="name">${esc(d.name)}</span>
        <span class="role">${d.seed ? "no stored company" : esc(plural(d.events_removed || 0, "event")) + " removed"}</span></td>
      <td>${esc(String(d.discarded_at).slice(0, 10))}</td>
      <td>${d.seed ? "—" : d.was_confirmed ? "Confirmed" : "Unconfirmed"}</td>
      <td>${Number(d.emails_since)}</td>
      <td class="pipe-actions"><button class="btn btn-sm btn-keep" data-key="${esc(d.name_key)}">Restore</button></td></tr>`).join("");
  tb.querySelectorAll(".btn").forEach((b) =>
    b.addEventListener("click", () => openRestoreDialog(rows.find((x) => x.name_key === b.dataset.key))));
}

/* ----------------------------------------------------------- the edit panel (v15) */
// Nothing is sent until Save; Save is ONE request (/api/v15/save), ONE transaction on the server. P is the open panel:
// {row, staged: {field: text, or null = back to automatic}, adds: [steps to add], removes: Set of step ids, touched: the Add step boxes were changed}
const PANEL_FIELDS = ["company", "role", "industry", "status", "stage", "notes"];
const byId = (id) => document.getElementById(id);
let P = null;
function todayISO() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
const fieldInput = (f) => byId(`ep-${f}`);
const fieldBox = (f) => document.querySelector(`#ep-fields [data-f="${f}"]`);
const statusName = (s) => (DATA.status_choices.find((c) => c[0] === s) || [s, s || "none"])[1];
// what the panel shows for a field when it opens (the value the table shows), and the automatic value (what Reset goes back to)
function shownValue(f) {
  const v = P.row.v15;
  if (f === "status") return P.row.status;
  if (f === "stage") return v.stage_raw;
  if (f === "notes") return v.notes;
  return v.mine.includes(f) ? (v.edits[f] ?? "") : (v.auto[f] ?? "");
}
const autoValue = (f) => (f === "notes" ? "" : P.row.v15.auto[f] ?? "");
// the step picked in "What did you do?": null for "Nothing new to record", else {type_id or new_type, label, date, note}, or a sentence saying what is missing
function pickedStep() {
  const sel = byId("ep-step-type").value, date = byId("ep-step-date").value, note = byId("ep-step-note").value.trim();
  if (!sel) return null;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || date > todayISO()) return "Pick the day you did it (today or earlier).";
  if (sel !== "new") {
    const t = DATA.step_types.find((x) => String(x.id) === sel);
    return { type_id: t.id, label: t.label, date, note };
  }
  const label = byId("ep-newtype-label").value.trim().replace(/\s+/g, " ");
  if (!label) return "Type a name for the new step type.";
  const known = DATA.step_types.find((t) => t.label.toLowerCase() === label.toLowerCase());
  return known ? { type_id: known.id, label: known.label, date, note } : { new_type: { label, sets_status: byId("ep-newtype-status").value || null }, label, date, note };
}
function isChanged() { return Object.keys(P.staged).length > 0 || P.removes.size > 0 || pickedStep() !== null || P.dateRemoves.size > 0 || pickedDate() !== null; }   // v17: dates
function isDirty() { return !!P && isChanged(); }
function panelSay(text) { byId("ep-msg").textContent = text; }

function fieldNote(f) {
  const v = P.row.v15, a = v.auto, st = P.staged[f];
  if (st === null) return f === "notes" ? "Your note is removed when you save." : "Goes back to the automatic value when you save.";
  if (f === "company") return `Changes the name shown on this dashboard only. New emails may still file under the old name, ${a.company}.`;
  if (f === "notes") return "Only you write notes; the sync never changes them.";
  if (f === "status" || f === "stage") {
    const from = `From the emails: ${f === "status" ? a.status_label || "none" : a.stage || "none"}${a.last ? ", " + a.last : ""}.`;
    if (typeof st === "string") return `${from} Your choice counts from today; a newer email takes over again.`;
    if (v.by[f] === "edit") return `${from} Showing your choice of ${v.edits[f + "_at"].slice(0, 10)}; a newer email takes over again.`;
    if (v.by[f] === "step") return `${from} Showing your newest step; a newer email takes over again.`;
    if (v.edits[f] != null) return `${from} Your choice of ${v.edits[f + "_at"].slice(0, 10)} is older, so the emails' value shows. Reset removes it.`;
    return from;
  }
  if (autoWhy(P.row, f)) return autoWhy(P.row, f);       // v18: where the automatic role or industry came from, under John's value too
  return typeof st === "string" || v.mine.includes(f) ? `Automatic: ${a[f] || "none"}.` : "";
}

function renderPanelSteps() {
  const picked = pickedStep(), ul = byId("ep-steps"), items = [
    ...P.row.v15.steps.map((s) => ({ ...s, saved: true })),
    ...(picked && typeof picked === "object" ? [{ ...picked, picked: true }] : []),   // shown at once, so it is clear what Save will record
  ].sort((x, y) => (x.date < y.date ? -1 : x.date > y.date ? 1 : 0));
  ul.textContent = "";
  if (!items.length) { const li = document.createElement("li"); li.className = "hint"; li.textContent = "No steps recorded yet."; ul.appendChild(li); return; }
  items.forEach((s) => {
    const li = document.createElement("li"), when = document.createElement("span"), what = document.createElement("span"), b = document.createElement("button");
    when.className = "when"; when.textContent = s.date;
    what.className = "what"; what.textContent = s.label;
    if (s.note) { const n = document.createElement("small"); n.textContent = s.note; what.appendChild(n); }
    if (s.picked) { const t = document.createElement("small"); t.className = "tag-new"; t.textContent = "not saved yet: Save records it"; what.appendChild(t); }
    const removing = s.saved && P.removes.has(s.id);
    li.classList.toggle("removing", removing);
    b.type = "button"; b.className = "btn btn-sm btn-drop"; b.textContent = removing ? "Undo" : s.picked ? "Don't add" : "Remove";
    b.addEventListener("click", () => {
      if (s.saved) { if (removing) P.removes.delete(s.id); else P.removes.add(s.id); } else { byId("ep-step-type").value = ""; byId("ep-step-note").value = ""; }
      refreshPanel();
    });
    li.append(when, what, b);
    ul.appendChild(li);
  });
}

// "Changes you made" (read only): the edit log the server sent, newest first, as "Field: from → to"
const CHANGE_FIELD = { company: "Company", role: "Role", industry: "Industry", status: "Status", stage: "Stage", notes: "Notes",
  "step removed": "Step removed", "step type added": "New step type", "date added": "Upcoming date added", "date removed": "Upcoming date removed" };   // v17: dates
const cut = (s, n = 60) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
function changeText(c) {
  const name = CHANGE_FIELD[c.field] || c.field, one = c.field.startsWith("step") || c.field.startsWith("date ");   // v17: dates
  const val = (v) => (v == null ? (c.field === "notes" ? "no note" : "automatic") : c.field === "status" ? statusName(v) : v === "" ? "blank" : v);
  return { name, text: one ? (c.old ?? c.new ?? "") : `${val(c.old)} → ${val(c.new)}` };
}
function renderPanelChanges() {
  const ul = byId("ep-changes"), list = P.row.v15.changes || [];
  ul.textContent = "";
  if (!list.length) { const li = document.createElement("li"); li.className = "hint"; li.textContent = "No changes yet."; ul.appendChild(li); return; }
  list.forEach((c) => {
    const li = document.createElement("li"), when = document.createElement("span"), what = document.createElement("span"), b = document.createElement("b");
    const t = changeText(c);
    when.className = "when"; when.textContent = String(c.at || "").slice(0, 16).replace("T", " ");
    what.className = "what"; what.title = `${t.name}: ${t.text}`;
    b.textContent = `${t.name}: `;
    what.append(b, cut(t.text));
    li.append(when, what);
    ul.appendChild(li);
  });
}

function refreshPanel() {
  const v = P.row.v15;
  PANEL_FIELDS.forEach((f) => {
    const st = P.staged[f], box = fieldBox(f);
    box.querySelector(".mine-dot").hidden = !(typeof st === "string" || (st === undefined && v.mine.includes(f)));
    box.querySelector(".ep-reset").hidden = !(typeof st === "string" || (st === undefined && v.edits[f] != null));
    box.querySelector(".ep-note").textContent = fieldNote(f);
  });
  const sel = byId("ep-step-type").value;
  byId("ep-newtype").hidden = sel !== "new";
  byId("ep-step-date").disabled = byId("ep-step-note").disabled = !sel;
  const step = pickedStep(), save = byId("ep-save");             // Save names the step it will record, next to the button
  save.textContent = step && typeof step === "object" ? `Save and record ${cut(step.label, 30)}` : "Save";
  save.title = step && typeof step === "object" ? `Save and record ${step.label} on ${step.date}` : "";
  saveNamesDate(step, save);                              // v17: and the date it will add
  renderPanelSteps();
  renderPanelDates();                                     // v17
}

function openPanel(id, focus) {
  hideHover();                                            // v16: the board's hover card goes when the panel opens
  if (P && P.row.id === id) return;
  if (isDirty()) { askDiscard(); return; }
  const row = DATA.pipeline.find((x) => x.id === id);
  if (!row) return;
  P = { row, staged: {}, removes: new Set(), dateRemoves: new Set() };   // v17: dateRemoves
  byId("ep-title").textContent = row.company;
  byId("ep-sub").textContent = row.role || "no role yet";
  const types = byId("ep-step-type");
  types.textContent = "";
  types.add(new Option("Nothing new to record", ""));
  DATA.step_types.forEach((t) => types.add(new Option(t.label + (t.id === row.v15.next ? " (likely next)" : ""), String(t.id))));
  types.add(new Option("New step type…", "new"));
  types.value = "";
  const sets = byId("ep-newtype-status");
  sets.textContent = "";
  sets.add(new Option("No, it only sets the Stage text", ""));
  DATA.status_choices.forEach(([s, label]) => sets.add(new Option(`Yes, to ${label}`, s)));
  byId("ep-newtype-label").value = "";
  byId("ep-step-date").value = byId("ep-step-date").max = todayISO();
  byId("ep-step-note").value = "";
  resetDateFields();                                      // v17: Upcoming dates starts empty, on Interview
  const st = fieldInput("status");
  st.textContent = "";
  DATA.status_choices.forEach(([s, label]) => st.add(new Option(label, s)));
  if (row.status && !DATA.status_choices.some((c) => c[0] === row.status)) st.add(new Option(row.status, row.status));
  const dl = byId("ep-industries");
  dl.textContent = "";
  [...new Set(DATA.pipeline.map((r) => r.industry))].filter((i) => i !== "Not recorded").sort().forEach((i) => { const o = document.createElement("option"); o.value = i; dl.appendChild(o); });
  PANEL_FIELDS.forEach((f) => { fieldInput(f).value = shownValue(f) ?? ""; });
  panelSay("");
  byId("ep-discard").hidden = true;
  byId("ep-save").disabled = false;
  refreshPanel();
  renderPanelChanges();
  byId("panel-scrim").classList.add("visible");
  byId("edit-panel").inert = false;                       // v16: see closePanel
  byId("edit-panel").hidden = false;
  // the cursor never starts in "What did you do?": a stray key there picks a step (L.E.K., 27 Oct). The close button ignores letters.
  (focus && PANEL_FIELDS.includes(focus) ? fieldInput(focus) : byId("ep-x")).focus();
}

function onFieldInput(f) {
  const val = fieldInput(f).value, same = (x, y) => String(x ?? "").trim() === String(y ?? "").trim();
  if (P.staged[f] === null && same(val, autoValue(f))) return refreshPanel();    // still "back to automatic"
  if (P.staged[f] !== null && same(val, shownValue(f))) delete P.staged[f]; else P.staged[f] = val;
  refreshPanel();
}
function resetField(f) {
  if (P.row.v15.edits[f] != null) P.staged[f] = null; else delete P.staged[f];
  fieldInput(f).value = P.staged[f] === null ? autoValue(f) : shownValue(f) ?? "";
  refreshPanel();
}

async function savePanel() {
  const step = pickedStep();
  if (typeof step === "string") return panelSay(step);
  const due = pickedDate();                               // v17: a half typed upcoming date blocks Save with a plain message
  if (typeof due === "string") return panelSay(due);
  if (!isChanged()) return closePanel();
  const body = { id: P.row.id, rev: P.row.v15.rev, fields: P.staged, remove_steps: [...P.removes],
    add_steps: step ? [{ ...(step.new_type ? { new_type: step.new_type } : { type_id: step.type_id }), date: step.date, note: step.note || null }] : [] };
  const save = byId("ep-save");
  save.disabled = true;                                   // a double click posts once
  const dated = !!due || P.dateRemoves.size > 0;          // v17: a date added or removed saves through /api/v17/save, with everything else, in one go;
  const res = dated ? await post("/api/v17/save", { ...body, add_dates: due ? [due] : [], remove_dates: [...P.dateRemoves] })   // any other Save is v16's
    : await post("/api/v15/save", body);
  if (!res.ok) {
    panelSay(res.error || "unknown error");
    if (STALE.includes(res.code)) { P.staged = {}; P.removes.clear(); byId("ep-step-type").value = ""; P.dateRemoves.clear(); resetDateFields(); reloadKeepScroll(); }   // nothing was saved; close and open it again (v17: dates too)
    else save.disabled = false;                           // busy or refused input: it can be pressed again
    return;
  }
  closePanel();
  await reloadKeepScroll();
}

function askDiscard() { byId("ep-discard").hidden = false; byId("ep-discard-no").focus(); }
function requestClose() { if (!P) return; if (isDirty()) askDiscard(); else closePanel(); }
function closePanel() {
  P = null;
  byId("edit-panel").inert = true;                        // v16: it slides out for a fifth of a second; nothing in it can be pressed meanwhile
  byId("edit-panel").hidden = true;
  byId("panel-scrim").classList.remove("visible");
}
// the application row an event happened on, or null where the browser's own behaviour must win (links, inputs, buttons, empty space)
function appRow(e) {
  if (!DATA || e.target.closest("a, input, textarea, select, button, label, .badge-checks, .edit-panel")) return null;
  const row = e.target.closest("[data-app]");
  return row && DATA.pipeline.some((x) => x.id === +row.dataset.app) ? row : null;
}
function selectionIn(row) {
  const s = window.getSelection();
  return !!s && !s.isCollapsed && s.toString().trim() !== "" && row.contains(s.anchorNode);
}
function wirePanel() {
  document.addEventListener("contextmenu", (e) => {     // right click, and Control click on a Mac
    const row = appRow(e);
    if (!row || selectionIn(row)) return;               // selected text keeps the browser's menu (Copy)
    e.preventDefault();
    openPanel(+row.dataset.app);
  });
  document.addEventListener("dblclick", (e) => {
    const row = appRow(e);
    if (!row) return;
    window.getSelection().removeAllRanges();
    openPanel(+row.dataset.app);
  });
  document.addEventListener("click", (e) => {
    const o = e.target.closest("[data-app-open]");
    if (o) return openPanel(+o.dataset.appOpen, o.dataset.field);
    if (e.ctrlKey) { const row = appRow(e); if (row) { e.preventDefault(); openPanel(+row.dataset.app); } }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !P) return;
    e.preventDefault();
    if (!byId("ep-discard").hidden) byId("ep-discard").hidden = true; else requestClose();
  });
  byId("panel-scrim").addEventListener("click", requestClose);
  byId("ep-x").addEventListener("click", requestClose);
  byId("ep-cancel").addEventListener("click", requestClose);
  byId("ep-save").addEventListener("click", savePanel);
  byId("ep-discard-yes").addEventListener("click", closePanel);
  byId("ep-discard-no").addEventListener("click", () => { byId("ep-discard").hidden = true; });
  ["ep-step-type", "ep-step-date", "ep-step-note", "ep-newtype-label", "ep-newtype-status"].forEach((id) =>
    byId(id).addEventListener("input", () => refreshPanel()));
  PANEL_FIELDS.forEach((f) => {
    fieldInput(f).addEventListener("input", () => onFieldInput(f));
    fieldBox(f).querySelector(".ep-reset").addEventListener("click", () => resetField(f));
  });
}

/* ------------------------------------------------------------- the board (v16) */
// Kanban view of Pipeline. Its rows are the list's rows that the list's own filters left visible, so both views always show the same set.
// One column per status name, in the Status filter's order, then any other name the list shows. A drop saves exactly what the edit panel saves
// when only Status changes (/api/v15/save); Undo puts back the earlier value with its date (/api/v16/undo_move). Text goes in as text, never as HTML.
const BOARD_CAP = 25, START_COLLAPSED = ["Rejected", "Withdrawn"];
const store = {                                           // the browser's memory of the switch and the collapsed columns; the page works without it
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* not remembered, nothing else changes */ } },
};
let VIEW = store.get("jt.v16.view") === "kanban" ? "kanban" : "normal";
let COLLAPSED = (() => { try { return JSON.parse(store.get("jt.v16.collapsed")) || {}; } catch (e) { return {}; } })();
let boardMore = {}, dragId = null, moving = false, LAST_MOVE = null, barTimer = null;
let JUST = null, SETTLE = null;                           // the column just opened or closed, the card just moved: animated once, on the next drawing
// scrolls the page makes by itself are smooth, unless the computer asks for less motion
function smooth() { try { return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth"; } catch (e) { return "auto"; } }
const isCollapsed = (label) => (own(COLLAPSED, label) ? !!COLLAPSED[label] : START_COLLAPSED.includes(label));
const activity = (r) => (/^\d{4}-\d{2}-\d{2}/.test(r.last) ? r.last : "");            // "—" (no date) sorts last
const byActivity = (a, b) => (activity(b) > activity(a)) - (activity(b) < activity(a)) || a.company.localeCompare(b.company) || a.id - b.id;
// the status a drop on a column sets: the first of the panel's statuses with the column's name ("Interview" -> "Interview (invited)"); none = not a drop target
function dropStatus(label) {
  const c = DATA.status_choices.find(([, l]) => l === label || l.startsWith(label + " ("));
  return c ? c[0] : null;
}
// [{label, cat, status, rows}]: every status name any application has gets a column, so no row is ever left out
function boardColumns(rows) {
  const names = [...new Set(DATA.pipeline.map((r) => r.status_label))];
  const labels = [...DATA.status_filter, ...names.filter((l) => !DATA.status_filter.includes(l)).sort()];
  return labels.map((label) => {
    const any = DATA.pipeline.find((r) => r.status_label === label);
    return { label, cat: any ? any.cat : "applied", status: dropStatus(label), rows: rows.filter((r) => r.status_label === label).sort(byActivity) };
  });
}
function listedRows() {
  const shown = new Set([...document.querySelectorAll("#pipeline-tbody tr")].filter((tr) => !tr.classList.contains("hidden-row")).map((tr) => +tr.dataset.app));
  return DATA.pipeline.filter((r) => shown.has(r.id));
}
const textEl = (tag, cls, s) => { const e = document.createElement(tag); e.className = cls; e.textContent = s; return e; };

function renderBoard() {
  const board = byId("board"), kanban = VIEW === "kanban";
  byId("pipeline").classList.toggle("kanban", kanban);
  document.querySelectorAll(".vs-opt").forEach((b) => { b.classList.toggle("active", b.dataset.view === VIEW); b.setAttribute("aria-pressed", String(b.dataset.view === VIEW)); });
  const scroll = {};                                      // each column keeps its scroll position when the board is drawn again
  board.querySelectorAll(".col").forEach((c) => { const l = c.querySelector(".col-cards"); if (l) scroll[c.dataset.label] = l.scrollTop; });
  hideHover();
  board.textContent = "";
  if (!kanban || !DATA) return;
  const quiet = new Map(DATA.stale.map((s) => [s.app_id, s]));
  boardColumns(listedRows()).forEach((col) => {
    const el = columnEl(col, quiet), l = el.querySelector(".col-cards");
    board.appendChild(el);
    if (l && scroll[col.label]) l.scrollTop = scroll[col.label];   // put back as it was, at once (not an animated scroll)
  });
  const settled = SETTLE !== null && board.querySelectorAll(".kcard.settle")[0];
  if (settled) settled.scrollIntoView({ block: "nearest", inline: "nearest", behavior: smooth() });   // the moved card, brought into view
  JUST = SETTLE = null;
}

function columnEl(col, quiet) {
  const el = document.createElement("section"), n = col.rows.length, name = col.label || "No status";
  el.className = "col";
  el.dataset.label = col.label;
  if (JUST && JUST.label === col.label) el.classList.add(JUST.on ? "shutting" : "opening");
  if (isCollapsed(col.label)) {                           // a thin strip with the name and count; a click expands it
    el.classList.add("collapsed");
    const strip = document.createElement("button");
    strip.type = "button"; strip.className = "col-strip"; strip.title = `Expand ${name} (${n})`;
    strip.append(textEl("span", "col-count", String(n)), textEl("span", "col-name", name));
    strip.addEventListener("click", () => setCollapsed(col.label, false));
    el.appendChild(strip);
    return el;
  }
  if (col.status) el.dataset.drop = col.status;           // only an expanded column of a status the panel offers takes a drop
  const head = document.createElement("div"), hide = textEl("button", "col-hide", "–"), list = document.createElement("div");
  head.className = "col-head"; list.className = "col-cards";
  hide.type = "button"; hide.title = `Collapse ${name}`; hide.setAttribute("aria-label", `Collapse ${name}`);
  hide.addEventListener("click", () => setCollapsed(col.label, true));
  head.append(textEl("span", `pill pill-${col.cat}`, name), textEl("span", "col-count", String(n)), hide);
  const cap = boardMore[col.label] || BOARD_CAP;
  if (!n) list.appendChild(textEl("div", "col-empty", "Nothing here"));
  col.rows.slice(0, cap).forEach((r) => list.appendChild(cardEl(r, quiet.get(r.id))));
  if (n > cap) {
    const more = textEl("button", "btn btn-sm col-more", "Show 25 more");
    more.type = "button"; more.title = `${n - cap} more in ${name}`;
    more.addEventListener("click", () => { boardMore[col.label] = cap + BOARD_CAP; renderBoard(); });
    list.appendChild(more);
  }
  el.append(head, list);
  return el;
}

// one card: company (a dot if John edited anything), role or "add role", stage, last activity, industry if known, Gone quiet if the server says so
function cardEl(r, q) {
  const c = document.createElement("article"), top = document.createElement("div"), foot = document.createElement("div");
  const line = (cls, s) => { const e = textEl("div", cls, s); e.title = s; return e; };
  c.className = r.id === SETTLE ? "kcard settle" : "kcard"; c.tabIndex = 0; c.draggable = true;
  c.dataset.app = r.id; c.dataset.appOpen = r.id;          // a click, right click, Control click or double click opens the panel (wirePanel)
  top.className = "kc-top"; foot.className = "kc-foot";
  const co = textEl("span", "kc-company", r.company); co.title = r.company;
  top.appendChild(co);
  if (r.v15 && r.v15.mine.length) { const d = textEl("span", "mine-dot", ""); d.title = "Your edit"; top.appendChild(d); }
  if (q) { const b = textEl("span", "kc-quiet", "Gone quiet"); b.title = `${q.days} days since ${q.last}`; top.appendChild(b); }
  let role;
  if (r.role) { role = line("kc-line", r.role); role.title = autoTip(r, "role") || r.role; }   // v18: tooltip
  else { role = textEl("button", "add-role", "add role"); role.type = "button"; role.dataset.appOpen = r.id; role.dataset.field = "role"; }
  const last = textEl("span", "", r.last); last.title = "Last activity";
  foot.appendChild(last);
  if (r.industry && r.industry !== "Not recorded") { const i = line("kc-line", r.industry); i.title = autoTip(r, "industry") || r.industry; foot.appendChild(i); }   // v18
  c.append(top, role, line("kc-line", r.stage), foot);
  return c;
}

function setView(v) { VIEW = v === "kanban" ? "kanban" : "normal"; store.set("jt.v16.view", VIEW); renderBoard(); }
function setCollapsed(label, on) { COLLAPSED[label] = on; JUST = { label, on }; store.set("jt.v16.collapsed", JSON.stringify(COLLAPSED)); renderBoard(); }
const colOf = (t) => (t && t.nodeType === 3 ? t.parentElement : t)?.closest?.(".col") || null;
function canDrop(col, id) {
  const r = id == null ? null : DATA.pipeline.find((x) => x.id === id);
  return !!(r && col && col.dataset.drop && col.dataset.label !== r.status_label);
}
function endDrag() {
  const board = byId("board");
  board.classList.remove("dragging");
  board.querySelectorAll(".col").forEach((c) => { c.classList.remove("drop-ok"); c.classList.remove("drop-here"); });
  dragId = null;
}
// a drop on a column that is not outlined does nothing; a drop on an outlined one always sends one move, or says why not
function onBoardDrop(e) {
  const col = colOf(e.target), id = dragId;
  endDrag();
  if (!canDrop(col, id)) return;
  e.preventDefault();
  moveCard(id, col.dataset.drop, col.dataset.label);
}

function barSay(msg, undo = false) {                     // the bar at the bottom, for 10 seconds; Undo only while it shows
  if (!undo) LAST_MOVE = null;
  byId("move-bar-text").textContent = msg;
  byId("move-bar-undo").hidden = !undo;
  byId("move-bar").hidden = false;
  clearTimeout(barTimer);
  barTimer = setTimeout(() => { byId("move-bar").hidden = true; LAST_MOVE = null; }, 10000);
}
async function moveCard(id, status, label) {
  const r = DATA.pipeline.find((x) => x.id === id);
  if (moving) return barSay("Still saving the last move. Nothing else was moved; try again in a moment.");
  if (!r) return barSay("That application is no longer on the page. Nothing was moved.");
  moving = true;
  const before = { status: r.v15.edits.status ?? null, status_at: r.v15.edits.status_at ?? null }, company = r.company;
  let res;
  try { res = await post("/api/v15/save", { id, rev: r.v15.rev, fields: { status }, remove_steps: [], add_steps: [] }); } finally { moving = false; }
  if (!res.ok) {                                          // the card stays where it was
    barSay(`${company} was not moved: ${res.error || "unknown error"}`);
    if (STALE.includes(res.code)) reloadKeepScroll();
    return;
  }
  SETTLE = id;
  await reloadKeepScroll();
  LAST_MOVE = { id, company, before };
  barSay(`Moved ${company} to ${label}.`, true);
}
async function undoMove() {
  const m = LAST_MOVE, r = m && DATA.pipeline.find((x) => x.id === m.id);
  LAST_MOVE = null;                                       // a double click undoes once
  if (!m) return;
  if (!r) return barSay("That application is no longer on the page. Nothing was changed.");
  const res = await post("/api/v16/undo_move", { id: m.id, rev: r.v15.rev, status: m.before.status, status_at: m.before.status_at });
  if (!res.ok) {
    if (res.busy) LAST_MOVE = m;                          // busy: Undo can be pressed again
    barSay(`Undo did not work: ${res.error || "unknown error"}`, !!res.busy);
    if (STALE.includes(res.code)) reloadKeepScroll();
    return;
  }
  SETTLE = m.id;
  await reloadKeepScroll();
  const now = DATA.pipeline.find((x) => x.id === m.id);
  barSay(`Undone: ${m.company} is back in ${now ? now.status_label : "its earlier status"}.`);
}

// The hover card: what the page already has about one application, beside the card the pointer rests on. It reads only the row, the Gone quiet
// list and the dates and names of Timeline (never an email's subject, text or sender: no detail, subject or sender field is read). Text only.
const HOVER = { card: null, timer: null }, HOVER_MS = 330;
function hideHover() {
  if (HOVER.timer) { clearTimeout(HOVER.timer); HOVER.timer = null; }
  HOVER.card = null;
  const h = byId("kc-hover");
  if (!h.hidden) h.hidden = true;
}
function ago(day) {
  const n = Math.round((Date.parse(DATA.today) - Date.parse(day)) / 864e5);
  return n === 0 ? "today" : n === 1 ? "yesterday" : n > 1 ? `${n} days ago` : "after today";
}
function hoverParts(r) {
  const v = r.v15 || { mine: [], notes: "" }, q = DATA.stale.find((s) => s.app_id === r.id);
  const rows = [["Status", r.status_label || "—"], ["Stage", r.stage || "—"], ["Last activity", activity(r) ? `${r.last}, ${ago(r.last)}` : "none recorded"]];
  if (q) rows.push(["Gone quiet", `quiet for ${plural(q.days, "day")}, since ${q.last}`, "hv-quiet"]);
  if (v.mine.length) rows.push(["You edited", v.mine.map((f) => CHANGE_FIELD[f] || f).join(", ")]);
  const notes = String(v.notes || "").split("\n").map((l) => l.trim()).filter(Boolean).slice(0, 2);
  const latest = DATA.timeline.filter((t) => t.app_id === r.id).slice(-3).reverse()      // Timeline is in date order: the newest three
    .map((t) => [t.date, t.source === "you" ? `${t.status} (you)` : t.status]);
  return { title: r.company, role: r.role || "No role yet", rows, notes, latest };
}
function showHover(card) {
  HOVER.timer = null;
  const r = DATA && DATA.pipeline.find((x) => x.id === +card.dataset.app);
  if (HOVER.card !== card || !r || P || dragId !== null) return;      // gone meanwhile, the panel is open, or a drag started
  const h = byId("kc-hover"), x = hoverParts(r), line = (cls, s) => { const e = textEl("div", cls, s); h.appendChild(e); return e; };
  h.textContent = "";
  line("hv-title hv-line", x.title);
  line(r.role ? "hv-line" : "hv-line hv-faint", x.role);
  x.rows.forEach(([k, val, cls]) => { const e = line(`hv-row ${cls || ""}`.trim(), ""); e.append(textEl("span", "", k), textEl("span", "", val)); });
  if (x.notes.length) { line("hv-sub", "Your notes"); x.notes.forEach((n) => line("hv-line", n)); }
  if (x.latest.length) { line("hv-sub", "Latest"); x.latest.forEach(([d, l]) => { const e = line("hv-ev", ""); e.append(textEl("span", "", d), textEl("span", "", l)); }); }
  h.hidden = false;
  const a = card.getBoundingClientRect(), w = h.offsetWidth, ht = h.offsetHeight, W = window.innerWidth, H = window.innerHeight;
  const right = a.right + 10, left = a.left - 10 - w;                  // beside the card: right if it fits, else left; always inside the window
  h.style.left = `${Math.max(8, Math.min(right + w <= W - 8 ? right : left, W - w - 8))}px`;
  h.style.top = `${Math.max(8, Math.min(a.top, H - ht - 8))}px`;
}

function wireBoard() {
  document.querySelectorAll(".vs-opt").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
  const board = byId("board");
  board.addEventListener("dragstart", (e) => {
    hideHover();
    const card = e.target.closest && e.target.closest(".kcard");
    if (!card) return;
    dragId = +card.dataset.app;
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", String(dragId));
    board.classList.add("dragging");
    board.querySelectorAll(".col").forEach((c) => c.classList.toggle("drop-ok", canDrop(c, dragId)));
  });
  board.addEventListener("dragover", (e) => {
    const col = colOf(e.target);
    if (!canDrop(col, dragId)) return;                    // not a drop target: the browser shows "no drop" and nothing happens
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    board.querySelectorAll(".col.drop-here").forEach((c) => { if (c !== col) c.classList.remove("drop-here"); });
    col.classList.add("drop-here");
  });
  board.addEventListener("drop", onBoardDrop);
  board.addEventListener("dragend", endDrag);
  board.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.classList.contains("kcard")) { e.preventDefault(); openPanel(+e.target.dataset.app); }
  });
  // the board has no scrollbars: an up-and-down wheel over a column title or a collapsed strip moves the board sideways (a trackpad swipes sideways anywhere)
  let wheelTo = 0, wheelAt = 0;
  board.addEventListener("wheel", (e) => {
    if (!e.target.closest(".col-head, .col-strip") || Math.abs(e.deltaX) >= Math.abs(e.deltaY) || board.scrollWidth <= board.clientWidth) return;
    e.preventDefault();
    const now = Date.now(), from = now - wheelAt < 300 ? wheelTo : board.scrollLeft;   // quick turns add up while the smooth scroll is under way
    wheelTo = Math.max(0, Math.min(from + e.deltaY, board.scrollWidth - board.clientWidth));
    wheelAt = now;
    board.scrollTo({ left: wheelTo, behavior: smooth() });
  }, { passive: false });
  board.addEventListener("mouseover", (e) => {            // the pointer rests on a card for a third of a second: its hover card
    const card = e.target.closest && e.target.closest(".kcard");
    if (!card || card === HOVER.card) return;
    hideHover();
    HOVER.card = card;
    HOVER.timer = setTimeout(() => showHover(card), HOVER_MS);
  });
  board.addEventListener("mouseout", (e) => { if (HOVER.card && !(e.relatedTarget && HOVER.card.contains(e.relatedTarget))) hideHover(); });
  ["mousedown", "keydown", "contextmenu"].forEach((t) => document.addEventListener(t, () => { if (HOVER.card) hideHover(); }, true));
  window.addEventListener("scroll", () => { if (HOVER.card) hideHover(); }, { capture: true, passive: true });   // any scroll: page, board or column
  byId("move-bar-undo").addEventListener("click", undoMove);
}

/* ------------------------------------------------------------ static wiring */
function wireStatic() {
  const heading = document.getElementById("page-heading");
  document.querySelectorAll(".nav-item").forEach((item) => item.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    item.classList.add("active");
    document.getElementById(item.dataset.tab).classList.add("active");
    heading.textContent = item.querySelector(".nav-label").textContent;
  }));
  document.querySelectorAll(".theme-opt").forEach((opt) => opt.addEventListener("click", () => {
    document.querySelectorAll(".theme-opt").forEach((o) => o.classList.remove("active"));
    opt.classList.add("active");
    document.documentElement.setAttribute("data-theme", opt.dataset.theme);
  }));
  document.getElementById("pipeline-search").addEventListener("input", applyPipelineFilters);
  document.getElementById("pipeline-status-filter").addEventListener("change", applyPipelineFilters);
  document.getElementById("pipeline-industry-filter").addEventListener("change", applyPipelineFilters);
  document.querySelectorAll("#timeline-controls .chip").forEach((chip) => chip.addEventListener("click", () => {
    document.querySelectorAll("#timeline-controls .chip").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    const f = chip.dataset.tlFilter;
    document.querySelectorAll(".timeline-point").forEach((p) =>
      { p.style.display = (f === "all" || p.dataset.cat === CHIP_TO_CAT[f]) ? "" : "none"; });
  }));
  document.getElementById("cal-prev").addEventListener("click", () => { if (--calM < 0) { calM = 11; calY--; } renderCalendar(); });
  document.getElementById("cal-next").addEventListener("click", () => { if (++calM > 11) { calM = 0; calY++; } renderCalendar(); });
  document.getElementById("dlg-cancel").addEventListener("click", () => document.getElementById("dialog-overlay").classList.remove("visible"));
  document.getElementById("dlg-save").addEventListener("click", commitDialog);
  document.getElementById("dialog-overlay").addEventListener("click", (e) => {
    if (e.target.id === "dialog-overlay") e.target.classList.remove("visible");
  });
  document.getElementById("dlg2-cancel").addEventListener("click", closeDialog2);
  document.getElementById("dlg2-ok").addEventListener("click", commitDialog2);
  document.getElementById("dlg2-check").addEventListener("change", syncCheckLines);
  document.getElementById("dialog2-overlay").addEventListener("click", (e) => { if (e.target.id === "dialog2-overlay") closeDialog2(); });
  document.getElementById("confirm-all-btn").addEventListener("click", openConfirmAllDialog);
  document.addEventListener("click", (e) => {            // the content checks badge: click opens its list, click elsewhere closes it
    const b = e.target.closest(".badge-checks");
    document.querySelectorAll(".badge-checks.open").forEach((x) => { if (x !== b) x.classList.remove("open"); });
    if (b) { b.classList.toggle("open"); if (b.classList.contains("open")) placePop(b); }
  });
  window.addEventListener("scroll", () => document.querySelectorAll(".badge-checks.open").forEach((x) => x.classList.remove("open")), true);
}
// the checks list is position:fixed (the tables clip anything that sticks out of them), so it is placed from the badge: below it, or above if there is no room
function placePop(b) {
  const pop = b.querySelector(".pop"), r = b.getBoundingClientRect();
  pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - pop.offsetWidth - 8)) + "px";
  pop.style.top = (r.bottom + pop.offsetHeight + 12 > window.innerHeight ? Math.max(8, r.top - pop.offsetHeight - 6) : r.bottom + 6) + "px";
}

/* ------------------------------------------------------------- the calendar (v17) */
// Timeline's Calendar view, the Coming up strip and the panel's Upcoming dates. The calendar's events are Timeline's own list (DATA.timeline)
// under the same chip filter (timelineShown), so both views always show the same events; an application counts once per type per day.
// Follow up dates use the Follow-up planning calendar's rule (a Gone quiet application's last signal + 7 days). Today and every date are the
// browser's local date; dates from the database are plain text and are never converted. Text goes in as text, never as HTML.
const STRIP_LEN = 5, DAY_CAP = 25;
const TYPES = [   // [key, name, word in a count]: the order of the legend, the totals line and the day list; each key has its dot (.cd-key) in index_v17.html
  ["applied", "Applied", "applied"], ["intass", "Interview or assessment", "interview or assessment"], ["offer", "Offer", "offer"],
  ["rejected", "Rejected", "rejected"], ["withdrawn", "Withdrawn", "withdrawn"], ["other", "Other step", "other step"], ["follow", "Follow up", "follow up"],
  ["mine", "Your date", "of your dates"]];
const MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
let UP = [], KINDS = ["Interview"], TLVIEW = store.get("jt.v17.tlview") === "calendar" ? "calendar" : "list";
let CAL = null, CAL_DAY = null, DAY_MORE = DAY_CAP, STRIP_ALL = false;

async function loadUpcoming() {                           // never throws: without the dates the page still works (the strip and rings are just empty)
  try {
    const u = await (await fetch("/api/v17/upcoming")).json();
    UP = Array.isArray(u.upcoming) ? u.upcoming : [];
    if (Array.isArray(u.kinds) && u.kinds.length) KINDS = u.kinds;
  } catch (e) { UP = []; }
}
const pad2 = (n) => String(n).padStart(2, "0");
const isoOf = (d) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;              // the local date, never UTC
const ymd = (iso) => iso.split("-").map(Number);
const dayNum = (iso) => { const [y, m, d] = ymd(iso); return Date.UTC(y, m - 1, d) / 864e5; };        // whole calendar days, not 24 hour chunks
function dayShort(iso) { const [y, m, d] = ymd(iso); return `${d} ${MONTHS[m - 1]}${y === new Date().getFullYear() ? "" : " " + y}`; }
function dayLong(iso) { const [y, m, d] = ymd(iso); return `${WEEKDAYS[new Date(y, m - 1, d).getDay()]} ${d} ${MONTH_NAMES[m - 1]} ${y}`; }
function howFar(iso) { const n = dayNum(iso) - dayNum(todayISO()); return n === 0 ? "Today" : n === 1 ? "Tomorrow" : `in ${n} days`; }

function tlFilter() { const c = document.querySelector("#timeline-controls .chip.active"); return (c && c.dataset.tlFilter) || "all"; }
// the events Timeline's list shows now: v16's list and v16's chip rule (wireStatic), shared by both views
const timelineShown = () => { const f = tlFilter(); return DATA.timeline.filter((e) => f === "all" || e.cat === CHIP_TO_CAT[f]); };
// the calendar type of a Timeline event: its colour on the Timeline (cat), with Offer told apart from Interview by its name
function typeOf(e) {
  if (e.cat === "assessment") return "intass";
  if (e.cat === "interview") {
    const offer = e.source === "you" ? (DATA.step_types.find((t) => t.label === e.status) || {}).sets_status === "offer" : e.status === "Offer";
    return offer ? "offer" : "intass";
  }
  return ["applied", "rejected", "withdrawn"].includes(e.cat) ? e.cat : "other";
}
// the Follow-up planning calendar's rule (flaggedDates): a Gone quiet application's last signal + 7 days, counted on the local calendar
function followUps() {
  return DATA.stale.filter((s) => /^\d{4}-\d{2}-\d{2}$/.test(s.last)).map((s) => { const [y, m, d] = ymd(s.last); return { iso: isoOf(new Date(y, m - 1, d + 7)), s }; });
}
// date -> type -> application -> {app, type, labels}: what the dots count (each application once per type per day) and the day list shows
function calDays() {
  const days = new Map();
  const put = (iso, type, app, label) => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(iso)) return;
    const d = days.get(iso) || days.set(iso, new Map()).get(iso), t = d.get(type) || d.set(type, new Map()).get(type);
    (t.get(app) || t.set(app, { app, type, labels: new Set() }).get(app)).labels.add(label);
  };
  timelineShown().forEach((e) => put(String(e.date).slice(0, 10), typeOf(e), e.app_id, e.status));
  if (tlFilter() === "all") {                             // not Timeline events: shown with All events only (the legend says so)
    followUps().forEach(({ iso, s }) => put(iso, "follow", s.app_id, `Quiet since ${dayShort(s.last)}`));
    UP.forEach((u) => put(u.date, "mine", u.app_id, [u.kind, u.time, u.note].filter(Boolean).join(" · ")));
  }
  return days;
}

function setTlView(v) { TLVIEW = v === "calendar" ? "calendar" : "list"; store.set("jt.v17.tlview", TLVIEW); renderTlCal(); }
function moveMonth(n) { const d = new Date(CAL.y, CAL.m + n, 1); CAL = { y: d.getFullYear(), m: d.getMonth() }; CAL_DAY = null; renderTlCal(); }
function renderTlCal() {
  renderComingUp();
  byId("timeline").classList.toggle("cal", TLVIEW === "calendar");
  document.querySelectorAll("[data-tlview]").forEach((b) => { const on = b.dataset.tlview === TLVIEW; b.classList.toggle("active", on); b.setAttribute("aria-pressed", String(on)); });
  if (TLVIEW !== "calendar") return;
  if (!CAL) { const t = new Date(); CAL = { y: t.getFullYear(), m: t.getMonth() }; }
  const days = calDays(), today = todayISO(), grid = byId("tlc-grid"), total = {};
  byId("tlc-title").textContent = `${MONTH_NAMES[CAL.m]} ${CAL.y}`;
  grid.textContent = "";
  ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"].forEach((d) => grid.appendChild(textEl("div", "tlc-dow", d)));
  const start = new Date(CAL.y, CAL.m, 1).getDay(), n = new Date(CAL.y, CAL.m + 1, 0).getDate(), prevN = new Date(CAL.y, CAL.m, 0).getDate();
  for (let i = start - 1; i >= 0; i--) grid.appendChild(textEl("div", "tlc-cell out", String(prevN - i)));
  for (let d = 1; d <= n; d++) {
    const iso = `${CAL.y}-${pad2(CAL.m + 1)}-${pad2(d)}`, day = days.get(iso), b = document.createElement("button"), words = [];
    b.type = "button"; b.className = `tlc-cell${iso === today ? " today" : ""}${iso === CAL_DAY ? " sel" : ""}`; b.dataset.day = iso;
    b.appendChild(textEl("span", "tlc-n", String(d)));
    const dots = document.createElement("span");
    dots.className = "tlc-dots";
    TYPES.forEach(([k, name, word]) => {
      const c = day && day.get(k) ? day.get(k).size : 0;
      if (!c) return;
      total[k] = (total[k] || 0) + c;
      words.push(`${c} ${word}`);
      const s = document.createElement("span");
      s.className = "tlc-dot"; s.dataset.type = k; s.title = `${c} ${name}`;
      s.appendChild(textEl("i", `cd cd-${k}`, ""));
      if (c > 1) s.appendChild(textEl("b", "", String(c)));
      dots.appendChild(s);
    });
    b.appendChild(dots);
    b.setAttribute("aria-label", `${dayLong(iso)}${iso === today ? ", today" : ""}: ${words.join(", ") || "nothing"}`);
    grid.appendChild(b);
  }
  for (let d = 1; d <= (7 - ((start + n) % 7)) % 7; d++) grid.appendChild(textEl("div", "tlc-cell out", String(d)));
  byId("tlc-totals").textContent = TYPES.filter(([k]) => total[k]).map(([k, , word]) => `${total[k]} ${word}`).join(", ") || "Nothing this month.";
  const legend = byId("tlc-legend");
  legend.textContent = "";
  TYPES.forEach(([k, name]) => { const s = textEl("span", "tlc-key", name); s.prepend(textEl("i", `cd cd-${k}`, "")); legend.appendChild(s); });
  if (tlFilter() !== "all") legend.appendChild(textEl("span", "tlc-key hint", "Follow up and your dates show with All events."));
  renderDayList(days);
}
function renderDayList(days) {
  const box = byId("tlc-day"), day = CAL_DAY && days.get(CAL_DAY);
  box.textContent = "";
  box.hidden = !CAL_DAY;
  if (!CAL_DAY) return;
  box.appendChild(textEl("h3", "tlc-day-title", `${dayLong(CAL_DAY)}${CAL_DAY === todayISO() ? " (today)" : ""}`));
  const items = day ? TYPES.flatMap(([k]) => [...(day.get(k) || new Map()).values()]) : [];
  if (!items.length) { box.appendChild(textEl("div", "hint", "Nothing on this day.")); return; }
  let group = null;
  items.slice(0, DAY_MORE).forEach((it) => {
    const name = TYPES.find(([k]) => k === it.type)[1];
    if (it.type !== group) {
      group = it.type;
      const h = textEl("div", "tlc-group", `${name} (${day.get(group).size})`);
      h.prepend(textEl("i", `cd cd-${group}`, ""));
      box.appendChild(h);
    }
    box.appendChild(dayItem(it, name));
  });
  if (items.length > DAY_MORE) {
    const more = textEl("button", "btn btn-sm tlc-more", `Show ${Math.min(DAY_CAP, items.length - DAY_MORE)} more`);
    more.type = "button"; more.title = `${items.length - DAY_MORE} more on this day`;
    more.addEventListener("click", () => { DAY_MORE += DAY_CAP; renderTlCal(); });
    box.appendChild(more);
  }
}
// one line of the day list: the company (a button: Enter or a click opens the panel), the role or "add role", and what happened or is due
function dayItem(it, name) {
  const r = DATA.pipeline.find((x) => x.id === it.app) || { company: "—", stage: "—" }, row = document.createElement("div");
  row.className = "tlc-item"; row.dataset.appOpen = it.app;
  const co = textEl("button", "tlc-co", r.company);
  co.type = "button";
  let role;
  if (r.role) { role = textEl("span", "tlc-role", r.role); role.title = autoTip(r, "role"); }   // v18: tooltip
  else { role = textEl("button", "add-role", "add role"); role.type = "button"; role.dataset.appOpen = it.app; role.dataset.field = "role"; }
  const what = [...it.labels].filter((l) => l && l !== name);
  if (it.type !== "mine" && it.type !== "follow" && r.stage && r.stage !== "—") what.push(r.stage);
  row.append(co, role, textEl("span", "tlc-what", what.join(" · ")));
  return row;
}

// Coming up: the next upcoming dates from today (today included), soonest first; dates that have passed leave it (they stay on the calendar)
function renderComingUp() {
  const box = byId("comingup"), today = todayISO();
  const list = UP.filter((u) => u.date >= today && DATA.pipeline.some((r) => r.id === u.app_id))
    .sort((a, b) => (a.date + (a.time || "")).localeCompare(b.date + (b.time || "")) || a.id - b.id);
  box.textContent = "";
  box.hidden = !list.length;                              // no upcoming dates: no strip at all (Timeline as v16 showed it)
  if (!list.length) return;
  box.appendChild(textEl("span", "cu-title", "Coming up"));
  (STRIP_ALL ? list : list.slice(0, STRIP_LEN)).forEach((u) => {
    const r = DATA.pipeline.find((x) => x.id === u.app_id), b = document.createElement("button"), [y, m, d] = ymd(u.date);
    b.type = "button"; b.className = "cu-item"; b.dataset.appOpen = u.app_id;
    b.title = [u.kind, dayLong(u.date), u.time, r.company, r.role, u.note].filter(Boolean).join(" · ");
    b.append(textEl("span", "cu-when", `${WEEKDAYS[new Date(y, m - 1, d).getDay()].slice(0, 3)} ${dayShort(u.date)}${u.time ? " · " + u.time : ""}`),
      textEl("span", "cu-far", howFar(u.date)), textEl("span", "cu-what", [u.kind, r.company, r.role].filter(Boolean).join(" · ")));
    box.appendChild(b);
  });
  if (!STRIP_ALL && list.length > STRIP_LEN) {
    const more = textEl("button", "cu-more", `+${list.length - STRIP_LEN} more`);
    more.type = "button";
    more.addEventListener("click", () => { STRIP_ALL = true; renderComingUp(); });
    box.appendChild(more);
  }
}

// the panel's Upcoming dates: nothing is saved until Save, Save names what it adds, and a half typed date blocks Save with a plain message
const dueEl = (k) => byId(`ep-due-${k}`);
function resetDateFields() {
  const k = dueEl("kind");
  k.textContent = "";
  KINDS.forEach((x) => k.add(new Option(x, x)));
  k.value = KINDS[0];
  dueEl("date").value = dueEl("time").value = dueEl("note").value = "";
}
// the date typed in Upcoming dates: null when nothing is typed, else {kind, date, time, note}, or a sentence saying what is missing
function pickedDate() {
  const kind = dueEl("kind").value || KINDS[0], date = dueEl("date").value, time = dueEl("time").value, note = dueEl("note").value.trim();
  if ((dueEl("date").validity || {}).badInput) return "The upcoming date is not complete. Finish it or clear it.";
  if ((dueEl("time").validity || {}).badInput) return "The upcoming time is not complete. Finish it or clear it.";
  if (!date) return time || note || kind !== KINDS[0] ? `Pick a date for the ${kind}. To add no date, empty the time and note and set Kind back to ${KINDS[0]}.` : null;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || date < "2000-01-01" || date > "2099-12-31") return "The upcoming date is not a real date.";
  if (time && !/^([01]\d|2[0-3]):[0-5]\d$/.test(time)) return "The upcoming time is not a real time.";
  if (UP.some((u) => u.app_id === P.row.id && !P.dateRemoves.has(u.id) && u.kind === kind && u.date === date)) return `This application already has ${kind} on ${dayShort(date)}.`;
  return { kind, date, time: time || null, note: note || null };
}
function saveNamesDate(step, save) {
  const due = pickedDate();
  if (!due || typeof due !== "object") return;
  const s = step && typeof step === "object", add = `add ${due.kind} on ${dayShort(due.date)}`;
  save.textContent = s ? `Save, record ${cut(step.label, 24)} and ${add}` : `Save and ${add}`;
  save.title = (s ? `Save, record ${step.label} on ${step.date} and ` : "Save and ") + `add ${due.kind} on ${due.date}${due.time ? " at " + due.time : ""}`;
}
function renderPanelDates() {
  const due = pickedDate(), ul = byId("ep-dates"), items = [
    ...UP.filter((u) => u.app_id === P.row.id).map((u) => ({ ...u, saved: true })),
    ...(due && typeof due === "object" ? [{ ...due, picked: true }] : []),          // shown at once, so it is clear what Save will add
  ].sort((x, y) => (x.date + (x.time || "")).localeCompare(y.date + (y.time || "")));
  ul.textContent = "";
  if (!items.length) { const li = document.createElement("li"); li.className = "hint"; li.textContent = "No upcoming dates."; ul.appendChild(li); return; }
  items.forEach((u) => {
    const li = document.createElement("li"), what = textEl("span", "what", u.kind), b = document.createElement("button");
    if (u.note) what.appendChild(textEl("small", "", u.note));
    if (u.picked) what.appendChild(textEl("small", "tag-new", "not saved yet: Save adds it"));
    else if (u.date < todayISO()) what.appendChild(textEl("small", "", "passed"));
    const removing = u.saved && P.dateRemoves.has(u.id);
    li.classList.toggle("removing", removing);
    b.type = "button"; b.className = "btn btn-sm btn-drop"; b.textContent = removing ? "Undo" : u.picked ? "Don't add" : "Remove";
    b.addEventListener("click", () => {
      if (u.saved) { if (removing) P.dateRemoves.delete(u.id); else P.dateRemoves.add(u.id); } else resetDateFields();
      refreshPanel();
    });
    li.append(textEl("span", "when", `${dayShort(u.date)}${u.time ? " " + u.time : ""}`), what, b);
    ul.appendChild(li);
  });
}

function wireTlCal() {
  document.querySelectorAll("[data-tlview]").forEach((b) => b.addEventListener("click", () => setTlView(b.dataset.tlview)));
  // after v16's own chip handler (wireStatic runs first), so the active chip is already the new one
  document.querySelectorAll("#timeline-controls .chip").forEach((c) => c.addEventListener("click", () => { if (DATA) { CAL_DAY = null; renderTlCal(); } }));
  byId("tlc-prev").addEventListener("click", () => moveMonth(-1));
  byId("tlc-next").addEventListener("click", () => moveMonth(1));
  byId("tlc-today").addEventListener("click", () => { CAL = null; CAL_DAY = null; renderTlCal(); });
  byId("tlc-grid").addEventListener("click", (e) => {
    const b = e.target.closest("[data-day]");
    if (!b) return;
    CAL_DAY = CAL_DAY === b.dataset.day ? null : b.dataset.day;
    DAY_MORE = DAY_CAP;
    renderTlCal();
    const again = byId("tlc-grid").querySelector(`[data-day="${b.dataset.day}"]`);   // the grid was drawn again: keep the keyboard on that day
    if (again) again.focus();
  });
  ["ep-due-kind", "ep-due-date", "ep-due-time", "ep-due-note"].forEach((id) => byId(id).addEventListener("input", () => refreshPanel()));
}

if (typeof document !== "undefined") wireBoard();
if (typeof document !== "undefined") { wireStatic(); wirePanel(); load(); setInterval(pollHeartbeat, 30000); }
if (typeof document !== "undefined") wireTlCal();       // v17: after wireStatic, so Timeline's own chip handler runs first
if (typeof module !== "undefined") module.exports = { CAT_LABELS, CHECK_LABELS, NO_CATEGORY, catLabel, groupReview, ageWords, hbView, deleteText, restoreText, repliedText, attachText, confirmAllText, checksBadge, esc, roleLine, autoWhy, autoTip };
