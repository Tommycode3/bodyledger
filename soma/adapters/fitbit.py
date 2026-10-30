"""Fitbit adapter (Web API, Google account). Legacy: superseded by Google Health.

It is the *recovery* channel: sleep by stage, nightly HRV, resting HR,
breathing rate, SpO2, skin temperature, steps. Everything COROS does not expose.

OAuth2 with PKCE and a loopback redirect: the secret never leaves the machine,
tokens live in the Keychain. Refresh is automatic and transparent — once
authorized, there is nothing more to touch.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import os
import secrets as pysecrets
import threading
import urllib.parse
import webbrowser
from datetime import datetime, timedelta

from .. import net, secrets
from ..net import HttpError
from ..store import Sample, Store

SOURCE = "fitbit"
API = "https://api.fitbit.com"
AUTHORIZE = "https://www.fitbit.com/oauth2/authorize"
TOKEN_URL = f"{API}/oauth2/token"
REDIRECT_PORT = 8731
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
SCOPES = ("activity heartrate sleep profile weight "
          "respiratory_rate oxygen_saturation temperature cardio_fitness")


class FitbitError(Exception):
    pass


# ---------------------------------------------------------------------------
# Authorization (one-off)
# ---------------------------------------------------------------------------

def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(64)).decode().rstrip("=")[:128]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        q = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.result = {k: v[0] for k, v in q.items()}
        ok = "code" in _CallbackHandler.result
        body = ("<h2>soma: account connected.</h2><p>You can close this tab.</p>"
                if ok else
                f"<h2>soma: authorization failed</h2><pre>{_CallbackHandler.result}</pre>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):  # silence
        pass


def authorize(client_id: str, client_secret: str = "", timeout_s: int = 300) -> None:
    """Full flow: opens the browser, receives the code on loopback, stores the
    tokens in the Keychain."""
    verifier, challenge = _pkce()
    state = pysecrets.token_urlsafe(16)
    url = AUTHORIZE + "?" + urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "scope": SCOPES,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "redirect_uri": REDIRECT_URI, "state": state, "expires_in": "31536000",
    })

    _CallbackHandler.result = {}
    server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), _CallbackHandler)
    server.timeout = 1
    thread = threading.Thread(target=_serve_until, args=(server, timeout_s), daemon=True)
    thread.start()

    print(f"Opening the browser to authorize Fitbit.\nIf it does not open: {url}\n")
    webbrowser.open(url)
    thread.join(timeout_s + 2)
    server.server_close()

    res = _CallbackHandler.result
    if not res.get("code"):
        raise FitbitError(f"no code received ({res or 'timeout'})")
    if res.get("state") != state:
        raise FitbitError("state mismatch — authorization discarded")

    headers = {}
    form = {"client_id": client_id, "grant_type": "authorization_code",
            "code": res["code"], "code_verifier": verifier, "redirect_uri": REDIRECT_URI}
    if client_secret:
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {basic}"
    tok = net.post(TOKEN_URL, form=form, headers=headers)
    if "access_token" not in tok:
        raise FitbitError(f"no token issued: {tok}")
    _save_tokens(tok, client_id, client_secret)


def _serve_until(server, timeout_s: int) -> None:
    deadline = datetime.now() + timedelta(seconds=timeout_s)
    while datetime.now() < deadline and not _CallbackHandler.result:
        server.handle_request()


def _save_tokens(tok: dict, client_id: str, client_secret: str) -> None:
    secrets.set_json("fitbit.tokens", {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", ""),
        "expires_at": int(datetime.now().timestamp()) + int(tok.get("expires_in", 28800)) - 120,
        "client_id": client_id, "client_secret": client_secret,
        "user_id": tok.get("user_id", ""),
    })


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

def _day_ts(date_str: str, hour: int) -> int:
    return int(datetime.strptime(f"{date_str} {hour:02d}:00", "%Y-%m-%d %H:%M").timestamp())


def _f(v) -> float | None:
    """Fitbit sometimes returns ranges ('40-44') or empty strings."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v)
    if "-" in s:
        parts = [p for p in s.split("-") if p.strip()]
        try:
            return sum(float(p) for p in parts) / len(parts)
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


class Fitbit:
    def __init__(self, store: Store):
        self.store = store
        self.tokens = secrets.get_json("fitbit.tokens")
        if not self.tokens:
            raise FitbitError("Fitbit not authorized — run `soma auth fitbit`")

    def _access_token(self) -> str:
        if datetime.now().timestamp() >= self.tokens.get("expires_at", 0):
            self._refresh()
        return self.tokens["access_token"]

    def _refresh(self) -> None:
        headers = {}
        cid, csec = self.tokens.get("client_id", ""), self.tokens.get("client_secret", "")
        if csec:
            basic = base64.b64encode(f"{cid}:{csec}".encode()).decode()
            headers["Authorization"] = f"Basic {basic}"
        try:
            tok = net.post(TOKEN_URL, form={
                "grant_type": "refresh_token",
                "refresh_token": self.tokens["refresh_token"],
                "client_id": cid,
            }, headers=headers)
        except HttpError as e:
            raise FitbitError(f"refresh failed ({e.status}) — re-authorize with "
                              f"`soma auth fitbit`") from None
        if "access_token" not in tok:
            raise FitbitError(f"refresh returned no token: {tok}")
        _save_tokens(tok, cid, csec)
        self.tokens = secrets.get_json("fitbit.tokens")

    def _get(self, path: str) -> dict:
        # The locale selects the unit system of the response (metric for it_IT).
        return net.get(f"{API}{path}",
                       headers={"Authorization": f"Bearer {self._access_token()}",
                                "Accept-Language": "it_IT"})

    # -- sync ------------------------------------------------------------
    def sync(self, days: int = 30) -> tuple[int, list[str]]:
        """Sync in 30-day windows (API limit). Returns samples written and the
        list of non-fatal errors: an endpoint not available on your device must
        not make everything else fail."""
        total, problems = 0, []
        today = datetime.now().date()
        chunks = []
        remaining = days
        end = today
        while remaining > 0:
            span = min(30, remaining)
            start = end - timedelta(days=span - 1)
            chunks.append((start.isoformat(), end.isoformat()))
            end = start - timedelta(days=1)
            remaining -= span

        for start, stop in chunks:
            for label, fn in (
                ("sleep", self._sleep), ("hrv", self._hrv), ("breathing", self._br),
                ("spo2", self._spo2), ("temperature", self._temp),
                ("resting hr", self._resting_hr), ("activity", self._activity),
                ("zone minutes", self._azm), ("vo2max", self._cardio),
            ):
                try:
                    total += fn(start, stop)
                except HttpError as e:
                    msg = f"{label} {start}: HTTP {e.status}"
                    if e.status == 429:
                        problems.append(f"{msg} (Fitbit rate limit: 150 requests/hour)")
                        return total, problems
                    problems.append(msg)
                except FitbitError as e:
                    problems.append(f"{label}: {e}")
        self.store.set_state(SOURCE, "last_sync",
                             datetime.now().isoformat(timespec="seconds"))
        return total, problems

    def _sleep(self, start: str, stop: str) -> int:
        d = self._get(f"/1.2/user/-/sleep/date/{start}/{stop}.json")
        out: list[Sample] = []
        for s in d.get("sleep", []):
            date = s.get("dateOfSleep")
            if not date:
                continue
            ts = _day_ts(date, 7)
            summary = (s.get("levels") or {}).get("summary") or {}
            try:
                st = datetime.strptime(s["startTime"][:19], "%Y-%m-%dT%H:%M:%S")
                en = datetime.strptime(s["endTime"][:19], "%Y-%m-%dT%H:%M:%S")
            except (KeyError, ValueError):
                st = en = None
            if s.get("isMainSleep", True):
                out.append(Sample(SOURCE, "sleep.duration_min", ts, date,
                                  s.get("minutesAsleep")))
                out.append(Sample(SOURCE, "sleep.awake_min", ts, date, s.get("minutesAwake")))
                out.append(Sample(SOURCE, "sleep.efficiency", ts, date, s.get("efficiency")))
                for stage, metric in (("deep", "sleep.deep_min"), ("rem", "sleep.rem_min"),
                                      ("light", "sleep.light_min")):
                    v = (summary.get(stage) or {}).get("minutes")
                    if v is not None:
                        out.append(Sample(SOURCE, metric, ts, date, v))
                if st and en:
                    out.append(Sample(SOURCE, "sleep.start_hour", ts, date,
                                      st.hour + st.minute / 60))
                    out.append(Sample(SOURCE, "sleep.end_hour", ts, date,
                                      en.hour + en.minute / 60))
            if st and en:
                self.store.put_event(SOURCE, "sleep", str(s.get("logId")),
                                     int(st.timestamp()), int(en.timestamp()),
                                     "main sleep" if s.get("isMainSleep") else "nap",
                                     meta={"efficiency": s.get("efficiency"),
                                           "minutesAsleep": s.get("minutesAsleep")})
        return self.store.put([x for x in out if x.value is not None])

    def _hrv(self, start: str, stop: str) -> int:
        d = self._get(f"/1/user/-/hrv/date/{start}/{stop}.json")
        out = []
        for e in d.get("hrv", []):
            date, v = e.get("dateTime"), e.get("value") or {}
            if not date:
                continue
            ts = _day_ts(date, 7)
            out.append(Sample(SOURCE, "hr.hrv_rmssd", ts, date, _f(v.get("dailyRmssd"))))
            out.append(Sample(SOURCE, "hr.hrv_deep_rmssd", ts, date, _f(v.get("deepRmssd"))))
        return self.store.put([x for x in out if x.value is not None])

    def _br(self, start: str, stop: str) -> int:
        d = self._get(f"/1/user/-/br/date/{start}/{stop}.json")
        out = [Sample(SOURCE, "body.breathing_rate", _day_ts(e["dateTime"], 7),
                      e["dateTime"], _f((e.get("value") or {}).get("breathingRate")))
               for e in d.get("br", []) if e.get("dateTime")]
        return self.store.put([x for x in out if x.value is not None])

    def _spo2(self, start: str, stop: str) -> int:
        d = self._get(f"/1/user/-/spo2/date/{start}/{stop}.json")
        rows = d if isinstance(d, list) else d.get("spo2", [])
        out = [Sample(SOURCE, "body.spo2", _day_ts(e["dateTime"], 7), e["dateTime"],
                      _f((e.get("value") or {}).get("avg")))
               for e in rows if isinstance(e, dict) and e.get("dateTime")]
        return self.store.put([x for x in out if x.value is not None])

    def _temp(self, start: str, stop: str) -> int:
        d = self._get(f"/1/user/-/temp/skin/date/{start}/{stop}.json")
        out = [Sample(SOURCE, "body.temp_delta", _day_ts(e["dateTime"], 7), e["dateTime"],
                      _f((e.get("value") or {}).get("nightlyRelative")))
               for e in d.get("tempSkin", []) if e.get("dateTime")]
        return self.store.put([x for x in out if x.value is not None])

    def _resting_hr(self, start: str, stop: str) -> int:
        d = self._get(f"/1/user/-/activities/heart/date/{start}/{stop}.json")
        out = []
        for e in d.get("activities-heart", []):
            date, v = e.get("dateTime"), e.get("value") or {}
            if not date:
                continue
            out.append(Sample(SOURCE, "hr.resting", _day_ts(date, 7), date,
                              _f(v.get("restingHeartRate"))))
        return self.store.put([x for x in out if x.value is not None])

    def _activity(self, start: str, stop: str) -> int:
        out = []
        for res, metric, scale in (("steps", "activity.steps", 1),
                                   ("calories", "activity.calories", 1),
                                   ("distance", "activity.distance_m", 1000),
                                   ("floors", "activity.floors", 1)):
            try:
                d = self._get(f"/1/user/-/activities/{res}/date/{start}/{stop}.json")
            except HttpError as e:
                if e.status in (400, 404):   # resource not supported by the device
                    continue
                raise
            for e in d.get(f"activities-{res}", []):
                v = _f(e.get("value"))
                if v is None or not e.get("dateTime"):
                    continue
                out.append(Sample(SOURCE, metric, _day_ts(e["dateTime"], 23),
                                  e["dateTime"], v * scale))
        return self.store.put(out)

    def _azm(self, start: str, stop: str) -> int:
        d = self._get(f"/1/user/-/activities/active-zone-minutes/date/{start}/{stop}.json")
        out = [Sample(SOURCE, "activity.active_min", _day_ts(e["dateTime"], 23),
                      e["dateTime"], _f((e.get("value") or {}).get("activeZoneMinutes")))
               for e in d.get("activities-active-zone-minutes", []) if e.get("dateTime")]
        return self.store.put([x for x in out if x.value is not None])

    def _cardio(self, start: str, stop: str) -> int:
        d = self._get(f"/1/user/-/cardioscore/date/{start}/{stop}.json")
        out = [Sample(SOURCE, "training.vo2max", _day_ts(e["dateTime"], 23), e["dateTime"],
                      _f((e.get("value") or {}).get("vo2Max")))
               for e in d.get("cardioScore", []) if e.get("dateTime")]
        return self.store.put([x for x in out if x.value is not None])


def sync(store: Store, days: int = 30) -> str:
    f = Fitbit(store)
    n, problems = f.sync(days=days)
    msg = f"fitbit: {n} samples"
    if problems:
        msg += " (" + "; ".join(problems[:4]) + ")"
    return msg
