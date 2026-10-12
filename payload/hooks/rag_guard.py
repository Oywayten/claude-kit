#!/usr/bin/env python3
"""
Global hook for the pgvector-rag MCP server (PreToolUse + PostToolUse, matcher mcp__pgvector-rag__.*).

Rules of using RAG here:
  1. Cite RAG material as the file path plus the quote from the chunk; line numbers are not needed
     (author, 2026-10-11).
  2. The «Не источники» list of the project stage file (<repo>/.claude/stage.md) applies to RAG
     results too: a hit whose path matches a rule is reported, and must not be used unless the
     author allows it (/stage-allow). The check is on the returned paths.
  3. RAG memories (retrieve_memory) may be stale: check them against the repository or file memory.
  4. Writing/deleting RAG memories asks the author: file memory (memory/, .claude/handoff/) is the
     main store; a second copy drifts.

PostToolUse: rag_search -> list of hits from non-sources (silent when none); retrieve_memory -> reminder.
PreToolUse:  store_memory, update_memory, delete_memory -> permissionDecision ask.
Fail-open. Escape hatch: env RAG_GUARD_OFF=1.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stage_rules as sr  # noqa: E402

PREFIX = "mcp__pgvector-rag__"
PATH_FALLBACK = re.compile(r'\\*"path\\*"\s*:\s*\\*"((?:[^"\\]|\\\\)+)')


def flatten(value, out):
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            flatten(v, out)
    elif isinstance(value, list):
        for v in value:
            flatten(v, out)


def hit_paths(response):
    """Paths of RAG hits found anywhere in the tool response (JSON array inside a string, or objects)."""
    strings = []
    flatten(response, strings)
    paths = []
    for s in strings:
        s = s.strip()
        if s.startswith("["):
            try:
                for item in json.loads(s):
                    if isinstance(item, dict) and item.get("path"):
                        paths.append(str(item["path"]))
                continue
            except (ValueError, TypeError):
                pass
        paths.extend(m.group(1).replace("\\\\", "\\") for m in PATH_FALLBACK.finditer(s))
    return list(dict.fromkeys(paths))


def stage_hits(paths, cwd, session_id):
    root = sr.project_root(cwd)
    if not root:
        return []
    allowed = set(sr.load_state(session_id or "")["allow"])
    found = []
    for p in paths:
        norm = p.replace("\\", "/")
        for name, rx, reason in sr.rules(root):
            if rx is not None and name not in allowed and rx.search(norm):
                found.append((p, reason))
    return found


def emit(event, **fields):
    out = {"hookSpecificOutput": {"hookEventName": event, **fields}}
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))


def main():
    if os.environ.get("RAG_GUARD_OFF") == "1":
        return 0
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (json.JSONDecodeError, ValueError):
        return 0
    tool = payload.get("tool_name") or ""
    if not tool.startswith(PREFIX):
        return 0
    name = tool[len(PREFIX):]
    event = payload.get("hook_event_name")
    ti = payload.get("tool_input") or {}
    if event == "PreToolUse":
        if name == "store_memory":
            reason = ("Запись в RAG-память: основное хранилище — файловая память (memory/) и потоки (.claude/handoff/); "
                      "вторая копия расходится. Статусы и задачи сюда не пишут. Записать?")
        elif name == "update_memory":
            reason = f"Правка записи RAG-памяти #{ti.get('memory_id')}. Продолжить?"
        elif name == "delete_memory":
            reason = f"Удаление записи RAG-памяти #{ti.get('memory_id')} необратимо (копия таблицы: E:\\backup\\rag). Удалить?"
        else:
            return 0
        emit("PreToolUse", permissionDecision="ask", permissionDecisionReason=reason)
        return 0
    if event != "PostToolUse":
        return 0
    if name == "rag_search":
        msg = ""
        bad = stage_hits(hit_paths(payload.get("tool_response")), payload.get("cwd"), payload.get("session_id"))
        if bad:
            lines = "; ".join(f"{p} ({r})" for p, r in dict.fromkeys(bad))
            msg = "ВНИМАНИЕ, источники не для текущего этапа среди результатов: " + lines + ". Не использовать без разрешения автора (/stage-allow)."
        if msg:
            emit("PostToolUse", additionalContext=msg)
    elif name == "retrieve_memory":
        emit("PostToolUse", additionalContext="Записи RAG-памяти могут быть устаревшими: сверь с репозиторием и файловой памятью до использования.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - fail-open
        sys.exit(0)
