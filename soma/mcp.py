"""MCP server over stdio: body data inside any Claude session.

This is the "always-on access" piece: once registered with
`claude mcp add soma -- python3 -m soma mcp`, any conversation can ask
"how did I sleep this week?" and read the local store.

Read-only, by design: an LLM must not be able to write to your physiological
history. No network: it reads SQLite and nothing else.
"""
from __future__ import annotations

import json
import sys

from . import fuse
from .store import METRICS, Store

PROTOCOL = "2024-11-05"

TOOLS = [
    {
        "name": "soma_today",
        "description": ("Today's body state (or a given day's): readiness, "
                        "sleep, HRV, resting HR, load, and the suggested posture. "
                        "Explicitly states what is missing instead of guessing."),
        "inputSchema": {"type": "object", "properties": {
            "day": {"type": "string", "description": "YYYY-MM-DD, default today"}}},
    },
    {
        "name": "soma_metric",
        "description": ("Daily series of a canonical metric over N days. "
                        "Use soma_metrics for valid names."),
        "inputSchema": {"type": "object", "properties": {
            "metric": {"type": "string"},
            "days": {"type": "integer", "description": "default 30"}},
            "required": ["metric"]},
    },
    {
        "name": "soma_metrics",
        "description": "Vocabulary of available metrics, with units and per-source coverage.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "soma_events",
        "description": "Recorded workouts and sleep sessions.",
        "inputSchema": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["workout", "sleep"]},
            "days": {"type": "integer", "description": "default 30"}}},
    },
]


def _handle(method: str, params: dict, store: Store):
    if method == "initialize":
        return {"protocolVersion": PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "soma", "version": "0.1.0"}}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method != "tools/call":
        raise ValueError(f"unsupported method: {method}")

    name = params.get("name")
    args = params.get("arguments") or {}

    if name == "soma_today":
        day = args.get("day")
        text = fuse.brief(store, day) + "\n\n" + json.dumps(
            fuse.state(store, day), ensure_ascii=False, indent=2)
    elif name == "soma_metric":
        metric = args["metric"]
        if metric not in METRICS:
            text = (f"Unknown metric: {metric}\nAvailable: "
                    + ", ".join(sorted(METRICS)))
        else:
            rows = store.series(metric, days=int(args.get("days", 30)))
            unit = METRICS[metric][0]
            text = (f"{metric} ({unit}) — {len(rows)} days\n"
                    + "\n".join(f"{r['day']}  {r['value']:.1f}  [{r['source']}]" for r in rows)
                    ) if rows else f"{metric}: no data in the period."
    elif name == "soma_metrics":
        cov = store.coverage()
        lines = ["Canonical vocabulary:"]
        lines += [f"  {m:<24} {u:<12} {d}" for m, (u, d) in sorted(METRICS.items())]
        lines.append("\nActual coverage:")
        lines += [f"  {c['source']:<8} {c['n']:>7} samples, {c['metrics']} metrics, "
                  f"{c['first_day']} → {c['last_day']}" for c in cov] or ["  (empty store)"]
        text = "\n".join(lines)
    elif name == "soma_events":
        evs = store.events(kind=args.get("kind"), days=int(args.get("days", 30)))
        text = "\n".join(
            f"{e['day']}  {e['kind']:<8} {e['title']}  {(e['meta'] or '')}" for e in evs
        ) or "No events in the period."
    else:
        raise ValueError(f"unknown tool: {name}")

    return {"content": [{"type": "text", "text": text}]}


def serve() -> None:
    store = Store()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid = msg.get("id")
        try:
            result = _handle(msg.get("method", ""), msg.get("params") or {}, store)
            resp = {"jsonrpc": "2.0", "id": mid, "result": result}
        except Exception as e:  # a broken request must not kill the server
            resp = {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32603, "message": str(e)}}
        if mid is None:      # notification: no response expected
            continue
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()
