import os
import logging
import time
import asyncio
import sqlite3
from io import BytesIO
import re

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, InputFile
import aiohttp
import pandas as pd
import matplotlib.pyplot as plt
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

# ---------- РАБОТА С БАЗОЙ ДАННЫХ (SQLite) ----------
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

def escape_markdown(text: str) -> str:
    """Экранирует специальные символы для MarkdownV2."""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(r'([{}])'.format(re.escape(escape_chars)), r'\\\1', str(text))

# ---------- ФУНКЦИИ ДЛЯ РАБОТЫ С MOEX ----------
async def get_all_shares():
    """
    Получает рыночные данные (marketdata) и справочную информацию (securities),
    включая уровень листинга (LISTLEVEL).
    """
    async with aiohttp.ClientSession() as session:
        url = "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json?iss.meta=off&iss.only=marketdata,securities"
        try:
            async with session.get(url) as resp:
                json_data = await resp.json()
                if 'marketdata' not in json_data or 'securities' not in json_data:
                    logging.error("Нет данных от MOEX")
                    return pd.DataFrame()
                md_columns = json_data['marketdata']['columns']
                md_rows = json_data['marketdata']['data']
                market_df = pd.DataFrame(md_rows, columns=md_columns)
                sec_columns = json_data['securities']['columns']
                sec_rows = json_data['securities']['data']
                sec_df = pd.DataFrame(sec_rows, columns=sec_columns)
                # Берём только нужные колонки из securities
                sec_df = sec_df[['SECID', 'SECNAME', 'LISTLEVEL']]
                merged = pd.merge(market_df, sec_df, on='SECID', how='left')
                logging.info(f"Загружено {len(merged)} строк")
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
    """
    Возвращает два DataFrame: лидеры роста и падения.
    Если exclude_level3=True, исключает акции 3-го эшелона.
    """
    if data.empty:
        return pd.DataFrame(), pd.DataFrame()
    
    # Исключаем 3-й эшелон
    if exclude_level3 and 'LISTLEVEL' in data.columns:
        data = data[data['LISTLEVEL'] < 3]
        logging.info(f"После исключения 3-го эшелона осталось {len(data)} строк")
    
    # Проверка наличия колонок для расчёта
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
                data['SECNAME'] = data['SECID']  # fallback
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

# ---------- ФОРМАТИРОВАНИЕ СООБЩЕНИЙ ----------
def format_message(gainers: pd.DataFrame, losers: pd.DataFrame, index_value, update_time: str) -> str:
    if index_value is not None:
        header = f"📊 *Индекс МосБиржи* {escape_markdown(f'{index_value:.2f}')}\n"
    else:
        header = "📊 *Индекс МосБиржи* временно недоступен\n"
    header += f"🕒 Обновлено: {escape_markdown(update_time)}\n\n"

    def build_table(df, title):
        if df.empty:
            return ""
        rows = []
        for _, row in df.iterrows():
            ticker = escape_markdown(row['SECID'])
            name = row.get('SECNAME', row['SECID'])
            # Обрезаем и экранируем
            if len(name) > 25:
                name = name[:22] + "…"
            name = escape_markdown(name)
            price = f"{row['LAST']:.2f}" if isinstance(row['LAST'], (int, float)) else str(row['LAST'])
            price = escape_markdown(price)
            change = row['CHANGEPERCENT']
            sign = "▲" if change > 0 else "▼"
            # Изменение НЕ экранируем, потому что мы сами ставим звёздочки
            change_str = f"*{sign} {change:.2f}%*"
            rows.append([ticker, name, price, change_str])
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
# ---------- ГЕНЕРАЦИЯ КАРТИНКИ (Идея 3) ----------
def create_table_image(df: pd.DataFrame, title: str) -> BytesIO:
    """
    Создаёт изображение таблицы с цветовой индикацией (зелёный/красный).
    """
    if df.empty:
        return None
    # Подготовка данных
    data = df[['SECID', 'SECNAME', 'LAST', 'CHANGEPERCENT']].head(TOP_N).copy()
    data['CHANGEPERCENT'] = data['CHANGEPERCENT'].apply(lambda x: f"{x:+.2f}%")
    data['LAST'] = data['LAST'].apply(lambda x: f"{x:.2f}")
    data.columns = ['Тикер', 'Название', 'Цена', 'Изменение']
    # Цвета строк
    colors = []
    for val in df['CHANGEPERCENT'].head(TOP_N):
        if val > 0:
            colors.append('lightgreen')
        elif val < 0:
            colors.append('lightcoral')
        else:
            colors.append('white')
    
    fig, ax = plt.subplots(figsize=(8, len(data)*0.5 + 1))
    ax.axis('off')
    table = ax.table(cellText=data.values, colLabels=data.columns, loc='center', cellLoc='center', colColours=['#f0f0f0']*4)
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1.2, 1.5)
    for i, color in enumerate(colors):
        for j in range(4):
            table[(i+1, j)].set_facecolor(color)
    ax.set_title(title, fontsize=14, fontweight='bold', pad=20)
    buf = BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', pad_inches=0.2)
    buf.seek(0)
    plt.close()
    return buf

# ---------- ОБРАБОТЧИКИ КОМАНД ----------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Я бот для отслеживания топ-акций Мосбиржи.\n\n"
        "📌 Доступные команды:\n"
        "/top — показать лидеров роста и падения\n"
        "/top_image — показать лидеров в виде картинки\n"
        "/add TICKER — добавить акцию в избранное\n"
        "/remove TICKER — удалить из избранного\n"
        "/favorites — показать избранные акции"
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

@dp.message(Command("top_image"))
async def cmd_top_image(message: types.Message):
    await message.answer("⏳ Генерирую картинку...")
    try:
        shares_df = await get_all_shares()
        gainers, losers = get_top_movers(shares_df, top_n=TOP_N)
        if gainers.empty and losers.empty:
            await message.answer("Нет данных для отображения.")
            return
        img_gainers = create_table_image(gainers, "📈 Лидеры роста")
        if img_gainers:
            await message.answer_photo(photo=InputFile(img_gainers, filename="gainers.png"))
        img_losers = create_table_image(losers, "📉 Лидеры падения")
        if img_losers:
            await message.answer_photo(photo=InputFile(img_losers, filename="losers.png"))
    except Exception as e:
        logging.error(f"Ошибка в /top_image: {e}", exc_info=True)
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
        await message.answer("Не удалось получить данные с биржи.")
        return
    fav_df = shares_df[shares_df['SECID'].isin(favs)]
    if fav_df.empty:
        await message.answer("По вашему списку нет актуальных данных.")
        return
    if 'CHANGEPERCENT' in fav_df.columns:
        fav_df = fav_df.sort_values('CHANGEPERCENT', ascending=False)
    else:
        # пытаемся вычислить
        if 'OPEN' in fav_df.columns and 'LAST' in fav_df.columns:
            fav_df['CHANGEPERCENT'] = ((fav_df['LAST'] - fav_df['OPEN']) / fav_df['OPEN']) * 100
            fav_df = fav_df.sort_values('CHANGEPERCENT', ascending=False)
        else:
            await message.answer("Недостаточно данных для сортировки.")
            return
    text = "⭐ Ваши избранные акции:\n\n"
    for _, row in fav_df.iterrows():
        name = row.get('SECNAME', row['SECID'])
        price = row['LAST']
        change = row['CHANGEPERCENT']
        sign = "▲" if change > 0 else "▼"
        text += f"• {row['SECID']} ({name}) — {price:.2f}  {sign} {change:+.2f}%\n"
    await message.answer(text)

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
    # Инициализация БД
    init_db()
    # Удаляем вебхук (на случай, если он остался)
    await bot.delete_webhook(drop_pending_updates=True)   # добавили drop_pending_updates
    await asyncio.sleep(0.5)
    # Запускаем polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
