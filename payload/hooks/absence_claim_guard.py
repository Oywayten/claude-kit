#!/usr/bin/env python3
"""
Global Stop hook for Claude Code.

I said «не сказано / указаний нет» about lessons and curator notes while the
answer was in CLAUDE.md (loaded in context) and in an index I did not open.
This hook catches an absence claim about sources in the final reply of the turn
and blocks the stop ONCE until the reply names where it searched («Где искал: …»)
or the claim is corrected.

Absence claim = phrase like «не сказано», «не указано», «не упоминается»,
«указаний нет», «нет ни слова», «нигде не» in a sentence that also mentions a
source (урок, теория, практика, задание, куратор, преподаватель, документ,
CLAUDE.md, README, ADR, спека, постановка). Quotes, code and blockquotes are ignored.

Active only in projects that have tasks_src/INDEX.md or .claude/stage.md.
Skipped: subagents, a repeated stop in the same turn. Fail-open.
Escape hatch: env ABSENCE_GUARD_OFF=1.
"""
import json
import os
import re
import sys

ABSENCE = re.compile(
    r"не\s+(?:сказано|указано|упоминается|упомянут\w*|оговорен\w*|описан\w*)\b"
    r"|(?:указаний|упоминаний|рекомендаций|указания)\s+нет\b"
    r"|нет\s+(?:указаний|упоминаний|рекомендаций|ни\s+слова)"
    r"|ничего\s+не\s+(?:сказано|говорится)"
    r"|нигде\s+не\b"
    r"|(?:не\s+|ни\s+)(?:обсуждается|рассматривается)",
    re.I,
)
SOURCE = re.compile(
    r"урок|теори|практик|задани|куратор|преподавател|документ|CLAUDE\.md|README|ADR|спек|постановк|lesson|spec",
    re.I,
)
SEARCHED = re.compile(r"где\s+искал|искал\s+в\b|проверил\s+в\b|searched\s+in", re.I)
QUOTED = re.compile(r"```.*?```|`[^`\n]*`|«[^»\n]*»|“[^”\n]*”|\"[^\"\n]*\"", re.S)


def project_root(start):
    d = os.path.abspath(start or ".")
    while True:
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def last_reply(transcript_path):
    """Assistant text written after the last real user prompt."""
    texts = []
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
                if o.get("type") == "user" and isinstance(content, str) and not o.get("isMeta"):
                    texts = []  # a new real prompt starts the turn
                elif o.get("type") == "assistant" and isinstance(content, list):
                    texts += [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
    except OSError:
        return ""
    return "\n".join(texts)


def absence_claims(text):
    text = QUOTED.sub(" ", text)
    text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">"))
    found = []
    for paragraph in re.split(r"\n\s*\n|\n(?=\s*(?:[-*]|\d+[.)])\s)", text):
        sentences = re.split(r"(?<=[.!?])\s+|\n", paragraph)
        for i, sentence in enumerate(sentences):
            context = sentence + " " + (sentences[i - 1] if i else "")  # the source is often named a sentence earlier
            if ABSENCE.search(sentence) and SOURCE.search(context):
                found.append(sentence.strip()[:150])
    return found


def main():
    if os.environ.get("ABSENCE_GUARD_OFF") == "1":
        return 0
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("hook_event_name") != "Stop" or payload.get("agent_id") or payload.get("stop_hook_active"):
        return 0
    root = project_root(payload.get("cwd"))
    if not root or not (os.path.isfile(os.path.join(root, "tasks_src", "INDEX.md"))
                        or os.path.isfile(os.path.join(root, ".claude", "stage.md"))):
        return 0
    reply = last_reply(payload.get("transcript_path") or "")
    claims = absence_claims(reply)
    if not claims or SEARCHED.search(reply):
        return 0
    index = "tasks_src/INDEX.md, " if os.path.isfile(os.path.join(root, "tasks_src", "INDEX.md")) else ""
    reason = (f"В ответе утверждение об отсутствии в источниках: «{claims[0]}». Перед ним пройди {index}CLAUDE.md "
              "проекта, .claude/stage.md и память. Если нашлось — исправь ответ. Если нет — допиши одну строку "
              "«Где искал: …» со списком проверенных файлов.")
    sys.stdout.buffer.write(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - fail-open
        sys.exit(0)
