import logging
import asyncio
import datetime
import pandas as pd
from aiogram import types, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from config import MY_CHAT_ID, TOP_N
from utils import (
    get_moscow_time, get_local_time, is_weekend, get_session_status,
    get_week_number, get_month_name_ru, build_table_universal
)
import db
from moex_api import (
    get_market_data, get_historical_shares, get_historical_close,
    get_moex_index_info, get_top_movers, calc_period_change
)
from keyboards import main_keyboard

# ---------- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ----------
_http_session = None
_bot = None

def set_http_session(session):
    global _http_session
    _http_session = session

def set_bot(bot_instance):
    global _bot
    _bot = bot_instance

# ---------- ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ПОРТФЕЛЯ ----------
def get_portfolio_change_str():
    today = datetime.date.today().isoformat()
    snapshot = db.get_daily_snapshot(today)
    current = db.get_portfolio_value()
    if snapshot is None or current is None or snapshot == 0:
        return ""
    change = (current - snapshot) / snapshot * 100
    return f"💼 Портфель: {change:+.2f}% за день\n"

# ---------- ФОРМАТИРОВАНИЕ ----------
def format_message(gainers, losers, index_info, update_time, session_status, portfolio_change_str=""):
    header = ""
    if index_info and 'last' in index_info:
        last = index_info['last']
        change = index_info.get('change_percent', 0)
        header += f"📈 Индекс МосБиржи: {last:.2f} ({change:+.2f}%)\n"
    if portfolio_change_str:
        header += portfolio_change_str
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

# ---------- ОТПРАВКА ТОПА (РАЗОВАЯ) ----------
async def send_top(message: types.Message, period: str = 'day'):
    loading_msg = await message.answer("⏳ Загружаю данные...")
    try:
        if period == 'day':
            shares_df = await get_market_data(_http_session)
            gainers, losers = get_top_movers(shares_df, top_n=TOP_N)
            if gainers.empty and losers.empty:
                await loading_msg.delete()
                session_status = get_session_status(time_offset=1)
                await message.answer(f"📌 {session_status}\nДанные обновятся в рабочее время.")
                return
            index_info = await get_moex_index_info(_http_session)
            session_status = get_session_status(time_offset=1)
            update_time = get_local_time().strftime("%d/%m/%y %H:%M:%S")
            portfolio_line = get_portfolio_change_str()
            text = format_message(gainers, losers, index_info, update_time, session_status, portfolio_line)
            await message.answer(text, parse_mode="HTML")
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
                mask = (shares_all['LISTLEVEL'] < 3) & (shares_all['SECTYPE'].isin(['1', '2']))
                allowed_tickers = shares_all[mask]['SECID'].unique()
                changes = changes[changes['SECID'].isin(allowed_tickers)]
                names = shares_all[mask][['SECID', 'SHORTNAME']].drop_duplicates('SECID')
                changes = changes.merge(names, on='SECID', how='left')
            positive = changes[changes['CHANGE_PCT'] > 0]
            negative = changes[changes['CHANGE_PCT'] < 0]
            gainers = positive.nlargest(TOP_N, 'CHANGE_PCT') if not positive.empty else pd.DataFrame()
            losers = negative.nsmallest(TOP_N, 'CHANGE_PCT') if not negative.empty else pd.DataFrame()
            text = format_historical_table(gainers, losers, period_name_short, from_date, till_date)
            await message.answer(text, parse_mode="HTML")
        await loading_msg.delete()
    except Exception as e:
        await loading_msg.delete()
        logging.error(f"❌ Ошибка в send_top (period={period}): {e}", exc_info=True)
        await message.answer(f"❌ Ошибка при загрузке данных: {e}")

# ---------- ОБРАБОТЧИКИ ----------
async def cmd_start(message: types.Message, state: FSMContext):
    if message.from_user.id != MY_CHAT_ID:
        await message.answer("⛔ Доступ запрещён.")
        await _bot.send_message(MY_CHAT_ID, f"⚠️ Попытка доступа от {message.from_user.full_name} (@{message.from_user.username})")
        return
    await state.clear()
    try:
        await message.answer(
            "👋 Привет! Используй кнопки для навигации",
            reply_markup=main_keyboard()
        )
    except Exception as e:
        logging.error(f"❌ Ошибка в /start: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка при запуске: {e}")

async def handle_buttons_and_commands(message: types.Message, state: FSMContext):
    if message.from_user.id != MY_CHAT_ID:
        await message.answer("⛔ Доступ запрещён.")
        await _bot.send_message(MY_CHAT_ID, f"⚠️ Попытка доступа от {message.from_user.full_name} (@{message.from_user.username})")
        return
    text = message.text
    logging.info(f"🔄 Обработка сообщения: '{text}'")

    # ---------- ТОП НЕДЕЛИ ----------
    if text == "📊 Топ недели":
        await send_top(message, 'week')
        return

    # ---------- ТОП МЕСЯЦА ----------
    if text == "🗓️ Топ месяца":
        await send_top(message, 'month')
        return

    logging.info(f"FALLBACK: получено сообщение: '{text}'")
    await message.answer("Используйте кнопки меню.", reply_markup=main_keyboard())

def register_handlers(dp):
    dp.message.register(cmd_start, Command("start"))
    dp.message.register(handle_buttons_and_commands)