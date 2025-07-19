FROM python:3.11

RUN mkdir /status_notification

WORKDIR /status_notification

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Создаем директорию для данных
RUN mkdir -p /status_notification/data

COPY . .

CMD gunicorn app:app --workers 1 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:5000