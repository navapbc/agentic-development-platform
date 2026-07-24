FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY services ./services
COPY adapters ./adapters
COPY schemas ./schemas
COPY database ./database
COPY scripts ./scripts

RUN pip install --upgrade pip && pip install -e ".[reasoning]"

RUN useradd --create-home --uid 10001 appuser
USER appuser

CMD ["python", "-m", "services.orchestration.worker"]
