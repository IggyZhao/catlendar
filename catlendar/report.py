"""Turns raw samples into the numbers the dashboard shows."""
import bisect
import datetime as dt
from collections import Counter, defaultdict

from . import config, db

WORK_STATES = ("active", "meeting")
SLOT = 600     # ten minutes: the unit everything is counted in

# Markers for where a slot came from, not kinds of work. They still decide which
# slots count and which project they belong to; they just do not belong in a
# chart answering "what kind of work was this".
SOURCE_MARKERS = {"claude", "files"}
KIND_LABELS = {"research": "Research", "teaching": "Teaching", "service": "Service",
               "admin": "Admin", "unassigned": "Unmatched"}


def _folder_of(path):
    """The project folder a changed file sits in, shortened for display."""
    if not path:
        return ""
    parts = [p for p in str(path).split("/") if p]
    for marker in ("Research", "Teaching", "Service"):
        if marker in parts:
            i = parts.index(marker)
            return " / ".join(parts[i + 1:i + 3])
    return " / ".join(parts[-3:-1])


# ---------------------------------------------------------------- time ranges
# A day does not end at midnight. Work at 00:30 belongs to the evening you were
# already in, so the day runs from day_start_hour to day_start_hour the next
# morning. Set it in projects.yaml.
def day_offset():
    return int(config.setting("day_start_hour")) * 3600


def day_anchor(d):
    """Midnight of `d` pushed forward to the hour the day really starts."""
    return dt.datetime(d.year, d.month, d.day) + dt.timedelta(seconds=day_offset())


def logical_date(ts):
    """Which day a moment belongs to. 1am Tuesday is still Monday."""
    return (dt.datetime.fromtimestamp(ts) - dt.timedelta(seconds=day_offset())).date()


def today():
    import time as _time
    return logical_date(_time.time())


def local_midnight(d):          # kept for callers that want the real midnight
    return dt.datetime(d.year, d.month, d.day)


def day_range(d=None):
    d = d or today()
    start = day_anchor(d)
    return start.timestamp(), (start + dt.timedelta(days=1)).timestamp()


def week_range(d=None):
    d = d or today()
    start = day_anchor(d - dt.timedelta(days=d.weekday()))   # Monday
    return start.timestamp(), (start + dt.timedelta(days=7)).timestamp()


def month_range(d=None):
    d = d or today()
    start = day_anchor(d.replace(day=1))
    end_day = day_anchor((start + dt.timedelta(days=32)).date().replace(day=1))
    return start.timestamp(), end_day.timestamp()


def range_for(scope, anchor=None):
    anchor = anchor or today()
    return {"day": day_range, "week": week_range, "month": month_range}[scope](anchor)


# ------------------------------------------------------------------ aggregate
def _blocks(samples, max_gap):
    """Collapse consecutive samples of the same project into blocks."""
    blocks = []
    for s in samples:
        if s["state"] not in WORK_STATES:
            continue
        key = (s["project"], s["activity"])
        if blocks:
            last = blocks[-1]
            if last["key"] == key and s["ts"] - last["end"] <= max_gap:
                last["end"] = s["ts"] + s["dur"]
                last["seconds"] += s["dur"]
                if s["title"]:
                    last["titles"][s["title"]] += s["dur"]
                continue
        blocks.append(
            {
                "key": key,
                "project": s["project"],
                "activity": s["activity"],
                "start": s["ts"],
                "end": s["ts"] + s["dur"],
                "seconds": s["dur"],
                "titles": Counter({s["title"]: s["dur"]} if s["title"] else {}),
            }
        )
    return blocks


def _day_series(by_day, start_ts, end_ts, scope):
    """One entry per calendar day in range, zero-filled, so a week always shows
    seven columns and a gap reads as a gap instead of vanishing."""
    out = []
    day = logical_date(start_ts)
    last = logical_date(end_ts - 1)
    now_day = today()
    if scope == "month" and last > now_day:
        last = now_day
    while day <= last:
        key = day.strftime("%Y-%m-%d")
        counter = by_day.get(key, Counter())
        out.append({
            "date": key,
            "seconds": counter.pop("__total__", 0),
            "projects": [
                {"key": k, "label": config.project_label(k), "seconds": v}
                for k, v in counter.most_common()
            ],
        })
        day += dt.timedelta(days=1)
    return out


def _slot_index(ts, start_ts):
    return int((ts - start_ts) // SLOT)


def summarize(start_ts, end_ts, scope="day"):
    """Time is counted in ten minute slots. A slot where you touched three
    projects counts for all three, so the per project numbers add up to more
    than the clock. That is deliberate: it is how the work actually happens."""
    start_ts, end_ts = int(start_ts), int(end_ts)
    n_slots = max(1, (end_ts - start_ts + SLOT - 1) // SLOT)

    samples = db.samples_between(start_ts, end_ts)
    signals = [r for r in db.signals_between(start_ts - 7200, end_ts + 7200)]
    events = [e for e in db.events_between(start_ts, end_ts) if not e["all_day"]]
    manual = db.manual_between(start_ts, end_ts)

    # Nothing in the future counts as time already spent. A meeting on Thursday
    # is on the calendar, not on the clock.
    horizon = min(end_ts, int(dt.datetime.now().timestamp()) + SLOT)

    slot_projects = defaultdict(set)      # slot -> {project key}
    slot_acts = defaultdict(set)          # slot -> {activity key}
    slot_active = set()                   # slots where work was really happening
    slot_apps = defaultdict(set)
    titles_by_project = defaultdict(Counter)
    only_slots = {}                       # slot -> project, from a manual override
    pending = []                          # evidence that can label a slot but not create one

    def span_slots(a, b):
        a, b = max(int(a), start_ts), min(int(b), horizon - 1)
        if b < a:
            return range(0)
        return range(_slot_index(a, start_ts), _slot_index(b, start_ts) + 1)

    def mark(slot, project, activity=None, app=None, presence=True):
        """presence=True means this evidence proves work was happening.
        presence=False can only label a slot that something else already proved."""
        if slot < 0 or slot >= n_slots:
            return
        if presence:
            slot_active.add(slot)
        elif slot not in slot_active:
            return
        if project:
            slot_projects[slot].add(project)
        if activity:
            slot_acts[slot].add(activity)
        if app:
            slot_apps[slot].add(app)

    # --- presence: you at the keyboard ---
    for row in samples:
        if row["state"] not in WORK_STATES:
            continue          # idle or locked is not work
        mark(_slot_index(row["ts"], start_ts), row["project"], row["activity"], row["app"])
        if row["title"]:
            titles_by_project[row["project"]][row["title"]] += row["dur"]

    # --- presence: Claude actually producing. A span ends at its last message,
    #     so a finished session stops counting on its own. ---
    for sig in signals:
        if sig["kind"] != "claude":
            pending.append(sig)
            continue
        for slot in span_slots(sig["ts"], sig["end_ts"] or sig["ts"]):
            mark(slot, sig["project"], "claude")

    # --- presence: meetings and anything you added by hand ---
    meetings_count = bool(config.setting("count_meetings_as_work"))
    for ev in events:
        for slot in span_slots(ev["start_ts"], ev["end_ts"]):
            mark(slot, ev["project"], "meeting", presence=meetings_count)
    for m in manual:
        for slot in span_slots(m["start_ts"], m["end_ts"]):
            mark(slot, m["project"], "manual")
        if m["mode"] == "only":
            for slot in span_slots(m["start_ts"], m["end_ts"]):
                only_slots[slot] = m["project"]

    # --- not presence: a saved file proves a project, not that you were there.
    #     Dropbox sync, a backup or a build can touch files on their own. ---
    for sig in pending:
        for slot in span_slots(sig["ts"], sig["end_ts"] or sig["ts"]):
            mark(slot, sig["project"], "files", presence=False)

    for slot, project in only_slots.items():
        slot_projects[slot] = {project} if project else set()

    # ---- roll the slots up
    by_project, by_activity, by_app, by_hour = Counter(), Counter(), Counter(), Counter()
    by_day = defaultdict(Counter)
    for slot in sorted(slot_active):
        when = dt.datetime.fromtimestamp(start_ts + slot * SLOT)
        day_key = logical_date(start_ts + slot * SLOT).strftime("%Y-%m-%d")
        by_hour[when.hour] += SLOT
        by_day[day_key]["__total__"] += SLOT
        # Ten minutes is ten minutes. If three projects share a slot they get
        # a third each, so the parts add up to the clock instead of inflating it.
        projects = slot_projects.get(slot) or {None}
        share = SLOT / len(projects)
        for project in projects:
            by_project[project] += share
            if project:
                by_day[day_key][project] += share
        for activity in slot_acts.get(slot, ()):
            by_activity[activity] += SLOT
        for app in slot_apps.get(slot, ()):
            by_app[app] += SLOT

    total = len(slot_active) * SLOT
    attributed = sum(v for k, v in by_project.items() if k)
    unmatched = by_project.get(None, 0)

    by_kind = Counter()
    for slot in slot_active:
        kinds = {config.project_kind(p) for p in slot_projects.get(slot, ())} or {"unassigned"}
        for kind in kinds:
            by_kind[kind] += SLOT / len(kinds)

    # whole seconds once the sharing is done
    by_project = Counter({k: int(round(v)) for k, v in by_project.items()})
    by_kind = Counter({k: int(round(v)) for k, v in by_kind.items()})
    by_day = {d: Counter({k: int(round(v)) for k, v in c.items()}) for d, c in by_day.items()}

    # ---- contiguous runs per project, for the timeline
    timeline = []
    for project in {p for s in slot_projects.values() for p in s}:
        slots = sorted(s for s, ps in slot_projects.items() if project in ps)
        run = []
        for slot in slots + [None]:
            if run and slot is not None and slot == run[-1] + 1:
                run.append(slot)
                continue
            if run:
                a = start_ts + run[0] * SLOT
                b = start_ts + (run[-1] + 1) * SLOT
                acts = Counter()
                for sl in run:
                    for act in slot_acts.get(sl, ()):
                        acts[act] += 1
                best_title = titles_by_project[project].most_common(1)
                timeline.append({
                    "start": a, "end": b, "seconds": b - a,
                    "project": project, "project_label": config.project_label(project),
                    "activity": acts.most_common(1)[0][0] if acts else None,
                    "activity_label": config.activity_label(acts.most_common(1)[0][0]) if acts else "",
                    "title": best_title[0][0] if best_title else None,
                })
            run = [slot] if slot is not None else []
    timeline.sort(key=lambda b: (b["start"], b["project_label"]))

    runs = [b["seconds"] for b in timeline]
    active_runs = _active_runs(sorted(slot_active), start_ts)

    def rows(counter, labeler):
        return [{"key": k, "label": labeler(k), "seconds": v,
                 "share": (v / total) if total else 0.0}
                for k, v in counter.most_common()]

    n_days = len({logical_date(start_ts + s * SLOT) for s in slot_active}) or 1
    day_prompts = [r for r in signals if r["kind"] == "claude" and start_ts <= r["ts"] < end_ts]
    day_files = [r for r in signals if r["kind"] == "file" and start_ts <= r["ts"] < end_ts]
    claude_seconds = sum(SLOT for s in slot_active if "claude" in slot_acts.get(s, ()))

    return {
        "scope": scope,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "slot_seconds": SLOT,
        "generated_at": int(dt.datetime.now().timestamp()),
        "total_seconds": total,
        "attributed_seconds": attributed,
        "unmatched_seconds": unmatched,
        "overlap_seconds": 0,   # a slot is shared, never counted twice
        "claude_seconds": claude_seconds,
        "active_days": n_days,
        "meeting_seconds": sum(max(0, min(e["end_ts"], horizon) - max(e["start_ts"], start_ts))
                               for e in events),
        "meeting_count": sum(1 for e in events if e["start_ts"] < horizon),
        "meetings_ahead": sum(1 for e in events if e["start_ts"] >= horizon),
        "deep_seconds": sum(r for r in active_runs if r >= 1800),
        "focus_seconds": sum(r for r in active_runs if r >= 900),
        "longest_block": max(active_runs, default=0),
        "switches": max(0, len(timeline) - 1),
        "first_ts": start_ts + min(slot_active) * SLOT if slot_active else None,
        "last_ts": start_ts + (max(slot_active) + 1) * SLOT if slot_active else None,
        "projects": rows(Counter({k: v for k, v in by_project.items() if k}), config.project_label)
                    + ([{"key": None, "label": "Unmatched", "seconds": unmatched,
                         "share": (unmatched / total) if total else 0.0}] if unmatched else []),
        "activities": rows(Counter({k: v for k, v in by_activity.items()
                                    if k not in SOURCE_MARKERS}), _activity_label),
        "apps": [{"label": k or "Unknown", "seconds": v} for k, v in by_app.most_common(12)],
        "hours": [{"hour": h, "seconds": by_hour.get(h, 0)} for h in range(24)],
        "days": _day_series(by_day, start_ts, end_ts, scope),
        "timeline": timeline,
        "detail": {
            config.project_label(p): [{"title": t, "seconds": sec} for t, sec in c.most_common(6)]
            for p, c in sorted(titles_by_project.items(), key=lambda kv: -sum(kv[1].values()))[:10]
        },
        "events": [{
            "title": e["title"], "start": e["start_ts"], "end": e["end_ts"],
            "calendar": e["calendar"], "attendees": e["attendees"],
            "project": e["project"], "project_label": config.project_label(e["project"]),
            "seconds": max(0, e["end_ts"] - e["start_ts"]),
        } for e in events],
        "manual": [{
            "id": m["id"], "start": m["start_ts"], "end": m["end_ts"],
            "project": m["project"], "project_label": config.project_label(m["project"]),
            "note": m["note"] or "", "mode": m["mode"] or "add",
        } for m in manual],
        "kinds": [{"key": k, "label": KIND_LABELS.get(k, k), "seconds": v,
                   "share": (v / total) if total else 0.0}
                  for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1])],
        "prompts": [{"ts": r["ts"], "end": r["end_ts"], "text": r["detail"],
                     "project": r["project"], "project_label": config.project_label(r["project"])}
                    for r in day_prompts[-60:]],
        "prompt_count": len(day_prompts),
        "files": [{"ts": r["ts"], "name": r["detail"], "project": r["project"],
                   "project_label": config.project_label(r["project"]),
                   "folder": _folder_of(r["path"])} for r in day_files[-80:]],
        "file_count": len(day_files),
        "top_file_projects": [{"label": config.project_label(k), "count": v}
                              for k, v in Counter(r["project"] for r in day_files).most_common(6)],
        "calendar_source": db.get_meta("calendar_source", "none"),
        "last_calendar_sync": db.get_meta("last_calendar_sync"),
        "projects_all": [{"key": p["key"], "label": p["label"], "kind": p["kind"]}
                         for p in config.load()["projects"]],
    }


def _active_runs(slots, start_ts):
    """Lengths of unbroken stretches of active slots."""
    runs, run = [], 0
    prev = None
    for slot in slots:
        if prev is not None and slot == prev + 1:
            run += SLOT
        else:
            if run:
                runs.append(run)
            run = SLOT
        prev = slot
    if run:
        runs.append(run)
    return runs


def _activity_label(key):
    if key == "claude":
        return "With Claude"
    if key == "files":
        return "Saving files"
    if key == "manual":
        return "Added by you"
    return config.activity_label(key)


def current_status():
    """One-line 'what am I doing right now', for the menu bar and the cat."""
    import time
    now = int(time.time())
    recent = db.samples_between(now - 4 * 3600, now + 1)
    today = summarize(*day_range(), scope="day")
    live = recent[-1] if recent else None
    block_seconds = 0
    if live and live["state"] in WORK_STATES:
        gap_limit = max(120, int(config.setting("sample_interval_seconds")) * 6)
        prev_ts = None
        for s_row in reversed(recent):
            if s_row["state"] not in WORK_STATES or s_row["project"] != live["project"]:
                break
            if prev_ts is not None and prev_ts - (s_row["ts"] + s_row["dur"]) > gap_limit:
                break
            block_seconds += s_row["dur"]
            prev_ts = s_row["ts"]
    ev = None
    for e in db.events_between(now - 300, now + 3600):
        if e["all_day"]:
            continue
        # strictly started: a five minute grace here would mask the "starting
        # soon" alert, since a meeting about to begin would already read as one
        # you are in
        if e["start_ts"] <= now <= e["end_ts"]:
            ev = e
            break
    next_ev = None
    for e in db.events_between(now, now + 86400):
        if e["all_day"] or e["start_ts"] <= now:
            continue
        next_ev = e
        break
    return {
        "now": now,
        "state": live["state"] if live else "unknown",
        "block_seconds": block_seconds,
        "app": live["app"] if live else None,
        "project": live["project"] if live else None,
        "project_label": config.project_label(live["project"]) if live else "Unassigned",
        "activity_label": config.activity_label(live["activity"]) if live else "",
        "in_meeting": ev is not None,
        "meeting_title": ev["title"] if ev else None,
        "meeting_ends": ev["end_ts"] if ev else None,
        "next_meeting": next_ev["title"] if next_ev else None,
        "next_meeting_ts": next_ev["start_ts"] if next_ev else None,
        "today_seconds": today["total_seconds"],
        "today_top": today["projects"][0]["label"] if today["projects"] else None,
        "today_meetings": today["meeting_count"],
    }


def fmt_hm(seconds):
    seconds = int(seconds or 0)
    h, m = seconds // 3600, (seconds % 3600) // 60
    if h and m:
        return "{}h {}m".format(h, m)
    if h:
        return "{}h".format(h)
    return "{}m".format(m)
