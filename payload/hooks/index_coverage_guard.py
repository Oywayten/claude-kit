#!/usr/bin/env python3
"""
Global Stop hook for Claude Code.

An index of sources goes stale the moment a new lesson/review file is saved
without an entry. At the end of a turn this hook compares the .md files under
<repo>/tasks_src (except backup/ folders and INDEX.md itself) with
<repo>/tasks_src/INDEX.md and blocks the stop ONCE, listing files missing from it.

A file counts as listed if INDEX.md contains, in backticks:
- its path relative to tasks_src (`m3_device_collector/theory.md`), or
- a glob matching that path (`m1_arch_practice/invariants*.md`), or
- its folder ending with / (`m2_lessons/`), or
- its file name on a line that also names its folder (`audit-evolution.md` next to `m3_device_collector/...`).
Files that should stay out of the index: list them (or a glob) under a heading
«## Вне указателя» in INDEX.md.

Active only when tasks_src/INDEX.md exists. Skipped: subagents, a repeated stop
in the same turn. Fail-open. Escape hatch: env INDEX_GUARD_OFF=1.
"""
import fnmatch
import json
import os
import re
import sys

TOKEN = re.compile(r"`([^`\n]+)`")


def project_root(start):
    d = os.path.abspath(start or ".")
    while True:
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def files_under(base):
    out = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d != "backup" and not d.startswith(".")]
        for f in files:
            if f.lower().endswith(".md"):
                rel = os.path.relpath(os.path.join(root, f), base).replace("\\", "/")
                if rel != "INDEX.md":
                    out.append(rel)
    return sorted(out)


def listed(rel, index_text):
    lines = index_text.splitlines()
    tokens = [t.strip().lstrip("./") for t in TOKEN.findall(index_text)]
    folder, name = os.path.split(rel)
    for t in tokens:
        if t == rel or (("*" in t or "?" in t) and fnmatch.fnmatch(rel, t)):
            return True
        if t.endswith("/") and rel.startswith(t):
            return True
    if folder:
        for line in lines:
            line_tokens = TOKEN.findall(line)
            if name in line_tokens and any(x.startswith(folder + "/") or x == folder for x in line_tokens):
                return True
    return False


def missing(root):
    base = os.path.join(root, "tasks_src")
    with open(os.path.join(base, "INDEX.md"), encoding="utf-8") as f:
        text = f.read()
    return [rel for rel in files_under(base) if not listed(rel, text)]


def main():
    if os.environ.get("INDEX_GUARD_OFF") == "1":
        return 0
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("hook_event_name") != "Stop" or payload.get("agent_id") or payload.get("stop_hook_active"):
        return 0
    root = project_root(payload.get("cwd"))
    if not root or not os.path.isfile(os.path.join(root, "tasks_src", "INDEX.md")):
        return 0
    lost = missing(root)
    if not lost:
        return 0
    reason = ("Файлы в tasks_src не внесены в указатель tasks_src/INDEX.md: " + ", ".join(lost[:15])
              + ". Добавь их в указатель (или в раздел «## Вне указателя», если им там не место) и скажи об этом пользователю одной строкой.")
    sys.stdout.buffer.write(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - fail-open
        sys.exit(0)
