from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from config import TINKOFF_TOKEN

def main_keyboard():
    kb = [
        [KeyboardButton(text="📊 Топ недели"), KeyboardButton(text="🗓️ Топ месяца")],
        [KeyboardButton(text="⭐ Избранные")],
        [KeyboardButton(text="✅ Добавить тикер"), KeyboardButton(text="❌ Удалить тикер")],
    ]
    if TINKOFF_TOKEN:
        kb.append([KeyboardButton(text="💼 Портфель")])
    kb.append([
        KeyboardButton(text="✏️ Изменить"),
        KeyboardButton(text="🗑 Удалить"),
        KeyboardButton(text="📋 Список имён")
    ])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=False)