"""Loads projects.yaml and turns (app, title) into (project, activity)."""
import os
import re
import shutil
import threading

import yaml

from .paths import CONFIG_PATH, DEFAULT_CONFIG_PATH

_lock = threading.Lock()
_cache = {"mtime": 0.0, "cfg": None}

DEFAULTS = {
    "sample_interval_seconds": 10,
    "idle_after_seconds": 150,
    "calendar_sync_minutes": 10,
    "day_start_hour": 4,
    "asleep_after_minutes": 5,
    "hide_when_fullscreen": True,
    "cat_layer": "desktop",     # desktop: windows cover it. floating: always on top
    "raise_for_meeting_alert": True,   # lift it above windows for the 5 minute warning
    "count_meetings_as_work": True,
    "meeting_grace_minutes": 5,
}


def ensure_user_config():
    """First run: copy the shipped rules into Application Support so edits survive updates."""
    if not os.path.exists(CONFIG_PATH) and os.path.exists(DEFAULT_CONFIG_PATH):
        shutil.copyfile(DEFAULT_CONFIG_PATH, CONFIG_PATH)
    return CONFIG_PATH


def load(force=False):
    ensure_user_config()
    with _lock:
        try:
            mtime = os.path.getmtime(CONFIG_PATH)
        except OSError:
            mtime = 0.0
        if not force and _cache["cfg"] is not None and mtime <= _cache["mtime"]:
            return _cache["cfg"]
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        cfg = _compile(raw)
        _cache["mtime"] = mtime
        _cache["cfg"] = cfg
        return cfg


def _compile_keywords(words):
    """Compile keywords to (regex, weight).

    A keyword that starts with a letter or digit only matches at a word start,
    so "review" no longer fires on "Preview" and "dia " no longer fires on
    "media ". Endings stay open so "review" still matches "reviewing".
    """
    out = []
    for raw_kw in words or []:
        kw = str(raw_kw).lower()
        if not kw:
            continue
        pattern = re.escape(kw)
        if kw[0].isalnum() or kw[0] == "_":
            pattern = r"(?<!\w)" + pattern
        if len(kw) <= 3 and (kw[-1].isalnum() or kw[-1] == "_"):
            # a tag as short as "p1" must not fire inside "p10" or "displayed"
            pattern += r"(?!\w)"
        out.append((re.compile(pattern), len(kw)))
    return out


def _compile(raw):
    settings = dict(DEFAULTS)
    settings.update(raw.get("settings") or {})
    privacy = raw.get("privacy") or {}

    projects = []
    for p in raw.get("projects") or []:
        projects.append(
            {
                "key": p.get("key"),
                "label": p.get("label") or p.get("key"),
                "kind": p.get("kind") or "research",
                "paths": [x.lower() for x in (p.get("paths") or [])],
                "keywords": _compile_keywords(p.get("keywords")),
                "exclude": _compile_keywords(p.get("exclude")),
            }
        )

    activities = []
    for a in raw.get("activities") or []:
        activities.append(
            {
                "key": a.get("key"),
                "label": a.get("label") or a.get("key"),
                "apps": [x.lower() for x in (a.get("apps") or [])],
                "keywords": _compile_keywords(a.get("keywords")),
            }
        )

    return {
        "settings": settings,
        "privacy": {
            "redact_title_contains": [x.lower() for x in (privacy.get("redact_title_contains") or [])],
            "redact_apps": [x.lower() for x in (privacy.get("redact_apps") or [])],
        },
        "projects": projects,
        "activities": activities,
        "project_labels": {p["key"]: p["label"] for p in projects},
        "project_kinds": {p["key"]: p["kind"] for p in projects},
        "activity_labels": {a["key"]: a["label"] for a in activities},
    }


def setting(name):
    return load()["settings"].get(name, DEFAULTS.get(name))


def set_setting(name, value):
    """Write one setting back to projects.yaml, leaving the rest of the file be."""
    import re as _re
    path = ensure_user_config()
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    line = "  {}: {}".format(name, value)
    pattern = _re.compile(r"^  {}:.*$".format(_re.escape(name)), _re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(line, text, count=1)
    else:
        text = text.replace("settings:", "settings:\n" + line, 1)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    load(force=True)
    return value


def redact_title(app, title):
    """Return the title to store, blanking anything the privacy rules cover."""
    cfg = load()
    app_l = (app or "").lower()
    title_l = (title or "").lower()
    if any(a == app_l for a in cfg["privacy"]["redact_apps"]):
        return "(private)"
    if any(k in title_l for k in cfg["privacy"]["redact_title_contains"]):
        return "(private)"
    return title


def classify(app, title, cfg=None):
    """Longest matching keyword wins, so a specific phrase beats a generic one."""
    cfg = cfg or load()
    hay = "{} | {}".format(app or "", title or "").lower()

    project, best = None, 0
    for p in cfg["projects"]:
        if any(rx.search(hay) for rx, _ in p["exclude"]):
            continue
        for rx, weight in p["keywords"]:
            if weight > best and rx.search(hay):
                project, best = p["key"], weight

    activity, best_a = None, 0
    for a in cfg["activities"]:
        for rx, weight in a["keywords"]:
            if weight > best_a and rx.search(hay):
                activity, best_a = a["key"], weight
    if activity is None:
        app_l = (app or "").lower()
        for a in cfg["activities"]:
            if app_l in a["apps"]:
                activity = a["key"]
                break
    return project, activity


def classify_path(path):
    """Match a filesystem path against the project folders. Longest folder
    match wins, so a nested folder beats its parent."""
    if not path:
        return None
    hay = str(path).lower().replace("\\", "/")
    best, project = 0, None
    for p in load()["projects"]:
        for folder in p["paths"]:
            if folder and folder in hay and len(folder) > best:
                best, project = len(folder), p["key"]
    if project is None:
        # No folder matched, so try the keywords against the path text itself.
        # This catches a working copy that lives nowhere near the project folder.
        project = classify(None, hay)[0]
    return project


def project_kind(key):
    if not key:
        return "unassigned"
    return load()["project_kinds"].get(key, "research")


def project_label(key):
    if not key:
        return "Unassigned"
    return load()["project_labels"].get(key, key)


def activity_label(key):
    if not key:
        return "Other"
    return load()["activity_labels"].get(key, key)
