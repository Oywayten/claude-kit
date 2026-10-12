"""PostToolUse(Edit|Write): имена, метрики и версии из нового текста диаграммы/доки ищет в коде и конфигах репозитория.

Диаграмма (.mmd/.puml, папка diagrams/): не найдено -> exit 2 со списком (запись уже сделана, это сигнал
перепроверить по коду). Прочие доки (README*, docs/): тот же список подсказкой, без блока.
Одиночные слова с заглавной (имена библиотек вроде Lettuce) проверяются только в .mmd.
Намеренные имена только из дизайна -> <repo>/.claude/doc_fact_allowlist.txt, по одному на строку.
Ручной режим: python doc_fact_guard.py <файл> -- проверить файл целиком.
"""
import json
import os
import re
import subprocess
import sys

sys.stderr.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")

DOC_EXT = (".mmd", ".puml")
DOC_DIRS = ("diagrams", "docs")
SKIP_EXT = {".md", ".mmd", ".puml", ".png", ".jpg", ".jpeg", ".svg", ".gif", ".jar", ".txt", ".pdf",
            ".patch", ".jsonl", ".lock"}
SKIP_DIRS = ("tasks_src/", "docs/", "diagrams/", "reports/", "verification/", ".claude/")
MAX_FILE_BYTES = 2_000_000
METRIC_SUFFIXES = ("_total", "_seconds", "_count", "_sum", "_bucket", "_max", "_bytes")
FILE_EXT_RE = re.compile(r"\.(kt|kts|java|ya?ml|toml|xml|properties|sql|avsc|json|md|mmd|puml|sh|py|alloy|env|example)$")

ANNOTATION_RE = re.compile(r"@([A-Z][A-Za-z0-9]+)\b")
CAMEL_RE = re.compile(r"(?<![\w@./-])([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+)\b")
WORD_RE = re.compile(r"(?<![\w@./-])([A-Z][a-z]{3,})\b(?![./-]\w)")
UPPER_SNAKE_RE = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b")
SNAKE_RE = re.compile(r"(?<![\w./-])([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b(?![./-]\w)")
DOTTED_RE = re.compile(r"`([a-z][\w-]*(?:\.[\w-]+)+)`")
VERSION_RE = re.compile(r"\b([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+)?) v?(\d+\.\d+(?:\.\d+)?)\b")


def is_doc(path: str) -> bool:
    p = path.replace("\\", "/")
    name = os.path.basename(p).lower()
    if name.endswith(DOC_EXT):
        return True
    if not name.endswith(".md"):
        return False
    return name.startswith("readme") or any(f"/{d}/" in f"/{p}" for d in DOC_DIRS)


def repo_root(path: str):
    proc = subprocess.run(["git", "-C", os.path.dirname(os.path.abspath(path)), "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def load_corpus(root: str):
    proc = subprocess.run(["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                          capture_output=True)
    files = [f for f in proc.stdout.decode("utf-8", "replace").split("\0") if f]
    lines, names = [], set()
    for rel in files:
        names.add(os.path.basename(rel))
        if rel.startswith(SKIP_DIRS) or os.path.splitext(rel)[1].lower() in SKIP_EXT:
            continue
        full = os.path.join(root, rel)
        try:
            if os.path.getsize(full) > MAX_FILE_BYTES:
                continue
            with open(full, encoding="utf-8", errors="replace") as fh:
                lines.extend(l for l in fh.read().splitlines() if "exclude(" not in l)
        except OSError:
            continue
    text = "\n".join(lines)
    return text, text.lower(), names


def load_allowlist(root: str):
    path = os.path.join(root, ".claude", "doc_fact_allowlist.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            return {l.strip() for l in fh if l.strip() and not l.startswith("#")}
    except OSError:
        return set()


def squash(s: str) -> str:
    return re.sub(r"[^a-z0-9.]", "", s.lower())


def diagram_ids(path: str):
    if not path.lower().endswith(DOC_EXT):
        return set()
    try:
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
    except OSError:
        return set()
    ids = set(re.findall(r"(?m)^\s*(?:participant|actor)\s+\"?(\w+)\"?\s+as\b", source))
    ids |= set(re.findall(r"(?m)^\s*(?:participant|actor)\s+\"[^\"]*\"\s+as\s+(\w+)", source))
    ids |= set(re.findall(r"(?m)^\s*(?:note|object|rectangle)\b.*?\bas\s+(\w+)", source))
    ids |= set(re.findall(r"(?m)^\s*subgraph\s+(\w+)", source))
    ids |= set(re.findall(r"(?m)^\s*\w+\(\s*(\w+)\s*,", source))
    ids |= set(re.findall(r"(?m)(?:^\s*|[->|&]\s*)(\w+)\s*(?:\[|\(|\{|>)", source))
    return ids


def is_diagram(path: str) -> bool:
    p = "/" + path.replace("\\", "/")
    return p.lower().endswith(DOC_EXT) or "/diagrams/" in p


def find_unknown(text: str, corpus: str, corpus_lower: str, names: set, allow: set, check_words: bool):
    unknown = []

    def report(token, why):
        if token not in allow and (token, why) not in unknown:
            unknown.append((token, why))

    for name in ANNOTATION_RE.findall(text):
        if f"@{name}" not in corpus:
            report(f"@{name}", "аннотации нет в коде")
    for name in CAMEL_RE.findall(text):
        if not re.search(rf"\b{name}\b", corpus) and name.lower() not in corpus_lower:
            report(name, "имени нет в коде/конфигах")
    for name in WORD_RE.findall(text) if check_words else []:
        if name.lower() not in corpus_lower:
            report(name, "слова нет в коде/конфигах")
    for name in UPPER_SNAKE_RE.findall(text):
        if name not in corpus and not any(n.startswith(name) for n in names):
            report(name, "константы нет в коде")
    for name in SNAKE_RE.findall(text):
        base = name
        for suffix in METRIC_SUFFIXES:
            if base.endswith(suffix) and base != suffix[1:]:
                base = base[: -len(suffix)]
                break
        if name not in corpus and base not in corpus and base.replace("_", ".") not in corpus:
            report(name, "имени/метрики нет в коде")
    for name in DOTTED_RE.findall(text):
        if FILE_EXT_RE.search(name):
            if os.path.basename(name) not in names:
                report(name, "файла нет в репозитории")
        elif name not in corpus and not re.search(rf"\b{re.escape(name.split('.')[-1])}\b", corpus):
            report(name, "ключа/спана нет в коде")
    squashed_lines = None
    for name, version in VERSION_RE.findall(text):
        if f"{name} {version}" in allow:
            continue
        if squashed_lines is None:
            squashed_lines = [squash(l) for l in corpus.splitlines()]
        key = squash(name)
        ver_re = re.compile(rf"(?<![\d.]){re.escape(version)}(?![\d])")
        if not any(key in l and ver_re.search(l) for l in squashed_lines):
            report(f"{name} {version}", "версия не совпадает с compose/сборкой")
    return unknown


def main():
    if len(sys.argv) > 1:
        path = sys.argv[1]
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    else:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        tool_input = payload.get("tool_input") or {}
        path = tool_input.get("file_path") or ""
        text = tool_input.get("new_string") if "new_string" in tool_input else tool_input.get("content", "")
    if not path or not text or not is_doc(path):
        sys.exit(0)
    root = repo_root(path)
    if not root:
        sys.exit(0)
    corpus, corpus_lower, names = load_corpus(root)
    unknown = find_unknown(text, corpus, corpus_lower, names, load_allowlist(root) | diagram_ids(path),
                           path.lower().endswith(".mmd"))
    if not unknown:
        sys.exit(0)
    shown = "\n".join(f"  {token} -- {why}" for token, why in unknown[:25])
    more = f"\n  ... ещё {len(unknown) - 25}" if len(unknown) > 25 else ""
    message = (f"doc_fact_guard: {os.path.basename(path)} -- не подтверждено кодом/конфигом репозитория:\n{shown}{more}\n"
               "Сверь с кодом (не по памяти). Намеренное имя из дизайна -> .claude/doc_fact_allowlist.txt.")
    if is_diagram(path):
        print(message, file=sys.stderr)
        sys.exit(2)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": message}},
                     ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
