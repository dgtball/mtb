from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from config import TINKOFF_TOKEN

def main_keyboard():
    kb = [
        [KeyboardButton(text="📌 Топ дня")],
        [KeyboardButton(text="📊 Топ недели"), KeyboardButton(text="🗓️ Топ месяца")],
        [KeyboardButton(text="⭐ Избранные")],
        [KeyboardButton(text="✅ Добавить тикер"), KeyboardButton(text="❌ Удалить тикер")],
    ]
    if TINKOFF_TOKEN:
        kb.insert(3, [KeyboardButton(text="📈 Портфель")])
    # Новые кнопки управления переименованиями
    kb.append([KeyboardButton(text="✏️ Переименовать тикер")])
    kb.append([KeyboardButton(text="🗑 Удалить переименование")])
    kb.append([KeyboardButton(text="📋 Все переименования")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=False)