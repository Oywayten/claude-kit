"""Мёржит хуки из settings.hooks.template.json в settings.json пользователя.

Аргументы: <settings.json> <шаблон> <python-команда> <папка hooks>
Уже существующие хуки (по тексту команды) не дублируются. Остальные ключи settings.json не трогаются.
"""
import json
import os
import sys

settings_path, template_path, py, hooks_dir = sys.argv[1:5]

settings = {}
if os.path.isfile(settings_path):
    with open(settings_path, encoding="utf-8") as f:
        settings = json.load(f)

with open(template_path, encoding="utf-8") as f:
    template = json.load(f)["hooks"]

hooks = settings.setdefault("hooks", {})
added = 0
for event, groups in template.items():
    cur = hooks.setdefault(event, [])
    known = {h.get("command") for g in cur for h in g.get("hooks", [])}
    for g in groups:
        new_hooks = []
        for h in g["hooks"]:
            h = dict(h)
            h["command"] = h["command"].replace("@PY@", py).replace("@HOOKS@", hooks_dir)
            if h["command"] not in known:
                new_hooks.append(h)
                known.add(h["command"])
        if new_hooks:
            g2 = dict(g)
            g2["hooks"] = new_hooks
            cur.append(g2)
            added += len(new_hooks)

with open(settings_path, "w", encoding="utf-8") as f:
    json.dump(settings, f, ensure_ascii=False, indent=2)
print(f"хуков добавлено: {added}")
