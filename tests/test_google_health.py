"""Google Health → canonical vocabulary mapping.

The adapter cannot be exercised without a real OAuth, so these tests cover
everything *below* the network: parsing, time windows, extractors. The
synthetic responses follow the real schemas of the v4 discovery document
(int64 as strings, `Date` as {year, month, day}, `DataPoint` union).
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from soma.adapters.google_health import (            # noqa: E402
    DAILY_TYPES, ROLLUP_TYPES, GoogleHealth, _civil_day, _date_of, _local_time,
    _main_session, _num, _offset_seconds,
)
from soma.store import Store                          # noqa: E402


@pytest.fixture()
def client(tmp_path):
    """Instance that bypasses the Keychain: these tests cover the mapping."""
    g = object.__new__(GoogleHealth)
    g.store = Store(str(tmp_path / "t.db"))
    g.tokens = {}
    yield g
    g.store.close()


def daily(name):
    return next(d for d in DAILY_TYPES if d.path == name)


def rollup(name):
    return next(r for r in ROLLUP_TYPES if r[0] == name)


# --- primitives ------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("420", 420.0),      # int64 arrives as a string
    (52.5, 52.5), (0, 0.0), (None, None), ("", None), ("junk", None),
])
def test_num(raw, expected):
    assert _num(raw) == expected


def test_date_of_rebuilds_the_date_type():
    assert _date_of({"year": 2026, "month": 8, "day": 7}) == "2026-08-07"
    assert _date_of({"year": 2026, "month": 12, "day": 31}) == "2026-12-31"
    assert _date_of({}) is None
    assert _date_of(None) is None


# --- time windows --------------------------------------------------------

def test_windows_cover_the_period_without_gaps(client):
    today = date(2026, 8, 7)
    w = list(client._windows(today, days=100, size=90))
    assert w[0][1] == "2026-08-08"                 # `end` exclusive: includes today
    # each window starts exactly where the previous one ends
    for before, after in zip(w, w[1:]):
        assert after[1] == before[0]
    assert w[-1][0] == "2026-04-30"                # 100 days back
    assert len(w) == 2


def test_windows_respect_the_tight_limit(client):
    """total-calories accepts at most 14 days per request."""
    w = list(client._windows(date(2026, 8, 7), days=30, size=14))
    assert len(w) == 3
    assert all((date.fromisoformat(b) - date.fromisoformat(a)).days <= 14 for a, b in w)


# --- daily extractors ----------------------------------------------------

def test_hrv():
    out = daily("daily-heart-rate-variability").extract({
        "averageHeartRateVariabilityMilliseconds": 48.2,
        "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds": 61.7,
    })
    assert out["hr.hrv_rmssd"] == 48.2
    assert out["hr.hrv_deep_rmssd"] == 61.7


def test_resting_hr_as_string():
    assert daily("daily-resting-heart-rate").extract(
        {"beatsPerMinute": "54"})["hr.resting"] == 54.0


def test_temperature_is_a_deviation():
    """The API gives value and baseline separately: the canonical metric is the difference."""
    out = daily("daily-sleep-temperature-derivations").extract({
        "nightlyTemperatureCelsius": 36.4, "baselineTemperatureCelsius": 36.1})
    assert out["body.temp_delta"] == pytest.approx(0.3)


def test_temperature_without_baseline_does_not_invent():
    out = daily("daily-sleep-temperature-derivations").extract(
        {"nightlyTemperatureCelsius": 36.4})
    assert out["body.temp_delta"] is None


# --- rollup ----------------------------------------------------------------

def test_steps():
    assert rollup("steps")[1]({"steps": {"countSum": "8421"}})["activity.steps"] == 8421.0


def test_distance_from_millimeters_to_meters():
    out = rollup("distance")[1]({"distance": {"millimetersSum": "10496380"}})
    assert out["activity.distance_m"] == pytest.approx(10496.38)


def test_zone_minutes_sums_the_three_zones():
    out = rollup("active-zone-minutes")[1]({"activeZoneMinutes": {
        "sumInFatBurnHeartZone": "22", "sumInCardioHeartZone": "11",
        "sumInPeakHeartZone": "3"}})
    assert out["activity.active_min"] == 36


def test_empty_rollup_does_not_produce_fake_zeros():
    assert rollup("steps")[1]({})["activity.steps"] is None
    assert rollup("active-zone-minutes")[1]({})["activity.active_min"] is None


# --- sleep: shapes observed in real data ---------------------------------

# A night as it really arrives: `civilEndTime` is NOT there (sessions imported
# from HealthKit lack it), offsets are protobuf durations, integers are strings.
NIGHT = {
    "interval": {
        "startTime": "2026-08-06T22:34:00Z", "startUtcOffset": "7200s",
        "endTime": "2026-08-07T07:18:00Z", "endUtcOffset": "7200s",
    },
    "type": "STAGES",
    "metadata": {"processed": True, "externalId": "100000000000000001", "mainSleep": True},
    "summary": {
        "minutesInSleepPeriod": "524", "minutesAsleep": "486", "minutesAwake": "38",
        "stagesSummary": [
            {"type": "AWAKE", "minutes": "38"}, {"type": "LIGHT", "minutes": "311"},
            {"type": "DEEP", "minutes": "86"}, {"type": "REM", "minutes": "89"},
        ],
    },
}

# Same night recorded by the other source: no mainSleep, different duration.
TWIN = {
    "interval": {
        "startTime": "2026-08-06T22:53:00Z", "startUtcOffset": "7200s",
        "endTime": "2026-08-07T05:38:00Z", "endUtcOffset": "7200s",
    },
    "metadata": {"stagesStatus": "SUCCEEDED", "processed": True},
    "summary": {"minutesInSleepPeriod": "465", "minutesAsleep": "459", "minutesAwake": "6",
                "stagesSummary": [{"type": "DEEP", "minutes": "82"}]},
}

NAP = {
    "interval": {"startTime": "2026-08-05T13:34:00Z", "startUtcOffset": "7200s",
                 "endTime": "2026-08-05T14:30:00Z", "endUtcOffset": "7200s"},
    "metadata": {"processed": True, "nap": True, "externalId": "100000000000000002"},
    "summary": {"minutesInSleepPeriod": "56", "minutesAsleep": "56", "minutesAwake": "0",
                "stagesSummary": [{"type": "ASLEEP", "minutes": "56"}]},
}


def test_day_rebuilt_without_civil_end_time():
    """The defect that zeroed out all sleep: `civilEndTime` is documented as
    an output field but never arrives in real data."""
    assert _civil_day(NIGHT["interval"]) == "2026-08-07"


def test_day_uses_civil_end_time_when_present():
    iv = {**NIGHT["interval"],
          "civilEndTime": {"date": {"year": 2026, "month": 8, "day": 9}}}
    assert _civil_day(iv) == "2026-08-09"


def test_day_is_the_wake_up_day_not_the_fall_asleep_day():
    """Asleep at 00:34 local on the 7th (still the 6th in UTC): the night belongs to the 7th."""
    assert _civil_day(NIGHT["interval"]) == "2026-08-07"
    assert NIGHT["interval"]["startTime"].startswith("2026-08-06")


def test_offset_protobuf():
    assert _offset_seconds("7200s") == 7200
    assert _offset_seconds("-18000s") == -18000
    assert _offset_seconds(None) == 0
    assert _offset_seconds("junk") == 0


def test_local_time_from_offsets_not_from_machine_time_zone():
    """When travelling, the machine's time zone is the current one, not the one
    slept in. 22:34Z + 2h = 00:34 local."""
    local = _local_time("2026-08-06T22:34:00Z", "7200s")
    assert (local.hour, local.minute) == (0, 34)
    ny = _local_time("2026-08-06T22:34:00Z", "-14400s")     # New York
    assert (ny.hour, ny.minute) == (18, 34)


def test_night_mapped(client):
    got = {s.metric: s.value for s in client._sleep_samples(NIGHT, "2026-08-07")}
    assert got["sleep.duration_min"] == 486
    assert got["sleep.awake_min"] == 38
    assert got["sleep.deep_min"] == 86
    assert got["sleep.rem_min"] == 89
    assert got["sleep.light_min"] == 311
    assert got["sleep.start_hour"] == pytest.approx(0.57, abs=0.01)   # 00:34
    assert got["sleep.end_hour"] == pytest.approx(9.30, abs=0.01)     # 09:18


def test_efficiency_is_derived_and_says_so(client):
    eff = next(s for s in client._sleep_samples(NIGHT, "2026-08-07")
               if s.metric == "sleep.efficiency")
    assert eff.value == pytest.approx(92.7, abs=0.1)      # 486/524
    assert eff.meta == {"derived": True}


def test_duplicate_night_is_not_decided_by_chance():
    """The same night arrives from two sources. Without an explicit choice
    the last one written would win."""
    assert _main_session([TWIN, NIGHT]) is NIGHT
    assert _main_session([NIGHT, TWIN]) is NIGHT      # order does not matter


def test_without_main_sleep_the_longest_wins():
    short = {**TWIN, "summary": {**TWIN["summary"], "minutesAsleep": "120"}}
    assert _main_session([short, TWIN]) is TWIN


def test_unknown_stages_ignored_without_breaking(client):
    odd = {**NIGHT, "summary": {**NIGHT["summary"], "stagesSummary": [
        {"type": "RESTLESS", "minutes": "12"}, {"type": "DEEP", "minutes": "86"}]}}
    got = {s.metric: s.value for s in client._sleep_samples(odd, "2026-08-07")}
    assert got["sleep.deep_min"] == 86
    assert not any(k.endswith("restless_min") for k in got)


def test_nap_recognized():
    assert (NAP["metadata"]).get("nap") is True
    assert _civil_day(NAP["interval"]) == "2026-08-05"


def test_event_stored_with_stable_id(client):
    """Re-syncing must not duplicate the same night."""
    for _ in range(3):
        client._put_sleep_event(NIGHT, "2026-08-07")
    ev = client.store.events(kind="sleep", days=400)
    assert len(ev) == 1
    assert (ev[0]["end_ts"] - ev[0]["start_ts"]) // 60 == 524


def test_session_without_times_produces_no_event(client):
    client._put_sleep_event({"interval": {}, "metadata": {}}, "2026-08-07")
    assert client.store.events(kind="sleep", days=400) == []


# --- the most likely silent failure ---------------------------------------

def test_invalid_grant_explains_the_7_day_expiry():
    """Without this message sync would stop after a week without saying why."""
    from soma.adapters.google_health import _refresh_hint
    from soma.net import HttpError

    e = HttpError(400, '{"error":"invalid_grant","error_description":"Token has been expired or revoked."}', TOKEN_URL_FAKE)
    msg = _refresh_hint(e)
    assert "7" in msg and "Testing" in msg
    assert "In production" in msg


def test_other_refresh_errors_do_not_guess():
    from soma.adapters.google_health import _refresh_hint
    from soma.net import HttpError

    msg = _refresh_hint(HttpError(500, "boom", TOKEN_URL_FAKE))
    assert "500" in msg
    assert "Testing" not in msg


TOKEN_URL_FAKE = "https://oauth2.googleapis.com/token"
