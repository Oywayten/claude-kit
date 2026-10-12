#!/usr/bin/env python3
"""
Global PreToolUse hook for Claude Code (matcher: Bash).

Blocks `git commit` invocations that:
  1. carry an attribution trailer (Co-Authored-By / Signed-off-by / Author:) --
     the user is the sole author of every commit, no AI co-authorship ever.
  2. have a malformed commit message: empty subject, subject line too long,
     or no blank line between subject and body.

Applies to every project on this machine (registered in the GLOBAL
~/.claude/settings.json, not a per-project .claude/settings.json).

Exit code 2 = block the tool call; the stderr text is surfaced to Claude
as the reason. Exit code 0 = allow.
"""
import json
import os
import re
import shlex
import sys

MAX_SUBJECT_LEN = 100
ATTRIBUTION_RE = re.compile(r"^\s*(co-authored-by|signed-off-by|author)\s*:", re.IGNORECASE)


HEREDOC_RE = re.compile(r"<<[-~]?[ \t]*(['\"]?)(\w+)\1([^\n]*)\n(.*?)\n[ \t]*\2(?=\s|\)|$)", re.DOTALL)
PLACEHOLDER_RE = re.compile(r"__HEREDOC_(\d+)__")
CAT_HEREDOC_RE = re.compile(r"^\$\(\s*cat\s+__HEREDOC_(\d+)__\s*\)$")
SEPARATOR_CHARS = set(";&|\n")
GIT_OPTIONS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
COMMIT_SHORT_FLAGS_WITH_VALUE = "mFCct"


def cut_heredocs(command: str):
    bodies = []

    def replace(match):
        bodies.append(match.group(4))
        return f"__HEREDOC_{len(bodies) - 1}__{match.group(3)}"

    return HEREDOC_RE.sub(replace, command), bodies


def split_subcommands(command: str):
    lexer = shlex.shlex(command, posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return [part.split() for part in re.split(r"&&|\|\||;|\||\n", command)]
    segments, current = [], []
    for tok in tokens:
        if tok and set(tok) <= set("();<>|&\n") and set(tok) & SEPARATOR_CHARS:
            segments.append(current)
            current = []
        else:
            current.append(tok)
    segments.append(current)
    return segments


def commit_args(tokens):
    i = 0
    while i < len(tokens) and re.match(r"^\w+=", tokens[i]):
        i += 1
    if i >= len(tokens) or re.split(r"[\\/]", tokens[i])[-1] not in ("git", "git.exe"):
        return None
    i += 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in GIT_OPTIONS_WITH_VALUE:
            i += 2
        elif tok.startswith("-"):
            i += 1
        else:
            return tokens[i + 1:] if tok == "commit" else None
    return None


def resolve_value(value: str, bodies):
    whole = CAT_HEREDOC_RE.match(value.strip())
    if whole:
        return bodies[int(whole.group(1))]
    return PLACEHOLDER_RE.sub(lambda m: bodies[int(m.group(1))], value)


def read_message_file(value: str, args, bodies, cwd):
    if value == "-":
        for tok in args:
            found = PLACEHOLDER_RE.fullmatch(tok)
            if found:
                return bodies[int(found.group(1))]
        return None
    path = value if os.path.isabs(value) or not cwd else os.path.join(cwd, value)
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def extract_commit_messages(command: str, cwd: str = ""):
    """Return the -m/-F messages of every `git commit` subcommand, ignoring the rest of the command."""
    if "git" not in command or "commit" not in command:
        return []

    command, bodies = cut_heredocs(command)
    messages = []
    for tokens in split_subcommands(command):
        args = commit_args(tokens)
        if args is None:
            continue
        inline, files = [], []
        i = 0
        while i < len(args):
            tok = args[i]
            nxt = args[i + 1] if i + 1 < len(args) else None
            if tok in ("-m", "--message"):
                if nxt is not None:
                    inline.append(nxt)
                i += 2
                continue
            if tok in ("-F", "--file"):
                if nxt is not None:
                    files.append(nxt)
                i += 2
                continue
            if tok.startswith("--message="):
                inline.append(tok[len("--message="):])
            elif tok.startswith("--file="):
                files.append(tok[len("--file="):])
            elif tok.startswith("-") and not tok.startswith("--"):
                for pos, ch in enumerate(tok[1:], start=1):
                    if ch in COMMIT_SHORT_FLAGS_WITH_VALUE:
                        value = tok[pos + 1:] or nxt
                        if not tok[pos + 1:]:
                            i += 1
                        if value is not None and ch == "m":
                            inline.append(value)
                        elif value is not None and ch == "F":
                            files.append(value)
                        break
            i += 1
        if inline:
            messages.append("\n\n".join(resolve_value(v, bodies) for v in inline))
        for value in files:
            text = read_message_file(value, args, bodies, cwd)
            if text is not None:
                messages.append(text)

    return messages


def check_format(message: str):
    lines = message.split("\n")
    subject = lines[0].strip() if lines else ""

    if not subject:
        return "commit message has an empty subject line"
    if len(subject) > MAX_SUBJECT_LEN:
        return f"commit subject line is {len(subject)} chars (max {MAX_SUBJECT_LEN}): {subject!r}"
    if len(lines) > 1 and lines[1].strip() != "":
        return "no blank line between the subject and the body"
    return None


def check_attribution(message: str):
    for line in message.split("\n"):
        if ATTRIBUTION_RE.match(line):
            return line.strip()
    return None


def main():
    # The harness sends UTF-8; on Windows the default stdin codec is cp1251, which turns every
    # Cyrillic letter into two characters and doubled the measured subject length.
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except Exception:
        sys.exit(0)  # can't parse input -> don't block

    if payload.get("tool_name") != "Bash":
        sys.exit(0)

    command = (payload.get("tool_input") or {}).get("command", "")
    messages = extract_commit_messages(command, payload.get("cwd") or "")
    if not messages:
        sys.exit(0)

    for message in messages:
        bad_attr = check_attribution(message)
        if bad_attr:
            print(
                f"Blocked: commit message contains an attribution trailer ({bad_attr!r}). "
                "The user is the sole author of every commit -- never add "
                "Co-Authored-By / Signed-off-by / Author lines.",
                file=sys.stderr,
            )
            sys.exit(2)

        fmt_error = check_format(message)
        if fmt_error:
            print(f"Blocked: commit message format problem -- {fmt_error}.", file=sys.stderr)
            sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()

