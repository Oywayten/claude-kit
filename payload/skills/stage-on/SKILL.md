---
name: stage-on
description: Включить подстановку файла этапа проекта (.claude/stage.md) в текущей сессии. Только по прямой команде пользователя /stage-on.
disable-model-invocation: true
---

Команду выполняет хук `stage_context.py` (UserPromptSubmit). Его результат уже есть в контексте
этого сообщения: блок «Команду /stage-on уже выполнил хук stage_context. Результат: …».

Ответь пользователю ровно этим результатом. Без инструментов, без пояснений.

Если такого блока в контексте нет, ответь одной строкой: «Хук stage_context не сработал:
проверьте раздел UserPromptSubmit в ~/.claude/settings.json».
