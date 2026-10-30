"""soma CLI. `soma` = `python3 -m soma`."""
from __future__ import annotations

import argparse
import getpass
import sys
from datetime import datetime

from . import cloud, fuse, secrets
from .store import METRICS, Store


def cmd_auth(args) -> int:
    if args.service == "coros":
        email = args.email or input("COROS email: ").strip()
        password = args.password or getpass.getpass("COROS password: ")
        secrets.set_secret("coros.email", email)
        secrets.set_secret("coros.password", password)
        from .adapters.coros import Coros, CorosError
        try:
            Coros(Store()).login()
        except CorosError as e:
            print(f"✗ credentials saved but login fails: {e}")
            return 1
        print("✓ COROS connected (credentials in Keychain)")
        return 0

    if args.service == "google":
        from .adapters.google_health import GoogleHealthError, authorize
        client_id = args.client_id or input("OAuth Client ID (Google Cloud): ").strip()
        client_secret = args.client_secret or getpass.getpass(
            "Client Secret (press Enter to skip if the client is a 'Desktop app'): ")
        try:
            authorize(client_id, client_secret.strip())
        except GoogleHealthError as e:
            print(f"✗ {e}")
            return 1
        print("✓ Google Health connected (token in Keychain, automatic refresh)")
        return 0

    if args.service == "fitbit":
        print("⚠ Google is shutting down the Fitbit Web API in September 2026.\n"
              "  The new path is `soma auth google` (Google Health API).\n")
        from .adapters.fitbit import FitbitError, authorize
        client_id = args.client_id or input("Fitbit Client ID: ").strip()
        client_secret = args.client_secret or getpass.getpass(
            "Client Secret (press Enter to skip if the app is public PKCE): ")
        try:
            authorize(client_id, client_secret.strip())
        except FitbitError as e:
            print(f"✗ {e}")
            return 1
        print("✓ Fitbit connected (token in Keychain, automatic refresh)")
        return 0

    if args.service == "supabase":
        url = args.url or input("Supabase project URL (https://xxx.supabase.co): ").strip()
        key = args.key or getpass.getpass("service_role key: ")
        secrets.set_json("supabase", {"url": url.strip(), "key": key.strip()})
        print("✓ Supabase configured — next: `soma cloud schema` and `soma cloud push`")
        return 0
    return 1


def _default_sources() -> list[str]:
    """Fitbit stays in the loop only until Google Health is connected: the two
    sources provide the same data, and syncing both would mean two rows per
    metric every day."""
    body = ["coros", "google"] if secrets.get_secret("google_health.tokens") \
        else ["coros", "fitbit"]
    return body


def cmd_sync(args) -> int:
    store = Store()
    # `--fast` is the frequent cycle: short window, minimal cost. Upstream data
    # lags 2-3 hours anyway (watch→phone→cloud chain), so it makes sense to
    # look often but not far back.
    body_days = 3 if args.fast else (args.days or 30)
    if args.fast and args.hr_days is None:
        args.hr_days = 3
    sources = [args.source] if args.source else _default_sources()
    rc = 0
    for name in sources:
        try:
            if name == "coros":
                from .adapters import coros
                print(coros.sync(store, days=args.days or 120))
            elif name == "google":
                from .adapters import google_health
                print(google_health.sync(store, days=body_days,
                                         hr_days=args.hr_days))
            elif name == "fitbit":
                from .adapters import fitbit
                print(fitbit.sync(store, days=args.days or 30))
        except Exception as e:
            print(f"✗ {name}: {e}")
            rc = 1
    path = fuse.write_today(store)
    print(f"→ {path}")
    if args.cloud:
        try:
            print(cloud.push(store))
        except Exception as e:
            print(f"✗ cloud: {e}")
            rc = 1
    return rc


def cmd_status(args) -> int:
    print(fuse.brief(Store(), args.day))
    return 0


def cmd_metric(args) -> int:
    store = Store()
    if args.metric not in METRICS:
        print(f"Unknown metric. Available:\n  " + "\n  ".join(sorted(METRICS)))
        return 1
    rows = store.series(args.metric, days=args.days)
    if not rows:
        print(f"{args.metric}: no data in the last {args.days} days.")
        return 0
    unit = METRICS[args.metric][0]
    vals = [r["value"] for r in rows]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    for r in rows:
        bar = "█" * int(1 + 28 * (r["value"] - lo) / span)
        print(f"{r['day']}  {r['value']:>8.1f} {unit:<6} {bar}")
    print(f"\n{len(rows)} days · min {lo:.1f} · max {hi:.1f} · "
          f"median {sorted(vals)[len(vals) // 2]:.1f} {unit}")
    return 0


def cmd_metrics(args) -> int:
    store = Store()
    print("Canonical vocabulary:")
    for m, (u, d) in sorted(METRICS.items()):
        print(f"  {m:<24} {u:<12} {d}")
    print("\nActual coverage:")
    cov = store.coverage()
    if not cov:
        print("  (store empty — run `soma sync`)")
    for c in cov:
        print(f"  {c['source']:<14} {c['n']:>7} samples · {c['metrics']} metrics · "
              f"{c['first_day']} → {c['last_day']}")
    return 0


def cmd_events(args) -> int:
    for e in Store().events(kind=args.kind, days=args.days):
        dur = ""
        if e["end_ts"]:
            dur = f"{(e['end_ts'] - e['start_ts']) // 60} min"
        print(f"{e['day']}  {e['kind']:<8} {e['title'][:38]:<38} {dur:>8}  [{e['source']}]")
    return 0


def cmd_cloud(args) -> int:
    if args.action == "schema":
        print(cloud.SCHEMA_SQL)
        return 0
    try:
        print(cloud.push(Store(), full=args.full))
    except Exception as e:
        print(f"✗ {e}")
        return 1
    return 0


def cmd_insight(args) -> int:
    from . import insight
    print(insight.report(Store(), days=args.days, verbose=args.all))
    return 0


def cmd_dashboard(args) -> int:
    from .web import PORT, serve
    serve(port=args.port or PORT, open_browser=not args.no_open)
    return 0


def cmd_mcp(args) -> int:
    from .mcp import serve
    serve()
    return 0


def cmd_doctor(args) -> int:
    """What is connected, what is arriving, what is missing. The first thing to
    run when a number looks wrong."""
    store = Store()
    print("Credentials")
    for label, name in (("COROS", "coros.password"),
                        ("Google Health", "google_health.tokens"),
                        ("Fitbit", "fitbit.tokens"), ("Supabase", "supabase")):
        print(f"  {label:<14} {'✓ in Keychain' if secrets.get_secret(name) else '— not configured'}")

    if secrets.get_secret("fitbit.tokens") and not secrets.get_secret("google_health.tokens"):
        print("\n  ⚠ The Fitbit Web API shuts down in September 2026: migrate with "
              "`soma auth google`.")

    print("\nLast sync")
    stale = []
    for src in ("coros", "google_health", "fitbit"):
        last = store.get_state(src, "last_sync")
        note = ""
        if last:
            try:
                hours = (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 3600
                if hours > 48:
                    note = f"  ⚠ stalled for {int(hours // 24)} days"
                    stale.append(src)
            except ValueError:
                pass
        print(f"  {src:<14} {last or '— never'}{note}")
    print(f"  {'cloud':<14} {store.get_state('cloud', 'last_push') or '— never'}")
    if "google_health" in stale:
        # the typical way this integration dies: token revoked after 7 days
        print("\n  Google Health stalled: try `soma sync --source google` and read "
              "the error.\n  If it says 'invalid_grant', the OAuth consent screen is in "
              "'Testing' status.")

    print("\nCoverage")
    cov = store.coverage()
    if not cov:
        print("  store empty")
    for c in cov:
        print(f"  {c['source']:<14} {c['n']:>7} samples · {c['first_day']} → {c['last_day']}")

    today = datetime.now().strftime("%Y-%m-%d")
    st = fuse.state(store, today)
    print(f"\nToday ({today}): readiness "
          f"{st['readiness'] if st['readiness'] is not None else '—'} · "
          f"evidence {int(st['confidence'] * 100)}%")
    if st["missing"]:
        print("  missing: " + ", ".join(st["missing"]))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="soma", description="The body layer: wearables → a single local store.")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("auth", help="connect a source")
    a.add_argument("service", choices=["coros", "google", "fitbit", "supabase"])
    a.add_argument("--email"); a.add_argument("--password")
    a.add_argument("--client-id"); a.add_argument("--client-secret")
    a.add_argument("--url"); a.add_argument("--key")
    a.set_defaults(func=cmd_auth)

    s = sub.add_parser("sync", help="download data and update the state")
    s.add_argument("--days", type=int)
    s.add_argument("--source", choices=["coros", "google", "fitbit"])
    s.add_argument("--cloud", action="store_true", help="also replicate to Supabase")
    s.add_argument("--fast", action="store_true",
                   help="frequent cycle: only the last 3 days")
    s.add_argument("--hr-days", type=int, dest="hr_days",
                   help="window for minute-by-minute HR (default: min(days,14))")
    s.set_defaults(func=cmd_sync)

    st = sub.add_parser("status", help="body status, in plain language")
    st.add_argument("day", nargs="?")
    st.set_defaults(func=cmd_status)

    m = sub.add_parser("metric", help="series of one metric")
    m.add_argument("metric"); m.add_argument("--days", type=int, default=30)
    m.set_defaults(func=cmd_metric)

    sub.add_parser("metrics", help="vocabulary and coverage").set_defaults(func=cmd_metrics)

    e = sub.add_parser("events", help="workouts and sleep sessions")
    e.add_argument("--kind", choices=["workout", "sleep"]); e.add_argument("--days", type=int, default=30)
    e.set_defaults(func=cmd_events)

    c = sub.add_parser("cloud", help="replicate to Supabase")
    c.add_argument("action", choices=["push", "schema"])
    c.add_argument("--full", action="store_true")
    c.set_defaults(func=cmd_cloud)

    i = sub.add_parser("insight", help="relationships between metrics, with statistical discipline")
    i.add_argument("--days", type=int, default=180)
    i.add_argument("--all", action="store_true", help="also show hypotheses that do not hold")
    i.set_defaults(func=cmd_insight)

    w = sub.add_parser("dashboard", help="open the local dashboard in the browser")
    w.add_argument("--port", type=int)
    w.add_argument("--no-open", action="store_true", help="do not open the browser")
    w.set_defaults(func=cmd_dashboard)

    sub.add_parser("mcp", help="MCP server on stdio (for Claude)").set_defaults(func=cmd_mcp)
    sub.add_parser("doctor", help="diagnostics: what is connected and what is arriving").set_defaults(func=cmd_doctor)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
