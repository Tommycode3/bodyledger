"""COROS adapter (APEX 2 Pro).

The `teamapi.coros.com` API is not public, but it is the one the app uses: login
with an md5 password, then `accessToken` in a header. It exposes **activities and
profile**, not continuous daily data (sleep and HRV stay inside the COROS app):
so here we take the *training and load* side, while recovery comes from Google
Health (or the legacy Fitbit adapter).

The token is reused as long as it holds; on 401 we log in again, once.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from .. import net, secrets
from ..store import Sample, Store, day_of

SOURCE = "coros"
BASE = "https://teamapi.coros.com"

SPORT_TYPES = {
    8: "run", 9: "trail", 100: "bike", 10: "pool swim", 11: "open water",
    13: "walk", 15: "strength", 18: "cardio", 19: "gpsCardio", 22: "hike",
}


class CorosError(Exception):
    pass


class Coros:
    def __init__(self, store: Store):
        self.store = store
        self.token = store.get_state(SOURCE, "token")

    # -- authentication ----------------------------------------------------
    def login(self) -> str:
        email = secrets.get_secret("coros.email")
        password = secrets.get_secret("coros.password")
        if not email or not password:
            raise CorosError("COROS credentials missing from the Keychain — `soma auth coros`")
        r = net.post(f"{BASE}/account/login", json_body={
            "account": email, "accountType": 2,
            "pwd": hashlib.md5(password.encode()).hexdigest(),
        })
        if r.get("result") != "0000":
            raise CorosError(f"login rejected: {r.get('message')}")
        data = r["data"]
        self.token = data["accessToken"]
        self.store.set_state(SOURCE, "token", self.token)
        self.store.set_state(SOURCE, "user_id", data.get("userId", ""))
        return self.token

    def _get(self, path: str, params: dict, _retry: bool = True) -> dict:
        if not self.token:
            self.login()
        r = net.get(f"{BASE}{path}", params=params, headers={"accessToken": self.token})
        # COROS does not use the HTTP status: the session error is in the body.
        if r.get("result") not in ("0000", None) and _retry:
            self.login()
            return self._get(path, params, _retry=False)
        if r.get("result") != "0000":
            raise CorosError(f"{path}: {r.get('message')}")
        return r.get("data") or {}

    # -- profile -----------------------------------------------------------
    def sync_profile(self) -> int:
        """Resting HR and weight from the profile: one snapshot per day, not a
        continuous series, but it is the only recovery channel COROS exposes here."""
        d = self._get("/account/query", {})
        now = int(datetime.now().timestamp())
        today = day_of(now)
        out = []
        if d.get("rhr"):
            out.append(Sample(SOURCE, "hr.resting", now, today, d["rhr"],
                              meta={"maxHr": d.get("maxHr")}))
        if d.get("weight"):
            # the COROS profile stores weight in grams
            w = d["weight"] / 1000 if d["weight"] > 300 else d["weight"]
            out.append(Sample(SOURCE, "body.weight", now, today, w))
        return self.store.put(out)

    # -- activities --------------------------------------------------------
    def sync_activities(self, days: int = 120) -> tuple[int, int]:
        """Download activities from the last `days` days. Idempotent: re-reads
        do not duplicate (key = labelId)."""
        start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        end = datetime.now().strftime("%Y%m%d")
        page, total_pages, acts = 1, 1, []
        while page <= total_pages:
            d = self._get("/activity/query", {
                "size": 50, "pageNumber": page, "modeList": "",
                "startDay": start, "endDay": end,
            })
            acts.extend(d.get("dataList") or [])
            total_pages = d.get("totalPage") or 1
            page += 1
        samples: list[Sample] = []
        for a in acts:
            ts = int(a.get("startTime") or 0)
            if not ts:
                continue
            day = day_of(ts)
            name = a.get("name") or SPORT_TYPES.get(a.get("sportType"), "activity")
            self.store.put_event(
                SOURCE, "workout", str(a.get("labelId")), ts, a.get("endTime"), name,
                meta={"sport": SPORT_TYPES.get(a.get("sportType"), a.get("sportType")),
                      "distance_m": a.get("distance"), "ascent": a.get("ascent"),
                      "device": a.get("device")})
            if a.get("trainingLoad"):
                samples.append(Sample(SOURCE, "training.load", ts, day, a["trainingLoad"],
                                      meta={"activity": name}))
            if a.get("totalTime"):
                samples.append(Sample(SOURCE, "training.duration_min", ts, day,
                                      a["totalTime"] / 60))
            if a.get("avgHr"):
                samples.append(Sample(SOURCE, "training.avg_hr", ts, day, a["avgHr"]))
            if a.get("distance"):
                samples.append(Sample(SOURCE, "activity.distance_m", ts, day, a["distance"]))
            if a.get("calorie"):
                # COROS counts calories in thousandths of a kcal
                samples.append(Sample(SOURCE, "activity.calories", ts, day,
                                      a["calorie"] / 1000))
        n = self.store.put(samples)
        self.store.set_state(SOURCE, "last_sync", datetime.now().isoformat(timespec="seconds"))
        return len(acts), n


def sync(store: Store, days: int = 120) -> str:
    c = Coros(store)
    n_act, n_sig = c.sync_activities(days=days)
    n_prof = c.sync_profile()
    return f"coros: {n_act} activities, {n_sig + n_prof} samples"
