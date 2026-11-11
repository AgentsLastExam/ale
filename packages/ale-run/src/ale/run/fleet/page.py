"""The control plane's web page: the status document as HTML, refreshed every few seconds.

Rendered on the server from the same :class:`FleetStatus` the JSON API returns, with no
script and no build step. Each operator action is a form that posts to the API's own
operation and comes back here.
"""

from __future__ import annotations

import time
from html import escape

from ale.run.fleet.status import FleetStatus, NodeStatus

__all__ = ["render"]

REFRESH_SEC = 5

_STYLE = """
:root { --fg: #1d1d1f; --muted: #6e6e73; --line: #d2d2d7; --bg: #fff; --ok: #1a7f37;
        --warn: #9a6700; --bad: #cf222e; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #f5f5f7; --muted: #a1a1a6; --line: #3a3a3c; --bg: #111; --ok: #3fb950;
          --warn: #d29922; --bad: #f85149; }
}
body { font: 14px/1.45 -apple-system, system-ui, sans-serif; color: var(--fg);
       background: var(--bg); margin: 16px; }
h1 { font-size: 20px; margin: 0 0 4px; } h2 { font-size: 16px; margin: 24px 0 8px; }
p.meta { color: var(--muted); margin: 0; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--line);
         vertical-align: top; }
th { color: var(--muted); font-weight: 500; }
td.num { font-variant-numeric: tabular-nums; }
.ok { color: var(--ok); } .warn { color: var(--warn); } .bad { color: var(--bad); }
.muted { color: var(--muted); } code { font-size: 12px; }
form { display: inline; } button { font: inherit; padding: 0 6px; }
.wrap { overflow-x: auto; }
"""

_TONE = {
    "ready": "ok",
    "starting": "warn",
    "placed": "warn",
    "queued": "warn",
    "releasing": "muted",
    "released": "muted",
    "failed": "bad",
    "lost": "bad",
}


def _gb(value: int | None) -> str:
    return "" if value is None else f"{value / 1e9:.1f} GB"


def _sec(value: float | None) -> str:
    return "" if value is None else f"{value:.0f}s"


def _action(path: str, label: str) -> str:
    return f'<form method="post" action="{escape(path)}"><button>{escape(label)}</button></form>'


def _table(headers: list[str], rows: list[list[str]], empty: str) -> str:
    if not rows:
        return f'<p class="muted">{escape(empty)}</p>'
    head = "".join(f"<th>{escape(header)}</th>" for header in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="wrap"><table><tr>{head}</tr>{body}</table></div>'


def _node_state(node: NodeStatus) -> str:
    if not node.online:
        return (
            f'<span class="bad">offline</span> <span class="muted">{node.seen_sec_ago:.0f}s</span>'
        )
    if node.draining:
        return '<span class="warn">draining</span>'
    return '<span class="ok">online</span>'


def render(status: FleetStatus) -> str:
    nodes = [
        [
            f"<b>{escape(node.name)}</b><br><span class='muted'>{escape(node.address)}</span>",
            _node_state(node),
            "<br>".join(escape(profile) for profile in node.profiles),
            f"{node.used.cpus}/{node.total.cpus} CPUs<br>"
            f"{node.used.memory_mb // 1024}/{node.total.memory_mb // 1024} GB"
            + (
                f"<br>{node.used.macos_vm_slots}/{node.total.macos_vm_slots} macOS"
                if node.total.macos_vm_slots
                else ""
            )
            + (f"<br>{node.used.gpus}/{node.total.gpus} GPUs" if node.total.gpus else ""),
            f"{node.images} ({_gb(node.image_bytes)})<br>{node.disk_free_gb} GB free",
            f"{escape(node.agent_version)} <code>{escape(node.engine_commit)}</code>",
            _action(f"/ui/nodes/{node.name}/resume", "resume")
            if node.draining
            else _action(f"/ui/nodes/{node.name}/drain", "drain"),
        ]
        for node in status.nodes
    ]
    queue = [
        [
            f"<code>{escape(item.run_id)}</code> {escape(item.label)}",
            str(item.waiting),
            "<br>".join(f"{count}: {escape(reason)}" for reason, count in item.reasons.items()),
            _action(f"/ui/runs/{item.run_id}/cancel", "cancel queued"),
        ]
        for item in status.queue
    ]
    sandboxes = [
        [
            f"<code>{escape(row.sandbox_id)}</code>",
            escape(row.node or ""),
            f'<span class="{_TONE.get(row.status, "")}">{escape(row.status)}</span>'
            + (f"<br><span class='muted'>{escape(row.reason)}</span>" if row.reason else ""),
            f"<code>{escape(row.run_id)}</code><br>{escape(row.episode_id)} {escape(row.role)}",
            _sec(row.age_sec),
            _sec(row.lease_left_sec),
            " / ".join(
                part
                for part in (
                    f"queue {_sec(row.queue_sec)}" if row.queue_sec is not None else "",
                    f"image {_sec(row.image_sec)}" if row.image_sec is not None else "",
                    f"start {_sec(row.create_sec)}" if row.create_sec is not None else "",
                )
                if part
            ),
            ""
            if row.status in {"released", "failed", "releasing"}
            else _action(f"/ui/sandboxes/{row.sandbox_id}/destroy", "destroy"),
        ]
        for row in status.sandboxes
    ]
    runs = [
        [
            f"<code>{escape(run.run_id)}</code> {escape(run.label)}",
            "<br>".join(
                f"{escape(task.task)}: {task.completed} done, {task.running} running, "
                f"{task.waiting} waiting, {task.failed} failed, "
                f"{task.infrastructure} infrastructure of {task.episodes}"
                for task in run.tasks
            ),
            f"queue {_sec(run.queue_sec)}<br>image {_sec(run.image_sec)}<br>"
            f"start {_sec(run.create_sec)}",
        ]
        for run in status.runs
    ]
    images = [
        [
            f"<code>{escape(image.key[:19])}</code>",
            _gb(image.bytes),
            _sec(image.prepare_sec),
            escape(", ".join(image.nodes)),
        ]
        for image in status.images
    ]
    events = [
        [
            time.strftime("%H:%M:%S", time.localtime(event.at)),
            escape(event.kind),
            escape(event.node),
            f"<code>{escape(event.sandbox)}</code>",
            escape(event.detail),
        ]
        for event in status.events
    ]
    online = sum(node.online for node in status.nodes)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_SEC}">
<title>ALE Fleet</title><style>{_STYLE}</style></head><body>
<h1>ALE Fleet</h1>
<p class="meta">{online}/{len(status.nodes)} Nodes online · {len(status.sandboxes)} Sandboxes ·
{sum(item.waiting for item in status.queue)} queued · refreshed every {REFRESH_SEC}s ·
the same data is <a href="/v1/status">/v1/status</a></p>
<h2>Nodes</h2>
{
        _table(
            ["Node", "State", "Profiles", "Used", "Images", "Agent", ""],
            nodes,
            "No Node has registered.",
        )
    }
<h2>Queue</h2>
{_table(["Run", "Waiting", "Why", ""], queue, "Nothing is waiting.")}
<h2>Runs</h2>
{_table(["Run", "Progress", "Mean start"], runs, "No Run has asked for a Sandbox.")}
<h2>Sandboxes</h2>
{
        _table(
            ["Sandbox", "Node", "Status", "Run", "Age", "Lease", "Start", ""],
            sandboxes,
            "No Sandbox in the last hour.",
        )
    }
<h2>Images</h2>
{_table(["Image", "Size", "Prepared in", "Nodes"], images, "No Node reports an image.")}
<h2>Events</h2>
{_table(["Time", "Event", "Node", "Sandbox", "Detail"], events, "No events yet.")}
</body></html>
"""
