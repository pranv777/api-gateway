FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY pyproject.toml ./
RUN pip install .

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY benchmarks ./benchmarks

RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /srv
USER appuser
EXPOSE 8000
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8000"]

# Adds test tooling and the test suite (used by the `test` service in docker-compose.yml).
FROM base AS dev
USER root
RUN pip install ".[dev]"
COPY --chown=appuser:appuser tests ./tests
USER appuser
CMD ["pytest"]
