import datetime
import pandas as pd
from tabulate import tabulate

# ---------- ВРЕМЯ ----------
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

# ---------- СТАТУС СЕССИИ ----------
def get_session_status(no_trading_weekends=None):
    now = get_moscow_time()
    today_str = now.strftime("%Y-%m-%d")
    weekend = now.weekday() in (5, 6)

    if weekend and no_trading_weekends:
        for start, end in no_trading_weekends:
            if start <= today_str <= end:
                return "Биржа закрыта (выходной)"

    if weekend:
        if (now.hour > 9 or (now.hour == 9 and now.minute >= 50)) and (
            now.hour < 19 or (now.hour == 19 and now.minute == 0)
        ):
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

# ---------- УМНОЕ ФОРМАТИРОВАНИЕ ЦЕНЫ ----------
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

# ---------- ПОСТРОЕНИЕ ТАБЛИЦЫ ----------
def build_table_universal(df, title, headers, data_columns):
    if df.empty:
        return ""
    table_data = []
    for _, row in df.iterrows():
        row_data = []
        for col in data_columns:
            val = row.get(col, "")
            if col == 'SHORTNAME' and len(str(val)) > 25:
                val = str(val)[:22] + "…"
            elif col == 'LAST' and isinstance(val, (int, float)):
                val = smart_price(val)
            elif col in ('CHANGEPERCENT', 'CHANGE_PCT') and isinstance(val, (int, float)):
                val = f"{val:+.2f}%"
            row_data.append(val)
        table_data.append(row_data)
    table = tabulate(table_data, headers=headers, tablefmt="simple", numalign="right", stralign="left")
    return f"<b>{title}</b>\n<pre>{table}</pre>\n"
