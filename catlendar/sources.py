"""Two signals that know far more than a window title does: what you asked
Claude, and which files you actually changed."""
import json
import logging
import os
import re
import subprocess
import threading
import time

from . import config, db
from .paths import DATA_DIR

log = logging.getLogger("catlendar.sources")

HOME = os.path.expanduser("~")
CLAUDE_ROOT = os.path.join(HOME, ".claude", "projects")
SPAN_GAP = 600      # messages closer than this belong to one stretch of work


def watch_folders():
    """Folders to watch for saved files. Set `watch_folders` in projects.yaml;
    otherwise guess at the usual places."""
    configured = config.load()["settings"].get("watch_folders")
    if configured:
        return [os.path.expanduser(p) for p in configured]
    guesses = ["Documents", "Dropbox", "Code", "Projects",
               os.path.join("OneDrive", "Documents")]
    return [os.path.join(HOME, g) for g in guesses if os.path.isdir(os.path.join(HOME, g))]
SKIP_PARTS = (
    "/.git/", "/.venv/", "/node_modules/", "/.dropbox", "/.cache/", "/__pycache__/",
    "/.DS_Store", "/Icon\r", "/.Trash/", "/.claude/", "/.tmp",
)
SKIP_NAMES = (".DS_Store", "Icon\r", "desktop.ini")
MAX_PROMPT = 280


# ------------------------------------------------------------------- Claude
def _prompt_text(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content
                 if isinstance(c, dict) and c.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return ""


IMAGE_MARKER = re.compile(r"\[Image:[^\]]*\]\s*")


def _is_human_prompt(rec):
    """Only what the user actually typed. Skill payloads, pasted images, command
    output and compaction notices all carry isMeta or lack a human origin."""
    if rec.get("type") != "user" or rec.get("isSidechain") or rec.get("isMeta"):
        return False
    if (rec.get("origin") or {}).get("kind") != "human":
        return False
    if rec.get("promptSource") not in (None, "sdk", "cli", "user"):
        return False
    text = clean_prompt(_prompt_text(rec.get("message") or {}))
    if len(text) < 4:
        return False
    if text.lstrip().startswith(("<", "Caveat:", "[Request interrupted")):
        return False
    return True


def clean_prompt(text):
    return IMAGE_MARKER.sub("", text or "").strip()


def scan_claude(max_age_hours=36, limit=400):
    """Read new lines from recently touched transcripts and turn them into spans
    of working time. A prompt is an instant, but the work is the whole exchange,
    so every message timestamp counts and messages less than SPAN_GAP apart are
    one stretch."""
    if not os.path.isdir(CLAUDE_ROOT):
        return 0
    cutoff = time.time() - max_age_hours * 3600
    offsets = json.loads(db.get_meta("claude_offsets", "{}"))
    sessions = json.loads(db.get_meta("claude_sessions", "{}"))
    rows, touched = [], {}

    for project_dir in os.listdir(CLAUDE_ROOT):
        full_dir = os.path.join(CLAUDE_ROOT, project_dir)
        if not os.path.isdir(full_dir):
            continue
        for name in os.listdir(full_dir):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(full_dir, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            if stat.st_mtime < cutoff:
                continue
            start = offsets.get(path, 0)
            if start > stat.st_size:
                start = 0
            if start == stat.st_size:
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(start)
                    chunk = fh.read()
                    touched[path] = fh.tell()
            except OSError:
                continue

            marks = []     # (ts, prompt_text or None)
            cwd = ""
            for line in chunk.splitlines():
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("cwd"):
                    cwd = rec["cwd"]
                if rec.get("type") not in ("user", "assistant") or rec.get("isSidechain"):
                    continue
                ts = _iso_to_epoch(rec.get("timestamp"))
                if not ts:
                    continue
                text = clean_prompt(_prompt_text(rec.get("message") or {})) \
                    if _is_human_prompt(rec) else None
                marks.append((ts, text))

            # A session is normally about one thing, so let the whole file vote:
            # a span with no project of its own inherits the session's.
            prompts = [t for _, t in marks if t]
            session_project = (config.classify_path(cwd)
                               or config.classify(None, " ".join(prompts))[0]
                               or sessions.get(path))
            if session_project:
                sessions[path] = session_project
            rows.extend(_spans(marks, cwd, session_project))

    offsets.update(touched)
    if len(offsets) > 4000:
        offsets = dict(sorted(offsets.items(), key=lambda kv: -kv[1])[:2000])
    db.set_meta("claude_offsets", json.dumps(offsets))
    if len(sessions) > 4000:
        sessions = dict(list(sessions.items())[-2000:])
    db.set_meta("claude_sessions", json.dumps(sessions))
    rows.sort(key=lambda r: r["ts"])
    return db.insert_signals(rows[-limit:])


def _spans(marks, cwd, session_project=None):
    """Group message timestamps into stretches of work."""
    if not marks:
        return []
    marks.sort(key=lambda m: m[0])
    out, run = [], [marks[0]]
    for mark in marks[1:]:
        if mark[0] - run[-1][0] <= SPAN_GAP:
            run.append(mark)
        else:
            out.append(run)
            run = [mark]
    out.append(run)

    rows = []
    for run in out:
        prompts = [t for _, t in run if t]
        text = prompts[0] if prompts else "(working with Claude)"
        project = (config.classify_path(cwd)
                   or config.classify(None, " ".join(prompts[:4]))[0]
                   or session_project)
        rows.append({
            "ts": run[0][0],
            "end_ts": max(run[-1][0], run[0][0] + 60),   # a lone message still means a minute
            "kind": "claude",
            "project": project,
            "detail": text[:MAX_PROMPT].replace("\n", " "),
            "path": cwd,
        })
    return rows


def _iso_to_epoch(value):
    if not value:
        return None
    try:
        import datetime as dt
        text = value.replace("Z", "+00:00")
        return int(dt.datetime.fromisoformat(text).timestamp())
    except Exception:
        return None


# -------------------------------------------------------------- saved files
def scan_files(limit=400):
    """Files saved since the last scan. `find` does this in seconds on Unix;
    Windows gets a pruned walk instead."""
    since = int(db.get_meta("files_cursor", 0) or 0)
    now = int(time.time())
    if since == 0:
        since = now - 6 * 3600

    rows = []
    for root in watch_folders():
        if not os.path.isdir(root):
            continue
        for path in _changed_since(root, since):
            base = os.path.basename(path)
            if base in SKIP_NAMES or base.startswith("~$") or base.startswith("."):
                continue
            if any(part in path for part in SKIP_PARTS):
                continue
            try:
                mtime = int(os.stat(path).st_mtime)
            except OSError:
                continue
            project = config.classify_path(path) or config.classify(None, base)[0]
            rows.append({"ts": mtime, "end_ts": None, "kind": "file", "project": project,
                         "detail": base[:MAX_PROMPT], "path": path})
    db.set_meta("files_cursor", now)
    rows.sort(key=lambda r: r["ts"])
    return db.insert_signals(rows[-limit:])


# kept under the old name so existing scripts and docs still work
scan_dropbox = scan_files


def _changed_since(root, since):
    if os.name != "nt":
        yield from _changed_since_find(root, since)
    else:
        yield from _changed_since_walk(root, since)


def _changed_since_find(root, since):
    """BSD find cannot parse an @epoch argument, so compare against a stamp file
    whose modification time we set ourselves."""
    stamp = os.path.join(DATA_DIR, ".files_stamp")
    with open(stamp, "a"):
        pass
    os.utime(stamp, (since, since))
    try:
        res = subprocess.run(
            ["find", root, "-type", "f", "-newer", stamp, "-not", "-path", "*/.*",
             "-not", "-path", "*/node_modules/*", "-not", "-path", "*/.venv/*"],
            capture_output=True, text=True, timeout=180)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        log.warning("file scan timed out or find is missing under %s", root)
        return
    if res.returncode not in (0, 1):
        log.warning("file scan failed under %s: %s", root, res.stderr.strip()[:160])
        return
    for line in res.stdout.splitlines():
        yield line


PRUNE = {"node_modules", ".venv", ".git", "__pycache__", "AppData", "Library"}


def _changed_since_walk(root, since, budget=120):
    """Pure Python fallback. Prunes the directories that hold the most files and
    the least meaning, and gives up rather than grinding forever."""
    deadline = time.time() + budget
    stack = [root]
    while stack:
        if time.time() > deadline:
            log.warning("file scan gave up early under %s", root)
            return
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    name = entry.name
                    if name.startswith(".") or name in PRUNE:
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.stat(follow_symlinks=False).st_mtime > since:
                            yield entry.path
                    except OSError:
                        continue
        except (PermissionError, OSError):
            continue


class SignalScanner(threading.Thread):
    """Two scans on very different budgets.

    Reading new lines from Claude transcripts costs milliseconds, so it runs
    often. The file scan walks whole folder trees and can take half a minute
    whether or not anything changed, so it runs rarely. Polling it every few
    minutes would keep the disk busy a noticeable fraction of the time.
    """

    daemon = True

    def __init__(self, interval=180, files_interval=900):
        super().__init__(name="catlendar-signals")
        self._stop = threading.Event()
        self.interval = interval
        self.files_interval = files_interval
        self._last_files = 0.0

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                added = scan_claude()
                if added:
                    log.info("claude signals: +%d", added)
            except Exception:
                log.exception("claude scan failed")

            if time.time() - self._last_files >= self.files_interval:
                self._last_files = time.time()
                started = time.time()
                try:
                    added = scan_dropbox()
                    log.info("dropbox scan: +%d in %.0fs", added, time.time() - started)
                except Exception:
                    log.exception("dropbox scan failed")

            self._stop.wait(self.interval)
