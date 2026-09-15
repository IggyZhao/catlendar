"""Fills a Catlendar database with invented data, so you can see the dashboard
without waiting a week for your own to build up.

    CATLENDAR_DATA_DIR=/tmp/catlendar-demo python scripts/demo_data.py
    CATLENDAR_DATA_DIR=/tmp/catlendar-demo python -m catlendar report

Everything here is made up. Point it at a throwaway data folder, never your real
one: it clears whatever is already there.
"""
import datetime as dt
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from catlendar import db                                    # noqa: E402
from catlendar.paths import DATA_DIR                        # noqa: E402

PROJECTS = [
    ("thesis-chapter-three", "Thesis Chapter Three", "research"),
    ("grant-proposal", "Grant Proposal", "research"),
    ("survey-analysis", "Survey Analysis", "research"),
    ("intro-statistics", "Intro Statistics", "teaching"),
    ("peer-review", "Peer Review", "service"),
    ("department-admin", "Department Admin", "admin"),
]
SCENES = [
    ("Cursor", "code", ["analysis.py", "clean_data.py", "figures.R"], "thesis-chapter-three"),
    ("Microsoft Word", "writing", ["Chapter3_draft.docx", "Proposal_v4.docx"], "grant-proposal"),
    ("RStudio", "analysis", ["models.R", "survey_tables.R"], "survey-analysis"),
    ("Preview", "reading", ["Smith2024.pdf", "review_manuscript.pdf"], "peer-review"),
    ("Google Chrome", "browsing", ["Course site", "Library search"], "intro-statistics"),
    ("Microsoft Outlook", "comms", ["Inbox"], "department-admin"),
]
MEETINGS = [("Weekly lab meeting", 10, 60), ("Advisor check-in", 14, 30),
            ("Reading group", 15, 60), ("Office hours", 13, 90)]
PROMPTS = [
    "help me tighten the argument in section 3",
    "what does this regression output actually say",
    "draft a reply to the reviewer on the sample size",
    "rewrite this paragraph so it is shorter",
    "check whether these two figures tell the same story",
]


def run(days=28):
    random.seed(11)
    db.init()
    with db.cursor() as conn:
        for table in ("samples", "signals", "events", "manual", "meta"):
            conn.execute("DELETE FROM {}".format(table))

    today = dt.date.today()
    samples, signals, events = [], [], []

    for back in range(days):
        day = today - dt.timedelta(days=back)
        if day.weekday() >= 5 and random.random() < 0.7:
            continue
        start_hour = random.choice([8, 9, 9, 10])
        end_hour = random.choice([16, 17, 18, 18, 19])
        if day == today:
            end_hour = min(end_hour, max(start_hour + 3, dt.datetime.now().hour))

        for title, hour, minutes in random.sample(MEETINGS, random.randint(0, 2)):
            begin = dt.datetime(day.year, day.month, day.day, hour, 0)
            events.append({
                "uid": "demo-{}-{}".format(day, title.replace(" ", "-")),
                "title": title,
                "start_ts": int(begin.timestamp()),
                "end_ts": int((begin + dt.timedelta(minutes=minutes)).timestamp()),
                "calendar": "Work", "location": None, "all_day": 0,
                "organizer": None, "attendees": random.randint(2, 8),
                "status": "1", "project": None, "source": "demo",
            })

        clock = dt.datetime(day.year, day.month, day.day, start_hour, random.randint(0, 40))
        finish = dt.datetime(day.year, day.month, day.day, end_hour, 0)
        while clock < finish:
            app, activity, titles, project = random.choice(SCENES)
            block = random.choice([20, 30, 45, 60, 80]) * 60
            title = random.choice(titles)
            for offset in range(0, block, 10):
                samples.append((int(clock.timestamp()) + offset, 10, app, None,
                                title, project, activity, "active"))
            if random.random() < 0.5:
                begin = int(clock.timestamp()) + random.randint(0, block // 2)
                signals.append((begin, begin + random.choice([600, 1200, 1800]),
                                "claude", project, random.choice(PROMPTS), ""))
            if random.random() < 0.6:
                signals.append((int(clock.timestamp()) + block - 60, None, "file",
                                project, random.choice(titles), ""))
            clock += dt.timedelta(seconds=block)
            if random.random() < 0.3:
                clock += dt.timedelta(minutes=random.choice([10, 20, 40]))

    with db.cursor() as conn:
        conn.executemany(
            "INSERT INTO samples (ts,dur,app,bundle_id,title,project,activity,state)"
            " VALUES (?,?,?,?,?,?,?,?)", samples)
        conn.executemany(
            "INSERT OR IGNORE INTO signals (ts,end_ts,kind,project,detail,path)"
            " VALUES (?,?,?,?,?,?)", signals)
    db.upsert_events(events)
    _write_pipeline(today)
    db.set_meta("calendar_source", "demo")
    db.set_meta("last_calendar_sync", int(time.time()))
    print("demo data written to {}".format(DATA_DIR))
    print("  {} samples, {} signals, {} events".format(len(samples), len(signals), len(events)))


def _write_pipeline(today):
    """Dates relative to today, so the to do list has something in it."""
    import yaml
    def when(days):
        return (today + dt.timedelta(days=days)).isoformat()
    data = {"sections": [
        {"key": "under_review", "label": "Under review", "date_role": "submitted",
         "note": "Out for review, waiting on a decision.", "items": [
            {"text": "Survey Analysis", "venue": "Journal of Examples",
             "date": when(-70), "status": "under_review"},
            {"text": "Thesis Chapter Three", "venue": "Example Review",
             "date": when(-40), "status": "under_review"}]},
        {"key": "to_submit", "label": "In preparation", "date_role": "deadline",
         "note": "Written, not out the door yet.", "items": [
            {"text": "Grant Proposal", "venue": "Funding call", "date": when(9),
             "status": "in_prep"},
            {"text": "Conference abstract", "venue": "Annual meeting", "date": when(21),
             "status": "in_prep"}]},
        {"key": "deadlines", "label": "Deadlines", "date_role": "deadline",
         "note": "Everything with a date on it.", "items": [
            {"text": "Peer review due", "date": when(2), "status": "to_be_done"},
            {"text": "Marks submitted", "date": when(5), "status": "to_be_done"},
            {"text": "Ethics renewal", "date": when(-3), "status": "not_done"},
            {"text": "Travel claim", "date": when(12), "status": "to_be_done"},
            {"text": "Seminar slides", "date": when(-9), "status": "done"}]},
    ]}
    path = os.path.join(DATA_DIR, "pipeline.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Demo pipeline.\n")
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)

    good = {"items": [
        {"text": "Survey Analysis accepted", "date": when(-6)},
        {"text": "Grant shortlisted", "date": when(-13)},
        {"text": "Chapter draft finished at last", "date": when(-21)}]}
    with open(os.path.join(DATA_DIR, "goodnews.yaml"), "w", encoding="utf-8") as fh:
        yaml.safe_dump(good, fh, allow_unicode=True, sort_keys=False)


if __name__ == "__main__":
    if "Catlendar" in DATA_DIR and not os.environ.get("CATLENDAR_DATA_DIR"):
        print("Refusing to overwrite your real data folder.")
        print("Run with CATLENDAR_DATA_DIR pointing somewhere throwaway.")
        sys.exit(1)
    run()
