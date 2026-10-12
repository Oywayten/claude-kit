#!/usr/bin/env bash
# Раскладывает хуки, скиллы, команды и агентов по ~/.claude и подключает хуки в settings.json.
# Запуск: bash install.sh [--with-rag]
set -euo pipefail

KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${CLAUDE_HOME:-$HOME/.claude}"
WITH_RAG=0
[ "${1:-}" = "--with-rag" ] && WITH_RAG=1

# python
if command -v python >/dev/null 2>&1; then PY=python
elif command -v python3 >/dev/null 2>&1; then PY=python3
else echo "Нужен python 3 в PATH (хуки написаны на нём)." >&2; exit 1; fi

# путь к hooks в виде, понятном python (на Windows: C:/Users/...)
if command -v cygpath >/dev/null 2>&1; then HOOKS_DIR="$(cygpath -m "$CLAUDE_DIR/hooks")"; else HOOKS_DIR="$CLAUDE_DIR/hooks"; fi

export PYTHONIOENCODING=utf-8
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$CLAUDE_DIR/backups/kit-$STAMP"
mkdir -p "$CLAUDE_DIR"/hooks "$CLAUDE_DIR"/skills "$CLAUDE_DIR"/commands "$CLAUDE_DIR"/agents "$BACKUP"

# резервная копия того, что будет перезаписано; $1 = путь относительно CLAUDE_DIR
backup() {
  if [ -e "$CLAUDE_DIR/$1" ]; then mkdir -p "$BACKUP/$(dirname "$1")"; cp -r "$CLAUDE_DIR/$1" "$BACKUP/$1"; fi
}
[ -f "$CLAUDE_DIR/settings.json" ] && backup settings.json

for f in "$KIT"/payload/hooks/*.py; do backup "hooks/$(basename "$f")"; cp "$f" "$CLAUDE_DIR/hooks/"; done
for d in "$KIT"/payload/skills/*/; do n="$(basename "$d")"; backup "skills/$n"; rm -rf "$CLAUDE_DIR/skills/$n"; cp -r "$d" "$CLAUDE_DIR/skills/$n"; done
for f in "$KIT"/payload/commands/*; do backup "commands/$(basename "$f")"; cp "$f" "$CLAUDE_DIR/commands/"; done
for f in "$KIT"/payload/agents/*; do backup "agents/$(basename "$f")"; cp "$f" "$CLAUDE_DIR/agents/"; done

# CLAUDE.md не перезаписываем: это личные правила автора, кладём как пример
cp "$KIT/payload/CLAUDE.md.example" "$CLAUDE_DIR/CLAUDE.md.example"

# хуки в settings.json
"$PY" "$KIT/merge_settings.py" "$CLAUDE_DIR/settings.json" "$KIT/settings.hooks.template.json" "$PY" "$HOOKS_DIR"

# проверка: все файлы хуков компилируются
"$PY" -m py_compile "$CLAUDE_DIR"/hooks/*.py && echo "хуки: синтаксис ок"
rm -rf "$CLAUDE_DIR/hooks/__pycache__"

if [ "$WITH_RAG" = 1 ]; then
  RAG_DIR="${RAG_HOME:-$HOME/claude-rag}"
  mkdir -p "$RAG_DIR"; cp -r "$KIT"/rag/. "$RAG_DIR"/
  command -v docker >/dev/null 2>&1 || { echo "Для RAG нужен docker." >&2; exit 1; }
  ( cd "$RAG_DIR" && docker compose up -d && docker build -t mcp-rag-pgvector mcp-rag-server )
  if command -v claude >/dev/null 2>&1; then
    claude mcp add --scope user pgvector-rag -- docker run -i --rm --network rag-network \
      -e DATABASE_URL=postgresql://postgres:postgres@postgres:5432/rag_docs \
      -e EMBED_MODEL=BAAI/bge-m3 -e TOP_K_DEFAULT=5 -e LOG_LEVEL=INFO mcp-rag-pgvector
  else
    echo "claude CLI не найден: добавь MCP pgvector-rag вручную (см. README.md, раздел RAG)."
  fi
  echo "RAG: $RAG_DIR. База пустая: положи свои документы и запусти index.py (см. README.md в этой папке)."
fi

echo
echo "Готово. Резервная копия заменённого: $BACKUP"
echo "Перезапусти Claude Code. Проверка: новая сессия должна сама вызвать скиллы oh-my-god и caveman."
