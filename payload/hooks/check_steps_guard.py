#!/usr/bin/env python3
"""
Global PreToolUse hook for Claude Code (matcher: Write|Edit|Bash).

A check step written for another session or person must say HOW to check
(command, path, tool, UI action) and WHAT must come out (quoted value, number,
hash, present/absent). A step like «Claude проверит: должен быть раздел X»
without a method was written many times despite rules in memory, so this hook
blocks it.

Scope: .md / .txt files (Write, Edit, Bash heredoc into such a file).
Only list items and table rows are steps; plain prose is not checked.
Only NEW offending steps block: steps already present in the file before the
edit are ignored, so legacy files stay editable.

Not checked: fenced code, blockquotes, YAML front matter, text inside quotes or
backticks (quoted examples of bad steps), questions, past-tense reports,
descriptive present tense («сервис проверяет токен»).
Skipped paths: globs in ~/.claude/check_steps_allowlist.txt and
<repo>/.claude/check_steps_allowlist.txt, plus verification/ and *.log.
Fail-open on any internal error. Escape hatch: env CHECK_STEPS_GUARD_OFF=1.
"""
import fnmatch
import json
import os
import re
import sys

TEXT_EXT = (".md", ".txt", ".markdown")
SKIP_PARTS = ("/verification/",)

QUOTED = re.compile(r"`[^`\n]*`|«[^»\n]*»|“[^”\n]*”|\"[^\"\n]*\"|'[^'\n]{2,}'")
_NOT_BEFORE = r"(?<!не )(?<!невозможно )(?<!нельзя )(?<!как )(?<!что )(?<!чем )(?<!где )(?<!чтобы )"
TRIGGER_RU = re.compile(
    # imperative / future forms: anywhere in the line
    _NOT_BEFORE + r"(?<![\w-])(?:провер(?:ь|ьте|ит|им|ите)|убеди(?:сь|тесь)|удостовер(?:ься|ьтесь)"
    r"|свер(?:ь|ьте)|сравни(?:те)?|протестируй(?:те)?)(?![\w-])"
    # infinitive: only at the start of a clause (instruction style «Проверить, что ...»)
    r"|(?:^|[:;—–|]\s*)(?:проверить|убедиться|удостовериться|сверить)(?![\w-])"
    r"|^\s*проверка\s*[:,—–-]",
    re.I,
)
TRIGGER_EN = re.compile(
    r"^\s*(?:verify|check|ensure|confirm|make sure)\b(?!\s*(?:box|list|point|out|in)\b)"
    r"|\b(?:verify|check|ensure|confirm|make sure)\s+(?:that|whether|if)\b",
    re.I,
)
METHOD = re.compile(
    r"`[^`\n]+`"
    r"|```"
    r"|\b[A-Za-z]:[\\/]"
    r"|(?:^|[\s(])(?:~|\.{1,2})?/[\w.-]+/[\w./*-]*"
    r"|\b[\w*.-]+\.(?:md|txt|py|sh|ps1|json|jsonl|ya?ml|toml|kt|kts|java|xml|csv|zip|log|env|mmd|gradle|sql|html|js|ts)\b"
    r"|https?://"
    r"|\b(?:python3?|bash|git|grep|rg|curl|docker|kubectl|gradlew|mvnw?|npm|npx|pip|jq|sha256sum|certutil|psql|make|mmdc|java|javap"
    r"|Get-FileHash|Select-String|Get-ChildItem|Get-Content|Test-Path)\b"
    r"|\b(?:Skill|Read|Grep|Glob|WebFetch)\b"
    r"|\b(?:инструмент\w*|команд\w*|скрипт\w*)\s+[`«\"\w]"
    r"|\b(?:Ctrl|Alt|Shift|Win|Cmd)\s*\+"
    r"|(?:нажм\w*|выбер\w*|откро\w*|откр\w*|click|open|select)\s[^.\n]{0,60}[«\"“]"
    r"|Диспетчер\w*\s+задач"
    r"|(?:в|на)\s+(?:списке|странице|окне|меню|вкладке|панели)\b|переключател\w*|кнопк\w*",
    re.I | re.M,
)
EXPECT = re.compile(
    r"«[^»\n]+»|\"[^\"\n]+\"|“[^”\n]+”"
    r"|\d"
    r"|\b[0-9a-f]{7,}\b"
    r"|\b(?:есть|нет|пуст\w*|совпада\w*|совпал\w*|равн\w*|содерж\w*|отсутству\w*|появ\w*|исчез\w*"
    r"|exists?|empty|equals?|matches?|contains?|absent|present|true|false|ok|pass|fail|healthy|up|running|зелён\w*)\b",
    re.I,
)
RESULT_WORDS = re.compile(r"\b(?:вывод\w*|output|результат\w*|ответ\w*|лог\w*|log)\b", re.I)
ITEM = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(.*)$")
TABLE = re.compile(r"^\s*\|.*\|\s*$")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
FENCE = re.compile(r"^\s*(```|~~~)")


TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def blocks(text):
    """Split markdown-ish text into step blocks.

    Each block: head (first line), own (lines of this item only, used to find the
    check verb) and text (own lines plus nested children and code, used to find
    the method and the expected result).
    """
    lines = text.splitlines()
    out, cur, in_fence, i = [], None, False, 0
    if lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() == "---":
                i = j + 1
                break
    body = lines[i:]
    child = None
    for n, line in enumerate(body):
        if FENCE.match(line):
            in_fence = not in_fence
            if cur is not None:
                cur["text"] += "\n```"
            continue
        if in_fence:
            if cur is not None:
                cur["text"] += "\n" + line
            continue
        m = ITEM.match(line)
        if m:
            indent = len(m.group(1))
            if cur is not None and indent > cur["indent"]:
                cur["text"] += "\n" + line  # nested child: method/result for the parent
                child = {"head": m.group(2), "own": m.group(2), "text": m.group(2), "indent": indent}
                out.append(child)
                continue
            cur = {"head": m.group(2), "own": m.group(2), "text": m.group(2), "indent": indent}
            child = None
            out.append(cur)
            continue
        if TABLE.match(line):
            nxt = body[n + 1] if n + 1 < len(body) else ""
            if not TABLE_SEP.match(line) and not TABLE_SEP.match(nxt):  # skip separator and header rows
                out.append({"head": line, "own": line, "text": line, "indent": 0})
            cur = child = None
            continue
        if not line.strip() or HEADING.match(line) or line.lstrip().startswith(">") or re.match(r"^\s*[-=]{3,}\s*$", line):
            cur = child = None
            continue
        if cur is not None:
            cur["text"] += "\n" + line
            target = child if child is not None else cur
            target["own"] += "\n" + line
            if target is child:
                child["text"] += "\n" + line
    return out


def headings(text):
    lines = text.splitlines()
    found = [m.group(1).strip().lower() for m in map(HEADING.match, lines) if m]
    for a, b in zip(lines, lines[1:]):  # setext / plain-text underline headings
        if a.strip() and re.match(r"^\s*[-=]{3,}\s*$", b):
            found.append(a.strip().lower())
    return found


def is_check(block_text):
    for line in block_text.splitlines():
        bare = QUOTED.sub(" ", line)
        if bare.rstrip().endswith("?"):
            continue
        bare = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?(?:\[[ xX]\]\s*)?(?:\*\*|__)?", "", bare)
        if TRIGGER_RU.search(bare) or TRIGGER_EN.search(bare):
            return True
    return False


def has_reference(block_text, heads):
    for q in re.findall(r"«([^»\n]+)»|\"([^\"\n]+)\"", block_text):
        name = (q[0] or q[1]).strip().lower()
        if len(name) >= 4 and any(h.startswith(name) or name in h for h in heads):
            return True
    return False


def offenders(text):
    found, items, heads = [], blocks(text), headings(text)
    for idx, b in enumerate(items):
        if not is_check(b["own"]):
            continue
        method = METHOD.search(b["text"]) or has_reference(b["text"], heads)
        if not method and idx > 0 and RESULT_WORDS.search(b["text"]):
            prev = items[idx - 1]
            method = METHOD.search(prev["text"]) is not None
        expect = EXPECT.search(re.sub(r"`[^`\n]*`", " ", b["text"]))
        if not method or not expect:
            missing = " и ".join(x for x, ok in (("чем проверить", method), ("что должно получиться", expect)) if not ok)
            found.append((b["head"].strip(), missing))
    return found


def allowlisted(path):
    norm = path.replace("\\", "/")
    if any(p in norm.lower() for p in SKIP_PARTS) or norm.lower().endswith(".log"):
        return True
    lists = [os.path.expanduser("~/.claude/check_steps_allowlist.txt")]
    d = os.path.dirname(os.path.abspath(path))
    while True:
        lists.append(os.path.join(d, ".claude", "check_steps_allowlist.txt"))
        if os.path.isdir(os.path.join(d, ".git")) or os.path.dirname(d) == d:
            break
        d = os.path.dirname(d)
    for lst in lists:
        try:
            for g in open(lst, encoding="utf-8").read().splitlines():
                g = g.strip()
                if g and not g.startswith("#") and (fnmatch.fnmatch(norm, g) or fnmatch.fnmatch(os.path.basename(norm), g)):
                    return True
        except OSError:
            continue
    return False


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


HEREDOC = re.compile(
    r"(?:>|\btee\s+(?:-a\s+)?)\s*[\"']?([^\s\"'<>|;&]+)[\"']?\s*<<-?\s*[\"']?(\w+)[\"']?[^\n]*\n(.*?)\n\s*\2\s*(?:\n|$)"
    r"|<<-?\s*[\"']?(\w+)[\"']?\s*(?:>|\|\s*tee\s+(?:-a\s+)?)\s*[\"']?([^\s\"'<>|;&]+)[\"']?[^\n]*\n(.*?)\n\s*\4\s*(?:\n|$)",
    re.DOTALL,
)


def writes(tool, ti):
    """Yield (path, old_text, new_text)."""
    if tool == "Write":
        p = ti.get("file_path") or ""
        yield p, read(p), ti.get("content") or ""
    elif tool == "Edit":
        p = ti.get("file_path") or ""
        old = read(p)
        a, b = ti.get("old_string") or "", ti.get("new_string") or ""
        new = old.replace(a, b) if ti.get("replace_all") else old.replace(a, b, 1)
        yield p, old, new if a in old else old + "\n" + b
    elif tool == "Bash":
        for m in HEREDOC.finditer(ti.get("command") or ""):
            p, body = (m.group(1), m.group(3)) if m.group(1) else (m.group(5), m.group(6))
            yield p, read(p), body


def main():
    if os.environ.get("CHECK_STEPS_GUARD_OFF") == "1":
        return 0
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("hook_event_name") != "PreToolUse":
        return 0
    report = []
    for path, old, new in writes(payload.get("tool_name"), payload.get("tool_input") or {}):
        if not path.lower().endswith(TEXT_EXT) or allowlisted(path):
            continue
        before = {h for h, _ in offenders(old)}
        for head, missing in offenders(new):
            if head not in before:
                report.append(f"{os.path.basename(path)}: «{head[:110]}» — нет: {missing}")
    if not report:
        return 0
    sys.stderr.buffer.write((
        "Шаг проверки без способа или без ожидаемого результата:\n" + "\n".join(report[:10])
        + "\nДобавь в шаг: чем проверить (команда, путь, инструмент, действие в интерфейсе) "
        "и что должно получиться (значение, строка, число, есть/нет)."
    ).encode("utf-8"))
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - fail-open, a broken guard must not stop work
        sys.exit(0)
