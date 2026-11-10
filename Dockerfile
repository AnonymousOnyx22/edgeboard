# Hosted Edgeboard. Mount a persistent disk at /data so the database and the
# ESPN/odds caches survive a redeploy.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EDGEBOARD_DB=/data/db.sqlite \
    PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /data

EXPOSE 8000
CMD ["sh", "-c", "waitress-serve --host 0.0.0.0 --port ${PORT} --threads 8 wsgi:app"]
