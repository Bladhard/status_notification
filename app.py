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
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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


def init_db():
    conn = get_db_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS objects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            paused INTEGER DEFAULT 0
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
    finally:
        conn.close()
    return {"status": "updated"}


@app.get("/status_tree")
def get_status_tree():
    now = datetime.now(timezone.utc)
    conn = get_db_connection()
    result = []
    try:
        objects = conn.execute("SELECT * FROM objects").fetchall()
        for obj in objects:
            sub_objects = conn.execute(
                "SELECT * FROM sub_objects WHERE object_id = ?", (obj["id"],)
            ).fetchall()
            children = []
            for sub in sub_objects:
                if sub["last_update"]:
                    last = datetime.fromisoformat(sub["last_update"])
                    # Преобразуем наивную дату в timezone-aware
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    # Сравниваем разницу с ALLOWED_DELAY
                    time_diff = now - last
                    status = "active" if time_diff <= ALLOWED_DELAY else "inactive"
                else:
                    status = "inactive"
                children.append(
                    {
                        "name": sub["name"],
                        "last_update": sub["last_update"],
                        "status": status,
                        "paused": bool(sub["paused"]),
                    }
                )
            result.append(
                {
                    "name": obj["name"],
                    "paused": bool(obj["paused"]),
                    "children": children,
                }
            )
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
    while True:
        time.sleep(CHECK_INTERVAL)
        now = datetime.now(timezone.utc)
        conn = get_db_connection()
        try:
            objects = conn.execute("SELECT id, name, paused FROM objects").fetchall()
            for obj in objects:
                if obj["paused"]:
                    continue
                subs = conn.execute(
                    "SELECT * FROM sub_objects WHERE object_id = ?", (obj["id"],)
                ).fetchall()
                for sub in subs:
                    if sub["paused"]:
                        continue
                    if not sub["last_update"]:
                        continue
                    last = datetime.fromisoformat(sub["last_update"])
                    # Обеспечиваем, что last - timezone-aware
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    # Теперь обе now и last - timezone-aware
                    if now - last > ALLOWED_DELAY:
                        if not sub["notified"]:
                            send_telegram_message(
                                f"🔴 {obj['name']}::{sub['name']} не отвечает"
                            )
                            conn.execute(
                                "UPDATE sub_objects SET notified = 1 WHERE id = ?",
                                (sub["id"],),
                            )
                    else:
                        if sub["notified"]:
                            send_telegram_message(
                                f"🟢 {obj['name']}::{sub['name']} восстановлен"
                            )
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
