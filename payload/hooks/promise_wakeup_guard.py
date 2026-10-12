#!/usr/bin/env python3
"""
Global Stop hook for Claude Code.

Lesson 2026-10-07 (memory feedback-confirm-launch-started): I started a long run as a detached process and
wrote «Когда закончится, сообщу». Nothing could wake me when it ended, so the author found the result an hour
later by asking. This hook catches a final reply that promises to report after something finishes, while the
turn armed no mechanism that wakes the session, and blocks the stop ONCE.

Promise = one sentence with a notify verb (сообщу, напишу, дам знать, доложу, уведомлю, отчитаюсь, скажу,
отпишусь) and a wait marker (когда, как только, после, по завершении/окончании/итогам, закончится,
завершится, будет готов, дождусь).
Wake-up mechanism = in the current turn (after the last real user prompt) a tool call that brings the session
back: Bash/PowerShell/Agent with run_in_background (Agent: unless false), ScheduleWakeup, Monitor, CronCreate.
Admission = the reply says plainly that it will not learn about the end by itself (сам не узнаю, спросите,
напомните, без уведомления): not blocked.
Quotes, code and blockquotes are ignored.

Skipped: subagents, a repeated stop in the same turn. Fail-open. Escape hatch: env WAKEUP_GUARD_OFF=1.
"""
import json
import os
import re
import sys

NOTIFY = re.compile(r"(?<!\w)(?:сообщу|напишу|дам\s+знать|доложу|уведомлю|отчитаюсь|скажу|отпишусь)(?!\w)", re.I)
WAIT = re.compile(
    r"(?<!\w)(?:когда|как\s+только|после|по\s+(?:завершени\w+|окончани\w+|итогам)|закончится|завершится"
    r"|будет\s+готов\w*|дождусь)(?!\w)",
    re.I,
)
ADMIT = re.compile(
    r"сам\s+(?:не\s+)?(?:узнаю|проснусь|увижу)|не\s+смогу\s+(?:сам|сообщить)|спросите|напомните"
    r"|без\s+уведомлени|уведомлени\w+\s+не\s+будет",
    re.I,
)
QUOTED = re.compile(r"```.*?```|`[^`\n]*`|«[^»\n]*»|“[^”\n]*”|\"[^\"\n]*\"", re.S)
SENTENCE = re.compile(r"(?<=[.!?])\s+|\n")
WAKE_TOOLS = {"ScheduleWakeup", "Monitor", "CronCreate"}
SHELL_TOOLS = {"Bash", "PowerShell"}


def is_prompt(o, content):
    if o.get("type") != "user" or o.get("isMeta"):
        return False
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        kinds = {c.get("type") for c in content if isinstance(c, dict)}
        return "text" in kinds and "tool_result" not in kinds
    return False


def is_wake_call(block):
    name, inp = block.get("name"), block.get("input") or {}
    if name in WAKE_TOOLS:
        return True
    if name in SHELL_TOOLS:
        return inp.get("run_in_background") is True
    if name == "Agent":
        return inp.get("run_in_background") is not False
    return False


def last_turn(transcript_path):
    """(assistant text, wake-up armed) for the turn after the last real user prompt."""
    texts, armed = [], False
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
                    texts, armed = [], False
                elif o.get("type") == "assistant" and isinstance(content, list):
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        if c.get("type") == "text":
                            texts.append(c.get("text", ""))
                        elif c.get("type") == "tool_use" and is_wake_call(c):
                            armed = True
    except OSError:
        return "", False
    return "\n".join(texts), armed


def clean(text):
    text = QUOTED.sub(" ", text)
    text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">"))
    return text


def promises(reply):
    text = clean(reply)
    if ADMIT.search(text):
        return []
    return [s.strip() for s in SENTENCE.split(text) if NOTIFY.search(s) and WAIT.search(s)]


def main():
    if os.environ.get("WAKEUP_GUARD_OFF") == "1":
        return 0
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("hook_event_name") != "Stop" or payload.get("agent_id") or payload.get("stop_hook_active"):
        return 0
    reply, armed = last_turn(payload.get("transcript_path") or "")
    if armed:
        return 0
    found = promises(reply)
    if not found:
        return 0
    reason = (f"В ответе обещание сообщить после завершения («{found[0][:80]}»), а в этом ходе нет механизма, "
              "который меня разбудит: сам я о конце не узнаю. Запусти долгую команду с run_in_background "
              "(придёт уведомление) или поставь ScheduleWakeup/Monitor. Если механизма нет, перепиши фразу: "
              "скажи прямо, что сам не узнаю о конце, и попроси спросить позже.")
    sys.stdout.buffer.write(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - fail-open
        sys.exit(0)
