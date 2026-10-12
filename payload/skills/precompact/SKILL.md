---
name: precompact
description: >
  Flush important session context to persistent storage right before the user runs /compact
  manually. Use when the user says "подготовься к компакции", "prepare for compaction",
  "/precompact", or otherwise asks to get ready before compacting/clearing context. This is the
  live session doing the work itself (not a sub-agent reading a cold transcript) — it only works
  invoked mid-conversation, before compaction happens.
---

## Purpose

Right before the user manually runs `/compact`, do one deliberate pass over the CURRENT
conversation and persist anything that would be expensive or impossible to reconstruct from a
compacted summary alone: exact decisions made, file paths and line numbers, commit hashes,
rejected alternatives and why, open/pending items, and any nuance a generic auto-summary would
likely flatten or drop.

This is not the same as ambient "note things down as you go" — it is a dedicated, explicit sweep
triggered right before a known compaction, so treat it as a checklist to run through now, not a
vague reminder.

## What to do when invoked

1. **Check for this project's own memory system first.** If a directory like
   `~/.claude/projects/<project-id>/memory/` with a `MEMORY.md` index exists (check the system
   prompt / prior tool use in this conversation for the exact path already in use this session —
   do not guess a new path), that is the right place: update the relevant existing memory file(s)
   (or add a new one, then add its index line to `MEMORY.md`) following whatever type taxonomy
   that system already defines (e.g. user/feedback/project/reference). Prefer editing an existing
   memory file over creating a near-duplicate one.
2. **If no such memory system is present in this environment/session**, write to a project-local
   fallback instead: `.claude/pre-compact-notes/<YYYY-MM-DD-HHmm>.md` in the current working
   directory (create the folder if missing). Use plain, dense prose — this file is read by a
   future Claude session recovering context, not a human skimming casually.
3. **Content to capture, in priority order** (skip a bullet if genuinely nothing new since the
   last save):
   - Decisions made this session that aren't yet written anywhere else (with the *why*, not just
     the *what* — reasoning is the part a summary tends to compress away first).
   - Concrete state: commit hashes, file paths edited, branch name, what's pushed vs. local-only.
   - Anything explicitly deferred, rejected, or left open, and why — so it isn't silently
     re-litigated or silently dropped next turn.
   - Corrections or preferences the user gave this session that aren't yet in a feedback-type
     memory (if this project has one).
   - Exact next step, if one was agreed but not yet started.
4. **Do not pad.** If the session's substance is already fully captured in existing memory files
   (check before writing), say so plainly instead of writing filler — the point is recall
   fidelity, not activity.
5. **Finish with a short confirmation to the user**, in whatever language they've been using this
   session: what was saved (or that nothing new needed saving), and that it's now safe to run
   `/compact`. Do not attempt to trigger `/compact` yourself — there is no tool for that; the user
   runs it manually right after this.
