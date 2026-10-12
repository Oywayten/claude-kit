#!/usr/bin/env python3
"""Compact list of real tool calls of a Claude Code session or subagent — for checking
claims («прогнал тесты», «проверил») against what was actually executed.

usage: tool_calls.py [--cwd DIR] [--session ID] [--agent AGENT_ID] [--since ISO] [--limit N]
  --cwd      project dir; transcripts live in ~/.claude/projects/<cwd with : \\ / replaced by ->/
  --session  session id; default: the most recently modified main transcript of that project
  --agent    subagent id: read <session>/subagents/agent-<id>.jsonl instead of the main transcript
  --since    only calls at or after this ISO timestamp (e.g. 2026-10-04T18:00:00Z)
Output: one line per call: time | tool | input summary | result (ERROR + first chars if failed).
"""
import argparse
import glob
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


def project_dir(cwd):
    name = re.sub(r"[:\\/]", "-", os.path.abspath(cwd))
    return os.path.join(os.path.expanduser("~/.claude/projects"), name)


def transcript_path(cwd, session, agent):
    pdir = project_dir(cwd)
    if not session:
        mains = glob.glob(os.path.join(pdir, "*.jsonl"))
        if not mains:
            sys.exit(f"нет транскриптов в {pdir}")
        session = os.path.splitext(os.path.basename(max(mains, key=os.path.getmtime)))[0]
    if agent:
        return os.path.join(pdir, session, "subagents", f"agent-{agent}.jsonl")
    return os.path.join(pdir, session + ".jsonl")


def summary(name, inp):
    for key in ("command", "file_path", "pattern", "skill", "url", "description"):
        if inp.get(key):
            return f"{key}={str(inp[key])[:160]}"
    return json.dumps(inp, ensure_ascii=False)[:160]


def result_text(content):
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return re.sub(r"\s+", " ", str(content or ""))[:120]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--session")
    ap.add_argument("--agent")
    ap.add_argument("--since", default="")
    ap.add_argument("--limit", type=int, default=400)
    a = ap.parse_args()
    path = transcript_path(a.cwd, a.session, a.agent)
    if not os.path.isfile(path):
        sys.exit(f"нет файла {path}")
    calls, results = [], {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            ts = o.get("timestamp") or ""
            for c in (o.get("message") or {}).get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use" and ts >= a.since:
                    calls.append((ts, c.get("id"), c.get("name"), summary(c.get("name"), c.get("input") or {})))
                elif c.get("type") == "tool_result":
                    results[c.get("tool_use_id")] = (bool(c.get("is_error")), result_text(c.get("content")))
    print(f"# {path}\n# вызовов: {len(calls)}" + (f" с {a.since}" if a.since else ""))
    for ts, uid, name, summ in calls[-a.limit:]:
        err, text = results.get(uid, (None, ""))
        res = "нет результата" if err is None else ("ERROR " + text if err else text)
        print(f"{ts[11:19]} | {name} | {summ} | {res}")


if __name__ == "__main__":
    main()
