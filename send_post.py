#!/usr/bin/env python3
"""
Скрипт для отправки поста в Telegram с использованием функции send_to_telegram из news_bot.py.
Перед запуском убедитесь, что в файле .env заданы TELEGRAM_BOT_TOKEN и TELEGRAM_CHANNEL_ID.
"""

import os
import sys
from pathlib import Path

# Попытка загрузить переменные окружения из .env
try:
    from dotenv import load_dotenv
    # Загружаем .env из текущей директории
    env_path = Path(__file__).parent / '.env'
    load_dotenv(env_path)
    print(f"Загружен .env файл: {env_path}")
except ImportError:
    print("Модуль python-dotenv не установлен, пытаемся прочитать переменные окружения из системы")
    # Если нет dotenv, можно вручную прочитать файл .env
    env_path = Path(__file__).parent / '.env'
    if env_path.exists():
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key.strip()] = value.strip()
        print(f"Прочитаны переменные из {env_path}")
    else:
        print(f"Файл .env не найден: {env_path}")

# Проверяем наличие необходимых переменных
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID')

if not TELEGRAM_BOT_TOKEN:
    print("ОШИБКА: TELEGRAM_BOT_TOKEN не задан.")
    sys.exit(1)
if not TELEGRAM_CHANNEL_ID:
    print("ОШИБКА: TELEGRAM_CHANNEL_ID не задан.")
    sys.exit(1)

print(f"Токен: {TELEGRAM_BOT_TOKEN[:5]}...")
print(f"Канал: {TELEGRAM_CHANNEL_ID}")
print("Используется новый токен из обновленного .env")

# Импортируем функцию отправки
try:
    from news_bot import send_to_telegram
except ImportError as e:
    print(f"ОШИБКА: Не удалось импортировать send_to_telegram из news_bot.py: {e}")
    sys.exit(1)

# Данные поста
title = "ЖЕСТЬ! У Британии к лету может НАКРЫТЬСЯ с продуктами!"
summary = "Чиновники нарисовали самый мрачный сценарий: если всё пойдёт по худшему, к лету в UK начнётся реальная нехватка еды.\n\nДержитесь там, британцы! 😬"
images = []  # пустой список
original_link = "https://www.bbc.com/news"  # можно оставить пустую строку ""

print(f"\nОтправляем пост:")
print(f"Заголовок: {title}")
print(f"Текст: {summary[:100]}...")
print(f"Изображения: {images}")
print(f"Ссылка: {original_link}")

# Вызов функции
print("\n--- Начало отправки ---")
success = send_to_telegram(title, summary, images, original_link)

if success:
    print("✅ Успешно отправлено в Telegram!")
else:
    print("❌ Ошибка отправки. Возможные причины:")
    print("   - Неверный токен бота")
    print("   - Бот не добавлен в канал")
    print("   - Проблемы с сетью")
    print("   - Неверный формат chat_id")
    print("   - Другая ошибка Telegram API")

sys.exit(0 if success else 1)