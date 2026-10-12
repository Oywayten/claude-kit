#!/usr/bin/env python3
"""
Global PreToolUse hook for Claude Code (matcher: Bash, PowerShell).

Applies to every project on this machine (registered in the GLOBAL
~/.claude/settings.json). Automates a lesson learned the hard way: a commit
landed inside someone else's in-progress `git rebase -i` without Claude
noticing, because nothing forced a state check before the risky command ran.

Before a history-changing git command runs, verify the repo isn't already in
an unexpected state (detached HEAD, or a rebase/merge/cherry-pick/bisect left
in progress by something/someone else). If so, block and tell Claude to run
`git status` and confirm with the user before proceeding -- don't guess
--abort/--continue.

Risky commands checked: commit, rebase (unless --continue/--abort/--skip),
reset, revert, cherry-pick (unless --continue/--abort/--skip), merge, and
push with --force/-f/--force-with-lease. A plain `git push`, `git status`,
`git log`, `git diff`, etc. are left alone.

Exit code 2 = block; stderr is the reason shown to Claude. Exit 0 = allow.
"""
import json
import os
import subprocess
import sys

RISKY_SUBCOMMANDS = {"commit", "rebase", "reset", "revert", "cherry-pick", "merge"}
GLOBAL_OPTS_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree"}


def find_git_invocations(command: str):
    tokens = command.split()
    invocations = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "git" or tok.endswith("/git") or tok.endswith("\\git"):
            j = i + 1
            while j < n and tokens[j].startswith("-"):
                bare = tokens[j].split("=")[0]
                if bare in GLOBAL_OPTS_WITH_ARG and "=" not in tokens[j] and j + 1 < n:
                    j += 2
                else:
                    j += 1
            if j < n:
                invocations.append((tokens[j], tokens[j + 1 : j + 6]))
            i = j
        else:
            i += 1
    return invocations


def is_risky(command: str) -> bool:
    for sub, rest in find_git_invocations(command):
        if sub in RISKY_SUBCOMMANDS:
            if sub in ("rebase", "cherry-pick") and any(
                a in ("--continue", "--abort", "--skip") for a in rest
            ):
                continue  # resolving an in-progress op is the correct move, don't block it
            return True
        if sub == "push" and any(
            a in ("--force", "-f", "--force-with-lease") for a in rest
        ):
            return True
    return False


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def main():
    # Harness sends UTF-8; Windows default stdin/stderr codec is cp1251 (mangles Cyrillic).
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except Exception:
        sys.exit(0)

    if payload.get("tool_name") not in ("Bash", "PowerShell"):
        sys.exit(0)

    command = (payload.get("tool_input") or {}).get("command", "")
    if not is_risky(command):
        sys.exit(0)

    if run(["git", "rev-parse", "--is-inside-work-tree"]) != "true":
        sys.exit(0)  # not a git repo, nothing to check

    branch = run(["git", "symbolic-ref", "-q", "--short", "HEAD"])
    git_dir = run(["git", "rev-parse", "--git-dir"])

    problems = []
    if not branch:
        problems.append("HEAD is detached (not on a named branch)")

    for marker, label in [
        ("rebase-merge", "an interactive rebase"),
        ("rebase-apply", "a rebase"),
        ("MERGE_HEAD", "a merge"),
        ("CHERRY_PICK_HEAD", "a cherry-pick"),
        ("BISECT_LOG", "a bisect"),
    ]:
        if git_dir and os.path.exists(os.path.join(git_dir, marker)):
            problems.append(f"{label} is in progress")

    if problems:
        print(
            "Blocked: repo is in an unexpected state before a history-changing git "
            "command -- " + "; ".join(problems) + ". Run `git status` and confirm "
            "with the user whose operation this is before proceeding (don't guess "
            "--abort/--continue). Command: " + command,
            file=sys.stderr,
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
