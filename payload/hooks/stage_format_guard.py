#!/usr/bin/env python3
"""
Global PreToolUse hook for Claude Code (matcher: Write|Edit).

A malformed item in the «Не источники» section of <repo>/.claude/stage.md is
silently skipped by source_guard and /stage-allow, so the user would believe a
prohibition works while it does not. This hook blocks a write to any
.claude/stage.md whose result has such an item: each list line in that section
must be «- name: `regex` — reason», names unique, regex valid.
Other sections are free text and are not checked.

Not covered: edits through Bash/PowerShell. Fail-open. Escape: env STAGE_FORMAT_GUARD_OFF=1.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stage_rules as sr  # noqa: E402


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def new_text(tool, ti):
    path = ti.get("file_path") or ""
    if not path.replace("\\", "/").lower().endswith("/.claude/stage.md"):
        return None
    if tool == "Write":
        return ti.get("content") or ""
    if tool == "Edit":
        old = read(path)
        a, b = ti.get("old_string") or "", ti.get("new_string") or ""
        if a not in old:
            return None  # Edit itself will fail; nothing to validate
        return old.replace(a, b) if ti.get("replace_all") else old.replace(a, b, 1)
    return None


def main():
    if os.environ.get("STAGE_FORMAT_GUARD_OFF") == "1":
        return 0
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("hook_event_name") != "PreToolUse":
        return 0
    text = new_text(payload.get("tool_name"), payload.get("tool_input") or {})
    if text is None:
        return 0
    errors = sr.validate(text)
    if not errors:
        return 0
    sys.stderr.buffer.write(("stage.md: раздел «Не источники» не разбирается, хук проверки источников "
                             "такие пункты пропустит:\n" + "\n".join(errors[:10])).encode("utf-8"))
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - fail-open
        sys.exit(0)
