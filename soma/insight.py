"""From data to knowledge — with the brakes on.

This is the module that can do the most damage, because it produces sentences
that sound like truth. With dozens of metrics there are hundreds of testable
pairs: search them all, and chance alone hands you dozens of "significant"
ones at 5%. Such a system would state with perfect confidence that your
oxygen saturation predicts the next day's steps, and you would believe it,
because the computer said so about your own data.

Four constraints, all enforced in code:

1. **Pre-registered hypotheses.** Only the list below is tested, written before
   looking at the data and with an expected direction. No exhaustive scan:
   the number of tests is fixed and small.
2. **Spearman, not Pearson.** Rank instead of value: a 90-minute night does
   not drag a line, and no normality is assumed.
3. **Circular-shift permutation, not the textbook p-value.** Daily series
   are autocorrelated (you sleep badly two nights in a row), and the classic
   formula on autocorrelated data hands out significance that does not exist.
   The circular shift compares the observed correlation with that of the same
   series shifted in time: it preserves autocorrelation and **uses no random
   numbers**, so the same data always give the same answer.
4. **Benjamini-Hochberg over the total number of tests.** The false-positive
   rate is controlled over the whole family, and the number of tests run is
   always declared — even when nothing survives.

And the rule that matters most: **correlation is not causation**, not even
with a time lag. The generated text says "goes together with", never
"causes"; and a relationship that survives is a hypothesis to test by changing
something, not a prescription.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .store import METRICS, Store

MIN_PAIRS = 20          # below: no claim, only "not yet"
FDR_Q = 0.10            # accepted false-positive rate over the family
LOOKBACK_DAYS = 180


@dataclass(frozen=True)
class Hypothesis:
    predictor: str
    outcome: str
    lag: int            # days: 1 = the predictor is from yesterday. Sleep and
                        # overnight metrics are dated by wake-up day, so
                        # "day t -> following night" is lag 1.
    question: str       # plain language, human-readable


# --- the hypotheses, written before seeing the data ------------------------
# Two families, each with a declared physiological or behavioral rationale.
HYPOTHESES = [
    # training and recovery
    Hypothesis("training.load", "hr.hrv_rmssd", 1,
               "training load goes together with the next day's HRV"),
    Hypothesis("training.load", "sleep.deep_min", 1,
               "training load goes together with the following night's deep sleep"),
    Hypothesis("training.load", "hr.resting", 1,
               "training load goes together with the next day's resting HR"),

    # inside the body
    Hypothesis("sleep.start_hour", "hr.hrv_rmssd", 0,
               "the time you fall asleep goes together with that night's HRV"),
    Hypothesis("sleep.duration_min", "hr.resting", 0,
               "longer nights go together with a lower resting HR"),
    Hypothesis("activity.steps", "sleep.duration_min", 1,
               "more active days go together with a longer following night"),
    Hypothesis("activity.active_min", "hr.hrv_rmssd", 1,
               "active minutes go together with the next day's HRV"),
]


# --- statistics -------------------------------------------------------------

def _ranks(xs: list[float]) -> list[float]:
    """Ranks, averaging over ties."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        mean_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            r[order[k]] = mean_rank
        i = j + 1
    return r


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    sxx = sum((a - mx) ** 2 for a in xs)
    syy = sum((b - my) ** 2 for b in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / (sxx * syy) ** 0.5


def spearman(xs: list[float], ys: list[float]) -> Optional[float]:
    return _pearson(_ranks(xs), _ranks(ys))


def circular_shift_p(xs: list[float], ys: list[float], rho: float) -> float:
    """Circular-shift p-value.

    Each shift of `ys` breaks its pairing with the days of `xs` but preserves
    the series' internal temporal structure. The fraction of shifts that reach
    a correlation as strong as the observed one is the p-value. Deterministic:
    no seed, no variability between two runs on the same data.
    """
    n = len(ys)
    if n < MIN_PAIRS:
        return 1.0
    extremes = 0
    trials = 0
    # the smallest shifts are skipped: shifting by 1 day leaves the pairing
    # almost intact and would produce fake "chance" cases indistinguishable from the real one
    for s in range(3, n - 2):
        r = spearman(xs, ys[s:] + ys[:s])
        if r is None:
            continue
        trials += 1
        if abs(r) >= abs(rho):
            extremes += 1
    if trials == 0:
        return 1.0
    # +1 in numerator and denominator: a p-value of exactly 0 would be a
    # certainty that a permutation test can never grant
    return (extremes + 1) / (trials + 1)


def benjamini_hochberg(pvalues: list[float], q: float = FDR_Q) -> list[bool]:
    """Which tests survive when controlling the false-positive rate."""
    m = len(pvalues)
    if m == 0:
        return []
    ordered = sorted(range(m), key=lambda i: pvalues[i])
    max_rank = -1
    for rank, i in enumerate(ordered, start=1):
        if pvalues[i] <= q * rank / m:
            max_rank = rank
    kept = [False] * m
    for rank, i in enumerate(ordered, start=1):
        if rank <= max_rank:
            kept[i] = True
    return kept


# --- the engine -------------------------------------------------------------

def _paired(store: Store, h: Hypothesis, days: int) -> tuple[list[float], list[float], list[str]]:
    """Days on which BOTH measurements exist, with the lag applied."""
    from datetime import datetime, timedelta

    pred = {r["day"]: r["value"] for r in store.series(h.predictor, days=days)}
    out = {r["day"]: r["value"] for r in store.series(h.outcome, days=days)}
    xs, ys, paired_days = [], [], []
    for day, y in sorted(out.items()):
        d = datetime.strptime(day, "%Y-%m-%d") - timedelta(days=h.lag)
        key = d.strftime("%Y-%m-%d")
        if key in pred:
            xs.append(pred[key])
            ys.append(y)
            paired_days.append(day)
    return xs, ys, paired_days


@dataclass
class Result:
    hypothesis: Hypothesis
    n: int
    rho: Optional[float]
    p: Optional[float]
    survives: bool = False

    @property
    def testable(self) -> bool:
        return self.n >= MIN_PAIRS and self.rho is not None


def run(store: Store, days: int = LOOKBACK_DAYS) -> dict:
    raw = []
    for h in HYPOTHESES:
        xs, ys, _ = _paired(store, h, days)
        rho = spearman(xs, ys) if len(xs) >= MIN_PAIRS else None
        p = circular_shift_p(xs, ys, rho) if rho is not None else None
        raw.append(Result(h, len(xs), rho, p))

    testable = [r for r in raw if r.testable]
    kept = benjamini_hochberg([r.p for r in testable])
    for r, ok in zip(testable, kept):
        r.survives = ok

    return {
        "results": raw,
        "tested": len(testable),
        "total": len(raw),
        "survivors": [r for r in raw if r.survives],
        "not_enough": [r for r in raw if not r.testable],
        "q": FDR_Q,
        "min_pairs": MIN_PAIRS,
    }


def _strength(rho: float) -> str:
    a = abs(rho)
    if a >= 0.5:
        return "strong"
    if a >= 0.3:
        return "moderate"
    return "weak"


def report(store: Store, days: int = LOOKBACK_DAYS, verbose: bool = False) -> str:
    r = run(store, days)
    out = [f"Pre-registered hypotheses: {r['total']} · testable today: {r['tested']} "
           f"({r['min_pairs']} paired days needed) · FDR q={r['q']}"]

    if not r["tested"]:
        out.append("\nNo hypothesis has enough paired data yet.")
        out.append("Relationships become testable after a few weeks of data.")
    elif not r["survivors"]:
        out.append(f"\nNo relationship survives the false-positive control.")
        out.append("This is not a failure: it is the honest answer with this data.")
    else:
        out.append("\nSurviving relationships:")
        for x in sorted(r["survivors"], key=lambda z: -abs(z.rho)):
            sign = "＋" if x.rho > 0 else "－"
            out.append(f"  {sign} {x.hypothesis.question}")
            out.append(f"    rho {x.rho:+.2f} ({_strength(x.rho)}) · n={x.n} days · p={x.p:.3f}")
        out.append("\n  They go together, which does not mean one causes the other.")
        out.append("  Treat them as experiments to try, not as instructions.")

    if verbose:
        out.append("\nAll hypotheses:")
        for x in r["results"]:
            if not x.testable:
                out.append(f"  · n={x.n:<3} (insufficient)  {x.hypothesis.question}")
            else:
                outcome = "survives" if x.survives else "fails"
                out.append(f"  · rho {x.rho:+.2f} p={x.p:.3f} n={x.n:<3} {outcome:<11} "
                           f"{x.hypothesis.question}")

    # the parentheses matter: without them, `-` binds before `|` and the warning lists everything
    used = {h.predictor for h in HYPOTHESES} | {h.outcome for h in HYPOTHESES}
    missing = sorted(used - set(METRICS))
    if missing:
        out.append("\n⚠ hypotheses on metrics not in the vocabulary: " + ", ".join(missing))
    return "\n".join(out)
