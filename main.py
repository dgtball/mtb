import os
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Update
import aiohttp
import pandas as pd
import aiomoex
from tabulate import tabulate

# ---------- КОНФИГУРАЦИЯ ----------
API_TOKEN = os.getenv("6231234093:AAFqG9gpCNrsr2RLoi2lNrMtvSg1pnEIt3Y")  # Токен берём из переменных окружения
BASE_URL = os.getenv("https://mtb-0rk3.onrender.com")    # Например, https://your-service.onrender.com
TOP_N = 10

if not API_TOKEN or not BASE_URL:
    raise ValueError("Переменные BOT_TOKEN и BASE_URL должны быть заданы")

# ---------- ЛОГИРОВАНИЕ ----------
logging.basicConfig(level=logging.INFO)

# ---------- ИНИЦИАЛИЗАЦИЯ БОТА ----------
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# ---------- ФУНКЦИИ ДЛЯ РАБОТЫ С MOEX ----------
async def get_all_shares():
    async with aiohttp.ClientSession() as session:
        data = await aiomoex.get_board_securities(
            session,
            board='TQBR',
            engine='stock',
            market='shares'
        )
        return data

async def get_moex_index():
    async with aiohttp.ClientSession() as session:
        url = "https://iss.moex.com/iss/engines/stock/markets/index/boards/SNDX/securities/IMOEX.json?iss.meta=off"
        try:
            async with session.get(url) as resp:
                json_data = await resp.json()
                columns = json_data['securities']['columns']
                data_rows = json_data['securities']['data']
                if data_rows:
                    last_idx = columns.index('LAST')
                    return float(data_rows[0][last_idx])
        except Exception:
            return None
        return None

def get_top_movers(data: pd.DataFrame, top_n: int = TOP_N):
    data = data.dropna(subset=['OPEN', 'LAST'])
    data['OPEN'] = pd.to_numeric(data['OPEN'], errors='coerce')
    data['LAST'] = pd.to_numeric(data['LAST'], errors='coerce')
    data = data.dropna(subset=['OPEN', 'LAST'])
    data = data[data['OPEN'] != 0]
    data['change_percent'] = ((data['LAST'] - data['OPEN']) / data['OPEN']) * 100
    gainers = data.nlargest(top_n, 'change_percent')
    losers = data.nsmallest(top_n, 'change_percent')
    return gainers, losers

def format_message(gainers: pd.DataFrame, losers: pd.DataFrame, index_value, update_time: str) -> str:
    if 'SHORTNAME' not in gainers.columns:
        gainers['SHORTNAME'] = gainers['SECID']
    if 'SHORTNAME' not in losers.columns:
        losers['SHORTNAME'] = losers['SECID']

    gainers_rows = []
    for _, row in gainers.iterrows():
        gainers_rows.append([
            row['SECID'],
            row['SHORTNAME'],
            f"{row['LAST']:.2f}" if isinstance(row['LAST'], (int, float)) else str(row['LAST']),
            f"+{row['change_percent']:.2f}%"
        ])

    losers_rows = []
    for _, row in losers.iterrows():
        losers_rows.append([
            row['SECID'],
            row['SHORTNAME'],
            f"{row['LAST']:.2f}" if isinstance(row['LAST'], (int, float)) else str(row['LAST']),
            f"{row['change_percent']:.2f}%"
        ])

    headers = ["Тикер", "Название", "Цена", "Изменение"]

    table_gainers = tabulate(gainers_rows, headers=headers, tablefmt="simple", numalign="right", stralign="left")
    table_losers = tabulate(losers_rows, headers=headers, tablefmt="simple", numalign="right", stralign="left")

    if index_value is not None:
        header = f"📊 **IMOEX Индекс МосБиржи** {index_value:.2f}\n"
    else:
        header = "📊 **Индекс МосБиржи** временно недоступен\n"
    header += f"🕒 Обновлено: {update_time}\n\n"

    text = header
    text += "📈 **Лидеры роста**\n```\n" + table_gainers + "\n```\n"
    text += "📉 **Лидеры падения**\n```\n" + table_losers + "\n```"
    return text

# ---------- ОБРАБОТЧИКИ КОМАНД ----------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.reply(
        "👋 Привет! Я бот для отслеживания топ-акций Мосбиржи.\n\n"
        "📌 Используй команду /top — я покажу лидеров роста и падения."
    )

@dp.message(Command("top"))
async def cmd_top(message: types.Message):
    await message.reply("⏳ Загружаю данные с Мосбиржи...")
    try:
        shares_df = await get_all_shares()
        gainers, losers = get_top_movers(shares_df, top_n=TOP_N)
        index_val = await get_moex_index()
        update_time = time.strftime("%Y-%m-%d %H:%M:%S")
        text = format_message(gainers, losers, index_val, update_time)
        await message.reply(text, parse_mode="Markdown")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")

# ---------- FASTAPI ПРИЛОЖЕНИЕ ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # При старте приложения устанавливаем вебхук
    webhook_url = f"{BASE_URL}/webhook/{API_TOKEN}"
    await bot.set_webhook(webhook_url)
    logging.info(f"Webhook set to {webhook_url}")
    yield
    # При завершении (опционально) удаляем вебхук
    await bot.delete_webhook()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def index():
    return {"status": "Bot is running!"}

@app.post(f"/webhook/{API_TOKEN}")
async def webhook(request: Request):
    """Принимает обновления от Telegram."""
    json_data = await request.json()
    update = Update(**json_data)
    await dp.feed_update(bot, update)
    return Response(status_code=200)

# (Опционально) для локального теста можно запустить через uvicorn
# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8000)
