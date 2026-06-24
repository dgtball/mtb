import asyncio
import datetime
import logging
import pandas as pd
from aiogram import Bot
from utils import get_moscow_time, get_local_time, get_session_status
from handlers import format_message, format_historical_table, get_portfolio_change_str
from moex_api import get_market_data, get_moex_index_info, get_top_movers, get_historical_shares, calc_period_change
from config import MY_CHAT_ID, TOP_N

_bot = None
_http_session = None
_active_day_message_id = None

def set_bot(bot: Bot):
    global _bot
    _bot = bot

def set_http_session(session):
    global _http_session
    _http_session = session

async def send_weekly_top():
    try:
        now = get_moscow_time()
        start = now - datetime.timedelta(days=now.weekday())
        from_date = start
        from_date_str = start.strftime("%Y-%m-%d")
        till_date = now
        till_date_str = now.strftime("%Y-%m-%d")
        df = await get_historical_shares(_http_session, from_date_str, till_date_str)
        if df.empty:
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
        if gainers.empty and losers.empty:
            return
        text = format_historical_table(gainers, losers, 'week', from_date, till_date)
        await _bot.send_message(MY_CHAT_ID, text, parse_mode="HTML")
        logging.info("Еженедельный топ недели отправлен")
    except Exception as e:
        logging.error(f"Ошибка отправки еженедельного топа: {e}")

async def send_monthly_top():
    try:
        now = get_moscow_time()
        start = now.replace(day=1)
        from_date = start
        from_date_str = start.strftime("%Y-%m-%d")
        till_date = now
        till_date_str = now.strftime("%Y-%m-%d")
        df = await get_historical_shares(_http_session, from_date_str, till_date_str)
        if df.empty:
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
        if gainers.empty and losers.empty:
            return
        text = format_historical_table(gainers, losers, 'month', from_date, till_date)
        await _bot.send_message(MY_CHAT_ID, text, parse_mode="HTML")
        logging.info("Ежемесячный топ месяца отправлен")
    except Exception as e:
        logging.error(f"Ошибка отправки ежемесячного топа: {e}")

def last_trading_day(today):
    if today.month == 12:
        last_day = datetime.date(today.year+1, 1, 1) - datetime.timedelta(days=1)
    else:
        last_day = datetime.date(today.year, today.month+1, 1) - datetime.timedelta(days=1)
    while last_day.weekday() >= 5:
        last_day -= datetime.timedelta(days=1)
    return last_day

async def scheduler_loop():
    global _active_day_message_id
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

            # Окно 06:50 – 00:50 МСК
            start_minutes = 6*60 + 50
            end_minutes = 0*60 + 50 + 24*60
            current_minutes = hour*60 + minute
            if current_minutes < start_minutes:
                current_minutes += 24*60
            day_window = start_minutes <= current_minutes <= end_minutes

            if day_window:
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
                        # Сохраняем снэпшот портфеля
                        from tinkoff_api import get_portfolio_summary
                        data = await get_portfolio_summary(_http_session)
                        if data:
                            import db
                            db.set_portfolio_value(data['total_amount'])
                            today_str = today.isoformat()
                            if db.get_daily_snapshot(today_str) is None:
                                db.set_daily_snapshot(today_str, data['total_amount'])
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
                if _active_day_message_id is not None:
                    try:
                        await _bot.delete_message(MY_CHAT_ID, _active_day_message_id)
                    except Exception:
                        pass
                    _active_day_message_id = None

            # Пятница после 23:50
            if weekday == 4 and hour == 23 and minute >= 50:
                if last_week_sent_date != today:
                    await send_weekly_top()
                    last_week_sent_date = today

            # Последний торговый день месяца после 23:50
            last_trade_day = last_trading_day(today)
            if today == last_trade_day and hour == 23 and minute >= 50:
                if last_month_sent_date != today:
                    await send_monthly_top()
                    last_month_sent_date = today

            await asyncio.sleep(5)

        except Exception as e:
            logging.error(f"Ошибка в scheduler_loop: {e}")
            await asyncio.sleep(5)