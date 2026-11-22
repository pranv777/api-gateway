FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
RUN pip install "fastapi>=0.115" "uvicorn>=0.30" "pydantic>=2.7"
COPY main.py .
RUN useradd --create-home --uid 10001 appuser
USER appuser
EXPOSE 8002
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"]
