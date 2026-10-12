"""Shared by handoff_start.py and handoff_hint.py: threads of work («потоки») kept as small files.

A thread file lives in <repo>/.claude/handoff/<name>.md (project) or ~/.claude/handoff/<name>.md (any project):

    ---
    name: video-pipeline
    description: one line: what the thread is
    keywords: слово, другое слово, WEEK
    status: active            (active | closed)
    updated: 2026-10-05
    ---
    ## Статус сейчас
    ## Что сделать   (задача — условие запуска)
    ## Где лежит

Only the header is read by the hooks; the body is read by the model when the topic comes up.
"""
import datetime as dt
import os
import re

GLOBAL_DIR = os.path.expanduser("~/.claude/handoff")
STALE_DAYS = 14
KV = re.compile(r"^([A-Za-z_]+):\s*(.*?)\s*$")


def project_root(start):
    d = os.path.abspath(start or ".")
    while True:
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def header(path):
    """Front matter of a thread file as a dict, or None if the file has none."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read(4000).splitlines()
    except OSError:
        return None
    if not lines or lines[0].strip() != "---":
        return None
    out = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return out
        m = KV.match(line)
        if m:
            out[m.group(1).lower()] = m.group(2)
    return None


def threads(cwd):
    """All threads visible from cwd: list of dicts with name, description, keywords, status, updated, path."""
    dirs = []
    root = project_root(cwd)
    if root:
        dirs.append(os.path.join(root, ".claude", "handoff"))
    dirs.append(GLOBAL_DIR)
    out = []
    for d in dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for fn in names:
            if not fn.endswith(".md"):
                continue
            path = os.path.join(d, fn)
            h = header(path)
            if not h:
                continue
            out.append({
                "name": h.get("name") or fn[:-3],
                "description": h.get("description", ""),
                "keywords": [k.strip().lower() for k in h.get("keywords", "").split(",") if k.strip()],
                "status": (h.get("status") or "active").lower(),
                "updated": h.get("updated", ""),
                "path": path,
            })
    return out


def active(cwd):
    return [t for t in threads(cwd) if t["status"] != "closed"]


def age_days(updated, today=None):
    try:
        d = dt.date.fromisoformat(updated)
    except ValueError:
        return None
    return ((today or dt.date.today()) - d).days
