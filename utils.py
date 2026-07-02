import datetime
import logging
import asyncio
from functools import wraps
import pandas as pd
from tabulate import tabulate
from config import NAME_OVERRIDES, DOMAIN
import db

def retry(max_attempts=3, delay=2, backoff=2, exceptions=(Exception,)):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    if attempt == max_attempts:
                        logging.error(f"{func.__name__} failed after {max_attempts} attempts: {e}")
                        raise
                    wait = delay * (backoff ** (attempt - 1))
                    logging.warning(f"Retry {attempt}/{max_attempts} for {func.__name__} after {wait}s: {e}")
                    await asyncio.sleep(wait)
            return None
        return wrapper
    return decorator

def get_moscow_time():
    return datetime.datetime.now(datetime.timezone.utc).astimezone(
        datetime.timezone(datetime.timedelta(hours=3))
    )

def get_local_time():
    return datetime.datetime.now(datetime.timezone.utc).astimezone(
        datetime.timezone(datetime.timedelta(hours=4))
    )

def is_weekend():
    now = get_moscow_time()
    return now.weekday() in (5, 6)

def get_session_status(no_trading_weekends=None, time_offset=0):
    now_moscow = get_moscow_time()
    today_str = now_moscow.strftime("%Y-%m-%d")
    weekend = now_moscow.weekday() in (5, 6)

    if weekend and no_trading_weekends:
        for start, end in no_trading_weekends:
            if start <= today_str <= end:
                return "Биржа закрыта (выходной)"

    if weekend:
        if (now_moscow.hour > 9 or (now_moscow.hour == 9 and now_moscow.minute >= 50)) and (
            now_moscow.hour < 19 or (now_moscow.hour == 19 and now_moscow.minute == 0)
        ):
            return "Сессия выходного дня"
        else:
            return "Биржа закрыта"

    if now_moscow.hour < 6 or (now_moscow.hour == 6 and now_moscow.minute < 50):
        return "Биржа закрыта"
    elif now_moscow.hour == 6 and now_moscow.minute >= 50:
        session = "Утренняя сессия"
        times = ("06:50", "09:50")
    elif now_moscow.hour < 9 or (now_moscow.hour == 9 and now_moscow.minute < 50):
        session = "Утренняя сессия"
        times = ("06:50", "09:50")
    elif now_moscow.hour == 9 and now_moscow.minute >= 50:
        session = "Основная сессия"
        times = ("09:50", "19:00")
    elif now_moscow.hour < 19:
        session = "Основная сессия"
        times = ("09:50", "19:00")
    elif now_moscow.hour == 19 and now_moscow.minute == 0:
        session = "Вечерняя сессия"
        times = ("19:00", "23:50")
    elif now_moscow.hour < 23 or (now_moscow.hour == 23 and now_moscow.minute <= 50):
        session = "Вечерняя сессия"
        times = ("19:00", "23:50")
    else:
        return "Биржа закрыта"

    if time_offset != 0:
        def shift_time(t):
            h, m = map(int, t.split(':'))
            h += time_offset
            if h >= 24:
                h -= 24
            return f"{h:02d}:{m:02d}"
        times = (shift_time(times[0]), shift_time(times[1]))

    return f"{session} ({times[0]}–{times[1]})"

def get_week_number(date):
    return date.isocalendar()[1]

def get_month_name_ru(month_num):
    months = {
        1: "Января", 2: "Февраля", 3: "Марта", 4: "Апреля",
        5: "Мая", 6: "Июня", 7: "Июля", 8: "Августа",
        9: "Сентября", 10: "Октября", 11: "Ноября", 12: "Декабря"
    }
    return months.get(month_num, str(month_num))

def smart_price(price):
    if price is None or (isinstance(price, float) and pd.isna(price)):
        return "—"
    if abs(price) < 0.01:
        return f"{price:.6f}"
    elif abs(price) < 1:
        return f"{price:.4f}"
    elif abs(price) < 10:
        return f"{price:.3f}"
    else:
        return f"{price:.2f}"

def build_table_universal(df, title, headers, data_columns):
    if df.empty:
        return ""
    table_data = []
    for _, row in df.iterrows():
        secid = row.get('SECID', '')
        row_data = []
        for col in data_columns:
            val = row.get(col, "")
            if col == 'SHORTNAME':
                original = str(val)
                clean_val = original
                if clean_val.endswith(' ао') or clean_val.endswith(' ап'):
                    clean_val = clean_val[:-3]
                if clean_val.startswith('i') and len(clean_val) > 1 and clean_val[1].isalpha():
                    clean_val = clean_val[1:]
                display = NAME_OVERRIDES.get(secid, clean_val)
                if display == clean_val:
                    display = NAME_OVERRIDES.get(clean_val, clean_val)
                if len(display) > 25:
                    display = display[:22] + "…"
                val = display
            elif col == 'LAST' and isinstance(val, (int, float)):
                val = smart_price(val)
            elif col == 'LAST':
                if isinstance(val, str):
                    try:
                        val = float(val)
                    except ValueError:
                        pass
                if isinstance(val, (int, float)):
                    val = smart_price(val)
            elif col in ('CHANGEPERCENT', 'CHANGE_PCT') and isinstance(val, (int, float)):
                val = f"{val:+.2f}%"
            row_data.append(val)
        table_data.append(row_data)
    table = tabulate(table_data, headers=headers, tablefmt="simple", numalign="right", stralign="left")
    return f"<b>{title}</b>\n<pre>{table}</pre>\n"

def last_trading_day(today):
    if today.month == 12:
        last_day = datetime.date(today.year + 1, 1, 1) - datetime.timedelta(days=1)
    else:
        last_day = datetime.date(today.year, today.month + 1, 1) - datetime.timedelta(days=1)
    while last_day.weekday() >= 5:
        last_day -= datetime.timedelta(days=1)
    return last_day

async def get_portfolio_change_str():
    today = datetime.date.today().isoformat()
    snapshot = await db.get_daily_snapshot(today)
    current = await db.get_portfolio_value()
    if snapshot is None or current is None or snapshot == 0:
        return ""
    change = (current - snapshot) / snapshot * 100
    return f"💼 Портфель: {change:+.2f}% за день\n"
