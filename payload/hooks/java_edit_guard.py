#!/usr/bin/env python3
"""
Global PreToolUse hook for Claude Code (matcher: Edit|Write).

Applies to *.java, *.kt, *.kts files, on every project on this machine
(registered in the GLOBAL ~/.claude/settings.json).

Changed 2026-09-28: the outright "no new comment" ban is gone. A regex
hook can only detect the PRESENCE of a comment, not whether it is
justified -- "is this comment critically needed and non-obvious" is a
judgment call, not a pattern match, so a hard mechanical block was the
wrong tool for it. It now only LOGS newly-introduced comment lines
(append-only, never blocks) to NEW_COMMENTS_LOG, so Claude or the user
can review the diff and judge each one on its merits.

Rules:
  A) Newly-introduced comment lines (// or block/KDoc) are appended to
     NEW_COMMENTS_LOG with file path + line text. Never blocks.
  B) For the Edit tool: `old_string` must not start with a newline. On a
     CRLF file this glues the previous and next lines together when the
     match is removed. This still blocks.

Exit code 2 = block; stderr is the reason shown to Claude. Exit 0 = allow.
"""
import json
import re
import sys
from pathlib import Path

COVERED_EXTENSIONS = (".java", ".kt", ".kts")
COMMENT_RE = re.compile(r"(?<!:)//")  # skip "://" (URLs)
BLOCK_COMMENT_START_RE = re.compile(r"^(/\*|\*)")  # /*, /**, or a KDoc/Javadoc continuation "* ..."
NEW_COMMENTS_LOG = Path.home() / ".claude" / "hooks" / "new-comments-log.jsonl"
NEW_COMMENTS_LOG_MAX_RECORDS = 200


def new_comments(old_text: str, new_text: str):
    old_lines = {line.strip() for line in old_text.split("\n")}
    found = []
    for line in new_text.split("\n"):
        stripped = line.strip()
        if stripped in old_lines:
            continue  # unchanged line, not newly introduced
        if COMMENT_RE.search(line) or BLOCK_COMMENT_START_RE.match(stripped):
            found.append(stripped)
    return found


def log_new_comments(file_path: str, comments: list[str]):
    if not comments:
        return
    with NEW_COMMENTS_LOG.open("a", encoding="utf-8") as f:
        for line in comments:
            f.write(json.dumps({"file": file_path, "line": line}, ensure_ascii=False) + "\n")
    trim_log_to_max_records()


def trim_log_to_max_records():
    lines = NEW_COMMENTS_LOG.read_text(encoding="utf-8").splitlines(keepends=True)
    if len(lines) <= NEW_COMMENTS_LOG_MAX_RECORDS:
        return
    NEW_COMMENTS_LOG.write_text("".join(lines[-NEW_COMMENTS_LOG_MAX_RECORDS:]), encoding="utf-8")


def main():
    # Harness sends UTF-8; Windows default stdin/stderr codec is cp1251 (mangles Cyrillic).
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except Exception:
        sys.exit(0)

    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    if tool_name not in ("Edit", "Write"):
        sys.exit(0)

    file_path = tool_input.get("file_path", "")
    if not file_path.endswith(COVERED_EXTENSIONS):
        sys.exit(0)

    if tool_name == "Edit":
        old_text = tool_input.get("old_string", "")
        new_text = tool_input.get("new_string", "")

        if old_text.startswith("\n"):
            print(
                "Blocked: the replaced fragment starts with a newline. On a CRLF file, "
                "removing it glues the previous and next lines together. Widen old_string "
                "to start on the previous non-blank line instead.",
                file=sys.stderr,
            )
            sys.exit(2)
    else:  # Write
        old_text = ""
        new_text = tool_input.get("content", "")

    log_new_comments(file_path, new_comments(old_text, new_text))
    sys.exit(0)


if __name__ == "__main__":
    main()

