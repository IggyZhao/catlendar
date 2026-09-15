"""Catlendar command line. Works on macOS, Windows and Linux.

    python -m catlendar track     keep recording until you stop it
    python -m catlendar report    build the dashboard and open it
    python -m catlendar scan      rebuild the project list from your folders
    python -m catlendar status    what it knows so far
    python -m catlendar app       the menu bar app and desktop cat (macOS)
"""
import argparse
import logging
import logging.handlers
import os
import sys
import time
import webbrowser

from . import config, db
from .paths import IS_MAC, LOG_DIR


def setup_logging(verbose=False):
    handler = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, "catlendar.log"), maxBytes=1_000_000, backupCount=2)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler())


def cmd_track(args):
    from . import calsync, sources, tracker
    db.init()
    config.ensure_user_config()
    if not tracker.accessibility_trusted():
        hint = tracker.permission_hint()
        print("Window titles are not available." + (" " + hint if hint else ""))
        print("Catlendar will still record Claude sessions and saved files.\n")
    worker = tracker.Tracker()
    worker.start()
    signals = sources.SignalScanner()
    signals.start()
    calendar = None
    if IS_MAC:
        calendar = calsync.CalendarSync()
        calendar.start()
    print("Recording. Leave this running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        worker.stop()
        signals.stop()
        if calendar:
            calendar.stop()


def cmd_report(args):
    from . import dashboard, sources
    db.init()
    if not args.no_scan:
        try:
            sources.scan_claude()
            sources.scan_files()
        except Exception:
            logging.getLogger("catlendar").exception("a scan failed, reporting anyway")
    path = dashboard.render(scope=args.scope)
    print(path)
    if not args.no_open:
        webbrowser.open("file://" + path.replace(os.sep, "/"))


def cmd_scan(args):
    from . import scan_projects
    count, path = scan_projects.run()
    print("wrote {} projects to {}".format(count, path))
    if not count:
        print("Nothing found. Set project_roots in that file and run this again.")


def cmd_status(args):
    from . import report, tracker
    db.init()
    with db.cursor() as conn:
        titled = conn.execute(
            "SELECT COUNT(*) n FROM samples WHERE title IS NOT NULL").fetchone()["n"]
        samples = conn.execute("SELECT COUNT(*) n FROM samples").fetchone()["n"]
        events = conn.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]
        kinds = {r["kind"]: r["n"] for r in
                 conn.execute("SELECT kind, COUNT(*) n FROM signals GROUP BY kind")}
    today = report.summarize(*report.range_for("day"), scope="day")
    print("window titles :", "on" if tracker.accessibility_trusted() else "not available")
    print("samples       :", samples, "({} with a title)".format(titled))
    print("calendar      :", events, "events")
    print("signals       :", "{} Claude spans, {} file changes".format(
        kinds.get("claude", 0), kinds.get("file", 0)))
    print("today         :", report.fmt_hm(today["total_seconds"]))
    for row in today["projects"][:5]:
        print("   {:>8s}  {}".format(report.fmt_hm(row["seconds"]), row["label"]))


def cmd_app(args):
    if not IS_MAC:
        print("The menu bar app and the desktop cat are macOS only for now.")
        print("On Windows use:  python -m catlendar track   and   python -m catlendar report")
        return 1
    from . import app
    app.main()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="catlendar", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    subs = parser.add_subparsers(dest="command")

    subs.add_parser("track", help="record until stopped").set_defaults(func=cmd_track)

    rep = subs.add_parser("report", help="build the dashboard and open it")
    rep.add_argument("--scope", default="day", choices=["day", "week", "month", "pipeline"])
    rep.add_argument("--no-open", action="store_true", help="just print the path")
    rep.add_argument("--no-scan", action="store_true", help="skip the signal scan first")
    rep.set_defaults(func=cmd_report)

    subs.add_parser("scan", help="rebuild the project list").set_defaults(func=cmd_scan)
    subs.add_parser("status", help="what it knows so far").set_defaults(func=cmd_status)
    subs.add_parser("app", help="menu bar app and desktop cat (macOS)").set_defaults(func=cmd_app)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    setup_logging(args.verbose)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
