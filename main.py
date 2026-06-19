import os
import logging
import time
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import aiohttp
import pandas as pd
import aiomoex
from tabulate import tabulate

# ---------- КОНФИГУРАЦИЯ ----------
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

TOP_N = 10

# ---------- ЛОГИРОВАНИЕ ----------
logging.basicConfig(level=logging.INFO)

# ---------- ИНИЦИАЛИЗАЦИЯ БОТА ----------
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# ---------- ФУНКЦИИ ДЛЯ РАБОТЫ С MOEX ----------
async def get_all_shares():
    """
    Получает данные с Московской биржи.
    Объединяет marketdata (цены) и securities (справочную информацию).
    """
    async with aiohttp.ClientSession() as session:
        url = "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json?iss.meta=off&iss.only=marketdata,securities"
        try:
            async with session.get(url) as resp:
                json_data = await resp.json()
                
                # Получаем рыночные данные
                if 'marketdata' not in json_data:
                    logging.error("Нет marketdata")
                    return pd.DataFrame()
                md_columns = json_data['marketdata']['columns']
                md_rows = json_data['marketdata']['data']
                market_df = pd.DataFrame(md_rows, columns=md_columns)
                
                # Получаем справочные данные (названия)
                if 'securities' not in json_data:
                    logging.error("Нет securities")
                    return pd.DataFrame()
                sec_columns = json_data['securities']['columns']
                sec_rows = json_data['securities']['data']
                sec_df = pd.DataFrame(sec_rows, columns=sec_columns)
                
                # Выбираем нужные колонки из securities: SECID и SECNAME (полное имя)
                sec_df = sec_df[['SECID', 'SECNAME']]
                
                # Объединяем с marketdata по SECID
                merged_df = pd.merge(market_df, sec_df, on='SECID', how='left')
                
                logging.info(f"Загружено {len(merged_df)} строк, колонки: {merged_df.columns.tolist()}")
                return merged_df
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

def get_top_movers(data: pd.DataFrame, top_n: int = TOP_N):
    if data.empty:
        return pd.DataFrame(), pd.DataFrame()
    if 'CHANGEPERCENT' not in data.columns:
        if 'OPEN' in data.columns and 'LAST' in data.columns:
            data['CHANGEPERCENT'] = ((data['LAST'] - data['OPEN']) / data['OPEN']) * 100
        else:
            logging.error("Нет данных для расчёта изменения")
            return pd.DataFrame(), pd.DataFrame()
    required = ['SECID', 'CHANGEPERCENT', 'LAST', 'SECNAME']
    for col in required:
        if col not in data.columns:
            if col == 'SECNAME':
                data['SECNAME'] = data['SECID']  # fallback, если нет названия
            else:
                logging.error(f"Отсутствует колонка {col}")
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

def format_message(gainers: pd.DataFrame, losers: pd.DataFrame, index_value, update_time: str) -> str:
    """
    Формирует сообщение в виде Markdown-таблицы (без моноширинного блока).
    """
    # Шапка
    if index_value is not None:
        header = f"📊 *Индекс МосБиржи* {index_value:.2f}\n"
    else:
        header = "📊 *Индекс МосБиржи* временно недоступен\n"
    header += f"🕒 Обновлено: {update_time}\n\n"

    # Функция для создания таблицы
    def build_table(df, title):
        if df.empty:
            return ""
        # Подготавливаем строки
        rows = []
        for _, row in df.iterrows():
            ticker = row['SECID']
            name = row.get('SECNAME', ticker)
            # Обрезаем слишком длинные названия (до 25 символов)
            if len(name) > 25:
                name = name[:22] + "…"
            price = f"{row['LAST']:.2f}" if isinstance(row['LAST'], (int, float)) else str(row['LAST'])
            change = row['CHANGEPERCENT']
            sign = "▲" if change > 0 else "▼"
            # Выделяем изменение жирным шрифтом
            change_str = f"*{sign} {change:.2f}%*"
            rows.append([ticker, name, price, change_str])
        
        # Заголовки таблицы
        table = f"### {title}\n"
        table += "| Тикер | Название | Цена | Изменение |\n"
        table += "|-------|----------|-----:|-----------|\n"
        for row in rows:
            table += f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} |\n"
        return table + "\n"

    text = header
    text += build_table(gainers, "📈 Лидеры роста")
    text += build_table(losers, "📉 Лидеры падения")
    return text

# ---------- ОБРАБОТЧИКИ КОМАНД ----------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Я бот для отслеживания топ-акций Мосбиржи.\n\n"
        "📌 Используй команду /top — я покажу лидеров роста и падения.\n"
        "🔄 После вывода данных появится кнопка 'Обновить'."
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
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh")]
            ]
        )
        await message.answer(text, parse_mode="MarkdownV2", reply_markup=keyboard)
    except Exception as e:
        logging.error(f"Ошибка в /top: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")

@dp.callback_query(lambda c: c.data == "refresh")
async def process_refresh(callback: CallbackQuery):
    await callback.answer("Обновляю данные...")
    try:
        shares_df = await get_all_shares()
        gainers, losers = get_top_movers(shares_df, top_n=TOP_N)
        if gainers.empty and losers.empty:
            await callback.message.answer("⚠️ Не удалось получить данные. Проверьте логи.")
            return
        index_val = await get_moex_index()
        update_time = time.strftime("%Y-%m-%d %H:%M:%S")
        text = format_message(gainers, losers, index_val, update_time)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh")]
            ]
        )
        await callback.message.edit_text(text, parse_mode="MarkdownV2", reply_markup=keyboard)
    except Exception as e:
        logging.error(f"Ошибка обновления: {e}")
        await callback.message.answer(f"❌ Ошибка обновления: {e}")

# ---------- ЗАПУСК ПОЛЛИНГА ----------
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
