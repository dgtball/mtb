from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
from config import TINKOFF_TOKEN

def main_keyboard():
    kb = [
        [KeyboardButton(text="📊 Топ недели"), KeyboardButton(text="🗓️ Топ месяца")],
    ]
    kb.append([
        KeyboardButton(text="🖥 Управление", web_app=WebAppInfo(url="https://minvest.bothost.tech//mini-app"))
    ])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=False)