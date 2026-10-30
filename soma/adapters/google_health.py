"""Google Health API (v4) adapter — the successor of the Fitbit Web API.

Google shuts down the Fitbit Web API in **September 2026** and moves Fitbit data
to the Google Health API: new platform, new console, Google OAuth, and above
all **tokens do not carry over** — a new consent is required.

API structure, verified against the discovery document
(`https://health.googleapis.com/$discovery/rest?version=v4`):

- a single endpoint scheme for every type:
  `GET /v4/users/me/dataTypes/{data-type}/dataPoints?filter=...`
- types have kebab-case ids (`daily-heart-rate-variability`), but filter
  fields are snake_case (`daily_heart_rate_variability.date`);
- a `DataPoint` is a union: exactly one field is set, named after the type
  in camelCase (`dailyHeartRateVariability`). Three conventions for the same
  thing, so each type here declares all three instead of deriving them.
- integers arrive as **strings** (int64 in JSON), hence `_num`.

Cumulative metrics (steps, distance, floors) use `dataPoints:dailyRollUp`,
which aggregates server-side: requesting intraday points would mean
downloading thousands of samples to derive a single one.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets as pysecrets
import urllib.parse
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .. import net, secrets
from ..net import HttpError
from ..store import Sample, Store

SOURCE = "google_health"
API = "https://health.googleapis.com/v4"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REDIRECT_PORT = 8731
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"

SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
]


class GoogleHealthError(Exception):
    pass


def _num(v):
    """int64-as-string, float, or None."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _date_of(d: dict) -> str | None:
    """Google's `Date` type is {year, month, day}, not a string."""
    if not d or not d.get("year"):
        return None
    return f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"


def _ts_at(day: str, hour: int) -> int:
    return int(datetime.strptime(f"{day} {hour:02d}:00", "%Y-%m-%d %H:%M").timestamp())


def _instant(rfc3339: str | None) -> datetime | None:
    if not rfc3339:
        return None
    try:
        return datetime.fromisoformat(rfc3339.replace("Z", "+00:00"))
    except ValueError:
        return None


def _offset_seconds(raw: str | None) -> int:
    """Offsets arrive as protobuf durations: `"7200s"`."""
    if not raw:
        return 0
    try:
        return int(str(raw).rstrip("s"))
    except ValueError:
        return 0


def _local_time(rfc3339: str | None, offset: str | None) -> datetime | None:
    """UTC instant + declared offset = wall-clock time on the wrist at that
    moment. The machine's time zone is not used: it is the current one, not the
    one in which the data was recorded."""
    inst = _instant(rfc3339)
    if not inst:
        return None
    return inst + timedelta(seconds=_offset_seconds(offset))


def _civil_day(interval: dict) -> str | None:
    """Civil day on which the session **ends**: a night belongs to the day
    you wake up.

    `civilEndTime` is documented as an output field, but in real data it is
    missing (sessions imported from HealthKit lack it): in that case the day
    is reconstructed from `endTime` + `endUtcOffset`.
    """
    civil = _date_of((interval.get("civilEndTime") or {}).get("date"))
    if civil:
        return civil
    local = _local_time(interval.get("endTime"), interval.get("endUtcOffset"))
    return local.strftime("%Y-%m-%d") if local else None


def _main_session(candidates: list[dict]) -> dict:
    """Only one session can define "how much I slept last night".

    The same night often arrives twice — the watch via HealthKit and the native
    device both record it — with slightly different durations. Without an
    explicit choice the last one written would win, i.e. chance. Instead the
    session marked `mainSleep` wins, and on a tie the longest one.
    """
    return max(candidates, key=lambda s: (
        1 if (s.get("metadata") or {}).get("mainSleep") else 0,
        _num((s.get("summary") or {}).get("minutesAsleep")) or 0))


# ---------------------------------------------------------------------------
# Daily types: one data point per day, filterable by `.date`
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Daily:
    path: str          # id in the path, kebab-case
    field: str         # DataPoint union field, camelCase
    filter_key: str    # filter field, snake_case
    max_days: int
    extract: callable   # (payload) -> {metric: value}


DAILY_TYPES = [
    Daily("daily-resting-heart-rate", "dailyRestingHeartRate", "daily_resting_heart_rate", 90,
          lambda v: {"hr.resting": _num(v.get("beatsPerMinute"))}),
    Daily("daily-heart-rate-variability", "dailyHeartRateVariability",
          "daily_heart_rate_variability", 90,
          lambda v: {
              "hr.hrv_rmssd": _num(v.get("averageHeartRateVariabilityMilliseconds")),
              "hr.hrv_deep_rmssd": _num(
                  v.get("deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds")),
          }),
    Daily("daily-respiratory-rate", "dailyRespiratoryRate", "daily_respiratory_rate", 90,
          lambda v: {"body.breathing_rate": _num(v.get("breathsPerMinute"))}),
    Daily("daily-oxygen-saturation", "dailyOxygenSaturation", "daily_oxygen_saturation", 90,
          lambda v: {"body.spo2": _num(v.get("averagePercentage"))}),
    Daily("daily-vo2-max", "dailyVo2Max", "daily_vo2_max", 90,
          lambda v: {"training.vo2max": _num(v.get("vo2Max"))}),
    Daily("daily-sleep-temperature-derivations", "dailySleepTemperatureDerivations",
          "daily_sleep_temperature_derivations", 90, lambda v: {
              "body.temp_delta": (
                  _num(v.get("nightlyTemperatureCelsius")) -
                  _num(v.get("baselineTemperatureCelsius"))
                  if _num(v.get("nightlyTemperatureCelsius")) is not None
                  and _num(v.get("baselineTemperatureCelsius")) is not None else None)}),
]

# Cumulative metrics: aggregated server-side by dailyRollUp.
ROLLUP_TYPES = [
    ("steps", lambda r: {"activity.steps": _num((r.get("steps") or {}).get("countSum"))}, 90),
    ("distance", lambda r: {"activity.distance_m": (
        _num((r.get("distance") or {}).get("millimetersSum")) / 1000
        if _num((r.get("distance") or {}).get("millimetersSum")) is not None else None)}, 90),
    ("floors", lambda r: {"activity.floors": _num((r.get("floors") or {}).get("countSum"))}, 90),
    # total-calories has a tighter range limit than the others
    ("total-calories", lambda r: {"activity.calories": _num(
        (r.get("totalCalories") or {}).get("kcalSum"))}, 14),
    ("active-zone-minutes", lambda r: {"activity.active_min": sum(
        _num(v) or 0 for k, v in (r.get("activeZoneMinutes") or {}).items()
        if k.startswith("sumIn")) or None}, 90),
]

STAGE_TO_METRIC = {"DEEP": "sleep.deep_min", "REM": "sleep.rem_min",
                   "LIGHT": "sleep.light_min", "AWAKE": "sleep.awake_min"}


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

def authorize(client_id: str, client_secret: str = "", timeout_s: int = 300) -> None:
    from .fitbit import _CallbackHandler, _serve_until   # same loopback, a single implementation
    import http.server

    verifier = base64.urlsafe_b64encode(os.urandom(64)).decode().rstrip("=")[:128]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = pysecrets.token_urlsafe(16)

    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "scope": " ".join(SCOPES),
        "redirect_uri": REDIRECT_URI, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
        # without these two Google does not issue a refresh token
        "access_type": "offline", "prompt": "consent",
    })

    _CallbackHandler.result = {}
    server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), _CallbackHandler)
    server.timeout = 1
    import threading
    t = threading.Thread(target=_serve_until, args=(server, timeout_s), daemon=True)
    t.start()
    print(f"Opening the browser to authorize Google Health.\nIf it does not open: {url}\n")
    webbrowser.open(url)
    t.join(timeout_s + 2)
    server.server_close()

    res = _CallbackHandler.result
    if not res.get("code"):
        raise GoogleHealthError(f"no code received ({res or 'timeout'})")
    if res.get("state") != state:
        raise GoogleHealthError("state mismatch — authorization discarded")

    form = {"client_id": client_id, "grant_type": "authorization_code",
            "code": res["code"], "code_verifier": verifier, "redirect_uri": REDIRECT_URI}
    if client_secret:
        form["client_secret"] = client_secret
    tok = net.post(TOKEN_URL, form=form)
    if "access_token" not in tok:
        raise GoogleHealthError(f"no token issued: {tok}")
    if not tok.get("refresh_token"):
        raise GoogleHealthError(
            "Google did not issue a refresh token: revoke access at "
            "myaccount.google.com/permissions and retry (prompt=consent is required).")
    _save(tok, client_id, client_secret)


def _refresh_hint(e: HttpError) -> str:
    """The most likely way this integration dies silently.

    Google revokes refresh tokens after **7 days** for apps whose consent
    screen is in "Testing" status with user type "External". With a personal
    Gmail account that is the default, so without this message sync would
    stop after a week without saying why.
    """
    try:
        err = json.loads(e.body).get("error", "")
    except (json.JSONDecodeError, AttributeError):
        err = ""
    if err == "invalid_grant":
        return ("refresh token revoked by Google. This almost always means the "
                "consent screen is in 'Testing' status, where tokens last 7 "
                "days.\n  Fix: Google Cloud Console → OAuth consent screen → "
                "Publishing status → 'In production' (personal use is an explicit "
                "exemption from verification), then `soma auth google`.")
    return f"refresh failed ({e.status}) — re-authorize with `soma auth google`"


def _save(tok: dict, client_id: str, client_secret: str, keep_refresh: str = "") -> None:
    secrets.set_json("google_health.tokens", {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token") or keep_refresh,
        "expires_at": int(datetime.now().timestamp()) + int(tok.get("expires_in", 3600)) - 120,
        "client_id": client_id, "client_secret": client_secret,
    })


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class GoogleHealth:
    def __init__(self, store: Store):
        self.store = store
        self.tokens = secrets.get_json("google_health.tokens")
        if not self.tokens:
            raise GoogleHealthError(
                "Google Health not authorized — run `soma auth google`")

    def _token(self) -> str:
        if datetime.now().timestamp() >= self.tokens.get("expires_at", 0):
            self._refresh()
        return self.tokens["access_token"]

    def _refresh(self) -> None:
        form = {"grant_type": "refresh_token",
                "refresh_token": self.tokens["refresh_token"],
                "client_id": self.tokens.get("client_id", "")}
        if self.tokens.get("client_secret"):
            form["client_secret"] = self.tokens["client_secret"]
        try:
            tok = net.post(TOKEN_URL, form=form)
        except HttpError as e:
            raise GoogleHealthError(_refresh_hint(e)) from None
        # the refresh token is not sent back: keep the existing one
        _save(tok, self.tokens.get("client_id", ""), self.tokens.get("client_secret", ""),
              keep_refresh=self.tokens["refresh_token"])
        self.tokens = secrets.get_json("google_health.tokens")

    def _list(self, data_type: str, flt: str, page_size: int = 1000) -> list[dict]:
        out, token = [], None
        while True:
            params = {"filter": flt, "pageSize": page_size}
            if token:
                params["pageToken"] = token
            d = net.get(f"{API}/users/me/dataTypes/{data_type}/dataPoints", params=params,
                        headers={"Authorization": f"Bearer {self._token()}"})
            out.extend(d.get("dataPoints") or [])
            token = d.get("nextPageToken")
            if not token:
                return out

    def _rollup(self, data_type: str, start: str, stop: str) -> list[dict]:
        """`stop` is exclusive, as CivilTimeInterval requires.

        Mind the `pageSize`: here it counts **aggregation windows**, not data
        points, and the API enforces `windowSizeDays * pageSize <= maxDurationDays`
        (90 days, 14 for total-calories). With the default of 1000 the request
        is rejected with `INVALID_ROLLUP_QUERY_DURATION` — so the pageSize is
        exactly the width of the requested window.
        """
        def civil(day: str) -> dict:
            y, m, dd = (int(x) for x in day.split("-"))
            return {"date": {"year": y, "month": m, "day": dd}}

        span = (date.fromisoformat(stop) - date.fromisoformat(start)).days
        out, token = [], None
        while True:
            body = {"range": {"start": civil(start), "end": civil(stop)},
                    "windowSizeDays": 1, "pageSize": max(1, span)}
            if token:
                body["pageToken"] = token
            d = net.post(f"{API}/users/me/dataTypes/{data_type}/dataPoints:dailyRollUp",
                         json_body=body,
                         headers={"Authorization": f"Bearer {self._token()}"})
            out.extend(d.get("rollupDataPoints") or [])
            token = d.get("nextPageToken")
            if not token:
                return out

    # -- sync ------------------------------------------------------------
    def sync(self, days: int = 30, hr_days: int | None = None) -> tuple[int, list[str]]:
        """`hr_days` limits only the minute-by-minute HR.

        It is the only type whose cost grows per minute rather than per day: 200
        days are ~290,000 samples to page through, half an hour of network. Tying
        it to `days` made both backfill and the frequent cycle impossible. They
        are two different cadences because they are two different costs.
        """
        total, problems = 0, []
        today = datetime.now().date()
        hr_days = min(days, 14) if hr_days is None else hr_days

        def attempt(label, fn, *args):
            """A failing type does not take the others down with it.

            A socket timeout used to slip past `except HttpError` and abort the
            whole sync: sleep, HRV and steps lost because intraday HR was slow.
            """
            nonlocal total
            try:
                total += fn(*args)
            except HttpError as e:
                problems.append(self._explain(label, e))
            except GoogleHealthError as e:
                problems.append(f"{label}: {e}")
            except (TimeoutError, OSError) as e:
                problems.append(f"{label}: network slow or interrupted ({type(e).__name__})")

        for spec in DAILY_TYPES:
            attempt(spec.path, self._sync_daily, spec, today, days)
        for data_type, extract, max_days in ROLLUP_TYPES:
            attempt(data_type, self._sync_rollup, data_type, extract, max_days, today, days)
        attempt("sleep", self._sync_sleep, today, days)
        attempt("exercise", self._sync_exercise, today, days)
        if hr_days > 0:
            attempt("heart-rate", self._sync_intraday_hr, today, hr_days)

        self.store.set_state(SOURCE, "last_sync",
                             datetime.now().isoformat(timespec="seconds"))
        return total, problems

    @staticmethod
    def _explain(what: str, e: HttpError) -> str:
        if e.status == 403:
            return (f"{what}: 403 — missing scope, or the Google Health API is not "
                    f"enabled on the Cloud project")
        if e.status == 429:
            return f"{what}: 429 — quota exhausted, retry later"
        if e.status == 404:
            return f"{what}: 404 — data type not available for this account"
        return f"{what}: HTTP {e.status}"

    def _windows(self, today, days: int, size: int):
        end = today + timedelta(days=1)          # `end` exclusive: includes today
        remaining = days
        while remaining > 0:
            span = min(size, remaining)
            start = end - timedelta(days=span)
            yield start.isoformat(), end.isoformat()
            end = start
            remaining -= span

    def _sync_daily(self, spec: Daily, today, days: int) -> int:
        n = 0
        for start, stop in self._windows(today, days, spec.max_days):
            flt = f'{spec.filter_key}.date >= "{start}" AND {spec.filter_key}.date < "{stop}"'
            samples = []
            for dp in self._list(spec.path, flt):
                payload = dp.get(spec.field) or {}
                day = _date_of(payload.get("date"))
                if not day:
                    continue
                for metric, value in spec.extract(payload).items():
                    if value is not None:
                        samples.append(Sample(SOURCE, metric, _ts_at(day, 7), day, value))
            n += self.store.put(samples)
        return n

    def _sync_rollup(self, data_type: str, extract, max_days: int, today, days: int) -> int:
        n = 0
        for start, stop in self._windows(today, days, max_days):
            samples = []
            for r in self._rollup(data_type, start, stop):
                day = _date_of((r.get("civilStartTime") or {}).get("date"))
                if not day:
                    continue
                for metric, value in extract(r).items():
                    if value is not None:
                        samples.append(Sample(SOURCE, metric, _ts_at(day, 23), day, value))
            n += self.store.put(samples)
        return n

    def _sync_sleep(self, today, days: int) -> int:
        n = 0
        for start, stop in self._windows(today, days, 90):
            flt = (f'sleep.interval.civil_end_time >= "{start}" AND '
                   f'sleep.interval.civil_end_time < "{stop}"')
            by_day: dict[str, list[dict]] = {}
            for dp in self._list("sleep", flt, page_size=25):   # hard API limit
                s = dp.get("sleep") or {}
                day = _civil_day(s.get("interval") or {})
                if not day:
                    continue
                self._put_sleep_event(s, day)
                # naps remain events but do not enter the day's metrics: adding
                # them would change the question the number answers
                if (s.get("metadata") or {}).get("nap"):
                    continue
                by_day.setdefault(day, []).append(s)

            samples = []
            for day, candidates in by_day.items():
                samples.extend(self._sleep_samples(_main_session(candidates), day))
            n += self.store.put(samples)
        return n

    def _sync_exercise(self, today, days: int) -> int:
        """Native workout sessions. They partly overlap with COROS, but sessions
        recorded without the watch also come through here."""
        n = 0
        for start, stop in self._windows(today, days, 90):
            flt = (f'exercise.interval.civil_start_time >= "{start}" AND '
                   f'exercise.interval.civil_start_time < "{stop}"')
            samples = []
            for dp in self._list("exercise", flt, page_size=25):
                e = dp.get("exercise") or {}
                iv = e.get("interval") or {}
                st, en = _instant(iv.get("startTime")), _instant(iv.get("endTime"))
                if not (st and en):
                    continue
                local = _local_time(iv.get("startTime"), iv.get("startUtcOffset"))
                day = local.strftime("%Y-%m-%d") if local else None
                if not day:
                    continue
                kind = e.get("exerciseType") or e.get("type") or "workout"
                self.store.put_event(SOURCE, "workout",
                                     (e.get("metadata") or {}).get("externalId")
                                     or f"{day}:{iv.get('startTime')}",
                                     int(st.timestamp()), int(en.timestamp()), str(kind),
                                     meta={"source": "google_health"})
                minutes = (en - st).total_seconds() / 60
                if minutes > 0:
                    samples.append(Sample(SOURCE, "training.duration_min",
                                          int(st.timestamp()), day, round(minutes, 1)))
            n += self.store.put(samples)
        return n

    def _sync_intraday_hr(self, today, days: int) -> int:
        """Minute-by-minute HR → three numbers per day.

        The value lies in waking hours: nightly resting HR says how you recover,
        daytime HR says how activated you are *while working*. Samples are
        downloaded but only the aggregates are kept: storing 130,000 per-minute
        rows to answer daily questions would be weight without information.

        14-day window: API limit for this type.
        """
        n = 0
        for start, stop in self._windows(today, days, 14):
            flt = (f'heart_rate.sample_time.physical_time >= "{start}T00:00:00Z" AND '
                   f'heart_rate.sample_time.physical_time < "{stop}T00:00:00Z"')
            per_day: dict[str, list[float]] = {}
            for dp in self._list("heart-rate", flt, page_size=10000):
                hr = dp.get("heartRate") or {}
                bpm = _num(hr.get("beatsPerMinute"))
                civil = ((hr.get("sampleTime") or {}).get("civilTime") or {})
                day = _date_of(civil.get("date"))
                hour = (civil.get("time") or {}).get("hours")
                if bpm is None or not day or hour is None:
                    continue
                if 9 <= hour < 21:             # waking hours: sleep and evening stay out
                    per_day.setdefault(day, []).append(bpm)
            samples = []
            for day, values in per_day.items():
                if len(values) < 60:           # under an hour of coverage: not summarizable
                    continue
                values.sort()
                p10 = values[int(0.10 * (len(values) - 1))]
                p90 = values[int(0.90 * (len(values) - 1))]
                ts = _ts_at(day, 15)
                samples += [
                    Sample(SOURCE, "hr.daytime_mean", ts, day,
                           round(sum(values) / len(values), 1),
                           meta={"samples": len(values)}),
                    Sample(SOURCE, "hr.daytime_p90", ts, day, p90),
                    Sample(SOURCE, "hr.daytime_spread", ts, day, p90 - p10),
                ]
            n += self.store.put(samples)
        return n

    def _put_sleep_event(self, s: dict, day: str) -> None:
        interval = s.get("interval") or {}
        meta = s.get("metadata") or {}
        st, en = _instant(interval.get("startTime")), _instant(interval.get("endTime"))
        if not (st and en):
            return
        self.store.put_event(
            SOURCE, "sleep", meta.get("externalId") or f"{day}:{interval.get('startTime')}",
            int(st.timestamp()), int(en.timestamp()),
            "nap" if meta.get("nap") else "main sleep",
            meta={"stagesStatus": meta.get("stagesStatus"),
                  "mainSleep": meta.get("mainSleep")})

    def _sleep_samples(self, s: dict, day: str) -> list[Sample]:
        interval = s.get("interval") or {}
        summary = s.get("summary") or {}
        asleep = _num(summary.get("minutesAsleep"))
        in_bed = _num(summary.get("minutesInSleepPeriod"))
        ts = _ts_at(day, 7)
        out = [
            Sample(SOURCE, "sleep.duration_min", ts, day, asleep),
            Sample(SOURCE, "sleep.awake_min", ts, day, _num(summary.get("minutesAwake"))),
        ]
        if asleep and in_bed:
            # the API exposes no efficiency: this one is derived, and says so
            out.append(Sample(SOURCE, "sleep.efficiency", ts, day,
                              round(100 * asleep / in_bed, 1), meta={"derived": True}))
        for stage in (summary.get("stagesSummary") or []):
            metric = STAGE_TO_METRIC.get(stage.get("type"))
            if metric:
                out.append(Sample(SOURCE, metric, ts, day, _num(stage.get("minutes"))))
        # local time must be rebuilt from the offsets declared in the session:
        # `astimezone()` would use the machine's time zone, which is the current
        # one, not the one you slept in (relevant when travelling across zones)
        for field, offset_field, metric in (("startTime", "startUtcOffset", "sleep.start_hour"),
                                            ("endTime", "endUtcOffset", "sleep.end_hour")):
            local = _local_time(interval.get(field), interval.get(offset_field))
            if local:
                out.append(Sample(SOURCE, metric, ts, day, local.hour + local.minute / 60))
        return [x for x in out if x.value is not None]


def sync(store: Store, days: int = 30, hr_days: int | None = None) -> str:
    g = GoogleHealth(store)
    n, problems = g.sync(days=days, hr_days=hr_days)
    msg = f"google health: {n} samples"
    if problems:
        msg += " (" + "; ".join(problems[:4]) + ")"
    return msg
