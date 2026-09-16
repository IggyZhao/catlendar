"""Builds the self-contained dashboard HTML from the local database."""
import datetime as dt
import json
import os

from . import db, goodnews, pipeline, report
from .paths import ASSETS_DIR, REPORT_DIR

TEMPLATE = os.path.join(ASSETS_DIR, "dashboard.html")
OUTPUT = os.path.join(REPORT_DIR, "dashboard.html")


def _clock(ts):
    d = dt.datetime.fromtimestamp(ts)
    return d.strftime("%-I:%M") + d.strftime("%p").lower()


def _range_labels(today):
    week_start = today - dt.timedelta(days=today.weekday())
    week_end = week_start + dt.timedelta(days=6)
    return {
        "day": today.strftime("%A, %B %-d"),
        "week": "{} to {}".format(week_start.strftime("%b %-d"), week_end.strftime("%b %-d, %Y")),
        "month": today.strftime("%B %Y"),
    }


def build_payload(today=None, scope="day"):
    today = today or report.today()
    payload = {
        scope: report.summarize(*report.range_for(scope, today), scope=scope)
        for scope in ("day", "week", "month")
    }
    last = db.get_meta("last_calendar_sync")
    now = dt.datetime.now()
    payload["meta"] = {
        "generated": now.strftime("%b %-d, %-I:%M") + now.strftime("%p").lower(),
        "range_label": _range_labels(today),
        "last_sync": _clock(int(last)) if last else None,
        "calendar_source": db.get_meta("calendar_source", "none"),
    }
    payload["status"] = report.current_status()
    payload["pipeline"] = pipeline.summary(report.today())   # deadlines are always relative to now
    payload["goodnews"] = goodnews.load()
    payload["meta"]["now"] = int(dt.datetime.now().timestamp())
    payload["meta"]["initial_scope"] = scope
    payload["meta"]["date"] = today.isoformat()
    payload["meta"]["is_today"] = (today == report.today())
    payload["meta"]["earliest"] = _earliest_day()
    payload["meta"]["latest"] = _latest_day()
    payload["meta"]["is_future"] = today > report.today()
    return payload


def _latest_day():
    """The last day worth turning to: the furthest meeting already on the
    calendar, so you can look ahead but not wander into empty weeks."""
    with db.cursor() as conn:
        row = conn.execute("SELECT MAX(start_ts) AS t FROM events").fetchone()
    latest = report.today()
    if row and row["t"]:
        latest = max(latest, report.logical_date(row["t"]))
    return latest.isoformat()


def _earliest_day():
    """The first day with anything recorded. History reaches back further than
    the tracker does, because Claude transcripts and file times predate it."""
    stamps = []
    with db.cursor() as conn:
        for table in ("samples", "signals", "events"):
            row = conn.execute("SELECT MIN(ts) AS t FROM {}".format(
                table if table != "events" else "events")).fetchone() \
                if table != "events" else \
                conn.execute("SELECT MIN(start_ts) AS t FROM events").fetchone()
            if row and row["t"]:
                stamps.append(row["t"])
    if not stamps:
        return report.today().isoformat()
    return report.logical_date(min(stamps)).isoformat()


def render(path=OUTPUT, today=None, scope="day"):
    with open(TEMPLATE, "r", encoding="utf-8") as fh:
        html = fh.read()
    data = json.dumps(build_payload(today, scope), ensure_ascii=False, separators=(",", ":"))
    html = html.replace("/*__DATA__*/{}", data, 1)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(html)
    os.replace(tmp, path)
    return path


if __name__ == "__main__":
    print(render())
