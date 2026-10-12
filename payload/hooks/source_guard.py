#!/usr/bin/env python3
"""
Global PreToolUse hook for Claude Code (matcher: Bash|PowerShell|Read|Grep|Glob).

Some sources must not drive work at the current stage: a future module, a
speculative branch with a possible result, unapproved drafts. Memory rules about
this were broken repeatedly, so this hook asks the user before such a source is
read. It does not deny: comparing with such a source can be legitimate when the
user asked for it.

Config (per project): section «## Не источники…» of <repo>/.claude/stage.md (the
same file the stage_context hook injects), parsed by stage_rules.py:
    - <name>: `<python regex>` — <reason shown to the user>
No file or no section -> the hook does nothing. Items the user allowed in this
session with /stage-allow <name> are skipped.
The regex is matched against the Bash/PowerShell command, the Read file_path,
or the Grep/Glob path + pattern + glob.

Fail-open on any internal error. Escape hatch: env SOURCE_GUARD_OFF=1.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stage_rules as sr  # noqa: E402


def subject(tool, ti):
    if tool in ("Bash", "PowerShell"):
        return ti.get("command") or ""
    if tool == "Read":
        return ti.get("file_path") or ""
    if tool in ("Grep", "Glob"):
        return " ".join(str(ti.get(k) or "") for k in ("path", "pattern", "glob"))
    return ""


def main():
    if os.environ.get("SOURCE_GUARD_OFF") == "1":
        return 0
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("hook_event_name") != "PreToolUse":
        return 0
    root = sr.project_root(payload.get("cwd"))
    if not root:
        return 0
    text = subject(payload.get("tool_name"), payload.get("tool_input") or {})
    allowed = set(sr.load_state(payload.get("session_id") or "")["allow"])
    hits = [reason for name, rx, reason in sr.rules(root)
            if rx is not None and name not in allowed and rx.search(text.replace("\\", "/"))]
    if not hits:
        return 0
    reason = "Источник не для текущего этапа: " + "; ".join(dict.fromkeys(hits)) + ". Разрешить чтение?"
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                  "permissionDecisionReason": reason}}
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - fail-open, a broken guard must not stop work
        sys.exit(0)
