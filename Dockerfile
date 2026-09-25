FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MAGPIE_DATA_DIR=/data
WORKDIR /app

# libgl/libglib: OpenCV (used by the bundled OCR). tesseract: optional OCR engine with
# Hebrew/Arabic/... support (MAGPIE_OCR=tesseract); add more tesseract-ocr-<lang> packages as needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 tesseract-ocr tesseract-ocr-eng tesseract-ocr-heb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY magpie ./magpie
RUN useradd --system --uid 1000 magpie && mkdir -p /data && chown magpie /data
USER magpie
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["uvicorn", "magpie.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
