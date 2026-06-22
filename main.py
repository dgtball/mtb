# ==============================================
# БОТ ДЛЯ ТОП-АКЦИЙ МОСБИРЖИ И ПОРТФЕЛЯ Т-ИНВЕСТИЦИЙ
# Версия: 8.7 (исправлена фильтрация акций)
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
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile
)
from aiohttp import web
import aiohttp
import pandas as pd
from tabulate import tabulate
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import plotly.io as pio
pio.kaleido.scope.default_format = "png"

# ---------- ВЕРСИЯ ----------
VERSION = "8.7"

# ---------- КОНФИГУРАЦИЯ ----------
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

TINKOFF_TOKEN = os.getenv("TITN")

MY_CHAT_ID = os.getenv("MY_CHAT_ID")
if not MY_CHAT_ID:
    raise ValueError("MY_CHAT_ID не задан")
try:
    MY_CHAT_ID = int(MY_CHAT_ID)
except ValueError:
    raise ValueError("MY_CHAT_ID должен быть числом")

TOP_N = 10
DATA_DIR = os.getenv('DATA_DIR', '/app/data')
DB_PATH = os.path.join(DATA_DIR, 'favorites.db')
PORT = int(os.getenv('PORT', 3000))

TINKOFF_API_URL = os.getenv("TINKOFF_API_URL", "https://invest-public-api.tbank.ru/rest/")

# ---------- ПЕРЕОПРЕДЕЛЕНИЕ НАЗВАНИЙ ДЛЯ ПОРТФЕЛЯ ----------
NAME_OVERRIDES = {
    "MDMG-ао": "Мать и Дитя",
    "iАстра ао": "Астра",
    "iСофтлайн": "Софтлайн",
    "МКПАО \"ВК\"": "ВК",
    "iВУШХолдинг": "ВУШ",
    "iКаршеринг": "Делимобиль",
    "Татнфт Зап": "Татнефть-ап",
    "СевСт-ао": "Северсталь",
    "Роснефть": "Роснефть",
    "Газпотреб": "Газпром нефть",
    "ГАЗПРОМ ао": "Газпром",
    "Ростел -ао": "Ростелеком",
    "Т-Техно ао": "Т-Технологии",
    "КЦ ИКС 5": "X5",
    "Самолет ао": "Самолет",
}

# ---------- СПИСОК НЕТОРГОВЫХ ВЫХОДНЫХ 2026 ----------
NO_TRADING_WEEKENDS_2026 = [
    ("2026-01-03", "2026-01-04"),
    ("2026-01-10", "2026-01-11"),
    ("2026-02-14", "2026-02-15"),
    ("2026-03-07", "2026-03-08"),
    ("2026-03-21", "2026-03-22"),
    ("2026-05-09", "2026-05-10"),
    ("2026-06-20", "2026-06-21"),
    ("2026-08-01", "2026-08-02"),
    ("2026-08-15", "2026-08-16"),
    ("2026-09-12", "2026-09-13"),
    ("2026-10-24", "2026-10-25"),
    ("2026-12-05", "2026-12-06"),
]

# ---------- ЛОГИРОВАНИЕ ----------
logging.basicConfig(level=logging.INFO)

# ---------- ГЛОБАЛЬНЫЙ СЛОВАРЬ ДЛЯ НАЗВАНИЙ ИЗ MOEX ----------
ticker_to_name = {}

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
        [KeyboardButton(text="✅ Добавить тикер"), KeyboardButton(text="❌ Удалить тикер")],
    ]
    if TINKOFF_TOKEN:
        kb.insert(3, [KeyboardButton(text="📈 Портфель")])
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
    today_str = now.strftime("%Y-%m-%d")
    is_weekend = now.weekday() in (5, 6)

    if is_weekend:
        for start, end in NO_TRADING_WEEKENDS_2026:
            if start <= today_str <= end:
                return "Биржа закрыта (выходной)"

    if is_weekend:
        if (now.hour > 9 or (now.hour == 9 and now.minute >= 50)) and (now.hour < 19 or (now.hour == 19 and now.minute == 0)):
            return "Сессия выходного дня"
        else:
            return "Биржа закрыта"

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

# ---------- ЗАГРУЗКА НАЗВАНИЙ ИЗ MOEX ----------
async def load_instrument_names():
    global ticker_to_name
    global http_session
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    url_shares = "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json?iss.meta=off&iss.only=securities"
    try:
        async with http_session.get(url_shares, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                json_data = await resp.json()
                if 'securities' in json_data:
                    columns = json_data['securities']['columns']
                    data_rows = json_data['securities']['data']
                    df = pd.DataFrame(data_rows, columns=columns)
                    if 'SECID' in df.columns and 'SHORTNAME' in df.columns:
                        for _, row in df.iterrows():
                            ticker_to_name[row['SECID']] = row['SHORTNAME']
    except Exception as e:
        logging.error(f"Ошибка загрузки акций: {e}")

    for board in ['TQOB', 'TQCB']:
        url_bonds = f"https://iss.moex.com/iss/engines/stock/markets/bonds/boards/{board}/securities.json?iss.meta=off&iss.only=securities"
        try:
            async with http_session.get(url_bonds, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    json_data = await resp.json()
                    if 'securities' in json_data:
                        columns = json_data['securities']['columns']
                        data_rows = json_data['securities']['data']
                        df = pd.DataFrame(data_rows, columns=columns)
                        if 'SECID' in df.columns and 'SHORTNAME' in df.columns:
                            for _, row in df.iterrows():
                                ticker_to_name[row['SECID']] = row['SHORTNAME']
        except Exception as e:
            logging.error(f"Ошибка загрузки облигаций {board}: {e}")

    url_etf = "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQTF/securities.json?iss.meta=off&iss.only=securities"
    try:
        async with http_session.get(url_etf, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                json_data = await resp.json()
                if 'securities' in json_data:
                    columns = json_data['securities']['columns']
                    data_rows = json_data['securities']['data']
                    df = pd.DataFrame(data_rows, columns=columns)
                    if 'SECID' in df.columns and 'SHORTNAME' in df.columns:
                        for _, row in df.iterrows():
                            ticker_to_name[row['SECID']] = row['SHORTNAME']
    except Exception as e:
        logging.error(f"Ошибка загрузки ETF: {e}")

    logging.info(f"✅ Загружено {len(ticker_to_name)} наименований")

# ---------- ЗАПРОСЫ К MOEX ДЛЯ ТОПА ----------
async def get_market_data():
    global http_session
    url = "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json?iss.meta=off&iss.only=marketdata,securities"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    for attempt in range(3):
        try:
            async with http_session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    await asyncio.sleep(2)
                    continue
                json_data = await resp.json()
                if 'marketdata' not in json_data or 'securities' not in json_data:
                    await asyncio.sleep(2)
                    continue
                md_columns = json_data['marketdata']['columns']
                md_rows = json_data['marketdata']['data']
                market_df = pd.DataFrame(md_rows, columns=md_columns)
                sec_columns = json_data['securities']['columns']
                sec_rows = json_data['securities']['data']
                sec_df = pd.DataFrame(sec_rows, columns=sec_columns)
                available_cols = ['SECID', 'SHORTNAME', 'LISTLEVEL']
                if 'BOARDID' in sec_df.columns:
                    available_cols.append('BOARDID')
                # Не используем SECTYPE
                sec_df = sec_df[available_cols].copy()
                merged = pd.merge(market_df, sec_df, on='SECID', how='left')
                return merged
        except Exception:
            await asyncio.sleep(2)
    return pd.DataFrame()

async def get_moex_index_info():
    """
    Возвращает словарь с данными индекса IMOEX:
    last (текущее значение), change_percent
    """
    global http_session
    url = "https://iss.moex.com/iss/engines/stock/markets/index/boards/SNDX/securities/IMOEX.json?iss.meta=off"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        async with http_session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                logging.warning(f"MOEX index returned status {resp.status}")
                return None
            json_data = await resp.json()
            if 'marketdata' in json_data:
                md_columns = json_data['marketdata']['columns']
                md_rows = json_data['marketdata']['data']
                if md_rows:
                    row = md_rows[0]
                    current_idx = md_columns.index('CURRENTVALUE') if 'CURRENTVALUE' in md_columns else None
                    if current_idx is None:
                        current_idx = md_columns.index('LASTVALUE') if 'LASTVALUE' in md_columns else None
                    change_idx = md_columns.index('LASTCHANGEPRC') if 'LASTCHANGEPRC' in md_columns else None
                    if change_idx is None:
                        change_idx = md_columns.index('LASTCHANGETOOPENPRC') if 'LASTCHANGETOOPENPRC' in md_columns else None
                    result = {}
                    if current_idx is not None:
                        result['last'] = float(row[current_idx])
                    if change_idx is not None:
                        result['change_percent'] = float(row[change_idx])
                    if result:
                        logging.info(f"Индекс IMOEX из marketdata: {result}")
                        return result
            if 'securities' in json_data:
                columns = json_data['securities']['columns']
                data_rows = json_data['securities']['data']
                if data_rows:
                    row = data_rows[0]
                    last_idx = columns.index('LAST') if 'LAST' in columns else None
                    change_percent_idx = columns.index('CHANGEPERCENT') if 'CHANGEPERCENT' in columns else None
                    result = {}
                    if last_idx is not None:
                        result['last'] = float(row[last_idx])
                    if change_percent_idx is not None:
                        result['change_percent'] = float(row[change_percent_idx])
                    if result:
                        logging.info(f"Индекс IMOEX из securities: {result}")
                        return result
            logging.warning("Не найдены данные индекса")
            return None
    except Exception as e:
        logging.error(f"Ошибка получения индекса: {e}")
        return None

async def get_moex_index():
    info = await get_moex_index_info()
    return info.get('last') if info else None

async def get_historical_shares(from_date: str, till_date: str):
    global http_session
    url = f"https://iss.moex.com/iss/history/engines/stock/markets/shares/boards/TQBR/securities.json?from={from_date}&till={till_date}&iss.meta=off&iss.only=history"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    for attempt in range(3):
        try:
            async with http_session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    await asyncio.sleep(2)
                    continue
                json_data = await resp.json()
                if 'history' not in json_data:
                    return pd.DataFrame()
                columns = json_data['history']['columns']
                data_rows = json_data['history']['data']
                df = pd.DataFrame(data_rows, columns=columns)
                return df
        except Exception:
            await asyncio.sleep(2)
    return pd.DataFrame()

def get_top_movers(data: pd.DataFrame, top_n: int = TOP_N, exclude_level3: bool = True):
    if data.empty:
        return pd.DataFrame(), pd.DataFrame()
    # Фильтрация по BOARDID (акции)
    if 'BOARDID' in data.columns:
        data = data[data['BOARDID'] == 'TQBR'].copy()
    # Исключаем 3-й эшелон
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

# ---------- ФУНКЦИИ ДЛЯ Т-ИНВЕСТИЦИЙ ----------
async def tinkoff_api_request(method: str, endpoint: str, params: dict = None) -> dict:
    if not TINKOFF_TOKEN:
        raise ValueError("Токен TITN не задан")
    url = f"{TINKOFF_API_URL}{endpoint}"
    logging.info(f"🔗 Запрос к API Т-Инвестиций: {url}")
    headers = {
        "Authorization": f"Bearer {TINKOFF_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    async with http_session.request(method, url, headers=headers, json=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        logging.info(f"📊 Статус ответа: {resp.status}")
        if resp.status != 200:
            text = await resp.text()
            logging.error(f"Ошибка API: статус {resp.status}, тело: {text[:500]}")
            raise Exception(f"API вернул ошибку {resp.status}: {text[:200]}")
        data = await resp.json()
        return data

async def get_accounts() -> list:
    params = {"status": "ACCOUNT_STATUS_OPEN"}
    data = await tinkoff_api_request("POST", "tinkoff.public.invest.api.contract.v1.UsersService/GetAccounts", params=params)
    return data.get("accounts", [])

async def get_portfolio_data(account_id: str) -> dict:
    params = {"accountId": account_id}
    data = await tinkoff_api_request("POST", "tinkoff.public.invest.api.contract.v1.OperationsService/GetPortfolio", params=params)
    return data

async def get_portfolio_summary():
    try:
        if not ticker_to_name:
            await load_instrument_names()

        accounts = await get_accounts()
        if not accounts:
            return None
        account_id = accounts[0].get("id")
        if not account_id:
            return None

        data = await get_portfolio_data(account_id)
        positions = data.get("positions", [])
        total_amount = data.get("totalAmountPortfolio", {})
        total = float(total_amount.get("units", 0))
        total_currency = total_amount.get("currency", "RUB")

        total_cost = 0.0
        total_value = 0.0
        balance = 0.0

        type_map = {
            "INSTRUMENT_TYPE_SHARE": "Акции",
            "INSTRUMENT_TYPE_BOND": "Облигации",
            "INSTRUMENT_TYPE_ETF": "Фонды",
            "INSTRUMENT_TYPE_CURRENCY": "Валюта",
        }

        # Находим валютную позицию по тикеру или типу
        for pos in positions:
            ticker = pos.get("ticker", "")
            if ticker == "RUB000UTSTOM" or pos.get("instrumentType") == "INSTRUMENT_TYPE_CURRENCY":
                balance = float(pos.get("quantity", {}).get("units", 0)) * float(pos.get("currentPrice", {}).get("units", 1))
                break

        # Остальные позиции (исключаем валюту)
        filtered_positions = []
        for pos in positions:
            ticker = pos.get("ticker", "")
            if ticker == "RUB000UTSTOM" or pos.get("instrumentType") == "INSTRUMENT_TYPE_CURRENCY":
                continue
            filtered_positions.append(pos)

        logging.info(f"После фильтрации осталось {len(filtered_positions)} позиций")

        for pos in filtered_positions:
            quantity = float(pos.get("quantity", {}).get("units", 0))
            avg_price = float(pos.get("averagePositionPrice", {}).get("units", 0))
            price = float(pos.get("currentPrice", {}).get("units", 0))
            total_cost += quantity * avg_price
            total_value += quantity * price

        if total_cost > 0:
            total_yield_pct = (total_value - total_cost) / total_cost * 100
        else:
            total_yield_pct = 0.0

        result = {
            "total_amount": total_value,
            "currency": total_currency,
            "total_cost": total_cost,
            "total_yield_pct": total_yield_pct,
            "balance": balance,
            "positions": [],
            "expected_dividends": float(data.get("expectedDividends", 0))
        }

        for pos in filtered_positions:
            figi = pos.get("figi")
            ticker = pos.get("ticker") or figi
            raw_name = ticker_to_name.get(ticker, ticker)
            name = NAME_OVERRIDES.get(raw_name, raw_name)

            quantity = float(pos.get("quantity", {}).get("units", 0))
            price = float(pos.get("currentPrice", {}).get("units", 0))
            avg_price = float(pos.get("averagePositionPrice", {}).get("units", 0))
            expected_yield = float(pos.get("expectedYield", {}).get("units", 0))
            if avg_price and quantity:
                pos_yield_pct = (expected_yield / (avg_price * quantity)) * 100
            else:
                pos_yield_pct = 0.0
            instrument_type = pos.get("instrumentType", "")

            if instrument_type in type_map:
                type_display = type_map[instrument_type]
            else:
                if "ОФЗ" in name or "SU" in ticker:
                    type_display = "Облигации"
                elif "ETF" in name or "LQDT" in ticker or "TGLD" in ticker:
                    type_display = "Фонды"
                else:
                    type_display = "Акции"

            result["positions"].append({
                "figi": figi,
                "ticker": ticker,
                "name": name,
                "instrument_type": instrument_type,
                "type_display": type_display,
                "quantity": quantity,
                "price": price,
                "avg_price": avg_price,
                "pos_yield_pct": pos_yield_pct,
            })

        return result
    except Exception as e:
        logging.error(f"Ошибка портфеля: {e}")
        return None

# ---------- ГЕНЕРАЦИЯ КАРТИНКИ ПОРТФЕЛЯ ----------
def generate_portfolio_image(portfolio_data) -> io.BytesIO:
    if not portfolio_data or not portfolio_data["positions"]:
        logging.warning("Нет позиций для отображения портфеля")
        return None

    groups = defaultdict(list)
    for pos in portfolio_data["positions"]:
        groups[pos["type_display"]].append(pos)

    order = ["Акции", "Облигации", "Фонды"]
    ordered_groups = [(key, groups.pop(key)) for key in order if key in groups]
    for key, vals in groups.items():
        ordered_groups.append((key, vals))

    if not ordered_groups:
        return None

    for group_name, positions in ordered_groups:
        logging.info(f"Группа {group_name}: {len(positions)} позиций")

    total_amount = portfolio_data["total_amount"]
    total_cost = portfolio_data["total_cost"]
    total_yield = portfolio_data["total_yield_pct"]
    balance = portfolio_data.get("balance", 0.0)

    rows = len(ordered_groups)
    specs = [[{"type": "table"} for _ in range(1)] for _ in range(rows)]
    fig = make_subplots(rows=rows, cols=1, shared_xaxes=False,
                        vertical_spacing=0.05, subplot_titles=[g[0] for g in ordered_groups],
                        specs=specs)

    height = 200 + sum(len(v) * 25 for _, v in ordered_groups) + rows * 60

    col_labels = ["Название", "Кол-во", "Цена", "Средняя", "Доходность"]

    for idx, (group_name, positions) in enumerate(ordered_groups, start=1):
        table_data = []
        for pos in positions:
            display_name = pos["name"][:30] if pos["name"] else pos["ticker"]
            table_data.append([
                display_name,
                f"{pos['quantity']:.0f}",
                f"{pos['price']:.2f}",
                f"{pos['avg_price']:.2f}",
                f"{pos['pos_yield_pct']:+.2f}%"
            ])

        table_trace = go.Table(
            header=dict(
                values=col_labels,
                fill_color='#f0f0f0',
                align='center',
                font=dict(size=12, color='black', family='Arial')
            ),
            cells=dict(
                values=[list(col) for col in zip(*table_data)] if table_data else [[]],
                fill_color=[[
                    '#e6f9e6' if float(row[4].replace('%', '').replace('+', '')) > 0 else '#fce4e4'
                    for row in table_data
                ]],
                align='center',
                font=dict(size=11, color='black', family='Arial')
            ),
            name=group_name
        )

        fig.add_trace(table_trace, row=idx, col=1)

    fig.update_layout(
        title=dict(
            text=f"Портфель<br>Сумма: {total_amount:.2f} ₽   Вложено: {total_cost:.2f} ₽   "
                 f"Доходность: {total_yield:+.2f}%   Баланс: {balance:.2f} ₽",
            x=0.5,
            xanchor='center',
            font=dict(size=14, family='Arial', color='black')
        ),
        width=800,
        height=height,
        margin=dict(l=20, r=20, t=80, b=20),
        paper_bgcolor='white',
        showlegend=False
    )

    try:
        img_bytes = pio.to_image(fig, format='png', engine='kaleido')
        return io.BytesIO(img_bytes)
    except Exception as e:
        logging.error(f"Ошибка экспорта портфеля в PNG: {e}")
        return None

# ---------- ОСТАЛЬНЫЕ ФУНКЦИИ ----------
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

def format_message(gainers: pd.DataFrame, losers: pd.DataFrame, index_info: dict, update_time: str, session_status: str) -> str:
    header = ""
    if index_info and 'last' in index_info:
        last = index_info['last']
        change = index_info.get('change_percent', 0)
        arrow = ""
        if change > 0:
            arrow = "📈"
        elif change < 0:
            arrow = "📉"
        header += f"💼 Индекс МосБиржи: {last:.2f} ({change:+.2f}%) {arrow}\n"
    header += f"📌 {session_status}\n"
    header += f"🕒 Обновлено: {update_time}\n\n"
    text = header
    text += build_table_universal(gainers, "📈 Лидеры роста", ["Тикер", "Название", "Цена", "Изменение"], ['SECID', 'SHORTNAME', 'LAST', 'CHANGEPERCENT'])
    text += build_table_universal(losers, "📉 Лидеры падения", ["Тикер", "Название", "Цена", "Изменение"], ['SECID', 'SHORTNAME', 'LAST', 'CHANGEPERCENT'])
    return text

def format_historical_table(gainers, losers, period, from_date_dt, till_date_dt):
    if period == 'week':
        week_num = get_week_number(from_date_dt)
        title = f"📅 Топ за неделю #{week_num}"
        period_str = f"Период: {from_date_dt.strftime('%d/%m/%y')} – {till_date_dt.strftime('%d/%m/%y')}"
    else:
        month_name = get_month_name_ru(from_date_dt.month)
        title = f"🗓️ Топ {month_name}"
        period_str = f"Период: {from_date_dt.strftime('%d/%m/%y')} – {till_date_dt.strftime('%d/%m/%y')}"
    text = f"{title}\n{period_str}\n\n"
    text += build_table_universal(gainers, "📈 Рост", ["Тикер", "Название", "Изменение"], ['SECID', 'SHORTNAME', 'CHANGE_PCT'])
    text += build_table_universal(losers, "📉 Падение", ["Тикер", "Название", "Изменение"], ['SECID', 'SHORTNAME', 'CHANGE_PCT'])
    return text

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

async def get_favorites_data(chat_id: int):
    favs = get_favorites(chat_id)
    if not favs:
        return None, "⭐ У вас пока нет избранных акций.\n\nДобавьте их через кнопку ✅ Добавить тикер."
    shares_df = await get_market_data()
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
    fav_df['change_week'] = fav_df['change_week'].fillna(float('nan')).infer_objects(copy=False)
    fav_df['change_month'] = fav_df['change_month'].fillna(float('nan')).infer_objects(copy=False)
    fav_df = fav_df.sort_values('CHANGEPERCENT', ascending=False)
    return fav_df, None

async def send_top(message: types.Message, period: str = 'day'):
    loading_msg = await message.answer("⏳ Загружаю данные...")
    try:
        if period == 'day':
            shares_df = await get_market_data()
            gainers, losers = get_top_movers(shares_df, top_n=TOP_N)
            if gainers.empty and losers.empty:
                await loading_msg.delete()
                session_status = get_session_status()
                await message.answer(f"📌 {session_status}\nДанные обновятся в рабочее время.")
                return
            index_info = await get_moex_index_info()
            session_status = get_session_status()
            update_time = get_local_time().strftime("%d/%m/%y %H:%M:%S")
            text = format_message(gainers, losers, index_info, update_time, session_status)
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
            shares_all = await get_market_data()
            if not shares_all.empty:
                allowed_tickers = shares_all[shares_all['LISTLEVEL'] < 3]['SECID'].unique()
                changes = changes[changes['SECID'].isin(allowed_tickers)]
                names = shares_all[['SECID', 'SHORTNAME']].drop_duplicates('SECID')
                changes = changes.merge(names, on='SECID', how='left')
            gainers = changes.nlargest(TOP_N, 'CHANGE_PCT')
            losers = changes.nsmallest(TOP_N, 'CHANGE_PCT')
            text = format_historical_table(gainers, losers, period_name_short, from_date, till_date)

        sent_msg = await message.answer(text, parse_mode="HTML")
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
            shares_df = await get_market_data()
            gainers, losers = get_top_movers(shares_df, top_n=TOP_N)
            if gainers.empty and losers.empty:
                continue
            index_info = await get_moex_index_info()
            session_status = get_session_status()
            update_time = get_local_time().strftime("%d/%m/%y %H:%M:%S")
            text = format_message(gainers, losers, index_info, update_time, session_status)
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Ошибка автообновления для чата {chat_id}: {e}")
            break

# ---------- ОБРАБОТЧИКИ КОМАНД ----------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id != MY_CHAT_ID:
        await message.answer("⛔ Доступ запрещён.")
        return
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

@dp.message()
async def handle_buttons_and_commands(message: types.Message):
    if message.from_user.id != MY_CHAT_ID:
        await message.answer("⛔ Доступ запрещён.")
        return
    text = message.text
    logging.info(f"🔄 Обработка сообщения: '{text}'")

    if text == "/portfolio" or text == "📈 Портфель":
        logging.info("🔍 Обработка портфеля")
        if not TINKOFF_TOKEN:
            await message.answer("❌ Токен TITN не задан. Добавьте его в переменные окружения.")
            await safe_delete_message(message.chat.id, message.message_id)
            return
        loading_msg = await message.answer("⏳ Загружаю данные портфеля...")
        try:
            data = await get_portfolio_summary()
            if not data:
                await loading_msg.delete()
                await message.answer("❌ Не удалось получить данные портфеля.")
                await safe_delete_message(message.chat.id, message.message_id)
                return
            img_buf = generate_portfolio_image(data)
            if img_buf is None:
                await loading_msg.delete()
                await message.answer("Нет данных для отображения.")
                await safe_delete_message(message.chat.id, message.message_id)
                return
            await message.answer_photo(
                photo=BufferedInputFile(img_buf.getvalue(), filename="portfolio.png")
            )
            await loading_msg.delete()
            await safe_delete_message(message.chat.id, message.message_id)
        except Exception as e:
            await loading_msg.delete()
            logging.error(f"❌ Ошибка портфеля: {e}", exc_info=True)
            await message.answer(f"❌ Ошибка: {e}")
            await safe_delete_message(message.chat.id, message.message_id)
        return

    if text == "📌 Топ дня":
        shares_df = await get_market_data()
        gainers, losers = get_top_movers(shares_df, top_n=TOP_N)
        if gainers.empty and losers.empty:
            session_status = get_session_status()
            await message.answer(f"📌 {session_status}\nДанные обновятся в рабочее время.")
            await safe_delete_message(message.chat.id, message.message_id)
            return
        index_info = await get_moex_index_info()
        session_status = get_session_status()
        update_time = get_local_time().strftime("%d/%m/%y %H:%M:%S")
        text = format_message(gainers, losers, index_info, update_time, session_status)
        sent_msg = await message.answer(text, parse_mode="HTML")
        chat_id = message.chat.id
        last_messages[chat_id] = sent_msg.message_id
        if auto_update_enabled.get(chat_id, False):
            if chat_id not in update_tasks or update_tasks[chat_id].done():
                task = asyncio.create_task(auto_update_task(chat_id, sent_msg.message_id))
                update_tasks[chat_id] = task
        await safe_delete_message(message.chat.id, message.message_id)
        return

    if text == "📊 Топ недели":
        await send_top(message, 'week')
        await safe_delete_message(message.chat.id, message.message_id)
        return

    if text == "🗓️ Топ месяца":
        await send_top(message, 'month')
        await safe_delete_message(message.chat.id, message.message_id)
        return

    if text == "⭐ Избранные":
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
        return

    if text == "✅ Добавить тикер":
        prompt_msg = await message.answer("Введите тикер для добавления (например, SBER или SBER, GAZP):")
        user_state[message.chat.id] = {'state': 'add', 'prompt_msg_id': prompt_msg.message_id}
        await safe_delete_message(message.chat.id, message.message_id)
        return

    if text == "❌ Удалить тикер":
        prompt_msg = await message.answer("Введите тикер для удаления (например, SBER или SBER, GAZP):")
        user_state[message.chat.id] = {'state': 'remove', 'prompt_msg_id': prompt_msg.message_id}
        await safe_delete_message(message.chat.id, message.message_id)
        return

    chat_id = message.chat.id
    if chat_id in user_state:
        state_data = user_state[chat_id]
        state = state_data['state']
        prompt_msg_id = state_data['prompt_msg_id']

        await safe_delete_message(chat_id, prompt_msg_id)
        await safe_delete_message(chat_id, message.message_id)

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
        return

    logging.info(f"FALLBACK: получено сообщение: '{text}'")
    await message.answer("Используйте кнопки меню.", reply_markup=main_keyboard())
    await safe_delete_message(message.chat.id, message.message_id)

# ---------- ЗАПУСК ----------
async def health_handler(request):
    return web.Response(text="OK")

async def run_health_server():
    app = web.Application()
    app.router.add_get('/health', health_handler)
    app.router.add_get('/', health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logging.info(f"✅ Health‑сервер запущен на порту {PORT}")
    await asyncio.Event().wait()

async def main():
    global http_session
    init_db()
    http_session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))

    await load_instrument_names()

    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("✅ Вебхук удалён")

    try:
        await bot.send_message(MY_CHAT_ID, f"🚀 Бот перезапущен и готов к работе! ver: {VERSION}")
        logging.info("✅ Уведомление о запуске отправлено")
    except Exception as e:
        logging.error(f"❌ Не удалось отправить уведомление о запуске: {e}")

    logging.info("✅ Запускаем polling...")
    polling_task = asyncio.create_task(dp.start_polling(bot))
    health_task = asyncio.create_task(run_health_server())

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