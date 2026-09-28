"""Small wins: the things you finished today, kept so you can look at them.

Good news is for the milestones, the funding and the acceptances. This is the
other half: the ordinary done things that a day is actually made of, which
otherwise leave no trace once the day is over.
"""
import datetime as dt
import os
import shutil
import threading

import yaml

from .paths import DATA_DIR

PATH = os.path.join(DATA_DIR, "wins.yaml")
_lock = threading.Lock()
MAX_ITEMS = 2000


def _clean(item):
    text = str((item or {}).get("text") or "").strip()
    if not text:
        return None
    date = str(item.get("date") or "").strip() or dt.date.today().isoformat()
    try:
        dt.date.fromisoformat(date)
    except ValueError:
        date = dt.date.today().isoformat()
    return {"text": text[:300], "date": date,
            "at": str(item.get("at") or "").strip()[:5]}


def load():
    if not os.path.exists(PATH):
        return []
    try:
        with open(PATH, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        return []
    out = [w for w in (_clean(i) for i in (data.get("items") or [])) if w]
    out.sort(key=lambda w: (w["date"], w["at"]), reverse=True)
    return out[:MAX_ITEMS]


def for_day(day):
    """Newest first, so the thing you just finished is at the top."""
    key = day.isoformat() if hasattr(day, "isoformat") else str(day)
    return [w for w in load() if w["date"] == key]


def replace_day(day, items):
    """The card sends back every win for the day it is showing."""
    key = day.isoformat() if hasattr(day, "isoformat") else str(day)
    kept = [w for w in load() if w["date"] != key]
    fresh = []
    for item in items or []:
        w = _clean(dict(item, date=key))
        if w:
            fresh.append(w)
    return _write(kept + fresh)


def _write(rows):
    rows.sort(key=lambda w: (w["date"], w["at"]), reverse=True)
    rows = rows[:MAX_ITEMS]
    with _lock:
        if os.path.exists(PATH):
            shutil.copyfile(PATH, PATH + ".bak")
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("# Catlendar wins. Small things you finished. "
                     "Add anything that felt good.\n")
            yaml.safe_dump({"items": rows}, fh, allow_unicode=True,
                           sort_keys=False, default_flow_style=False)
        os.replace(tmp, PATH)
    return len(rows)


def stats(today):
    """Counts worth showing, and the streak, which is the part that pulls.

    A day you have not filled in yet does not break the streak: it only counts
    back from the last day that has something, so an empty morning never shows
    you a zero you did not earn.
    """
    days = {}
    for w in load():
        days.setdefault(w["date"], 0)
        days[w["date"]] += 1
    today_key = today.isoformat()
    week_start = today - dt.timedelta(days=today.weekday())
    week = sum(n for d, n in days.items() if d >= week_start.isoformat()
               and d <= today_key)

    streak, cursor = 0, today
    if not days.get(today_key):
        cursor = today - dt.timedelta(days=1)
    while days.get(cursor.isoformat()):
        streak += 1
        cursor -= dt.timedelta(days=1)
    return {"today": days.get(today_key, 0), "week": week,
            "streak": streak, "total": sum(days.values()),
            "days_logged": len(days)}
