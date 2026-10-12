# claude-kit

Набор хуков, скиллов и RAG для Claude Code.

## Установка

```bash
git clone https://github.com/Oywayten/claude-kit.git
cd claude-kit
bash install.sh            # хуки, скиллы, команды, агенты
bash install.sh --with-rag # то же + база pgvector и MCP-сервер (нужен docker)
```

Нужны: python 3 в PATH, bash (на Windows — Git Bash). Для RAG ещё docker и `claude` CLI.
Скрипт можно запускать повторно: заменяемое он сначала копирует в `~/.claude/backups/kit-<время>/`.
`settings.json` не перезаписывается: хуки добавляются в него без дублей, остальные ключи целы.

## Что внутри

| Часть | Что делает |
|---|---|
| handoff (хуки `handoff_*`, `context_handoff_guard`, скилл `handoff`) | Потоки работы в `.claude/handoff/<поток>.md`. На старте сессии хук показывает активные потоки, при сжатии контекста просит записать состояние. Обновить: `/handoff`. |
| memory-cleanup (скилл, хуки `memory_cleanup_reminder`, `memory_index_guard`, `memory_size_guard`) | Чистка файловой памяти проекта, напоминание на старте, проверка индекса `MEMORY.md`. Звать: `/memory-cleanup`. |
| stage (скиллы `stage-*`, хуки `stage_*`, `source_guard`, `absence_claim_guard`) | Этап проекта в `<repo>/.claude/stage.md`: список «не источников», вбрасывается в контекст. Команды `/stage-on`, `/stage-off`, `/stage-status`, `/stage-info`, `/stage-allow`, `/stage-disallow`. |
| RAG (хук `rag_guard.py`, папка `rag/`) | Хук-страж для MCP `pgvector-rag` + код базы и сервера. |
| oh-my-god, caveman | Правило доказательств и сжатый стиль ответов. Хук SessionStart просит загрузить их в каждой сессии, `skills_loaded_guard` проверяет. |
| Остальные хуки | `commit_guard`, `git_state_guard`, `java_edit_guard`, `analysis_script_guard`, `check_steps_guard`, `doc_fact_guard`, `mermaid_render_guard`, `index_coverage_guard`, `promise_wakeup_guard`, `unexplained_label_guard`. |
| `orchestrate` + агенты `orch-*` | Оркестрация задач через субагентов. |

## Что подогнать под себя

- `~/.claude/CLAUDE.md.example` — личные правила автора (Java, русский текст, имена классов). Сам не подключается. Возьми нужное в свой `CLAUDE.md`.
- Хук SessionStart заставляет отвечать по-русски стилем caveman ultra. Не нужно — удали первую команду из `SessionStart` в `settings.json` и хук `skills_loaded_guard.py` из `Stop` и `PreToolUse`.
- `java_edit_guard` работает только на Java-файлах, `mermaid_render_guard` — на диаграммах Mermaid и может требовать установленный renderer.
- Хуки stage, absence и doc_fact включаются только в проектах, где есть `.claude/stage.md` или `tasks_src/INDEX.md`.
- Мастер-чек-лист автора (`MASTER_CHECK_LIST.md`) в набор не входит.

## RAG

`bash install.sh --with-rag` кладёт код в `~/claude-rag`, поднимает `rag-postgres` (pgvector), собирает образ `mcp-rag-pgvector` и регистрирует MCP `pgvector-rag` через `claude mcp add`. База пустая.

Модель `BAAI/bge-m3` нужна для эмбеддингов (индексация и поиск), сам чанкинг идёт по заголовкам и длине текста, без модели. Модель качается с HuggingFace сама при первом обращении, около 2 ГБ, и лежит в `~/.cache/huggingface`. Её качают два разных процесса:

- MCP-контейнер: при первом поиске. Папка кэша примонтирована в контейнер, поэтому повторно не качается.
- `python index.py` на хосте: при первой индексации. Нужны venv и `pip install -r requirements.txt` (torch, docling), как в `~/claude-rag/README.md`. Ставит их не `install.sh`, а ты вручную.

Свои документы клади в `~/claude-rag/input_material/` и запускай `python index.py`.
Пароль БД `postgres/postgres` годится только для локального запуска.

## Проверка после установки

1. Перезапусти Claude Code.
2. В новой сессии первым делом должны вызваться скиллы `oh-my-god` и `caveman`.
3. `/handoff`, `/memory-cleanup`, `/stage-status` есть в списке команд.
