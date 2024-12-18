import time
import requests

# Конфигурация клиента
API_URL = "http://127.0.0.1:5000/update_status"
PROGRAM_NAME = "Energy_SolDar"
API_KEY = "Energy_SolDar"  # Тот же ключ, что указан в config.ini сервера


def notify_server():
    payload = {"program_name": PROGRAM_NAME, "api_key": API_KEY}
    try:
        response = requests.post(API_URL, json=payload)
        response.raise_for_status()
        print(f"Статус для {PROGRAM_NAME} отправлен.")
    except Exception as e:
        print(f"Ошибка отправки статуса: {e}")


while True:
    try:
        # Основной код программы
        print("Работа программы")
        time.sleep(5)  # Задержка цикла
        notify_server()
    except Exception as e:
        print(f"Ошибка: {e}")
