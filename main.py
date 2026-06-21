# ==============================================
# БОТ ДЛЯ ТОП-АКЦИЙ МОСБИРЖИ И ПОРТФЕЛЯ Т-ИНВЕСТИЦИЙ
# Версия: 3.3.0 (Health‑сервер для Bothost)
# ==============================================

import os
import logging
import time
import asyncio
import datetime
import io
import sqlite3
from collections import defaultdict

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import FuncFormatter

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, Filter
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile
)
from aiohttp import web
import aiohttp
import pandas as pd
from tabulate import tabulate
from tinkoff.invest import Client
from tinkoff.invest.services import InstrumentsService, OperationsService
from tinkoff.invest.schemas import OperationType, InstrumentIdType, OperationState

# ---------- КОНФИГУРАЦИЯ ----------
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

TINKOFF_TOKEN = os.getenv("TITN")
if not TINKOFF_TOKEN:
    logging.warning("TITN не задан – команды портфеля будут недоступны")

MY_CHAT_ID = os.getenv("MY_CHAT_ID")
if not MY_CHAT_ID:
    raise ValueError("MY_CHAT_ID не задан. Добавьте его в переменные окружения.")
try:
    MY_CHAT_ID = int(MY_CHAT_ID)
except ValueError:
    raise ValueError("MY_CHAT_ID должен быть числом (целым).")

TOP_N = 10
DATA_DIR = os.getenv('DATA_DIR', '/app/data')
DB_PATH = os.path.join(DATA_DIR, 'favorites.db')
PORT = int(os.getenv('PORT', 3000))  # Bothost использует 3000

# ---------- ФИЛЬТР ДЛЯ ПРИВАТНОСТИ ----------
class PrivateFilter(Filter):
    async def __call__(self, message: types.Message) -> bool:
        return message.from_user.id == MY_CHAT_ID

# ---------- ЛОГИРОВАНИЕ ----------
logging.basicConfig(level=logging.INFO)

# ---------- ИНИЦИАЛИЗАЦИЯ БОТА ----------
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# ---------- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ----------
last_messages = {}
update_tasks = {}
auto_update_enabled = {}
user_state = {}
http_session = None

# ---------- КЛАВИАТУРА ----------
def main_keyboard():
    kb = [
        [KeyboardButton(text="📌 Топ дня")],
        [KeyboardButton(text="📊 Топ недели"), KeyboardButton(text="🗓️ Топ месяца")],
        [KeyboardButton(text="⭐ Избранные")],
        [KeyboardButton(text="📈 Портфель"), KeyboardButton(text="📊 График покупок")],
        [KeyboardButton(text="✅ Добавить тикер"), KeyboardButton(text="❌ Удалить тикер")],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=False)

# ---------- УДАЛЕНИЕ СООБЩЕНИЙ ----------
async def safe_delete_message(chat_id: int, message_id: int):
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception as e:
        logging.warning(f"Не удалось удалить сообщение {message_id}: {e}")

# ---------- РАБОТА С БАЗОЙ ДАННЫХ (SQLite) ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS favorites
                 (chat_id INTEGER, ticker TEXT, 
                  PRIMARY KEY (chat_id, ticker))''')
    conn.commit()
    conn.close()
    logging.info(f"✅ База данных инициализирована: {DB_PATH}")

def add_favorite(chat_id: int, ticker: str) -> bool:
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO favorites (chat_id, ticker) VALUES (?, ?)", (chat_id, ticker.upper()))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        return False
    except Exception as e:
        logging.error(f"Ошибка добавления в SQLite: {e}")
        return False

def remove_favorite(chat_id: int, ticker: str) -> bool:
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM favorites WHERE chat_id = ? AND ticker = ?", (chat_id, ticker.upper()))
        conn.commit()
        deleted = c.rowcount > 0
        conn.close()
        return deleted
    except Exception as e:
        logging.error(f"Ошибка удаления из SQLite: {e}")
        return False

def get_favorites(chat_id: int) -> list:
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT ticker FROM favorites WHERE chat_id = ?", (chat_id,))
        rows = c.fetchall()
        conn.close()
        return [row[0] for row in rows]
    except Exception as e:
        logging.error(f"Ошибка получения избранного из SQLite: {e}")
        return []

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------
def get_moscow_time():
    return datetime.datetime.now(datetime.timezone.utc).astimezone(datetime.timezone(datetime.timedelta(hours=3)))

def get_local_time():
    return datetime.datetime.now(datetime.timezone.utc).astimezone(datetime.timezone(datetime.timedelta(hours=4)))

def is_weekend():
    now = get_moscow_time()
    return now.weekday() in (5, 6)

def get_session_status():
    now = get_moscow_time()
    if is_weekend():
        if (now.hour > 9 or (now.hour == 9 and now.minute >= 50)) and (now.hour < 18 or (now.hour == 18 and now.minute <= 59)):
            return "Сессия выходного дня"
        else:
            return "Биржа закрыта (выходной)"
    else:
        if now.hour < 6 or (now.hour == 6 and now.minute < 50):
            return "Биржа закрыта"
        elif now.hour == 6 and now.minute >= 50:
            return "Утренняя сессия (06:50–09:50)"
        elif now.hour < 9 or (now.hour == 9 and now.minute < 50):
            return "Утренняя сессия (06:50–09:50)"
        elif now.hour == 9 and now.minute >= 50:
            return "Основная сессия (09:50–19:00)"
        elif now.hour < 19:
            return "Основная сессия (09:50–19:00)"
        elif now.hour == 19 and now.minute == 0:
            return "Вечерняя сессия (19:00–23:50)"
        elif now.hour < 23 or (now.hour == 23 and now.minute <= 50):
            return "Вечерняя сессия (19:00–23:50)"
        else:
            return "Биржа закрыта"

def get_week_number(date):
    return date.isocalendar()[1]

def get_month_name_ru(month_num):
    months = {
        1: "Января", 2: "Февраля", 3: "Марта", 4: "Апреля",
        5: "Мая", 6: "Июня", 7: "Июля", 8: "Августа",
        9: "Сентября", 10: "Октября", 11: "Ноября", 12: "Декабря"
    }
    return months.get(month_num, str(month_num))

# ---------- ФУНКЦИИ ДЛЯ РАБОТЫ С Т-ИНВЕСТИЦИЙ ----------
def _to_float(units: int, nano: int) -> float:
    return units + nano / 1e9

def get_portfolio_data(account_id: str = None) -> dict:
    if not TINKOFF_TOKEN:
        return {"error": "Токен TITN не задан"}
    with Client(TINKOFF_TOKEN) as client:
        accounts = client.users.get_accounts().accounts
        if not accounts:
            return {"error": "Нет доступных счетов"}
        if account_id is None:
            account_id = accounts[0].id
        portfolio = client.operations.get_portfolio(account_id=account_id)
        result = {
            "account_name": next((a.name for a in accounts if a.id == account_id), "Основной"),
            "total_amount": _to_float(portfolio.total_amount_units, portfolio.total_amount_nano),
            "positions": []
        }
        for pos in portfolio.positions:
            try:
                instrument = client.instruments.get_instrument_by_id(
                    instrument_id=pos.instrument_uid,
                    id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID
                ).instrument
                name = instrument.name if instrument else pos.figi
            except:
                name = pos.figi
            result["positions"].append({
                "figi": pos.figi,
                "name": name,
                "quantity": pos.quantity.units,
                "current_price": _to_float(pos.current_price.units, pos.current_price.nano),
                "average_price": _to_float(pos.average_position_price.units, pos.average_position_price.nano),
                "expected_yield": _to_float(pos.expected_yield.units, pos.expected_yield.nano),
            })
        return result

def get_operations(account_id: str = None, from_date: datetime.date = None, to_date: datetime.date = None) -> list:
    if not TINKOFF_TOKEN:
        return []
    if from_date is None:
        from_date = get_moscow_time().date().replace(day=1)
    if to_date is None:
        to_date = get_moscow_time().date()
    with Client(TINKOFF_TOKEN) as client:
        accounts = client.users.get_accounts().accounts
        if not accounts:
            return []
        if account_id is None:
            account_id = accounts[0].id
        from_ts = int(datetime.datetime.combine(from_date, datetime.time.min).timestamp())
        to_ts = int(datetime.datetime.combine(to_date, datetime.time.max).timestamp())
        try:
            ops = client.operations.get_operations(
                account_id=account_id,
                from_=from_ts,
                to=to_ts,
                state=OperationState.OPERATION_STATE_EXECUTED
            )
            return ops.operations
        except Exception as e:
            logging.error(f"Ошибка получения операций: {e}")
            return []

def build_purchases_chart(account_id: str = None) -> io.BytesIO:
    now = get_moscow_time()
    from_date = now.date().replace(day=1)
    to_date = now.date()
    ops = get_operations(account_id, from_date, to_date)
    if not ops:
        return None
    buys = [op for op in ops if op.operation_type == OperationType.OPERATION_TYPE_BUY]
    if not buys:
        return None
    day_amounts = defaultdict(float)
    for op in buys:
        dt = op.date.astimezone(datetime.timezone.utc).astimezone(datetime.timezone(datetime.timedelta(hours=3)))
        day_key = dt.date()
        amount = op.payment.units + op.payment.nano / 1e9
        day_amounts[day_key] += abs(amount)
    if not day_amounts:
        return None
    sorted_days = sorted(day_amounts.items())
    dates = [d[0] for d in sorted_days]
    amounts = [d[1] for d in sorted_days]
    fig, ax = plt.subplots(figsize=(10, 6))
    ax.bar(dates, amounts, width=0.6, color='green', alpha=0.7)
    ax.set_title(f"Покупки за {from_date.strftime('%B %Y')}", fontsize=14)
    ax.set_xlabel("Дата")
    ax.set_ylabel("Сумма покупок (₽)")
    ax.grid(axis='y', linestyle='--', alpha=0.7)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%d'))
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=2))
    plt.xticks(rotation=45)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f'{int(x):,} ₽'))
    fig.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight')
    buf.seek(0)
    plt.close()
    return buf

# ---------- ЗАПРОСЫ К MOEX ----------
async def get_all_shares():
    global http_session
    url = "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json?iss.meta=off&iss.only=marketdata,securities"
    try:
        async with http_session.get(url) as resp:
            json_data = await resp.json()
            if 'marketdata' not in json_data or 'securities' not in json_data:
                return pd.DataFrame()
            md_columns = json_data['marketdata']['columns']
            md_rows = json_data['marketdata']['data']
            market_df = pd.DataFrame(md_rows, columns=md_columns)
            sec_columns = json_data['securities']['columns']
            sec_rows = json_data['securities']['data']
            sec_df = pd.DataFrame(sec_rows, columns=sec_columns)
            sec_df = sec_df[['SECID', 'SHORTNAME', 'LISTLEVEL']].copy()
            merged = pd.merge(market_df, sec_df, on='SECID', how='left')
            return merged
    except Exception as e:
        logging.error(f"Ошибка загрузки: {e}")
        return pd.DataFrame()

async def get_moex_index():
    global http_session
    url = "https://iss.moex.com/iss/engines/stock/markets/index/boards/SNDX/securities/IMOEX.json?iss.meta=off"
    try:
        async with http_session.get(url) as resp:
            json_data = await resp.json()
            columns = json_data['securities']['columns']
            data_rows = json_data['securities']['data']
            if data_rows:
                last_idx = columns.index('LAST')
                return float(data_rows[0][last_idx])
    except Exception:
        return None
    return None

async def get_historical_shares(from_date: str, till_date: str):
    global http_session
    url = f"https://iss.moex.com/iss/history/engines/stock/markets/shares/boards/TQBR/securities.json?from={from_date}&till={till_date}&iss.meta=off&iss.only=history"
    try:
        async with http_session.get(url) as resp:
            json_data = await resp.json()
            if 'history' not in json_data:
                return pd.DataFrame()
            columns = json_data['history']['columns']
            data_rows = json_data['history']['data']
            df = pd.DataFrame(data_rows, columns=columns)
            return df
    except Exception:
        return pd.DataFrame()

def get_top_movers(data: pd.DataFrame, top_n: int = TOP_N, exclude_level3: bool = True):
    if data.empty:
        return pd.DataFrame(), pd.DataFrame()
    if exclude_level3 and 'LISTLEVEL' in data.columns:
        data = data[data['LISTLEVEL'] < 3].copy()
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

def calc_period_change(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    df = df.copy()
    df['TRADEDATE'] = pd.to_datetime(df['TRADEDATE'])
    df = df.sort_values('TRADEDATE')
    first = df.groupby('SECID').first()[['OPEN']]
    last = df.groupby('SECID').last()[['CLOSE']]
    combined = first.join(last, how='inner')
    combined['CHANGE_PCT'] = ((combined['CLOSE'] - combined['OPEN']) / combined['OPEN']) * 100
    return combined.reset_index()

async def get_historical_close(ticker: str, target_date: datetime.date) -> float | None:
    from_date = (target_date - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
    till_date = target_date.strftime("%Y-%m-%d")
    df = await get_historical_shares(from_date, till_date)
    if df.empty:
        return None
    ticker_data = df[df['SECID'] == ticker].copy()
    if ticker_data.empty:
        return None
    ticker_data['TRADEDATE'] = pd.to_datetime(ticker_data['TRADEDATE'])
    ticker_data = ticker_data.sort_values('TRADEDATE')
    return ticker_data.iloc[-1]['CLOSE']

async def get_favorites_data(chat_id: int):
    favs = get_favorites(chat_id)
    if not favs:
        return None, "⭐ У вас пока нет избранных акций.\n\nДобавьте их через кнопку ✅ Добавить тикер."
    shares_df = await get_all_shares()
    if shares_df.empty:
        if is_weekend():
            return None, "📊 Сессия выходного дня. Избранное обновится в рабочие дни."
        else:
            return None, "📊 Биржа закрыта. Попробуйте позже."
    fav_df = shares_df[shares_df['SECID'].isin(favs)].copy()
    if fav_df.empty:
        return None, "По вашему списку нет актуальных данных."
    if 'CHANGEPERCENT' not in fav_df.columns:
        if 'OPEN' in fav_df.columns and 'LAST' in fav_df.columns:
            fav_df['CHANGEPERCENT'] = ((fav_df['LAST'] - fav_df['OPEN']) / fav_df['OPEN']) * 100
        else:
            return None, "Недостаточно данных для расчёта изменений."
    if 'SHORTNAME' not in fav_df.columns:
        fav_df['SHORTNAME'] = fav_df['SECID']

    now = get_moscow_time()
    monday = now - datetime.timedelta(days=now.weekday())
    week_reference = (monday - datetime.timedelta(days=1)).date()
    first_of_month = now.replace(day=1)
    month_reference = (first_of_month - datetime.timedelta(days=1)).date()

    week_changes = []
    month_changes = []
    for _, row in fav_df.iterrows():
        ticker = row['SECID']
        current_price = row['LAST']
        week_price = await get_historical_close(ticker, week_reference)
        if week_price is not None and week_price > 0:
            week_change = ((current_price - week_price) / week_price) * 100
        else:
            week_change = None
        month_price = await get_historical_close(ticker, month_reference)
        if month_price is not None and month_price > 0:
            month_change = ((current_price - month_price) / month_price) * 100
        else:
            month_change = None
        week_changes.append(week_change)
        month_changes.append(month_change)

    fav_df['change_week'] = week_changes
    fav_df['change_month'] = month_changes
    fav_df['change_week'] = fav_df['change_week'].fillna(float('nan'))
    fav_df['change_month'] = fav_df['change_month'].fillna(float('nan'))
    fav_df = fav_df.sort_values('CHANGEPERCENT', ascending=False)
    return fav_df, None

# ---------- ФОРМАТИРОВАНИЕ ТАБЛИЦ ----------
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

def format_message(gainers: pd.DataFrame, losers: pd.DataFrame, index_value, update_time: str, session_status: str) -> str:
    if index_value is not None:
        header = f"📊 Индекс МосБиржи: {index_value:.2f}\n"
    else:
        header = f"📊 {session_status}\n"
    header += f"🕒 Обновлено: {update_time}\n\n"
    text = header
    text += build_table_universal(gainers, "📈 Лидеры роста", ["Тикер", "Название", "Цена", "Изменение"], ['SECID', 'SHORTNAME', 'LAST', 'CHANGEPERCENT'])
    text += build_table_universal(losers, "📉 Лидеры падения", ["Тикер", "Название", "Цена", "Изменение"], ['SECID', 'SHORTNAME', 'LAST', 'CHANGEPERCENT'])
    return text

def format_historical_table(gainers, losers, period, from_date_dt, till_date_dt):
    if period == 'week':
        week_num = get_week_number(from_date_dt)
        title = f"📊 Топ за неделю #{week_num}"
        period_str = f"📅 Период: {from_date_dt.strftime('%d/%m/%y')} – {till_date_dt.strftime('%d/%m/%y')}"
    else:
        month_name = get_month_name_ru(from_date_dt.month)
        title = f"📅 Топ {month_name}"
        period_str = f"📅 Период: {from_date_dt.strftime('%d/%m/%y')} – {till_date_dt.strftime('%d/%m/%y')}"
    text = f"{title}\n{period_str}\n\n"
    text += build_table_universal(gainers, "📈 Рост", ["Тикер", "Название", "Изменение"], ['SECID', 'SHORTNAME', 'CHANGE_PCT'])
    text += build_table_universal(losers, "📉 Падение", ["Тикер", "Название", "Изменение"], ['SECID', 'SHORTNAME', 'CHANGE_PCT'])
    return text

# ---------- ГЕНЕРАЦИЯ КАРТИНКИ ----------
def generate_favorites_image(fav_df) -> io.BytesIO:
    if fav_df.empty:
        return None
    table_data = []
    for _, row in fav_df.iterrows():
        name = row.get('SHORTNAME', row['SECID'])
        if len(name) > 20:
            name = name[:17] + "…"
        price = f"{row['LAST']:.2f}" if isinstance(row['LAST'], (int, float)) else str(row['LAST'])
        day = f"{row['CHANGEPERCENT']:+.2f}%" if pd.notna(row['CHANGEPERCENT']) else "—"
        week = f"{row['change_week']:+.2f}%" if pd.notna(row['change_week']) else "—"
        month = f"{row['change_month']:+.2f}%" if pd.notna(row['change_month']) else "—"
        table_data.append([name, price, day, week, month])
    headers = ["Название", "Цена", "День", "Неделя", "Месяц"]
    fig, ax = plt.subplots(figsize=(8, max(3, len(table_data) * 0.4 + 1)))
    ax.axis('off')
    table = ax.table(cellText=table_data, colLabels=headers, loc='center', cellLoc='center',
                     colColours=['#f0f0f0']*5, bbox=[0, 0, 1, 1])
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1, 1.5)
    for i, row in enumerate(table_data):
        for j, cell in enumerate(row):
            if j >= 2:
                val = row[j]
                if val != "—" and val.startswith('+'):
                    table[(i+1, j)].set_facecolor('lightgreen')
                elif val != "—" and val.startswith('-'):
                    table[(i+1, j)].set_facecolor('lightcoral')
    ax.set_title("Избранные акции", fontsize=14, fontweight='bold', pad=20)
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', pad_inches=0.2)
    buf.seek(0)
    plt.close()
    return buf

# ---------- ОТПРАВКА ТОПА И АВТООБНОВЛЕНИЕ ----------
async def send_top(message: types.Message, period: str = 'day'):
    loading_msg = await message.answer("⏳ Загружаю данные...")
    try:
        if period == 'day':
            shares_df = await get_all_shares()
            gainers, losers = get_top_movers(shares_df, top_n=TOP_N)
            if gainers.empty and losers.empty:
                await loading_msg.delete()
                session_status = get_session_status()
                await message.answer(f"📊 {session_status}\nДанные обновятся в рабочее время.")
                return
            index_val = await get_moex_index()
            session_status = get_session_status()
            update_time = get_local_time().strftime("%d/%m/%y %H:%M:%S")
            text = format_message(gainers, losers, index_val, update_time, session_status)
        else:
            now = get_moscow_time()
            if period == 'week':
                start = now - datetime.timedelta(days=now.weekday())
                from_date = start
                from_date_str = start.strftime("%Y-%m-%d")
                period_name_short = 'week'
            else:
                start = now.replace(day=1)
                from_date = start
                from_date_str = start.strftime("%Y-%m-%d")
                period_name_short = 'month'
            till_date = now
            till_date_str = now.strftime("%Y-%m-%d")
            df = await get_historical_shares(from_date_str, till_date_str)
            if df.empty:
                await loading_msg.delete()
                await message.answer(f"Нет данных за {period}.")
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
            text = format_historical_table(gainers, losers, period_name_short, from_date, till_date)

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
        await loading_msg.delete()
    except Exception as e:
        await loading_msg.delete()
        logging.error(f"❌ Ошибка в send_top (period={period}): {e}", exc_info=True)
        await message.answer(f"❌ Ошибка при загрузке данных: {e}")

async def auto_update_task(chat_id: int, message_id: int):
    while True:
        await asyncio.sleep(30)
        try:
            shares_df = await get_all_shares()
            gainers, losers = get_top_movers(shares_df, top_n=TOP_N)
            if gainers.empty and losers.empty:
                continue
            index_val = await get_moex_index()
            session_status = get_session_status()
            update_time = get_local_time().strftime("%d/%m/%y %H:%M:%S")
            text = format_message(gainers, losers, index_val, update_time, session_status)
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh")]
                ]
            )
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode="HTML", reply_markup=keyboard)
        except Exception as e:
            logging.error(f"Ошибка автообновления для чата {chat_id}: {e}")
            break

# ---------- ОБРАБОТЧИКИ КОМАНД (ВСЕ С PrivateFilter) ----------
@dp.message(Command("start"), PrivateFilter())
async def cmd_start(message: types.Message):
    chat_id = message.chat.id
    auto_update_enabled[chat_id] = True
    try:
        await message.answer(
            "👋 Привет! Я бот для отслеживания топ-акций Мосбиржи и вашего портфеля Т-Инвестиций.\n\n"
            "Используйте кнопки ниже для навигации.",
            reply_markup=main_keyboard()
        )
        await send_top(message, 'day')
    except Exception as e:
        logging.error(f"❌ Ошибка в /start: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка при запуске: {e}")

@dp.message(lambda msg: msg.text == "📌 Топ дня", PrivateFilter())
async def top_day_button(message: types.Message):
    await send_top(message, 'day')
    await safe_delete_message(message.chat.id, message.message_id)

@dp.message(lambda msg: msg.text == "📊 Топ недели", PrivateFilter())
async def top_week_button(message: types.Message):
    await send_top(message, 'week')
    await safe_delete_message(message.chat.id, message.message_id)

@dp.message(lambda msg: msg.text == "🗓️ Топ месяца", PrivateFilter())
async def top_month_button(message: types.Message):
    await send_top(message, 'month')
    await safe_delete_message(message.chat.id, message.message_id)

@dp.message(lambda msg: msg.text == "⭐ Избранные", PrivateFilter())
async def favorites_button(message: types.Message):
    try:
        loading_msg = await message.answer("⏳ Загружаю избранное...")
        fav_df, error = await get_favorites_data(message.chat.id)
        if error:
            await loading_msg.delete()
            await message.answer(error)
            await safe_delete_message(message.chat.id, message.message_id)
            return
        img_buf = generate_favorites_image(fav_df)
        if img_buf is None:
            await loading_msg.delete()
            await message.answer("Нет данных для отображения.")
            await safe_delete_message(message.chat.id, message.message_id)
            return
        await message.answer_photo(
            photo=BufferedInputFile(img_buf.getvalue(), filename="favorites.png")
        )
        await loading_msg.delete()
        await safe_delete_message(message.chat.id, message.message_id)
    except Exception as e:
        await loading_msg.delete()
        logging.error(f"❌ Ошибка в favorites: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка при загрузке избранного: {e}")
        await safe_delete_message(message.chat.id, message.message_id)

@dp.message(lambda msg: msg.text == "📈 Портфель", PrivateFilter())
async def portfolio_button(message: types.Message):
    await cmd_portfolio(message)

@dp.message(lambda msg: msg.text == "📊 График покупок", PrivateFilter())
async def buys_chart_button(message: types.Message):
    await cmd_buys(message)

@dp.message(Command("portfolio"), PrivateFilter())
async def cmd_portfolio(message: types.Message):
    if not TINKOFF_TOKEN:
        await message.answer("❌ Токен TITN не задан. Добавьте его в переменные окружения.")
        return
    loading_msg = await message.answer("⏳ Загружаю данные портфеля...")
    try:
        data = get_portfolio_data()
        if "error" in data:
            await loading_msg.delete()
            await message.answer(f"❌ {data['error']}")
            return
        text = f"📊 *{data['account_name']}*\n"
        text += f"💰 Сумма портфеля: {data['total_amount']:.2f} ₽\n\n"
        if not data["positions"]:
            text += "Позиций нет."
        else:
            text += "📈 *Позиции:*\n"
            for pos in data["positions"]:
                yield_pct = (pos["expected_yield"] / (pos["average_price"] * pos["quantity"]) * 100) if pos["average_price"] and pos["quantity"] else 0
                text += f"• {pos['name']} ({pos['figi'][:6]}): {pos['quantity']} шт., {pos['current_price']:.2f} ₽, доходность {yield_pct:+.2f}%\n"
        await message.answer(text, parse_mode="Markdown")
        await loading_msg.delete()
    except Exception as e:
        await loading_msg.delete()
        logging.error(f"Ошибка портфеля: {e}")
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("buys"), PrivateFilter())
async def cmd_buys(message: types.Message):
    if not TINKOFF_TOKEN:
        await message.answer("❌ Токен TITN не задан. Добавьте его в переменные окружения.")
        return
    loading_msg = await message.answer("⏳ Строю график покупок за месяц...")
    try:
        chart_buf = build_purchases_chart()
        if chart_buf is None:
            await loading_msg.delete()
            await message.answer("❌ Не удалось построить график покупок. Возможно, за месяц не было покупок или ошибка API.")
            return
        await message.answer_photo(
            photo=BufferedInputFile(chart_buf.getvalue(), filename="purchases.png"),
            caption=f"📊 Покупки за {get_moscow_time().strftime('%B %Y')}"
        )
        await loading_msg.delete()
    except Exception as e:
        await loading_msg.delete()
        logging.error(f"Ошибка графика покупок: {e}")
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(lambda msg: msg.text == "✅ Добавить тикер", PrivateFilter())
async def add_ticker_button(message: types.Message):
    user_state[message.chat.id] = 'add'
    await message.answer("Введите тикер для добавления (например, SBER или SBER, GAZP):")
    await safe_delete_message(message.chat.id, message.message_id)

@dp.message(lambda msg: msg.text == "❌ Удалить тикер", PrivateFilter())
async def remove_ticker_button(message: types.Message):
    user_state[message.chat.id] = 'remove'
    await message.answer("Введите тикер для удаления (например, SBER или SBER, GAZP):")
    await safe_delete_message(message.chat.id, message.message_id)

@dp.message(PrivateFilter())
async def handle_text(message: types.Message):
    chat_id = message.chat.id
    if chat_id in user_state:
        state = user_state[chat_id]
        raw = message.text.strip()
        tickers = [t.strip().upper() for t in raw.split(',') if t.strip()]
        results = []
        for ticker in tickers:
            if state == 'add':
                if add_favorite(chat_id, ticker):
                    results.append(f"✅ {ticker} добавлен")
                else:
                    results.append(f"ℹ️ {ticker} уже есть")
            elif state == 'remove':
                if remove_favorite(chat_id, ticker):
                    results.append(f"✅ {ticker} удалён")
                else:
                    results.append(f"ℹ️ {ticker} не найден")
        await message.answer("\n".join(results) if results else "Ничего не сделано.")
        del user_state[chat_id]
        await message.answer("✅ Готово. Выберите действие из меню.", reply_markup=main_keyboard())
        await safe_delete_message(chat_id, message.message_id)
    else:
        await message.answer("Используйте кнопки меню.", reply_markup=main_keyboard())
        await safe_delete_message(chat_id, message.message_id)

@dp.callback_query(lambda c: c.data == "refresh", PrivateFilter())
async def process_refresh(callback: CallbackQuery):
    try:
        await callback.answer("Обновляю...", cache_time=0)
    except Exception:
        pass
    try:
        shares_df = await get_all_shares()
        gainers, losers = get_top_movers(shares_df, top_n=TOP_N)
        if gainers.empty and losers.empty:
            session_status = get_session_status()
            await callback.message.answer(f"📊 {session_status}\nДанные обновятся в рабочее время.")
            return
        index_val = await get_moex_index()
        session_status = get_session_status()
        update_time = get_local_time().strftime("%d/%m/%y %H:%M:%S")
        text = format_message(gainers, losers, index_val, update_time, session_status)
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

# ---------- ЗАПУСК (POLLING + HEALTH-SERVER) ----------
async def health_handler(request):
    return web.Response(text="OK")

async def run_health_server():
    app = web.Application()
    app.router.add_get('/health', health_handler)
    app.router.add_get('/', health_handler)  # корень тоже отвечает
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logging.info(f"✅ Health‑сервер запущен на порту {PORT}")
    # Бесконечно ждём, пока сервер работает
    await asyncio.Event().wait()

async def main():
    global http_session
    init_db()
    http_session = aiohttp.ClientSession()

    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("✅ Вебхук удалён")

    logging.info("✅ Запускаем polling...")
    polling_task = asyncio.create_task(dp.start_polling(bot))
    health_task = asyncio.create_task(run_health_server())

    # Ждём завершения любой задачи (если одна упадёт, завершаем другую)
    done, pending = await asyncio.wait(
        [polling_task, health_task],
        return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    await http_session.close()
    logging.info("✅ HTTP сессия закрыта")

if __name__ == "__main__":
    asyncio.run(main())
