import logging
import asyncio
import datetime
import pandas as pd
from aiogram import types, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import MY_CHAT_ID, TOP_N
from utils import (
    get_moscow_time, get_local_time, is_weekend, get_session_status,
    get_week_number, get_month_name_ru, build_table_universal
)
from db import add_favorite, remove_favorite, get_favorites
from moex_api import (
    get_market_data, get_historical_shares, get_historical_close,
    get_moex_index_info, get_top_movers, calc_period_change,
    get_historical_prices_batch   # <-- новая функция
)
from tinkoff_api import get_portfolio_summary
from visualization import generate_portfolio_image, generate_favorites_image
from keyboards import main_keyboard

# ---------- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ----------
last_messages = {}
update_tasks = {}
auto_update_enabled = {}

_http_session = None
_bot = None

def set_http_session(session):
    global _http_session
    _http_session = session

def set_bot(bot_instance):
    global _bot
    _bot = bot_instance

# ---------- FSM: Состояния добавления/удаления ----------
class AddRemoveStates(StatesGroup):
    waiting_for_add = State()
    waiting_for_remove = State()
    waiting_for_rename = State()
    waiting_for_unrename = State()

# ---------- УДАЛЕНИЕ СООБЩЕНИЙ ----------
async def safe_delete_message(chat_id: int, message_id: int):
    try:
        if _bot is None:
            logging.error("Bot instance not set, cannot delete message")
            return
        await _bot.delete_message(chat_id, message_id)
    except Exception as e:
        logging.warning(f"Не удалось удалить сообщение {message_id}: {e}")

# ---------- ФОРМАТИРОВАНИЕ ----------
def format_message(gainers, losers, index_info, update_time, session_status):
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
    if not gainers.empty:
        text += build_table_universal(gainers, "📈 Лидеры роста", ["Тикер", "Название", "Цена", "Изменение"], ['SECID', 'SHORTNAME', 'LAST', 'CHANGEPERCENT'])
    if not losers.empty:
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
    if not gainers.empty:
        text += build_table_universal(gainers, "📈 Рост", ["Тикер", "Название", "Изменение"], ['SECID', 'SHORTNAME', 'CHANGE_PCT'])
    if not losers.empty:
        text += build_table_universal(losers, "📉 Падение", ["Тикер", "Название", "Изменение"], ['SECID', 'SHORTNAME', 'CHANGE_PCT'])
    return text

# ---------- ПОЛУЧЕНИЕ ДАННЫХ ИЗБРАННОГО ----------
async def get_favorites_data(chat_id: int):
    favs = get_favorites(chat_id)
    if not favs:
        return None, "⭐ У вас пока нет избранных акций.\n\nДобавьте их через кнопку ✅ Добавить тикер."
    shares_df = await get_market_data(_http_session)
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

    # Пакетная загрузка исторических цен для всех тикеров
    tickers = fav_df['SECID'].tolist()
    target_dates = [week_reference, month_reference]
    prices = await get_historical_prices_batch(_http_session, tickers, target_dates)

    week_changes = []
    month_changes = []
    for _, row in fav_df.iterrows():
        ticker = row['SECID']
        current_price = row['LAST']
        week_price = prices.get((ticker, week_reference))
        month_price = prices.get((ticker, month_reference))

        if week_price is not None and week_price > 0:
            week_change = ((current_price - week_price) / week_price) * 100
        else:
            week_change = None
        if month_price is not None and month_price > 0:
            month_change = ((current_price - month_price) / month_price) * 100
        else:
            month_change = None
        week_changes.append(week_change)
        month_changes.append(month_change)

    fav_df['change_week'] = week_changes
    fav_df['change_month'] = month_changes
    fav_df = fav_df.sort_values('CHANGEPERCENT', ascending=False)
    return fav_df, None

# ---------- ОТПРАВКА ТОПА ----------
async def send_top(message: types.Message, period: str = 'day'):
    loading_msg = await message.answer("⏳ Загружаю данные...")
    try:
        if period == 'day':
            shares_df = await get_market_data(_http_session)
            gainers, losers = get_top_movers(shares_df, top_n=TOP_N)
            if gainers.empty and losers.empty:
                await loading_msg.delete()
                session_status = get_session_status()
                await message.answer(f"📌 {session_status}\nДанные обновятся в рабочее время.")
                return
            index_info = await get_moex_index_info(_http_session)
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
            df = await get_historical_shares(_http_session, from_date_str, till_date_str)
            if df.empty:
                await loading_msg.delete()
                await message.answer(f"Нет данных за {period}.")
                return
            changes = calc_period_change(df)
            shares_all = await get_market_data(_http_session)
            if not shares_all.empty:
                # Оставляем только акции (SECTYPE 1 или 2) и высокий уровень листинга
                mask = (shares_all['LISTLEVEL'] < 3) & (shares_all['SECTYPE'].isin(['1', '2']))
                allowed_tickers = shares_all[mask]['SECID'].unique()
                changes = changes[changes['SECID'].isin(allowed_tickers)]
                names = shares_all[mask][['SECID', 'SHORTNAME']].drop_duplicates('SECID')
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
            if _bot is None:
                logging.error("Bot instance not set, cannot auto-update")
                break
            shares_df = await get_market_data(_http_session)
            gainers, losers = get_top_movers(shares_df, top_n=TOP_N)
            if gainers.empty and losers.empty:
                continue
            index_info = await get_moex_index_info(_http_session)
            session_status = get_session_status()
            update_time = get_local_time().strftime("%d/%m/%y %H:%M:%S")
            text = format_message(gainers, losers, index_info, update_time, session_status)
            await _bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Ошибка автообновления для чата {chat_id}: {e}")
            break

# ---------- ОБРАБОТЧИКИ ----------
async def cmd_start(message: types.Message, state: FSMContext):
    if message.from_user.id != MY_CHAT_ID:
        await message.answer("⛔ Доступ запрещён.")
        return
    chat_id = message.chat.id
    auto_update_enabled[chat_id] = True
    await state.clear()
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

async def handle_buttons_and_commands(message: types.Message, state: FSMContext):
    if message.from_user.id != MY_CHAT_ID:
        await message.answer("⛔ Доступ запрещён.")
        return
    text = message.text
    logging.info(f"🔄 Обработка сообщения: '{text}'")

    # ---------- ПОРТФЕЛЬ ----------
    if text == "/portfolio" or text == "📈 Портфель":
        logging.info("🔍 Обработка портфеля")
        if not _http_session:
            await message.answer("❌ Нет активной сессии.")
            await safe_delete_message(message.chat.id, message.message_id)
            return
        loading_msg = await message.answer("⏳ Загружаю данные портфеля...")
        try:
            data = await get_portfolio_summary(_http_session)
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
            from aiogram.types import BufferedInputFile
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

    # ---------- ТОП ДНЯ ----------
    if text == "📌 Топ дня":
        shares_df = await get_market_data(_http_session)
        gainers, losers = get_top_movers(shares_df, top_n=TOP_N)
        if gainers.empty and losers.empty:
            session_status = get_session_status()
            await message.answer(f"📌 {session_status}\nДанные обновятся в рабочее время.")
            await safe_delete_message(message.chat.id, message.message_id)
            return
        index_info = await get_moex_index_info(_http_session)
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

    # ---------- ТОП НЕДЕЛИ / МЕСЯЦА ----------
    if text == "📊 Топ недели":
        await send_top(message, 'week')
        await safe_delete_message(message.chat.id, message.message_id)
        return

    if text == "🗓️ Топ месяца":
        await send_top(message, 'month')
        await safe_delete_message(message.chat.id, message.message_id)
        return

    # ---------- ИЗБРАННЫЕ ----------
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
            from aiogram.types import BufferedInputFile
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

    # ---------- ДОБАВИТЬ ТИКЕР (FSM) ----------
    if text == "✅ Добавить тикер":
        await state.set_state(AddRemoveStates.waiting_for_add)
        prompt_msg = await message.answer("Введите тикер для добавления (например, SBER или SBER, GAZP):")
        await state.update_data(prompt_msg_id=prompt_msg.message_id)
        await safe_delete_message(message.chat.id, message.message_id)
        return

    # ---------- УДАЛИТЬ ТИКЕР (FSM) ----------
    if text == "❌ Удалить тикер":
        await state.set_state(AddRemoveStates.waiting_for_remove)
        prompt_msg = await message.answer("Введите тикер для удаления (например, SBER или SBER, GAZP):")
        await state.update_data(prompt_msg_id=prompt_msg.message_id)
        await safe_delete_message(message.chat.id, message.message_id)
        return
        
    # ---------- ПЕРЕИМЕНОВАТЬ ТИКЕР ----------
    if text == "✏️ Переименовать тикер":
        await state.set_state(AddRemoveStates.waiting_for_rename)
        prompt_msg = await message.answer(
            "Введите тикер и новое название через пробел (например, SBER Сбер):"
        )
        await state.update_data(prompt_msg_id=prompt_msg.message_id)
        await safe_delete_message(message.chat.id, message.message_id)
        return

    # ---------- УДАЛИТЬ ПЕРЕИМЕНОВАНИЕ ----------
    if text == "🗑 Удалить переименование":
        await state.set_state(AddRemoveStates.waiting_for_unrename)
        prompt_msg = await message.answer(
            "Введите тикер, для которого нужно удалить переименование (например, SBER):"
        )
        await state.update_data(prompt_msg_id=prompt_msg.message_id)
        await safe_delete_message(message.chat.id, message.message_id)
        return

    # ---------- ПОКАЗАТЬ ВСЕ ПЕРЕИМЕНОВАНИЯ ----------
    if text == "📋 Все переименования":
        from config import NAME_OVERRIDES
        if not NAME_OVERRIDES:
            await message.answer("Переопределений названий пока нет.")
        else:
            lines = [f"{t} → {n}" for t, n in NAME_OVERRIDES.items()]
            await message.answer("Текущие переименования:\n" + "\n".join(lines))
        await safe_delete_message(message.chat.id, message.message_id)
        return

    # ---------- ОБРАБОТКА ТЕКСТА В ЗАВИСИМОСТИ ОТ СОСТОЯНИЯ ----------
    current_state = await state.get_state()
    if current_state == AddRemoveStates.waiting_for_add.state:
        data = await state.get_data()
        prompt_msg_id = data.get('prompt_msg_id')
        if prompt_msg_id:
            await safe_delete_message(message.chat.id, prompt_msg_id)
        await safe_delete_message(message.chat.id, message.message_id)

        raw = message.text.strip()
        tickers = [t.strip().upper() for t in raw.split(',') if t.strip()]
        results = []
        for ticker in tickers:
            if add_favorite(message.chat.id, ticker):
                results.append(f"✅ {ticker} добавлен")
            else:
                results.append(f"ℹ️ {ticker} уже есть")
        await message.answer("\n".join(results) if results else "Ничего не сделано.")
        await state.clear()
        return

    if current_state == AddRemoveStates.waiting_for_rename.state:
        data = await state.get_data()
        prompt_msg_id = data.get('prompt_msg_id')
        if prompt_msg_id:
            await safe_delete_message(message.chat.id, prompt_msg_id)
        await safe_delete_message(message.chat.id, message.message_id)

        parts = message.text.strip().split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Нужно указать тикер и название через пробел. Например: SBER Сбер")
            await state.clear()
            return
        ticker, new_name = parts[0].upper(), parts[1].strip()
        db.set_name_override(ticker, new_name)
        await message.answer(f"✅ Тикер {ticker} теперь будет отображаться как «{new_name}»")
        await state.clear()
        return

    if current_state == AddRemoveStates.waiting_for_unrename.state:
        data = await state.get_data()
        prompt_msg_id = data.get('prompt_msg_id')
        if prompt_msg_id:
            await safe_delete_message(message.chat.id, prompt_msg_id)
        await safe_delete_message(message.chat.id, message.message_id)

        ticker = message.text.strip().upper()
        db.remove_name_override(ticker)
        await message.answer(f"✅ Переименование для {ticker} удалено (если было)")
        await state.clear()
        return                                                                                                                                                                                                      

    # Если сообщение не попало ни в одно состояние и не является командой
    logging.info(f"FALLBACK: получено сообщение: '{text}'")
    await message.answer("Используйте кнопки меню.", reply_markup=main_keyboard())
    await safe_delete_message(message.chat.id, message.message_id)

def register_handlers(dp):
    dp.message.register(cmd_start, Command("start"))
    dp.message.register(handle_buttons_and_commands)