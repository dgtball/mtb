import os
import logging
import time
import asyncio
import sqlite3
import datetime
from io import BytesIO

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, InputFile
import aiohttp
import pandas as pd
from tabulate import tabulate

# ---------- КОНФИГУРАЦИЯ ----------
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

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

# ---------- УНИВЕРСАЛЬНАЯ ФУНКЦИЯ ПОСТРОЕНИЯ ТАБЛИЦ (с simple) ----------
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

# ---------- ФОРМАТИРОВАНИЕ СООБЩЕНИЙ ----------
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
    loading_msg = await message.answer("⏳ Загружаю данные с Мосбиржи...")
    try:
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
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh")]
            ]
        )
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
        await loading_msg.delete()
    except Exception as e:
        await loading_msg.delete()
        logging.error(f"Ошибка в /top: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("week"))
async def cmd_week(message: types.Message):
    loading_msg = await message.answer("⏳ Загружаю данные за неделю...")
    try:
        now = get_moscow_time()
        monday = now - datetime.timedelta(days=now.weekday())
        from_date = monday.strftime("%Y-%m-%d")
        till_date = now.strftime("%Y-%m-%d")
        df = await get_historical_shares(from_date, till_date)
        if df.empty:
            await loading_msg.delete()
            await message.answer("Нет данных за неделю.")
            return
        changes = calc_period_change(df)
        # Один вызов get_all_shares для фильтрации и получения названий
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
        await loading_msg.delete()
    except Exception as e:
        await loading_msg.delete()
        logging.error(f"Ошибка в /week: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("month"))
async def cmd_month(message: types.Message):
    loading_msg = await message.answer("⏳ Загружаю данные за месяц...")
    try:
        now = get_moscow_time()
        first_day = now.replace(day=1)
        from_date = first_day.strftime("%Y-%m-%d")
        till_date = now.strftime("%Y-%m-%d")
        df = await get_historical_shares(from_date, till_date)
        if df.empty:
            await loading_msg.delete()
            await message.answer("Нет данных за месяц.")
            return
        changes = calc_period_change(df)
        # Один вызов get_all_shares
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
        await loading_msg.delete()
    except Exception as e:
        await loading_msg.delete()
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
    shares_df = await get_all_shares()
    if shares_df.empty:
        if is_weekend():
            await message.answer("📊 Сессия выходного дня. Избранное обновится в рабочие дни.")
        else:
            await message.answer("📊 Биржа закрыта. Попробуйте позже.")
        return
    fav_df = shares_df[shares_df['SECID'].isin(favs)]
    if fav_df.empty:
        await message.answer("По вашему списку нет актуальных данных.")
        return
    if 'CHANGEPERCENT' in fav_df.columns:
        fav_df = fav_df.sort_values('CHANGEPERCENT', ascending=False)
    else:
        if 'OPEN' in fav_df.columns and 'LAST' in fav_df.columns:
            fav_df['CHANGEPERCENT'] = ((fav_df['LAST'] - fav_df['OPEN']) / fav_df['OPEN']) * 100
            fav_df = fav_df.sort_values('CHANGEPERCENT', ascending=False)
        else:
            await message.answer("Недостаточно данных для сортировки.")
            return
    text = "⭐ Ваши избранные акции:\n\n"
    for _, row in fav_df.iterrows():
        name = row.get('SHORTNAME', row['SECID'])
        price = row['LAST']
        change = row['CHANGEPERCENT']
        text += f"• {row['SECID']} ({name}) — {price:.2f}  {change:+.2f}%\n"
    await message.answer(text)

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

# ---------- ЗАПУСК ----------
async def main():
    init_db()
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logging.info("Webhook удалён")
    except Exception as e:
        logging.warning(f"Не удалось удалить вебхук: {e}")
    await asyncio.sleep(1)
    logging.info("Запускаем polling...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
