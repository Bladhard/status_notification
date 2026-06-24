from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime, timezone, timedelta
import sqlite3
import threading
import requests
import logging
import time
import re
import os

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
    "status": "active",
    "stats": {
      "total_children": 2,
      "active_children": 1,
      "inactive_children": 1
    },
    "children": [
      {
        "name": "PLC1",
        "last_update": "2025-07-18T12:45:00",
        "status": "active",
        "notification": true
      },
      ...
    ]
  }
]
```

---

### POST /set_notification
Включает или выключает уведомления для под-объекта.

Параметры запроса:
{
  "object_name": "Energy_SolDar",
  "sub_object_name": "PLC1"
}

Пример ответа:
{
  "notification": true
}
---

---

### POST /pause
Приостанавливает мониторинг объекта или под-объекта.

Параметры запроса:
- object_name: str (обязательный)

Примеры:
- Приостановить весь объект: `/pause?object_name=Energy_SolDar`

---

### POST /resume
Возобновляет мониторинг объекта или под-объекта.

Аналогично /pause:
- `/resume?object_name=Energy_SolDar`

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


class NotificationToggle(BaseModel):
    object_name: str
    sub_object_name: str


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
            notification INTEGER DEFAULT 0,  -- Изменено с paused на notification
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
def update_status(request: Request, data: StatusUpdate):
    now = datetime.now(timezone.utc).isoformat()

    # Поддержка старого формата
    if not data.object_name and data.program_name and data.api_key:
        object_name = data.program_name
        sub_object_name = data.api_key
    else:
        if not data.object_name or not data.sub_object_name:
            raise HTTPException(
                status_code=400,
                detail="Missing object_name or sub_object_name"
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
            ON CONFLICT(object_id, name)
            DO UPDATE SET last_update = excluded.last_update
            """,
            (object_id, sub_object_name, now),
        )

        conn.commit()
    finally:
        conn.close()

    server_address = f"{request.url.scheme}://{request.url.netloc}"

    return {
        "status": "updated",
        "server": server_address
    }


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
                if sub["last_update"]:
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
                        "notification": bool(sub["notification"]),
                    }
                )

            current_status = "active" if active_children > 0 else "inactive"

            if obj["status"] != current_status:
                conn.execute(
                    "UPDATE objects SET status = ? WHERE id = ?",
                    (current_status, obj["id"]),
                )
                conn.commit()

            if current_status == "active":
                total_active_objects += 1
            else:
                total_inactive_objects += 1

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

        for item in result:
            if "children" in item and isinstance(item["children"], list):
                item["children"].sort(key=lambda x: natural_sort_key(x["name"]))

        return result
    finally:
        conn.close()


@app.post("/set_notification")
def toggle_notification(data: NotificationToggle):
    conn = get_db_connection()
    try:
        obj = conn.execute(
            "SELECT id FROM objects WHERE name = ?", (data.object_name,)
        ).fetchone()
        if not obj:
            raise HTTPException(status_code=404, detail="Object not found")

        object_id = obj["id"]
        sub = conn.execute(
            "SELECT id, notification FROM sub_objects WHERE object_id = ? AND name = ?",
            (object_id, data.sub_object_name),
        ).fetchone()
        if not sub:
            raise HTTPException(status_code=404, detail="Sub-object not found")

        new_state = 0 if sub["notification"] else 1
        conn.execute(
            "UPDATE sub_objects SET notification = ? WHERE id = ?",
            (new_state, sub["id"]),
        )
        conn.commit()
        return {"notification": bool(new_state)}
    finally:
        conn.close()


@app.post("/pause")
def pause_monitoring(object_name: str):
    conn = get_db_connection()
    try:
        conn.execute("UPDATE objects SET paused = 1 WHERE name = ?", (object_name,))
        conn.commit()
    finally:
        conn.close()
    return {"status": "paused"}


@app.post("/resume")
def resume_monitoring(object_name: str):
    conn = get_db_connection()
    try:
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


def send_status_alert(obj_name, is_active, sub_name=None):
    if sub_name:  # Для подсистем
        if is_active:
            send_telegram_message(f"✅ {obj_name} ➜ {sub_name} Восстановлен")
        else:
            send_telegram_message(f"❌ {obj_name} ➜ {sub_name} Недоступен")
    else:  # Для основного объекта
        status = "🟢 АКТИВЕН" if is_active else "🔴 НЕАКТИВЕН"
        send_telegram_message(f"{status} ➜ {obj_name}")


# Цикл мониторинга
def monitor_loop():
    object_statuses = {}  # Словарь для хранения предыдущих статусов объектов

    while True:
        time.sleep(CHECK_INTERVAL)
        now = datetime.now(timezone.utc)
        conn = get_db_connection()
        try:
            objects = conn.execute("SELECT id, name, paused FROM objects").fetchall()

            for obj in objects:
                obj_id = obj["id"]
                obj_name = obj["name"]

                if obj["paused"]:
                    continue  # Пропускаем приостановленные объекты

                subs = conn.execute(
                    "SELECT * FROM sub_objects WHERE object_id = ?", (obj_id,)
                ).fetchall()

                is_active = False
                for sub in subs:
                    if not sub["last_update"]:
                        continue

                    last = datetime.fromisoformat(sub["last_update"])
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)

                    # Проверка статуса под-объекта
                    sub_active = now - last <= ALLOWED_DELAY

                    # Индивидуальные уведомления для под-объектов с notification = 1
                    if sub["notification"]:
                        if not sub_active and sub["notified"] == 0:
                            # Под-объект неактивен, отправляем уведомление
                            send_status_alert(obj_name, False, sub["name"])

                            conn.execute(
                                "UPDATE sub_objects SET notified = 1 WHERE id = ?",
                                (sub["id"],),
                            )
                        elif sub_active and sub["notified"] == 1:
                            # Под-объект восстановлен, отправляем уведомление
                            send_status_alert(obj_name, True, sub["name"])

                            conn.execute(
                                "UPDATE sub_objects SET notified = 0 WHERE id = ?",
                                (sub["id"],),
                            )

                    # Если под-объект активен, объект считается активным
                    if sub_active:
                        is_active = True

                # Проверка изменения статуса объекта
                if obj_id in object_statuses:
                    if object_statuses[obj_id] != is_active:
                        send_status_alert(obj_name, is_active)

                        conn.execute(
                            "UPDATE objects SET status = ? WHERE id = ?",
                            ("active" if is_active else "inactive", obj_id),
                        )

                object_statuses[obj_id] = is_active

            conn.commit()

        except Exception as e:
            logging.error(f"Ошибка мониторинга: {e}")
            send_telegram_message(f"Ошибка мониторинга: {e}")
        finally:
            conn.close()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5000)
