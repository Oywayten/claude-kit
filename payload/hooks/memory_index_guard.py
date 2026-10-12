#!/usr/bin/env python3
"""
Global Stop hook for Claude Code.

A memory note that is not listed in MEMORY.md is never loaded into context,
and an index line that points to a missing file is noise. At the end of a turn
this hook (only in a turn that touched the memory directory) compares the *.md notes in the project memory directory
(~/.claude/projects/<project>/memory) with the links `](file.md)` in MEMORY.md
and blocks the stop ONCE, listing notes without an index line and index
lines without a file.

Skipped: a turn with no Edit/Write/Bash/PowerShell call mentioning the memory
directory (an unreadable transcript counts as touched), subagents, a repeated stop in the same turn, a project without a
memory directory or without MEMORY.md. Fail-open.
Escape hatch: env MEMORY_INDEX_GUARD_OFF=1.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory_cleanup_reminder import memory_dir, notes  # noqa: E402

LINK = re.compile(r"\]\(([^)\s]+\.md)\)")


WRITE_TOOLS = {"Edit", "Write", "Bash", "PowerShell"}


def norm(s):
    return re.sub(r"/+", "/", str(s).replace("\\", "/")).lower()


def is_prompt(o, content):
    if o.get("type") != "user" or o.get("isMeta"):
        return False
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        kinds = {c.get("type") for c in content if isinstance(c, dict)}
        return "text" in kinds and "tool_result" not in kinds
    return False


def touched_memory(transcript_path, mdir):
    """True if the turn after the last real user prompt used a write-capable tool on the memory dir."""
    key = "/".join(norm(mdir).rstrip("/").split("/")[-3:])
    hit = False
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("isSidechain"):
                    continue
                content = (o.get("message") or {}).get("content")
                if is_prompt(o, content):
                    hit = False
                elif o.get("type") == "assistant" and isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") in WRITE_TOOLS:
                            args = " ".join(str(v) for v in (c.get("input") or {}).values())
                            if key in norm(args):
                                hit = True
    except OSError:
        return True
    return hit


def check(mdir):
    with open(os.path.join(mdir, "MEMORY.md"), encoding="utf-8") as f:
        linked = set(LINK.findall(f.read()))
    files = set(notes(mdir))
    return sorted(files - linked), sorted(linked - files)


def main():
    if os.environ.get("MEMORY_INDEX_GUARD_OFF") == "1":
        return 0
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("hook_event_name") != "Stop" or payload.get("agent_id") or payload.get("stop_hook_active"):
        return 0
    mdir = memory_dir(payload.get("cwd"))
    if not os.path.isfile(os.path.join(mdir, "MEMORY.md")):
        return 0
    tp = payload.get("transcript_path")
    if tp and not touched_memory(tp, mdir):
        return 0
    unlisted, dangling = check(mdir)
    if not unlisted and not dangling:
        return 0
    parts = []
    if unlisted:
        parts.append("заметки без строки в MEMORY.md: " + ", ".join(unlisted[:15]))
    if dangling:
        parts.append("строки MEMORY.md без файла: " + ", ".join(dangling[:15]))
    reason = ("Индекс памяти не сходится с файлами (" + mdir + "): " + "; ".join(parts)
              + ". Добавь недостающие строки (формат «- [Название](файл.md) — суть», до 150 символов) "
              "или убери строки на несуществующие файлы и скажи об этом пользователю одной строкой.")
    sys.stdout.buffer.write(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - fail-open
        sys.exit(0)
