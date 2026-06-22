# ==============================================
# БОТ ДЛЯ ТОП-АКЦИЙ МОСБИРЖИ И ПОРТФЕЛЯ Т-ИНВЕСТИЦИЙ
# Версия: 6.7 (названия из MOEX, расширенная картинка портфеля)
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
from matplotlib.patches import Patch
import numpy as np

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

# ---------- КОНФИГУРАЦИЯ ----------
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

TINKOFF_TOKEN = os.getenv("TITN")   # Токен Т-Инвестиций (read-only)

MY_CHAT_ID = os.getenv("MY_CHAT_ID")
if not MY_CHAT_ID:
    raise ValueError("MY_CHAT_ID не задан. Добавьте его в переменные окружения.")
try:
    MY_CHAT_ID = int(MY_CHAT_ID)
except ValueError:
    raise ValueError("MY_CHAT_ID должен быть числом")

TOP_N = 10
DATA_DIR = os.getenv('DATA_DIR', '/app/data')
DB_PATH = os.path.join(DATA_DIR, 'favorites.db')
PORT = int(os.getenv('PORT', 3000))

# Новый URL API Т-Инвестиций (REST/gRPC)
TINKOFF_API_URL = os.getenv("TINKOFF_API_URL", "https://invest-public-api.tbank.ru/rest/")

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

# ---------- ГЛОБАЛЬНЫЙ СЛОВАРЬ ДЛЯ НАЗВАНИЙ ----------
ticker_to_name = {}  # будет заполнен при первом вызове get_all_shares()

# ---------- ИНИЦИАЛИЗАЦИЯ БОТА ----------
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# ---------- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ----------
last_messages = {}
update_tasks = {}
auto_update_enabled = {}
user_state = {}  # chat_id -> {'state': 'add'/'remove', 'prompt_msg_id': int}
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

# ---------- ЗАПРОСЫ К MOEX (с заполнением словаря названий) ----------
async def get_all_shares():
    global ticker_to_name
    global http_session
    url = "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json?iss.meta=off&iss.only=securities"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        async with http_session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                logging.warning(f"MOEX вернул статус {resp.status}, используем только имеющиеся названия")
                return pd.DataFrame()
            json_data = await resp.json()
            if 'securities' not in json_data:
                return pd.DataFrame()
            columns = json_data['securities']['columns']
            data_rows = json_data['securities']['data']
            df = pd.DataFrame(data_rows, columns=columns)
            # Заполняем словарь названий (SECID -> SHORTNAME)
            if 'SECID' in df.columns and 'SHORTNAME' in df.columns:
                for _, row in df.iterrows():
                    ticker_to_name[row['SECID']] = row['SHORTNAME']
            # Возвращаем также данные для рынка (чтобы не ломать существующий код)
            # Для топа дня нужны marketdata, поэтому запрашиваем их отдельно
            return df
    except Exception as e:
        logging.error(f"Ошибка загрузки названий из MOEX: {e}")
        return pd.DataFrame()

# Функция для получения рыночных данных (используется в топе дня)
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
                sec_df = sec_df[['SECID', 'SHORTNAME', 'LISTLEVEL']].copy()
                merged = pd.merge(market_df, sec_df, on='SECID', how='left')
                return merged
        except Exception:
            await asyncio.sleep(2)
    return pd.DataFrame()

async def get_moex_index():
    global http_session
    url = "https://iss.moex.com/iss/engines/stock/markets/index/boards/SNDX/securities/IMOEX.json?iss.meta=off"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    for attempt in range(3):
        try:
            async with http_session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    await asyncio.sleep(2)
                    continue
                json_data = await resp.json()
                columns = json_data['securities']['columns']
                data_rows = json_data['securities']['data']
                if data_rows:
                    last_idx = columns.index('LAST')
                    return float(data_rows[0][last_idx])
        except Exception:
            await asyncio.sleep(2)
    return None

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

# ---------- ФУНКЦИИ ДЛЯ РАБОТЫ С API Т-ИНВЕСТИЦИЙ (REST) ----------
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
        # Загружаем названия из MOEX, если словарь пуст
        if not ticker_to_name:
            await get_all_shares()

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

        # Расчёт затраченной суммы (средняя цена * количество)
        total_cost = 0.0
        total_value = 0.0
        for pos in positions:
            quantity = float(pos.get("quantity", {}).get("units", 0))
            avg_price = float(pos.get("averagePositionPrice", {}).get("units", 0))
            price = float(pos.get("currentPrice", {}).get("units", 0))
            total_cost += quantity * avg_price
            total_value += quantity * price

        # Доходность портфеля
        if total_cost > 0:
            total_yield_pct = (total_value - total_cost) / total_cost * 100
        else:
            total_yield_pct = 0.0

        result = {
            "total_amount": total_value,
            "currency": total_currency,
            "total_cost": total_cost,
            "total_yield_pct": total_yield_pct,
            "positions": [],
            "expected_dividends": float(data.get("expectedDividends", 0))
        }

        type_map = {
            "INSTRUMENT_TYPE_SHARE": "Акции",
            "INSTRUMENT_TYPE_BOND": "Облигации",
            "INSTRUMENT_TYPE_ETF": "Фонды",
            "INSTRUMENT_TYPE_CURRENCY": "Валюта",
        }

        for pos in positions:
            figi = pos.get("figi")
            ticker = pos.get("ticker") or figi
            # Получаем название из словаря, если есть, иначе оставляем тикер
            name = ticker_to_name.get(ticker, ticker)
            instrument_type = pos.get("instrumentType", "unknown")
            quantity = float(pos.get("quantity", {}).get("units", 0))
            price = float(pos.get("currentPrice", {}).get("units", 0))
            avg_price = float(pos.get("averagePositionPrice", {}).get("units", 0))
            expected_yield = float(pos.get("expectedYield", {}).get("units", 0))
            currency = pos.get("currentPrice", {}).get("currency", "RUB")
            current_value = quantity * price
            invested = quantity * avg_price
            dividends = float(pos.get("expectedDividend", 0))
            # Доходность позиции
            if invested > 0:
                pos_yield_pct = (expected_yield / invested) * 100
            else:
                pos_yield_pct = 0.0

            result["positions"].append({
                "figi": figi,
                "ticker": ticker,
                "name": name,
                "instrument_type": instrument_type,
                "type_display": type_map.get(instrument_type, instrument_type),
                "quantity": quantity,
                "price": price,
                "avg_price": avg_price,
                "expected_yield": expected_yield,
                "currency": currency,
                "current_value": current_value,
                "invested": invested,
                "pos_yield_pct": pos_yield_pct,
                "dividends": dividends,
                "share": (current_value / total_value * 100) if total_value > 0 else 0
            })

        return result
    except Exception as e:
        logging.error(f"Ошибка получения портфеля: {e}")
        return None

# ---------- ГЕНЕРАЦИЯ КАРТИНКИ ПОРТФЕЛЯ ----------
def generate_portfolio_image(portfolio_data) -> io.BytesIO:
    if not portfolio_data or not portfolio_data["positions"]:
        return None

    # Группировка по типу (отображаемому)
    groups = defaultdict(list)
    for pos in portfolio_data["positions"]:
        groups[pos["type_display"]].append(pos)

    # Определяем порядок
    order = ["Акции", "Облигации", "Фонды", "Валюта"]
    ordered_groups = []
    for key in order:
        if key in groups:
            ordered_groups.append((key, groups.pop(key)))
    for key, vals in groups.items():
        ordered_groups.append((key, vals))

    # Подсчёт общего количества строк для высоты
    total_rows = sum(len(v) for _, v in ordered_groups) + len(ordered_groups) * 2  # заголовки групп
    height = max(4, total_rows * 0.35 + 2)

    fig, ax = plt.subplots(figsize=(10, height))
    ax.axis('off')

    # Заголовок
    total_amount = portfolio_data["total_amount"]
    total_cost = portfolio_data["total_cost"]
    total_yield = portfolio_data["total_yield_pct"]
    title = f"Портфель\nСумма: {total_amount:.2f} ₽   Вложено: {total_cost:.2f} ₽   Доходность: {total_yield:+.2f}%"
    ax.text(0.5, 0.98, title, fontsize=14, fontweight='bold', ha='center', va='top', transform=ax.transAxes)

    # Подготовка данных для таблицы
    col_labels = ["Название", "Тип", "Кол-во", "Цена", "Доходность", "Вложено", "Доля"]
    table_data = []
    row_colors = []
    for group_name, positions in ordered_groups:
        # Заголовок группы
        table_data.append([f"__{group_name}__", "", "", "", "", "", ""])
        row_colors.append('#e0e0e0')  # серый фон для заголовка
        for pos in positions:
            # Формируем строку
            if pos["name"] and pos["name"] != pos["ticker"]:
                display_name = f"{pos['name'][:25]}"
            else:
                display_name = pos["ticker"]
            # Округление
            price_str = f"{pos['price']:.2f}"
            invested_str = f"{pos['invested']:.2f}"
            yield_str = f"{pos['pos_yield_pct']:+.2f}%"
            share_str = f"{pos['share']:.1f}%"
            table_data.append([
                display_name,
                pos["type_display"][:3],  # сокращённый тип
                f"{pos['quantity']:.0f}",
                price_str,
                yield_str,
                invested_str,
                share_str
            ])
            # Цвет фона в зависимости от доходности
            if pos['pos_yield_pct'] > 0:
                row_colors.append('#d4edda')  # светло-зелёный
            else:
                row_colors.append('#f8d7da')  # светло-красный

    # Создаём таблицу
    table = ax.table(cellText=table_data, colLabels=col_labels, loc='center', cellLoc='center',
                     colColours=['#f0f0f0']*len(col_labels), rowColours=row_colors)
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1, 1.5)

    # Выделяем заголовки групп жирным шрифтом
    row_idx = 0
    for i, row_text in enumerate(table_data):
        if row_text[0].startswith("__") and row_text[0].endswith("__"):
            for j in range(len(col_labels)):
                cell = table[(i+1, j)]
                cell.set_text_props(fontweight='bold', fontsize=10)
                cell.set_facecolor('#e0e0e0')
            # Очищаем текст от маркеров
            cell = table[(i+1, 0)]
            cell.get_text().set_text(row_text[0][2:-2])
        else:
            # Цвет текста в зависимости от доходности
            yield_text = row_text[4]  # столбец "Доходность"
            if yield_text.startswith('+'):
                # зелёный
                for j in range(len(col_labels)):
                    table[(i+1, j)].set_text_props(color='#006600')
            elif yield_text.startswith('-'):
                for j in range(len(col_labels)):
                    table[(i+1, j)].set_text_props(color='#990000')

    fig.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', dpi=120)
    buf.seek(0)
    plt.close()
    return buf

# ---------- ОСТАЛЬНЫЕ ФУНКЦИИ (без изменений) ----------
# get_top_movers, calc_period_change, get_historical_close, get_favorites_data, build_table_universal, format_message, format_historical_table, generate_favorites_image, send_top, auto_update_task

# (остальные функции остаются без изменений – они уже есть в предыдущем коде)

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

    # ---- КОМАНДЫ (перехватываем вручную) ----
    if text == "/portfolio":
        logging.info("🔍 Обработка команды /portfolio")
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

    # ---- КНОПКИ ----
    if text == "📌 Топ дня":
        shares_df = await get_market_data()
        gainers, losers = get_top_movers(shares_df, top_n=TOP_N)
        if gainers.empty and losers.empty:
            session_status = get_session_status()
            await message.answer(f"📊 {session_status}\nДанные обновятся в рабочее время.")
            await safe_delete_message(message.chat.id, message.message_id)
            return
        index_val = await get_moex_index()
        session_status = get_session_status()
        update_time = get_local_time().strftime("%d/%m/%y %H:%M:%S")
        text = format_message(gainers, losers, index_val, update_time, session_status)
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

    if text == "📈 Портфель":
        logging.info("🔍 Обработка кнопки портфеля")
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

    # ---- ВВОД ТИКЕРА (состояние) ----
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

    # ---- ФОЛБЭК ----
    logging.info(f"FALLBACK: получено сообщение: '{text}'")
    await message.answer("Используйте кнопки меню.", reply_markup=main_keyboard())
    await safe_delete_message(message.chat.id, message.message_id)

# ---------- ЗАПУСК (POLLING + HEALTH-SERVER) ----------
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

    # Загружаем названия из MOEX при старте
    await get_all_shares()
    logging.info("✅ Словарь названий загружен")

    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("✅ Вебхук удалён")

    try:
        await bot.send_message(MY_CHAT_ID, "🚀 Бот перезапущен и готов к работе!")
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
