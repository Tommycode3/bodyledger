"""Local dashboard: stdlib `http.server`, 127.0.0.1 only, zero external
assets: local-first applies to the CSS too.

Read-only: the dashboard has no endpoint that writes. Looking at your own data
must not be able to change your own data.
"""
from __future__ import annotations

import json
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import fuse, insight
from .store import METRICS, Store

PORT = 4748
WINDOW_DAYS = 90

# Panels, in the order in which they make sense to read: first how you slept,
# then how the heart is doing, then what you did.
PANELS = [
    ("Sleep", [
        ("sleep.duration_min", "duration", "min", True),
        ("sleep.efficiency", "efficiency", "%", True),
        ("sleep.deep_min", "deep", "min", True),
        ("sleep.rem_min", "REM", "min", True),
        ("sleep.start_hour", "bedtime", "h", None),
        ("sleep.end_hour", "wake-up", "h", None),
    ]),
    ("Recovery", [
        ("hr.hrv_rmssd", "HRV", "ms", True),
        ("hr.resting", "resting HR", "bpm", False),
        ("hr.daytime_mean", "daytime HR", "bpm", False),
        ("body.breathing_rate", "breathing", "rpm", None),
        ("body.spo2", "SpO₂", "%", True),
        ("body.temp_delta", "temperature", "°C", None),
    ]),
    ("Activity", [
        ("activity.steps", "steps", "", True),
        ("activity.active_min", "active minutes", "min", True),
        ("training.load", "load", "au", None),
        ("activity.distance_m", "distance", "m", True),
    ]),
]



def _series_over_days(store: Store, metric: str, days: int) -> list:
    """Values aligned to a continuous day axis, with `null` in the gaps.

    Gaps must stay gaps: joining two points two weeks apart with a straight
    line would tell a continuity that never happened.
    """
    rows = {r["day"]: r["value"] for r in store.series(metric, days=days)}
    today = datetime.now().date()
    out = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        out.append({"day": d, "v": rows.get(d)})
    return out


def _stats(values: list) -> dict:
    present = [v for v in values if v is not None]
    if not present:
        return {"last": None, "median": None, "n": 0}
    ordered = sorted(present)
    return {
        "last": present[-1],
        "median": ordered[len(ordered) // 2],
        "n": len(present),
        "min": ordered[0],
        "max": ordered[-1],
    }


def build_data(store: Store) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    state = fuse.state(store, today)

    panels = []
    for title, metrics in PANELS:
        cards = []
        for metric, label, unit, higher_better in metrics:
            series = _series_over_days(store, metric, WINDOW_DAYS)
            values = [p["v"] for p in series]
            st = _stats(values)
            if st["n"] == 0:
                continue          # no card for metrics that never arrived
            cards.append({
                "metric": metric, "label": label, "unit": unit,
                "higher_better": higher_better,
                "series": series, **st,
                "desc": METRICS.get(metric, ("", ""))[1],
            })
        if cards:
            panels.append({"title": title, "cards": cards})

    ins = insight.run(store)
    hypotheses = [{
        "q": r.hypothesis.question, "n": r.n,
        "rho": round(r.rho, 2) if r.rho is not None else None,
        "p": round(r.p, 3) if r.p is not None else None,
        "survives": r.survives, "testable": r.testable,
        "lag": r.hypothesis.lag,
    } for r in ins["results"]]
    hypotheses.sort(key=lambda x: (not x["survives"], not x["testable"],
                                  -(abs(x["rho"]) if x["rho"] is not None else 0)))

    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "today": state,
        "brief": fuse.brief(store, today),
        "coverage": [dict(c, last_sync=store.get_state(c["source"], "last_sync"))
                     for c in store.coverage()],
        "panels": panels,
        "events": store.events(days=14)[:12],
        "insight": {"tested": ins["tested"], "total": ins["total"],
                    "min_pairs": ins["min_pairs"], "q": ins["q"],
                    "hypotheses": hypotheses},
    }


def make_handler():
    html_path = Path(__file__).parent / "dashboard.html"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            raw = body if isinstance(body, bytes) else json.dumps(
                body, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                try:
                    self._send(200, html_path.read_bytes(), "text/html; charset=utf-8")
                except OSError:
                    self._send(500, b"dashboard.html missing", "text/plain; charset=utf-8")
            elif self.path.startswith("/api/data"):
                store = Store()          # one connection per request, per thread
                try:
                    self._send(200, build_data(store))
                finally:
                    store.close()
            else:
                self._send(404, {"error": "not found"})

    return Handler


def serve(port: int = PORT, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler())
    url = f"http://127.0.0.1:{port}"
    print(f"soma dashboard at {url}  (Ctrl-C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nclosed.")
    finally:
        server.server_close()
