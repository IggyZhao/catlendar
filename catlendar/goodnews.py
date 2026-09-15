"""Good news, kept in its own file. Small, append-only in spirit, editable."""
import datetime as dt
import os
import shutil
import threading

import yaml

from .paths import DATA_DIR

PATH = os.path.join(DATA_DIR, "goodnews.yaml")
_lock = threading.Lock()
MAX_ITEMS = 200


def load():
    if not os.path.exists(PATH):
        return []
    try:
        with open(PATH, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        return []
    out = []
    for item in data.get("items") or []:
        if isinstance(item, dict) and str(item.get("text") or "").strip():
            out.append({
                "text": str(item["text"]).strip(),
                "date": str(item.get("date") or ""),
            })
    out.sort(key=lambda i: i["date"], reverse=True)
    return out[:MAX_ITEMS]


def save(items):
    rows = []
    for item in items or []:
        text = str((item or {}).get("text") or "").strip()
        if not text:
            continue
        date = str(item.get("date") or "").strip() or dt.date.today().isoformat()
        rows.append({"text": text[:300], "date": date})
    rows.sort(key=lambda i: i["date"], reverse=True)
    rows = rows[:MAX_ITEMS]
    with _lock:
        if os.path.exists(PATH):
            shutil.copyfile(PATH, PATH + ".bak")
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("# Catlendar good news. Add anything worth remembering.\n")
            yaml.safe_dump({"items": rows}, fh, allow_unicode=True,
                           sort_keys=False, default_flow_style=False)
        os.replace(tmp, PATH)
    return len(rows)
