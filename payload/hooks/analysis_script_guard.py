#!/usr/bin/env python3
"""
Global PreToolUse hook for Claude Code (matcher: Write|Edit|Bash).

Rules for search/analysis scripts live in the skill `analysis-script-rules`.
A skill loads only when the model remembers it, so this hook makes it
mandatory: writing or editing a script that searches files recursively is
blocked until the skill was loaded after the last compaction.

What counts as such a script:
- Write/Edit of a file with a script extension whose new text (or, for Edit,
  the file on disk plus the new text) contains a recursive search;
- Bash heredoc redirected into a file with a script extension, same test.
One-off commands (plain `grep -r ...` in Bash) are not scripts and pass.

Fail-open: any internal error, missing transcript or unreadable payload
allows the call. Escape hatch: env ANALYSIS_GUARD_OFF=1.
"""
import json
import mmap
import os
import re
import sys

SKILL = "analysis-script-rules"
SKILL_CALL = re.compile(rb'"name":"Skill","input":\{"skill":"(?:[\w-]+:)?analysis-script-rules"')
BOUNDARY = b'"subtype":"compact_boundary"'
SCRIPT_EXT = (".py", ".sh", ".bash", ".ps1", ".psm1", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl")
RECURSIVE_SEARCH = re.compile(
    r"\bgrep\b[^\n|;]*\s-[a-zA-Z]*[rR]"
    r"|\brg\s"
    r"|\bfind\s+[.~/\"'$]"
    r"|\bos\.walk\("
    r"|\.rglob\("
    r"|\bi?glob\([^)\n]*recursive\s*=\s*True"
    r"|Get-ChildItem\b[^\n]*-Recurse"
    r"|\bFiles\.(?:walk|find)\("
    r"|readdirSync\([^)\n]*recursive"
)
HEREDOC_TO_FILE = re.compile(
    r"(?:>|\btee\s+(?:-a\s+)?)\s*[\"']?([^\s\"'<>|;&]+)[\"']?\s*<<-?\s*[\"']?(\w+)[\"']?[^\n]*\n(.*?)\n\s*\2\s*(?:\n|$)"
    r"|<<-?\s*[\"']?(\w+)[\"']?\s*(?:>|\|\s*tee\s+(?:-a\s+)?)\s*[\"']?([^\s\"'<>|;&]+)[\"']?[^\n]*\n(.*?)\n\s*\4\s*(?:\n|$)",
    re.DOTALL,
)


def is_script(path):
    return path.lower().endswith(SCRIPT_EXT)


def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def written_scripts(tool, tool_input):
    """Yield (path, text) for every script this call writes."""
    if tool == "Write":
        path = tool_input.get("file_path") or ""
        if is_script(path):
            yield path, tool_input.get("content") or ""
    elif tool == "Edit":
        path = tool_input.get("file_path") or ""
        if is_script(path):
            yield path, read_text(path) + "\n" + (tool_input.get("new_string") or "")
    elif tool == "Bash":
        command = tool_input.get("command") or ""
        for m in HEREDOC_TO_FILE.finditer(command):
            path, body = (m.group(1), m.group(3)) if m.group(1) else (m.group(5), m.group(6))
            if is_script(path):
                yield path, body


def skill_loaded(transcript_path):
    try:
        with open(transcript_path, "rb") as f:
            if os.fstat(f.fileno()).st_size == 0:
                return False
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
                start = data.rfind(BOUNDARY)
                return SKILL_CALL.search(data[max(start, 0):]) is not None
    except OSError:
        return True  # no readable transcript: do not block blindly


def transcript_for(payload):
    """Main session transcript, or the subagent's own one.

    A subagent's payload carries the main transcript_path plus agent_id; its own
    transcript lies at <transcript dir>/<session_id>/subagents/agent-<agent_id>.jsonl.
    """
    main = payload.get("transcript_path") or ""
    agent = payload.get("agent_id")
    if not agent:
        return main
    if not main:
        return ""
    own = os.path.join(os.path.dirname(main), payload.get("session_id") or "", "subagents", f"agent-{agent}.jsonl")
    return own if os.path.isfile(own) else ""


def reason(path):
    return (
        f"Скрипт поиска или анализа: {path}. Сначала загрузи скилл {SKILL} "
        f"(инструмент Skill, skill={SKILL}) и выполни его правила: самопроверка, "
        "динамический поиск, исключение служебных папок сборки. Потом повтори запись."
    )


def main():
    if os.environ.get("ANALYSIS_GUARD_OFF") == "1":
        return 0
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("hook_event_name") != "PreToolUse":
        return 0
    flagged = [p for p, text in written_scripts(payload.get("tool_name"), payload.get("tool_input") or {})
               if RECURSIVE_SEARCH.search(text)]
    if not flagged:
        return 0
    transcript = transcript_for(payload)
    if not transcript or skill_loaded(transcript):
        return 0
    sys.stderr.buffer.write(reason(flagged[0]).encode("utf-8"))
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - fail-open, a broken guard must not stop work
        sys.exit(0)
