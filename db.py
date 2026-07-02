import aiosqlite
import logging
from config import DB_PATH, NAME_OVERRIDES, ticker_to_name

_db: aiosqlite.Connection | None = None

async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA foreign_keys=ON")
    return _db

async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None

async def init_db():
    db = await get_db()
    await db.executescript('''
        CREATE TABLE IF NOT EXISTS name_overrides
            (ticker TEXT PRIMARY KEY, display_name TEXT);
        CREATE TABLE IF NOT EXISTS portfolio_state
            (key TEXT PRIMARY KEY, value REAL);
        CREATE TABLE IF NOT EXISTS sectors
            (ticker TEXT PRIMARY KEY, sector_name TEXT);
        CREATE TABLE IF NOT EXISTS operations
            (id TEXT PRIMARY KEY, date TEXT NOT NULL,
             type TEXT NOT NULL, ticker TEXT, figi TEXT,
             instrument_type TEXT, quantity INTEGER, payment REAL,
             currency TEXT, commission REAL, name TEXT);
        CREATE INDEX IF NOT EXISTS idx_operations_date ON operations(date);
        CREATE INDEX IF NOT EXISTS idx_operations_type ON operations(type);
        CREATE TABLE IF NOT EXISTS instruments
            (ticker TEXT PRIMARY KEY, name TEXT, sector TEXT,
             figi TEXT, instrument_type TEXT, updated_at TIMESTAMP,
             maturity_date TEXT, coupon_period INTEGER);
        CREATE INDEX IF NOT EXISTS idx_instruments_figi ON instruments(figi);
        CREATE TABLE IF NOT EXISTS dividend_calendar (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, figi TEXT NOT NULL,
            declared_date TEXT, record_date TEXT,
            payment_date TEXT, dividend_net REAL, dividend_type TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(ticker, declared_date, payment_date));
        CREATE INDEX IF NOT EXISTS idx_dividend_calendar_ticker ON dividend_calendar(ticker);
        CREATE INDEX IF NOT EXISTS idx_dividend_calendar_payment_date ON dividend_calendar(payment_date);
        CREATE TABLE IF NOT EXISTS coupon_calendar (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, figi TEXT NOT NULL,
            coupon_date TEXT, coupon_value REAL,
            coupon_currency TEXT, record_date TEXT,
            is_redemption BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(ticker, coupon_date));
        CREATE INDEX IF NOT EXISTS idx_coupon_calendar_ticker ON coupon_calendar(ticker);
        CREATE INDEX IF NOT EXISTS idx_coupon_calendar_coupon_date ON coupon_calendar(coupon_date);
        CREATE TABLE IF NOT EXISTS dividend_forecast (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            forecast_amount REAL, forecast_month INTEGER,
            forecast_year INTEGER, confidence_score REAL DEFAULT 1.0,
            method TEXT DEFAULT 'historical_cagr',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(ticker, forecast_year, forecast_month));
        CREATE INDEX IF NOT EXISTS idx_forecast_ticker ON dividend_forecast(ticker);
        CREATE INDEX IF NOT EXISTS idx_forecast_year ON dividend_forecast(forecast_year);
    ''')

    for col in ('maturity_date', 'coupon_period'):
        cursor = await db.execute("PRAGMA table_info(instruments)")
        columns = [row[1] for row in await cursor.fetchall()]
        if col not in columns:
            await db.execute(f"ALTER TABLE instruments ADD COLUMN {col} TEXT")

    cursor = await db.execute("PRAGMA table_info(coupon_calendar)")
    columns = [row[1] for row in await cursor.fetchall()]
    if 'is_redemption' not in columns:
        await db.execute("ALTER TABLE coupon_calendar ADD COLUMN is_redemption BOOLEAN DEFAULT 0")

    cursor = await db.execute("PRAGMA table_info(dividend_forecast)")
    columns = [row[1] for row in await cursor.fetchall()]
    if columns and columns[0] == 'ticker':
        await db.execute("DROP TABLE dividend_forecast")
        await db.execute("""CREATE TABLE IF NOT EXISTS dividend_forecast (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            forecast_amount REAL, forecast_month INTEGER,
            forecast_year INTEGER, confidence_score REAL DEFAULT 1.0,
            method TEXT DEFAULT 'historical_cagr',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(ticker, forecast_year, forecast_month))""")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_forecast_ticker ON dividend_forecast(ticker)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_forecast_year ON dividend_forecast(forecast_year)")
        logging.info("Таблица dividend_forecast пересоздана (новая схема с id)")

    await db.commit()
    logging.info(f"База данных инициализирована: {DB_PATH}")
    await seed_overrides()
    await seed_sectors()
    await migrate_sectors_to_instruments()

async def seed_overrides():
    initial = [
        ("WUSH", "ВУШ"),
        ("DELI", "Делимобиль"),
    ]
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) FROM name_overrides")
    count = (await cursor.fetchone())[0]
    if count == 0:
        await db.executemany("INSERT OR IGNORE INTO name_overrides (ticker, display_name) VALUES (?, ?)", initial)
    else:
        for ticker, display_name in initial:
            await db.execute("INSERT OR IGNORE INTO name_overrides (ticker, display_name) VALUES (?, ?)", (ticker, display_name))
    await db.commit()
    logging.info("Начальные переопределения названий добавлены в БД")

async def seed_sectors():
    initial_sectors = {
        "SBER": "Финансы", "ASTR": "ИТ", "CHMF": "Металл",
        "DELI": "Транспорт", "FIXR": "Товары", "FLOT": "Транспорт",
        "GAZP": "Нефтегаз", "HNFG": "Товары", "LKOH": "Нефтегаз",
        "MGNT": "Товары", "MTLR": "Металл", "NVTK": "Нефтегаз",
        "ROSN": "Нефтегаз", "RTKM": "ИТ", "SMLT": "Стройка",
        "SOFL": "ИТ", "WUSH": "Транспорт", "TATNP": "Нефтегаз",
        "TRNFP": "Нефтегаз", "MDMG": "Медицина", "T": "Финансы",
        "VKCO": "ИТ", "YDEX": "ИТ", "SU26233RMFS5": "Облигации",
        "SU26238RMFS4": "Облигации", "SU26240RMFS0": "Облигации",
        "SU26245RMFS9": "Облигации", "SU26246RMFS7": "Облигации",
        "SU26247RMFS5": "Облигации", "SU26248RMFS3": "Облигации",
        "RU000A106UW3": "Облигации", "LQDT": "Фонд", "TDIV": "Фонд",
        "TGLD": "Фонд", "TGLD@": "Фонд", "VTBR": "Финансы",
        "X5": "Товары", "TPAY": "Фонд", "SU26254RMFS1": "Облигации",
        "NLMK": "Металл", "MGKL": "Финансы", "SIBN": "Нефтегаз",
        "GLRX": "Стройка", "AFLT": "Транспорт",
    }
    db = await get_db()
    for ticker, sector in initial_sectors.items():
        await db.execute("INSERT OR REPLACE INTO sectors (ticker, sector_name) VALUES (?, ?)", (ticker, sector))
    await db.commit()
    logging.info(f"Сектора обновлены ({len(initial_sectors)} шт.)")

async def migrate_sectors_to_instruments():
    db = await get_db()
    cursor = await db.execute("SELECT ticker, sector_name FROM sectors")
    rows = await cursor.fetchall()
    for ticker, sector in rows:
        await db.execute("INSERT OR IGNORE INTO instruments (ticker, sector, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)", (ticker, sector))
    await db.commit()
    logging.info(f"Перенесено {len(rows)} секторов в instruments")

async def insert_operation(op):
    db = await get_db()
    await db.execute('''INSERT OR REPLACE INTO operations
        (id, date, type, ticker, figi, instrument_type, quantity, payment, currency, commission, name)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (op.get('id'), op.get('date'), op.get('type'), op.get('ticker'),
         op.get('figi'), op.get('instrument_type'), op.get('quantity'),
         op.get('payment'), op.get('currency'), op.get('commission'), op.get('name')))
    await db.commit()

async def get_personal_dividends():
    db = await get_db()
    cursor = await db.execute("SELECT date, ticker, payment FROM operations WHERE type IN ('Выплата дивидендов', 'Выплата купонов') AND currency = 'RUB' ORDER BY date")
    rows = await cursor.fetchall()
    result = []
    for r in rows:
        ticker = r[1] if r[1] else "Прочие"
        if ticker == "Прочие":
            name = "Прочие"
        else:
            name = NAME_OVERRIDES.get(ticker)
            if name is None:
                name = ticker_to_name.get(ticker, ticker)
        result.append({"date": r[0], "ticker": name, "amount": r[2]})
    return result

async def get_last_dividends(limit=10):
    db = await get_db()
    cursor = await db.execute("SELECT date, ticker, payment FROM operations WHERE type IN ('Выплата дивидендов', 'Выплата купонов') AND currency = 'RUB' ORDER BY date DESC LIMIT ?", (limit,))
    rows = await cursor.fetchall()
    return [{"date": r[0], "ticker": r[1], "amount": r[2]} for r in rows]

async def get_last_operation_date():
    db = await get_db()
    cursor = await db.execute("SELECT MAX(date) FROM operations")
    row = await cursor.fetchone()
    return row[0] if row else None

async def _get_state(key: str) -> float | None:
    db = await get_db()
    cursor = await db.execute("SELECT value FROM portfolio_state WHERE key = ?", (key,))
    row = await cursor.fetchone()
    return float(row[0]) if row else None

async def _set_state(key: str, value: float):
    db = await get_db()
    await db.execute("INSERT OR REPLACE INTO portfolio_state (key, value) VALUES (?, ?)", (key, value))
    await db.commit()

async def set_portfolio_value(value: float):
    await _set_state("last_total_value", value)

async def get_portfolio_value() -> float | None:
    return await _get_state("last_total_value")

async def set_daily_snapshot(date_str: str, value: float):
    await _set_state(f"snapshot_{date_str}", value)

async def get_daily_snapshot(date_str: str) -> float | None:
    return await _get_state(f"snapshot_{date_str}")

async def load_name_overrides():
    db = await get_db()
    cursor = await db.execute("SELECT ticker, display_name FROM name_overrides")
    rows = await cursor.fetchall()
    NAME_OVERRIDES.clear()
    for ticker, display_name in rows:
        NAME_OVERRIDES[ticker] = display_name
    logging.info(f"Загружено {len(NAME_OVERRIDES)} переопределений названий")

async def set_name_override(ticker: str, display_name: str):
    db = await get_db()
    await db.execute("INSERT OR REPLACE INTO name_overrides (ticker, display_name) VALUES (?, ?)", (ticker, display_name))
    await db.commit()
    await load_name_overrides()

async def remove_name_override(ticker: str):
    db = await get_db()
    await db.execute("DELETE FROM name_overrides WHERE ticker = ?", (ticker,))
    await db.commit()
    await load_name_overrides()

async def get_instrument(ticker: str) -> dict | None:
    db = await get_db()
    cursor = await db.execute("SELECT ticker, name, sector, figi, instrument_type, updated_at FROM instruments WHERE ticker = ?", (ticker,))
    row = await cursor.fetchone()
    if row:
        return {"ticker": row[0], "name": row[1], "sector": row[2], "figi": row[3], "instrument_type": row[4], "updated_at": row[5]}
    return None

async def get_all_instruments():
    db = await get_db()
    cursor = await db.execute("SELECT ticker, name, sector, figi, instrument_type, updated_at FROM instruments")
    rows = await cursor.fetchall()
    return [{"ticker": r[0], "name": r[1], "sector": r[2], "figi": r[3], "instrument_type": r[4], "updated_at": r[5]} for r in rows]

async def upsert_instrument(ticker: str, name: str = None, sector: str = None, figi: str = None,
                            instrument_type: str = None, maturity_date: str = None, coupon_period: int = None):
    db = await get_db()
    cursor = await db.execute("SELECT name, sector, figi, instrument_type, maturity_date, coupon_period FROM instruments WHERE ticker = ?", (ticker,))
    row = await cursor.fetchone()
    existing = {
        "name": row[0] if row else None,
        "sector": row[1] if row else None,
        "figi": row[2] if row else None,
        "instrument_type": row[3] if row else None,
        "maturity_date": row[4] if row else None,
        "coupon_period": row[5] if row else None,
    }
    if sector is None:
        cursor2 = await db.execute("SELECT sector_name FROM sectors WHERE ticker = ?", (ticker,))
        row_sector = await cursor2.fetchone()
        sector = row_sector[0] if row_sector else (existing["sector"] or "Прочие")
    if name is None:
        name = existing["name"] or ticker
    if figi is None:
        figi = existing["figi"]
    if instrument_type is None:
        instrument_type = existing["instrument_type"]
    if maturity_date is None:
        maturity_date = existing["maturity_date"]
    if coupon_period is None:
        coupon_period = existing["coupon_period"]
    await db.execute('''INSERT OR REPLACE INTO instruments
        (ticker, name, sector, figi, instrument_type, updated_at, maturity_date, coupon_period)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)''',
        (ticker, name, sector, figi, instrument_type, maturity_date, coupon_period))
    await db.commit()

async def update_instrument_sector(ticker: str, new_sector: str):
    db = await get_db()
    await db.execute("UPDATE instruments SET sector = ?, updated_at = CURRENT_TIMESTAMP WHERE ticker = ?", (new_sector, ticker))
    await db.execute("INSERT OR REPLACE INTO sectors (ticker, sector_name) VALUES (?, ?)", (ticker, new_sector))
    await db.commit()

async def upsert_dividend_calendar(ticker, figi, dividend_data):
    dividend_net_obj = dividend_data.get("dividendNet", {})
    units = float(dividend_net_obj.get("units", 0))
    nano = float(dividend_net_obj.get("nano", 0)) / 1e9
    dividend_net = units + nano
    db = await get_db()
    await db.execute("""
        INSERT OR REPLACE INTO dividend_calendar
        (ticker, figi, declared_date, record_date, payment_date, dividend_net, dividend_type)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (ticker, figi, dividend_data.get("declaredDate"), dividend_data.get("recordDate"),
         dividend_data.get("paymentDate"), dividend_net, dividend_data.get("dividendType")))
    await db.commit()

async def get_dividend_calendar(ticker=None, year=None, month=None):
    query = "SELECT ticker, figi, declared_date, record_date, payment_date, dividend_net, dividend_type FROM dividend_calendar"
    params = []
    conditions = []
    if ticker:
        conditions.append("ticker = ?")
        params.append(ticker)
    if year and month:
        conditions.append("strftime('%Y', payment_date) = ? AND strftime('%m', payment_date) = ?")
        params.extend([str(year), f"{month:02d}"])
    elif year:
        conditions.append("strftime('%Y', payment_date) = ?")
        params.append(str(year))
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY payment_date DESC"
    db = await get_db()
    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return [{
        "ticker": r[0], "figi": r[1], "declared_date": r[2],
        "record_date": r[3], "payment_date": r[4], "dividend_net": r[5], "dividend_type": r[6]
    } for r in rows]

async def upsert_coupon_calendar(ticker, figi, coupon_data):
    pay_obj = coupon_data.get("payOneBond", {})
    units = float(pay_obj.get("units", 0))
    nano = float(pay_obj.get("nano", 0)) / 1e9
    coupon_value = units + nano
    is_redemption = coupon_data.get("is_redemption", False)
    db = await get_db()
    await db.execute("""
        INSERT OR REPLACE INTO coupon_calendar
        (ticker, figi, coupon_date, coupon_value, coupon_currency, record_date, is_redemption)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (ticker, figi, coupon_data.get("couponDate"), coupon_value,
         coupon_data.get("currency", "RUB"), coupon_data.get("fixDate"),
         1 if is_redemption else 0))
    await db.commit()

async def get_coupon_calendar(ticker=None, year=None, month=None):
    query = "SELECT ticker, figi, coupon_date, coupon_value, coupon_currency, record_date FROM coupon_calendar"
    params = []
    conditions = []
    if ticker:
        conditions.append("ticker = ?")
        params.append(ticker)
    if year and month:
        conditions.append("strftime('%Y', coupon_date) = ? AND strftime('%m', coupon_date) = ?")
        params.extend([str(year), f"{month:02d}"])
    elif year:
        conditions.append("strftime('%Y', coupon_date) = ?")
        params.append(str(year))
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY coupon_date DESC"
    db = await get_db()
    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return [{
        "ticker": r[0], "figi": r[1], "coupon_date": r[2],
        "coupon_value": r[3], "coupon_currency": r[4], "record_date": r[5]
    } for r in rows]

async def get_sector(ticker: str) -> str:
    inst = await get_instrument(ticker)
    if inst and inst['sector']:
        return inst['sector']
    db = await get_db()
    cursor = await db.execute("SELECT sector_name FROM sectors WHERE ticker = ?", (ticker,))
    row = await cursor.fetchone()
    return row[0] if row else "Прочие"

async def upsert_dividend_forecasts(ticker: str, forecasts: list[dict]):
    db = await get_db()
    await db.execute("DELETE FROM dividend_forecast WHERE ticker = ?", (ticker,))
    for f in forecasts:
        await db.execute('''INSERT OR REPLACE INTO dividend_forecast
            (ticker, forecast_amount, forecast_month, forecast_year, confidence_score, method, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)''',
            (ticker, f["amount"], f["month"], f["year"], f["confidence"], f["method"]))
    await db.commit()
    logging.info(f"Обновлено {len(forecasts)} прогнозов для {ticker}")

async def get_dividend_forecast(ticker: str = None, year: int = None):
    db = await get_db()
    parts = ["SELECT ticker, forecast_amount, forecast_month, forecast_year, confidence_score, method, updated_at FROM dividend_forecast"]
    params = []
    conds = []
    if ticker is not None:
        conds.append("ticker = ?")
        params.append(ticker)
    if year is not None:
        conds.append("forecast_year = ?")
        params.append(year)
    if conds:
        parts.append("WHERE " + " AND ".join(conds))
    parts.append("ORDER BY forecast_year, forecast_month")
    cursor = await db.execute(" ".join(parts), params)
    rows = await cursor.fetchall()
    return [{"ticker": r[0], "amount": r[1], "month": r[2], "year": r[3], "confidence": r[4], "method": r[5], "updated_at": r[6]} for r in rows]

async def set_last_sync_time(timestamp: str):
    await _set_state("last_sync_time", timestamp)

async def get_last_sync_time() -> str | None:
    db = await get_db()
    cursor = await db.execute("SELECT value FROM portfolio_state WHERE key = ?", ("last_sync_time",))
    row = await cursor.fetchone()
    return row[0] if row else None

async def operation_exists(op_id: str) -> bool:
    db = await get_db()
    cursor = await db.execute("SELECT id FROM operations WHERE id = ?", (op_id,))
    return (await cursor.fetchone()) is not None
