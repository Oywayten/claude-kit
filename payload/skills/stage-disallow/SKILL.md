---
name: stage-disallow
description: Вернуть запрет на пункт из «Не источники» файла этапа в текущей сессии; без имени — показать список. Только по прямой команде пользователя /stage-disallow.
argument-hint: "[имя]"
disable-model-invocation: true
---

Команду выполняет хук `stage_context.py` (UserPromptSubmit). Его результат уже есть в контексте
этого сообщения: блок «Команду /stage-disallow уже выполнил хук stage_context. Результат: …».

Ответь пользователю ровно этим результатом. Без инструментов, без пояснений.

Если такого блока в контексте нет, ответь одной строкой: «Хук stage_context не сработал:
проверьте раздел UserPromptSubmit в ~/.claude/settings.json».
