import os
import logging
import time
import asyncio
import sqlite3
import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Update, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import aiohttp
import pandas as pd
from tabulate import tabulate

# ---------- КОНФИГУРАЦИЯ ----------
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

BASE_URL = os.getenv("RENDER_EXTERNAL_URL")  # автоматический URL от Render
if not BASE_URL:
    raise ValueError("RENDER_EXTERNAL_URL не задан")

TOP_N = 10
DB_PATH = "favorites.db"

# ---------- ЛОГИРОВАНИЕ ----------
logging.basicConfig(level=logging.INFO)

# ---------- ИНИЦИАЛИЗАЦИЯ БОТА ----------
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# ---------- РАБОТА С БАЗОЙ ДАННЫХ ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS favorites
                 (chat_id INTEGER, ticker TEXT, 
                  PRIMARY KEY (chat_id, ticker))''')
    conn.commit()
    conn.close()

def add_favorite(chat_id: int, ticker: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("INSERT INTO favorites (chat_id, ticker) VALUES (?, ?)", (chat_id, ticker.upper()))
        conn.commit()
        result = True
    except sqlite3.IntegrityError:
        result = False
    conn.close()
    return result

def remove_favorite(chat_id: int, ticker: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM favorites WHERE chat_id = ? AND ticker = ?", (chat_id, ticker.upper()))
    conn.commit()
    deleted = c.rowcount > 0
    conn.close()
    return deleted

def get_favorites(chat_id: int) -> list:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT ticker FROM favorites WHERE chat_id = ?", (chat_id,))
    rows = c.fetchall()
    conn.close()
    return [row[0] for row in rows]

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------
def get_moscow_time():
    return datetime.datetime.now(datetime.timezone.utc).astimezone(datetime.timezone(datetime.timedelta(hours=3)))

def is_weekend():
    now = get_moscow_time()
    return now.weekday() in (5, 6)

# ---------- ЗАПРОСЫ К MOEX ----------
async def get_all_shares():
    async with aiohttp.ClientSession() as session:
        url = "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json?iss.meta=off&iss.only=marketdata,securities"
        try:
            async with session.get(url) as resp:
                json_data = await resp.json()
                if 'marketdata' not in json_data or 'securities' not in json_data:
                    return pd.DataFrame()
                md_columns = json_data['marketdata']['columns']
                md_rows = json_data['marketdata']['data']
                market_df = pd.DataFrame(md_rows, columns=md_columns)
                sec_columns = json_data['securities']['columns']
                sec_rows = json_data['securities']['data']
                sec_df = pd.DataFrame(sec_rows, columns=sec_columns)
                sec_df = sec_df[['SECID', 'SHORTNAME', 'LISTLEVEL']]
                merged = pd.merge(market_df, sec_df, on='SECID', how='left')
                return merged
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
        except Exception:
            return None
        return None

def get_top_movers(data: pd.DataFrame, top_n: int = TOP_N, exclude_level3: bool = True):
    if data.empty:
        return pd.DataFrame(), pd.DataFrame()
    if exclude_level3 and 'LISTLEVEL' in data.columns:
        data = data[data['LISTLEVEL'] < 3]
    if 'CHANGEPERCENT' not in data.columns:
        if 'OPEN' in data.columns and 'LAST' in data.columns:
            data['CHANGEPERCENT'] = ((data['LAST'] - data['OPEN']) / data['OPEN']) * 100
        else:
            return pd.DataFrame(), pd.DataFrame()
    required = ['SECID', 'CHANGEPERCENT', 'LAST', 'SHORTNAME']
    for col in required:
        if col not in data.columns:
            if col == 'SHORTNAME':
                data['SHORTNAME'] = data['SECID']
            else:
                return pd.DataFrame(), pd.DataFrame()
    data = data.dropna(subset=['SECID', 'CHANGEPERCENT', 'LAST'])
    data['CHANGEPERCENT'] = pd.to_numeric(data['CHANGEPERCENT'], errors='coerce')
    data['LAST'] = pd.to_numeric(data['LAST'], errors='coerce')
    data = data.dropna(subset=['CHANGEPERCENT', 'LAST'])
    if data.empty:
        return pd.DataFrame(), pd.DataFrame()
    gainers = data.nlargest(top_n, 'CHANGEPERCENT')
    losers = data.nsmallest(top_n, 'CHANGEPERCENT')
    return gainers, losers

# ---------- ИСТОРИЧЕСКИЕ ДАННЫЕ ----------
async def get_historical_shares(from_date: str, till_date: str):
    async with aiohttp.ClientSession() as session:
        url = f"https://iss.moex.com/iss/history/engines/stock/markets/shares/boards/TQBR/securities.json?from={from_date}&till={till_date}&iss.meta=off&iss.only=history"
        try:
            async with session.get(url) as resp:
                json_data = await resp.json()
                if 'history' not in json_data:
                    return pd.DataFrame()
                columns = json_data['history']['columns']
                data_rows = json_data['history']['data']
                df = pd.DataFrame(data_rows, columns=columns)
                return df
        except Exception:
            return pd.DataFrame()

def calc_period_change(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    df['TRADEDATE'] = pd.to_datetime(df['TRADEDATE'])
    df = df.sort_values('TRADEDATE')
    first = df.groupby('SECID').first()[['OPEN']]
    last = df.groupby('SECID').last()[['CLOSE']]
    combined = first.join(last, how='inner')
    combined['CHANGE_PCT'] = ((combined['CLOSE'] - combined['OPEN']) / combined['OPEN']) * 100
    return combined.reset_index()

async def get_historical_changes(tickers: list, from_date: str, till_date: str) -> dict:
    """
    Возвращает словарь {ticker: change_percent} за период.
    """
    df = await get_historical_shares(from_date, till_date)
    if df.empty:
        return {}
    # Фильтруем только нужные тикеры
    df = df[df['SECID'].isin(tickers)]
    if df.empty:
        return {}
    changes = calc_period_change(df)
    # Превращаем в словарь
    return dict(zip(changes['SECID'], changes['CHANGE_PCT']))

# ---------- УНИВЕРСАЛЬНАЯ ФУНКЦИЯ ПОСТРОЕНИЯ ТАБЛИЦ ----------
def build_table_universal(df, title, headers, data_columns):
    if df.empty:
        return ""
    table_data = []
    for _, row in df.iterrows():
        row_data = []
        for col in data_columns:
            val = row.get(col, "")
            if col == 'SHORTNAME' and len(str(val)) > 25:
                val = str(val)[:22] + "…"
            elif col == 'LAST' and isinstance(val, (int, float)):
                val = f"{val:.2f}"
            elif col in ('CHANGEPERCENT', 'CHANGE_PCT') and isinstance(val, (int, float)):
                val = f"{val:+.2f}%"
            row_data.append(val)
        table_data.append(row_data)
    table = tabulate(table_data, headers=headers, tablefmt="simple", numalign="right", stralign="left")
    return f"<b>{title}</b>\n<pre>{table}</pre>\n"

def format_message(gainers: pd.DataFrame, losers: pd.DataFrame, index_value, update_time: str, is_weekend: bool = False) -> str:
    if index_value is not None:
        header = f"📊 Индекс МосБиржи: {index_value:.2f}\n"
    else:
        if is_weekend:
            header = "📊 Сессия выходного дня\n"
        else:
            header = "📊 Биржа закрыта\n"
    header += f"🕒 Обновлено: {update_time}\n\n"
    text = header
    text += build_table_universal(gainers, "📈 Лидеры роста", ["Тикер", "Название", "Цена", "Изменение"], ['SECID', 'SHORTNAME', 'LAST', 'CHANGEPERCENT'])
    text += build_table_universal(losers, "📉 Лидеры падения", ["Тикер", "Название", "Цена", "Изменение"], ['SECID', 'SHORTNAME', 'LAST', 'CHANGEPERCENT'])
    return text

def format_historical_table(gainers, losers, period_name, from_date, till_date):
    text = f"📊 Топ за {period_name}\n📅 Период: {from_date} – {till_date}\n\n"
    text += build_table_universal(gainers, "📈 Рост", ["Тикер", "Название", "Изменение"], ['SECID', 'SHORTNAME', 'CHANGE_PCT'])
    text += build_table_universal(losers, "📉 Падение", ["Тикер", "Название", "Изменение"], ['SECID', 'SHORTNAME', 'CHANGE_PCT'])
    return text

# ---------- ОБРАБОТЧИКИ КОМАНД ----------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Я бот для отслеживания топ-акций Мосбиржи.\n\n"
        "📌 Доступные команды:\n"
        "/top — показать лидеров роста и падения (текущий день)\n"
        "/week — топ за неделю (с понедельника)\n"
        "/month — топ за месяц (с 1 числа)\n"
        "/add TICKER — добавить акцию в избранное\n"
        "/remove TICKER — удалить из избранного\n"
        "/favorites — показать избранные акции"
    )

@dp.message(Command("top"))
async def cmd_top(message: types.Message):
    await message.answer("⏳ Загружаю данные...")
    try:
        shares_df = await get_all_shares()
        gainers, losers = get_top_movers(shares_df, top_n=TOP_N)
        if gainers.empty and losers.empty:
            if is_weekend():
                await message.answer("📊 Сессия выходного дня. Данные обновятся в рабочие дни.")
            else:
                await message.answer("📊 Биржа закрыта. Попробуйте позже.")
            return
        index_val = await get_moex_index()
        update_time = time.strftime("%Y-%m-%d %H:%M:%S")
        text = format_message(gainers, losers, index_val, update_time, is_weekend=is_weekend())
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh")]
            ]
        )
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logging.error(f"Ошибка в /top: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("week"))
async def cmd_week(message: types.Message):
    await message.answer("⏳ Загружаю данные за неделю...")
    try:
        now = get_moscow_time()
        monday = now - datetime.timedelta(days=now.weekday())
        from_date = monday.strftime("%Y-%m-%d")
        till_date = now.strftime("%Y-%m-%d")
        df = await get_historical_shares(from_date, till_date)
        if df.empty:
            await message.answer("Нет данных за неделю.")
            return
        changes = calc_period_change(df)
        shares_all = await get_all_shares()
        if not shares_all.empty:
            allowed_tickers = shares_all[shares_all['LISTLEVEL'] < 3]['SECID'].unique()
            changes = changes[changes['SECID'].isin(allowed_tickers)]
            names = shares_all[['SECID', 'SHORTNAME']].drop_duplicates('SECID')
            changes = changes.merge(names, on='SECID', how='left')
        gainers = changes.nlargest(TOP_N, 'CHANGE_PCT')
        losers = changes.nsmallest(TOP_N, 'CHANGE_PCT')
        text = format_historical_table(gainers, losers, "неделю", from_date, till_date)
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка в /week: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("month"))
async def cmd_month(message: types.Message):
    await message.answer("⏳ Загружаю данные за месяц...")
    try:
        now = get_moscow_time()
        first_day = now.replace(day=1)
        from_date = first_day.strftime("%Y-%m-%d")
        till_date = now.strftime("%Y-%m-%d")
        df = await get_historical_shares(from_date, till_date)
        if df.empty:
            await message.answer("Нет данных за месяц.")
            return
        changes = calc_period_change(df)
        shares_all = await get_all_shares()
        if not shares_all.empty:
            allowed_tickers = shares_all[shares_all['LISTLEVEL'] < 3]['SECID'].unique()
            changes = changes[changes['SECID'].isin(allowed_tickers)]
            names = shares_all[['SECID', 'SHORTNAME']].drop_duplicates('SECID')
            changes = changes.merge(names, on='SECID', how='left')
        gainers = changes.nlargest(TOP_N, 'CHANGE_PCT')
        losers = changes.nsmallest(TOP_N, 'CHANGE_PCT')
        text = format_historical_table(gainers, losers, "месяц", from_date, till_date)
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка в /month: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("add"))
async def cmd_add(message: types.Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Укажите тикер, например: /add SBER")
        return
    ticker = args[1].upper()
    if add_favorite(message.chat.id, ticker):
        await message.answer(f"✅ {ticker} добавлен в избранное.")
    else:
        await message.answer(f"ℹ️ {ticker} уже есть в избранном.")

@dp.message(Command("remove"))
async def cmd_remove(message: types.Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Укажите тикер, например: /remove SBER")
        return
    ticker = args[1].upper()
    if remove_favorite(message.chat.id, ticker):
        await message.answer(f"✅ {ticker} удалён из избранного.")
    else:
        await message.answer(f"ℹ️ {ticker} не найден в избранном.")

@dp.message(Command("favorites"))
async def cmd_favorites(message: types.Message):
    favs = get_favorites(message.chat.id)
    if not favs:
        await message.answer("У вас пока нет избранных акций. Добавьте через /add TICKER")
        return

    # Получаем текущие данные
    shares_df = await get_all_shares()
    if shares_df.empty:
        if is_weekend():
            await message.answer("📊 Сессия выходного дня. Избранное обновится в рабочие дни.")
        else:
            await message.answer("📊 Биржа закрыта. Попробуйте позже.")
        return

    # Фильтруем по избранным
    fav_df = shares_df[shares_df['SECID'].isin(favs)].copy()
    if fav_df.empty:
        await message.answer("По вашему списку нет актуальных данных.")
        return

    # Получаем исторические изменения за неделю и месяц
    now = get_moscow_time()
    # Неделя: с понедельника
    monday = now - datetime.timedelta(days=now.weekday())
    week_from = monday.strftime("%Y-%m-%d")
    week_till = now.strftime("%Y-%m-%d")
    # Месяц: с 1-го числа
    month_from = now.replace(day=1).strftime("%Y-%m-%d")
    month_till = now.strftime("%Y-%m-%d")

    week_changes = await get_historical_changes(favs, week_from, week_till)
    month_changes = await get_historical_changes(favs, month_from, month_till)

    # Добавляем колонки в fav_df
    fav_df['change_week'] = fav_df['SECID'].map(week_changes)
    fav_df['change_month'] = fav_df['SECID'].map(month_changes)

    # Заполняем пропуски (если данных нет)
    fav_df['change_week'] = fav_df['change_week'].fillna(float('nan'))
    fav_df['change_month'] = fav_df['change_month'].fillna(float('nan'))

    # Сортируем по дневному изменению (по убыванию)
    fav_df = fav_df.sort_values('CHANGEPERCENT', ascending=False)

    # Формируем таблицу
    # Используем build_table_universal, но передаём свои заголовки и колонки
    table_data = []
    for _, row in fav_df.iterrows():
        name = row.get('SHORTNAME', row['SECID'])
        if len(name) > 25:
            name = name[:22] + "…"
        price = f"{row['LAST']:.2f}" if isinstance(row['LAST'], (int, float)) else str(row['LAST'])
        day_change = f"{row['CHANGEPERCENT']:+.2f}%" if pd.notna(row['CHANGEPERCENT']) else "—"
        week_change = f"{row['change_week']:+.2f}%" if pd.notna(row['change_week']) else "—"
        month_change = f"{row['change_month']:+.2f}%" if pd.notna(row['change_month']) else "—"
        table_data.append([name, price, day_change, week_change, month_change])

    if not table_data:
        await message.answer("Нет данных для отображения.")
        return

    headers = ["Название", "Цена", "День", "Неделя", "Месяц"]
    # Используем tabulate напрямую, т.к. нам не нужен SECID в таблице
    table = tabulate(table_data, headers=headers, tablefmt="simple", numalign="right", stralign="left")
    text = f"⭐ <b>Избранные акции</b>\n<pre>{table}</pre>"
    await message.answer(text, parse_mode="HTML")

@dp.callback_query(lambda c: c.data == "refresh")
async def process_refresh(callback: CallbackQuery):
    await callback.answer("Обновляю данные...")
    try:
        shares_df = await get_all_shares()
        gainers, losers = get_top_movers(shares_df, top_n=TOP_N)
        if gainers.empty and losers.empty:
            if is_weekend():
                await callback.message.answer("📊 Сессия выходного дня. Данные обновятся в рабочие дни.")
            else:
                await callback.message.answer("📊 Биржа закрыта. Попробуйте позже.")
            return
        index_val = await get_moex_index()
        update_time = time.strftime("%Y-%m-%d %H:%M:%S")
        text = format_message(gainers, losers, index_val, update_time, is_weekend=is_weekend())
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh")]
            ]
        )
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logging.error(f"Ошибка обновления: {e}")
        await callback.message.answer(f"❌ Ошибка обновления: {e}")

# ---------- FASTAPI ПРИЛОЖЕНИЕ ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Инициализация БД
    init_db()
    # Установка вебхука при старте
    webhook_url = f"{BASE_URL}/webhook"
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(webhook_url)
    logging.info(f"Webhook установлен на {webhook_url}")
    yield
    # При завершении — удаляем вебхук
    await bot.delete_webhook()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def index():
    return {"status": "Bot is running!"}

@app.post("/webhook")
async def webhook(request: Request):
    json_data = await request.json()
    update = Update(**json_data)
    await dp.feed_update(bot, update)
    return Response(status_code=200)

# Для теста вебхука (на случай GET-запросов от Telegram)
@app.get("/webhook")
async def webhook_get():
    return {"status": "webhook is ready"}

# ---------- ЗАПУСК ----------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
