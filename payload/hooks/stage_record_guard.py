#!/usr/bin/env python3
"""
Global Stop hook for Claude Code.

When the user's message states a stage constraint («это сейчас не нужно»,
«не опирайся на X», «замечание преподавателя: …»), stage_context.py stores it as
«pending» in the session state. At the end of the turn this hook checks that
<repo>/.claude/stage.md was modified after that message (file mtime, so any way
of editing counts). If not, it blocks the stop ONCE with a reason: record the
item or say in one line why the phrase is not about the project stage.
The pending mark is cleared on the first check, so the block never repeats.

Skipped: subagents, sessions with /stage-off, projects without stage.md.
Fail-open. Escape hatch: env STAGE_RECORD_GUARD_OFF=1.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stage_rules as sr  # noqa: E402


def main():
    if os.environ.get("STAGE_RECORD_GUARD_OFF") == "1":
        return 0
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("hook_event_name") != "Stop" or payload.get("agent_id"):
        return 0
    session_id = payload.get("session_id") or ""
    st = sr.load_state(session_id)
    pending = st.pop("pending", None)
    if not pending:
        return 0
    sr.save_state(session_id, st)  # one check per user message, never a loop
    root = sr.project_root(payload.get("cwd"))
    if st["off"] or not root or payload.get("stop_hook_active"):
        return 0
    try:
        changed = os.path.getmtime(sr.stage_path(root)) >= float(pending.get("ts", 0))
    except OSError:
        return 0
    if changed:
        return 0
    reason = (f"В сообщении пользователя есть ограничение этапа: «{pending.get('excerpt', '')}». "
              "Если оно про проект — внеси пункт в .claude/stage.md (раздел «Не источники» для путей "
              "или обычной строкой) и покажи его. Если нет — одной строкой скажи, почему это не про этап проекта.")
    sys.stdout.buffer.write(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - fail-open
        sys.exit(0)
