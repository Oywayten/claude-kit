FROM python:3.12-slim AS base

# Системные зависимости для sentence-transformers / torch
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Кэшируем установку зависимостей
COPY requirements.txt .
# Ставим CPU-only torch отдельно, чтобы не тянуть CUDA (экономит ~1.5 GB)
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
    torch>=2.0.0 && \
    pip install --no-cache-dir -r requirements.txt

# Копируем код сервера
COPY mcp_rag_server.py .


ENV PYTHONUNBUFFERED=1

# MCP stdio: сервер читает из stdin, пишет в stdout
CMD ["python", "mcp_rag_server.py"]
