# syntax=docker/dockerfile:1
FROM python:3.13-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
# tesseract-ocr: OCR for images/scanned pages. poppler-utils: PDF rasterisation for scanned-page OCR.
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng poppler-utils \
 && rm -rf /var/lib/apt/lists/*
# Dependencies first (cached layer). All versions are pinned in requirements.txt.
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh \
 && useradd --system --uid 10001 --create-home nexus \
 && mkdir -p /data && chown -R nexus:nexus /data /app
USER nexus
ENV NEXUS_STORAGE_DIR=/data/storage NEXUS_CHECKPOINT_PATH=/data/checkpoints.sqlite
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2).status==200 else 1)"
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
