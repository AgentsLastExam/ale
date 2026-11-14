"""The control plane's web page: the status document, drawn and kept current in place.

The server embeds the status document so the page draws at once. A script then reads
``/v1/status`` every few seconds and changes only the elements whose content changed, so
the page never reloads: scrolling, selected text, a typed filter and open sections stay as
they are, and the page says when the control plane stops answering. Operator actions call
the API's own operations. Style and script live in this file; there is no build step.
"""

from __future__ import annotations

from ale.fleet.status import FleetStatus

__all__ = ["render"]

REFRESH_SEC = 2

_STYLE = """
:root {
  --bg: #f5f6f8; --panel: #fff; --fg: #1b1f24; --muted: #687384; --line: #e3e6eb;
  --track: #eceef2; --chip: #f0f2f5; --accent: #3866d6;
  --ok: #1d7f3c; --ok-bg: #e6f4ea; --warn: #a96300; --warn-bg: #fcf1dc;
  --bad: #c4362c; --bad-bg: #fbe8e6; --info: #2f63d1; --info-bg: #e7eefc;
  --idle: #6b7280; --idle-bg: #eef0f3;
  --shadow: 0 1px 2px rgba(16, 24, 40, .05);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0e1014; --panel: #161920; --fg: #e5e7eb; --muted: #8d95a3; --line: #262a33;
    --track: #232731; --chip: #1f232b; --accent: #7aa2f7;
    --ok: #4cc26e; --ok-bg: #12301d; --warn: #e0a43a; --warn-bg: #352710;
    --bad: #f06b61; --bad-bg: #3a1614; --info: #7aa2f7; --info-bg: #19253f;
    --idle: #9ca3af; --idle-bg: #22262e; --shadow: none;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
.page { max-width: 1320px; margin: 0 auto; padding: 20px 16px 48px; }
code, .mono { font: 12px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; }
h1 { font-size: 20px; margin: 0; letter-spacing: -.01em; }
h2 { font-size: 15px; margin: 0; }
section { margin-top: 28px; }
.head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 10px; }
.head .grow { flex: 1; }
.muted, .sub { color: var(--muted); }
a { color: var(--accent); }
.sub { font-size: 12px; }
.top { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.live { font-size: 12px; color: var(--muted); display: flex; align-items: center; gap: 6px; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--ok); }
.live.down .dot { background: var(--bad); }
.alert { margin-top: 12px; padding: 10px 12px; border-radius: 8px; background: var(--bad-bg);
  color: var(--bad); }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px;
  margin-top: 16px; }
.tile, .card, .panel { background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; box-shadow: var(--shadow); }
.tile { padding: 12px 14px; }
.tile .label { font-size: 12px; color: var(--muted); }
.tile .value { font-size: 22px; font-weight: 600; margin-top: 2px;
  font-variant-numeric: tabular-nums; }
.tile .value small { font-size: 13px; font-weight: 400; color: var(--muted); }
.tile .bar { margin-top: 8px; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
.card { padding: 14px; display: flex; flex-direction: column; gap: 10px; }
.card-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }
.title { font-weight: 600; overflow-wrap: anywhere; }
.pill { display: inline-flex; align-items: center; gap: 4px; padding: 1px 8px; border-radius: 99px;
  font-size: 12px; font-weight: 500; white-space: nowrap; background: var(--idle-bg);
  color: var(--idle); }
.pill.ok { background: var(--ok-bg); color: var(--ok); }
.pill.warn { background: var(--warn-bg); color: var(--warn); }
.pill.bad { background: var(--bad-bg); color: var(--bad); }
.pill.info { background: var(--info-bg); color: var(--info); }
.meters { display: grid; gap: 8px; }
.meter-label { display: flex; justify-content: space-between; font-size: 12px; }
.meter-label span:last-child { color: var(--muted); font-variant-numeric: tabular-nums; }
.bar { height: 6px; border-radius: 99px; background: var(--track); overflow: hidden;
  display: flex; }
.bar > span { display: block; height: 100%; background: var(--accent); }
.bar > span.warn { background: var(--warn); } .bar > span.bad { background: var(--bad); }
.bar > span.ok { background: var(--ok); } .bar > span.info { background: var(--info); }
.chips { display: flex; flex-wrap: wrap; gap: 4px; }
.chip { background: var(--chip); border-radius: 6px; padding: 1px 7px; font-size: 12px; }
.foot { display: flex; align-items: center; justify-content: space-between; gap: 8px;
  font-size: 12px; color: var(--muted); border-top: 1px solid var(--line); padding-top: 10px; }
button { font: inherit; font-size: 12px; padding: 3px 10px; border-radius: 6px; cursor: pointer;
  border: 1px solid var(--line); background: var(--panel); color: var(--fg); }
button:hover { border-color: var(--muted); }
button.danger { color: var(--bad); }
button.danger:hover { border-color: var(--bad); }
.legend { display: flex; flex-wrap: wrap; gap: 12px; font-size: 12px; color: var(--muted); }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 3px;
  margin-right: 4px; vertical-align: -1px; background: var(--track);
  box-shadow: inset 0 0 0 1px var(--line); }
.legend i.ok { background: var(--ok); } .legend i.info { background: var(--info); }
.legend i.bad { background: var(--bad); } .legend i.warn { background: var(--warn); }
.run .tasks { display: grid; gap: 8px; }
.task-row { display: grid; grid-template-columns: minmax(140px, 1.2fr) 2fr 260px; gap: 12px;
  align-items: center; font-size: 13px; }
.task-row .bar { height: 8px; }
.task-row .count { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums;
  white-space: nowrap; text-align: right; }
.reasons { font-size: 12px; color: var(--warn); }
.panel { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; table-layout: fixed; }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--line);
  vertical-align: top; overflow: hidden; text-overflow: ellipsis; }
tr:last-child td { border-bottom: 0; }
th { font-size: 12px; font-weight: 500; color: var(--muted); white-space: nowrap; }
td.num { font-variant-numeric: tabular-nums; }
.panel td, .panel td > div { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.strong { font-weight: 600; overflow-wrap: anywhere; }
.empty { padding: 16px; color: var(--muted); }
.tools { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; font-size: 13px; }
input[type=search] { font: inherit; font-size: 13px; padding: 5px 10px; width: 280px;
  max-width: 100%; border-radius: 6px; border: 1px solid var(--line); background: var(--panel);
  color: var(--fg); }
details > summary { cursor: pointer; list-style: none; }
details > summary::-webkit-details-marker { display: none; }
details > summary h2::before { content: "\\25B8"; display: inline-block; width: 14px;
  color: var(--muted); }
details[open] > summary h2::before { content: "\\25BE"; }
.sandboxes table { min-width: 980px; }
.images table { min-width: 720px; }
.events table { min-width: 720px; }
@media (max-width: 640px) {
  .task-row { grid-template-columns: 1fr auto; }
  .task-row .count { text-align: left; }
  .task-row .bar { grid-column: 1 / -1; grid-row: 2; }
}
"""

_SCRIPT = r"""
const REFRESH_MS = __REFRESH_MS__;
let status = JSON.parse(document.getElementById("status").textContent);
let fetchedAt = Date.now();
let failure = "";

const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const FINAL = new Set(["released", "failed", "lost"]);
// Their reason only repeats the Node column.
const LIVE = new Set(["placed", "starting", "ready"]);
const RANK = ["ready", "starting", "placed", "queued", "retained", "releasing", "failed", "lost",
  "released"];
const TONE = { ready: "ok", retained: "info", placed: "warn", starting: "warn", queued: "warn",
  releasing: "", released: "", failed: "bad", lost: "bad" };

function dur(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  let s = Math.max(0, Math.round(seconds));
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60); s %= 60;
  if (m < 60) return m + "m " + s + "s";
  const h = Math.floor(m / 60);
  if (h < 48) return h + "h " + (m % 60) + "m";
  return Math.floor(h / 24) + "d " + (h % 24) + "h";
}
const gb = (bytes) => (bytes / 1e9).toFixed(1) + " GB";
const short = (key) => String(key).replace(/^sha256:/, "").slice(0, 12);
const pill = (text, tone) => `<span class="pill ${tone || ""}">${esc(text)}</span>`;
const pct = (used, total) => (total > 0 ? Math.min(100, (100 * used) / total) : 0);
function bar(used, total) {
  const p = pct(used, total);
  const tone = p >= 95 ? "bad" : p >= 80 ? "warn" : "";
  return `<div class="bar"><span class="${tone}" style="width:${p.toFixed(1)}%"></span></div>`;
}
function meter(label, used, total, unit = "") {
  return `<div class="meter"><div class="meter-label"><span>${label}</span>` +
    `<span>${used} / ${total}${unit}</span></div>${bar(used, total)}</div>`;
}
const action = (verb, target, label, danger) =>
  `<button class="${danger ? "danger" : ""}" data-verb="${verb}" ` +
  `data-target="${esc(target)}">${esc(label)}</button>`;
const runName = (runId) => {
  const run = status.runs.find((r) => r.run_id === runId);
  return run && run.label ? run.label : runId;
};

function summary() {
  const online = status.nodes.filter((n) => n.online);
  const sum = (key, part) => online.reduce((total, n) => total + n[part][key], 0);
  const count = (...states) => status.sandboxes.filter((r) => states.includes(r.status)).length;
  const draining = status.nodes.filter((n) => n.draining).length;
  const queued = status.queue.reduce((total, q) => total + q.waiting, 0);
  const troubled = count("failed", "lost") + status.nodes.length - online.length;
  const tile = (label, value, extra = "") =>
    `<div class="tile" data-key="${label}"><div class="label">${label}</div>` +
    `<div class="value">${value}</div>${extra}</div>`;
  const resource = (label, key, unit, scale = 1) => {
    const used = Math.round(sum(key, "used") / scale);
    const total = Math.round(sum(key, "total") / scale);
    return total ? tile(label, `${used}<small> / ${total}${unit}</small>`, bar(used, total)) : "";
  };
  return [
    tile("Nodes", `${online.length}<small> / ${status.nodes.length} online` +
      (draining ? ` · ${draining} draining` : "") + "</small>"),
    tile("Sandboxes", `${count("ready")}<small> ready · ${count("placed", "starting")} starting` +
      ` · ${queued} queued` + (count("retained") ? ` · ${count("retained")} kept` : "") +
      "</small>"),
    resource("CPUs", "cpus", ""),
    resource("Memory", "memory_mb", " GB", 1024),
    resource("GPUs", "gpus", ""),
    resource("macOS VMs", "macos_vm_slots", ""),
    tile("Problems", troubled ? `<span style="color:var(--bad)">${troubled}</span>` +
      "<small> failed, lost or offline</small>" : "0<small> in the last hour</small>"),
  ].join("");
}

function nodes() {
  if (!status.nodes.length) return `<p class="empty">No Node has registered.</p>`;
  return status.nodes.map((n) => {
    const state = !n.online ? pill(`offline · seen ${dur(n.seen_sec_ago)} ago`, "bad")
      : n.draining ? pill("draining", "warn") : pill("online", "ok");
    const meters = [
      meter("CPUs", n.used.cpus, n.total.cpus),
      meter("Memory", Math.round(n.used.memory_mb / 1024), Math.round(n.total.memory_mb / 1024),
        " GB"),
      n.total.gpus ? meter("GPUs", n.used.gpus, n.total.gpus) : "",
      n.total.macos_vm_slots ? meter("macOS VMs", n.used.macos_vm_slots, n.total.macos_vm_slots)
        : "",
    ].join("");
    return `<article class="card" data-key="node:${esc(n.name)}">` +
      `<div class="card-head"><div><div class="title">${esc(n.name)}</div>` +
      `<div class="sub">${esc(n.address)} · ${esc(n.platform)}</div></div>${state}</div>` +
      `<div class="meters">${meters}</div>` +
      `<div class="chips">${n.profiles.map((p) => `<span class="chip">${esc(p)}</span>`)
        .join("")}</div>` +
      `<div class="foot"><span>${n.sandboxes} Sandboxes · ${n.images} images ` +
      `(${gb(n.image_bytes)})<br>${n.disk_free_gb} GB disk free · agent ` +
      `${esc(n.agent_version)} <code>${esc(n.engine_commit)}</code></span>` +
      (n.draining ? action("resume", n.name, "Resume") : action("drain", n.name, "Drain")) +
      `</div></article>`;
  }).join("");
}

function progress(t) {
  const parts = [["ok", t.completed], ["bad", t.failed], ["warn", t.infrastructure],
    ["info", t.running]];
  const spans = parts.filter(([, n]) => n).map(([tone, n]) =>
    `<span class="${tone}" style="width:${pct(n, t.episodes).toFixed(1)}%"></span>`).join("");
  const words = [`${t.completed}/${t.episodes} done`];
  if (t.running) words.push(`${t.running} running`);
  if (t.failed) words.push(`${t.failed} failed`);
  if (t.infrastructure) words.push(`${t.infrastructure} infrastructure`);
  if (t.waiting) words.push(`${t.waiting} waiting`);
  return `<div class="task-row" data-key="task:${esc(t.task)}">` +
    `<div class="strong">${esc(t.task)}</div><div class="bar">${spans}</div>` +
    `<div class="count">${words.join(" · ")}</div></div>`;
}

function queueLine(q) {
  const reasons = Object.entries(q.reasons).map(([why, n]) => `${esc(why)} (${n})`).join("; ");
  return `<div class="reasons">${q.waiting} queued: ${reasons}</div>`;
}

function runs() {
  const known = new Set(status.runs.map((r) => r.run_id));
  const cards = status.runs.map((r) => {
    const q = status.queue.find((item) => item.run_id === r.run_id);
    const start = [["queue", r.queue_sec], ["image", r.image_sec], ["create", r.create_sec]]
      .filter(([, s]) => s !== null).map(([name, s]) => `${name} ${dur(s)}`).join(" · ");
    return `<article class="card run" data-key="run:${esc(r.run_id)}">` +
      `<div class="card-head"><div><div class="title">${esc(r.label || r.run_id)}</div>` +
      `<div class="sub">${r.label ? `<code>${esc(r.run_id)}</code> · ` : ""}` +
      (r.concurrency ? `concurrency ${r.concurrency} · ` : "") + `${r.sandboxes} Sandboxes` +
      (start ? ` · mean start: ${start}` : "") + `</div></div>` +
      (q ? action("cancel", r.run_id, "Cancel queued", true) : "") + `</div>` +
      (q ? queueLine(q) : "") +
      `<div class="tasks">${r.tasks.map(progress).join("") ||
        '<span class="sub">No Task reported yet.</span>'}</div></article>`;
  });
  for (const q of status.queue.filter((item) => !known.has(item.run_id))) {
    cards.push(`<article class="card run" data-key="queue:${esc(q.run_id)}">` +
      `<div class="card-head"><div class="title">${esc(q.label || q.run_id)}</div>` +
      `${action("cancel", q.run_id, "Cancel queued", true)}</div>${queueLine(q)}</article>`);
  }
  if (!cards.length) return `<p class="empty">No Run has asked for a Sandbox.</p>`;
  return `<div class="legend" data-key="legend"><span><i class="ok"></i>done</span>` +
    `<span><i class="info"></i>running</span><span><i class="bad"></i>failed</span>` +
    `<span><i class="warn"></i>infrastructure</span><span><i></i>waiting</span></div>` +
    `<div class="cards" data-key="runs" style="grid-template-columns:1fr">${cards.join("")}</div>`;
}

function sandboxes() {
  const query = document.getElementById("filter").value.trim().toLowerCase();
  const withFinished = document.getElementById("finished").checked;
  const finished = status.sandboxes.filter((r) => FINAL.has(r.status)).length;
  const rows = status.sandboxes.filter((r) => withFinished || !FINAL.has(r.status)).filter((r) =>
    !query || [r.task, r.sandbox_id, r.run_id, runName(r.run_id), r.node, r.image, r.status]
      .some((field) => String(field || "").toLowerCase().includes(query)))
    .sort((a, b) => a.task.localeCompare(b.task) || RANK.indexOf(a.status) -
      RANK.indexOf(b.status) || b.age_sec - a.age_sec);
  document.getElementById("finished-count").textContent = finished ? `(${finished})` : "";
  if (!rows.length) {
    const why = status.sandboxes.length ? "No Sandbox matches." : "No Sandbox in the last hour.";
    return `<p class="empty">${why}</p>`;
  }
  const body = rows.map((r) => {
    const known = [r.queue_sec, r.image_sec, r.create_sec].some((s) => s !== null);
    const start = known ? `<span title="queue ${dur(r.queue_sec)}, image ${dur(r.image_sec)}, ` +
      `create ${dur(r.create_sec)}">${dur(r.queue_sec)} · ${dur(r.image_sec)} · ` +
      `${dur(r.create_sec)}</span>` : "—";
    const ended = FINAL.has(r.status) || r.status === "releasing";
    const episode = r.episode_id.split("-").pop();
    return `<tr data-key="sb:${esc(r.sandbox_id)}">` +
      `<td><div class="strong" title="${esc(r.task)}">${esc(r.task || "—")}</div>` +
      `<div class="sub" title="${esc(r.sandbox_id)}">${esc(r.role)} · episode ` +
      `<span class="mono">${esc(episode)}</span></div></td>` +
      `<td>${pill(r.status, TONE[r.status])}` +
      (r.reason && !LIVE.has(r.status)
        ? `<div class="sub" title="${esc(r.reason)}">${esc(r.reason)}</div>` : "") +
      `</td><td title="${esc(r.node || "")}">${esc(r.node || "—")}` +
      `<div class="sub">${esc(r.provider || "")}</div></td>` +
      `<td><code title="${esc(r.image)}">${esc(short(r.image))}</code></td>` +
      `<td title="${esc(r.run_id)}">${esc(runName(r.run_id))}</td>` +
      `<td class="num">${dur(r.age_sec)}</td><td class="num">${dur(r.lease_left_sec)}</td>` +
      `<td class="sub num">${start}</td>` +
      `<td>${ended ? "" : action("destroy", r.sandbox_id, "Destroy", true)}</td></tr>`;
  }).join("");
  return `<div class="panel"><table><colgroup><col style="width:21%"><col style="width:15%">` +
    `<col style="width:14%"><col style="width:10%"><col style="width:9%">` +
    `<col style="width:7%"><col style="width:7%"><col style="width:10%"><col style="width:7%">` +
    `</colgroup><tr><th>Task</th><th>Status</th><th>Node</th><th>Image</th><th>Run</th>` +
    `<th>Age</th><th>Lease</th><th title="queue · image · create">Start</th><th></th></tr>` +
    `${body}</table></div>`;
}

function images() {
  if (!status.images.length) return `<p class="empty">No Node reports an image.</p>`;
  const body = status.images.map((i) => `<tr data-key="img:${esc(i.key)}">` +
    `<td><div class="chips">${i.tasks.length ? i.tasks.map((t) =>
      `<span class="chip">${esc(t)}</span>`).join("") : '<span class="sub">unknown</span>'}` +
    `</div></td><td><code title="${esc(i.key)}">${esc(short(i.key))}</code></td>` +
    `<td class="num">${gb(i.bytes)}</td><td class="num">${dur(i.prepare_sec)}</td>` +
    `<td><div class="chips">${i.nodes.map((n) => `<span class="chip">${esc(n)}</span>`)
      .join("")}</div></td></tr>`).join("");
  return `<div class="panel"><table><colgroup><col style="width:38%"><col style="width:14%">` +
    `<col style="width:10%"><col style="width:12%"><col style="width:26%"></colgroup>` +
    `<tr><th>Tasks</th><th>Image</th><th>Size</th><th>Prepared in</th><th>Nodes</th></tr>` +
    `${body}</table></div>`;
}

function events() {
  if (!status.events.length) return `<p class="empty">No events yet.</p>`;
  const body = status.events.map((e) => `<tr data-key="ev:${e.at}:${esc(e.kind)}:` +
    `${esc(e.sandbox)}"><td class="num">${new Date(e.at * 1000).toLocaleTimeString()}</td>` +
    `<td><code>${esc(e.kind)}</code></td><td>${esc(e.node)}</td>` +
    `<td><code>${esc(e.sandbox)}</code></td><td>${esc(e.detail)}</td></tr>`).join("");
  return `<div class="panel"><table><colgroup><col style="width:11%"><col style="width:16%">` +
    `<col style="width:13%"><col style="width:25%"><col style="width:35%"></colgroup>` +
    `<tr><th>Time</th><th>Event</th><th>Node</th><th>Sandbox</th><th>Detail</th></tr>` +
    `${body}</table></div>`;
}

// Bring `from` to look like `to`, touching only what differs. Children with a data-key are
// matched by key, so a row that moves keeps its element.
function morph(from, to) {
  if (from.nodeType !== 1) {
    if (from.nodeValue !== to.nodeValue) from.nodeValue = to.nodeValue;
    return;
  }
  for (const { name } of [...from.attributes]) {
    if (!to.hasAttribute(name)) from.removeAttribute(name);
  }
  for (const { name, value } of [...to.attributes]) {
    if (from.getAttribute(name) !== value) from.setAttribute(name, value);
  }
  const keyOf = (node) => (node.nodeType === 1 ? node.getAttribute("data-key") : null);
  const keyed = new Map();
  for (const child of from.childNodes) if (keyOf(child)) keyed.set(keyOf(child), child);
  let cursor = from.firstChild;
  for (const want of [...to.childNodes]) {
    const key = keyOf(want);
    let have = key ? keyed.get(key) : cursor;
    if (key) keyed.delete(key);
    if (!have || have.nodeName !== want.nodeName || (!key && keyOf(have))) {
      from.insertBefore(want, cursor);
      continue;
    }
    if (have === cursor) cursor = cursor.nextSibling;
    else from.insertBefore(have, cursor);
    morph(have, want);
  }
  while (cursor) {
    const next = cursor.nextSibling;
    from.removeChild(cursor);
    cursor = next;
  }
}

function paint(id, html) {
  const region = document.getElementById(id);
  const next = region.cloneNode(false);
  next.innerHTML = html;
  morph(region, next);
}

function draw() {
  paint("summary", summary());
  paint("nodes", nodes());
  paint("runs", runs());
  paint("sandboxes", sandboxes());
  paint("images", images());
  paint("events", events());
  document.getElementById("events-latest").textContent = status.events.length
    ? `latest ${new Date(status.events[0].at * 1000).toLocaleTimeString()}` : "";
}

function live() {
  const ago = Math.round((Date.now() - fetchedAt) / 1000);
  const box = document.getElementById("live");
  box.classList.toggle("down", Boolean(failure));
  document.getElementById("live-text").textContent =
    (failure ? "not answering · " : "live · ") + `updated ${dur(ago)} ago`;
  const banner = document.getElementById("alert");
  banner.hidden = !failure;
  banner.textContent = failure;
}

async function refresh() {
  if (document.hidden) return;
  try {
    const response = await fetch("/v1/status", { cache: "no-store" });
    if (response.status === 401) {
      throw new Error("The Fleet token is missing or wrong: open /?token=TOKEN again.");
    }
    if (!response.ok) throw new Error(`The control plane answered ${response.status}.`);
    status = await response.json();
    fetchedAt = Date.now();
    failure = "";
    draw();
  } catch (error) {
    failure = error instanceof TypeError
      ? "The control plane is not answering; this is what it last showed. Retrying."
      : error.message;
  }
  live();
}

const part = encodeURIComponent;
const VERBS = {
  drain: (node) => [`/v1/nodes/${part(node)}/drain`, { draining: true },
    `Drain ${node}? Nothing new is placed on it; what it holds runs to its end.`],
  resume: (node) => [`/v1/nodes/${part(node)}/drain`, { draining: false }, ""],
  destroy: (id) => [`/v1/sandboxes/${part(id)}/destroy`, null,
    `Destroy Sandbox ${id}? The episode in it fails.`],
  cancel: (run) => [`/v1/runs/${part(run)}/cancel`, null,
    `Cancel the queued Sandboxes of ${runName(run)}? What already runs is not touched.`],
};

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-verb]");
  if (!button) return;
  const [path, body, question] = VERBS[button.dataset.verb](button.dataset.target);
  if (question && !confirm(question)) return;
  button.disabled = true;
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!response.ok) alert(`${button.textContent} failed: ${await response.text()}`);
  } finally {
    button.disabled = false;
    await refresh();
  }
});
document.getElementById("filter").addEventListener("input", draw);
document.getElementById("finished").addEventListener("change", draw);
document.addEventListener("visibilitychange", refresh);

draw();
live();
setInterval(refresh, REFRESH_MS);
setInterval(live, 1000);
"""

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ALE Fleet</title><style>__STYLE__</style></head><body><div class="page">
<div class="top"><h1>ALE Fleet</h1>
<div class="live" id="live"><span class="dot"></span><span id="live-text">live</span>
· <a href="/v1/status">JSON</a></div></div>
<div class="alert" id="alert" hidden></div>
<noscript><p class="alert">This page draws with JavaScript; <code>ale-fleet status</code>
prints the same document.</p></noscript>
<div class="tiles" id="summary"></div>
<section><div class="head"><h2>Nodes</h2></div><div class="cards" id="nodes"></div></section>
<section><div class="head"><h2>Runs</h2></div><div id="runs"></div></section>
<section class="sandboxes"><div class="head"><h2>Sandboxes</h2><span class="grow"></span>
<div class="tools"><input type="search" id="filter" placeholder="Filter Sandboxes"
 title="Matches Task, Run, Node, image, status or Sandbox">
<label><input type="checkbox" id="finished"> finished <span id="finished-count"
 class="muted"></span></label></div></div><div id="sandboxes"></div></section>
<section class="images"><div class="head"><h2>Images</h2></div><div id="images"></div></section>
<section class="events"><details><summary class="head"><h2>Events</h2>
<span class="sub" id="events-latest"></span></summary><div id="events"></div></details></section>
</div>
<script type="application/json" id="status">__STATUS__</script>
<script>__SCRIPT__</script>
</body></html>
"""


def render(status: FleetStatus) -> str:
    # "<" escaped inside the JSON keeps "</script>" in any name from ending the element.
    data = status.model_dump_json().replace("<", "\\u003c")
    script = _SCRIPT.replace("__REFRESH_MS__", str(REFRESH_SEC * 1000))
    return (
        _PAGE.replace("__STYLE__", _STYLE).replace("__SCRIPT__", script).replace("__STATUS__", data)
    )
