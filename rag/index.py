#!/usr/bin/env python3
"""
Индексатор документов для pgvector RAG-системы.
Обходит input_material/**/*.md и **/*.pdf, чанкирует, считает embeddings, загружает в PostgreSQL.

Использование:
    python index.py                         # индексация всех файлов
    python index.py --force                 # принудительная переиндексация (пере-чанкинг + пере-эмбеддинг)
    python index.py --rebuild-embeddings    # пересчёт эмбеддингов (без пере-чанкинга, например после смены модели)
    python index.py --dir other_path        # другой каталог
"""

import os
import sys
import glob
import json
import hashlib
import logging
import argparse
import re
import time
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import AcceleratorOptions, PdfPipelineOptions
from md_optimizer import optimize_for_rag
from typing import List, Dict, Optional
from dataclasses import dataclass, field

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
import psycopg.errors
from sentence_transformers import SentenceTransformer
from tqdm import tqdm

# ─── Конфигурация (из переменных окружения или дефолты) ───────────────────────

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "rag_docs")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")
MODEL_NAME = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
INPUT_DIR = os.getenv("INPUT_DIR", r"D:\projects\4j\job4j_questions\07_system_design\03_my_system_design_prepare\input_material")

# Каталог кэша переводов: для EN-файлов индексируем готовый RU-перевод.
# Файл кэша называется {content_hash(исходного файла)}.ru.md, где hash — тот же sha256,
# что возвращает content_hash() (см. ниже).
TRANSLATIONS_DIR = os.getenv("TRANSLATIONS_DIR", r"D:\projects\aider_desk_rag\rag-project\translations_cache")

# Число потоков для docling (конвертация PDF). Дефолт 2 — ограничиваем потоки,
# чтобы снизить пиковое потребление RAM (защита от std::bad_alloc на больших PDF).
# OMP_NUM_THREADS выставляем равным DOCLING_NUM_THREADS, если пользователь его не задал,
# чтобы torch/OpenMP не плодили лишние потоки.
DOCLING_NUM_THREADS = int(os.getenv("DOCLING_NUM_THREADS", "2"))
os.environ.setdefault("OMP_NUM_THREADS", str(DOCLING_NUM_THREADS))

# ─── Авто-перевод EN-файлов ───────────────────────────────────────────────────
# При встрече MD-файла на английском без перевода в кэше index.py сам вызывает
# перевод (eval/translate_files.ensure_translation) ПЕРЕД индексацией этого файла,
# а затем индексирует русский текст. Если перевод недоступен/упал — индексируем
# оригинал с предупреждением (прежнее поведение). Отключить: AUTO_TRANSLATE=0
# (или 'false'/'no'/пустая строка). По умолчанию включён.
_EVAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval")
_translate_files_module = None


def auto_translate_enabled() -> bool:
    """AUTO_TRANSLATE включён по умолчанию; выключен только при значении
    '0'/'false'/'no' или пустой строке (регистр/пробелы не важны)."""
    val = os.getenv("AUTO_TRANSLATE", "1").strip().lower()
    return val not in ("", "0", "false", "no")


def _load_translate_files():
    """Ленивый импорт eval/translate_files.py — не тянет torch/transformers
    при старте index.py (тяжёлые зависимости грузятся только внутри перевода)."""
    global _translate_files_module
    if _translate_files_module is None:
        if _EVAL_DIR not in sys.path:
            sys.path.insert(0, _EVAL_DIR)
        import translate_files
        _translate_files_module = translate_files
    return _translate_files_module


def auto_translate_md(src_path: str) -> Optional[str]:
    """Перевести EN MD-файл в TRANSLATIONS_DIR. Возвращает путь к {hash}.ru.md
    или None (модель недоступна / перевод упал)."""
    try:
        tf = _load_translate_files()
        return tf.ensure_translation(os.path.abspath(src_path), TRANSLATIONS_DIR)
    except Exception as e:
        log.warning(f"Auto-translate failed to load/run for {src_path}: {e}")
        return None

# Параметры чанкинга
TARGET_CHARS = 3000       # ~750–1000 токенов
OVERLAP_CHARS = 300       # ~10% overlap
BATCH_SIZE = 16           # размер батча для encoding (уменьшено для bge-m3)
MD_CACHE_DIR = "_md_cache"  # директория для кэша оптимизированного MD (относительно INPUT_DIR)

# ─── Логирование ──────────────────────────────────────────────────────────────

class TqdmLoggingHandler(logging.Handler):
    """logging.Handler, который выводит записи через tqdm.write: tqdm сам сдвигает активные бары вниз и печатает сообщение над ними, не разрывая прогресс-бар."""

    def emit(self, record):
        try:
            msg = self.format(record)
            tqdm.write(msg)
        except Exception:
            self.handleError(record)


# Явная настройка корневого логгера (basicConfig убран, чтобы не было дублирования
# с дефолтным stderr-хендлером). Логгеры "index" и "md_optimizer" наследуют корневой конфиг.
_log_handler = TqdmLoggingHandler()
_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
_root_logger = logging.getLogger()
_root_logger.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper()))
_root_logger.addHandler(_log_handler)
log = logging.getLogger("index")

# ─── Валидация окружения ─────────────────────────────────────────────────────

def validate_env():
    """Проверяем, что все обязательные переменные заданы."""
    errors = []
    if not DB_HOST:
        errors.append("DB_HOST is not set")
    if not DB_NAME:
        errors.append("DB_NAME is not set")
    if not DB_USER:
        errors.append("DB_USER is not set")
    if not DB_PASSWORD:
        errors.append("DB_PASSWORD is not set")
    if not os.path.isdir(INPUT_DIR):
        errors.append(f"INPUT_DIR '{INPUT_DIR}' does not exist")
    if errors:
        for e in errors:
            log.error(f"Configuration error: {e}")
        sys.exit(1)

# ─── Утилиты ──────────────────────────────────────────────────────────────────

def content_hash(text: str) -> str:
    """SHA-256 хеш содержимого файла."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def detect_language(text: str) -> str:
    """
    Определяет язык текста по доле кириллических символов (U+0400–U+04FF)
    среди всех алфавитных символов (str.isalpha()).
    - доля кириллицы >= 0.30 → 'ru', иначе → 'en';
    - смешанные/код-файлы (много латиницы в коде/YAML) дают долю < 0.30 → 'en',
      поэтому они автоматически не считаются русскоязычными и не требуют перевода;
    - пустой текст или текст без алфавитных символов → дефолт 'ru'
      (исходный каталог преимущественно русскоязычный).
    """
    if not text:
        return "ru"
    cyrillic = 0
    alpha = 0
    for ch in text:
        if ch.isalpha():
            alpha += 1
            if "\u0400" <= ch <= "\u04FF":
                cyrillic += 1
    if alpha == 0:
        return "ru"
    return "ru" if (cyrillic / alpha) >= 0.30 else "en"


def convert_pdf_to_md(path: str) -> str:
    """Конвертирует PDF в Markdown через Docling.

    Ресурсы docling ограничены намеренно: меньше потоков и батчей → ниже пиковое
    потребление RAM (защита от std::bad_alloc на больших PDF). Остальные тяжёлые
    дефолты (OCR, tableformer) не трогаем.
    """
    try:
        pipeline_options = PdfPipelineOptions()
        # Буфер межстадийной очереди и батч layout-модели: 100→16 и 4→2.
        pipeline_options.queue_max_size = 16
        pipeline_options.layout_batch_size = 2
        pipeline_options.accelerator_options = AcceleratorOptions(
            num_threads=DOCLING_NUM_THREADS
        )
        converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
            }
        )
        result = converter.convert(path)
        md_text = result.document.export_to_markdown()
        log.debug(f"Converted PDF {path} to Markdown ({len(md_text)} chars)")
        return md_text
    except Exception as e:
        log.warning(f"Failed to convert PDF {path} to Markdown: {e}")
        return ""


def get_cached_md(pdf_path: str, input_dir: str, title: str, source: str) -> str:
    """
    Возвращает оптимизированный Markdown для PDF.
    Использует кэш: если _md_cache/...pdf.md существует и свежее PDF — читает его.
    Иначе конвертирует PDF → MD → optimize_for_rag → сохраняет в кэш.
    """
    # Формируем путь кэша: input_dir/_md_cache/relative/path.pdf.md
    rel = os.path.relpath(pdf_path, input_dir)
    cache_path = os.path.join(input_dir, MD_CACHE_DIR, rel + ".md")
    cache_dir = os.path.dirname(cache_path)
    
    # Проверяем кэш
    if os.path.exists(cache_path):
        pdf_mtime = os.path.getmtime(pdf_path)
        cache_mtime = os.path.getmtime(cache_path)
        if cache_mtime >= pdf_mtime:
            log.debug(f"Using cached MD for {rel}")
            with open(cache_path, "r", encoding="utf-8") as f:
                return f.read()
    
    # Конвертируем и оптимизируем
    log.info(f"Converting PDF to MD: {rel}")
    md_content = convert_pdf_to_md(pdf_path)
    if not md_content:
        return ""
    
    optimized = optimize_for_rag(md_content, title=title, source=source)
    
    # Сохраняем в кэш
    os.makedirs(cache_dir, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        f.write(optimized)
    log.debug(f"Cached optimized MD to {cache_path}")
    
    return optimized


@dataclass
class Chunk:
    chunk_index: int
    text: str
    metadata: Dict


def split_long_text(text: str, limit: int) -> List[str]:
    """
    Режет текст длиннее limit на куски не длиннее limit: сначала по пустым строкам
    (абзацы), потом по строкам, потом по символам. Короткий текст возвращает как есть.
    """
    if len(text) <= limit:
        return [text]
    pieces: List[str] = []
    buf = ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) > limit:
            if buf:
                pieces.append(buf)
                buf = ""
            line_buf = ""
            for line in para.split("\n"):
                while len(line) > limit:
                    if line_buf:
                        pieces.append(line_buf)
                        line_buf = ""
                    pieces.append(line[:limit])
                    line = line[limit:]
                if line_buf and len(line_buf) + 1 + len(line) > limit:
                    pieces.append(line_buf)
                    line_buf = ""
                line_buf = f"{line_buf}\n{line}" if line_buf else line
            if line_buf:
                pieces.append(line_buf)
        elif buf and len(buf) + 2 + len(para) > limit:
            pieces.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        pieces.append(buf)
    return pieces


def chunk_markdown(md: str, target_chars: int = TARGET_CHARS,
                   overlap_chars: int = OVERLAP_CHARS,
                   lang: str = "ru") -> List[Chunk]:
    """
    Чанкинг Markdown: разбиваем по заголовкам (#, ##, ###),
    затем собираем в чанки нужного размера с overlap.
    Каждый чанк получает heading_path из метаданных.

    Метаданные чанка:
    - heading_path — стек заголовков секции (извлекается из переданного текста md);
    - lang — язык индексируемого текста: 'ru' для русского файла и для переведённого
      EN-файла, 'en' для непереведённого EN-файла.
    Оригинал переведённого файла в чанки не пишется: он лежит на диске по documents.path.
    """
    lines = md.splitlines()
    sections: List[tuple] = []  # (heading_stack, text_lines)
    current_lines: List[str] = []
    heading_stack: List[str] = []

    def flush_section():
        if current_lines:
            text = "\n".join(current_lines).strip()
            if text:
                sections.append((heading_stack[:], text))
        current_lines.clear()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            flush_section()
            # Определяем уровень заголовка
            level = len(stripped) - len(stripped.lstrip("#"))
            heading_text = stripped.lstrip("# ").strip()
            # Обрезаем стек до нужного уровня
            heading_stack = heading_stack[:level - 1]
            if heading_text:
                heading_stack.append(heading_text)
            current_lines.append(line)
        else:
            current_lines.append(line)

    flush_section()

    # Секция без заголовков может быть сколь угодно длинной: режем её на куски до target_chars
    sections = [(h, piece) for h, t in sections for piece in split_long_text(t, target_chars)]

    # Собираем чанки нужного размера
    chunks: List[Chunk] = []
    buffer = ""
    buffer_headings: List[str] = []
    idx = 0

    def emit_chunk(text: str, headings: List[str]):
        nonlocal idx
        t = text.strip()
        if not t:
            return
        metadata = {"heading_path": headings, "lang": lang}
        chunks.append(Chunk(
            chunk_index=idx,
            text=t,
            metadata=metadata,
        ))
        idx += 1

    for section_headings, section_text in sections:
        if not buffer:
            buffer = section_text
            buffer_headings = section_headings
            continue

        if len(buffer) + 1 + len(section_text) <= target_chars:
            buffer = buffer + "\n" + section_text
            # Обновляем buffer_headings до наиболее специфичного заголовка
            buffer_headings = section_headings
        else:
            emit_chunk(buffer, buffer_headings)
            # Overlap: берём последние overlap_chars символов
            overlap = buffer[-overlap_chars:] if overlap_chars > 0 else ""
            buffer = (overlap + "\n" + section_text).strip()
            buffer_headings = section_headings

    if buffer.strip():
        emit_chunk(buffer, buffer_headings)

    log.debug(f"chunk_markdown: {len(sections)} sections → {len(chunks)} chunks")
    return chunks





def get_dsn() -> str:
    """Формируем DSN в правильном формате для psycopg."""
    return f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


# ─── Основная логика ──────────────────────────────────────────────────────────

def main():
    validate_env()

    parser = argparse.ArgumentParser(description="Index MD/PDF files into pgvector")
    parser.add_argument("--force", action="store_true", help="Force re-index all files (re-chunk + re-embed)")
    parser.add_argument("--rebuild-embeddings", action="store_true", help="Recompute embeddings for all files without re-chunking (useful after model change)")
    parser.add_argument("--dir", default=INPUT_DIR, help="Input directory")
    args = parser.parse_args()

    # Каталог переводов создаём при первом использовании, если его ещё нет.
    os.makedirs(TRANSLATIONS_DIR, exist_ok=True)

    dsn = get_dsn()
    log.info(f"Connecting to PostgreSQL: {DB_HOST}:{DB_PORT}/{DB_NAME}")
    log.info(f"Loading model: {MODEL_NAME}")

    try:
        model = SentenceTransformer(MODEL_NAME)
        model_dim = model.get_sentence_embedding_dimension()
        log.info(f"Model loaded. Embedding dimension: {model_dim}")
    except Exception as e:
        log.error(f"Failed to load model '{MODEL_NAME}': {e}")
        sys.exit(1)

    # ── Проверка совместимости размерности ──
    EXPECTED_DIM = 1024  # VECTOR(1024) в init.sql — должен совпадать с моделью
    if model_dim != EXPECTED_DIM:
        log.error(
            f"Model dimension mismatch: model produces {model_dim}-dim vectors, "
            f"but init.sql expects VECTOR({EXPECTED_DIM}). "
            f"Either change EMBED_MODEL or update init.sql."
        )
        sys.exit(1)

    # Ищем .md и .pdf файлы (исключая _md_cache)
    all_md = sorted(glob.glob(os.path.join(args.dir, "**", "*.md"), recursive=True))
    cache_prefix = os.path.join(args.dir, MD_CACHE_DIR) + os.sep
    md_files = sorted(f for f in all_md if not f.startswith(cache_prefix))
    pdf_files = sorted(glob.glob(os.path.join(args.dir, "**", "*.pdf"), recursive=True))
    all_files = md_files + pdf_files
    if not all_files:
        log.warning(f"No .md or .pdf files found in {args.dir}")
        sys.exit(0)
    log.info(f"Found {len(md_files)} markdown, {len(pdf_files)} pdf files")

    pool = ConnectionPool(min_size=1, max_size=3, conninfo=dsn)

    try:
        with pool.connection() as conn:
            conn.autocommit = False
            indexed = 0
            skipped = 0
            rebuilt = 0
            total_chunks = 0
            total_embed_time = 0.0
            t_start = time.monotonic()

            for file_path in tqdm(all_files, desc="Indexing", ncols=100):
                rel_path = os.path.relpath(file_path, ".").replace("\\", "/")
                ext = os.path.splitext(file_path)[1].lower()

                if ext == ".pdf":
                    title = os.path.basename(file_path)
                    content = get_cached_md(file_path, args.dir, title=title, source=rel_path)
                    if not content:
                        log.warning(f"Empty content from PDF {rel_path}, skipping")
                        continue
                    c_hash = content_hash(content)
                    source_type = "pdf"
                    # PDF: перевод не применяется (кэш переводов — только для MD),
                    # но язык определяем, чтобы заполнить metadata["lang"].
                    index_text = content
                    indexed_lang = detect_language(content)
                else:
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    c_hash = content_hash(content)
                    title = os.path.basename(file_path)
                    source_type = "md"
                    lang = detect_language(content)
                    # index_text / indexed_lang вычисляются НИЖЕ,
                    # ПОСЛЕ проверки content_hash (skip/rebuild), чтобы не читать
                    # и не создавать перевод для файлов, которые уже в БД и будут
                    # пропущены (иначе обычный прогон переводил бы всё подряд).
                    index_text = None
                    indexed_lang = "ru"

                log.debug(f"Processing {rel_path} ({source_type}, hash={c_hash[:12]}...)")

                # ── Глобальная дедупликация по content_hash (Вариант A) ──
                # Если документ с таким content_hash уже существует (под другим путём),
                # пропускаем индексацию. Это предотвращает создание дублей контента.
                # --force: если путь тот же — переиндексируем (path-скип ниже пропустится);
                # если путь другой — пересчитываем эмбеддинги существующей записи, НЕ создаём дубль.
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        "SELECT id, path FROM documents WHERE content_hash = %s",
                        (c_hash,),
                    )
                    existing_by_hash = cur.fetchone()
                    if existing_by_hash:
                        existing_path = existing_by_hash["path"]
                        existing_id = existing_by_hash["id"]
                        if existing_path != rel_path:
                            if args.force:
                                log.info(
                                    f"--force: re-embedding existing document {existing_path} "
                                    f"(doc_id={existing_id}) for duplicate content {rel_path}"
                                )
                                # Пересчитываем эмбеддинги для существующего документа
                                cur.execute(
                                    "SELECT id, text FROM chunks WHERE document_id = %s ORDER BY chunk_index",
                                    (existing_id,),
                                )
                                existing_chunks = cur.fetchall()
                                if existing_chunks:
                                    texts = [c["text"] for c in existing_chunks]
                                    t0 = time.monotonic()
                                    new_embeddings = []
                                    for i in range(0, len(texts), BATCH_SIZE):
                                        batch = texts[i : i + BATCH_SIZE]
                                        embs = model.encode(batch, normalize_embeddings=True, show_progress_bar=False)
                                        new_embeddings.extend(embs)
                                    elapsed = time.monotonic() - t0
                                    total_chunks += len(texts)
                                    total_embed_time += elapsed
                                    for chunk_row, emb in zip(existing_chunks, new_embeddings):
                                        cur.execute(
                                            "UPDATE chunks SET embedding = %s WHERE id = %s",
                                            (emb.tolist(), chunk_row["id"]),
                                        )
                                    log.debug(f"Rebuilt embeddings for {existing_path}: {len(existing_chunks)} chunks")
                                conn.commit()
                                rebuilt += 1
                            else:
                                log.info(
                                    f"Skipped duplicate content {rel_path} "
                                    f"(same content_hash as {existing_path}, doc_id={existing_id})"
                                )
                                skipped += 1
                            conn.rollback()
                            continue
                        # existing_path == rel_path — этот файл будет обработан path-скипом ниже
                        # или переиндексирован через --force
                    conn.rollback()

                # ── Пересчёт эмбеддингов (без пере-чанкинга) ──
                if args.rebuild_embeddings:
                    with conn.cursor(row_factory=dict_row) as cur:
                        cur.execute(
                            "SELECT id FROM documents WHERE path = %s",
                            (rel_path,),
                        )
                        row = cur.fetchone()
                        if not row:
                            log.warning(f"Document {rel_path} not in DB, skipping (use --force to index)")
                            conn.rollback()
                            continue
                        doc_id = row["id"]
                        cur.execute(
                            "SELECT id, text FROM chunks WHERE document_id = %s ORDER BY chunk_index",
                            (doc_id,),
                        )
                        existing_chunks = cur.fetchall()
                        if not existing_chunks:
                            log.warning(f"No chunks for {rel_path}, skipping")
                            conn.rollback()
                            continue
                        texts = [c["text"] for c in existing_chunks]
                        t0 = time.monotonic()
                        new_embeddings = []
                        for i in range(0, len(texts), BATCH_SIZE):
                            batch = texts[i : i + BATCH_SIZE]
                            embs = model.encode(batch, normalize_embeddings=True, show_progress_bar=False)
                            new_embeddings.extend(embs)
                        elapsed = time.monotonic() - t0
                        total_chunks += len(texts)
                        total_embed_time += elapsed
                        for chunk_row, emb in zip(existing_chunks, new_embeddings):
                            cur.execute(
                                "UPDATE chunks SET embedding = %s WHERE id = %s",
                                (emb.tolist(), chunk_row["id"]),
                            )
                        log.debug(f"Rebuilt embeddings for {rel_path}: {len(existing_chunks)} chunks")
                    conn.commit()
                    rebuilt += 1
                    continue

                # ── Инкрементальное индексирование ──
                if not args.force:
                    with conn.cursor(row_factory=dict_row) as cur:
                        cur.execute(
                            "SELECT content_hash FROM documents WHERE path = %s",
                            (rel_path,),
                        )
                        row = cur.fetchone()
                        if row and row["content_hash"] == c_hash:
                            log.debug(f"Skipped unchanged {rel_path}")
                            skipped += 1
                            conn.rollback()
                            continue
                    # Закрываем транзакцию SELECT, если файл новый или изменился
                    conn.rollback()

                # ── Разрешаем текст для чанкинга: для EN-MD предпочитаем перевод ──
                # c_hash — это sha256 исходного файла (та же функция content_hash),
                # перевод лежит в TRANSLATIONS_DIR как {c_hash}.ru.md. Файлы, которые
                # будут индексироваться (прошли skip-проверку выше), ищут кэш перевода,
                # а при его отсутствии авто-переводятся (AUTO_TRANSLATE включён).
                if source_type == "md":
                    if lang == "en":
                        translation_path = os.path.join(TRANSLATIONS_DIR, f"{c_hash}.ru.md")
                        if os.path.isfile(translation_path):
                            with open(translation_path, "r", encoding="utf-8") as f:
                                index_text = f.read()
                            indexed_lang = "ru"
                            log.info(f"RU translation found for {rel_path}; indexing translated text")
                        elif auto_translate_enabled():
                            t0 = time.monotonic()
                            translated_path = auto_translate_md(file_path)
                            dt = time.monotonic() - t0
                            if translated_path is not None:
                                with open(translated_path, "r", encoding="utf-8") as f:
                                    index_text = f.read()
                                indexed_lang = "ru"
                                log.info(f"Auto-translated {rel_path} in {dt/60:.1f} min ({dt:.0f}s); indexing translated text")
                            else:
                                index_text = content
                                indexed_lang = "en"
                                log.warning(
                                    f"Auto-translate failed for {rel_path} "
                                    f"(hash={c_hash[:12]}...) in {dt/60:.1f} min; indexing original EN text"
                                )
                        else:
                            index_text = content
                            indexed_lang = "en"
                            log.warning(
                                f"No RU translation for {rel_path} "
                                f"(hash={c_hash[:12]}...) in {TRANSLATIONS_DIR}; indexing original EN text"
                            )
                    else:
                        index_text = content
                        indexed_lang = "ru"
                else:
                    # PDF: перевод не применяется (кэш переводов — только для MD),
                    # но язык определяем, чтобы заполнить metadata["lang"].
                    index_text = content
                    indexed_lang = detect_language(content)

                log.info(f"Processing {rel_path} ({source_type})")

                # ── Чанкинг (без БД) ──
                # PDF уже сконвертирован в оптимизированный MD,
                # используем chunk_markdown для чанкинга по заголовкам.
                # Для переведённых EN-файлов чанкается перевод (index_text).
                chunks = chunk_markdown(index_text, lang=indexed_lang)
                log.info(f"Chunked {rel_path} into {len(chunks)} chunks")

                # ── Эмбеддинги (без БД, долго) ──
                texts = [c.text for c in chunks] if chunks else []
                all_embeddings = []
                embed_pbar = tqdm(total=len(texts), desc=f"  Embedding {title[:40]}", ncols=100, unit="chunk", leave=True)
                t_file = time.monotonic()
                for i in range(0, len(texts), BATCH_SIZE):
                    batch = texts[i : i + BATCH_SIZE]
                    embs = model.encode(batch, normalize_embeddings=True, show_progress_bar=False)
                    all_embeddings.extend(embs)
                    embed_pbar.update(len(batch))
                embed_pbar.close()
                file_dt = time.monotonic() - t_file
                total_chunks += len(texts)
                total_embed_time += file_dt
                if file_dt > 0 and len(texts) > 0:
                    log.info(f"  Embedded {title}: {len(texts)} chunks in {file_dt:.1f}s ({len(texts)/file_dt:.2f} chunk/s, {file_dt/len(texts):.3f} s/chunk)")
                else:
                    log.info(f"  Embedded {title}: {len(texts)} chunks in {file_dt:.1f}s")

                if chunks and all_embeddings:
                    # ── Upsert + DELETE + INSERT в одной короткой транзакции ──
                    # Защита от гонки по content_hash (UNIQUE constraint):
                    # пробуем вставить/обновить по path; если конфликт по content_hash
                    # (другой процесс вставил тот же контент под другим путём) —
                    # находим существующий документ и используем его id.
                    with conn.cursor(row_factory=dict_row) as cur:
                        try:
                            cur.execute(
                                """
                                INSERT INTO documents (path, content_hash, source_type, title)
                                VALUES (%s, %s, %s, %s)
                                ON CONFLICT (path) DO UPDATE
                                    SET content_hash = EXCLUDED.content_hash,
                                        source_type = EXCLUDED.source_type,
                                        title = EXCLUDED.title
                                RETURNING id
                                """,
                                (rel_path, c_hash, source_type, title),
                            )
                            doc_id = cur.fetchone()["id"]
                            log.debug(f"Upserted document {rel_path} → id={doc_id}")
                        except psycopg.errors.UniqueViolation as e:
                            # Конфликт по UNIQUE(content_hash) — другой процесс уже вставил этот контент.
                            # Находим существующий документ по content_hash и используем его.
                            conn.rollback()
                            cur.execute(
                                "SELECT id, path FROM documents WHERE content_hash = %s",
                                (c_hash,),
                            )
                            existing = cur.fetchone()
                            if not existing:
                                # Неожиданно: конфликт был, но запись исчезла — пробуем снова
                                raise
                            doc_id = existing["id"]
                            existing_path = existing["path"]
                            log.info(
                                f"Content hash conflict for {rel_path}: using existing document "
                                f"{existing_path} (doc_id={doc_id})"
                            )

                        # ── Удаляем старые чанки ──
                        cur.execute("DELETE FROM chunks WHERE document_id = %s", (doc_id,))
                        log.debug(f"Deleted old chunks for document {doc_id}")

                        # ── Вставка новых чанков ──
                        for chunk_obj, emb in zip(chunks, all_embeddings):
                            cur.execute(
                                """
                                INSERT INTO chunks (document_id, chunk_index, text, metadata, embedding)
                                VALUES (%s, %s, %s, %s, %s)
                                """,
                                (
                                    doc_id,
                                    chunk_obj.chunk_index,
                                    chunk_obj.text,
                                    json.dumps(chunk_obj.metadata, ensure_ascii=False),
                                    emb.tolist(),
                                ),
                            )
                        log.debug(f"Inserted {len(all_embeddings)} chunks for document {doc_id}")

                    conn.commit()
                    log.debug(f"Committed {rel_path}")
                    indexed += 1
                else:
                    log.warning(f"No chunks or embeddings for {rel_path}, skipping")

            # Анализ для query planner
            with conn.cursor() as cur:
                cur.execute("ANALYZE chunks")
                log.debug("ANALYZE chunks completed")
                conn.commit()

        total_dt = time.monotonic() - t_start
        if args.rebuild_embeddings:
            log.info(f"Done. Rebuilt embeddings for {rebuilt} documents in {total_dt:.1f}s")
        else:
            log.info(f"Done. Indexed: {indexed}, Skipped (unchanged): {skipped} in {total_dt:.1f}s")

        if total_embed_time > 0:
            log.info(f"Embedding total: {total_chunks} chunks in {total_embed_time:.1f}s ({total_chunks/total_embed_time:.2f} chunk/s)")
        else:
            log.info(f"Embedding total: {total_chunks} chunks")

    except psycopg.OperationalError as e:
        log.error(f"Database connection failed: {e}")
        log.error("Make sure PostgreSQL is running: docker compose up -d")
        sys.exit(1)
    except Exception as e:
        log.error(f"Indexing failed: {e}")
        log.debug(f"Exception details: {e}", exc_info=True)
        sys.exit(1)
    finally:
        pool.close()


if __name__ == "__main__":
    main()
