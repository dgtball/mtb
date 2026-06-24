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
        kb.append([KeyboardButton(text="💼 Портфель")])          # теперь строка 4
    # Новые кнопки управления переименованиями в одной строке
    kb.append([
        KeyboardButton(text="✏️ Переименовать тикер"),
        KeyboardButton(text="🗑 Удалить переименование"),
        KeyboardButton(text="📋 Все переименования")
    ])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=False)