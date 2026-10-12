#!/usr/bin/env python3
"""
Global PostToolUse hook for Claude Code (matcher: Edit|Write): keep memory notes short.

Memory notes are read whole when a topic comes up, and the MEMORY.md index is loaded into every
session. Long notes and long index lines cost tokens on every turn / every read. After an edit of a
file in ~/.claude/projects/<project>/memory/ this hook warns (never blocks) when:
  - a MEMORY.md line is longer than INDEX_LINE chars;
  - a note body is longer than the limit for its type (status/feedback: short, reference: longer);
  - a note description is longer than DESCRIPTION chars.
The warning goes to the model as context: shorten, keep «статус, а не история».

Fail-open. Escape hatch: env MEMORY_SIZE_GUARD_OFF=1.
"""
import json
import os
import re
import sys

INDEX_LINE = 150
DESCRIPTION = 220
LIMITS = {"project": 50, "feedback": 40, "user": 40, "reference": 90}
DEFAULT_LIMIT = 50
MEMORY_FILE = re.compile(r"[/\\]\.claude[/\\]projects[/\\][^/\\]+[/\\]memory[/\\][^/\\]+\.md$")


def split_note(text):
    """(front matter lines, body lines)."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return lines[1:i], lines[i + 1:]
    return [], lines


def field(front, key):
    for line in front:
        m = re.match(r"^\s*" + key + r":\s*(.*?)\s*$", line)
        if m:
            return m.group(1).strip().strip('"')
    return ""


def check(path, text):
    notes = []
    name = os.path.basename(path)
    if name == "MEMORY.md":
        long_lines = [n for n, line in enumerate(text.splitlines(), 1) if len(line) > INDEX_LINE]
        if long_lines:
            notes.append(f"в индексе MEMORY.md строки длиннее {INDEX_LINE} символов: {', '.join(map(str, long_lines[:6]))}. "
                         "Индекс грузится в каждую сессию: описание одной короткой фразой.")
        return notes
    front, body = split_note(text)
    kind = field(front, "type") or "project"
    limit = LIMITS.get(kind, DEFAULT_LIMIT)
    n = len([ln for ln in body if ln.strip()])
    if n > limit:
        notes.append(f"заметка {name} (тип {kind}) занимает {n} строк при лимите {limit}. "
                     "Оставь текущее состояние и факты, историю сожми или вынеси; факт в одну строку.")
    desc = field(front, "description")
    if len(desc) > DESCRIPTION:
        notes.append(f"описание заметки {name} длиннее {DESCRIPTION} символов ({len(desc)}): сократи до одной фразы.")
    return notes


def main():
    if os.environ.get("MEMORY_SIZE_GUARD_OFF") == "1":
        return 0
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("hook_event_name") != "PostToolUse" or payload.get("tool_name") not in ("Edit", "Write"):
        return 0
    path = (payload.get("tool_input") or {}).get("file_path") or ""
    if not MEMORY_FILE.search(path):
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return 0
    notes = check(path, text)
    if not notes:
        return 0
    msg = "Размер памяти: " + " ".join(notes)
    out = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - fail-open
        sys.exit(0)
