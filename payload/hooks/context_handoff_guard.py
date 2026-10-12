#!/usr/bin/env python3
"""
Global Stop hook for Claude Code: warn once before auto-compaction.

Auto-compaction has no model turn, so a skill cannot run "right before" it. A Stop hook that blocks
does force a live turn: when the last assistant turn's context is at or above the threshold, the hook
blocks the stop once and tells the model to save the work threads (skills/handoff/SKILL.md; the skill
itself is manual and cannot be called with the Skill tool).

Context size = input + cache write + cache read of the last assistant record in the transcript.
Once per crossing without any state file: the block reason is recorded in the transcript as a
`hook_blocking_error` record carrying MARKER; a warning counts only since the last `compact_boundary`,
so the next crossing after a compaction warns again.

The hook adds nothing to the context while the size is below the threshold or already warned.
Env: HANDOFF_GUARD_OFF=1 disables; HANDOFF_GUARD_TOKENS overrides the threshold (default 150000).
"""
import json
import mmap
import os
import sys

BOUNDARY = b'"subtype":"compact_boundary"'
MARKER = b"[handoff-guard]"
ERROR_RECORD = b'"type":"hook_blocking_error"'  # quotes unescaped: code or text quoting the record has them escaped
DEFAULT_THRESHOLD = 150_000
START_WINDOW = 256 * 1024
MAX_WINDOW = 64 * 1024 * 1024
USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def last_context_tokens(path):
    """Context size of the last assistant turn, or None when it cannot be read."""
    try:
        size = os.path.getsize(path)
        window = START_WINDOW
        with open(path, "rb") as f:
            while True:
                start = max(0, size - window)
                f.seek(start)
                lines = f.read(size - start).split(b"\n")
                for line in reversed(lines):  # a record cut by the window start fails json.loads and is skipped
                    if b'"type":"assistant"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    usage = (rec.get("message") or {}).get("usage")
                    if rec.get("type") == "assistant" and usage:
                        return sum(usage.get(k) or 0 for k in USAGE_KEYS)
                if start == 0 or window >= MAX_WINDOW:
                    return None
                window *= 4
    except OSError:
        return None


def warned_since_boundary(path):
    """True when a hook_blocking_error record with MARKER exists after the last compact_boundary."""
    try:
        with open(path, "rb") as f:
            if os.fstat(f.fileno()).st_size == 0:
                return False
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
                start = data.rfind(BOUNDARY)
                region = data[0 if start < 0 else start:]
    except OSError:
        return True  # unreadable: do not block blindly
    pos = region.find(MARKER)
    while pos >= 0:
        line_start = region.rfind(b"\n", 0, pos) + 1
        line_end = region.find(b"\n", pos)
        line = region[line_start:] if line_end < 0 else region[line_start:line_end]
        if ERROR_RECORD in line:
            return True
        pos = region.find(MARKER, pos + len(MARKER))
    return False


def threshold():
    try:
        return int(os.environ.get("HANDOFF_GUARD_TOKENS") or DEFAULT_THRESHOLD)
    except ValueError:
        return DEFAULT_THRESHOLD


def reason(tokens):
    return (
        MARKER.decode() + f" Контекст сессии около {tokens // 1000}k токенов, автосжатие близко (около 200k). "
        "Сохрани состояние потоков до сжатия: прочти ~/.claude/skills/handoff/SKILL.md и выполни его шаги "
        "(инструментом Skill этот скилл не вызвать, он ручной). Потом закончи ход. "
        "До следующего сжатия напоминать не буду."
    )


def main():
    if os.environ.get("HANDOFF_GUARD_OFF") == "1":
        return 0
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("hook_event_name") != "Stop" or payload.get("agent_id") or payload.get("stop_hook_active"):
        return 0
    transcript = payload.get("transcript_path") or ""
    if not transcript or os.sep + "subagents" + os.sep in transcript or "/subagents/" in transcript:
        return 0
    tokens = last_context_tokens(transcript)
    if tokens is None or tokens < threshold():
        return 0
    if warned_since_boundary(transcript):
        return 0
    sys.stdout.buffer.write(json.dumps({"decision": "block", "reason": reason(tokens)}, ensure_ascii=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
