"""Fusion: from raw samples to a usable body state.

The fragile point here: **one wrong recommendation costs more than
ten right ones**. So three invariants, enforced in code:

1. Personal baseline, never population norms. Your "good" HRV is your own
   median, not a number from a magazine.
2. No data -> no claim. Every missing component drops out of the calculation
   and is declared missing; there is no default that pretends.
3. Median and MAD instead of mean and standard deviation: one bad night or a
   race must not shift the baseline.

The output is not a diagnosis: it is a falsifiable observation ("you slept 5h20
against your usual 7h05") plus a suggested *posture* for whoever consumes
`today.json`.
"""
from __future__ import annotations

import json
import os
import statistics
from datetime import datetime, timedelta
from typing import Optional

from .store import Store

TODAY_JSON = os.path.expanduser("~/.soma/today.json")

BASELINE_DAYS = 21
MIN_BASELINE_N = 5          # below this threshold the baseline is not credible
MIN_TRAINING_DAYS = 3       # below this threshold the "chronic" load is not a baseline
MIN_EVIDENCE = 0.4          # below: show the facts, don't synthesize


def _median(xs: list[float]) -> Optional[float]:
    return statistics.median(xs) if xs else None


def _mad(xs: list[float], med: float) -> float:
    """Median absolute deviation, rescaled to be comparable with a standard
    deviation. The floor avoids division by zero on flat series."""
    if not xs:
        return 0.0
    return max(1.4826 * statistics.median([abs(x - med) for x in xs]), 1e-6)


class Component:
    """One piece of evidence: value, baseline, normalized deviation, sentence."""

    def __init__(self, name: str, value: Optional[float], baseline: Optional[float],
                 z: Optional[float], text: str, weight: float):
        self.name, self.value, self.baseline = name, value, baseline
        self.z, self.text, self.weight = z, text, weight

    @property
    def available(self) -> bool:
        return self.z is not None

    def as_dict(self) -> dict:
        return {"name": self.name, "value": self.value, "baseline": self.baseline,
                "z": round(self.z, 2) if self.z is not None else None,
                "text": self.text, "weight": self.weight}


def _component(store: Store, metric: str, name: str, weight: float,
               higher_is_better: bool, fmt, day: str) -> Component:
    series = store.series(metric, days=BASELINE_DAYS + 1, end=day)
    by_day = {r["day"]: r["value"] for r in series}
    today_v = by_day.get(day)
    history = [v for d, v in by_day.items() if d != day and v is not None]
    if today_v is None:
        return Component(name, None, None, None, f"{name}: no data", weight)
    if len(history) < MIN_BASELINE_N:
        return Component(name, today_v, None, None,
                         f"{name}: {fmt(today_v)} (baseline not yet reliable, "
                         f"{len(history)} of {MIN_BASELINE_N} days)", weight)
    med = _median(history)
    z = (today_v - med) / _mad(history, med)
    if not higher_is_better:
        z = -z
    delta = "in line with"
    if abs(z) >= 1.0:
        delta = "above" if (today_v > med) else "below"
    return Component(name, today_v, med, z,
                     f"{name}: {fmt(today_v)} — {delta} your usual {fmt(med)}", weight)


def _fmt_min(v: float) -> str:
    return f"{int(v) // 60}h{int(v) % 60:02d}"


def acwr(store: Store, day: str) -> Optional[float]:
    """Acute/chronic workload ratio: 7 days against the 4-week average.
    Below 0.8 you are deloading, above 1.5 you are accumulating more than you absorb."""
    end = datetime.strptime(day, "%Y-%m-%d")
    rows = store.con.execute(
        "SELECT day, SUM(value) AS v FROM signal WHERE metric='training.load' "
        "AND day BETWEEN ? AND ? GROUP BY day",
        ((end - timedelta(days=27)).strftime("%Y-%m-%d"), day)).fetchall()
    # with two isolated runs after weeks of rest the ratio jumps to 4.00 and
    # looks like an alarm, while it is just dividing by almost zero: better stay silent
    if len(rows) < MIN_TRAINING_DAYS:
        return None
    by_day = {r["day"]: r["v"] for r in rows}
    acute = sum(v for d, v in by_day.items()
                if (end - datetime.strptime(d, "%Y-%m-%d")).days < 7)
    chronic = sum(by_day.values()) / 4.0
    if chronic <= 0:
        return None
    return acute / chronic


def state(store: Store, day: Optional[str] = None) -> dict:
    """Body state for one day. The core of the layer."""
    day = day or datetime.now().strftime("%Y-%m-%d")
    comps = [
        _component(store, "sleep.duration_min", "sleep", 0.35, True, _fmt_min, day),
        _component(store, "hr.hrv_rmssd", "HRV", 0.30, True,
                   lambda v: f"{v:.0f} ms", day),
        _component(store, "hr.resting", "resting HR", 0.20, False,
                   lambda v: f"{v:.0f} bpm", day),
        _component(store, "sleep.efficiency", "sleep efficiency", 0.15, True,
                   lambda v: f"{v:.0f}%", day),
    ]
    usable = [c for c in comps if c.available]
    evidence = sum(c.weight for c in usable)
    readiness = None
    # A score built on a single component is not a score: early in the
    # morning only resting HR had arrived and the brief headlined
    # "Readiness 94/100" while concluding "insufficient data". Below the
    # evidence threshold, show the facts and don't synthesize.
    if usable and evidence >= MIN_EVIDENCE:
        wsum = evidence
        # z clamped to ±2: a single extreme night must not dominate the score
        score = sum(max(-2.0, min(2.0, c.z)) * c.weight for c in usable) / wsum
        readiness = round(max(0, min(100, 50 + score * 22)))

    ratio = acwr(store, day)
    load_note = None
    if ratio is not None:
        if ratio > 1.5:
            load_note = f"acute/chronic load {ratio:.2f}: loading more than usual"
        elif ratio < 0.7:
            load_note = f"acute/chronic load {ratio:.2f}: deload week"
        else:
            load_note = f"acute/chronic load {ratio:.2f}: within normal range"

    posture, hint = _posture(readiness, usable)

    return {
        "day": day,
        "readiness": readiness,
        "confidence": round(sum(c.weight for c in usable), 2),
        "components": [c.as_dict() for c in comps],
        # "the data is missing" and "the data is there but I have no yardstick
        # yet" are two different things: conflating them made the brief say
        # "resting HR: 49 bpm" and two lines below "no data for: resting HR"
        "missing": [c.name for c in comps if c.value is None],
        "no_baseline": [c.name for c in comps if c.value is not None and c.z is None],
        "acwr": round(ratio, 2) if ratio is not None else None,
        "load_note": load_note,
        "posture": posture,
        "hint": hint,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _posture(readiness: Optional[int], usable: list[Component]) -> tuple[str, str]:
    """The posture a consumer of today.json should adopt today. With little evidence
    it stays neutral: the system has no right to be assertive in the dark."""
    if readiness is None or sum(c.weight for c in usable) < MIN_EVIDENCE:
        return "neutral", "insufficient data: no signal from the body"
    if readiness < 38:
        return "gentle", ("low-battery day: small, concrete goals "
                          "are worth more than ambitious ones")
    if readiness > 68:
        return "challenging", "the body can take it: the right day for the hard thing"
    return "normal", "ordinary day: usual load"


def brief(store: Store, day: Optional[str] = None) -> str:
    """The text you read (and that a local agent can quote). Only verifiable facts."""
    s = state(store, day)
    lines = []
    if s["readiness"] is None and s["confidence"] > 0:
        lines.append(f"Evidence {int(s['confidence'] * 100)}%: too little for a "
                     f"score. The facts for {s['day']}:")
        for c in s["components"]:
            if c["value"] is not None:
                lines.append("  · " + c["text"])
        if s["missing"]:
            lines.append("  ⃰ not yet arrived: " + ", ".join(s["missing"]))
        lines.append("→ " + s["hint"])
        return "\n".join(lines)

    if s["readiness"] is None:
        lines.append(f"No usable data for {s['day']}.")
        if s["missing"]:
            lines.append("Missing: " + ", ".join(s["missing"]) + ".")
        lines.append("Run `soma sync`; if it stays empty, the device has not synced.")
        return "\n".join(lines)

    lines.append(f"Readiness {s['readiness']}/100 · posture {s['posture']} "
                 f"(evidence {int(s['confidence'] * 100)}%)")
    for c in s["components"]:
        if c["z"] is not None or c["value"] is not None:
            lines.append("  · " + c["text"])
    if s["load_note"]:
        lines.append("  · " + s["load_note"])
    if s["missing"]:
        lines.append("  ⃰ no data for: " + ", ".join(s["missing"]))
    if s["no_baseline"]:
        lines.append("  ⃰ no baseline yet: " + ", ".join(s["no_baseline"]))
    lines.append(f"→ {s['hint']}")
    return "\n".join(lines)


def write_today(store: Store) -> str:
    """Writes ~/.soma/today.json: the integration point for any local agent or
    program. A file, not an API: if soma isn't running, whoever reads it
    just finds a stale file and can ignore it."""
    s = state(store)
    os.makedirs(os.path.dirname(TODAY_JSON), exist_ok=True)
    with open(TODAY_JSON, "w") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    return TODAY_JSON
