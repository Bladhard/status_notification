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

# Загрузка конфигурации
load_dotenv()
config = configparser.ConfigParser()
config.read("config.ini")
DATABASE = config["database"]["path"]
ALLOWED_DELAY = timedelta(seconds=int(config["monitoring"]["allowed_delay"]))
CHECK_INTERVAL = int(config["monitoring"]["check_interval"])
AUTHORIZED_KEYS = config["security"]["api_keys"].split(",")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = config["security"]["chat_id"]

app = FastAPI()


def get_db_connection():
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS program_status (
            program_name TEXT PRIMARY KEY,
            last_update DATETIME
        )
    """)
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
            "INSERT INTO program_status (program_name, last_update) VALUES (?, ?) "
            "ON CONFLICT(program_name) DO UPDATE SET last_update = ?",
            (update.program_name, datetime.now(), datetime.now()),
        )
        conn.commit()
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
    requests.post(url, json=payload)


def monitor_programs():
    """
    Мониторинг статуса программ.
    """
    while True:
        time.sleep(CHECK_INTERVAL)
        try:
            now = datetime.now()
            conn = get_db_connection()
            cursor = conn.execute(
                "SELECT program_name, last_update FROM program_status"
            )
            inactive_programs = [
                row["program_name"]
                for row in cursor.fetchall()
                if now - datetime.fromisoformat(row["last_update"]) > ALLOWED_DELAY
            ]
            conn.close()

            # Уведомление о неактивных программах
            for program in inactive_programs:
                send_telegram_message(
                    f"🔴 Объект {program} перестал отправлять статус‼️"
                )

        except Exception as e:
            send_telegram_message(f"Ошибка мониторинга: {e}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5000)
