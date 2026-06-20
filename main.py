import os
import logging
import time
import asyncio
import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Update, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton
import aiohttp
import pandas as pd
from tabulate import tabulate
from supabase import create_client, Client

# ---------- КОНФИГУРАЦИЯ ----------
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

BASE_URL = os.getenv("RENDER_EXTERNAL_URL")
if not BASE_URL:
    raise ValueError("RENDER_EXTERNAL_URL не задан")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("SUPABASE_URL и SUPABASE_KEY должны быть заданы")

TOP_N = 10

# ---------- ЛОГИРОВАНИЕ ----------
logging.basicConfig(level=logging.INFO)

# ---------- ИНИЦИАЛИЗАЦИЯ SUPABASE ----------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------- ИНИЦИАЛИЗАЦИЯ БОТА ----------
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# ---------- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ----------
last_messages = {}       # chat_id -> message_id
update_tasks = {}        # chat_id -> asyncio.Task
auto_update_enabled = {} # chat_id -> True/False
user_state = {}          # chat_id -> 'add' / 'remove'

# ---------- КЛАВИАТУРА ----------
def main_keyboard():
    kb = [
        [KeyboardButton(text="📈 Топ дня")],
        [KeyboardButton(text="📊 Топ недели"), KeyboardButton(text="📉 Топ месяца")],
        [KeyboardButton(text="⭐ Избранные")],
        [KeyboardButton(text="➕ Добавить тикер"), KeyboardButton(text="➖ Удалить тикер")],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=False)

# ---------- РАБОТА С БАЗОЙ ДАННЫХ (SUPABASE) ----------
def add_favorite(chat_id: int, ticker: str) -> bool:
    try:
        supabase.table("favorites").insert({
            "chat_id": chat_id,
            "ticker": ticker.upper()
        }).execute()
        return True
    except Exception as e:
        logging.error(f"Ошибка добавления в Supabase: {e}")
        return False

def remove_favorite(chat_id: int, ticker: str) -> bool:
    try:
        result = supabase.table("favorites")\
            .delete()\
            .eq("chat_id", chat_id)\
            .eq("ticker", ticker.upper())\
            .execute()
        return len(result.data) > 0
    except Exception as e:
        logging.error(f"Ошибка удаления из Supabase: {e}")
        return False

def get_favorites(chat_id: int) -> list:
    try:
        result = supabase.table("favorites")\
            .select("ticker")\
            .eq("chat_id", chat_id)\
            .execute()
        return [row["ticker"] for row in result.data]
    except Exception as e:
        logging.error(f"Ошибка получения избранного из Supabase: {e}")
        return []

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------
def get_moscow_time():
    return datetime.datetime.now(datetime.timezone.utc).astimezone(datetime.timezone(datetime.timedelta(hours=3)))

def is_weekend():
    now = get_moscow_time()
    return now.weekday() in (5, 6)

# ---------- ЗАПРОСЫ К MOEX ----------
async def get_all_shares():
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json?iss.meta=off&iss.only=marketdata,securities"
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
        logging.error(f"Ошибка загрузки get_all_shares: {e}", exc_info=True)
        return pd.DataFrame()

async def get_moex_index():
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://iss.moex.com/iss/engines/stock/markets/index/boards/SNDX/securities/IMOEX.json?iss.meta=off"
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
    data = data.copy()
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
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://iss.moex.com/iss/history/engines/stock/markets/shares/boards/TQBR/securities.json?from={from_date}&till={till_date}&iss.meta=off&iss.only=history"
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

# ---------- ФУНКЦИЯ ДЛЯ ПОЛУЧЕНИЯ ДАННЫХ ДЛЯ ИЗБРАННОГО ----------
async def get_favorites_data(chat_id: int):
    favs = get_favorites(chat_id)
    if not favs:
        return None, "У вас пока нет избранных акций. Добавьте через /add TICKER"
    shares_df = await get_all_shares()
    if shares_df.empty:
        if is_weekend():
            return None, "📊 Сессия выходного дня. Избранное обновится в рабочие дни."
        else:
            return None, "📊 Биржа закрыта. Попробуйте позже."
    fav_df = shares_df[shares_df['SECID'].isin(favs)].copy()
    if fav_df.empty:
        return None, "По вашему списку нет актуальных данных."
    now = get_moscow_time()
    monday = now - datetime.timedelta(days=now.weekday())
    week_from = monday.strftime("%Y-%m-%d")
    week_till = now.strftime("%Y-%m-%d")
    month_from = now.replace(day=1).strftime("%Y-%m-%d")
    month_till = now.strftime("%Y-%m-%d")

    week_df = await get_historical_shares(week_from, week_till)
    month_df = await get_historical_shares(month_from, month_till)

    def get_change(df, ticker):
        if df.empty:
            return None
        ticker_data = df[df['SECID'] == ticker]
        if ticker_data.empty:
            return None
        changes = calc_period_change(ticker_data)
        if changes.empty:
            return None
        return changes.iloc[0]['CHANGE_PCT']

    fav_df['change_week'] = fav_df['SECID'].apply(lambda t: get_change(week_df, t))
    fav_df['change_month'] = fav_df['SECID'].apply(lambda t: get_change(month_df, t))
    fav_df['change_week'] = fav_df['change_week'].fillna(float('nan'))
    fav_df['change_month'] = fav_df['change_month'].fillna(float('nan'))
    fav_df = fav_df.sort_values('CHANGEPERCENT', ascending=False)
    return fav_df, None

# ---------- УНИВЕРСАЛЬНАЯ ОТПРАВКА ТОПА ----------
async def send_top(message: types.Message, period: str = 'day'):
    loading_msg = None
    try:
        loading_msg = await message.answer("⏳ Загружаю данные...")
        if period == 'day':
            shares_df = await get_all_shares()
            gainers, losers = get_top_movers(shares_df, top_n=TOP_N)
            if gainers.empty and losers.empty:
                await loading_msg.delete()
                if is_weekend():
                    await message.answer("📊 Сессия выходного дня. Данные обновятся в рабочие дни.")
                else:
                    await message.answer("📊 Биржа закрыта. Попробуйте позже.")
                return
            index_val = await get_moex_index()
            update_time = time.strftime("%Y-%m-%d %H:%M:%S")
            text = format_message(gainers, losers, index_val, update_time, is_weekend=is_weekend())
        else:
            now = get_moscow_time()
            if period == 'week':
                start = now - datetime.timedelta(days=now.weekday())
                from_date = start.strftime("%Y-%m-%d")
                period_name = "неделю"
            else:
                from_date = now.replace(day=1).strftime("%Y-%m-%d")
                period_name = "месяц"
            till_date = now.strftime("%Y-%m-%d")
            df = await get_historical_shares(from_date, till_date)
            if df.empty:
                await loading_msg.delete()
                await message.answer(f"Нет данных за {period_name}.")
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
            text = format_historical_table(gainers, losers, period_name, from_date, till_date)

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh")]
            ]
        )
        sent_msg = await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
        chat_id = message.chat.id
        last_messages[chat_id] = sent_msg.message_id
        if period == 'day' and auto_update_enabled.get(chat_id, False):
            if chat_id not in update_tasks or update_tasks[chat_id].done():
                task = asyncio.create_task(auto_update_task(chat_id, sent_msg.message_id))
                update_tasks[chat_id] = task
        if loading_msg:
            await loading_msg.delete()
    except Exception as e:
        if loading_msg:
            await loading_msg.delete()
        logging.error(f"❌ Критическая ошибка в send_top (period={period}): {e}", exc_info=True)
        await message.answer(f"❌ Ошибка при загрузке данных: {e}")

# ---------- АВТООБНОВЛЕНИЕ ----------
async def auto_update_task(chat_id: int, message_id: int):
    while True:
        await asyncio.sleep(30)
        try:
            shares_df = await get_all_shares()
            gainers, losers = get_top_movers(shares_df, top_n=TOP_N)
            if gainers.empty and losers.empty:
                continue
            index_val = await get_moex_index()
            update_time = time.strftime("%Y-%m-%d %H:%M:%S")
            text = format_message(gainers, losers, index_val, update_time, is_weekend=is_weekend())
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh")]
                ]
            )
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode="HTML", reply_markup=keyboard)
        except Exception as e:
            logging.error(f"Ошибка автообновления для чата {chat_id}: {e}")
            break

# ---------- ОБРАБОТЧИКИ КОМАНД И КНОПОК ----------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    chat_id = message.chat.id
    auto_update_enabled[chat_id] = True
    try:
        await message.answer(
            "👋 Привет! Я бот для отслеживания топ-акций Мосбиржи.\n\n"
            "Используйте кнопки ниже для навигации.",
            reply_markup=main_keyboard()
        )
        await send_top(message, 'day')
    except Exception as e:
        logging.error(f"❌ Ошибка в /start: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка при запуске: {e}")

@dp.message(lambda msg: msg.text == "📈 Топ дня")
async def top_day_button(message: types.Message):
    await send_top(message, 'day')

@dp.message(lambda msg: msg.text == "📊 Топ недели")
async def top_week_button(message: types.Message):
    await send_top(message, 'week')

@dp.message(lambda msg: msg.text == "📉 Топ месяца")
async def top_month_button(message: types.Message):
    await send_top(message, 'month')

@dp.message(lambda msg: msg.text == "⭐ Избранные")
async def favorites_button(message: types.Message):
    try:
        fav_df, error = await get_favorites_data(message.chat.id)
        if error:
            await message.answer(error)
            return
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
        header_line = "  ".join(f"<b>{h}</b>" for h in headers)
        table = tabulate(table_data, headers=[], tablefmt="simple", numalign="right", stralign="left")
        text = f"⭐ <b>Избранные акции</b>\n{header_line}\n<pre>{table}</pre>"
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"❌ Ошибка в favorites: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(lambda msg: msg.text == "➕ Добавить тикер")
async def add_ticker_button(message: types.Message):
    user_state[message.chat.id] = 'add'
    await message.answer("Введите тикер для добавления (например, SBER):")

@dp.message(lambda msg: msg.text == "➖ Удалить тикер")
async def remove_ticker_button(message: types.Message):
    user_state[message.chat.id] = 'remove'
    await message.answer("Введите тикер для удаления (например, SBER):")

@dp.message()
async def handle_text(message: types.Message):
    chat_id = message.chat.id
    if chat_id in user_state:
        state = user_state[chat_id]
        ticker = message.text.strip().upper()
        if state == 'add':
            if add_favorite(chat_id, ticker):
                await message.answer(f"✅ {ticker} добавлен в избранное.")
            else:
                await message.answer(f"ℹ️ {ticker} уже есть в избранном.")
        elif state == 'remove':
            if remove_favorite(chat_id, ticker):
                await message.answer(f"✅ {ticker} удалён из избранного.")
            else:
                await message.answer(f"ℹ️ {ticker} не найден в избранном.")
        del user_state[chat_id]
        await message.answer("Что дальше?", reply_markup=main_keyboard())
    else:
        await message.answer("Используйте кнопки меню.", reply_markup=main_keyboard())

@dp.callback_query(lambda c: c.data == "refresh")
async def process_refresh(callback: CallbackQuery):
    try:
        await callback.answer("Обновляю...", cache_time=0)
    except Exception:
        pass
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
        chat_id = callback.message.chat.id
        last_messages[chat_id] = callback.message.message_id
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logging.error(f"❌ Ошибка обновления: {e}", exc_info=True)
        await callback.message.answer(f"❌ Ошибка обновления: {e}")

# ---------- FASTAPI ПРИЛОЖЕНИЕ ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info("Запуск lifespan...")
    try:
        webhook_url = f"{BASE_URL}/webhook"
        for attempt in range(5):
            try:
                await bot.delete_webhook(drop_pending_updates=True)
                await bot.set_webhook(webhook_url)
                logging.info(f"✅ Webhook установлен на {webhook_url} (попытка {attempt+1})")
                break
            except Exception as e:
                logging.error(f"❌ Ошибка установки вебхука (попытка {attempt+1}): {e}")
                await asyncio.sleep(2)
        else:
            logging.error("❌ Не удалось установить вебхук после 5 попыток")
    except Exception as e:
        logging.error(f"❌ Критическая ошибка в lifespan: {e}", exc_info=True)
    yield
    logging.info("Завершение lifespan...")
    for task in update_tasks.values():
        if not task.done():
            task.cancel()
    await bot.delete_webhook()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def index():
    return {"status": "Bot is running!"}

@app.get("/set_webhook")
async def set_webhook():
    webhook_url = f"{BASE_URL}/webhook"
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_webhook(webhook_url)
        return {"status": "ok", "url": webhook_url}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/webhook")
async def webhook(request: Request):
    try:
        json_data = await request.json()
        update = Update(**json_data)
        await dp.feed_update(bot, update)
        return Response(status_code=200)
    except Exception as e:
        logging.error(f"❌ Webhook error: {e}", exc_info=True)
        return Response(status_code=200)

@app.get("/webhook")
async def webhook_get():
    return {"status": "webhook is ready"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
