#!/usr/bin/env python3
"""
Global SessionStart hook for Claude Code + state command for the /memory-cleanup skill.

The project memory (~/.claude/projects/<project>/memory) grows every session;
the MEMORY.md index is loaded each time, so duplicates and stale notes cost
tokens and hide the notes that matter. At session start this hook adds a
reminder when a cleanup is due: DAYS since the last cleanup, or GROWTH new
notes since then. The reminder asks to OFFER the cleanup to the user; nothing
is deleted without the user's OK (that is the skill's job).

State: <memory dir>/.cleanup_state.json {"last": ISO time, "files": count}.
No state yet = never cleaned = due.

CLI (used by the skill):
  memory_cleanup_reminder.py --status [--cwd DIR]
  memory_cleanup_reminder.py --done   [--cwd DIR]   # record a finished cleanup
Fail-open. Escape hatch: env MEMORY_CLEANUP_REMINDER_OFF=1.
"""
import datetime as dt
import json
import os
import re
import sys

DAYS = 7
GROWTH = 10


def memory_dir(cwd):
    name = re.sub(r"[:\\/]", "-", os.path.abspath(cwd or "."))
    return os.path.join(os.path.expanduser("~/.claude/projects"), name, "memory")


def notes(mdir):
    try:
        return [f for f in os.listdir(mdir) if f.endswith(".md") and f != "MEMORY.md"]
    except OSError:
        return []


def state(mdir):
    try:
        with open(os.path.join(mdir, ".cleanup_state.json"), encoding="utf-8") as f:
            s = json.load(f)
        return dt.datetime.fromisoformat(s["last"]), int(s["files"])
    except (OSError, ValueError, KeyError, TypeError):
        return None, 0


def status(cwd, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    mdir = memory_dir(cwd)
    count = len(notes(mdir))
    last, files_then = state(mdir)
    days = None if last is None else (now - last).days
    due = count > 0 and (last is None or days >= DAYS or count - files_then >= GROWTH)
    return {"dir": mdir, "notes": count, "last": last.isoformat() if last else None,
            "days": days, "growth": count - files_then if last else None, "due": due}


def done(cwd):
    mdir = memory_dir(cwd)
    os.makedirs(mdir, exist_ok=True)
    with open(os.path.join(mdir, ".cleanup_state.json"), "w", encoding="utf-8") as f:
        json.dump({"last": dt.datetime.now(dt.timezone.utc).isoformat(), "files": len(notes(mdir))}, f)
    return status(cwd)


def main(argv):
    if "--status" in argv or "--done" in argv:
        cwd = argv[argv.index("--cwd") + 1] if "--cwd" in argv else os.getcwd()
        res = done(cwd) if "--done" in argv else status(cwd)
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(res, ensure_ascii=False))
        return 0
    if os.environ.get("MEMORY_CLEANUP_REMINDER_OFF") == "1":
        return 0
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("hook_event_name") != "SessionStart":
        return 0
    s = status(payload.get("cwd"))
    if not s["due"]:
        return 0
    why = "чистки ещё не было" if s["last"] is None else f"последняя чистка {s['days']} дн. назад, с тех пор +{s['growth']} заметок"
    note = (f"Память проекта: {s['notes']} заметок, {why}. Когда закончишь первое дело сессии, предложи пользователю "
            "одной строкой провести чистку памяти командой /memory-cleanup. Сам ничего не удаляй.")
    out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": note}}
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:  # noqa: BLE001 - fail-open
        sys.exit(0)
