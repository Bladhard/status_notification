from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime, timedelta
import os
import sqlite3
import configparser
import time
import threading
import requests
import logging
from fastapi.middleware.cors import CORSMiddleware

# Загрузка конфигурации
load_dotenv()
config = configparser.ConfigParser()
config.read("config.ini")

try:
    DATABASE = config["database"]["path"]
    ALLOWED_DELAY = timedelta(seconds=int(config["monitoring"]["allowed_delay"]))
    CHECK_INTERVAL = int(config["monitoring"]["check_interval"])
    AUTHORIZED_KEYS = config["security"]["api_keys"].split(",")
    CHAT_ID = config["security"]["chat_id"]
except KeyError as e:
    raise ValueError(f"Ошибка в конфигурации: отсутствует ключ {e}")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Необходимо указать TELEGRAM_BOT_TOKEN в .env")

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


app = FastAPI(
    title="Status Notification Service",
    description="API для отправки уведомлений о статусе программ",
    version="0.1",
    redoc_url=None,
    docs_url=None,
)

# Настройка CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000",  # Локальный сервер
        "http://127.0.0.1:8000",  # Локальный сервер (альтернатива)
        "*",  # Разрешить все источники (используйте с осторожностью)
    ],
    allow_credentials=True,
    allow_methods=["*"],  # Разрешить все HTTP-методы
    allow_headers=["*"],  # Разрешить все заголовки
)


def get_db_connection():
    """Функция для получения соединения с базой данных."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


# Модель данных для API
class StatusUpdate(BaseModel):
    program_name: str
    api_key: str


@app.on_event("startup")
def startup():
    """Инициализация базы данных и запуск мониторинга."""
    conn = get_db_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS program_status (
            program_name TEXT PRIMARY KEY,
            last_update DATETIME,
            notified INTEGER DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()

    # Запускаем мониторинг в отдельном потоке
    threading.Thread(target=monitor_programs, daemon=True).start()


@app.post("/update_status")
async def update_status(update: StatusUpdate):
    """
    Endpoint для обновления статуса программы.
    """
    # Проверка API-ключа
    if update.api_key not in AUTHORIZED_KEYS:
        raise HTTPException(status_code=401, detail="Unauthorized")

    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO program_status (program_name, last_update, notified) VALUES (?, ?, 0) "
            "ON CONFLICT(program_name) DO UPDATE SET last_update = ?",
            (update.program_name, datetime.now(), datetime.now()),
        )
        conn.commit()
        logging.info(f"Status for {update.program_name} updated")
        return {"message": f"Status for {update.program_name} updated"}
    finally:
        conn.close()


@app.get("/check_status")
async def check_status():
    """
    Endpoint для проверки статуса программ.
    """
    conn = get_db_connection()
    try:
        now = datetime.now()
        cursor = conn.execute("SELECT program_name, last_update FROM program_status")
        inactive_programs = [
            row["program_name"]
            for row in cursor.fetchall()
            if now - datetime.fromisoformat(row["last_update"]) > ALLOWED_DELAY
        ]
        return {
            "inactive_programs": inactive_programs,
            "all_programs": [
                row["program_name"]
                for row in conn.execute("SELECT program_name FROM program_status")
            ],
        }
    finally:
        conn.close()


def send_telegram_message(message):
    """
    Отправка уведомлений в Telegram.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
    }
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
    except requests.RequestException as e:
        logging.error(f"Ошибка отправки сообщения в Telegram: {e}")


def monitor_programs():
    """
    Мониторинг статуса программ.
    """
    while True:
        time.sleep(CHECK_INTERVAL)
        try:
            now = datetime.now()
            conn = get_db_connection()

            # Проверка на неактивные программы
            cursor = conn.execute(
                "SELECT program_name, last_update, notified FROM program_status"
            )
            programs = cursor.fetchall()

            for row in programs:
                program_name = row["program_name"]
                last_update = datetime.fromisoformat(row["last_update"])
                notified = row["notified"]

                print(now - last_update > ALLOWED_DELAY)
                print(notified)
                if now - last_update > ALLOWED_DELAY:
                    if not notified:
                        send_telegram_message(
                            f"🔴 Объект {program_name} перестал отправлять статус‼️"
                        )
                        conn.execute(
                            "UPDATE program_status SET notified = 1 WHERE program_name = ?",
                            (program_name,),
                        )
                else:
                    if notified:
                        send_telegram_message(f"🟢 Объект {program_name} в работе ✅")
                        conn.execute(
                            "UPDATE program_status SET notified = 0, last_update = ? WHERE program_name = ?",
                            (datetime.now(), program_name),
                        )

            conn.commit()
            conn.close()

        except Exception as e:
            logging.error(f"Ошибка мониторинга: {e}")
            send_telegram_message(f"Ошибка мониторинга: {e}")


# if __name__ == "__main__":
#     import uvicorn

#     uvicorn.run(app, host="0.0.0.0", port=5000)
