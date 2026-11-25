FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-fas tesseract-ocr-eng && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY tests ./tests
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
