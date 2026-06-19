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
BASE_URL = os.getenv("RENDER_EXTERNAL_URL")

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
        url = "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json?iss.meta=off&iss.only=marketdata"
        try:
            async with session.get(url) as resp:
                json_data = await resp.json()
                if 'marketdata' not in json_data:
                    logging.error("Нет данных marketdata в ответе")
                    return pd.DataFrame()
                columns = json_data['marketdata']['columns']
                data_rows = json_data['marketdata']['data']
                df = pd.DataFrame(data_rows, columns=columns)
                logging.info(f"Загружено {len(df)} строк, колонки: {df.columns.tolist()}")
                return df
        except Exception as e:
            logging.error(f"Ошибка загрузки: {e}")
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
            logging.error(f"Ошибка получения индекса: {e}")
        return None

def get_top_movers(data: pd.DataFrame, top_n: int = TOP_N):
    if data.empty:
        return pd.DataFrame(), pd.DataFrame()
    
    # Проверяем наличие колонок
    if 'CHANGEPERCENT' not in data.columns:
        # Если нет CHANGEPERCENT, пробуем вычислить из OPEN и LAST
        if 'OPEN' in data.columns and 'LAST' in data.columns:
            data['CHANGEPERCENT'] = ((data['LAST'] - data['OPEN']) / data['OPEN']) * 100
        else:
            logging.error("Нет колонок для расчёта изменения")
            return pd.DataFrame(), pd.DataFrame()
    
    required = ['SECID', 'CHANGEPERCENT', 'LAST']
    missing = [col for col in required if col not in data.columns]
    if missing:
        logging.error(f"Отсутствуют колонки: {missing}")
        return pd.DataFrame(), pd.DataFrame()
    
    # Очистка
    data = data.dropna(subset=required)
    data['CHANGEPERCENT'] = pd.to_numeric(data['CHANGEPERCENT'], errors='coerce')
    data['LAST'] = pd.to_numeric(data['LAST'], errors='coerce')
    data = data.dropna(subset=['CHANGEPERCENT', 'LAST'])
    
    if data.empty:
        return pd.DataFrame(), pd.DataFrame()
    
    gainers = data.nlargest(top_n, 'CHANGEPERCENT')
    losers = data.nsmallest(top_n, 'CHANGEPERCENT')
    return gainers, losers

def format_message(gainers: pd.DataFrame, losers: pd.DataFrame, index_value, update_time: str) -> str:
    # Добавляем название, если нет
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
            f"+{row['CHANGEPERCENT']:.2f}%"
        ])

    losers_rows = []
    for _, row in losers.iterrows():
        losers_rows.append([
            row['SECID'],
            row['SHORTNAME'],
            f"{row['LAST']:.2f}" if isinstance(row['LAST'], (int, float)) else str(row['LAST']),
            f"{row['CHANGEPERCENT']:.2f}%"
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
    await message.answer(
        "👋 Привет! Я бот для отслеживания топ-акций Мосбиржи.\n\n"
        "📌 Используй команду /top — я покажу лидеров роста и падения."
    )

@dp.message(Command("top"))
async def cmd_top(message: types.Message):
    await message.answer("⏳ Загружаю данные с Мосбиржи...")
    try:
        shares_df = await get_all_shares()
        gainers, losers = get_top_movers(shares_df, top_n=TOP_N)
        if gainers.empty and losers.empty:
            await message.answer("⚠️ Не удалось получить данные. Проверьте логи.")
            return
        index_val = await get_moex_index()
        update_time = time.strftime("%Y-%m-%d %H:%M:%S")
        text = format_message(gainers, losers, index_val, update_time)
        await message.answer(text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Ошибка в /top: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")

# ---------- FASTAPI ПРИЛОЖЕНИЕ ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    webhook_url = f"{BASE_URL}/webhook"
    try:
        # Сначала удалим старый вебхук (на всякий случай)
        await bot.delete_webhook()
        await bot.set_webhook(webhook_url)
        logging.info(f"✅ Webhook установлен на {webhook_url}")
    except Exception as e:
        logging.error(f"❌ Ошибка установки вебхука: {e}")
    yield
    await bot.delete_webhook()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def index():
    return {"status": "Bot is running!"}

@app.get("/set_webhook")
async def set_webhook():
    webhook_url = f"{BASE_URL}/webhook"
    try:
        await bot.delete_webhook()
        await bot.set_webhook(webhook_url)
        return {"status": "ok", "url": webhook_url}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.api_route("/webhook", methods=["GET", "POST"])
async def webhook(request: Request):
    logging.info(f"Webhook вызван с методом {request.method}")
    if request.method == "GET":
        return Response(status_code=200, content="Webhook is ready")
    try:
        json_data = await request.json()
        logging.info(f"Получено обновление: {json_data}")
        update = Update(**json_data)
        await dp.feed_update(bot, update)
        return Response(status_code=200)
    except Exception as e:
        logging.error(f"Ошибка в вебхуке: {e}", exc_info=True)
        return Response(status_code=500, content=str(e))
