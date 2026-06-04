FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-nld \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY annemieke_app ./annemieke_app
COPY logo.webp ./logo.webp

RUN pip install --no-cache-dir .

EXPOSE 9000

CMD ["uvicorn", "annemieke_app.main:app", "--host", "0.0.0.0", "--port", "9000"]
