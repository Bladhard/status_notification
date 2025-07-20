from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
import sqlite3
import threading
import requests
import logging
import os
import time
import re

# Конфигурация
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 30))
ALLOWED_DELAY = timedelta(seconds=int(os.getenv("ALLOWED_DELAY", 300)))
DATABASE = "monitoring.db"

# Логирование
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# FastAPI
app = FastAPI(
    title="Monitoring System API",
    version="1.1",
    description="""
## Эндпоинты:

### POST /update_status
Обновляет статус под-объекта (или создаёт его, если он отсутствует).

Пример запроса:
```json
{
  "object_name": "Energy_SolDar",
  "sub_object_name": "PLC1"
}
```

---

### GET /status_tree
Возвращает статус всех объектов и их под-объектов:

Пример ответа:
```json
[
  {
    "name": "Energy_SolDar",
    "paused": false,
    "children": [
      {
        "name": "PLC1",
        "last_update": "2025-07-18T12:45:00",
        "status": "active",
        "paused": false
      },
      ...
    ]
  }
]
```

---

### POST /pause
Приостанавливает мониторинг объекта или под-объекта.

Параметры запроса:
- object_name: str (обязательный)
- sub_object_name: str (опционально)

Примеры:
- Приостановить весь объект: `/pause?object_name=Energy_SolDar`
- Приостановить только под-объект: `/pause?object_name=Energy_SolDar&sub_object_name=PLC1`

---

### POST /resume
Возобновляет мониторинг объекта или под-объекта.

Аналогично /pause:
- `/resume?object_name=Energy_SolDar`
- `/resume?object_name=Energy_SolDar&sub_object_name=PLC1`

---

### POST /delete
Удаляет объект или под-объект.

Параметры запроса:
- object_name: str (обязательный)
- sub_object_name: str (опционально)

Примеры:
- Удалить объект: `/delete?object_name=Energy_SolDar`
- Удалить под-объект: `/delete?object_name=Energy_SolDar&sub_object_name=PLC1`

---
""",
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


# Модель
class StatusUpdate(BaseModel):
    object_name: str = None
    sub_object_name: str = None
    program_name: str = None
    api_key: str = None


# Инициализация БД
def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def natural_sort_key(s):
    """Ключ для сортировки с учётом чисел в строке"""
    if s is None:
        return []
    return [
        int(text) if text.isdigit() else text.lower()
        for text in re.split(r"(\d+)", str(s))
    ]


def init_db():
    conn = get_db_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS objects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            paused INTEGER DEFAULT 0,
            status TEXT DEFAULT 'inactive'
        )
    """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sub_objects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            object_id INTEGER,
            name TEXT,
            last_update TEXT,
            notified INTEGER DEFAULT 0,
            paused INTEGER DEFAULT 0,
            UNIQUE(object_id, name),
            FOREIGN KEY(object_id) REFERENCES objects(id) ON DELETE CASCADE
        )
    """
    )
    conn.commit()
    conn.close()


@app.on_event("startup")
def on_startup():
    init_db()
    threading.Thread(target=monitor_loop, daemon=True).start()


@app.post("/update_status")
def update_status(data: StatusUpdate):
    now = datetime.now(timezone.utc).isoformat()

    # Поддержка старого формата
    if not data.object_name and data.program_name and data.api_key:
        object_name = data.program_name
        sub_object_name = data.api_key
    else:
        if not data.object_name or not data.sub_object_name:
            raise HTTPException(
                status_code=400, detail="Missing object_name or sub_object_name"
            )
        object_name = data.object_name
        sub_object_name = data.sub_object_name

    conn = get_db_connection()
    try:
        object_row = conn.execute(
            "SELECT id FROM objects WHERE name = ?", (object_name,)
        ).fetchone()
        if not object_row:
            conn.execute("INSERT INTO objects (name) VALUES (?)", (object_name,))
            conn.commit()
            object_row = conn.execute(
                "SELECT id FROM objects WHERE name = ?", (object_name,)
            ).fetchone()

        object_id = object_row["id"]
        conn.execute(
            """
            INSERT INTO sub_objects (object_id, name, last_update, notified)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(object_id, name) DO UPDATE SET last_update = excluded.last_update
        """,
            (object_id, sub_object_name, now),
        )
        conn.commit()
        logging.info(f"Получен сигнал от {object_name}::{sub_object_name}")
    finally:
        conn.close()
    return {"status": "updated"}


@app.get("/status_tree")
def get_status_tree():
    now = datetime.now(timezone.utc)
    conn = get_db_connection()
    result = []
    total_active_objects = 0
    total_inactive_objects = 0
    try:
        objects = conn.execute("SELECT * FROM objects").fetchall()

        for obj in objects:
            sub_objects = conn.execute(
                "SELECT * FROM sub_objects WHERE object_id = ?", (obj["id"],)
            ).fetchall()

            children = []
            total_children = len(sub_objects)
            active_children = 0
            inactive_children = 0

            for sub in sub_objects:
                status = "inactive"
                if sub["last_update"] and not sub["paused"]:
                    last = datetime.fromisoformat(sub["last_update"])
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    if now - last <= ALLOWED_DELAY:
                        status = "active"
                        active_children += 1
                    else:
                        inactive_children += 1
                else:
                    inactive_children += 1

                children.append(
                    {
                        "name": sub["name"],
                        "last_update": sub["last_update"],
                        "status": status,
                        "paused": bool(sub["paused"]),
                    }
                )

                # Определяем статус объекта
            current_status = "active" if active_children > 0 else "inactive"

            # Обновляем статус в БД, если он изменился
            if obj["status"] != current_status:
                conn.execute(
                    "UPDATE objects SET status = ? WHERE id = ?",
                    (current_status, obj["id"]),
                )
                conn.commit()

            # Обновляем общую статистику
            if current_status == "active":
                total_active_objects += 1
            else:
                total_inactive_objects += 1

            # Добавляем объект в результат
            result.append(
                {
                    "name": obj["name"],
                    "paused": bool(obj["paused"]),
                    "status": current_status,
                    "stats": {
                        "total_children": total_children,
                        "active_children": active_children,
                        "inactive_children": inactive_children,
                    },
                    "children": children,
                }
            )

        # Сортировка дочерних элементов в каждом объекте по имени с учетом чисел
        for item in result:
            if "children" in item and isinstance(item["children"], list):
                item["children"].sort(key=lambda x: natural_sort_key(x["name"]))

        return result
    finally:
        conn.close()


@app.post("/pause")
def pause_monitoring(object_name: str, sub_object_name: str = None):
    conn = get_db_connection()
    try:
        if sub_object_name:
            conn.execute(
                """
                UPDATE sub_objects SET paused = 1
                WHERE name = ? AND object_id = (SELECT id FROM objects WHERE name = ?)
            """,
                (sub_object_name, object_name),
            )
        else:
            conn.execute("UPDATE objects SET paused = 1 WHERE name = ?", (object_name,))
        conn.commit()
    finally:
        conn.close()
    return {"status": "paused"}


@app.post("/resume")
def resume_monitoring(object_name: str, sub_object_name: str = None):
    conn = get_db_connection()
    try:
        if sub_object_name:
            conn.execute(
                """
                UPDATE sub_objects SET paused = 0
                WHERE name = ? AND object_id = (SELECT id FROM objects WHERE name = ?)
            """,
                (sub_object_name, object_name),
            )
        else:
            conn.execute("UPDATE objects SET paused = 0 WHERE name = ?", (object_name,))
        conn.commit()
    finally:
        conn.close()
    return {"status": "resumed"}


@app.post("/delete")
def delete(object_name: str, sub_object_name: str = None):
    conn = get_db_connection()
    try:
        if sub_object_name:
            conn.execute(
                """
                DELETE FROM sub_objects
                WHERE name = ? AND object_id = (SELECT id FROM objects WHERE name = ?)
            """,
                (sub_object_name, object_name),
            )
        else:
            conn.execute("DELETE FROM objects WHERE name = ?", (object_name,))
        conn.commit()
    finally:
        conn.close()
    return {"status": "deleted"}


# Telegram уведомления
def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        logging.error(f"Ошибка отправки в Telegram: {e}")


# Цикл мониторинга
def monitor_loop():
    object_statuses = {}  # Словарь для хранения предыдущих статусов объектов

    while True:
        time.sleep(CHECK_INTERVAL)
        now = datetime.now(timezone.utc)
        conn = get_db_connection()
        try:
            objects = conn.execute("SELECT id, name, paused FROM objects").fetchall()

            # Проходим по всем объектам
            for obj in objects:
                obj_id = obj["id"]
                obj_name = obj["name"]

                if obj["paused"]:
                    continue  # Пропускаем приостановленные объекты

                # Получаем все под-объекты для текущего объекта
                subs = conn.execute(
                    "SELECT * FROM sub_objects WHERE object_id = ?", (obj_id,)
                ).fetchall()

                # Проверяем статус объекта (активен, если хотя бы один под-объект активен)
                is_active = False
                for sub in subs:
                    if sub["paused"] or not sub["last_update"]:
                        continue

                    last = datetime.fromisoformat(sub["last_update"])
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)

                    # Если нашли хотя бы один активный под-объект
                    if now - last <= ALLOWED_DELAY:
                        is_active = True
                        break

                # Проверяем, изменился ли статус объекта
                if obj_id in object_statuses:
                    if object_statuses[obj_id] != is_active:
                        # Статус изменился, отправляем уведомление
                        status_text = "🟢 Активен" if is_active else "🔴 Неактивен"
                        send_telegram_message(f"{obj_name}: {status_text}")

                        # Обновляем статус в БД
                        conn.execute(
                            "UPDATE objects SET status = ? WHERE id = ?",
                            ("active" if is_active else "inactive", obj_id),
                        )

                # Обновляем статус в словаре
                object_statuses[obj_id] = is_active

                # Обновляем под-объекты (без уведомлений)
                for sub in subs:
                    if sub["paused"] or not sub["last_update"]:
                        continue

                    last = datetime.fromisoformat(sub["last_update"])
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)

                    # Обновляем только флаг notified
                    if now - last > ALLOWED_DELAY:
                        conn.execute(
                            "UPDATE sub_objects SET notified = 1 WHERE id = ?",
                            (sub["id"],),
                        )
                    else:
                        conn.execute(
                            "UPDATE sub_objects SET notified = 0 WHERE id = ?",
                            (sub["id"],),
                        )

            conn.commit()

        except Exception as e:
            logging.error(f"Ошибка мониторинга: {e}")
            send_telegram_message(f"Ошибка мониторинга: {e}")
        finally:
            conn.close()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5000)
