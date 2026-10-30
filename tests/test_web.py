"""Dashboard: the payload and its promises.

The promise that matters is the one about gaps. If a day without data became
just another point on the line, the chart would tell a continuity that never
happened — and the information is exactly in the gaps (you did not wear the
watch, you did not work, you were away).
"""
import json
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from soma import web                                     # noqa: E402
from soma.store import Sample, Store                     # noqa: E402


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    yield s
    s.close()


def day(offset):
    return (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")


def ts(offset):
    return int((datetime.now() - timedelta(days=offset)).timestamp())


def seed(store, metric, values, source="google_health"):
    store.put([Sample(source, metric, ts(o), day(o), v) for o, v in values.items()])


# --- series and gaps -------------------------------------------------------

def test_series_covers_every_day_of_the_window(store):
    seed(store, "sleep.duration_min", {0: 420})
    s = web._series_over_days(store, "sleep.duration_min", 30)
    assert len(s) == 30
    assert s[-1]["day"] == day(0)
    assert s[0]["day"] == day(29)


def test_gaps_stay_gaps(store):
    """A day without data comes out as None, not as an interpolated value."""
    seed(store, "sleep.duration_min", {0: 420, 3: 400})
    s = web._series_over_days(store, "sleep.duration_min", 5)
    per_day = {p["day"]: p["v"] for p in s}
    assert per_day[day(0)] == 420
    assert per_day[day(3)] == 400
    assert per_day[day(1)] is None
    assert per_day[day(2)] is None


def test_series_is_ordered_oldest_first(store):
    s = web._series_over_days(store, "sleep.duration_min", 10)
    days = [p["day"] for p in s]
    assert days == sorted(days)


# --- statistics -----------------------------------------------------------

def test_stats_ignores_gaps():
    st = web._stats([None, 10, None, 20, 30, None])
    assert st["n"] == 3
    assert st["last"] == 30
    assert st["median"] == 20
    assert (st["min"], st["max"]) == (10, 30)


def test_stats_on_empty_series_does_not_invent():
    st = web._stats([None, None])
    assert st["last"] is None and st["median"] is None and st["n"] == 0


def test_last_is_last_observed_not_last_day():
    """If today's data has not arrived yet, "last" is yesterday's."""
    assert web._stats([5, 9, None])["last"] == 9


# --- payload ---------------------------------------------------------------

def test_payload_serializable_and_complete(store):
    seed(store, "sleep.duration_min", {o: 400 + o for o in range(20)})
    d = web.build_data(store)
    for key in ("today", "brief", "panels", "insight", "coverage", "events", "generated"):
        assert key in d
    json.dumps(d, default=str)          # must be able to leave the server


def test_no_card_for_metrics_that_never_arrived(store):
    """An empty card is noise: if the data never arrived, there is no card."""
    seed(store, "sleep.duration_min", {0: 420})
    d = web.build_data(store)
    metrics = {c["metric"] for p in d["panels"] for c in p["cards"]}
    assert metrics == {"sleep.duration_min"}


def test_empty_panels_do_not_appear(store):
    seed(store, "sleep.duration_min", {0: 420})
    d = web.build_data(store)
    assert [p["title"] for p in d["panels"]] == ["Sleep"]


def test_empty_store_does_not_crash(store):
    d = web.build_data(store)
    assert d["panels"] == []
    assert d["today"]["readiness"] is None
    assert "No usable data" in d["brief"]


def test_every_card_carries_direction_and_unit(store):
    seed(store, "hr.resting", {o: 50 + (o % 3) for o in range(15)})
    d = web.build_data(store)
    card = d["panels"][0]["cards"][0]
    # for resting HR "lower is better": without the direction, the delta
    # would color a deterioration green
    assert card["higher_better"] is False
    assert card["unit"] == "bpm"
    assert card["desc"]


def test_hypotheses_are_ordered_by_relevance(store):
    d = build_with_hypotheses(store)
    hyps = d["insight"]["hypotheses"]
    testable = [h["testable"] for h in hyps]
    # testable ones first, then the rest: no random shuffling
    assert testable == sorted(testable, reverse=True)


def build_with_hypotheses(store):
    for o in range(25):
        seed(store, "sleep.duration_min", {o: 400 + (o * 7) % 90})
        seed(store, "hr.resting", {o: 50 + (o * 3) % 9})
    return web.build_data(store)


# --- server ----------------------------------------------------------------

def test_no_endpoint_that_writes():
    """Looking at your own data must not be able to change your own data."""
    handler = web.make_handler()
    assert hasattr(handler, "do_GET")
    assert not hasattr(handler, "do_POST")
    assert not hasattr(handler, "do_DELETE")
    assert not hasattr(handler, "do_PUT")


def test_dashboard_exists_next_to_module():
    import pathlib
    p = pathlib.Path(web.__file__).parent / "dashboard.html"
    assert p.exists()
    text = p.read_text()
    # local-first: no external assets, not even a font
    for forbidden in ("http://", "https://", "//cdn", "@import"):
        assert forbidden not in text, f"external asset in the dashboard: {forbidden}"
