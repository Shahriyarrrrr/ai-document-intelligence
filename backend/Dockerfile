FROM python:3.13-slim

# tesseract: OCR engine. libgl1/libglib2.0: OpenCV runtime deps.
# poppler-utils: PDF utilities used for diagnostics.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        libgl1 \
        libglib2.0-0 \
        poppler-utils \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so the layer caches across source changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user: the container handles untrusted uploaded files.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/storage \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
