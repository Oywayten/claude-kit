#!/usr/bin/env python3
"""
Global PreToolUse + Stop hook for Claude Code.

Two skills must be active in every session: anthropic-skills:oh-my-god
(Evidence Rule) and caveman with argument ultra. The SessionStart hook only
injects a text reminder; after compaction that reminder was ignored and the
session ran without both skills. This hook enforces it.

Rule: since the last `compact_boundary` record in the transcript (or since
session start when there is none), the transcript must contain a Skill
tool_use for each skill. Until then:
- PreToolUse: every tool except Skill and ToolSearch is blocked (exit 2).
- Stop: the turn cannot end (decision=block), so a text-only reply is caught.

Subagents are not checked (they cannot inherit the main session's skills).
A Skill call whose result is an error does not count as loading. After two failed calls of the same
skill the guard lets the session through and shows a warning in the user's terminal at Stop
(systemMessage), since the session then runs without that skill's rules.
Escape hatch: env SKILLS_GUARD_OFF=1.
"""
import json
import mmap
import os
import sys

BOUNDARY = b'"subtype":"compact_boundary"'
REQUIRED = {
    "anthropic-skills:oh-my-god": lambda inp: inp.get("skill") in ("anthropic-skills:oh-my-god", "oh-my-god"),
    "caveman ultra": lambda inp: inp.get("skill") == "caveman" and str(inp.get("args", "")).strip() == "ultra",
}
ALWAYS_ALLOWED_TOOLS = {"Skill", "ToolSearch"}
MAX_FAILED_CALLS = 2  # a skill whose call failed this many times is let through with a warning


def analyze(transcript_path: str):
    """Return (missing, given_up): skills still to load, and skills whose calls kept failing."""
    try:
        with open(transcript_path, "rb") as f:
            if os.fstat(f.fileno()).st_size == 0:
                return list(REQUIRED), []
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
                start = data.rfind(BOUNDARY)
                start = 0 if start < 0 else start
                region = data[start:]
    except OSError:
        return [], []  # no readable transcript: do not block blindly
    uses = []  # (tool_use_id, skill key)
    failed_ids = set()
    for line in region.split(b"\n"):
        if b'"name":"Skill"' not in line and b'"is_error":true' not in line:
            continue
        try:
            content = (json.loads(line).get("message") or {}).get("content")
        except ValueError:
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "Skill":
                inp = block.get("input") or {}
                uses += [(block.get("id"), key) for key, match in REQUIRED.items() if match(inp)]
            elif block.get("type") == "tool_result" and block.get("is_error"):
                failed_ids.add(block.get("tool_use_id"))
    missing, given_up = [], []
    for key in REQUIRED:
        calls = [tool_id for tool_id, k in uses if k == key]
        failures = sum(1 for tool_id in calls if tool_id is not None and tool_id in failed_ids)
        if failures < len(calls):
            continue  # at least one call without a failed result (no result yet counts as loaded)
        (given_up if failures >= MAX_FAILED_CALLS else missing).append(key)
    return missing, given_up


def reason(missing):
    return (
        "Скиллы не загружены после старта или сжатия контекста: "
        + ", ".join(missing)
        + ". Сначала вызови инструмент Skill: skill=oh-my-god (или anthropic-skills:oh-my-god); "
        "затем skill=caveman, args=ultra. Только потом остальная работа."
    )


def warning(given_up):
    return (
        "[skills-guard] Скиллы не загрузились (вызов Skill упал "
        + str(MAX_FAILED_CALLS)
        + " раза): "
        + ", ".join(given_up)
        + ". Сессия идёт БЕЗ их правил. Проверь скиллы и перезапусти сессию."
    )


def main():
    if os.environ.get("SKILLS_GUARD_OFF") == "1":
        return 0
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("agent_id"):
        return 0
    transcript = payload.get("transcript_path") or ""
    if not transcript or os.sep + "subagents" + os.sep in transcript or "/subagents/" in transcript:
        return 0

    event = payload.get("hook_event_name")
    if event == "PreToolUse" and payload.get("tool_name") in ALWAYS_ALLOWED_TOOLS:
        return 0

    missing, given_up = analyze(transcript)
    if not missing:
        if given_up and event == "Stop":
            sys.stdout.buffer.write(json.dumps({"systemMessage": warning(given_up)}, ensure_ascii=False).encode("utf-8"))
        return 0

    if event == "Stop":
        if payload.get("stop_hook_active"):
            return 0  # already forced once this turn; avoid an endless loop
        sys.stdout.buffer.write(json.dumps({"decision": "block", "reason": reason(missing)}).encode("utf-8"))
        return 0
    if event == "PreToolUse":
        sys.stderr.buffer.write(reason(missing).encode("utf-8"))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
