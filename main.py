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
import matplotlib
matplotlib.use('Agg')
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
                sec_df = sec_df[['SECID', 'SECNAME', 'LISTLEVEL']]
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
    required = ['SECID', 'CHANGEPERCENT', 'LAST', 'SECNAME']
    for col in required:
        if col not in data.columns:
            if col == 'SECNAME':
                data['SECNAME'] = data['SECID']
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

def format_historical(gainers, losers, period_name, from_date, till_date):
    text = f"📊 Топ за {period_name}\n📅 Период: {from_date} – {till_date}\n\n"
    if not gainers.empty:
        text += "📈 Рост:\n"
        for _, row in gainers.iterrows():
            text += f"• {row['SECID']}: {row['CHANGE_PCT']:.2f}%\n"
    if not losers.empty:
        text += "📉 Падение:\n"
        for _, row in losers.iterrows():
            text += f"• {row['SECID']}: {row['CHANGE_PCT']:.2f}%\n"
    return text

# ---------- ФОРМАТИРОВАНИЕ ТАБЛИЦЫ С МОНОШИРИННЫМ БЛОКОМ ----------
def format_message(gainers: pd.DataFrame, losers: pd.DataFrame, index_value, update_time: str, is_weekend: bool = False) -> str:
    if index_value is not None:
        header = f"📊 Индекс МосБиржи: {index_value:.2f}\n"
    else:
        if is_weekend:
            header = "📊 Сессия выходного дня\n"
        else:
            header = "📊 Биржа закрыта\n"
    header += f"🕒 Обновлено: {update_time}\n\n"

    def build_table(df, title):
        if df.empty:
            return ""
        table_data = []
        for _, row in df.iterrows():
            ticker = row['SECID']
            name = row.get('SECNAME', ticker)
            if len(name) > 25:
                name = name[:22] + "…"
            price = f"{row['LAST']:.2f}" if isinstance(row['LAST'], (int, float)) else str(row['LAST'])
            change = row['CHANGEPERCENT']
            sign = "▲" if change > 0 else "▼"
            change_str = f"{sign} {change:.2f}%"
            table_data.append([ticker, name, price, change_str])
        headers = ["Тикер", "Название", "Цена", "Изменение"]
        table = tabulate(table_data, headers=headers, tablefmt="grid", numalign="right", stralign="left")
        # Оборачиваем в моноширинный блок
        return f"{title}\n```\n{table}\n```\n"

    text = header
    text += build_table(gainers, "📈 Лидеры роста")
    text += build_table(losers, "📉 Лидеры падения")
    return text

# ---------- ГЕНЕРАЦИЯ КАРТИНКИ ----------
def create_table_image(df: pd.DataFrame, title: str) -> BytesIO:
    if df.empty:
        return None
    data = df[['SECID', 'SECNAME', 'LAST', 'CHANGEPERCENT']].head(TOP_N).copy()
    data['CHANGEPERCENT'] = data['CHANGEPERCENT'].apply(lambda x: f"{x:+.2f}%")
    data['LAST'] = data['LAST'].apply(lambda x: f"{x:.2f}")
    data.columns = ['Тикер', 'Название', 'Цена', 'Изменение']
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
        "/top — показать лидеров роста и падения (текущий день)\n"
        "/week — топ за неделю\n"
        "/month — топ за месяц\n"
        "/top_image — показать лидеров в виде картинки\n"
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
        await message.answer(text, reply_markup=keyboard)
        await loading_msg.delete()
    except Exception as e:
        await loading_msg.delete()
        logging.error(f"Ошибка в /top: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("top_image"))
async def cmd_top_image(message: types.Message):
    loading_msg = await message.answer("⏳ Генерирую картинку...")
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
        img_gainers = create_table_image(gainers, "📈 Лидеры роста")
        if img_gainers:
            await message.answer_photo(photo=InputFile(img_gainers, filename="gainers.png"))
        img_losers = create_table_image(losers, "📉 Лидеры падения")
        if img_losers:
            await message.answer_photo(photo=InputFile(img_losers, filename="losers.png"))
        await loading_msg.delete()
    except Exception as e:
        await loading_msg.delete()
        logging.error(f"Ошибка в /top_image: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("week"))
async def cmd_week(message: types.Message):
    loading_msg = await message.answer("⏳ Загружаю данные за неделю...")
    try:
        now = get_moscow_time()
        from_date = (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
        till_date = now.strftime("%Y-%m-%d")
        df = await get_historical_shares(from_date, till_date)
        if df.empty:
            await loading_msg.delete()
            await message.answer("Нет данных за неделю.")
            return
        changes = calc_period_change(df)
        gainers = changes.nlargest(TOP_N, 'CHANGE_PCT')
        losers = changes.nsmallest(TOP_N, 'CHANGE_PCT')
        text = format_historical(gainers, losers, "неделю", from_date, till_date)
        await message.answer(text)
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
        from_date = (now - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
        till_date = now.strftime("%Y-%m-%d")
        df = await get_historical_shares(from_date, till_date)
        if df.empty:
            await loading_msg.delete()
            await message.answer("Нет данных за месяц.")
            return
        changes = calc_period_change(df)
        gainers = changes.nlargest(TOP_N, 'CHANGE_PCT')
        losers = changes.nsmallest(TOP_N, 'CHANGE_PCT')
        text = format_historical(gainers, losers, "месяц", from_date, till_date)
        await message.answer(text)
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
        await callback.message.edit_text(text, reply_markup=keyboard)
    except Exception as e:
        logging.error(f"Ошибка обновления: {e}")
        await callback.message.answer(f"❌ Ошибка обновления: {e}")

# ---------- ЗАПУСК С УЛУЧШЕННЫМ СБРОСОМ ----------
async def main():
    init_db()
    # Принудительно удаляем вебхук и сбрасываем ожидающие обновления
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logging.info("Webhook удалён")
    except Exception as e:
        logging.warning(f"Не удалось удалить вебхук: {e}")
    
    # Закрываем старую сессию и создаём новую, чтобы избежать конфликтов
    try:
        await bot.session.close()
    except:
        pass
    bot.session = aiohttp.ClientSession()
    
    # Небольшая пауза, чтобы Telegram успел обработать удаление
    await asyncio.sleep(1)
    
    logging.info("Запускаем polling...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
