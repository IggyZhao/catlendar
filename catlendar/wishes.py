"""The wish wall: what you want, written down where you will see it.

Deliberately not a to do list. Nothing here has a deadline, nothing is overdue,
and nothing nags. A wish sits on the wall until the day it comes true, and then
it stays on the wall as proof that some of them do.
"""
import datetime as dt
import os
import shutil
import threading

import yaml

from .paths import DATA_DIR

PATH = os.path.join(DATA_DIR, "wishes.yaml")
_lock = threading.Lock()
MAX_ITEMS = 200


def _clean(item):
    text = str((item or {}).get("text") or "").strip()
    if not text:
        return None

    def date_or_blank(value, fallback=""):
        value = str(value or "").strip()
        try:
            dt.date.fromisoformat(value)
            return value
        except ValueError:
            return fallback

    today = dt.date.today().isoformat()
    return {
        "text": text[:400],
        "since": date_or_blank(item.get("since"), today),
        "granted": date_or_blank(item.get("granted"), ""),
    }


def load():
    if not os.path.exists(PATH):
        return []
    try:
        with open(PATH, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        return []
    out = [w for w in (_clean(i) for i in (data.get("items") or [])) if w]
    # still wanted first, oldest wish at the top: the one you have carried
    # longest deserves the most looking at
    out.sort(key=lambda w: (bool(w["granted"]), w["granted"] or "",
                            w["since"] or ""))
    return out[:MAX_ITEMS]


def save(items):
    rows = [w for w in (_clean(i) for i in (items or [])) if w]
    rows = rows[:MAX_ITEMS]
    with _lock:
        if os.path.exists(PATH):
            shutil.copyfile(PATH, PATH + ".bak")
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("# Catlendar wish wall. Write them as if they are already "
                     "true.\n# granted: the date it came true. Leave it blank "
                     "until then.\n")
            yaml.safe_dump({"items": rows}, fh, allow_unicode=True,
                           sort_keys=False, default_flow_style=False)
        os.replace(tmp, PATH)
    return len(rows)


def granted_count(items=None):
    return sum(1 for w in (items if items is not None else load()) if w["granted"])
