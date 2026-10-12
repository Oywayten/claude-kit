# RAG-система для AiderDesk

## Многоязычная семантическая база знаний на PostgreSQL + pgvector

---

## Обзор

RAG-система (Retrieval-Augmented Generation), подключаемая к AiderDesk через MCP-протокол. Позволяет агенту AiderDesk при ответе на вопросы пользователя семантически искать релевантные фрагменты из локальной базы знаний (Markdown-документы) и использовать их как контекст.

### Архитектура

```
┌──────────────────────────────────────────────────────┐
│                    AiderDesk                          │
│                                                      │
│  User query ──► MCP tool call ──► rag_search(query)  │
│                         │                            │
│                         ▼                            │
│              ┌─────────────────────┐                 │
│              │  mcp_rag_server.py  │ (Docker)        │
│              │  • encode query     │                 │
│              │  • pgvector SQL     │                 │
│              │  • return top-k     │                 │
│              └────────┬────────────┘                 │
│                       │                              │
│              ┌────────▼────────────┐                 │
│              │   PostgreSQL 18     │ (Docker)        │
│              │   + pgvector        │                 │
│              │   • documents       │                 │
│              │   • chunks          │                 │
│              │   • HNSW index      │                 │
│              └─────────────────────┘                 │
│                                                      │
│  index.py ──► md files ──► chunking ──► embeddings   │
│               (input_material/)   ──► pgvector       │
└──────────────────────────────────────────────────────┘
```

### Ключевые особенности

- **Мультиязычная модель** `BAAI/bge-m3` — 1024 dims, 100+ языков (русский, английский, CJK и др.)
- **HNSW индекс** — стабильный для малых и средних датасетов, не требует обучения
- **Durable-память (Memory)** — аналог Memory из AiderDesk: агент сохраняет и ищет долгосрочные заметки (решения, паттерны, предпочтения) через `store_memory` / `retrieve_memory` / `list_memories` / `update_memory` / `delete_memory`
- **Полностью локально** — pgvector (PostgreSQL в docker-сети) + локальная модель эмбеддингов с кэшем на диске; после первого запуска работает без интернета
- **Инкрементальная индексация** — `content_hash` (SHA-256) пропускает неизменённые файлы
- Глобальная дедупликация: одинаковый контент (SHA-256 content_hash) под разными путями индексируется один раз (UNIQUE content_hash в init.sql)
- Дедупликация на выдаче: exact-match по тексту чанка, выборка с запасом top_k × DEDUP_FACTOR
- **Connection pooling** — `psycopg.pool.ConnectionPool` для эффективного управления соединениями
- **MCP stdio** — стандартный протокол для интеграции с AiderDesk / OpenCode
- **heading_path** — извлекается из метаданных чанков для контекстного отображения результатов

---

## Durable-память (Memory)

Помимо поиска по документам, тот же MCP-сервер предоставляет долгосрочную
память для агента — заметки, которые переживают отдельные сессии:

| Инструмент | Назначение |
|---|---|
| `store_memory` | Сохранить заметку (решение / паттерн / предпочтение / общее) |
| `retrieve_memory` | Семантический поиск релевантных заметок по запросу |
| `list_memories` | Список заметок (фильтры по `project` и `memory_type`) |
| `update_memory` | Обновить содержимое заметки по ID (пере-эмбеддинг) |
| `delete_memory` | Удалить заметку по ID |

### Рекомендуемый workflow агента

- **В начале задачи** → `retrieve_memory(query)` — вспомнить контекст прошлых сессий.
- **В конце задачи** → `store_memory(content, memory_type, project)` — сохранить
  устойчивые результаты (предпочтения, архитектурные решения, паттерны).
- Не сохранять: разовые статусы, временные заметки, логи, секреты.

### Хранение

Заметки лежат в таблице `memories` (`VECTOR(1024)` + HNSW-индекс), эмбеддинги
считаются той же локальной моделью `BAAI/bge-m3`. Таблица создаётся
автоматически при старте сервера (`CREATE TABLE IF NOT EXISTS`) и в `init.sql`
для чистых установок.

---

## Предварительные требования

| Компонент | Минимальная версия | Заметки |
|---|---|---|
| Docker + Docker Compose | 24.x | Для запуска PostgreSQL и MCP-сервера |
| Python | 3.10+ | Для `index.py` и `mcp_rag_server.py` |
| AiderDesk | latest | С поддержкой MCP-серверов |
| RAM | 4 GB+ | Модель `bge-m3` ~1.2 GB при загрузке |
| Диск | 2 GB | Docker-образы + pgvector |

---

## Пошаговое руководство по установке

### Шаг 1: Поднять PostgreSQL

```bash
# Перейти в каталог проекта
cd path/to/rag-project

# Запустить PostgreSQL + pgvector
docker compose up -d

# Проверить, что контейнер работает
docker compose ps
```

### Шаг 2: Проверить подключение

```bash
# Подключиться к PostgreSQL
docker exec -it rag-postgres psql -U postgres -d rag_docs

# В psql выполнить:
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';
\q
```

Должно вывести: `vector | 0.8.0` (или выше).

### Шаг 3: Установить зависимости и запустить индексацию

```bash
# Создать виртуальное окружение
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/Mac

# Установить зависимости
pip install -r requirements.txt

# Разместите ваши MD-файлы в input_material/

# Запустить индексацию
python index.py

# Примечание: все команды `python` ниже предполагают активированное .venv.
# Если окружение не активировано, замените python на .\.venv\Scripts\python.exe

# Проверить количество записей
docker exec -it rag-postgres psql -U postgres -d rag_docs -c "SELECT count(*) FROM chunks;"
```

### Шаг 4: Собрать Docker-образ MCP-сервера

```bash
cd mcp-rag-server
docker build -t mcp-rag-pgvector .
cd ..
```

### Шаг 5: Добавить MCP-сервер в AiderDesk

1. Откройте настройки MCP-серверов в AiderDesk.
2. Добавьте конфигурацию `pgvector-rag` из файла `.aider/mcpServers.json`:
   ```json
   {
       "mcpServers": {
           "pgvector-rag": {
               "command": "docker",
               "args": [
                   "run", "-i", "--rm",
                   "--network", "rag-network",
                   "-e", "DATABASE_URL=postgresql://postgres:postgres@postgres:5432/rag_docs",
                   "-e", "EMBED_MODEL=BAAI/bge-m3",
                   "-e", "TOP_K_DEFAULT=5",
                   "-e", "LOG_LEVEL=INFO",
                   "mcp-rag-pgvector"
               ]
           }
       }
   }
   ```
3. Перезапустите AiderDesk (или перезагрузите MCP-серверы).

### Шаг 6: Проверить работу

В AiderDesk задайте вопрос, например:
> «Какие паттерны используются для проектирования микросервисов?»

AiderDesk должен вызвать инструмент `rag_search` и вернуть релевантные фрагменты из ваших документов.

### Шаг 7: Переиндексация (при обновлении документов)

```bash
# Активировать окружение (если ещё не активировано):
.\.venv\Scripts\Activate.ps1        # Windows PowerShell
# source .venv/bin/activate         # Linux/Mac

# Инкрементальная индексация (пропуск неизменённых файлов по content_hash)
python index.py

# Полная переиндексация (пере-чанкинг + пере-эмбеддинг)
# --force для файла с уже существующим content_hash (под другим путём)
# пересчитывает эмбеддинги каноничной записи, НЕ создаёт дубль
python index.py --force

# Пересчёт эмбеддингов без пере-чанкинга (например, после смены модели)
python index.py --rebuild-embeddings
```

Примечание: файлы-копии с одинаковым содержимым автоматически индексируются один раз (дедуп по `content_hash`).

---

## Переменные окружения

### `index.py`

| Переменная | На что влияет | Дефолт | Пример | Диапазон возможных значений |
|---|---|---|---|---|
| `DB_HOST` | Хост PostgreSQL | `localhost` | `DB_HOST=db.local` | hostname / IP-адрес |
| `DB_PORT` | Порт PostgreSQL | `5432` | `DB_PORT=5433` | 1–65535 |
| `DB_NAME` | Имя базы данных | `rag_docs` | `DB_NAME=rag_docs` | строка |
| `DB_USER` | Пользователь PostgreSQL | `postgres` | `DB_USER=postgres` | строка |
| `DB_PASSWORD` | Пароль PostgreSQL | `postgres` | `DB_PASSWORD=secret` | строка |
| `EMBED_MODEL` | Модель sentence-transformers для эмбеддингов. Размерность должна совпадать с `VECTOR(1024)` в `init.sql` | `BAAI/bge-m3` | `EMBED_MODEL=BAAI/bge-m3` | любая модель ST с размерностью 1024 |
| `INPUT_DIR` | Каталог с индексируемыми MD/PDF-файлами | `D:\projects\4j\job4j_questions\07_system_design\03_my_system_design_prepare\input_material` | `INPUT_DIR=input_material` | путь к каталогу |
| `TRANSLATIONS_DIR` | Каталог кэша RU-переводов EN-файлов (файл `{content_hash}.ru.md`) | `D:\projects\aider_desk_rag\rag-project\translations_cache` | `TRANSLATIONS_DIR=translations_cache` | путь к каталогу |
| `AUTO_TRANSLATE` | Авто-перевод EN-MD-файлов перед индексацией | `1` (включён) | `AUTO_TRANSLATE=0` | `1` / `0` / `false` / `no` / пусто (выкл.) |
| `LOG_LEVEL` | Уровень логирования | `INFO` | `LOG_LEVEL=DEBUG` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `DOCLING_NUM_THREADS` | Число потоков docling при конвертации PDF (передаётся в `AcceleratorOptions(num_threads=...)`) | `2` | `DOCLING_NUM_THREADS=2` | 1..число ядер CPU (≥1) |
| `OMP_NUM_THREADS` | Потоки OpenMP/torch. Если не задана — выставляется равной `DOCLING_NUM_THREADS` | равна `DOCLING_NUM_THREADS` | `OMP_NUM_THREADS=2` | 1..число ядер CPU (≥1) |

### `mcp_rag_server.py`

| Переменная | На что влияет | Дефолт | Пример | Диапазон возможных значений |
|---|---|---|---|---|
| `DATABASE_URL` | DSN PostgreSQL (`postgresql://user:pass@host:port/db`) | — (обязательная) | `DATABASE_URL=postgresql://postgres:postgres@localhost:5432/rag_docs` | строка DSN |
| `EMBED_MODEL` | Модель sentence-transformers для эмбеддингов | `BAAI/bge-m3` | `EMBED_MODEL=BAAI/bge-m3` | любая модель ST с размерностью 1024 |
| `TOP_K_DEFAULT` | Количество результатов поиска по умолчанию | `5` | `TOP_K_DEFAULT=5` | целое ≥1 |
| `POOL_MIN` | Минимальный размер пула соединений | `1` | `POOL_MIN=1` | целое ≥1 |
| `POOL_MAX` | Максимальный размер пула соединений | `3` | `POOL_MAX=3` | целое ≥1 |
| `LOG_LEVEL` | Уровень логирования | `INFO` | `LOG_LEVEL=DEBUG` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `MAX_DISTANCE` | Порог косинусного расстояния: результаты с `distance > порога` отсекаются после выборки из БД | `0.55` | `MAX_DISTANCE=0.6` | число в [0, 2] |
| `MEMORY_MAX_DISTANCE` | Порог косинусного расстояния для `retrieve_memory` | `1.2` | `MEMORY_MAX_DISTANCE=1.0` | число в [0, 2] |
| DEDUP_FACTOR | Множитель выборки с запасом перед дедупликацией по тексту в rag_search (SQL LIMIT = top_k × factor) | 2.0 | DEDUP_FACTOR=2.0 | [1, 10], дробные допустимы |

### Eval-скрипты (`eval/`)

| Переменная | На что влияет | Дефолт | Пример | Диапазон возможных значений |
|---|---|---|---|---|
| `TRANSLATION_MODEL_DIR` | Каталог модели перевода NLLB (`translate_files.py`) | `eval/model_nllb` | `TRANSLATION_MODEL_DIR=D:\models\nllb` | путь к каталогу |
| `LIMIT` | Ограничение числа обрабатываемых файлов при переводе — тестовый «рубильник» (`translate_files.py`) | `0` (без ограничения) | `LIMIT=5` | целое ≥0 |
| `DATABASE_URL` | DSN PostgreSQL для eval-скриптов (`measure_baseline.py`, `backfill_lang.py`) | `postgresql://postgres:postgres@localhost:5432/rag_docs` | `DATABASE_URL=postgresql://postgres:postgres@localhost:5432/rag_docs` | строка DSN |
| `EMBED_MODEL` | Модель эмбеддингов в `measure_baseline.py` | `BAAI/bge-m3` | `EMBED_MODEL=BAAI/bge-m3` | любая модель ST с размерностью 1024 |

---

## Диагностика проблем

| Симптом | Решение |
|---|---|
| `DATABASE_URL environment variable is not set` | Проверьте, что `-e DATABASE_URL=...` передаётся в docker run |
| `password authentication failed` | Пароль в `DATABASE_URL` должен совпадать с `POSTGRES_PASSWORD` в docker-compose |
| `relation "chunks" does not exist` | Таблицы создаются через `init.sql` автоматически при первом запуске `docker compose up`. Если образ PostgreSQL был создан без `init.sql`, выполните вручную: `docker exec -i rag-postgres psql -U postgres -d rag_docs < init.sql`. Также проверьте, что `init.sql` примонтирован в `/docker-entrypoint-initdb.d/`. |
| Пустые результаты поиска | Убедитесь, что `python index.py` отработал успешно; проверьте `SELECT count(*) FROM chunks` |
| MCP-сервер не стартует | Проверьте логи: `docker logs <container_name>`; убедитесь, что образ `mcp-rag-pgvector` собран |
| Медленная индексация | Нормально для первого запуска (загрузка модели ~30 сек). Повторные запуски быстрее (кэш модели) |
| `psycopg.OperationalError: connection refused` | PostgreSQL ещё не готов; подождите или проверьте `docker compose logs postgres` |
| В выдаче дубли текста из файлов-копий | Проверить дубли в БД: SELECT content_hash, count(*) FROM documents GROUP BY content_hash HAVING count(*) > 1; для существующих БД прогнать python eval/migrate_dedup_content_hash.py |

---

## Структура проекта

```
rag-project/
├── docker-compose.yml          # Конфигурация PostgreSQL + pgvector
├── input_material/             # Каталог с MD-файлами (пользовательские данные)
├── index.py                    # Индексатор документов
├── requirements.txt            # Зависимости для index.py
├── init.sql                    # SQL-скрипт инициализации БД
├── mcp-rag-server/
│   ├── Dockerfile              # Dockerfile для MCP-сервера
│   ├── requirements.txt        # Зависимости для MCP-сервера
│   └── mcp_rag_server.py       # MCP-сервер (stdio)
└── README.md                   # Этот файл
```

---

## Лицензия

Для внутреннего использования.
