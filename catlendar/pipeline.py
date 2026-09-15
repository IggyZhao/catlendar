"""The submission and deadline tracker. Read by the dashboard, written back
when you edit it there, and safe to edit by hand in pipeline.yaml."""
import datetime as dt
import os
import shutil
import threading

import yaml

from .paths import DATA_DIR, ROOT_DIR

PIPELINE_PATH = os.path.join(DATA_DIR, "pipeline.yaml")
DEFAULT_PATH = os.path.join(ROOT_DIR, "pipeline.yaml")
_lock = threading.Lock()

# A researcher's vocabulary, not a to-do list's.
VALID_STATUS = ("to_be_done", "in_prep", "under_review", "accepted",
                "rejected", "done", "not_done")
OPEN_STATUS = ("to_be_done", "in_prep", "under_review")
LEGACY = {"todo": "to_be_done", "missed": "not_done"}


def ensure():
    if not os.path.exists(PIPELINE_PATH) and os.path.exists(DEFAULT_PATH):
        shutil.copyfile(DEFAULT_PATH, PIPELINE_PATH)
    return PIPELINE_PATH


def load():
    ensure()
    try:
        with open(PIPELINE_PATH, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        return {"sections": []}
    sections = []
    for s in data.get("sections") or []:
        items = []
        for i in s.get("items") or []:
            if not isinstance(i, dict) or not i.get("text"):
                continue
            status = LEGACY.get(i.get("status"), i.get("status", "to_be_done"))
            items.append({
                "text": str(i["text"]),
                "venue": str(i.get("venue") or ""),
                "date": str(i.get("date") or ""),
                "detail": str(i.get("detail") or ""),
                "project": i.get("project") or None,
                "status": status if status in VALID_STATUS else "to_be_done",
            })
        sections.append({
            "key": s.get("key") or "section",
            "label": s.get("label") or s.get("key") or "Section",
            "note": s.get("note") or "",
            "date_role": s.get("date_role") or "deadline",
            "items": items,
        })
    return {"sections": sections}


def save(payload):
    """Write back what the dashboard sends. Keeps a single backup copy."""
    sections = []
    for s in (payload or {}).get("sections") or []:
        items = []
        for i in s.get("items") or []:
            text = str(i.get("text") or "").strip()
            if not text:
                continue
            status = LEGACY.get(i.get("status"), i.get("status"))
            row = {"text": text, "status": status if status in VALID_STATUS else "to_be_done"}
            for field in ("venue", "date", "detail", "project"):
                value = str(i.get(field) or "").strip()
                if value:
                    row[field] = value
            items.append(row)
        sections.append({
            "key": str(s.get("key") or "section"),
            "label": str(s.get("label") or "Section"),
            "note": str(s.get("note") or ""),
            "date_role": s.get("date_role") or "deadline",
            "items": items,
        })
    with _lock:
        ensure()
        if os.path.exists(PIPELINE_PATH):
            shutil.copyfile(PIPELINE_PATH, PIPELINE_PATH + ".bak")
        tmp = PIPELINE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("# Catlendar pipeline. status: todo | done | missed\n")
            yaml.safe_dump({"sections": sections}, fh, allow_unicode=True,
                           sort_keys=False, default_flow_style=False)
        os.replace(tmp, PIPELINE_PATH)
    return len(sections)


def summary(today=None):
    """Counts and the next few dated things, for the dashboard header."""
    today = today or dt.date.today()
    data = load()
    upcoming, overdue = [], []
    counts = {k: 0 for k in VALID_STATUS}
    for section in data["sections"]:
        for item in section["items"]:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
            if not item["date"] or section.get("date_role") != "deadline":
                continue
            try:
                when = dt.date.fromisoformat(item["date"])
            except ValueError:
                continue
            row = dict(item, section=section["label"], days=(when - today).days)
            if item["status"] in OPEN_STATUS and when >= today:
                upcoming.append(row)
            elif item["status"] in OPEN_STATUS + ("not_done",) and when < today:
                overdue.append(row)
    upcoming.sort(key=lambda r: r["date"])
    overdue.sort(key=lambda r: r["date"], reverse=True)
    return {"counts": counts, "upcoming": upcoming[:8], "overdue": overdue[:6],
            "sections": data["sections"]}
