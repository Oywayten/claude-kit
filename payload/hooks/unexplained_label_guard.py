#!/usr/bin/env python3
"""
Global Stop hook for Claude Code.

Lesson 2026-10-04 (memory feedback-explain-terms-in-questions): I asked the author to decide
on items he had never seen («правила 8–10», «решения по Р1–Р5 и Д20») and got «не понял, о чём речь».
It repeated on 2026-10-05. This hook catches a final reply that asks for a decision and uses a short
item label (Cyrillic capital + digits: Р1, Д20, В2) which is never described, and blocks the stop ONCE:
describe each label in one line.

Label = one Cyrillic capital letter + 1-3 digits, not «М» (module numbers the author writes himself).
Described = the label is followed by a dash, colon, pipe or bracket and two or more words, or stands at the
start of a line (after a bullet or a number) with two or more words after it. A range «Р1–Р5» counts as its
right end: «Р1–Р5: решения по контрактам» is described, «по Р1–Р5 и Д20» is not.
Request = «?» or a phrase like «ваше решение», «подтвердите», «выберите». Quotes, code and blockquotes are ignored.
Example = a label after the word «например», «вроде» or «пример…» in the same sentence is shown as an example, not
asked about: it is not reported. Labels before the word, in other sentences or on other lines still count.

Skipped: subagents, a repeated stop in the same turn. Fail-open. Escape hatch: env LABEL_GUARD_OFF=1.
"""
import json
import os
import re
import sys

LABEL = re.compile(r"(?<![\w\-.:\\])([А-ЛН-ЯЁ]\d{1,3})(?!\w|\.\w)")
RANGE = re.compile(r"([А-ЛН-ЯЁ])(\d{1,3})\s*[–-]\s*\1(\d{1,3})(?![\w])")
REQUEST = re.compile(
    r"\?|ваш(?:е|и|а)\w*\s+(?:решени|выбор|ответ|«?да\b)|нужн\w*\s+ваш|жд[её]т\w*\s+(?:ваш|решени)"
    r"|подтверд\w+|выбер\w+|решите|согласн\w+",
    re.I,
)
QUOTED = re.compile(r"```.*?```|`[^`\n]*`|«[^»\n]*»|“[^”\n]*”|\"[^\"\n]*\"", re.S)
SENTENCE = re.compile(r"(?<=[.!?])\s+|\n")
EXAMPLE = re.compile(r"(?<!\w)(?:например|вроде|пример(?:ы|ов|ам|ами|ах|а|е|ом|у)?)(?!\w)", re.I)
MAX_SHOWN = 6


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
                    texts = []
                elif o.get("type") == "assistant" and isinstance(content, list):
                    texts += [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
    except OSError:
        return ""
    return "\n".join(texts)


def clean(text):
    text = QUOTED.sub(" ", text)
    text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">"))
    return text.replace("**", "").replace("__", "")


def is_described(label, text):
    esc = re.escape(label)
    after_sign = re.compile(esc + r"(?![\w])\s*(?:[—–:|)]|\s-\s|\()\s*(?![А-ЯЁ]\d)[^\s.!?]+[ \t]+[^\s.!?]+")
    line_start = re.compile(
        r"^[ \t]*(?:[-*•]|\d+[.)])?[ \t]*\|?[ \t]*" + esc + r"(?![\w])[ \t]*[.,:;)—–-]?[ \t]+(?![А-ЯЁ]\d)(?![—–:|-])[^\s.!?]+(?:[ \t]+[^\s.!?]+)+",
        re.M,
    )
    return bool(after_sign.search(text) or line_start.search(text))


def without_examples(text):
    parts = []
    for sentence in SENTENCE.split(text):
        m = EXAMPLE.search(sentence)
        parts.append(sentence[:m.end()] if m else sentence)
    return "\n".join(parts)


def undescribed_labels(reply):
    text = clean(reply)
    if not REQUEST.search(text):
        return []
    ranges = {}
    for m in RANGE.finditer(text):
        ranges[m.group(1) + m.group(3)] = m.group(0)
    text = RANGE.sub(lambda m: m.group(1) + m.group(3), text)
    found = []
    for m in LABEL.finditer(without_examples(text)):
        label = m.group(1)
        if label not in found and not is_described(label, text):
            found.append(label)
    return [ranges.get(label, label) for label in found]


def main():
    if os.environ.get("LABEL_GUARD_OFF") == "1":
        return 0
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("hook_event_name") != "Stop" or payload.get("agent_id") or payload.get("stop_hook_active"):
        return 0
    labels = undescribed_labels(last_reply(payload.get("transcript_path") or ""))
    if not labels:
        return 0
    shown = ", ".join(labels[:MAX_SHOWN])
    reason = (f"В ответе просьба решить или ответить, а метки без описания: {shown}. Автор этих меток не видел. "
              "Допиши к каждой метке строку: что это и что именно решаем. Любой служебный термин из вопроса "
              "(название потока, параметра, скилла) объясни одной фразой простыми словами. Если метка не нужна, убери её.")
    sys.stdout.buffer.write(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - fail-open
        sys.exit(0)
