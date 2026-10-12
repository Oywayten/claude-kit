#!/usr/bin/env python3
"""
Global SessionStart hook for Claude Code: list of active work threads.

Work state («что запущено, что ждёт решения, что делать дальше») is kept per topic in
<repo>/.claude/handoff/<name>.md (see handoff_lib.py). At session start (startup, resume,
compaction) this hook adds only the header of each ACTIVE thread: name, description, date,
path. Bodies are not loaded: the model opens the file of the topic that comes up.
Closed threads are not listed. Threads not updated for 14+ days are marked.

Fail-open. Escape hatch: env HANDOFF_OFF=1.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import handoff_lib as hl  # noqa: E402

MAX_CHARS = 1800


def build(cwd):
    items = hl.active(cwd)
    if not items:
        return ""
    lines = ["Потоки работы (активные). Открывай файл потока только когда тема зашла в разговор; остальные не читай. "
             "Работа вне потоков: не заводи поток молча, спроси автора. Обновление: /handoff."]
    for t in sorted(items, key=lambda x: x["updated"], reverse=True):
        age = hl.age_days(t["updated"])
        mark = f", не обновлялся {age} дн." if age is not None and age >= hl.STALE_DAYS else ""
        lines.append(f"- {t['name']} — {t['description']} (обновлён {t['updated'] or '?'}{mark}) → {t['path']}")
    text = "\n".join(lines)
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + "\n… (список обрезан)"


def main():
    if os.environ.get("HANDOFF_OFF") == "1":
        return 0
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("hook_event_name") != "SessionStart":
        return 0
    text = build(payload.get("cwd"))
    if not text:
        return 0
    out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - fail-open
        sys.exit(0)
