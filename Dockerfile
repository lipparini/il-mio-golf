FROM mcr.microsoft.com/playwright/python:v1.49.0-noble
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN playwright install chromium
EXPOSE 8080
CMD gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 2 --timeout 120 "app:create_app()"
