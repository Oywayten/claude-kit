"""Shared by stage_context.py, source_guard.py, stage_format_guard.py and
stage_record_guard.py: the «not a source» list from <repo>/.claude/stage.md,
per-session state, the stage.md format check and the «stage constraint in the
user's message» detector.

stage.md section «## Не источники…», one item per line:
    - <name>: `<python regex>` — <reason>
Items without «<name>:» get names 1, 2, ... by position (legacy); new writes must name them.
Session state: ~/.claude/stage_session/<session_id>.json
    {"off": bool, "allow": [names], "pending": {"ts": float, "excerpt": str} | absent}
Files older than STATE_TTL_DAYS are removed by prune().
"""
import json
import os
import re
import time

STATE_DIR = os.path.expanduser("~/.claude/stage_session")
STATE_TTL_DAYS = 14
SAFE_ID = re.compile(r"^[\w.-]{1,128}$")
ITEM = re.compile(r"^\s*[-*]\s+(?:([\w-]+):\s+)?`([^`]+)`\s*[—–-]+\s*(.+?)\s*$")
LIST_LINE = re.compile(r"^\s*[-*]\s+")


def project_root(start):
    d = os.path.abspath(start or ".")
    while True:
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def stage_path(root):
    return os.path.join(root, ".claude", "stage.md")


def _section_lines(text):
    """Yield (line_no, line) of list lines inside the «Не источники» section."""
    in_section = False
    for n, line in enumerate(text.splitlines(), 1):
        if line.startswith("#"):
            in_section = line.lstrip("#").strip().lower().startswith("не источники")
            continue
        if in_section and LIST_LINE.match(line):
            yield n, line


def parse_text(text):
    out = []
    for _, line in _section_lines(text):
        m = ITEM.match(line)
        if not m:
            continue
        name = m.group(1) or str(len(out) + 1)
        try:
            rx = re.compile(m.group(2))
        except re.error:
            rx = None
        out.append((name, rx, m.group(3)))
    return out


def rules(root):
    """List of (name, compiled_regex_or_None, reason) from the «Не источники» section."""
    try:
        return parse_text(open(stage_path(root), encoding="utf-8").read())
    except OSError:
        return []


def validate(text):
    """Errors in the «Не источники» section: every list item must be `- name: `regex` — reason`."""
    errors, seen = [], {}
    for n, line in _section_lines(text):
        m = ITEM.match(line)
        if not m:
            errors.append(f"строка {n}: нужен вид «- имя: `шаблон` — причина»: {line.strip()[:80]}")
            continue
        name, pattern = m.group(1), m.group(2)
        if not name:
            errors.append(f"строка {n}: у пункта нет имени (нужно для /stage-allow): {line.strip()[:80]}")
        elif name in seen:
            errors.append(f"строка {n}: имя «{name}» уже есть в строке {seen[name]}")
        else:
            seen[name] = n
        try:
            re.compile(pattern)
        except re.error as e:
            errors.append(f"строка {n}: шаблон `{pattern}` не разбирается: {e}")
    return errors


def _state_file(session_id):
    return os.path.join(STATE_DIR, session_id + ".json") if session_id and SAFE_ID.match(session_id) else None


def load_state(session_id):
    p = _state_file(session_id)
    try:
        with open(p, encoding="utf-8") as f:
            st = json.load(f)
        out = {"off": bool(st.get("off")), "allow": [str(x) for x in st.get("allow", [])]}
        if isinstance(st.get("pending"), dict):
            out["pending"] = st["pending"]
        return out
    except (OSError, ValueError, TypeError, AttributeError):
        return {"off": False, "allow": []}


def save_state(session_id, st):
    p = _state_file(session_id)
    if not p:
        return False
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    return True


def prune():
    try:
        now = time.time()
        for name in os.listdir(STATE_DIR):
            p = os.path.join(STATE_DIR, name)
            if now - os.path.getmtime(p) > STATE_TTL_DAYS * 86400:
                os.remove(p)
    except OSError:
        pass


# ---- «stage constraint in the user's message» detector -------------------------------
_QUOTED = re.compile(r"<pasted_content[^>]*>.*?</pasted_content[^>]*>|«[^»]*»|“[^”]*”|\"[^\"\n]*\"|`[^`\n]*`", re.S)
_NEED = r"(?:нужен|нужн\w*|требуется|актуален|актуальн\w*)\b"
# «не нужно <infinitive>»: a source only if the verb is about using/reading something
_INFINITIVE = re.compile(r"^(?:\s+\w+){0,2}?\s+(\w+(?:ть|ться|чь|ти))\b", re.I)
_SOURCE_VERB = re.compile(r"^(?:учитыва|использова|чита|брать|смотре|опира|ориентирова|ссыла|трога|рассматрива|включа)", re.I)
_GENERIC_VERB = re.compile(r"\b(?:использовать|учитывать|использова\w*|учитыва\w*)\b", re.I)
_SOURCE_NOUN = re.compile(
    r"ветк|модул|теори|задани|документ|файл|черновик|аудит|данн|источник|папк|постановк|спек|ADR|"
    r"branch|module|draft|spec|docs?\b|\w/\w|\.md\b", re.I)
_PERSONAL = re.compile(r"(?:^|\s)(?:мне|нам|тебе|вам)\s+(?:\w+\s+)?$", re.I)
_TRIGGERS = [
    r"не\s+(?:является\s+|будет\s+)?источник\w*",
    r"не\s+(?:опирайся|опирайтесь|ориентируйся|ориентируйтесь|ссылайся|ссылайтесь|учитывай|учитывайте)\b",
    r"не\s+бер(?:и|ите)\s+(?:за\s+основу|в\s+расч[её]т)",
    # the author's own phrasing (found in real messages): «ориентироваться на неё сейчас не стоит»,
    # «нельзя … ссылаться на данные следующего», «X не трогаем»
    r"(?:ориентироваться|опираться|ссылаться|использовать|учитывать|брать\s+за\s+основу)\b[^.!?\n]{0,60}?\bне\s+(?:стоит|надо|нужно|следует)\b",
    r"\b(?:не\s+стоит|не\s+надо|не\s+следует|нельзя)\b[^.!?\n]{0,60}?\b(?:ориентироваться|опираться|ссылаться|использовать|учитывать)\b",
    r"\bне\s+трогаем\b",
    r"(?:сейчас|пока|на\s+этом\s+этапе)\s+не\s+" + _NEED,
    r"не\s+" + _NEED + r"\s+(?:сейчас|пока|на\s+этом\s+этапе)",
    r"(?:замечани\w*|правк\w*|комментари\w*)\s+преподавател\w*",
    r"преподавател\w*\s+(?:сказал\w*|написал\w*|просит|требует|вернул\w*|отклонил\w*|не\s+одобрил\w*)",
    r"ревьюер\w*\s+(?:сказал\w*|написал\w*|просит|требует|вернул\w*)",
    r"\bnot\s+a\s+source\b",
    r"\bdon'?t\s+rely\s+on\b",
    r"\bnot\s+(?:needed|relevant)\s+(?:now|yet)\b",
]
_TRIGGER = re.compile("|".join(_TRIGGERS), re.I)


def constraint_excerpt(prompt, root):
    """Sentence of the user's message that states a stage constraint, or None."""
    if not prompt or prompt.lstrip().startswith(("/", "<command-")):
        return None
    text = _QUOTED.sub(" ", prompt)
    text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">"))
    items = parse_text(open(stage_path(root), encoding="utf-8").read()) if root and os.path.isfile(stage_path(root)) else []
    for sentence in re.split(r"(?<=[.!?\n])\s+", text):
        s = sentence.strip()
        if not s or s.endswith("?"):
            continue
        m = _TRIGGER.search(s)
        if not m:
            continue
        if re.search(_NEED, m.group(0), re.I):
            if _PERSONAL.search(s[:m.start()]):
                continue  # «мне не нужно сейчас …» — the user's own need, not the project stage
            inf = _INFINITIVE.match(s[m.end():])
            if inf and not _SOURCE_VERB.match(inf.group(1)):
                continue  # «сейчас не нужно коммитить» — an action, not a source
            if inf and _GENERIC_VERB.match(inf.group(1)) and not _SOURCE_NOUN.search(s):
                continue  # «сейчас не нужно использовать таблицы» — not a source
        if _GENERIC_VERB.search(m.group(0)) and not _SOURCE_NOUN.search(s):
            continue  # «нельзя использовать f-строки» — code/style, not a source
        def mentions(name, rx, reason):
            if len(name) >= 2 and re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", s, re.I):
                return True
            if rx is not None and rx.search(s):
                return True
            head = re.split(r"[:,—–(]", reason, maxsplit=1)[0].strip().lower()  # «модуль 4: …» -> «модуль 4»
            return len(head) >= 4 and head in s.lower()
        already = any(mentions(n, rx, r) for n, rx, r in items)
        if already:
            continue
        return s[:160]
    return None
