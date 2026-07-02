import asyncio
import datetime
import logging
from aiogram import Bot
import db

from handlers import format_message, format_historical_table
from moex_api import get_market_data, get_moex_index_info, get_top_movers
from utils import get_moscow_time, get_local_time, get_session_status, last_trading_day, get_portfolio_change_str, build_table_universal
from config import MY_CHAT_ID, TOP_N, TINKOFF_TOKEN
from services.tops import get_top_data

_bot = None
_http_session = None
_active_day_message_id = None
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

async def send_periodic_top(period: str):
    try:
        now = get_moscow_time()
        gainers, losers, _, _, _, _ = await get_top_data(period, _http_session)
        if gainers.empty and losers.empty:
            return
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

async def refresh_instruments_cache():
    logging.info("Обновление справочника инструментов из MOEX")
    try:
        from moex_api import load_instrument_names
        await load_instrument_names(_http_session, force=True)
        logging.info("Справочник инструментов обновлён")
    except Exception as e:
        logging.error(f"Ошибка обновления справочника: {e}")

async def refresh_dividend_calendar():
    logging.info("Обновление календаря дивидендов")
    try:
        from tinkoff_api import fetch_all_dividends
        data = await fetch_all_dividends(_http_session)
        for ticker, info in data.items():
            for div in info["dividends"]:
                await db.upsert_dividend_calendar(ticker, info["figi"], div)
        logging.info(f"Календарь дивидендов обновлён для {len(data)} инструментов")
    except Exception as e:
        logging.error(f"Ошибка обновления календаря дивидендов: {e}")

async def refresh_coupon_calendar():
    logging.info("Обновление календаря купонов")
    try:
        from tinkoff_api import fetch_all_coupons
        data = await fetch_all_coupons(_http_session)
        for ticker, info in data.items():
            for coupon in info["coupons"]:
                await db.upsert_coupon_calendar(ticker, info["figi"], coupon)
        logging.info(f"Календарь купонов обновлён для {len(data)} облигаций")
    except Exception as e:
        logging.error(f"Ошибка обновления календаря купонов: {e}")

async def refresh_forecasts():
    logging.info("Обновление прогнозов дивидендов")
    try:
        from services.forecast import calculate_and_update_forecasts
        await calculate_and_update_forecasts(_http_session)
        logging.info("Прогнозы дивидендов обновлены")
    except Exception as e:
        logging.error(f"Ошибка обновления прогнозов: {e}")

def seconds_until(hour: int, minute: int = 0) -> float:
    now = get_moscow_time()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return (target - now).total_seconds()

def is_in_day_window(now) -> bool:
    minutes = now.hour * 60 + now.minute
    return 6 * 60 + 50 <= minutes <= 24 * 60 + 50

async def day_window_loop():
    global _active_day_message_id, portfolio_update_allowed
    day_update_interval = 30

    while True:
        window_open = 6 * 60 + 50
        now = get_moscow_time()
        current_minutes = now.hour * 60 + now.minute

        if current_minutes < window_open:
            delay = (window_open - current_minutes) * 60 - now.second
            logging.debug(f"До окна торгов {delay:.0f}с")
            await asyncio.sleep(delay)
            continue

        if current_minutes > 24 * 60 + 50:
            delay = (24 * 60 + 60 + window_open - current_minutes) * 60 - now.second
            logging.debug(f"Окно закрыто, до следующего {delay:.0f}с")
            await asyncio.sleep(delay)
            continue

        portfolio_update_allowed = True
        today = now.date()
        shares_df = await get_market_data(_http_session)
        gainers, losers = get_top_movers(shares_df, top_n=TOP_N)

        if not gainers.empty or not losers.empty:
            index_info = await get_moex_index_info(_http_session)
            session_status = get_session_status(time_offset=1)
            update_time = get_local_time().strftime("%d/%m/%y %H:%M:%S")
            portfolio_line = await get_portfolio_change_str()
            text = format_message(gainers, losers, index_info, update_time, session_status, portfolio_line)
            sent_msg = await _bot.send_message(MY_CHAT_ID, text, parse_mode="HTML")
            _active_day_message_id = sent_msg.message_id

            today_str = today.isoformat()
            if await db.get_daily_snapshot(today_str) is None:
                try:
                    from tinkoff_api import get_portfolio_summary
                    data = await get_portfolio_summary(_http_session)
                    if data:
                        total = data['total_amount']
                        await db.set_daily_snapshot(today_str, total)
                        await db.set_portfolio_value(total)
                        logging.info(f"Снэпшот портфеля восстановлен за {today_str}: {total:.2f}")
                except Exception as e:
                    logging.error(f"Не удалось создать страховочный снэпшот: {e}")

        while True:
            await asyncio.sleep(day_update_interval)
            now = get_moscow_time()
            if not is_in_day_window(now):
                portfolio_update_allowed = False
                _active_day_message_id = None
                break
            shares_df = await get_market_data(_http_session)
            gainers, losers = get_top_movers(shares_df, top_n=TOP_N)
            if not gainers.empty or not losers.empty:
                index_info = await get_moex_index_info(_http_session)
                session_status = get_session_status(time_offset=1)
                update_time = get_local_time().strftime("%d/%m/%y %H:%M:%S")
                portfolio_line = await get_portfolio_change_str()
                text = format_message(gainers, losers, index_info, update_time, session_status, portfolio_line)
                try:
                    await _bot.edit_message_text(text, chat_id=MY_CHAT_ID, message_id=_active_day_message_id, parse_mode="HTML")
                except Exception as e:
                    logging.error(f"Ошибка редактирования сообщения дня: {e}")
                    _active_day_message_id = None

async def snapshot_loop():
    while True:
        delay = seconds_until(23, 50)
        await asyncio.sleep(delay)
        tomorrow = (get_moscow_time() + datetime.timedelta(days=1)).date().isoformat()
        current = await db.get_portfolio_value()
        if current is not None:
            from moex_api import get_moex_index
            imoex = await get_moex_index(state.bot_session)
            await db.upsert_daily_snapshot(tomorrow, portfolio_value=current, imoex_value=imoex)
            logging.info(f"Снэпшот сохранён на {tomorrow}: портфель={current:.2f}, IMOEX={imoex}")

async def daily_task_loop(name: str, hour: int, minute: int, coro_func, *args):
    while True:
        delay = seconds_until(hour, minute)
        await asyncio.sleep(delay)
        logging.info(f"Запуск ежедневной задачи: {name}")
        try:
            await coro_func(*args)
        except Exception as e:
            logging.error(f"Ошибка в задаче {name}: {e}")

async def weekly_task_loop(weekday: int, hour: int, minute: int, task_coro, *args):
    while True:
        now = get_moscow_time()
        days_ahead = (weekday - now.weekday()) % 7
        if days_ahead == 0 and (now.hour > hour or (now.hour == hour and now.minute >= minute)):
            days_ahead = 7
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + datetime.timedelta(days=days_ahead)
        delay = (target - now).total_seconds()
        await asyncio.sleep(delay)
        logging.info(f"Запуск еженедельной задачи в {target}")
        try:
            await task_coro(*args)
        except Exception as e:
            logging.error(f"Ошибка в еженедельной задаче: {e}")

async def monthly_task_loop(hour: int, minute: int, task_coro, *args):
    while True:
        now = get_moscow_time()
        today = now.date()
        last = last_trading_day(today)
        if today < last or (today == last and (now.hour < hour or (now.hour == hour and now.minute < minute))):
            target = datetime.datetime.combine(last, datetime.time(hour, minute), tzinfo=now.tzinfo)
            delay = (target - now).total_seconds()
        else:
            next_month = today.replace(day=28) + datetime.timedelta(days=4)
            first_of_next = next_month.replace(day=1)
            last = last_trading_day(first_of_next)
            target = datetime.datetime.combine(last, datetime.time(hour, minute), tzinfo=now.tzinfo)
            delay = (target - now).total_seconds()
        await asyncio.sleep(delay)
        logging.info(f"Запуск ежемесячной задачи")
        try:
            await task_coro(*args)
        except Exception as e:
            logging.error(f"Ошибка в ежемесячной задаче: {e}")

async def scheduler_loop():
    tasks = [
        asyncio.create_task(day_window_loop()),
        asyncio.create_task(snapshot_loop()),
    ]
    if TINKOFF_TOKEN:
        from tinkoff_api import sync_operations
        tasks.append(asyncio.create_task(daily_task_loop("sync_operations", 10, 0, sync_operations, _http_session)))
        tasks.append(asyncio.create_task(daily_task_loop("dividend_calendar", 2, 0, refresh_dividend_calendar)))
        tasks.append(asyncio.create_task(daily_task_loop("coupon_calendar", 2, 30, refresh_coupon_calendar)))
        tasks.append(asyncio.create_task(daily_task_loop("instruments_cache", 3, 0, refresh_instruments_cache)))
        tasks.append(asyncio.create_task(daily_task_loop("forecasts", 4, 0, refresh_forecasts)))
        tasks.append(asyncio.create_task(weekly_task_loop(4, 23, 50, send_periodic_top, 'week')))
        tasks.append(asyncio.create_task(monthly_task_loop(23, 51, send_periodic_top, 'month')))

    logging.info(f"Планировщик запущен: {len(tasks)} задач")
    await asyncio.gather(*tasks)
