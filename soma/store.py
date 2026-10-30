"""Canonical body store.

One place, one vocabulary. Adapters translate each device's dialect into
canonical metrics: from here on nobody knows whether a data point came from
COROS or Fitbit, and adding a device changes nothing downstream.

Local-first: SQLite in ~/.soma, no network on reads.
Idempotent by construction: the primary key is (source, metric, ts), so
re-syncing the same period never duplicates anything.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

HOME = os.path.expanduser("~/.soma")
DB_PATH = os.path.join(HOME, "soma.db")

# --------------------------------------------------------------------------
# Canonical vocabulary. This table IS the contract: an adapter that wants to
# write a metric not listed here must add it first, so the vocabulary does
# not silently drift device after device.
# --------------------------------------------------------------------------
METRICS = {
    # sleep
    "sleep.duration_min":  ("min",   "Minutes asleep (asleep, not in bed)"),
    "sleep.deep_min":      ("min",   "Deep sleep"),
    "sleep.rem_min":       ("min",   "REM sleep"),
    "sleep.light_min":     ("min",   "Light sleep"),
    "sleep.awake_min":     ("min",   "Minutes awake within the sleep window"),
    "sleep.efficiency":    ("pct",   "Sleep efficiency"),
    "sleep.start_hour":    ("h",     "Sleep onset time (local, decimal)"),
    "sleep.end_hour":      ("h",     "Wake-up time (local, decimal)"),
    # heart / recovery
    "hr.resting":          ("bpm",   "Resting heart rate"),
    "hr.hrv_rmssd":        ("ms",    "Nightly HRV RMSSD"),
    "hr.hrv_deep_rmssd":   ("ms",    "HRV RMSSD during deep sleep"),
    "hr.max":              ("bpm",   "Maximum HR observed during the day"),
    # from minute-by-minute HR: how the body is while you are awake and working,
    # not only while you sleep. The daytime mean is a proxy for arousal.
    "hr.daytime_mean":     ("bpm",   "Mean HR during waking hours (9-21)"),
    "hr.daytime_p90":      ("bpm",   "90th percentile of daytime HR"),
    "hr.daytime_spread":   ("bpm",   "Daytime HR range (p90 − p10)"),
    # breathing / oxygen / temperature
    "body.breathing_rate": ("rpm",   "Nightly breathing rate"),
    "body.spo2":           ("pct",   "Mean nightly oxygen saturation"),
    "body.temp_delta":     ("degC",  "Skin temperature deviation from baseline"),
    "body.weight":         ("kg",    "Weight"),
    # activity
    "activity.steps":      ("count", "Steps"),
    "activity.calories":   ("kcal",  "Total calories"),
    "activity.distance_m": ("m",     "Distance covered"),
    "activity.active_min": ("min",   "Active minutes / zone minutes"),
    "activity.floors":     ("count", "Floors climbed"),
    # training
    "training.load":       ("au",    "Session training load"),
    "training.duration_min": ("min", "Session duration"),
    "training.avg_hr":     ("bpm",   "Session mean HR"),
    "training.vo2max":     ("ml/kg/min", "VO2max / cardio fitness estimate"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS signal(
    source TEXT NOT NULL,
    metric TEXT NOT NULL,
    ts     INTEGER NOT NULL,        -- UTC epoch of the observation
    day    TEXT NOT NULL,           -- local YYYY-MM-DD: the day the data point "belongs" to
    value  REAL,
    unit   TEXT,
    meta   TEXT,
    PRIMARY KEY(source, metric, ts)
);
CREATE INDEX IF NOT EXISTS signal_day    ON signal(day, metric);
CREATE INDEX IF NOT EXISTS signal_metric ON signal(metric, ts);

CREATE TABLE IF NOT EXISTS event(
    id       TEXT PRIMARY KEY,      -- "<source>:<native id>": free idempotency
    source   TEXT NOT NULL,
    kind     TEXT NOT NULL,         -- workout | sleep
    start_ts INTEGER NOT NULL,
    end_ts   INTEGER,
    day      TEXT NOT NULL,
    title    TEXT,
    meta     TEXT
);
CREATE INDEX IF NOT EXISTS event_day ON event(day, kind);

CREATE TABLE IF NOT EXISTS sync_state(
    source  TEXT NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT,
    updated TEXT,
    PRIMARY KEY(source, key)
);
"""


@dataclass
class Sample:
    source: str
    metric: str
    ts: int
    day: str
    value: float
    unit: str = ""
    meta: Optional[dict] = None


def day_of(ts: int) -> str:
    """Local day an epoch timestamp belongs to."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    parent = os.path.dirname(path)
    if parent:                       # ":memory:" and relative paths have no directory
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


class Store:
    def __init__(self, path: str = DB_PATH):
        self.con = connect(path)

    # -- writes ------------------------------------------------------------
    def put(self, samples: Iterable[Sample]) -> int:
        """Idempotent upsert. Returns how many rows were new or changed."""
        rows = []
        for s in samples:
            if s.value is None:
                continue
            unit = s.unit or METRICS.get(s.metric, ("", ""))[0]
            rows.append((s.source, s.metric, int(s.ts), s.day, float(s.value),
                         unit, json.dumps(s.meta, ensure_ascii=False) if s.meta else None))
        if not rows:
            return 0
        before = self.con.total_changes
        self.con.executemany(
            "INSERT INTO signal(source,metric,ts,day,value,unit,meta) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(source,metric,ts) DO UPDATE SET value=excluded.value, "
            "day=excluded.day, unit=excluded.unit, meta=excluded.meta", rows)
        self.con.commit()
        return self.con.total_changes - before

    def put_event(self, source: str, kind: str, native_id: str, start_ts: int,
                  end_ts: Optional[int], title: str, meta: Optional[dict] = None) -> None:
        self.con.execute(
            "INSERT INTO event(id,source,kind,start_ts,end_ts,day,title,meta) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET end_ts=excluded.end_ts, title=excluded.title, meta=excluded.meta",
            (f"{source}:{native_id}", source, kind, int(start_ts),
             int(end_ts) if end_ts else None, day_of(start_ts), title,
             json.dumps(meta, ensure_ascii=False) if meta else None))
        self.con.commit()

    # -- sync state -------------------------------------------------------
    def get_state(self, source: str, key: str, default=None):
        r = self.con.execute("SELECT value FROM sync_state WHERE source=? AND key=?",
                             (source, key)).fetchone()
        return r["value"] if r else default

    def set_state(self, source: str, key: str, value: str) -> None:
        self.con.execute(
            "INSERT INTO sync_state(source,key,value,updated) VALUES(?,?,?,?) "
            "ON CONFLICT(source,key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
            (source, key, str(value), datetime.now().isoformat(timespec="seconds")))
        self.con.commit()

    # -- reads -------------------------------------------------------------
    def series(self, metric: str, days: int = 30, end: Optional[str] = None) -> list[dict]:
        """Daily series of a metric: one value per day (the last observed)."""
        end = end or datetime.now().strftime("%Y-%m-%d")
        start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        rows = self.con.execute(
            "SELECT day, value, unit, source, MAX(ts) AS ts FROM signal "
            "WHERE metric=? AND day BETWEEN ? AND ? GROUP BY day ORDER BY day",
            (metric, start, end)).fetchall()
        return [dict(r) for r in rows]

    def latest(self, metric: str, within_days: int = 7) -> Optional[dict]:
        floor = (datetime.now() - timedelta(days=within_days)).strftime("%Y-%m-%d")
        r = self.con.execute(
            "SELECT day, value, unit, source, ts FROM signal WHERE metric=? AND day>=? "
            "ORDER BY ts DESC LIMIT 1", (metric, floor)).fetchone()
        return dict(r) if r else None

    def day_metrics(self, day: str) -> dict[str, dict]:
        rows = self.con.execute(
            "SELECT metric, value, unit, source, MAX(ts) AS ts FROM signal WHERE day=? "
            "GROUP BY metric ORDER BY metric", (day,)).fetchall()
        return {r["metric"]: dict(r) for r in rows}

    def events(self, kind: Optional[str] = None, days: int = 30) -> list[dict]:
        floor = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        q = "SELECT * FROM event WHERE day>=?"
        args: list = [floor]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        q += " ORDER BY start_ts DESC"
        return [dict(r) for r in self.con.execute(q, args).fetchall()]

    def coverage(self) -> list[dict]:
        """What is actually arriving, per source. The first question to ask
        when a recommendation looks odd is: does this data exist?"""
        rows = self.con.execute(
            "SELECT source, COUNT(*) AS n, COUNT(DISTINCT metric) AS metrics, "
            "MIN(day) AS first_day, MAX(day) AS last_day FROM signal GROUP BY source"
        ).fetchall()
        return [dict(r) for r in rows]

    def rows_since(self, iso_ts: str) -> list[dict]:
        """Rows for cloud replication: everything with ts >= threshold."""
        try:
            floor = int(datetime.fromisoformat(iso_ts).timestamp())
        except (ValueError, TypeError):
            floor = 0
        return [dict(r) for r in self.con.execute(
            "SELECT source,metric,ts,day,value,unit,meta FROM signal WHERE ts>=? ORDER BY ts",
            (floor,)).fetchall()]

    def close(self):
        self.con.close()


def utc_epoch(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())
