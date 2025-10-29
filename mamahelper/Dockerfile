# Dockerfile
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Часовой пояс внутри контейнера (для zoneinfo)
RUN apt-get update -y && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# Зависимости Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код бота
COPY . .

# Запуск polling-бота
CMD ["python", "-m", "app.main"]
