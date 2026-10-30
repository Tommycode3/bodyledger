"""The places where this system could lie, and the tests that prevent it."""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from soma import fuse                                    # noqa: E402
from soma.adapters.fitbit import _f                      # noqa: E402
from soma.store import Sample, Store, day_of             # noqa: E402


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    yield s
    s.close()


def day(offset: int) -> str:
    return (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")


def ts(offset: int) -> int:
    return int((datetime.now() - timedelta(days=offset)).timestamp())


def seed(store, metric, values, source="fitbit"):
    """values: {offset_days: value}"""
    store.put([Sample(source, metric, ts(o), day(o), v) for o, v in values.items()])


# --- store ----------------------------------------------------------------

def test_resync_does_not_duplicate(store):
    samples = [Sample("fitbit", "sleep.duration_min", ts(1), day(1), 420)]
    store.put(samples)
    store.put(samples)
    store.put(samples)
    rows = store.con.execute("SELECT COUNT(*) c FROM signal").fetchone()["c"]
    assert rows == 1


def test_corrected_value_overwrites(store):
    store.put([Sample("fitbit", "sleep.duration_min", ts(1), day(1), 420)])
    store.put([Sample("fitbit", "sleep.duration_min", ts(1), day(1), 435)])
    assert store.latest("sleep.duration_min")["value"] == 435


def test_different_sources_coexist(store):
    store.put([Sample("fitbit", "hr.resting", ts(0), day(0), 52),
               Sample("coros", "hr.resting", ts(0) + 1, day(0), 54)])
    assert len(store.series("hr.resting", days=2)) == 1        # one value per day
    assert store.con.execute("SELECT COUNT(*) c FROM signal").fetchone()["c"] == 2


def test_events_are_idempotent(store):
    for _ in range(3):
        store.put_event("coros", "workout", "abc123", ts(2), ts(2) + 1800, "Morning Run")
    assert len(store.events(kind="workout", days=10)) == 1


def test_day_of_uses_local_timezone():
    now = datetime.now()
    assert day_of(int(now.timestamp())) == now.strftime("%Y-%m-%d")


# --- fusion: where a wrong recommendation is costly ----------------------

def test_no_data_no_score(store):
    st = fuse.state(store, day(0))
    assert st["readiness"] is None
    assert st["confidence"] == 0
    assert set(st["missing"]) >= {"sleep", "HRV"}
    assert st["posture"] == "neutral"


def test_short_baseline_produces_no_judgement(store):
    """With 3 days of history the system has a value but no yardstick: it must
    say so, not fake a baseline."""
    seed(store, "sleep.duration_min", {0: 300, 1: 430, 2: 425, 3: 420})
    st = fuse.state(store, day(0))
    sleep = next(c for c in st["components"] if c["name"] == "sleep")
    assert sleep["z"] is None
    assert "not yet reliable" in sleep["text"]
    assert st["readiness"] is None


def test_short_night_lowers_readiness(store):
    history = {o: 430 for o in range(1, 15)}
    seed(store, "sleep.duration_min", {**history, 0: 300})
    seed(store, "hr.hrv_rmssd", {**{o: 60 for o in range(1, 15)}, 0: 42})
    low = fuse.state(store, day(0))

    store2 = Store(":memory:")
    seed(store2, "sleep.duration_min", {**history, 0: 455})
    seed(store2, "hr.hrv_rmssd", {**{o: 60 for o in range(1, 15)}, 0: 68})
    high = fuse.state(store2, day(0))

    assert low["readiness"] < high["readiness"]
    assert low["posture"] == "gentle"


def test_single_outlier_does_not_move_baseline(store):
    """Median and MAD instead of mean and standard deviation: one terrible night
    must not redefine what is normal for you."""
    usual = {o: 430 for o in range(1, 15)}
    seed(store, "sleep.duration_min", {**usual, 0: 430})
    clean = fuse.state(store, day(0))["components"][0]["baseline"]

    store2 = Store(":memory:")
    with_outlier = dict(usual)
    with_outlier[7] = 90          # a sleepless night
    seed(store2, "sleep.duration_min", {**with_outlier, 0: 430})
    dirty = fuse.state(store2, day(0))["components"][0]["baseline"]

    assert clean == dirty == 430


def test_readiness_stays_in_range(store):
    seed(store, "sleep.duration_min", {**{o: 430 for o in range(1, 15)}, 0: 12})
    seed(store, "hr.hrv_rmssd", {**{o: 60 for o in range(1, 15)}, 0: 1})
    seed(store, "hr.resting", {**{o: 52 for o in range(1, 15)}, 0: 140})
    st = fuse.state(store, day(0))
    assert 0 <= st["readiness"] <= 100


def test_partial_evidence_is_declared(store):
    seed(store, "sleep.duration_min", {**{o: 430 for o in range(1, 15)}, 0: 300})
    st = fuse.state(store, day(0))
    assert st["confidence"] == pytest.approx(0.35)
    assert "HRV" in st["missing"]
    assert st["posture"] == "neutral"    # below the evidence threshold: no assertiveness


def test_brief_does_not_invent_when_everything_is_missing(store):
    text = fuse.brief(store, day(0))
    assert "No usable data" in text
    assert "readiness" not in text.lower()


def test_acwr_flags_load_spike(store):
    # three quiet weeks (100 per week), then a 600 week
    seed(store, "training.load", {21: 100, 14: 100, 7: 100}, source="coros")
    seed(store, "training.load", {1: 300, 3: 300}, source="coros")
    # acute 600, chronic (900/4) = 225  →  2.67
    ratio = fuse.acwr(store, day(0))
    assert ratio == pytest.approx(600 / 225, rel=1e-3)
    assert "loading more than usual" in fuse.state(store, day(0))["load_note"]


def test_acwr_deload_week(store):
    seed(store, "training.load", {21: 400, 14: 400, 7: 400}, source="coros")
    ratio = fuse.acwr(store, day(0))
    assert ratio == pytest.approx(0.0)          # no load in the last 7 days


def test_acwr_absent_without_training(store):
    assert fuse.acwr(store, day(0)) is None


# --- Fitbit parsing -------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (52, 52.0), ("52", 52.0), (None, None), ("", None),
    ("40-44", 42.0),           # cardio fitness arrives as a range
    ("boh", None),
])
def test_fitbit_value_parsing(raw, expected):
    assert _f(raw) == expected


def test_lag_pairs_predictor_from_the_previous_day(tmp_path):
    """lag=1: the outcome on day d is paired with the predictor on day d-1."""
    from soma.insight import Hypothesis, _paired
    from soma.store import Sample, Store
    from datetime import datetime, timedelta
    store = Store(str(tmp_path / "t.db"))
    base = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    samples = []
    for i in range(5):
        d = base - timedelta(days=i)
        day = d.strftime("%Y-%m-%d")
        samples.append(Sample("x", "training.load", int(d.timestamp()), day, float(i)))
        samples.append(Sample("x", "hr.hrv_rmssd", int(d.timestamp()), day, float(100 + i)))
    store.put(samples)
    xs, ys, days = _paired(store, Hypothesis("training.load", "hr.hrv_rmssd", 1, ""), 30)
    # HRV on day i (value 100+i) pairs with load from day i+1 back in time (value i+1)
    assert all(y - x == 99 for x, y in zip(xs, ys)) and len(xs) == 4
