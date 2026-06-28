from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
from config import TINKOFF_TOKEN, DOMAIN

def main_keyboard():
    kb = [
        [KeyboardButton(text="📊 Топ недели"), KeyboardButton(text="🗓️ Топ месяца")],
    ]
    kb.append([
        KeyboardButton(text="💼 Портфель", web_app=WebAppInfo(url=f"{DOMAIN}/mini-app"))
    ])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=False)