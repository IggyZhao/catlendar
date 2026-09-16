"""Ask questions about your own time, and let the answer propose changes.

Two backends, tried in order:

1. The `claude` command line tool, if it is installed and signed in. Nothing to
   configure: it uses the session you already have.
2. The Anthropic API, if ANTHROPIC_API_KEY is set.

Changes are never applied by the model. It proposes them, the dashboard shows
them as a button, and nothing happens until you click it.
"""
import datetime as dt
import json
import logging
import os
import re
import shutil
import subprocess

from . import config, db, goodnews, pipeline, report

log = logging.getLogger("catlendar.chat")

MODEL = os.environ.get("CATLENDAR_MODEL", "claude-sonnet-5")
TIMEOUT = 90

INSTRUCTIONS = """You are the assistant inside Catlendar, a local time tracker.
You are given a summary of the user's own tracked time and answer questions about it.

Rules:
- Answer in at most three short sentences. No preamble, no bullet lists unless asked.
- Use the numbers you are given. Never invent a number. If the summary does not
  contain what was asked, say so plainly.
- Times are already formatted, quote them as they appear.

If the user asks you to change something, do not describe the change in prose.
Append one fenced block, exactly:

```catlendar
{"action": "...", ...}
```

Allowed actions:
{"action":"add_time","start":"HH:MM","end":"HH:MM","project":"<project key>","mode":"add"}

Use mode "add" unless the user explicitly says the time should count as that
project INSTEAD of what was detected. "only" erases everything else in those ten
minute slots, so never choose it just because the user said "I was doing X".
{"action":"add_good_news","text":"..."}
{"action":"add_todo","text":"...","date":"YYYY-MM-DD"}
{"action":"set_status","text":"<exact item text>","status":"to_be_done|in_prep|under_review|accepted|rejected|done|not_done"}

Only use a project key from the list you are given. One block per reply at most.
You cannot change the user's real calendar; say so if asked."""


# An app started at login gets a bare PATH (/usr/bin:/bin:/usr/sbin:/sbin), so
# shutil.which alone would not find a tool installed in a user directory. Look
# where it actually gets installed.
CLI_LOCATIONS = [
    "~/.local/bin/claude",
    "~/.claude/local/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    "~/bin/claude",
    "~/AppData/Roaming/npm/claude.cmd",
]


def find_cli():
    found = shutil.which("claude")
    if found:
        return found
    for candidate in CLI_LOCATIONS:
        path = os.path.expanduser(candidate)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def available():
    """Which backend, if any, is ready."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api"
    if find_cli():
        return "cli"
    return None


def build_context():
    """A compact picture of the user's data for the model to answer from."""
    today = report.summarize(*report.range_for("day"), scope="day")
    week = report.summarize(*report.range_for("week"), scope="week")
    due = pipeline.summary()

    def rows(summary, n=8):
        return [{"project": r["label"], "time": report.fmt_hm(r["seconds"])}
                for r in summary["projects"][:n]]

    return {
        "now": dt.datetime.now().strftime("%A %d %B %Y, %H:%M"),
        "today": {
            "worked": report.fmt_hm(today["total_seconds"]),
            "first_last": [report.fmt_hm(0) if not today["first_ts"] else
                           dt.datetime.fromtimestamp(today["first_ts"]).strftime("%H:%M"),
                           "" if not today["last_ts"] else
                           dt.datetime.fromtimestamp(today["last_ts"]).strftime("%H:%M")],
            "projects": rows(today),
            "activities": [{"kind": a["label"], "time": report.fmt_hm(a["seconds"])}
                           for a in today["activities"][:6]],
            "meetings": [{"title": e["title"],
                          "at": dt.datetime.fromtimestamp(e["start"]).strftime("%H:%M")}
                         for e in today["events"][:8]],
            "with_claude": report.fmt_hm(today["claude_seconds"]),
        },
        "this_week": {
            "worked": report.fmt_hm(week["total_seconds"]),
            "per_day": [{"day": d["date"], "time": report.fmt_hm(d["seconds"])}
                        for d in week["days"]],
            "projects": rows(week),
            "kinds": [{"kind": k["label"], "time": report.fmt_hm(k["seconds"])}
                      for k in week["kinds"]],
        },
        "open_items": [{"text": r["text"], "date": r["date"], "status": r["status"]}
                       for r in (due["overdue"] + due["upcoming"])[:10]],
        "good_news": [g["text"] for g in goodnews.load()[:5]],
        "project_keys": [{"key": p["key"], "label": p["label"]}
                         for p in config.load()["projects"]],
    }


def ask(question):
    """Returns {reply, actions, backend, error}."""
    backend = available()
    if backend is None:
        return {"reply": "No model is set up. Install the Claude command line tool "
                         "and sign in, or set ANTHROPIC_API_KEY.",
                "actions": [], "backend": None, "error": True}

    prompt = "{}\n\nHere is the user's data as JSON:\n{}\n\nQuestion: {}".format(
        INSTRUCTIONS, json.dumps(build_context(), ensure_ascii=False), question)
    try:
        text = _call_cli(prompt) if backend == "cli" else _call_api(prompt)
    except Exception as exc:
        log.exception("chat backend failed")
        return {"reply": "The model could not be reached: {}".format(exc),
                "actions": [], "backend": backend, "error": True}

    reply, actions = _split_actions(text)
    return {"reply": reply, "actions": actions, "backend": backend}


def _call_cli(prompt):
    binary = find_cli()
    if binary is None:
        raise RuntimeError("the claude command line tool is not installed")
    # without this the tool waits three seconds for stdin that never comes, and
    # its warning about that lands in stderr and reads like the error
    res = subprocess.run([binary, "-p", prompt], capture_output=True, text=True,
                         timeout=TIMEOUT, stdin=subprocess.DEVNULL)
    if res.returncode != 0:
        lines = [l for l in (res.stderr or res.stdout).strip().splitlines()
                 if l.strip() and not l.lower().startswith("warning:")]
        first = lines[0] if lines else "unknown error"
        low = first.lower()
        if "auth" in low or "login" in low or "oauth" in low or "expired" in low:
            raise RuntimeError(
                "your Claude sign in has expired. Open a terminal, run: claude "
                "  then /login, and ask again. Or set ANTHROPIC_API_KEY.")
        raise RuntimeError(first[:200])
    return res.stdout.strip()


def _call_api(prompt):
    import urllib.request
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 700,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"content-type": "application/json",
                 "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return "".join(part.get("text", "") for part in payload.get("content", []))


ACTION_BLOCK = re.compile(r"```catlendar\s*(.+?)```", re.DOTALL)


def _split_actions(text):
    """Pull the proposal out of the prose, and keep only what we understand."""
    actions = []
    for raw in ACTION_BLOCK.findall(text or ""):
        try:
            parsed = json.loads(raw.strip())
        except ValueError:
            continue
        for item in (parsed if isinstance(parsed, list) else [parsed]):
            if isinstance(item, dict) and item.get("action") in APPLY:
                actions.append(item)
    reply = ACTION_BLOCK.sub("", text or "").strip()
    return reply, actions


# --------------------------------------------------------------- applying one
def _apply_add_time(a):
    keys = {p["key"] for p in config.load()["projects"]}
    if a.get("project") not in keys:
        return False, "that project does not exist"
    day_start, day_end = report.range_for("day")
    def stamp(hhmm):
        hour, minute = [int(x) for x in str(hhmm).split(":")]
        base = dt.datetime.fromtimestamp(day_start).replace(hour=0, minute=0, second=0)
        when = base + dt.timedelta(hours=hour, minutes=minute)
        if when.timestamp() < day_start:          # after midnight belongs to this day
            when += dt.timedelta(days=1)
        return int(when.timestamp())
    try:
        start, end = stamp(a["start"]), stamp(a["end"])
    except Exception:
        return False, "could not read those times"
    if end <= start:
        return False, "the end is not after the start"
    rows = [{"start_ts": m["start"], "end_ts": m["end"], "project": m["project"],
             "note": m["note"], "mode": m["mode"]}
            for m in report.summarize(day_start, day_end, "day")["manual"]]
    rows.append({"start_ts": start, "end_ts": end, "project": a["project"],
                 "note": "added by chat",
                 "mode": "only" if a.get("mode") == "only" else "add"})
    db.replace_manual_for_day(int(day_start), int(day_end), rows)
    return True, "added"


def _apply_good_news(a):
    text = str(a.get("text") or "").strip()
    if not text:
        return False, "nothing to add"
    items = goodnews.load()
    items.insert(0, {"text": text, "date": dt.date.today().isoformat()})
    goodnews.save(items)
    return True, "added"


def _apply_add_todo(a):
    text = str(a.get("text") or "").strip()
    if not text:
        return False, "nothing to add"
    data = pipeline.load()
    target = next((s for s in data["sections"] if s["key"] == "deadlines"),
                  data["sections"][-1] if data["sections"] else None)
    if target is None:
        return False, "there is no section to add it to"
    target["items"].append({"text": text, "venue": "", "date": str(a.get("date") or ""),
                            "detail": "", "project": None, "status": "to_be_done"})
    pipeline.save(data)
    return True, "added"


def _apply_set_status(a):
    wanted = str(a.get("text") or "").strip().lower()
    status = a.get("status")
    if status not in pipeline.VALID_STATUS:
        return False, "that is not a status"
    data = pipeline.load()
    for section in data["sections"]:
        for item in section["items"]:
            if item["text"].strip().lower() == wanted:
                item["status"] = status
                pipeline.save(data)
                return True, "updated"
    return False, "could not find that item"


APPLY = {
    "add_time": _apply_add_time,
    "add_good_news": _apply_good_news,
    "add_todo": _apply_add_todo,
    "set_status": _apply_set_status,
}


def apply_action(action):
    handler = APPLY.get((action or {}).get("action"))
    if handler is None:
        return False, "unknown action"
    try:
        return handler(action)
    except Exception as exc:
        log.exception("could not apply an action")
        return False, str(exc)[:120]


def describe(action):
    """One line for the confirm button."""
    kind = action.get("action")
    if kind == "add_time":
        if action.get("mode") == "only":
            return ("REPLACE everything between {} and {} with {}, erasing whatever "
                    "else was detected in those slots").format(
                        action.get("start"), action.get("end"), action.get("project"))
        return "Add {} to {} as {}".format(
            action.get("start"), action.get("end"), action.get("project"))
    if kind == "add_good_news":
        return "Add good news: {}".format(action.get("text"))
    if kind == "add_todo":
        return "Add to do: {} {}".format(action.get("text"), action.get("date") or "")
    if kind == "set_status":
        return "Set {} to {}".format(action.get("text"), action.get("status"))
    return kind
