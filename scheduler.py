import asyncio
import datetime
import logging
import pandas as pd
import db
from aiogram import Bot

from handlers import format_message, format_historical_table, get_portfolio_change_str
from moex_api import get_market_data, get_moex_index_info, get_top_movers, get_historical_shares, calc_period_change
from utils import get_moscow_time, get_local_time, get_session_status, last_trading_day
from config import MY_CHAT_ID, TOP_N, TINKOFF_TOKEN
from services.tops import get_top_data

_bot = None
_http_session = None
_active_day_message_id = None
_snapshot_saved_for_date = None

# Новый флаг: разрешено ли автообновление портфеля (только днём)
portfolio_update_allowed = False

def set_bot(bot: Bot):
    global _bot
    _bot = bot

def set_http_session(session):
    global _http_session
    _http_session = session

def is_portfolio_update_allowed():
    global portfolio_update_allowed
    return portfolio_update_allowed

# ---------- ФУНКЦИИ ОТПРАВКИ ТОПОВ НЕДЕЛИ И МЕСЯЦА ----------
async def send_periodic_top(period: str):
    """period: 'week' или 'month'"""
    try:
        now = get_moscow_time()
        gainers, losers, _, _, _, _ = await get_top_data(period, _http_session)
        if gainers.empty and losers.empty:
            return
        # Формируем заголовок
        if period == 'week':
            start = now - datetime.timedelta(days=now.weekday())
            title = f"📅 Топ за неделю #{start.isocalendar()[1]}"
            period_str = f"Период: {start.strftime('%d/%m/%y')} – {now.strftime('%d/%m/%y')}"
        else:
            start = now.replace(day=1)
            month_name = ['Января','Февраля','Марта','Апреля','Мая','Июня','Июля','Августа','Сентября','Октября','Ноября','Декабря'][start.month-1]
            title = f"🗓️ Топ {month_name}"
            period_str = f"Период: {start.strftime('%d/%m/%y')} – {now.strftime('%d/%m/%y')}"
        text = f"{title}\n{period_str}\n\n"
        if not gainers.empty:
            text += build_table_universal(gainers, "📈 Рост", ["Тикер", "Название", "Изменение"], ['SECID', 'SHORTNAME', 'CHANGE_PCT'])
        if not losers.empty:
            text += build_table_universal(losers, "📉 Падение", ["Тикер", "Название", "Изменение"], ['SECID', 'SHORTNAME', 'CHANGE_PCT'])
        await _bot.send_message(MY_CHAT_ID, text, parse_mode="HTML")
        logging.info(f"Еженедельный/ежемесячный топ ({period}) отправлен")
    except Exception as e:
        logging.error(f"Ошибка отправки топа {period}: {e}")

# ---------- ФУНКЦИЯ ОБНОВЛЕНИЯ КЭША ИНСТРУМЕНТОВ ----------
async def refresh_instruments_cache():
    logging.info("🔄 Обновление справочника инструментов из MOEX")
    try:
        from moex_api import load_instrument_names
        await load_instrument_names(_http_session, force=True)
        logging.info("✅ Справочник инструментов обновлён")
    except Exception as e:
        logging.error(f"❌ Ошибка обновления справочника: {e}")

# ---------- ОСНОВНОЙ ЦИКЛ ПЛАНИРОВЩИКА ----------
async def scheduler_loop():
    global _active_day_message_id, portfolio_update_allowed, _snapshot_saved_for_date
    last_week_sent_date = None
    last_month_sent_date = None
    day_update_interval = 30

    await asyncio.sleep(5)

    while True:
        try:
            now = get_moscow_time()
            today = now.date()
            weekday = now.weekday()
            hour = now.hour
            minute = now.minute

            # Вечерний снэпшот портфеля (после 23:50) – только один раз за день
            if hour == 23 and minute >= 50:
                tomorrow = today + datetime.timedelta(days=1)
                tomorrow_str = tomorrow.isoformat()
                if _snapshot_saved_for_date != tomorrow_str:
                    current = db.get_portfolio_value()
                    if current is not None:
                        db.set_daily_snapshot(tomorrow_str, current)
                        _snapshot_saved_for_date = tomorrow_str
                        logging.info(f"Снэпшот портфеля сохранён на {tomorrow_str}: {current:.2f}")
                # Не спать, продолжить цикл – но можно сделать небольшой sleep, чтобы не проверять каждые 5 секунд
                await asyncio.sleep(60)  # после 23:50 проверяем раз в минуту
                continue

            # Окно 06:50 – 00:50 МСК
            start_minutes = 6*60 + 50
            end_minutes = 0*60 + 50 + 24*60
            current_minutes = hour*60 + minute
            if current_minutes < start_minutes:
                current_minutes += 24*60
            day_window = start_minutes <= current_minutes <= end_minutes

            if day_window:
                portfolio_update_allowed = True
                if _active_day_message_id is None:
                    shares_df = await get_market_data(_http_session)
                    gainers, losers = get_top_movers(shares_df, top_n=TOP_N)
                    if not gainers.empty or not losers.empty:
                        index_info = await get_moex_index_info(_http_session)
                        session_status = get_session_status(time_offset=1)
                        update_time = get_local_time().strftime("%d/%m/%y %H:%M:%S")
                        portfolio_line = get_portfolio_change_str()
                        text = format_message(gainers, losers, index_info, update_time, session_status, portfolio_line)
                        sent_msg = await _bot.send_message(MY_CHAT_ID, text, parse_mode="HTML")
                        _active_day_message_id = sent_msg.message_id
                        # Страховка: если снэпшот за сегодня отсутствует, создаём сейчас
                        today_str = today.isoformat()
                        if db.get_daily_snapshot(today_str) is None:
                            try:
                                from tinkoff_api import get_portfolio_summary
                                data = await get_portfolio_summary(_http_session)
                                if data:
                                    total = data['total_amount']
                                    db.set_daily_snapshot(today_str, total)
                                    db.set_portfolio_value(total)
                                    logging.info(f"Снэпшот портфеля восстановлен за {today_str}: {total:.2f}")
                            except Exception as e:
                                logging.error(f"Не удалось создать страховочный снэпшот: {e}")
                    await asyncio.sleep(day_update_interval)
                    continue
                else:
                    shares_df = await get_market_data(_http_session)
                    gainers, losers = get_top_movers(shares_df, top_n=TOP_N)
                    if not gainers.empty or not losers.empty:
                        index_info = await get_moex_index_info(_http_session)
                        session_status = get_session_status(time_offset=1)
                        update_time = get_local_time().strftime("%d/%m/%y %H:%M:%S")
                        portfolio_line = get_portfolio_change_str()
                        text = format_message(gainers, losers, index_info, update_time, session_status, portfolio_line)
                        try:
                            await _bot.edit_message_text(text, chat_id=MY_CHAT_ID, message_id=_active_day_message_id, parse_mode="HTML")
                        except Exception as e:
                            logging.error(f"Ошибка редактирования сообщения дня: {e}")
                            _active_day_message_id = None
                    await asyncio.sleep(day_update_interval)
                    continue
            else:
                portfolio_update_allowed = False
                _active_day_message_id = None # сбрасываем, чтобы утром отправить новый топ, но старый не трогаем

            # Ежедневная синхронизация операций в 10:00
            if hour == 10 and minute == 0:
                if TINKOFF_TOKEN:
                    from tinkoff_api import sync_operations
                    asyncio.create_task(sync_operations(_http_session))

            # Ежедневное обновление кэша инструментов в 03:00
            if hour == 3 and minute == 0:
                asyncio.create_task(refresh_instruments_cache())

            # Пятница после 23:50
            if weekday == 4 and hour == 23 and minute >= 50:
                if last_week_sent_date != today:
                    await send_periodic_top('week')
                    last_week_sent_date = today

            # Последний торговый день месяца после 23:50
            last_trade_day = last_trading_day(today)
            if today == last_trade_day and hour == 23 and minute >= 51:
                if last_month_sent_date != today:
                    await send_periodic_top('month')
                    last_month_sent_date = today

            await asyncio.sleep(5)

        except Exception as e:
            logging.error(f"Ошибка в scheduler_loop: {e}")
            await asyncio.sleep(5)