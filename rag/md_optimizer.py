#!/usr/bin/env python3
"""
Оптимизатор Markdown для RAG-индексации.

Приводит Markdown, полученный из PDF (через Docling), к виду,
оптимальному для чанкинга и поиска в RAG-системе.

Чек-лист оптимизации:
1. Удаление шума (колонтитулы, номера страниц, водяные знаки)
2. Нормализация заголовков (иерархия, пробелы)
3. Исправление таблиц (синтаксис, выравнивание)
4. Нормализация блоков кода (язык, артефакты)
5. Нормализация списков (отступы, bullet-символы)
6. Дедупликация параграфов
7. Схлопывание пустых секций и лишних пробелов
8. Удаление битых ссылок и спецсимволов
9. Добавление frontmatter (метаданные)
"""

import re
import logging
from collections import Counter
from datetime import date
import yaml

log = logging.getLogger("md_optimizer")


def clean_whitespace(text: str) -> str:
    """
    Нормализует пробелы: схлопывает множественные пустые строки,
    убирает пробелы в концах строк, нормализует переводы строк.
    """
    # Убираем символы \r (возврат каретки)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Убираем пробелы и табы в концах строк
    text = re.sub(r"[ \t]+\n", "\n", text)
    # Схлопываем 3+ пустые строки в 2 (оставляем абзацы)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def remove_headers_footers(text: str, frequency_threshold: int = 0) -> str:
    """
    Удаляет колонтитулы и номера страниц.
    Паттерны: "Page N", "Страница N", "N", отдельные цифры на строке с окружением.
    Также пытается удалить повторяющиеся строки-колонтитулы (автор, название главы).

    Args:
        text: Исходный текст
        frequency_threshold: Если > 0, удаляет строки, встречающиеся чаще
                            указанного числа раз (частотный анализ).
                            Значение 0 отключает частотный анализ.

    Использует эвристики:
    1. Простые паттерны номеров страниц
    2. Частотный анализ (если включён) — строки, повторяющиеся > N раз
    3. Позиционный анализ — строки на одинаковых позициях в разных блоках
    """
    lines = text.splitlines()
    
    # Step 1: Simple page number patterns
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if re.match(r"^\d+$", stripped) and len(stripped) <= 5:
            continue
        if re.match(r"^(Page|Страница|Стр\.|Pág\.)\s*\d+", stripped, re.IGNORECASE):
            continue
        if re.match(r"^[-—=]{3,}\s*\d+\s*[-—=]{3,}$", stripped):
            continue
        cleaned.append(line)
    text = "\n".join(cleaned)
    
    # Step 2: Frequency-based detection (if enabled)
    if frequency_threshold > 0:
        lines = text.splitlines()
        freq = Counter()
        for line in lines:
            stripped = line.strip()
            if stripped and len(stripped) > 10 \
                    and not re.match(r"^#{1,6}\s", stripped) \
                    and not re.match(r"^\s*(?:[-*+•∙]|\d+[.)])\s", stripped) \
                    and not stripped.startswith("|"):
                freq[stripped] += 1
        
        too_frequent = {line for line, count in freq.items() if count > frequency_threshold}
        cleaned = [line for line in lines if line.strip() not in too_frequent]
        text = "\n".join(cleaned)
    
    # Step 3: Position-based smart detection
    lines = text.splitlines()
    blocks = [b.splitlines() for b in re.split(r"\n\n+", text) if b.strip()]
    
    first_lines = Counter()
    last_lines = Counter()
    
    for block_lines in blocks:
        for i, line in enumerate(block_lines[:3]):
            stripped = line.strip()
            if stripped and not re.match(r"^#{1,6}\s", stripped) \
                    and not re.match(r"^\s*(?:[-*+•∙]|\d+[.)])\s", stripped) \
                    and not stripped.startswith("|"):
                first_lines[(i, stripped)] += 1
        blen = len(block_lines)
        for j, line in enumerate(block_lines[-3:]):
            # Use relative position (0, 1, 2) for cross-block deduplication
            stripped = line.strip()
            if stripped and not re.match(r"^#{1,6}\s", stripped) \
                    and not re.match(r"^\s*(?:[-*+•∙]|\d+[.)])\s", stripped) \
                    and not stripped.startswith("|"):
                last_lines[(j, stripped)] += 1
    
    repeated_first = {pos for pos, count in first_lines.items() if count > 1}
    repeated_last = {pos for pos, count in last_lines.items() if count > 1}
    
    result_lines = []
    for block_lines in blocks:
        block_result = []
        blen = len(block_lines)
        for i, line in enumerate(block_lines):
            stripped = line.strip()
            is_repeated = (i, stripped) in repeated_first
            if blen >= 3 and i >= blen - 3:
                rel_pos = i - (blen - 3)  # 0, 1, or 2
                if (rel_pos, stripped) in repeated_last:
                    is_repeated = True
            if not is_repeated:
                block_result.append(line)
        if block_result:
            result_lines.extend(block_result)
            result_lines.append("")
    
    return "\n".join(result_lines).strip()


def normalize_headings(text: str) -> str:
    """
    Нормализует заголовки:
    - Убирает пробелы между # и текстом ("#  Title" → "# Title")
    - Убирает точки в конце заголовков ("## Глава 1." → "## Глава 1")
    - Убирает номера страниц в заголовках
    """
    def fix_heading(m):
        prefix = m.group(1)
        text_content = m.group(2).strip()
        # Убираем точку в конце
        text_content = re.sub(r'\.$', '', text_content)
        # Убираем номер страницы в скобках
        text_content = re.sub(r"\s*\(стр\.\s*\d+\)\s*$", "", text_content)
        return f"{prefix} {text_content}"

    # Нормализуем заголовки: #, ##, ###, ####, #####, ######
    text = re.sub(r"^(#{1,6})\s+(.+)$", fix_heading, text, flags=re.MULTILINE)
    return text


def fix_tables(text: str) -> str:
    """
    Исправляет таблицы:
    - Убирает пустые строки внутри таблиц
    - Добавляет разделитель, если его нет
    - Удаляет битые таблицы (менее 2 столбцов)
    """
    lines = text.splitlines()
    result = []
    in_table = False
    table_buffer = []

    def flush_table():
        nonlocal table_buffer
        if len(table_buffer) < 2:
            table_buffer = []
            return
        # Проверяем, есть ли разделитель
        has_separator = any("|" in line and "---" in line for line in table_buffer)
        if not has_separator and len(table_buffer) >= 2:
            # Пробуем добавить разделитель из первой строки
            first_line = table_buffer[0]
            # Strip leading/trailing | pipes, then split by |
            cells = first_line.strip("|").split("|")
            header_cols = len(cells)
            if header_cols >= 1:
                separator = "|" + "|".join(["---"] * header_cols) + "|"
                table_buffer.insert(1, separator)
        # Фильтруем: если таблица валидна (есть заголовок и разделитель)
        has_sep = any("|" in line and "---" in line for line in table_buffer)
        if has_sep and len(table_buffer) >= 2:
            result.extend(table_buffer)
            # Добавляем пустую строку после таблицы
            result.append("")
        table_buffer = []

    for line in lines:
        if "|" in line and line.strip().startswith("|"):
            in_table = True
            table_buffer.append(line)
        else:
            if in_table:
                flush_table()
                in_table = False
            result.append(line)

    if in_table:
        flush_table()

    return "\n".join(result)


def normalize_code_blocks(text: str, default_lang: str = "text") -> str:
    """
    Нормализует блоки кода:
    - Добавляет языковой идентификатор (по умолчанию default_lang), если его нет
    - Удаляет разрывы страниц и артефакты номеров строк внутри блоков кода
    - Обеспечивает правильные пустые строки вокруг блоков кода

    Args:
        text: Исходный текст Markdown
        default_lang: Язык кода по умолчанию для блоков без указания языка
    """
    def fix_code_block(m):
        lang = (m.group(1) or default_lang).strip()
        code = m.group(2)
        # Remove line-number artifacts like "1 | " or "  1  | " at line starts
        code = re.sub(r"^\s*\d+\s*\| ", "", code, flags=re.MULTILINE)
        return f"```{lang}\n{code.strip()}\n```\n"

    # Match ``` optionally followed by a language identifier, then code, then ```
    text = re.sub(
        r"```(\w*)\s*\n(.*?)```",
        fix_code_block,
        text,
        flags=re.DOTALL,
    )
    return text


def normalize_lists(text: str) -> str:
    """
    Нормализует списки:
    - Обеспечивает согласованный отступ для вложенных списков (2 пробела на уровень)
    - Удаляет пустые строки внутри списков (списки должны быть непрерывными)
    - Нормализует символы маркеров (*, •, ∙) к -
    """
    lines = text.splitlines()
    result = []
    in_list = False
    list_buffer = []

    def flush_list():
        nonlocal list_buffer
        if not list_buffer:
            return
        normalized = []
        for line in list_buffer:
            # Normalize bullet characters: *, •, ∙ → -
            line = re.sub(r"^(\s*)[•∙*](\s)", r"\1-\2", line)
            stripped = line.lstrip()
            if stripped.startswith("- ") or stripped.startswith("+ ") or re.match(r"^\d+[.)]\s", stripped):
                old_indent = len(line) - len(stripped)
                level = max(0, old_indent // 2)
                new_indent = " " * (level * 2)
                line = new_indent + stripped
            normalized.append(line)
        result.extend(normalized)
        result.append("")  # blank line after list
        list_buffer = []

    current_item_indent = 0
    for line in lines:
        stripped = line.strip()
        is_list_item = bool(re.match(r"^\s*(?:[-*+•∙]|\d+[.)])\s", line))
        if is_list_item:
            in_list = True
            list_buffer.append(line)
            current_item_indent = len(line) - len(line.lstrip())
        else:
            if in_list:
                if stripped == "":
                    # Remove blank lines inside lists
                    continue
                else:
                    # Check if line is a continuation of current list item
                    # (indented at least 2 spaces deeper than the marker, and no list marker)
                    line_indent = len(line) - len(line.lstrip())
                    if line_indent >= current_item_indent + 2:
                        # Continuation of current list item
                        list_buffer.append(line)
                        continue
                    else:
                        flush_list()
                        in_list = False
            result.append(line)

    if in_list:
        flush_list()

    return "\n".join(result)


def remove_noise(text: str) -> str:
    """
    Удаляет шум:
    - Замена символов-заменителей (U+FFFD)
    - Удаление управляющих символов (кроме \n, \t)
    - Удаление водяных знаков (частые строки вроде "Confidential")
    - Удаление разрывов страниц (\f, ---page---)
    """
    # Удаляем \f (form feed) и другие управляющие символы
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    # Заменяем U+FFFD (знак замены) на пробел
    text = text.replace("\ufffd", " ")
    # Удаляем разрывы страниц: "--- Page N ---" (наш формат из pypdf) и \f
    text = re.sub(r"---\s*Page\s*\d+\s*---", "", text, flags=re.IGNORECASE)
    # Удаляем водяные знаки (частые слова-маркеры)
    watermark_patterns = [
        r"(?i)^confidential\s*$",
        r"(?i)^draft\s*$",
        r"(?i)^internal\s*$",
        r"(?i)^for review only\s*$",
    ]
    for pat in watermark_patterns:
        text = re.sub(pat + r"\n?", "", text, flags=re.MULTILINE)

    return text


def compress_empty_sections(text: str) -> str:
    """
    Удаляет пустые секции — заголовки, за которыми нет текста,
    но сохраняет заголовки, имеющие только подзаголовки (для иерархии).
    Не добавляет лишних пустых строк между секциями — существующие
    разделители в исходном тексте сохраняются как есть.
    
    Two-pass алгоритм:
    Pass 1: Собираем все заголовки с номерами строк и уровнями.
    Pass 2: Для каждой секции проверяем, есть ли в ней текст
    (не считая подзаголовков). Если нет текста и нет дочерних
    заголовков — удаляем. Если есть дочерние заголовки — сохраняем.
    """
    lines = text.splitlines()
    # Find all headings with line numbers and levels
    headings = []  # list of (line_index, level, heading_text)
    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+", line)
        if m:
            level = len(m.group(1))
            headings.append((i, level, line))
    
    if not headings:
        return text
    
    # Determine which sections to keep
    keep_section = [True] * len(headings)
    for idx, (start, level, _) in enumerate(headings):
        # End of this section: next heading of same/higher level, or EOF
        end = headings[idx + 1][0] if idx + 1 < len(headings) else len(lines)
        
        # Content lines (excluding sub-headings)
        content_lines = []
        for line in lines[start + 1:end]:
            if not re.match(r"^#{1,6}\s", line):
                content_lines.append(line)
        content_text = "\n".join(content_lines).strip()
        
        if content_text:
            continue  # Has text → keep
        
        # Check for child headings (headings with deeper level)
        has_children = False
        for j in range(idx + 1, len(headings)):
            if headings[j][1] <= level:
                break
            has_children = True
        
        if has_children:
            continue  # No text but has children → keep for hierarchy
        
        # No text, no children → remove
        keep_section[idx] = False
    
    # Build result: iterate through sections and keep those marked.
    # The original blank lines between sections in `lines[start + 1:end]`
    # already provide proper separation, so no extra blank line is added.
    result = []
    # Preserve preamble: all text before the first heading
    if headings:
        result.extend(lines[:headings[0][0]])
    for idx, (start, level, heading_text) in enumerate(headings):
        end = headings[idx + 1][0] if idx + 1 < len(headings) else len(lines)
        if keep_section[idx]:
            result.append(heading_text)
            result.extend(lines[start + 1:end])
    
    return "\n".join(result).strip()


def deduplicate_paragraphs(text: str, threshold: float = 0.9) -> str:
    """
    Дедуплицирует параграфы:
    - Разбивает текст на параграфы (блоки, разделённые пустыми строками)
    - Удаляет точные дубликаты (O(n) через set)
    - Удаляет почти дублирующиеся параграфы (Jaccard > threshold)
    - Оптимизация: предварительное кэширование word set'ов
      и отсев по длине (для Jaccard > t, |min|/|max| >= t)
    - Сохраняет первое вхождение, удаляет последующие дубликаты

    Args:
        text: Исходный текст Markdown
        threshold: Порог схожести Жаккара (0.0-1.0), рекомендуется 0.9
    """
    paragraphs = re.split(r"\n\n+", text)
    if len(paragraphs) <= 1:
        return text

    def jaccard_from_sets(words_a: set, words_b: set) -> float:
        """Jaccard similarity from pre-computed word sets."""
        if not words_a or not words_b:
            return 0.0
        # Use smaller set as iteration base for speed
        if len(words_a) > len(words_b):
            words_a, words_b = words_b, words_a
        intersection_size = sum(1 for w in words_a if w in words_b)
        union_size = len(words_a) + len(words_b) - intersection_size
        return intersection_size / max(union_size, 1)

    # Phase 1: Pre-compute word sets + exact dedup
    seen_exact = set()           # set of stripped text for exact dedup
    candidates = []              # list of (word_set, original_text)

    for para in paragraphs:
        text_stripped = para.strip()
        if not text_stripped:
            continue

        # Exact dedup (O(1) per paragraph)
        if text_stripped in seen_exact:
            continue
        seen_exact.add(text_stripped)

        words = set(text_stripped.lower().split())
        if not words:
            candidates.append((set(), para))
        else:
            candidates.append((words, para))

    if not candidates:
        return ""

    # Phase 2: Near-duplicate detection with word-count pruning
    # Sort by word count to enable early break in inner loop
    # (we track original order separately)
    indexed = [(i, len(words), words, text) for i, (words, text) in enumerate(candidates)]
    indexed.sort(key=lambda x: x[1])  # sort by word count ascending

    keep = [True] * len(candidates)  # by original index

    # For each paragraph (in sorted order), check against previous ones
    for idx in range(len(indexed)):
        i, count_a, set_a, _ = indexed[idx]
        if not keep[i]:
            continue

        # Check against earlier (in sorted order) paragraphs
        for jdx in range(idx - 1, -1, -1):
            j, count_b, set_b, _ = indexed[jdx]
            if not keep[j]:
                continue

            # Pruning: word counts must be within threshold ratio
            # For Jaccard > t, min(count_a, count_b) / max(count_a, count_b) >= t
            smaller = count_a if count_a < count_b else count_b
            larger = count_b if count_a < count_b else count_a
            if smaller / max(larger, 1) < threshold:
                # Since sorted ascending, all earlier items have <= count_b
                # so ratio can only get worse → break
                break

            if jaccard_from_sets(set_a, set_b) > threshold:
                keep[i] = False
                break

    # Phase 3: Build result preserving original order
    result = [text for i, (_, text) in enumerate(candidates) if keep[i]]

    return "\n\n".join(result)


def add_frontmatter(text: str, title: str = "", source: str = "", **metadata) -> str:
    """
    Добавляет YAML frontmatter с метаданными в начало документа,
    если его там ещё нет.
    
    Args:
        text: Исходный текст Markdown
        title: Заголовок документа
        source: Источник (путь к файлу)
        **metadata: Дополнительные произвольные поля метаданных
    """
    # Проверка наличия существующего frontmatter (ищем --- на первой строке)
    lines = text.splitlines()
    if lines and re.match(r"^---\s*$", lines[0]):
        return text  # Уже есть frontmatter
    
    # Формируем метаданные
    meta = {}
    if title:
        meta["title"] = title
    if source:
        meta["source"] = source
    meta["date"] = date.today().isoformat()
    # Добавляем произвольные поля
    for key, value in metadata.items():
        if value:
            meta[key] = value
    
    if not meta:
        return text  # Nothing to add
    
    try:
        frontmatter = "---\n" + yaml.safe_dump(meta, allow_unicode=True, default_flow_style=False).strip() + "\n---\n\n"
    except Exception as e:
        log.warning(f"Failed to serialize metadata to YAML: {e}. Adding minimal frontmatter.")
        # Fallback: add only safe string fields
        safe_meta = {k: str(v) for k, v in meta.items() if isinstance(v, (str, int, float, bool))}
        frontmatter = "---\n" + yaml.safe_dump(safe_meta, allow_unicode=True, default_flow_style=False).strip() + "\n---\n\n"
    
    return frontmatter + text.lstrip()


def optimize_for_rag(
    text: str,
    title: str = "",
    source: str = "",
    dedup_threshold: float = 0.9,
    default_code_lang: str = "text",
    deduplicate: bool = True,
    remove_headers_frequency: int = 0,
    **metadata
) -> str:
    """
    Полный пайплайн оптимизации Markdown для RAG.
    Применяет все этапы очистки и нормализации.
    
    Args:
        text: Исходный текст Markdown
        title: Заголовок документа
        source: Источник (путь к файлу)
        dedup_threshold: Порог схожести для дедупликации (0.0-1.0)
        default_code_lang: Язык кода по умолчанию для блоков без указания языка
        deduplicate: Включать/отключать дедупликацию параграфов
        remove_headers_frequency: Частотный порог удаления колонтитулов (0 = отключено)
        **metadata: Дополнительные поля для YAML frontmatter
    """
    if text is None:
        return ""
    
    original_len = len(text)
    
    log.info("Step 1/9: Removing noise...")
    text = remove_noise(text)
    log.info(f"  → {len(text)} chars")
    
    log.info("Step 2/9: Cleaning whitespace...")
    text = clean_whitespace(text)
    log.info(f"  → {len(text)} chars")
    
    log.info("Step 3/9: Removing headers/footers...")
    text = remove_headers_footers(text, frequency_threshold=remove_headers_frequency)
    log.info(f"  → {len(text)} chars")
    
    log.info("Step 4/9: Normalizing headings...")
    text = normalize_headings(text)
    log.info(f"  → {len(text)} chars")
    
    log.info("Step 5/9: Normalizing code blocks...")
    text = normalize_code_blocks(text, default_lang=default_code_lang)
    log.info(f"  → {len(text)} chars")
    
    log.info("Step 6/9: Fixing tables...")
    text = fix_tables(text)
    log.info(f"  → {len(text)} chars")
    
    log.info("Step 7/9: Normalizing lists...")
    text = normalize_lists(text)
    log.info(f"  → {len(text)} chars")
    
    log.info("Step 8/9: Compressing empty sections...")
    text = compress_empty_sections(text)
    log.info(f"  → {len(text)} chars")
    
    log.info("Step 9/9: Deduplicating paragraphs...")
    if deduplicate:
        text = deduplicate_paragraphs(text, threshold=dedup_threshold)
    else:
        log.info("  (skipped)")
    
    log.info("Adding frontmatter...")
    text = add_frontmatter(text, title=title, source=source, **metadata)
    
    optimized_len = len(text)
    pct = 100 * (1 - optimized_len / max(original_len, 1))
    log.info(f"Optimized MD: {original_len} → {optimized_len} chars ({pct:.1f}% compression)")
    
    return text


if __name__ == "__main__":
    # Тест
    import sys
    sample = """#  Introduction  

This is a test.

Page 42

##  Methodology  

"""
    print("=== Before ===")
    print(repr(sample))
    print("=== After ===")
    result = optimize_for_rag(sample, title="Test Document", source="test.pdf")
    print(result)
    print("=== End ===")
