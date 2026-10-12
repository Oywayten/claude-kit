#!/usr/bin/env python3
"""
Global UserPromptSubmit hook for Claude Code.

Known stage constraints (what is approved, what is not a source now, review
remarks) were in memory but not applied when choosing what to read. This hook
puts the project's stage file into context, so the constraints are in view at
decision time.

Once per context, not on every message: the block carries a marker
«[stage-ctx:<hash>]» (hash of the block text, allow-list included). The hook
reads the session transcript (payload transcript_path) and skips the block when
the last marker after the last compact_boundary has the same hash. So it is
injected on the first message, after each compaction and after stage.md or the
allow-list changes. Unreadable transcript or no marker -> inject (costs tokens,
never loses the file). Known gap: after a rewind to a message before the
injection, the marker from the abandoned branch is still in the file.
Env STAGE_CONTEXT_EVERY_TURN=1 brings back injection on every message.

File: <repo>/.claude/stage.md (repo found from cwd by the nearest .git).
No file -> nothing is added. Output is capped at MAX_CHARS.

User commands (typed as the whole message) are executed here; the result is
passed to the model as context with an instruction to show it verbatim. A
blocking reply (decision=block) is not shown by the desktop app — it only lands
in the transcript as a system warning; a systemMessage next to it is not shown
either (both verified live 2026-10-04) — so the model has to print the result.
All state is per session; a new session starts with injection on and no exceptions.
  /stage-on, /stage-off       inject the stage file or not
  /stage-status               on/off and whether the file exists
  /stage-info                 the file content
  /stage-allow [name]         this item stops being «not a source» in this session (no name: the list)
  /stage-disallow [name]      the item is «not a source» again (no name: the list)
Items and session state: see stage_rules.py.

Fail-open. Escape hatch: env STAGE_CONTEXT_OFF=1.
"""
import hashlib
import json
import mmap
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stage_rules as sr  # noqa: E402

MAX_CHARS = 6000
BOUNDARY = b'"subtype":"compact_boundary"'
MARKER = b"[stage-ctx:"
HASH_LEN = 12
# quotes unescaped: code or text quoting the record has them escaped, so it is not taken for a real record
HOOK_RECORD = b'"type":"hook_additional_context"'
COMMAND = re.compile(
    r"^\s*(?:<command-name>)?/stage-(on|off|status|info|disallow|allow)(?![\w-])"
    r"(?:</command-name>)?\s*(?:<command-args>)?\s*([\w-]*)"
)
HINT = "Разрешить в этой сессии: /stage-allow <имя>. Вернуть запрет: /stage-disallow <имя>."


def items_text(root, st):
    items = sr.rules(root) if root else []
    if not items:
        return "Не источников в файле этапа нет."
    width = max(len(n) for n, _, _ in items)
    lines = ["Не источники:"]
    for name, _, reason in items:
        state = "разрешено в этой сессии" if name in st["allow"] else "запрещено"
        lines.append(f"  {name.ljust(width)} — {reason} — {state}")
    return "\n".join(lines)


def run_command(cmd, arg, cwd, session_id):
    sr.prune()
    root = sr.project_root(cwd)
    stage = sr.stage_path(root) if root else None
    exists = bool(stage and os.path.isfile(stage))
    st = sr.load_state(session_id)
    needs_session = cmd in ("on", "off") or (cmd in ("allow", "disallow") and arg)
    if needs_session and not sr.save_state(session_id, st):
        return "stage: не удалось определить сессию, состояние не изменено."
    if cmd == "on":
        st["off"] = False
        sr.save_state(session_id, st)
        tail = "" if exists else (" Но файла этапа нет: " + stage if stage else " Но здесь нет проекта (.git).")
        return "stage: включено в этой сессии." + tail
    if cmd == "off":
        st["off"] = True
        sr.save_state(session_id, st)
        return "stage: выключено в этой сессии. Новая сессия начнётся со включённой подстановкой. Включить здесь: /stage-on."
    state = "выключено" if st["off"] else "включено"
    if cmd == "status":
        where = f"Файл этапа: {stage} — {'есть' if exists else 'нет'}." if stage else "Здесь нет проекта (.git)."
        return f"stage: {state} в этой сессии. {where}"
    if cmd == "info":
        if not exists:
            return "stage: файла этапа нет" + (": " + stage if stage else " (здесь нет проекта).")
        return f"stage ({state} в этой сессии), {stage}:\n\n" + open(stage, encoding="utf-8").read().strip()
    if cmd in ("allow", "disallow") and not arg:
        return f"{items_text(root, st)}\n{HINT}"
    names = [n for n, _, _ in (sr.rules(root) if root else [])]
    if arg not in names:
        return f"stage: нет пункта «{arg}».\n{items_text(root, st)}\n{HINT}"
    if cmd == "allow":
        if arg not in st["allow"]:
            st["allow"].append(arg)
        sr.save_state(session_id, st)
        return f"stage: «{arg}» разрешено в этой сессии. В других сессиях запрет действует.\n{items_text(root, st)}"
    st["allow"] = [n for n in st["allow"] if n != arg]
    sr.save_state(session_id, st)
    return f"stage: «{arg}» снова запрещено в этой сессии.\n{items_text(root, st)}"


def last_injected_hash(path):
    """Hash of the last stage-ctx marker in a hook record after the last compact_boundary, else None."""
    try:
        with open(path, "rb") as f:
            if os.fstat(f.fileno()).st_size == 0:
                return None
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
                low = max(data.rfind(BOUNDARY), 0)
                pos = data.rfind(MARKER, low)
                while pos >= 0:
                    line_start = data.rfind(b"\n", 0, pos) + 1
                    line_end = data.find(b"\n", pos)
                    line = data[line_start:] if line_end < 0 else data[line_start:line_end]
                    if HOOK_RECORD in line:
                        return data[pos + len(MARKER):pos + len(MARKER) + HASH_LEN].decode("ascii", "replace")
                    pos = data.rfind(MARKER, low, pos)
    except (OSError, ValueError, TypeError):
        pass
    return None


def inject(cwd, session_id, transcript_path=None):
    st = sr.load_state(session_id)
    if st["off"]:
        return None
    root = sr.project_root(cwd)
    if not root:
        return None
    try:
        text = open(sr.stage_path(root), encoding="utf-8").read().strip()
    except OSError:
        return None
    if not text:
        return None
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n…(обрезано)"
    allowed = [(n, r) for n, _, r in sr.rules(root) if n in st["allow"]]
    extra = ""
    if allowed:
        extra = "\n\nВ этой сессии пользователь разрешил (не считать «не источником»): " + \
                "; ".join(f"{n} — {r}" for n, r in allowed) + "."
    body = ("Учитывай при выборе источников и решений; "
            "новое замечание ревьюера или «это сейчас не нужно» от пользователя — сразу внеси в этот файл. "
            "Разрешения одной сессии в файл не вносить.\n\n" + text + extra)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:HASH_LEN]
    if transcript_path and os.environ.get("STAGE_CONTEXT_EVERY_TURN") != "1" and last_injected_hash(transcript_path) == digest:
        return None
    return f"Этап проекта (.claude/stage.md) {MARKER.decode()}{digest}]. " + body


def emit_context(note):
    out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": note}}
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))


def main():
    if os.environ.get("STAGE_CONTEXT_OFF") == "1":
        return 0
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (json.JSONDecodeError, ValueError):
        return 0
    session_id = payload.get("session_id") or ""
    m = COMMAND.match(payload.get("prompt") or "")
    if m:
        cmd, arg = m.group(1), m.group(2)
        result = run_command(cmd, arg, payload.get("cwd"), session_id)
        emit_context(f"Команду /stage-{cmd} уже выполнил хук stage_context. Результат:\n\n{result}\n\n"
                     "Ответь пользователю ровно этим результатом. Без инструментов, без пояснений.")
        return 0
    mark_pending(payload.get("prompt") or "", payload.get("cwd"), session_id)
    note = inject(payload.get("cwd"), session_id, payload.get("transcript_path"))
    if note:
        emit_context(note)
    return 0


def mark_pending(prompt, cwd, session_id):
    """Remember a stage constraint stated by the user; stage_record_guard checks at Stop that stage.md changed."""
    st = sr.load_state(session_id)
    root = sr.project_root(cwd)
    if st["off"] or not root or not os.path.isfile(sr.stage_path(root)):
        return
    excerpt = sr.constraint_excerpt(prompt, root)
    if excerpt:
        st["pending"] = {"ts": time.time(), "excerpt": excerpt}
        sr.save_state(session_id, st)
    elif "pending" in st:
        st.pop("pending")
        sr.save_state(session_id, st)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - fail-open
        sys.exit(0)
