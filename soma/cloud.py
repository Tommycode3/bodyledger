"""Replication to Supabase: local stays the source of truth, the cloud is a copy.

One direction only, local -> cloud. That way a network problem or a server-side
error cannot corrupt the history: at worst the replica falls behind and the
next sync realigns it (idempotent upsert on the same key as local).

Credentials in the Keychain: `soma auth supabase`.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from . import net, secrets
from .net import HttpError
from .store import Store

BATCH = 500

SCHEMA_SQL = """-- Run once in the Supabase project's SQL Editor.
create table if not exists signal (
  source text not null,
  metric text not null,
  ts     bigint not null,
  day    date not null,
  value  double precision,
  unit   text,
  meta   jsonb,
  primary key (source, metric, ts)
);
create index if not exists signal_day_idx on signal(day, metric);

create table if not exists event (
  id       text primary key,
  source   text not null,
  kind     text not null,
  start_ts bigint not null,
  end_ts   bigint,
  day      date not null,
  title    text,
  meta     jsonb
);

-- Personal data: no anonymous access.
alter table signal enable row level security;
alter table event  enable row level security;
"""


def _config() -> tuple[str, str]:
    cfg = secrets.get_json("supabase")
    if not cfg or not cfg.get("url") or not cfg.get("key"):
        raise RuntimeError("Supabase not configured — `soma auth supabase`")
    return cfg["url"].rstrip("/"), cfg["key"]


OVERLAP_DAYS = 14


def push(store: Store, full: bool = False) -> str:
    """Incremental replication with an overlap window.

    The watermark alone is not enough: a device often syncs an old night
    *after* the watermark has passed it, and that data would never come
    back. So every push also resends the last `OVERLAP_DAYS` — it costs
    little (idempotent upsert) and closes the gap.
    """
    url, key = _config()
    if full:
        since = "1970-01-01T00:00:00"
    else:
        mark = store.get_state("cloud", "last_push")
        since = ((datetime.fromisoformat(mark) - timedelta(days=OVERLAP_DAYS)).isoformat()
                 if mark else "1970-01-01T00:00:00")
    rows = store.rows_since(since)
    if not rows:
        return "cloud: nothing new to replicate"

    headers = {"apikey": key, "Authorization": f"Bearer {key}",
               "Prefer": "resolution=merge-duplicates,return=minimal"}
    sent = 0
    for i in range(0, len(rows), BATCH):
        chunk = []
        for r in rows[i:i + BATCH]:
            item = dict(r)
            if item.get("meta"):
                try:
                    item["meta"] = json.loads(item["meta"])
                except (json.JSONDecodeError, TypeError):
                    item["meta"] = None
            chunk.append(item)
        try:
            net.post(f"{url}/rest/v1/signal", json_body=chunk, headers=headers)
        except HttpError as e:
            return (f"cloud: interrupted after {sent} rows — {e}\n"
                    f"If the table does not exist, run the schema: `soma cloud schema`")
        sent += len(chunk)

    # watermark = highest replicated ts (not "now": the machine clock
    # and the data timestamps are two different axes)
    top = max(r["ts"] for r in rows)
    store.set_state("cloud", "last_push",
                    datetime.fromtimestamp(top).isoformat(timespec="seconds"))
    return f"cloud: {sent} rows replicated to Supabase"
