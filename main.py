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
API_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("RENDER_EXTERNAL_URL")  # используем автоматическую переменную Render

if not API_TOKEN:
    raise ValueError("BOT_TOKEN не задан")
if not BASE_URL:
    raise ValueError("RENDER_EXTERNAL_URL не задан")

TOP_N = 10

# ---------- ЛОГИРОВАНИЕ ----------
logging.basicConfig(level=logging.INFO)

# ---------- ИНИЦИАЛИЗАЦИЯ БОТА ----------
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# ---------- ФУНКЦИИ ДЛЯ РАБОТЫ С MOEX ----------
async def get_all_shares():
    async with aiohttp.ClientSession() as session:
        try:
            data = await aiomoex.get_board_securities(
                session,
                board='TQBR',
                engine='stock',
                market='shares',
                columns=['SECID', 'SHORTNAME', 'OPEN', 'LAST']
            )
            logging.info(f"Received data: type={type(data)}, shape={data.shape if hasattr(data, 'shape') else 'N/A'}")
            if hasattr(data, 'columns'):
                logging.info(f"Columns: {data.columns.tolist()}")
            return data
        except Exception as e:
            logging.error(f"Error in get_all_shares: {e}")
            return pd.DataFrame()

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
        except Exception as e:
            logging.error(f"Error getting index: {e}")
        return None

def get_top_movers(data: pd.DataFrame, top_n: int = TOP_N):
    if data.empty:
        logging.warning("Empty DataFrame in get_top_movers")
        return pd.DataFrame(), pd.DataFrame()
    
    required = ['OPEN', 'LAST', 'SECID']
    missing = [col for col in required if col not in data.columns]
    if missing:
        logging.error(f"Missing columns: {missing}")
        return pd.DataFrame(), pd.DataFrame()
    
    # Очистка
    data = data.dropna(subset=['OPEN', 'LAST'])
    data['OPEN'] = pd.to_numeric(data['OPEN'], errors='coerce')
    data['LAST'] = pd.to_numeric(data['LAST'], errors='coerce')
    data = data.dropna(subset=['OPEN', 'LAST'])
    data = data[data['OPEN'] != 0]
    
    if data.empty:
        logging.warning("No valid rows after cleaning")
        return pd.DataFrame(), pd.DataFrame()
    
    data['change_percent'] = ((data['LAST'] - data['OPEN']) / data['OPEN']) * 100
    gainers = data.nlargest(top_n, 'change_percent')
    losers = data.nsmallest(top_n, 'change_percent')
    return gainers, losers

def format_message(gainers: pd.DataFrame, losers: pd.DataFrame, index_value, update_time: str) -> str:
    # ... (оставляем без изменений, как было ранее) ...
    # Убедитесь, что у вас есть эта функция из предыдущего кода.
    # Я не буду дублировать её для краткости, но она должна быть.

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
        if gainers.empty and losers.empty:
            await message.reply("⚠️ Не удалось получить данные. Проверьте логи.")
            return
        index_val = await get_moex_index()
        update_time = time.strftime("%Y-%m-%d %H:%M:%S")
        text = format_message(gainers, losers, index_val, update_time)
        await message.reply(text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Error in cmd_top: {e}")
        await message.reply(f"❌ Ошибка: {e}")

# ---------- FASTAPI ПРИЛОЖЕНИЕ ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    webhook_url = f"{BASE_URL}/webhook/{API_TOKEN}"
    await bot.set_webhook(webhook_url)
    logging.info(f"Webhook set to {webhook_url}")
    yield
    await bot.delete_webhook()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def index():
    return {"status": "Bot is running!"}

@app.post(f"/webhook/{API_TOKEN}")
async def webhook(request: Request):
    json_data = await request.json()
    update = Update(**json_data)
    await dp.feed_update(bot, update)
    return Response(status_code=200)
