"""Builds the project list in projects.yaml from your folder tree, so the rules
match the way you already file your work instead of a guess.

Point `project_roots` in projects.yaml at the folders that hold one folder per
project, give each a kind, and re-run. Keywords you add by hand are kept.
"""
import os
import re
import sys

import yaml

from . import config
from .paths import CONFIG_PATH, DEFAULT_CONFIG_PATH

STOPWORDS = {
    "the", "a", "an", "of", "and", "for", "in", "on", "with", "to", "study", "studies",
    "project", "projects", "new", "old", "misc", "stuff", "temp", "archive", "templates",
    "from", "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
}


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]


def keywords_for(folder):
    """The folder name, plus a shorter form with the filler words removed."""
    name = folder.lower()
    words = [w for w in re.split(r"[^a-z0-9+]+", name)
             if w and w not in STOPWORDS and not re.fullmatch(r"(19|20)\d\d", w)]
    out = {name}
    tag = re.match(r"^([a-z]{1,3}\d{1,3})\b", name)      # a code such as p10 or proj7
    if tag:
        out.add(tag.group(1))
        rest = " ".join(words[1:])
        if len(rest) >= 6:
            out.add(rest)
    elif len(" ".join(words)) >= 6:
        out.add(" ".join(words))
    return sorted(k for k in out if len(k) >= 3)


def roots():
    """[(path, kind)] from the config, expanded and filtered to what exists."""
    out = []
    for entry in config.load()["settings"].get("project_roots") or []:
        if isinstance(entry, dict):
            path, kind = entry.get("path"), entry.get("kind", "research")
        else:
            path, kind = entry, "research"
        if not path:
            continue
        full = os.path.expanduser(str(path))
        if os.path.isdir(full):
            out.append((full, str(kind)))
    return out


def discover():
    projects, seen = [], set()
    for root, kind in roots():
        for entry in sorted(os.listdir(root)):
            full = os.path.join(root, entry)
            if not os.path.isdir(full) or entry.startswith(".") or entry.startswith("Icon"):
                continue
            key = slug(entry)
            if not key or key in seen:
                continue
            seen.add(key)
            projects.append({
                "key": key,
                "label": entry,
                "kind": kind,
                "paths": [os.path.join(os.path.basename(root), entry)],
                "keywords": keywords_for(entry),
            })
    return projects


def render(projects):
    lines, by_kind = [], {}
    for p in projects:
        by_kind.setdefault(p["kind"], []).append(p)
    for kind in sorted(by_kind):
        lines.append("\n  # ---- {} ----".format(kind))
        for p in by_kind[kind]:
            lines.append("  - key: {}".format(p["key"]))
            lines.append('    label: "{}"'.format(str(p["label"]).replace('"', "'")))
            lines.append("    kind: {}".format(p["kind"]))
            lines.append("    paths: [{}]".format(", ".join('"{}"'.format(x) for x in p["paths"])))
            lines.append("    keywords: [{}]".format(", ".join('"{}"'.format(k) for k in p["keywords"])))
    return "\n".join(lines)


def run(target=None):
    target = target or CONFIG_PATH
    source = target if os.path.exists(target) else DEFAULT_CONFIG_PATH
    with open(source, "r", encoding="utf-8") as fh:
        text = fh.read()
    existing = yaml.safe_load(text) or {}
    manual = {p.get("key"): p for p in (existing.get("projects") or [])}

    projects = discover()
    for p in projects:                          # keep anything you added by hand
        prev = manual.get(p["key"])
        if prev:
            p["keywords"] = sorted(set(p["keywords"]) | set(prev.get("keywords") or []))
            p["paths"] = sorted(set(p["paths"]) | set(prev.get("paths") or []))
    known = {p["key"] for p in projects}
    for key, prev in manual.items():
        # hand written projects survive, as long as they are in the current
        # format. An entry with no kind is from an older file and is dropped.
        if key not in known and prev.get("keywords") and prev.get("kind"):
            prev.setdefault("paths", [])
            projects.append(prev)

    head = text.split("projects:")[0].rstrip()
    body = "projects:" + render(projects) + "\n"
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(head + "\n\n" + body)
    return len(projects), target


if __name__ == "__main__":
    count, path = run(sys.argv[1] if len(sys.argv) > 1 else None)
    print("wrote {} projects to {}".format(count, path))
