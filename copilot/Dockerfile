FROM python:3.12-slim

WORKDIR /app

# System deps for PyMySQL are pure-python; no build tools needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY ui ./ui
# Guideline corpus — required by the RAG index (its absence in the image was
# caught by the /ready vector_index probe on first prod deploy: retrieval ran
# over an empty corpus while local runs always had the files).
COPY corpus ./corpus

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# Railway injects $PORT. Shell form so it expands.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
