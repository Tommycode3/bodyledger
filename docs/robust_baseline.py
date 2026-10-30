"""Render docs/robust_baseline.svg: median/MAD baseline vs mean/SD on SYNTHETIC data.

The data are generated here (seeded), not recorded from anyone. The baseline
and z-score use soma's own functions (`soma.fuse._median`, `soma.fuse._mad`),
with the same rolling window (`BASELINE_DAYS`) and z clamp (+/-2) as `fuse.state`.

    python3 docs/robust_baseline.py      # requires matplotlib
"""
from __future__ import annotations

import random
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from soma.fuse import BASELINE_DAYS, _mad, _median  # noqa: E402

rng = random.Random(7)
DAYS = 70
# synthetic nightly HRV (ms): slow drift + noise, plus a 3-night disruption
hrv = [55 + 4 * (i / DAYS) + rng.gauss(0, 4) for i in range(DAYS)]
for i in (30, 31, 32):
    hrv[i] = 22 + rng.gauss(0, 2)

days = list(range(DAYS))
med_line, mad_lo, mad_hi, mean_line, sd_lo, sd_hi, z_rob, z_std = ([] for _ in range(8))
for d in days:
    hist = hrv[max(0, d - BASELINE_DAYS):d]
    if len(hist) < 5:
        for lst in (med_line, mad_lo, mad_hi, mean_line, sd_lo, sd_hi, z_rob, z_std):
            lst.append(float("nan"))
        continue
    m = _median(hist)
    s = _mad(hist, m)
    mu, sd = statistics.mean(hist), statistics.stdev(hist)
    med_line.append(m); mad_lo.append(m - s); mad_hi.append(m + s)
    mean_line.append(mu); sd_lo.append(mu - sd); sd_hi.append(mu + sd)
    z_rob.append(max(-2, min(2, (hrv[d] - m) / s)))
    z_std.append(max(-2, min(2, (hrv[d] - mu) / sd)))

fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 5.6), sharex=True,
                               gridspec_kw={"height_ratios": [3, 2]})
ax1.plot(days, hrv, "o", ms=3, color="#333", label="nightly HRV (synthetic)")
ax1.plot(days, med_line, color="#1f6feb", label="median, 21-day window")
ax1.fill_between(days, mad_lo, mad_hi, color="#1f6feb", alpha=0.15, label="± 1.4826·MAD")
ax1.plot(days, mean_line, color="#d1242f", ls="--", label="mean, 21-day window")
ax1.fill_between(days, sd_lo, sd_hi, color="#d1242f", alpha=0.08, label="± SD")
ax1.set_ylabel("RMSSD [ms]")
ax1.legend(fontsize=8, ncol=2, loc="lower right", frameon=False)
ax1.set_title("Robust baseline after a 3-night disruption (synthetic data)", fontsize=10)

ax2.axhline(0, color="#999", lw=0.8)
ax2.plot(days, z_rob, color="#1f6feb", label="robust z (soma)")
ax2.plot(days, z_std, color="#d1242f", ls="--", label="classical z")
ax2.set_ylim(-2.3, 2.3)
ax2.set_ylabel("z, clamped to ±2")
ax2.set_xlabel("day")
ax2.legend(fontsize=8, loc="lower right", frameon=False)
for ax in (ax1, ax2):
    ax.spines[["top", "right"]].set_visible(False)
fig.tight_layout()
out = Path(__file__).with_suffix(".svg")
fig.savefig(out)
print(f"-> {out}")
