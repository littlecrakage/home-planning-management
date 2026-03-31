FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY app.py .
COPY static/ static/
COPY templates/ templates/

# DB lives in a mounted volume at /app/instance
VOLUME ["/app/instance"]

EXPOSE 8000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--preload", "app:app"]
