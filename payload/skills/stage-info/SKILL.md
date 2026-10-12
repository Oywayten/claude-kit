---
name: stage-info
description: Показать содержимое файла этапа проекта (.claude/stage.md). Только по прямой команде пользователя /stage-info.
disable-model-invocation: true
---

Команду выполняет хук `stage_context.py` (UserPromptSubmit). Его результат уже есть в контексте
этого сообщения: блок «Команду /stage-info уже выполнил хук stage_context. Результат: …».

Ответь пользователю ровно этим результатом. Без инструментов, без пояснений.

Если такого блока в контексте нет, ответь одной строкой: «Хук stage_context не сработал:
проверьте раздел UserPromptSubmit в ~/.claude/settings.json».
