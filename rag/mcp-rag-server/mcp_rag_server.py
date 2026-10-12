#!/usr/bin/env python3
"""
MCP-сервер для семантического поиска по pgvector + durable-память.

Предоставляет инструменты через stdio-транспорт:
    rag_search        — семантический поиск по базе знаний (чанки документов)
    store_memory      — сохранить durable-заметку (решение/паттерн/предпочтение)
    retrieve_memory   — семантический поиск по durable-заметкам
    list_memories     — список сохранённых заметок (фильтры по проекту/типу)
    update_memory     — обновить содержимое заметки (пере-эмбеддинг)
    delete_memory     — удалить заметку по ID

Память работает полностью локально: векторная БД — pgvector (PostgreSQL в
docker-сети), эмбеддинги — локальная модель sentence-transformers (кэш модели
на диске; повторные запуски без выхода в интернет).

Переменные окружения:
    DATABASE_URL        — DSN PostgreSQL (postgresql://user:pass@host:port/db)
    EMBED_MODEL         — имя модели sentence-transformers (по умолчанию BAAI/bge-m3)
    TOP_K_DEFAULT       — количество результатов по умолчанию (5)
    MAX_DISTANCE        — порог косинусного расстояния для rag_search
                          (по умолчанию 0.60, диапазон [0, 2])
    MEMORY_MAX_DISTANCE — порог косинусного расстояния для retrieve_memory
                          (по умолчанию 1.2, диапазон [0, 2])
    SEARCH_DEFAULT_PATHS — префиксы путей документов для rag_search через
                          запятую (по умолчанию "tasks_src/,video-analysis/")
"""

import os
import re
import sys
import json
import math
import signal
import logging
from collections import Counter
from typing import Optional
from urllib.parse import urlparse, urlunparse

import psycopg
from psycopg_pool import ConnectionPool
from sentence_transformers import SentenceTransformer

from mcp.server.fastmcp import FastMCP

# ─── Конфигурация ─────────────────────────────────────────────────────────────

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,  # stdout зарезервирован для MCP stdio
)
log = logging.getLogger("mcp-rag-server")

DATABASE_URL = os.getenv("DATABASE_URL")
EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
TOP_K_DEFAULT = int(os.getenv("TOP_K_DEFAULT", "5"))
POOL_MIN = int(os.getenv("POOL_MIN", "1"))
POOL_MAX = int(os.getenv("POOL_MAX", "3"))

# MAX_DISTANCE — порог косинусного расстояния (метрика <=>). Результаты с
# distance > порога отсекаются ПОСЛЕ выборки из БД. Порог нельзя проталкивать
# в SQL-запрос: HNSW index scan по векторной метрике не гарантирует корректной
# фильтрации дополнительным предикатом (и может отключить использование
# индекса), поэтому фильтруем в Python.
#
# Валидация: значение должно быть числом в диапазоне [0, 2] (косинусное
# расстояние лежит в [0, 2]). При невалидном значении логируем warning и
# используем дефолт 0.60. Clamp не применяем: значение вне [0, 2] — это ошибка
# конфигурации, безопаснее вернуться к дефолту, чем молча обрезать.
MAX_DISTANCE_DEFAULT = 0.60

# LEXICAL_DISTANCE_MARGIN — запас порога для чанков, найденных только лексикой:
# они проходят при distance <= MAX_DISTANCE + запас. Так точный термин не теряется,
# а запрос не по теме не набирает выдачу случайными совпадениями слов.
LEXICAL_DISTANCE_MARGIN = 0.10

# NEAREST_DISTANCE_LIMIT — ворота по ближайшему чанку: если даже он дальше этого
# расстояния, запрос не по теме базы и rag_search возвращает пустой список.
# Порог по чанку (MAX_DISTANCE) мусор не отсекает: у bge-m3 расстояния до
# нерелевантного текста лежат около 0.55-0.69, у релевантных запросов ближайший
# чанк обычно не дальше 0.48 (замер: verification/2026-10-11-rag-distance-dist.txt).
NEAREST_DISTANCE_LIMIT = 0.52

# DEDUP_FACTOR — множитель для выборки с запасом перед дедупликацией.
# SQL LIMIT = top_k * DEDUP_FACTOR, затем дедуп по точному совпадению text
# (первый по расстоянию побеждает), затем усечение до top_k.
# Дефолт 2.0 — достаточен для типичных дублей; можно увеличить при плотных дублях.
DEDUP_FACTOR_DEFAULT = 2.0


def _parse_max_distance() -> float:
    """Читает MAX_DISTANCE из окружения и валидирует его."""
    raw = os.getenv("MAX_DISTANCE")
    if raw is None:
        return MAX_DISTANCE_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        log.warning(
            f"MAX_DISTANCE='{raw}' is not a number; "
            f"using default {MAX_DISTANCE_DEFAULT}"
        )
        return MAX_DISTANCE_DEFAULT
    if not 0.0 <= value <= 2.0:
        log.warning(
            f"MAX_DISTANCE={value} is outside valid range [0, 2]; "
            f"using default {MAX_DISTANCE_DEFAULT}"
        )
        return MAX_DISTANCE_DEFAULT
    return value


MAX_DISTANCE = _parse_max_distance()


def _parse_dedup_factor() -> float:
    """Читает DEDUP_FACTOR из окружения и валидирует его."""
    raw = os.getenv("DEDUP_FACTOR")
    if raw is None:
        return DEDUP_FACTOR_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        log.warning(
            f"DEDUP_FACTOR='{raw}' is not a number; "
            f"using default {DEDUP_FACTOR_DEFAULT}"
        )
        return DEDUP_FACTOR_DEFAULT
    if not 1.0 <= value <= 10.0:
        log.warning(
            f"DEDUP_FACTOR={value} is outside valid range [1, 10]; "
            f"using default {DEDUP_FACTOR_DEFAULT}"
        )
        return DEDUP_FACTOR_DEFAULT
    return value


DEDUP_FACTOR = _parse_dedup_factor()

# MEMORY_MAX_DISTANCE — порог косинусного расстояния для retrieve_memory.
# Заметки памяти короче и свободнее сформулированы, чем чанки документов,
# поэтому порог по умолчанию мягче, чем MAX_DISTANCE для rag_search.
# Валидация аналогична MAX_DISTANCE: число в [0, 2], иначе дефолт 1.2.
MEMORY_MAX_DISTANCE_DEFAULT = 1.2


def _parse_memory_max_distance() -> float:
    """Читает MEMORY_MAX_DISTANCE из окружения и валидирует его."""
    raw = os.getenv("MEMORY_MAX_DISTANCE")
    if raw is None:
        return MEMORY_MAX_DISTANCE_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        log.warning(
            f"MEMORY_MAX_DISTANCE='{raw}' is not a number; "
            f"using default {MEMORY_MAX_DISTANCE_DEFAULT}"
        )
        return MEMORY_MAX_DISTANCE_DEFAULT
    if not 0.0 <= value <= 2.0:
        log.warning(
            f"MEMORY_MAX_DISTANCE={value} is outside valid range [0, 2]; "
            f"using default {MEMORY_MAX_DISTANCE_DEFAULT}"
        )
        return MEMORY_MAX_DISTANCE_DEFAULT
    return value


MEMORY_MAX_DISTANCE = _parse_memory_max_distance()

# SEARCH_DEFAULT_PATHS — префиксы путей документов, по которым rag_search ищет,
# если параметр paths не задан. Значение "*" (в переменной или в paths)
# отключает фильтр: поиск по всей базе.
SEARCH_DEFAULT_PATHS_DEFAULT = "tasks_src/,video-analysis/"
SEARCH_ALL = "*"

# CANDIDATES — сколько кандидатов берём с каждой стороны гибрида (dense и
# лексика) до слияния RRF; RRF_K — константа RRF; DENSE_WEIGHT — вес dense-
# списка против лексического (вес 1). Вес 2 и RRF_K=10 подобраны так, чтобы
# чанк из верхушки dense не вытеснялся лексикой за пределы топ-5.
CANDIDATES = 50
RRF_K = 10
DENSE_WEIGHT = 2.0


def _parse_search_paths(raw: str) -> list:
    """Разбирает список префиксов через запятую; пустые элементы отбрасывает."""
    return [p.strip() for p in raw.split(",") if p.strip()]


SEARCH_DEFAULT_PATHS = (
    _parse_search_paths(os.getenv("SEARCH_DEFAULT_PATHS", ""))
    or _parse_search_paths(SEARCH_DEFAULT_PATHS_DEFAULT)
)

# ─── Валидация ────────────────────────────────────────────────────────────────

if not DATABASE_URL:
    log.error("DATABASE_URL environment variable is not set!")
    log.error("Expected format: postgresql://user:pass@host:port/db")
    sys.exit(1)

# Проверяем формат DSN (не должен содержать +psycopg или +asyncpg)
if "+" in DATABASE_URL.split("@")[0].split("//")[-1]:
    log.warning(
        "DATABASE_URL contains '+dialect' modifier. "
        "psycopg uses plain 'postgresql://user:pass@host:port/db'. "
        "The modifier will be ignored."
    )

# ─── MCP-сервер ───────────────────────────────────────────────────────────────

mcp = FastMCP(
    "pgvector-rag",
    instructions="Semantic search tool for RAG knowledge base. Use rag_search to find relevant document chunks.",
)

# ─── Глобальные ресурсы (ленивая инициализация) ───────────────────────────────

_model: Optional[SentenceTransformer] = None
_pool: Optional[ConnectionPool] = None


def get_model() -> SentenceTransformer:
    """Ленивая загрузка модели эмбеддингов."""
    global _model
    if _model is None:
        log.info(f"Loading embedding model: {EMBED_MODEL}")
        _model = SentenceTransformer(EMBED_MODEL)
        dim = _model.get_sentence_embedding_dimension()
        log.info(f"Model loaded. Dimension: {dim}")
        # Проверка совместимости размерности с init.sql
        EXPECTED_DIM = 1024  # VECTOR(1024) в init.sql
        if dim != EXPECTED_DIM:
            log.error(
                f"Model dimension mismatch: model produces {dim}-dim vectors, "
                f"but init.sql expects VECTOR({EXPECTED_DIM}). "
                f"Update init.sql or change EMBED_MODEL."
            )
            sys.exit(1)
    return _model


def get_pool() -> ConnectionPool:
    """Ленивая инициализация пула соединений."""
    global _pool
    if _pool is None:
        log.info(f"Initializing connection pool to PostgreSQL")
        # Маскируем пароль в логах с помощью urllib.parse (надёжнее regex)
        parsed = urlparse(DATABASE_URL)
        if parsed.password:
            safe_dsn = urlunparse(parsed._replace(
                netloc=f"{parsed.username}:***@{parsed.hostname}"
                + (f":{parsed.port}" if parsed.port else "")
            ))
        else:
            safe_dsn = DATABASE_URL
        log.info(f"Connecting to PostgreSQL at {safe_dsn}")
        _pool = ConnectionPool(min_size=POOL_MIN, max_size=POOL_MAX, conninfo=DATABASE_URL)
    return _pool


# ─── Хелперы памяти ───────────────────────────────────────────────────────────

def _embed_text(text: str) -> list:
    """Генерирует эмбеддинг для текста локальной моделью."""
    model = get_model()
    return model.encode([text], normalize_embeddings=True)[0].tolist()


# Схема durable-памяти. Выполняется idempotent'но (CREATE TABLE IF NOT EXISTS)
# при старте сервера, поэтому работает и для существующих БД без пересоздания.
MEMORY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memories (
    id          BIGSERIAL PRIMARY KEY,
    project     TEXT NOT NULL DEFAULT '',
    memory_type TEXT NOT NULL DEFAULT 'general',
    content     TEXT NOT NULL,
    embedding   VECTOR(1024),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS memories_embedding_hnsw
    ON memories USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project);
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(memory_type);
"""


def ensure_memory_schema() -> None:
    """Создаёт таблицу memories и индексы, если их ещё нет."""
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(MEMORY_SCHEMA_SQL)
        log.info("Memory schema ensured ('memories' table is ready)")
    except Exception as e:
        log.error(f"Failed to ensure memory schema: {e}", exc_info=True)
        sys.exit(1)


def _memory_filter_sql(project: Optional[str], memory_type: Optional[str]):
    """Строит WHERE-условия и параметры для фильтрации заметок.

    Возвращает (where_sql, params): where_sql — пустая строка или
    "WHERE ...", params — список параметров для подстановки %s.
    """
    conditions = []
    params = []
    if project is not None:
        conditions.append("project = %s")
        params.append(project)
    if memory_type is not None:
        conditions.append("memory_type = %s")
        params.append(memory_type)
    where_sql = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    return where_sql, params


def _memory_distance_to_score(distance: float) -> float:
    """Монотонное преобразование косинусного расстояния в score (0–1)."""
    return float(1.0 / (1.0 + distance))


def _lex_tokens(text: str) -> list:
    """Токены для лексического поиска: markdown-экранирование подчёркивания снимается,
    регистр сводится к нижнему, слово режется до первых 6 символов (грубая
    основа, чтобы формы русских слов совпадали)."""
    norm = text.replace("\\_", "_").lower()
    return [w[:6] for w in re.findall(r"[\w/]+", norm) if len(w) > 1]


def _bm25_scores(docs: list, query: str, k1: float = 1.5, b: float = 0.75) -> list:
    """BM25-оценка каждого текста из docs для запроса; 0 — нет общих токенов."""
    tfs = [Counter(_lex_tokens(d)) for d in docs]
    lens = [sum(tf.values()) for tf in tfs]
    avg = (sum(lens) / len(lens)) if lens else 0.0
    df = Counter(w for tf in tfs for w in tf)
    n = len(docs)
    q_tokens = set(_lex_tokens(query))
    scores = []
    for tf, length in zip(tfs, lens):
        score = 0.0
        for w in q_tokens:
            if w in tf:
                idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                score += idf * tf[w] * (k1 + 1) / (
                    tf[w] + k1 * (1 - b + b * length / avg)
                )
        scores.append(score)
    return scores


def _rrf(dense_ids: list, lex_ids: list) -> list:
    """Взвешенный Reciprocal Rank Fusion: индексы по убыванию суммы
    вес/(RRF_K + позиция)."""
    fused = Counter()
    for weight, ranking in ((DENSE_WEIGHT, dense_ids), (1.0, lex_ids)):
        for pos, i in enumerate(ranking):
            fused[i] += weight / (RRF_K + pos + 1)
    return [i for i, _ in fused.most_common()]


# ─── Инструменты ──────────────────────────────────────────────────────────────

@mcp.tool(
    name="rag_search",
    description=(
        "Hybrid search (semantic + keyword) over the knowledge base (pgvector). "
        "Returns the most relevant document chunks for a given query. "
        "Each result includes the text, source file path, heading path, and relevance score. "
        "By default searches only course documents; pass paths=[\"*\"] to search the whole base."
    ),
)
def rag_search(
    query: str,
    top_k: Optional[int] = None,
    paths: Optional[list[str]] = None,
) -> str:
    """
    Выполняет гибридный поиск по базе знаний: dense (pgvector) + лексика
    (BM25 по основам слов), слияние через RRF.

    Args:
        query: Текст запроса на любом языке (русский, английский, ...)
        top_k: Количество результатов (по умолчанию TOP_K_DEFAULT)
        paths: Префиксы путей документов, по которым искать (по умолчанию
               SEARCH_DEFAULT_PATHS; пустой список = по умолчанию;
               ["*"] — вся база)

    Returns:
        JSON-строка с массивом результатов, каждый содержит:
        - text: текст чанка
        - path: путь к исходному файлу
        - heading_path: путь по заголовкам в документе
        - title: название документа
        - score: 1/(1+косинусное расстояние), 0–1; считается для любого
          результата, в том числе найденного только лексикой. Порядок
          результатов задаёт RRF, поэтому score не обязан убывать.
        - lang: язык чанка из metadata ('ru'/'en', 'unknown' если ключа нет)
    """
    k = top_k or TOP_K_DEFAULT
    k = max(1, min(k, 50))  # ограничиваем диапазон
    # Кандидатов с каждой стороны берём с запасом для дедупликации по тексту
    n_cand = max(CANDIDATES, int(k * DEDUP_FACTOR))
    prefixes = [p for p in (paths or []) if isinstance(p, str) and p.strip()]
    prefixes = prefixes or SEARCH_DEFAULT_PATHS

    try:
        # Эмбеддинг запроса
        model = get_model()
        query_embedding = model.encode(
            [query], normalize_embeddings=True
        )[0].tolist()

        # Выборка отфильтрованного набора целиком, расстояние считается
        # точно (без ORDER BY, HNSW не участвует): HNSW с WHERE может вернуть
        # меньше строк, чем нужно, а на таком объёме база сканируется быстро.
        where_sql = ""
        params = [query_embedding]
        if SEARCH_ALL not in prefixes:
            where_sql = "WHERE d.path LIKE ANY(%s)"
            params.append([
                p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                for p in prefixes
            ])
        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        d.path,
                        d.title,
                        c.chunk_index,
                        c.text,
                        c.metadata,
                        (c.embedding <=> %s::vector) AS distance
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    {where_sql}
                    """,
                    params,
                )
                rows = cur.fetchall()

        # Dense-кандидаты: ближайшие n_cand, затем порог MAX_DISTANCE в Python
        # (как раньше, порог в SQL не проталкиваем).
        total_raw = len(rows)
        by_distance = sorted(
            (i for i, row in enumerate(rows) if row[5] is not None),
            key=lambda i: rows[i][5],
        )[:n_cand]
        dense_ids = [i for i in by_distance if rows[i][5] <= MAX_DISTANCE]
        filtered_out = len(by_distance) - len(dense_ids)

        if by_distance and rows[by_distance[0]][5] > NEAREST_DISTANCE_LIMIT:
            log.info(
                f"Query: '{query[:50]}...' → 0 results (nearest chunk "
                f"{rows[by_distance[0]][5]:.3f} > {NEAREST_DISTANCE_LIMIT:.2f})"
            )
            return json.dumps([], ensure_ascii=False, indent=2)

        # Лексические кандидаты: BM25 по всему отфильтрованному набору,
        # порога расстояния нет, нулевые оценки не берём.
        bm25 = _bm25_scores([row[3] for row in rows], query)
        lex_ids = sorted(
            (i for i, sc in enumerate(bm25) if sc > 0), key=lambda i: -bm25[i]
        )[:n_cand]

        dense_set = set(dense_ids)
        fused_ids = _rrf(dense_ids, lex_ids)
        kept_ids = [
            i for i in fused_ids
            if rows[i][5] is None
            or i in dense_set
            or rows[i][5] <= MAX_DISTANCE + LEXICAL_DISTANCE_MARGIN
        ]
        lex_filtered_out = len(fused_ids) - len(kept_ids)
        kept_rows = [rows[i] for i in kept_ids]

        # Дедупликация по точному совпадению текста (exact-match).
        # Первый в порядке RRF (наиболее релевантный) побеждает.
        seen_texts = set()
        deduped_rows = []
        dedup_dropped = 0
        for row in kept_rows:
            text = row[3]  # text — 4-й столбец SELECT (индекс 3)
            if text not in seen_texts:
                seen_texts.add(text)
                deduped_rows.append(row)
            else:
                dedup_dropped += 1

        # Усечение до запрошенного k
        final_rows = deduped_rows[:k]

        # Формируем результаты
        results = []
        for row in final_rows:
            path, title, chunk_index, text, meta, distance = row
            meta = meta if isinstance(meta, dict) else json.loads(meta) if meta else {}
            heading_path = meta.get("heading_path", [])
            # distance None у чанка без эмбеддинга (найден только лексикой)
            score = float(1.0 / (1.0 + distance)) if distance is not None else 0.0

            item = {
                "text": text,
                "path": path,
                "title": title,
                "heading_path": heading_path,
                "chunk_index": chunk_index,
                "score": round(score, 4),
                "lang": meta.get("lang", "unknown"),
            }
            results.append(item)

        log.info(
            f"Query: '{query[:50]}...' → {len(results)} results "
            f"(scope {total_raw}, dense {len(dense_ids)}, lexical {len(lex_ids)}, "
            f"filtered out {filtered_out} by max_distance={MAX_DISTANCE:.2f}, "
            f"lexical filtered out {lex_filtered_out}, "
            f"dedup dropped {dedup_dropped})"
        )

        # Miss-предупреждение: один warning на вызов. Срабатывает, если после
        # фильтрации не осталось результатов (при том, что dense-кандидаты
        # были) ИЛИ отфильтровано > 50% dense-кандидатов.
        if by_distance:
            filtered_ratio = filtered_out / len(by_distance)
            if len(results) == 0 or filtered_ratio > 0.5:
                log.warning(
                    f"Query: '{query[:50]}...' — most candidates filtered by "
                    f"max_distance={MAX_DISTANCE:.2f}: dense {len(by_distance)}, "
                    f"kept {len(results)}, filtered out {filtered_out} "
                    f"({filtered_ratio:.0%})"
                )

        return json.dumps(results, ensure_ascii=False, indent=2)

    except psycopg.OperationalError as e:
        log.error(f"Database connection error: {e}")
        return json.dumps({"error": f"Database connection failed: {e}"})
    except Exception as e:
        log.error(f"Search failed: {e}", exc_info=True)
        return json.dumps({"error": f"Search failed: {e}"})


# ─── Durable-память (аналог Memory в AiderDesk) ───────────────────────────────

@mcp.tool(
    name="store_memory",
    description=(
        "Save a durable, project-scoped memory note (decision, code pattern, "
        "user preference). Use at the END of a task for stable outcomes that "
        "will help in future sessions. Do NOT store one-off status updates, "
        "debug notes, logs, or secrets. Text is embedded locally and stored "
        "in the local vector DB (offline)."
    ),
)
def store_memory(
    content: str,
    memory_type: Optional[str] = None,
    project: Optional[str] = None,
) -> str:
    """
    Сохраняет durable-заметку в локальную векторную память.

    Args:
        content: Текст заметки (решение, паттерн, предпочтение пользователя).
        memory_type: Тип заметки: 'decision' | 'pattern' | 'preference' |
                     'general' (по умолчанию 'general').
        project: Скоуп (имя проекта/каталога). По умолчанию '' (без скоупа).

    Returns:
        JSON-строка с сохранённой записью (id, content, memory_type, project,
        created_at) или сообщением об ошибке.
    """
    text = (content or "").strip()
    if not text:
        return json.dumps({"error": "content must not be empty"})
    mtype = (memory_type or "general").strip() or "general"
    proj = (project or "").strip()

    try:
        embedding = _embed_text(text)
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO memories (project, memory_type, content, embedding)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id, project, memory_type, content, created_at
                    """,
                    (proj, mtype, text, embedding),
                )
                row = cur.fetchone()
        id_, proj, mtype, content, created_at = row
        log.info(
            f"Memory stored: id={id_} type={mtype} project='{proj}' "
            f"content='{content[:50]}...'"
        )
        return json.dumps(
            {
                "id": id_,
                "content": content,
                "memory_type": mtype,
                "project": proj,
                "created_at": created_at.isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        )
    except psycopg.OperationalError as e:
        log.error(f"Database connection error: {e}")
        return json.dumps({"error": f"Database connection failed: {e}"})
    except Exception as e:
        log.error(f"store_memory failed: {e}", exc_info=True)
        return json.dumps({"error": f"store_memory failed: {e}"})


@mcp.tool(
    name="retrieve_memory",
    description=(
        "Search stored durable memories (decisions, patterns, preferences) by "
        "semantic relevance. Use at the START of a task to recall relevant "
        "context from previous sessions. Run this even if the task seems new."
    ),
)
def retrieve_memory(
    query: str,
    top_k: Optional[int] = None,
    project: Optional[str] = None,
    memory_type: Optional[str] = None,
    max_distance: Optional[float] = None,
) -> str:
    """
    Семантический поиск по durable-заметкам.

    Args:
        query: Текст запроса (на любом языке).
        top_k: Максимум результатов (по умолчанию TOP_K_DEFAULT).
        project: Фильтр по скоупу проекта (None — все проекты).
        memory_type: Фильтр по типу заметки (None — все типы).
        max_distance: Порог косинусного расстояния (None — MEMORY_MAX_DISTANCE).

    Returns:
        JSON-строка с массивом результатов [{id, content, memory_type,
        project, score, created_at}], отсортированных по релевантности.
    """
    k = top_k or TOP_K_DEFAULT
    k = max(1, min(k, 50))
    threshold = MEMORY_MAX_DISTANCE if max_distance is None else max_distance
    if not 0.0 <= threshold <= 2.0:
        threshold = MEMORY_MAX_DISTANCE

    try:
        query_embedding = _embed_text(query)
        where_sql, filter_params = _memory_filter_sql(project, memory_type)
        sql_limit = int(k * DEDUP_FACTOR)
        # Порядок %s в SQL: (1) SELECT-эмбеддинг, (2) фильтры WHERE,
        # (3) ORDER BY-эмбеддинг, (4) LIMIT — параметры в том же порядке.
        sql = f"""
            SELECT id, project, memory_type, content, created_at,
                   (embedding <=> %s::vector) AS distance
            FROM memories
            {where_sql}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """
        params = [query_embedding, *filter_params, query_embedding, sql_limit]
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()

        kept = [row for row in rows if row[5] <= threshold]

        # Дедупликация по точному совпадению текста (первый по расстоянию побеждает)
        seen = set()
        results = []
        for row in kept:
            id_, proj, mtype, content, created_at, distance = row
            if content in seen:
                continue
            seen.add(content)
            results.append(
                {
                    "id": id_,
                    "content": content,
                    "memory_type": mtype,
                    "project": proj,
                    "score": round(_memory_distance_to_score(distance), 4),
                    "created_at": created_at.isoformat(),
                }
            )

        results = results[:k]
        log.info(
            f"retrieve_memory: '{query[:50]}...' → {len(results)} results "
            f"(raw {len(rows)}, kept {len(kept)}, threshold {threshold:.2f})"
        )
        return json.dumps(results, ensure_ascii=False, indent=2)

    except psycopg.OperationalError as e:
        log.error(f"Database connection error: {e}")
        return json.dumps({"error": f"Database connection failed: {e}"})
    except Exception as e:
        log.error(f"retrieve_memory failed: {e}", exc_info=True)
        return json.dumps({"error": f"retrieve_memory failed: {e}"})


@mcp.tool(
    name="list_memories",
    description=(
        "List stored memories, optionally filtered by project and/or memory "
        "type. Newest first."
    ),
)
def list_memories(
    project: Optional[str] = None,
    memory_type: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> str:
    """
    Возвращает список сохранённых заметок без семантического поиска.

    Args:
        project: Фильтр по скоупу проекта (None — все проекты).
        memory_type: Фильтр по типу заметки (None — все типы).
        limit: Максимум записей (по умолчанию 100, максимум 500).
        offset: Смещение для пагинации.

    Returns:
        JSON-строка с массивом [{id, content, memory_type, project,
        created_at}], свежие первыми.
    """
    lim = min(limit or 100, 500)
    lim = max(1, lim)
    off = max(0, offset or 0)

    try:
        where_sql, filter_params = _memory_filter_sql(project, memory_type)
        sql = f"""
            SELECT id, project, memory_type, content, created_at
            FROM memories
            {where_sql}
            ORDER BY id DESC
            LIMIT %s OFFSET %s
        """
        params = [*filter_params, lim, off]
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()

        results = [
            {
                "id": row[0],
                "content": row[3],
                "memory_type": row[2],
                "project": row[1],
                "created_at": row[4].isoformat(),
            }
            for row in rows
        ]
        log.info(f"list_memories → {len(results)} records")
        return json.dumps(results, ensure_ascii=False, indent=2)

    except psycopg.OperationalError as e:
        log.error(f"Database connection error: {e}")
        return json.dumps({"error": f"Database connection failed: {e}"})
    except Exception as e:
        log.error(f"list_memories failed: {e}", exc_info=True)
        return json.dumps({"error": f"list_memories failed: {e}"})


@mcp.tool(
    name="update_memory",
    description=(
        "Update the content of an existing memory by id (re-embeds the new "
        "text automatically)."
    ),
)
def update_memory(memory_id: int, content: str) -> str:
    """
    Обновляет содержимое заметки и пересчитывает её эмбеддинг.

    Args:
        memory_id: ID заметки (из store_memory/retrieve_memory/list_memories).
        content: Новый текст заметки.

    Returns:
        JSON-строка с обновлённой записью или ошибкой, если ID не найден.
    """
    text = (content or "").strip()
    if not text:
        return json.dumps({"error": "content must not be empty"})

    try:
        embedding = _embed_text(text)
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE memories
                    SET content = %s, embedding = %s, updated_at = NOW()
                    WHERE id = %s
                    RETURNING id, project, memory_type, content, created_at, updated_at
                    """,
                    (text, embedding, memory_id),
                )
                row = cur.fetchone()
        if row is None:
            return json.dumps({"error": f"memory {memory_id} not found"})
        id_, proj, mtype, content, created_at, updated_at = row
        log.info(f"Memory updated: id={id_}")
        return json.dumps(
            {
                "id": id_,
                "content": content,
                "memory_type": mtype,
                "project": proj,
                "created_at": created_at.isoformat(),
                "updated_at": updated_at.isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        )
    except psycopg.OperationalError as e:
        log.error(f"Database connection error: {e}")
        return json.dumps({"error": f"Database connection failed: {e}"})
    except Exception as e:
        log.error(f"update_memory failed: {e}", exc_info=True)
        return json.dumps({"error": f"update_memory failed: {e}"})


@mcp.tool(
    name="delete_memory",
    description="Delete a specific memory by id.",
)
def delete_memory(memory_id: int) -> str:
    """
    Удаляет заметку по ID.

    Args:
        memory_id: ID заметки.

    Returns:
        JSON-строка {"deleted": true|false, "id": ...}.
    """
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM memories WHERE id = %s", (memory_id,))
                deleted = cur.rowcount > 0
        log.info(f"Memory delete: id={memory_id} deleted={deleted}")
        return json.dumps({"deleted": bool(deleted), "id": memory_id})
    except psycopg.OperationalError as e:
        log.error(f"Database connection error: {e}")
        return json.dumps({"error": f"Database connection failed: {e}"})
    except Exception as e:
        log.error(f"delete_memory failed: {e}", exc_info=True)
        return json.dumps({"error": f"delete_memory failed: {e}"})


def check_db_connection():
    """Проверяем доступность базы и таблицы chunks при старте."""
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM chunks LIMIT 1")
        log.info("Database connection OK, 'chunks' table is accessible")
    except Exception as e:
        log.error(f"Failed to connect to database or 'chunks' table is missing: {e}")
        log.error("Please ensure PostgreSQL is running and init.sql has been executed.")
        sys.exit(1)


# ─── Обработка сигналов ──────────────────────────────────────────────────────

def _shutdown(signum, frame):
    """Корректное завершение при SIGTERM/SIGINT."""
    sig_name = signal.Signals(signum).name
    log.info(f"Received {sig_name}, shutting down...")
    if _pool:
        _pool.close()
    sys.exit(0)


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)

# ─── Запуск ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("Starting pgvector RAG MCP server (stdio mode)")
    check_db_connection()
    ensure_memory_schema()
    try:
        mcp.run(transport="stdio")
    except KeyboardInterrupt:
        log.info("Server stopped")
    finally:
        if _pool:
            _pool.close()
