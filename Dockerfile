
FROM python:3.11

# Рабочая директория
WORKDIR /status_notification

# Установка зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Создание директории для данных
RUN mkdir -p /status_notification/data

# Копирование приложения
COPY . .

# Запуск Gunicorn
CMD ["gunicorn", "app:app", "--workers", "1", "--worker-class", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8050", "--timeout", "30", "--graceful-timeout", "10", "--keep-alive", "2", "--max-requests", "10000", "--max-requests-jitter", "1000", "--access-logfile", "-", "--error-logfile", "-"]
