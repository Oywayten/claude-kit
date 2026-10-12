#!/usr/bin/env python3
"""
Global UserPromptSubmit hook for Claude Code: hint when the user's message matches an active thread.

Matches the message against the `keywords` of the active threads (handoff_lib.py). On a match it
adds ONE line: «Тема совпала с потоком X: файл …». The same thread is hinted again only after
REHINT_AFTER prompts of the same session (the first hint scrolls away in long sessions).
Skips slash commands and very short messages. State: ~/.claude/handoff_session/<session_id>.json.

Fail-open. Escape hatch: env HANDOFF_OFF=1.
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import handoff_lib as hl  # noqa: E402

STATE_DIR = os.path.expanduser("~/.claude/handoff_session")
SAFE_ID = re.compile(r"^[\w.-]{1,128}$")
REHINT_AFTER = 25
TTL_DAYS = 14
MIN_LEN = 12


def load(session_id):
    try:
        with open(os.path.join(STATE_DIR, session_id + ".json"), encoding="utf-8") as f:
            st = json.load(f)
        return {"n": int(st.get("n", 0)), "hinted": dict(st.get("hinted", {}))}
    except (OSError, ValueError, TypeError):
        return {"n": 0, "hinted": {}}


def save(session_id, st):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, session_id + ".json"), "w", encoding="utf-8") as f:
            json.dump(st, f)
    except OSError:
        pass


def prune():
    try:
        limit = time.time() - TTL_DAYS * 86400
        for fn in os.listdir(STATE_DIR):
            p = os.path.join(STATE_DIR, fn)
            if os.path.getmtime(p) < limit:
                os.remove(p)
    except OSError:
        pass


def matches(prompt, items):
    low = prompt.lower()
    out = []
    for t in items:
        hit = [k for k in t["keywords"] if len(k) >= 3 and k in low]
        if hit:
            out.append((t, hit))
    return out


def main():
    if os.environ.get("HANDOFF_OFF") == "1":
        return 0
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("hook_event_name") != "UserPromptSubmit":
        return 0
    prompt = (payload.get("prompt") or "").strip()
    sid = payload.get("session_id") or ""
    if len(prompt) < MIN_LEN or prompt.startswith("/") or not SAFE_ID.match(sid):
        return 0
    items = hl.active(payload.get("cwd"))
    if not items:
        return 0
    st = load(sid)
    st["n"] += 1
    lines = []
    for t, hit in matches(prompt, items):
        last = st["hinted"].get(t["name"])
        if last is not None and st["n"] - last < REHINT_AFTER:
            continue
        st["hinted"][t["name"]] = st["n"]
        lines.append(f"Тема совпала с потоком «{t['name']}» ({', '.join(hit[:3])}): открой {t['path']}, если не читал его в этой сессии.")
    save(sid, st)
    prune()
    if not lines:
        return 0
    out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "\n".join(lines)}}
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - fail-open
        sys.exit(0)
